import unittest

from autograder.csource import normalized_tokens, parse

SAMPLE = r'''
/* file header */
#include <stdio.h>
#define LIMIT 10
typedef struct node { int value; struct node *next; } Node;
static int counter = 0;
static const int answer = 42;

int add(int lhs, int rhs);

/* adds */
int add(int lhs, int rhs) { return lhs + rhs; }

static int cmp(const void *a, const void *b, int (*fn)(int, int))
{
    int x = *(const int *)a, y = *(const int *)b;
    if (x < y && fn) return -1;
    else if (x > y || !fn) return 1;
    return x == y ? 0 : 2;
}

int main(void)
{
    int total = 0, i;
    char text[] = "a { brace } in a string // not a comment";
    for (i = 0; i < LIMIT; i++) {
        for (int j = 0; j < i; j++) {
            if (j % 2) { total += add(i, j); }
        }
    }
    while (total > 100) total /= 2;
    printf("%d %s\n", total, text);
    return 0;
}
'''


class CSourceTest(unittest.TestCase):
    def setUp(self):
        self.src = parse(SAMPLE)

    def test_functions_found(self):
        self.assertEqual([f.name for f in self.src.functions], ["add", "cmp", "main"])
        self.assertIn("add", self.src.prototypes)

    def test_cyclomatic_complexity(self):
        cmp = self.src.function("cmp")
        # 1 + if + && + if + || + ?  = 6
        self.assertEqual(cmp.cyclomatic, 6)
        main = self.src.function("main")
        # 1 + for + for + if + while = 5
        self.assertEqual(main.cyclomatic, 5)

    def test_nesting_and_params(self):
        self.assertEqual(self.src.function("main").max_nesting, 3)
        self.assertEqual(self.src.function("cmp").params, ["a", "b", "fn"])
        self.assertEqual(self.src.function("main").params, [])

    def test_declarations(self):
        names = {(d.name, d.kind) for d in self.src.declarations}
        self.assertIn(("total", "local"), names)
        self.assertIn(("text", "local"), names)
        self.assertIn(("x", "local"), names)
        self.assertIn(("y", "local"), names)
        globals_ = {d.name: d.is_const for d in self.src.globals}
        self.assertEqual(globals_, {"counter": False, "answer": True})
        counters = {d.name for d in self.src.declarations if d.is_loop_counter}
        self.assertEqual(counters, {"i", "j"})
        self.assertIn("Node", self.src.typedef_names)

    def test_comments_strings_and_directives(self):
        self.assertEqual(len(self.src.comments), 2)
        self.assertEqual(self.src.includes, ["stdio.h"])
        self.assertTrue(any("LIMIT" in d.text for d in self.src.directives))
        # braces inside the string literal must not confuse function extraction
        self.assertEqual(self.src.function("main").calls, ["add", "printf"])

    def test_normalisation_ignores_names_and_comments(self):
        a = parse("int main(void){int apples=3; /* c */ printf(\"%d\", apples); return 0;}")
        b = parse("int main(void){\n  int pears = 7;\n  printf(\"x=%d\", pears);\n  return 0;\n}")
        self.assertEqual([t for t, _ in normalized_tokens(a)], [t for t, _ in normalized_tokens(b)])

    def test_broken_code_does_not_crash(self):
        src = parse("int main( { if (x { return 0; ")
        self.assertIsNotNone(src.tokens)


if __name__ == "__main__":
    unittest.main()
