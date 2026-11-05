"""correctness: run the submission on the problem's test set.

The test set is ``testset_dir`` (``NN.in`` + ``NN.out``). When that folder has no
tests, they are generated at runtime before the first submission is graded
(see ``autograder.testgen``): an LLM designs test cases (input and expected
output) from the problem statement and the samples in the rules.
"""

from __future__ import annotations

from collections import Counter

from .. import testgen
from ..util import one_line
from ._common import evidence_view, failure_entry, hints
from .base import SKIPPED_ENVIRONMENT, Analyzer, AnalyzerResult, register


@register
class IOTests(Analyzer):
    name = "io_tests"
    description = "Test set run in the sandbox (designed by an LLM from the statement and samples when none exists)"
    needs_binary = True
    default_dir = "tests/"

    def prepare(self, agent, params):
        folder = agent.rules.resolve(params.get("testset_dir", self.default_dir))
        if folder.is_dir() and any(folder.glob("*.in")):
            return None  # instructor-written tests
        return testgen.prepare(agent, dict(params, testset_dir=params.get("testset_dir", self.default_dir)))

    def run(self, ctx, params):
        error = (params.get("_prepared") or {}).get("error")
        if error:
            return AnalyzerResult.skipped(SKIPPED_ENVIRONMENT, f"test generation failed: {error[:200]}")
        testset_dir = params.get("testset_dir", self.default_dir)
        tests = [t for t in ctx.testset(testset_dir) if t.expected is not None]
        generated = bool(tests) and tests[0].path.suffix == ".json"  # written by testgen, not by the instructor
        by_model = lambda tc: generated and not tc.name.startswith(testgen.SAMPLE_PREFIX)  # noqa: E731
        if not tests:
            return AnalyzerResult.skipped(SKIPPED_ENVIRONMENT, f"no usable tests in {testset_dir}")
        compare = ctx.compare_config(params)
        max_reported = int(params.get("max_reported", 5))

        results, failures = [], []
        for tc in tests:
            j = ctx.judge(tc.input, tc.expected, compare=compare)
            results.append({"test": tc.name, "verdict": j.verdict})
            if not j.passed:
                size = None
                if ctx.inputgen is not None and ctx.inputgen.size_var:
                    first = tc.input.split()[:1]
                    size = int(first[0]) if first and first[0].lstrip("-").isdigit() else None
                label = f"Test {tc.name}"
                if by_model(tc):  # the expected output was written by a model, not given
                    label += " [expected output written by the model]"
                failures.append(failure_entry(ctx, source=f"hidden test {tc.name}", label=label,
                                              judgement=j, input_text=tc.input, size=size))
        passed = len(tests) - len(failures)
        score = passed / len(tests)
        summary = f"{passed} of {len(tests)} tests pass"
        feedback = summary + "."
        if failures:
            f = failures[0]
            feedback += f" Fails on test {f['source'].split()[-1]} (input {one_line(f['input'], 40)}): {f['why']}"
        evidence = {
            "tests_passed": passed, "tests_total": len(tests),
            "verdicts": dict(sorted(Counter(r["verdict"] for r in results).items())),
            "results": results,
            "failing": [evidence_view(f) for f in failures[:max_reported]],
        }
        flags = []
        if generated:
            n_model = sum(1 for t in tests if by_model(t))
            if not n_model:
                flags.append({"type": "samples_only_tests", "severity": "medium",
                              "message": "the model gave no usable test case, so correctness was graded on the "
                                         "samples in the rules only"})
            evidence["generation"] = {"tests_from_samples": len(tests) - n_model, "tests_from_llm": n_model}
        return AnalyzerResult(score=score, summary=summary, feedback=feedback, evidence=evidence,
                              suggestions=hints(failures), failures=failures[:max_reported], flags=flags)
