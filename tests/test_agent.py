"""End-to-end: the whole agent on real submissions (needs a C compiler)."""

import json
import unittest

from autograder.agent import GradingAgent
from autograder.rules import load_rules

from .helpers import ADD_RULES, ROOT, SORT_RULES, TempDirCase, offline_rules, toolchain_or_skip

ADD = ROOT / "submissions" / "add_two_integers"

PLUGIN = '''
from autograder.analyzers.base import Analyzer, AnalyzerResult, register

@register
class LineCount(Analyzer):
    name = "line_count"
    description = "Penalise very long programs"

    def run(self, ctx, params):
        lines = len(ctx.csource.code_lines())
        limit = int(params.get("max_lines", 80))
        score = 1.0 if lines <= limit else max(0.0, 1 - (lines - limit) / limit)
        return AnalyzerResult(score, f"{lines} code lines", f"{lines} lines of code (limit {limit}).",
                              {"lines": lines, "limit": limit})
'''


class AgentTest(TempDirCase):
    @classmethod
    def setUpClass(cls):
        cls.toolchain = toolchain_or_skip()

    def grade(self, rules_path, submission):
        with GradingAgent(load_rules(rules_path), self.toolchain, cache_dir=self.tmp / "cache") as agent:
            return agent.grade(submission)

    def test_reproducible_grade(self):
        rules = offline_rules(ADD_RULES, self.tmp)
        first = self.grade(rules, ADD / "add.c")
        second = self.grade(rules, ADD / "add.c")
        self.assertEqual(json.dumps(first.report, sort_keys=True), json.dumps(second.report, sort_keys=True))

    def test_sample_submission_findings(self):
        report = self.grade(offline_rules(ADD_RULES, self.tmp), ADD / "add.c").report
        metrics = {m["name"]: m for m in report["metrics"]}
        self.assertEqual(metrics["correctness"]["evidence"]["tests_passed"], 8)
        self.assertEqual(metrics["modularity"]["evidence"]["functions_defined"], 1)
        failing = report["counterexamples"][0]
        self.assertEqual(failing["input"], "2147483647 1\n")
        self.assertEqual(failing["expected"], "2147483648\n")
        self.assertIn("32-bit", failing["why"])
        self.assertTrue(report["first_pass"]["smoke_ok"])
        self.assertLess(report["grade"], 90)

    def test_infinite_loop_gives_partial_report(self):
        report = self.grade(offline_rules(ADD_RULES, self.tmp), ADD / "add_infinite_loop.c").report
        self.assertTrue(report["first_pass"]["suspected_infinite_loop"])
        statuses = {m["name"]: m["status"] for m in report["metrics"]}
        for name in ("correctness", "logical", "complexity", "memory_safety"):
            self.assertEqual(statuses[name], "skipped_submission")
        self.assertEqual(statuses["readability"], "ok")
        self.assertIn("suspected_infinite_loop", {f["type"] for f in report["flags"]})

    def test_compile_error(self):
        broken = self.write_c("broken.c", "#include <stdio.h>\nint main(void) { printf(\"x\\n\") return 0; }\n")
        report = self.grade(offline_rules(ADD_RULES, self.tmp), broken).report
        self.assertFalse(report["first_pass"]["compiled"])
        metrics = {m["name"]: m for m in report["metrics"]}
        self.assertEqual(metrics["syntax"]["score"], 0.0)
        self.assertEqual(metrics["correctness"]["status"], "skipped_submission")

    def test_copy_is_flagged(self):
        report = self.grade(offline_rules(ADD_RULES, self.tmp), ADD / "add_copied.c").report
        flags = [f for f in report["flags"] if f["type"] == "suspected_plagiarism"]
        self.assertTrue(flags)
        self.assertEqual(flags[0]["peer"], "add_robust.c")

    def test_plugin_analyzer_from_rules(self):
        """A new metric is added through the rules alone (the example in docs/rules_reference.md)."""
        rules_path = offline_rules(ADD_RULES, self.tmp)
        (rules_path.parent / "line_count.py").write_text(PLUGIN, encoding="utf-8")
        raw = json.loads(rules_path.read_text(encoding="utf-8"))
        raw["plugins"] = ["line_count.py"]
        raw["metrics"][0]["weight"] = 0.15
        raw["metrics"].append({"name": "brevity", "analyzer": "line_count", "weight": 0.05,
                               "params": {"max_lines": 60}})
        rules_path.write_text(json.dumps(raw), encoding="utf-8")
        report = self.grade(rules_path, ADD / "add.c").report
        brevity = next(m for m in report["metrics"] if m["name"] == "brevity")
        # add.c has 7 lines; the #include directive is not a code-token line
        self.assertEqual((brevity["score"], brevity["evidence"]["lines"]), (1.0, 6))

    def test_quadratic_sort_detected(self):
        report = self.grade(offline_rules(SORT_RULES, self.tmp),
                            ROOT / "submissions" / "sort_integers" / "sort_bubble.c").report
        cx = next(m for m in report["metrics"] if m["name"] == "complexity")
        if self.toolchain.gcov:  # exact operation counts: the class is certain
            self.assertEqual(cx["evidence"]["estimated"], "O(n^2)")
        self.assertLess(cx["score"], 1.0)


if __name__ == "__main__":
    unittest.main()
