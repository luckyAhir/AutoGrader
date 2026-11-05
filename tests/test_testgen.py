"""llm_tests: tests an LLM designs from the statement + samples (the model is faked, the C code really runs)."""

import json
import re
from unittest import mock

from autograder import testgen
from autograder.agent import GradingAgent
from autograder.llm import LLMClient, LLMError, LLMReply
from autograder.rules import load_rules

from .helpers import ROOT, TempDirCase, toolchain_or_skip

SUBS = ROOT / "submissions" / "max_subarray_sum"

RULES = {
    "problem": "max_subarray_sum",
    "statement": "Print the largest sum of a non-empty contiguous subarray of n integers.",
    "sample": [{"input": "4\n1 -2 3 1\n", "output": "4\n"}, {"input": "3\n-5 -2 -7\n", "output": "-2\n"}],
    "input_spec": {"variables": [{"name": "n", "type": "int", "min": 1, "max": 1000, "role": "size"},
                                 {"name": "a", "type": "int[]", "length": "n", "min": -1000, "max": 1000}],
                   "layout": [["n"], ["a"]]},
    "metrics": [{"name": "correctness", "analyzer": "llm_tests", "weight": 1.0,
                 "params": {"num_tests": 8}}],
}


def fake_chat(self, system, user):
    tests = [{"name": "single element", "input": "1\n5\n", "expected": "5\n"},
             {"name": "zeros", "input": "3\n0 0 0\n", "expected": 0},
             {"name": "bad", "input": "2\n1\n", "expected": "1\n"},          # breaks the input_spec
             {"name": "no_expected", "input": "1\n9\n"},                       # malformed
             {"name": "zero among negatives", "input": "3\n-1 0 -2\n", "expected": "0"},
             {"name": "dup", "input": "1\n5\n", "expected": "5\n"}]            # duplicate input
    return LLMReply(json.dumps({"tests": tests}), False, self.provider, self.model, "k")


KADANE_C = r"""#include <stdio.h>
int main(void){int n; if(scanf("%d",&n)!=1) return 1; int x,best=0,cur=0;
for(int i=0;i<n;i++){ if(scanf("%d",&x)!=1) return 1; cur=(i==0||cur<0)?x:cur+x; if(i==0||cur>best) best=cur;}
printf("%d \n",best); return 0;}"""


def solution_chat(self, system, user):
    """The model writes a right solution but computes the expected outputs of its test cases wrongly."""
    if system == testgen.SOLUTION_SYSTEM:
        return LLMReply("```c\n" + KADANE_C + "\n```", False, self.provider, self.model, "k")
    tests = [{"name": "wrong_sum", "input": "3\n1 2 3\n", "expected": "7\n"},
             {"name": "negatives", "input": "3\n-4 -1 -3\n", "expected": "-3\n"}]
    return LLMReply(json.dumps({"tests": tests}), False, self.provider, self.model, "k")


def garbage_chat(self, system, user):
    return LLMReply("I cannot help with that", False, self.provider, self.model, "k")


def no_llm(self, system, user):
    raise LLMError("cannot reach the model: the model must not be asked again")


