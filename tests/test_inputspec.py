"""input_spec: ints, floats and text as single values, lists or matrices; an unusable spec never blocks grading."""

import json
import unittest
from unittest import mock

from autograder.agent import GradingAgent
from autograder.llm import LLMClient, LLMError
from autograder.inputgen import InputGenerator, InputSpecError, canonical_type
from autograder.rules import load_rules

from .helpers import TempDirCase, toolchain_or_skip

MATRIX = {"variables": [
    {"name": "n", "type": "int", "min": 1, "max": 50}, {"name": "m", "type": "int", "min": 1, "max": 50},
    {"name": "A", "type": "int[][]", "rows": "n", "cols": "m", "min": -100, "max": 100},
    {"name": "B", "type": "matrix", "rows": "n", "cols": "m", "min": -100, "max": 100}],
    "layout": [["n", "m"], ["A"], ["B"]]}


class InputSpecTypesTest(unittest.TestCase):
    def test_type_names(self):
        self.assertEqual([canonical_type(t) for t in ("int", "Integer", "double", "string", "int[]", "float[][]",
                                                      "matrix", "list", "long long", "char", "char[]", "char[][]")],
                         ["int", "int", "float", "str", "int[]", "float[][]", "int[][]", "int[]", "int",
                          "char", "str", "char[][]"])
        for bad in ("tensor", "int[][][]", "bool[]"):
            with self.assertRaises(InputSpecError):
                canonical_type(bad)

    def test_matrix_parse_render_and_size(self):
        g = InputGenerator(MATRIX, "seed")
        self.assertEqual(g.size_var, "n")
        text = "2 3\n1 2 3\n4 5 6\n7 8 9\n1 1 1\n"
        values = g.parse(text)
        self.assertEqual(values["A"], [[1, 2, 3], [4, 5, 6]])
        self.assertEqual(g.render(values), text)  # one matrix row per line
        self.assertIsNone(g.parse("2 3\n1 2 3\n4 5\n7 8 9\n1 1 1\n"))   # a short row
        self.assertIsNone(g.parse("2 3\n1 2 3\n4 5 600\n7 8 9\n1 1 1\n"))  # out of range
        self.assertIsNotNone(g.parse(g.for_size(6).text))

    def test_floats_and_text(self):
        g = InputGenerator({"variables": [
            {"name": "k", "type": "int", "min": 1, "max": 5},
            {"name": "xs", "type": "double[]", "length": "k", "min": -1.5, "max": 1.5},
            {"name": "w", "type": "string", "min_length": 2, "max_length": 4, "charset": "ab"},
            {"name": "ws", "type": "str[]", "length": 2, "min": 1, "max": 3, "charset": "xy"}]}, "seed")
        values = g.parse("2\n0.5 -1.25\nab\nx yy\n")
        self.assertEqual(values["xs"], [0.5, -1.25])
        self.assertIsNone(g.parse("2\n0.5 9.5\nab\nx yy\n"))   # float out of range
        self.assertIsNone(g.parse("2\n0.5 1.0\nabc2\nx yy\n"))  # text outside the charset

    def test_sized_inputs_keep_the_other_dimensions_fixed(self):
        g = InputGenerator(dict(MATRIX, variables=[dict(v, role="size") if v["name"] == "m" else v
                                                   for v in MATRIX["variables"]]), "seed")
        self.assertEqual(g.size_var, "m")
        cases = [g.for_size(n) for n in (1, 10, 40)]
        self.assertEqual([g.size_of(c.values) for c in cases], [1, 10, 40])
        self.assertEqual(len({c.values["n"] for c in cases}), 1)           # the rows never change
        self.assertEqual(g.size_of(g.for_size(5000).values), 50)           # asking past the maximum is clamped

    def test_every_common_type(self):
        """int, float, str, char, char[], int[], float[], int[][], float[][] and a char grid, in one input."""
        g = InputGenerator({"variables": [
            {"name": "a", "type": "int", "min": -50, "max": 50},
            {"name": "x", "type": "float", "min": -10, "max": 10},
            {"name": "s", "type": "str", "min_length": 1, "max_length": 12, "charset": "abc"},
            {"name": "c", "type": "char", "charset": "ABC"},
            {"name": "w", "type": "char[]", "min_length": 2, "max_length": 6},
            {"name": "k", "type": "int", "min": 1, "max": 5},
            {"name": "t", "type": "str", "length": "k", "charset": "xy"},
            {"name": "ia", "type": "int[]", "length": 3, "min": 0, "max": 9},
            {"name": "fa", "type": "float[]", "length": 2, "min": -1, "max": 1},
            {"name": "im", "type": "int[][]", "rows": 2, "cols": 2, "min": 0, "max": 9},
            {"name": "fm", "type": "float[][]", "rows": 2, "cols": 2, "min": -1, "max": 1},
            {"name": "g", "type": "char[][]", "rows": 2, "cols": 3, "charset": "#."}],
            "layout": [[n] for n in ("a", "x", "s", "c", "w", "k", "t", "ia", "fa", "im", "fm", "g")]}, "seed")
        text = "5\n3.14\nabcab\nA\nworld\n3\nxyx\n1 2 3\n0.5 -0.25\n1 2\n3 4\n0.5 0.25\n-0.5 -1\n#.#\n..#\n"
        values = g.parse(text)
        self.assertEqual((values["x"], values["c"], values["w"], values["g"]), (3.14, "A", "world", [list("#.#"), list("..#")]))
        self.assertEqual(g.render(values), text)  # the grid prints as "#.#", not "# . #"
        self.assertIsNone(g.parse(text.replace("\nA\n", "\nAB\n")))        # a char is exactly one character
        self.assertIsNone(g.parse(text.replace("xyx", "xy")))                # a text of length k
        self.assertIsNone(g.parse(text.replace("0.5 -0.25", "0.5 -2.5")))    # a float out of range

    def test_mistakes_are_explained(self):
        for spec, hint in (
            ({"variables": [{"name": "A", "type": "tensor"}]}, "unsupported type"),
            ({"variables": [{"name": "A", "type": "int[][]", "rows": "n"}]}, '"rows" and "cols"'),
            ({"variables": [{"name": "A", "type": "int[]", "length": "n"}]}, "declared before"),
            ({"variables": [{"name": "a"}], "layout": [["b"]]}, "unknown variable"),
        ):
            with self.assertRaises(InputSpecError) as ctx:
                InputGenerator(spec, "x")
            self.assertIn(hint, str(ctx.exception))


