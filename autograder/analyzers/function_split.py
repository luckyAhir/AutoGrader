"""modularity: is the program decomposed into functions that are actually used?

score = 0.4 * min(1, functions / min_functions)
      + 0.3 * (a helper is called from main, if expect_helpers)
      + 0.3 * main-share score (main holding <= max_main_share of all statements)
      - 0.1 per defined-but-never-called helper (dead code)
"""

from __future__ import annotations

from .base import Analyzer, AnalyzerResult, register


@register
class FunctionSplit(Analyzer):
    name = "function_split"
    description = "Decomposition into helper functions (count, use from main, main's share of the code)"

    def run(self, ctx, params):
        src = ctx.csource
        min_functions = int(params.get("min_functions", 2))
        expect_helpers = bool(params.get("expect_helpers", True))
        max_main_share = float(params.get("max_main_share", 0.6))
        helper_hint = params.get("helper_hint")

        funcs = {f.name: f for f in src.functions}
        # References, not just calls: a comparator passed to qsort() is used too.
        refs = {f.name: sorted({t.text for t in src.body_tokens(f) if t.kind == "id" and t.text in funcs})
                for f in src.functions}
        main = funcs.get("main")
        helpers = [f for f in src.functions if f.name != "main"]
        reachable = self._reachable(refs, "main") - {"main"}
        unused = sorted(f.name for f in helpers if f.name not in reachable)
        total_statements = sum(f.statements for f in src.functions) or 1
        main_share = (main.statements / total_statements) if main else 1.0

        count_score = min(1.0, len(funcs) / max(1, min_functions))
        used_score = 1.0 if (not expect_helpers or any(f.name in reachable for f in helpers)) else 0.0
        share_score = 1.0 if main_share <= max_main_share else max_main_share / main_share
        score = max(0.0, 0.4 * count_score + 0.3 * used_score + 0.3 * share_score - 0.1 * len(unused))

        suggestions = []
        if not helpers:
            summary = "only main() defined"
            hint = f" (e.g. `{helper_hint}`)" if helper_hint else ""
            feedback = (f"Only main() is defined. Move the core computation into a helper function{hint} "
                        f"and call it from main.")
            suggestions.append(f"Extract the computation into its own function{hint}; keep main() for I/O.")
        else:
            summary = f"{len(funcs)} functions; main holds {main_share:.0%} of statements"
            feedback = f"{len(helpers)} helper function(s): {', '.join(f.name + '()' for f in helpers)}."
            if used_score == 0.0:
                feedback += " None of them is called from main()."
            if main_share > max_main_share:
                feedback += f" main() still contains {main_share:.0%} of all statements."
                suggestions.append("Move more of main()'s logic into the helper functions.")
            if len(funcs) < min_functions:
                suggestions.append(f"Define at least {min_functions} functions.")
        if unused:
            feedback += f" Never called: {', '.join(unused)}."
            suggestions.append(f"Remove or use the dead function(s) {', '.join(unused)}.")
        return AnalyzerResult(score, summary, feedback, {
            "functions_defined": len(funcs), "expected_min": min_functions,
            "functions": [{"name": f.name, "statements": f.statements, "uses": refs[f.name]}
                          for f in src.functions],
            "called_from_main": sorted(reachable), "unused_functions": unused,
            "main_statement_share": round(main_share, 4),
            "subscores": {"count": round(count_score, 4), "helpers_used": used_score,
                          "main_share": round(share_score, 4)},
        }, suggestions)

    @staticmethod
    def _reachable(refs, root):
        seen, stack = set(), [root]
        while stack:
            name = stack.pop()
            if name in seen or name not in refs:
                continue
            seen.add(name)
            stack.extend(refs[name])
        return seen
