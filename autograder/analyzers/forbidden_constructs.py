"""forbidden_constructs: course-specific bans and requirements.

An example of how the rules can add metrics the agent did not anticipate.
The instructor lists calls, headers or keywords that are not allowed (for
example ``gets``, ``system``, ``goto``, or ``qsort`` in a "write your own
sort" lab) and calls that must appear.
"""

from __future__ import annotations

from ..csource import find_calls
from .base import Analyzer, AnalyzerResult, register


@register
class ForbiddenConstructs(Analyzer):
    name = "forbidden_constructs"
    description = "Instructor-listed banned calls/headers/keywords and required calls"

    def run(self, ctx, params):
        src = ctx.csource
        banned_calls = set(params.get("forbidden_calls", []))
        banned_headers = set(params.get("forbidden_headers", []))
        banned_keywords = set(params.get("forbidden_keywords", []))
        required_calls = list(params.get("required_calls", []))
        penalty = float(params.get("penalty_per_violation", 0.5))

        violations = []
        for name, lines in sorted(find_calls(src, banned_calls).items()):
            violations.append({"what": f"call to {name}()", "line": lines[0], "fix": f"Do not call {name}()."})
        for header in sorted(banned_headers & set(src.includes)):
            line = next((d.line for d in src.directives if header in d.text), 0)
            violations.append({"what": f"#include <{header}>", "line": line, "fix": f"Remove <{header}>."})
        for kw in sorted(banned_keywords):
            line = next((t.line for t in src.tokens if t.text == kw), None)
            if line is not None:
                violations.append({"what": f"keyword '{kw}'", "line": line, "fix": f"Do not use '{kw}'."})
        called = src.called_names()
        for name in required_calls:
            if name not in called:
                violations.append({"what": f"required call {name}() is missing", "line": 0,
                                   "fix": f"Use {name}() as the assignment requires."})

        score = max(0.0, 1.0 - penalty * len(violations))
        evidence = {"violations": violations, "forbidden_calls": sorted(banned_calls),
                    "forbidden_headers": sorted(banned_headers), "forbidden_keywords": sorted(banned_keywords),
                    "required_calls": required_calls}
        if not violations:
            return AnalyzerResult(1.0, "none used", "No forbidden constructs.", evidence)
        listed = "; ".join(f"{v['what']}" + (f" (line {v['line']})" if v["line"] else "") for v in violations)
        return AnalyzerResult(score, f"{len(violations)} violation(s)", f"Rule violations: {listed}.", evidence,
                              [v["fix"] for v in violations])
