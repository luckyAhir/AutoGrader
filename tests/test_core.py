"""Unit tests for the pure-Python parts (no compiler needed)."""

import json
import unittest

from autograder.analyzers.complexity_estimator import fit_growth, normalize_bound, parse_gcov
from autograder.compare import compare_outputs
from autograder.csource import parse
from autograder.explain import explain
from autograder.inputgen import InputGenerator
from autograder.rules import RulesError, load_rules
from autograder.similarity import fingerprint, overlap
from autograder.util import round_half_up

from .helpers import ADD_RULES, SORT_RULES, TempDirCase

SORT_SPEC = {
    "variables": [
        {"name": "n", "type": "int", "min": 0, "max": 1000, "role": "size"},
        {"name": "values", "type": "int[]", "length": "n", "min": -2147483648, "max": 2147483647},
    ],
    "layout": [["n"], ["values"]],
}


class CompareTest(unittest.TestCase):
    def test_modes(self):
        self.assertTrue(compare_outputs("12\n", "12", "lines").ok)
        self.assertTrue(compare_outputs("1 2\n", "1 2   \n\n", "lines").ok)
        self.assertEqual(compare_outputs("1 2\n", "1  2\n", "lines").verdict, "PE")
        self.assertEqual(compare_outputs("12\n", "13\n", "lines").verdict, "WA")
        self.assertTrue(compare_outputs("0.3333333\n", "0.33333334\n", "float").ok)
        self.assertEqual(compare_outputs("12\n", "12", "exact").verdict, "PE")
        self.assertTrue(compare_outputs("1\r\n2\r\n", "1\n2\n", "exact").ok)


class RulesTest(TempDirCase):
    def test_example_rules_load(self):
        rules = load_rules(ADD_RULES)
        self.assertEqual(rules.problem, "add_two_integers")
        self.assertAlmostEqual(sum(m.weight for m in rules.metrics), 1.0)
        self.assertTrue(rules.rules_hash.startswith("sha256:"))
        self.assertEqual(rules.rules_hash, load_rules(ADD_RULES).rules_hash)
        load_rules(SORT_RULES)

    def test_bad_weights_rejected(self):
        raw = json.loads(ADD_RULES.read_text(encoding="utf-8"))
        raw["metrics"][0]["weight"] = 0.5
        raw.pop("reference_solution")
        raw.pop("statement_file")
        path = self.tmp / "rules.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(RulesError) as ctx:
            load_rules(path)
        self.assertIn("must sum to 1.00", str(ctx.exception))

    def test_invalid_json_reports_position(self):
        path = self.tmp / "rules.json"
        path.write_text('{"problem": "x", "metrics": [}', encoding="utf-8")
        with self.assertRaises(RulesError) as ctx:
            load_rules(path)
        self.assertIn("line 1", str(ctx.exception))


class InputGenTest(unittest.TestCase):
    def test_size_scaling_without_size_var(self):
        spec = {"variables": [{"name": "a", "type": "int"}, {"name": "b", "type": "int"}]}
        self.assertEqual(InputGenerator(spec, "s").for_size(10).text, "10\n10\n")


class ExplainTest(unittest.TestCase):
    def test_overflow(self):
        e = explain("WA", "2147483647 1\n", "2147483648\n", "-2147483648\n")
        self.assertEqual(e.tag, "int_overflow")
        self.assertIn("long long", e.hint)

    def test_wrong_order_comparator(self):
        e = explain("WA", "2\n1 -2147483648\n", "-2147483648 1\n", "1 -2147483648\n")
        self.assertEqual(e.tag, "wrong_order")
        self.assertIn("(x > y) - (x < y)", e.hint)

    def test_prompt_text(self):
        e = explain("WA", "7 5\n", "12\n", "Enter two numbers: Sum = 12\n")
        self.assertEqual(e.tag, "extra_text")

    def test_infinite_loop_pattern(self):
        source = "while (scanf(\"%d\", &v) != 0) total += v;"
        e = explain("TLE", "7 5\n", "12\n", "", source=source)
        self.assertEqual(e.tag, "infinite_loop")
        self.assertIn("EOF", e.hint)


class SimilarityTest(unittest.TestCase):
    A = """#include <stdio.h>
static long long total(const int *v, int n) { long long s = 0; for (int i = 0; i < n; i++) { if (v[i] > 0) s += v[i]; } return s; }
int main(void) { int n; int v[100]; if (scanf("%d", &n) != 1) return 1; for (int i = 0; i < n; i++) scanf("%d", &v[i]);
printf("%lld\\n", total(v, n)); return 0; }"""

    def test_renamed_copy_is_similar(self):
        renamed = (self.A.replace("total", "acc").replace(" s ", " sum ").replace("s +=", "sum +=")
                   .replace("return s;", "return sum;").replace("v[", "arr[").replace("*v", "*arr"))
        a, b = fingerprint(parse(self.A)), fingerprint(parse("// copied\n" + renamed))
        self.assertGreaterEqual(overlap(a.keys, b.keys), 0.9)

    def test_different_program_is_not(self):
        other = """#include <stdio.h>
int main(void){ char line[64]; while (fgets(line, sizeof line, stdin)) { puts(line); } return 0; }"""
        a, b = fingerprint(parse(self.A)), fingerprint(parse(other))
        self.assertLess(overlap(a.keys, b.keys), 0.3)


class ComplexityFitTest(unittest.TestCase):
    sizes = [500, 1000, 2000, 4000, 8000]

    def test_models(self):
        import math

        self.assertEqual(fit_growth(self.sizes, [7] * 5)["estimate"], "O(1)")
        self.assertEqual(fit_growth(self.sizes, [100 + 5 * n for n in self.sizes])["estimate"], "O(n)")
        self.assertEqual(fit_growth(self.sizes, [n * math.log2(n) - 1.25 * n for n in self.sizes])["estimate"],
                         "O(n log n)")
        self.assertEqual(fit_growth(self.sizes, [n * n // 2 + 3 * n for n in self.sizes])["estimate"], "O(n^2)")

    def test_bounds_and_gcov_parsing(self):
        self.assertEqual(normalize_bound("O(n*log n)"), "O(n log n)")
        self.assertEqual(normalize_bound("O(n²)"), "O(n^2)")
        # bounds in several variables reduce to the class the estimator measures (one dimension at a time)
        for written, measured in (("O(n*m)", "O(n)"), ("O(nm)", "O(n)"), ("O(n + m)", "O(n)"),
                                  ("O(n^2 * m)", "O(n^2)"), ("O(m log n)", "O(n log n)"), ("O(V+E)", "O(n)")):
            self.assertEqual(normalize_bound(written), measured, written)
        for bad in ("fast", "O(n^5)", "O()"):
            with self.assertRaises(ValueError):
                normalize_bound(bad)
        counts = parse_gcov("        -:    0:Source:a.c\n        5:    3:  x++;\n    #####:    4:  y++;\n"
                            "       12*:    5:  for (;;)\n")
        self.assertEqual({k: v[0] for k, v in counts.items()}, {3: 5, 4: 0, 5: 12})


class RoundingTest(unittest.TestCase):
    def test_half_up(self):
        self.assertEqual(round_half_up(88.25, 1), 88.3)
        self.assertEqual(round_half_up(0.05, 1), 0.1)


if __name__ == "__main__":
    unittest.main()