class LLMTestsTest(TempDirCase):
    @classmethod
    def setUpClass(cls):
        cls.toolchain = toolchain_or_skip()

    def setUp(self):
        super().setUp()
        self.rules_path = self.tmp / "problem" / "rules.json"
        self.rules_path.parent.mkdir()
        self.write_rules(RULES)

    def write_rules(self, raw):
        self.rules_path.write_text(json.dumps(raw), encoding="utf-8")

    def grade(self, submission):
        with GradingAgent(load_rules(self.rules_path), self.toolchain, cache_dir=self.tmp / "cache") as agent:
            return agent.grade(submission).report

    def cases_file(self):
        return self.rules_path.parent / "test_cases.json"

    def test_writes_test_cases_json_then_reuses_it(self):
        with mock.patch.object(LLMClient, "chat", fake_chat):
            report = self.grade(SUBS / "kadane_zero_start.c")
        cases = json.loads(self.cases_file().read_text())
        self.assertEqual([c["name"] for c in cases],
                         ["sample_1", "sample_2", "single_element", "zeros", "zero_among_negatives"])
        self.assertTrue(all(set(c) == {"name", "input", "expected"} for c in cases))
        self.assertEqual(cases[3]["expected"], "0\n")   # a number is normalised to a line
        self.assertEqual(cases[4]["expected"], "0\n")

        metric = report["metrics"][0]
        self.assertEqual(metric["evidence"]["tests_total"], 5)
        self.assertEqual(metric["evidence"]["tests_passed"], 4)  # fails only the all-negative sample
        self.assertIn("sample_2", report["counterexamples"][0]["label"])

        with mock.patch.object(LLMClient, "chat", no_llm):  # file exists: no model call, same report
            again = self.grade(SUBS / "kadane_zero_start.c")
            self.assertEqual(json.dumps(report, sort_keys=True), json.dumps(again, sort_keys=True))
            self.assertEqual(self.grade(SUBS / "kadane.c")["grade"], 100.0)

    def test_expected_outputs_come_from_the_models_solution_when_it_matches_the_samples(self):
        with mock.patch.object(LLMClient, "chat", solution_chat):
            self.grade(SUBS / "kadane.c")
        cases = {c["name"]: c["expected"] for c in json.loads(self.cases_file().read_text())}
        self.assertEqual(cases["wrong_sum"], "6\n")   # the model said 7; its own program says 6
        self.assertEqual(cases["negatives"], "-1\n")  # and the model's -3 is corrected; no trailing spaces

    def edge_rules(self):
        raw = json.loads(json.dumps(RULES))
        raw["metrics"] = [{"name": "logical", "analyzer": "edge_case_runner", "weight": 1.0,
                           "params": {"cases": ["single element", "zero"]}}]
        return raw

    def test_edge_cases_are_written_to_edge_case_json_and_graded(self):
        self.write_rules(self.edge_rules())
        prompts = []

        def chat(client, system, user):
            prompts.append((system, user))
            return solution_chat(client, system, user)

        with mock.patch.object(LLMClient, "chat", chat):
            report = self.grade(SUBS / "kadane_zero_start.c")
        edge = json.loads((self.rules_path.parent / "edge_case.json").read_text())
        self.assertEqual([c["name"] for c in edge], ["wrong_sum", "negatives"])   # no samples in the file
        self.assertEqual([c["expected"] for c in edge], ["6\n", "-1\n"])         # from the model's solution
        self.assertFalse(self.cases_file().exists())                                # tests are a separate file
        self.assertIn("single element, zero", next(u for s, u in prompts if s == testgen.EDGE_SYSTEM))
        metric = report["metrics"][0]
        self.assertEqual(metric["status"], "ok")
        self.assertEqual(metric["evidence"]["edge_cases_total"], 2)
        self.assertEqual(metric["evidence"]["edge_cases_passed"], 1)  # kadane_zero_start fails all-negative data

        with mock.patch.object(LLMClient, "chat", no_llm):  # the file exists: nothing is asked again
            self.assertEqual(self.grade(SUBS / "kadane.c")["grade"], 100.0)

    def test_edge_metric_is_skipped_when_the_model_gives_no_edge_case(self):
        self.write_rules(self.edge_rules())
        with mock.patch.object(LLMClient, "chat", garbage_chat):
            report = self.grade(SUBS / "kadane.c")
        self.assertEqual(report["metrics"][0]["status"], "skipped_environment")
        self.assertFalse((self.rules_path.parent / "edge_case.json").exists())

    def test_input_format_for_the_complexity_check_comes_from_the_model_when_the_rules_have_none(self):
        raw = json.loads(json.dumps(RULES))
        del raw["input_spec"]
        raw["metrics"] = [{"name": "complexity", "analyzer": "complexity_estimator", "weight": 1.0,
                           "params": {"required": "O(n)", "sizes": [100, 200, 400, 800]}}]
        self.write_rules(raw)
        spec = {"variables": [{"name": "n", "type": "int", "min": 1, "max": 1000, "role": "size"},
                              {"name": "a", "type": "int[]", "length": "n", "min": -9, "max": 9}],
                "layout": [["n"], ["a"]]}
        bad = {"variables": [{"name": "n", "type": "int"}], "layout": [["n"]]}   # does not parse the samples
        replies = iter([bad, spec])

        def chat(client, system, user):
            self.assertEqual(system, testgen.SPEC_SYSTEM)
            return LLMReply(json.dumps(next(replies)), False, client.provider, client.model, "k")

        with mock.patch.object(LLMClient, "chat", chat):
            report = self.grade(SUBS / "kadane.c")
        self.assertEqual(json.loads((self.rules_path.parent / "input_spec.json").read_text()), spec)
        self.assertNotEqual(report["metrics"][0]["status"], "skipped_environment")

        with mock.patch.object(LLMClient, "chat", no_llm):  # the file exists: reused
            self.assertNotEqual(self.grade(SUBS / "kadane.c")["metrics"][0]["status"], "skipped_environment")

    def test_samples_are_still_graded_when_the_model_gives_nothing_usable(self):
        with mock.patch.object(LLMClient, "chat", garbage_chat):
            report = self.grade(SUBS / "kadane_zero_start.c")
        metric = report["metrics"][0]
        self.assertEqual(metric["status"], "ok")  # not dropped: the two samples are ground truth
        self.assertEqual((metric["evidence"]["tests_total"], metric["evidence"]["tests_passed"]), (2, 1))
        self.assertIn("samples_only_tests", [f["type"] for f in report["flags"]])

    def test_parse_cases_accepts_arrays_and_wrapped_lists(self):
        item = {"name": "a", "input": "1\n", "expected": "1\n"}
        self.assertEqual(testgen.parse_cases(json.dumps([item])), [item])
        self.assertEqual(testgen.parse_cases("```json\n" + json.dumps({"cases": [item]}) + "\n```"), [item])
        self.assertEqual(testgen.parse_cases("nothing"), [])

    def test_statement_is_the_problem_field_when_it_is_prose(self):
        raw = {k: v for k, v in RULES.items() if k != "statement"}
        raw["problem"] = "Print the sum of two integers.\nInput: a b"
        self.write_rules(raw)
        rules = load_rules(self.rules_path)
        self.assertEqual(rules.statement, raw["problem"])
        self.assertEqual(rules.problem, "problem")  # a name safe for report folders

    def test_input_spec_that_contradicts_the_samples_is_ignored(self):
        wrong = dict(RULES, input_spec={"variables": [{"name": "n", "type": "int"}], "layout": [["n"]]})
        self.write_rules(wrong)
        with mock.patch.object(LLMClient, "chat", fake_chat):
            report = self.grade(SUBS / "kadane.c")
        self.assertEqual(report["metrics"][0]["evidence"]["tests_total"], 6)  # 2 samples + 4 usable

    def test_a_sample_with_an_output_is_required(self):
        raw = dict(RULES, sample=[{"input": "1\n5\n"}])
        self.write_rules(raw)
        with mock.patch.object(LLMClient, "chat", fake_chat):
            report = self.grade(SUBS / "kadane.c")
        self.assertEqual(report["metrics"][0]["status"], "skipped_environment")
        self.assertIn("sample", report["metrics"][0]["summary"])

    def test_unreachable_model_skips_the_metric(self):
        with mock.patch.object(LLMClient, "chat", no_llm):
            report = self.grade(SUBS / "kadane.c")
        self.assertEqual(report["metrics"][0]["status"], "skipped_environment")
        self.assertIn("metric_not_evaluated", [f["type"] for f in report["flags"]])