class UnusableSpecTest(TempDirCase):
    def test_grading_continues_when_the_spec_cannot_be_used(self):
        toolchain = toolchain_or_skip()
        tests = self.tmp / "tests"
        tests.mkdir()
        (tests / "01.in").write_text("7 5\n")
        (tests / "01.out").write_text("12\n")
        rules = {"problem": "add", "sample": {"input": "7 5\n", "output": "12\n"},
                 "input_spec": {"variables": [{"name": "t", "type": "tensor"}]},
                 "metrics": [{"name": "correctness", "analyzer": "io_tests", "weight": 0.5, "params": {}},
                             {"name": "logical", "analyzer": "edge_case_runner", "weight": 0.5, "params": {}}]}
        (self.tmp / "rules.json").write_text(json.dumps(rules))
        src = self.write_c("add.c", '#include <stdio.h>\nint main(void){int a,b;scanf("%d %d",&a,&b);'
                                    'printf("%d\\n",a+b);return 0;}\n')
        def no_llm(self, system, user):
            raise LLMError("cannot reach the model")

        with mock.patch.object(LLMClient, "chat", no_llm), \
                GradingAgent(load_rules(self.tmp / "rules.json"), toolchain, cache_dir=self.tmp / "cache") as agent:
            report = agent.grade(src).report
        metrics = {m["name"]: m for m in report["metrics"]}
        self.assertEqual(metrics["correctness"]["score"], 1.0)            # still graded
        self.assertEqual(metrics["logical"]["status"], "skipped_environment")  # weight redistributed
        self.assertEqual(report["grade"], 100.0)
        self.assertIn("input_spec_ignored", [f["type"] for f in report["flags"]])


if __name__ == "__main__":
    unittest.main()
