"""logical: the submission on edge cases an LLM writes from the statement and the samples.

Same method as the correctness tests (see ``autograder.testgen``), kept apart: the model writes a few simple
edge cases (smallest and largest allowed values, zero, negatives, a single element, ...) into ``edge_case.json``
as ``[{"name", "input", "expected"}]``, next to the rules. They are written once and reused by every later run.
The metric is the fraction of edge cases the submission passes. ``params.cases`` (e.g. ``["zero", "negatives"]``)
is only a hint to the model about which situations matter for this problem.
"""

from __future__ import annotations

from .. import testgen
from ..util import one_line
from ._common import evidence_view, failure_entry, hints
from .base import SKIPPED_ENVIRONMENT, Analyzer, AnalyzerResult, register


@register
class EdgeCaseRunner(Analyzer):
    name = "edge_case_runner"
    description = "Edge cases (smallest/largest values, zero, negatives ...) written by an LLM from the statement"
    needs_binary = True

    def prepare(self, agent, params):
        return testgen.prepare(agent, dict(params), kind="edge")

    def run(self, ctx, params):
        error = (params.get("_prepared") or {}).get("error")
        if error:
            return AnalyzerResult.skipped(SKIPPED_ENVIRONMENT, f"edge case generation failed: {error[:200]}")
        edge_file = params.get("edge_file", testgen.EDGE_FILE)
        cases = [c for c in ctx.testset(edge_file) if c.expected is not None]
        if not cases:
            return AnalyzerResult.skipped(SKIPPED_ENVIRONMENT, f"no usable edge cases in {edge_file}")
        compare = ctx.compare_config(params)
        max_reported = int(params.get("max_reported", 3))

        failures, results = [], []
        for case in cases:
            j = ctx.judge(case.input, case.expected, compare=compare)
            results.append({"case": case.name, "verdict": j.verdict})
            if not j.passed:
                failures.append(failure_entry(ctx, source=f"edge case {case.name}",
                                              label=f"Edge case {case.name} [expected output written by the model]",
                                              judgement=j, input_text=case.input))
        passed = len(cases) - len(failures)
        summary = f"{passed} of {len(cases)} edge cases pass"
        feedback = summary + "."
        if failures:
            first = failures[0]
            feedback += f" Fails on {first['source']} (input {one_line(first['input'], 40)}): {first['why']}"
        evidence = {"edge_cases_passed": passed, "edge_cases_total": len(cases), "results": results,
                    "failing": [evidence_view(f) for f in failures[:max_reported]],
                    "oracle": "edge cases written by the model (checked against the samples)"}
        return AnalyzerResult(passed / len(cases), summary, feedback, evidence, hints(failures),
                              failures[:max_reported])
