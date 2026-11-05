"""syntax: compiler diagnostics under strict warning flags.

The submission is compiled with the rule's flags, but without ``-Werror``, so
that every warning can be counted. ``-O2`` is added by default because several
useful warnings (maybe-uninitialized, unused-result under _FORTIFY_SOURCE)
only appear with optimisation enabled.
"""

from __future__ import annotations

import re

from .base import Analyzer, AnalyzerResult, register

_DIAG = re.compile(r"^(?P<file>[^\n]*?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
                   r"(?P<kind>warning|error|fatal error):\s*(?P<msg>.*?)(?:\s+\[(?P<flag>-W[^\]]+)\])?$",
                   re.M)


@register
class CompilerWarnings(Analyzer):
    name = "compiler_warnings"
    description = "Counts compiler warnings/errors under the rule's flags (gcc -Wall -Wextra ...)"

    def run(self, ctx, params):
        flags = str(params.get("flags", "-Wall -Wextra -std=c11"))
        werror = "-Werror" in flags.split()
        clean_flags = " ".join(f for f in flags.split() if f != "-Werror" and not f.startswith("-Werror="))
        optimize = params.get("optimize", "-O2")
        if optimize and not any(f.startswith("-O") for f in clean_flags.split()):
            clean_flags = f"{optimize} {clean_flags}"
        penalty = float(params.get("penalty_per_warning", 0.1))

        res = ctx.build("warnings", clean_flags)
        diags = []
        seen = set()
        for m in _DIAG.finditer(res.output):
            key = (m.group("line"), m.group("kind"), m.group("msg"))
            if key in seen:
                continue
            seen.add(key)
            diags.append({"line": int(m.group("line")), "kind": m.group("kind").replace("fatal error", "error"),
                          "message": m.group("msg").strip(), "flag": m.group("flag")})
        warnings = [d for d in diags if d["kind"] == "warning"]
        errors = [d for d in diags if d["kind"] == "error"]
        evidence = {"command": " ".join(["cc", clean_flags, ctx.name]), "werror_requested": werror,
                    "warnings": warnings, "errors": errors[:10]}

        if not res.ok:
            first = errors[0] if errors else None
            where = f" (line {first['line']}: {first['message']})" if first else ""
            return AnalyzerResult(0.0, f"does not compile: {len(errors) or 1} error(s)",
                                  f"Compilation failed{where}.", evidence,
                                  ["Fix the compilation errors first; nothing else can be tested until then."])
        score = max(0.0, 1.0 - penalty * len(warnings))
        if not warnings:
            return AnalyzerResult(1.0, "no warnings", f"Compiles cleanly with {flags}.", evidence)
        counts = {}
        for w in warnings:
            label = w["flag"] or w["message"][:30]
            counts[label] = counts.get(label, 0) + 1
        summary = ", ".join(f"{k} x{v}" for k, v in sorted(counts.items()))
        listed = "; ".join(f"line {w['line']}: {w['message']}" for w in warnings[:3])
        feedback = f"{len(warnings)} warning(s): {listed}."
        if werror:
            feedback += " With -Werror (as the rules request) these warnings fail the build."
        suggestions = []
        for w in warnings[:3]:
            if "ignoring return value" in w["message"] and "scanf" in w["message"]:
                tip = "Check scanf's return value (e.g. `if (scanf(\"%d %d\", &a, &b) != 2) return 1;`)."
            else:
                tip = f"Fix the warning on line {w['line']}: {w['message']}."
            if tip not in suggestions:
                suggestions.append(tip)
        return AnalyzerResult(score, summary, feedback, evidence, suggestions)
