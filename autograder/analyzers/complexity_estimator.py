"""complexity: empirical growth rate from deterministic operation counts.

Method
------
1. Build the submission with gcov instrumentation (``-O0 --coverage``).
2. For each size n in the rules, generate a seeded input of size n, run the
   program, and read the per-line execution counts with gcov. The total
   number of executed source lines is the *operation count* ops(n). Unlike
   wall-clock time it is exactly the same on every run and every machine
   load, which keeps the grade reproducible.
3. Fit ``ops(n) ~ a + b*f(n)`` for f in {log n, n, n log n, n^2, n^3, 2^n}.
   The model with the smallest relative error wins. If ops barely changes
   across sizes, the result is O(1).
4. Compare with the required bound: full marks if the estimate is no worse,
   minus ``penalty_per_class`` for each class above it.

The same counts double as a deterministic **profile**: the hottest source
lines at the largest size are reported, together with the reference
solution's operation count for comparison.

Without gcov the analyzer falls back to CPU-time measurements and says that
the estimate is timing-based, so less reliable.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import testgen
from .base import SKIPPED_ENVIRONMENT, Analyzer, AnalyzerResult, register

MODELS: List[Tuple[str, object]] = [
    ("O(1)", lambda n: 1.0),
    ("O(log n)", lambda n: math.log2(max(n, 2))),
    ("O(n)", lambda n: float(n)),
    ("O(n log n)", lambda n: n * math.log2(max(n, 2))),
    ("O(n^2)", lambda n: float(n) ** 2),
    ("O(n^3)", lambda n: float(n) ** 3),
    ("O(2^n)", lambda n: 2.0 ** n),
]
RANK = {name: i for i, (name, _) in enumerate(MODELS)}


def normalize_bound(text: str) -> str:
    try:
        return _normalize_bound(text)
    except ValueError:
        # "O(n log n) average case", "O(n^2) in the worst case": the bound is the O(...) part
        m = re.search(r"O\s*\([^()]*(?:\([^()]*\)[^()]*)*\)", text)
        if m and m.group(0) != text.strip():
            return _normalize_bound(m.group(0))
        raise


def _normalize_bound(text: str) -> str:
    t = text.replace(" ", "").replace("*", "").replace("²", "^2").replace("³", "^3").lower()
    t = t.replace("logn", "log n").replace("lgn", "log n")
    table = {"o(1)": "O(1)", "o(log n)": "O(log n)", "o(n)": "O(n)", "o(nlog n)": "O(n log n)",
             "o(n^2)": "O(n^2)", "o(n2)": "O(n^2)", "o(n^3)": "O(n^3)", "o(n3)": "O(n^3)",
             "o(2^n)": "O(2^n)"}
    if t in table:
        return table[t]
    multi = _multi_variable_class(text)
    if multi is None:
        raise ValueError(f"unsupported complexity bound {text!r}; use one of {', '.join(RANK)}, or a bound "
                         f"in several variables such as O(n*m) or O(n+m)")
    return multi


def _variables(text: str) -> set:
    body = text.lower().replace("log", " ")
    return set(re.findall(r"[a-z]", body))


def _multi_variable_class(text: str) -> Optional[str]:
    """Reduce a bound in several variables (O(n*m), O(n+m), O(n^2*m) ...) to one of the measured classes.

    The estimator grows one input dimension at a time (the others stay small), so the growth it can see is
    that of the fastest-growing variable: O(n*m) shows up as O(n), O(n^2*m) as O(n^2).
    """
    t = text.lower().replace(" ", "").replace("²", "^2").replace("³", "^3")
    m = re.fullmatch(r"o\((.+)\)", t)
    if not m:
        return None
    body = m.group(1)
    if re.search(r"\d+\^[a-z]", body):
        return "O(2^n)"
    best: Optional[Tuple[int, bool]] = None
    for term in body.split("+"):
        exps: Dict[str, int] = {}
        has_log = False
        for tok in re.findall(r"log\(?[a-z]\)?|[a-z](?:\^\d)?|\d+", term):
            if tok.startswith("log"):
                has_log = True
            elif tok[0].isalpha():
                exps[tok[0]] = exps.get(tok[0], 0) + (int(tok[2:]) if "^" in tok else 1)
        if not exps and not has_log:
            if re.fullmatch(r"\d+", term):
                exps = {}
            else:
                return None
        grade = (max(exps.values(), default=0), has_log)
        best = grade if best is None or grade > best else best
    if best is None or best[0] > 3:
        return None
    exp, has_log = best
    return {(0, False): "O(1)", (0, True): "O(log n)", (1, False): "O(n)", (1, True): "O(n log n)",
            (2, False): "O(n^2)", (2, True): "O(n^2)", (3, False): "O(n^3)", (3, True): "O(n^3)"}[(exp, has_log)]


def fit_growth(sizes: List[int], ops: List[float], o1_tolerance: float = 0.10) -> Dict[str, object]:
    if min(ops) <= 0:
        ops = [o + 1 for o in ops]
    spread = max(ops) / min(ops)
    fits: Dict[str, Optional[float]] = {}
    if spread <= 1 + o1_tolerance:
        return {"estimate": "O(1)", "errors": {"O(1)": round(spread - 1, 6)}, "loglog_slope": 0.0}
    for name, f in MODELS[1:]:
        if name == "O(2^n)" and max(sizes) > 60:
            continue
        xs = [f(n) for n in sizes]
        fits[name] = _relative_fit_error(xs, ops)
    valid = {k: v for k, v in fits.items() if v is not None}
    if not valid:
        return {"estimate": "unknown", "errors": {}, "loglog_slope": None}
    best = min(valid.values())
    # Prefer the lower-order model when two fit equally well (exact counts tie only at ~0 error).
    estimate = next(name for name, _ in MODELS[1:] if name in valid and valid[name] <= best + 1e-3)
    slope = math.log(ops[-1] / ops[-2]) / math.log(sizes[-1] / sizes[-2]) if len(sizes) >= 2 else None
    return {"estimate": estimate, "errors": {k: round(v, 6) for k, v in valid.items()},
            "loglog_slope": round(slope, 3) if slope is not None else None}


def _relative_fit_error(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    if b <= 0:
        return None
    a = my - b * mx
    return math.sqrt(sum(((a + b * x - y) / y) ** 2 for x, y in zip(xs, ys)) / n)


_GCOV_LINE = re.compile(r"^\s*([^:]+):\s*(\d+):(.*)$")


def parse_gcov(text: str) -> Dict[int, Tuple[int, str]]:
    counts: Dict[int, Tuple[int, str]] = {}
    for line in text.splitlines():
        m = _GCOV_LINE.match(line)
        if not m:
            continue
        raw, lineno, code = m.group(1).strip(), int(m.group(2)), m.group(3)
        if lineno == 0 or raw == "-":
            continue
        if raw.startswith("#") or raw.startswith("="):
            value = 0
        else:
            try:
                value = int(raw.rstrip("*"))
            except ValueError:
                continue
        counts[lineno] = (value, code)
    return counts


def _effective_size(gen, case, requested: int) -> int:
    """The size actually generated (the spec's maximum caps what can be asked for)."""
    size = gen.size_of(getattr(case, "values", None))
    return int(size) if size is not None else requested


@register
class ComplexityEstimator(Analyzer):
    name = "complexity_estimator"
    description = "Growth rate fitted to gcov operation counts at increasing input sizes"
    needs_binary = True

    def prepare(self, agent, params):
        """Rules without an input_spec/generator: let the model describe the input format (see testgen)."""
        rules = agent.rules
        if agent.inputgen is None and not rules.input_spec and not rules.generator_path:
            with testgen._lock_for(rules.base_dir):
                agent.size_inputgen = testgen.ensure_input_spec(rules, agent.cache_dir, dict(params), agent.sandbox)
        return None

    def run(self, ctx, params):
        gen = ctx.inputgen or ctx.size_inputgen  # the rules' spec first, else the one the model described
        if gen is None:
            return AnalyzerResult.skipped(SKIPPED_ENVIRONMENT, "rules define no input_spec/generator")
        try:
            required = normalize_bound(str(params.get("required", "O(n)")))
        except ValueError as exc:
            return AnalyzerResult.skipped(SKIPPED_ENVIRONMENT, str(exc))
        sizes = sorted(int(s) for s in params.get("sizes", [1000, 2000, 4000, 8000, 16000]))
        penalty = float(params.get("penalty_per_class", 0.5))
        tl = float(params.get("time_limit_s", max(10 * ctx.rules.time_limit_s, 5.0)))
        limits = ctx.limits.scaled(tl / ctx.rules.time_limit_s, memory_mb=4 * ctx.rules.memory_limit_mb)

        method = "gcov line counts" if ctx.toolchain.gcov else "cpu time (gcov unavailable)"
        if ctx.toolchain.gcov:
            series, failure, hot = self._measure_gcov(ctx, gen, ctx.submission, "coverage", sizes, limits)
        else:
            series, failure, hot = self._measure_time(ctx, gen, sizes, limits)

        evidence: Dict[str, object] = {"method": method, "required": required, "sizes": sizes}
        written = str(params.get("required", "O(n)"))
        bound_note = ""
        if len(_variables(written)) > 1:
            evidence["required_as_written"] = written
            bound_note = (f" The bound {written} has several variables; it is checked by growing one input "
                          f"dimension at a time, which shows as {required}.")
        if failure is not None and len(series) < 3:
            n, verdict = failure
            evidence["failed_at"] = {"n": n, "verdict": verdict}
            if verdict == "TLE":
                return AnalyzerResult(0.0, f"too slow: no result at n={n}",
                                      f"The program exceeded {tl:g} s at n = {n}; it cannot reach the required "
                                      f"{required}.", evidence,
                                      [f"Aim for an {required} algorithm."])
            return AnalyzerResult(0.0, f"inconclusive: {verdict} at n={n}",
                                  f"Complexity could not be measured: the program failed ({verdict}) at n = {n}.",
                                  evidence)
        if len(series) < 2:
            return AnalyzerResult.skipped(SKIPPED_ENVIRONMENT, "not enough measurements", **evidence)

        ns = [n for n, _ in series]
        ops = [o for _, o in series]
        fit = fit_growth(ns, ops)
        estimate = fit["estimate"]
        evidence.update({"measurements": [{"n": n, "ops": o} for n, o in series], "fit": fit,
                         "estimated": estimate})
        if failure is not None:
            evidence["stopped_at"] = {"n": failure[0], "verdict": failure[1]}

        ref_note = ""
        if ctx.toolchain.gcov and ctx.oracle is not None and ctx.oracle.build() and params.get("compare_reference", True):
            ref_series, _, _ = self._measure_gcov(ctx, gen, ctx.rules.reference_path, "reference_coverage",
                                                  ns, limits)
            if ref_series and ref_series[-1][0] == ns[-1]:
                ratio = ops[-1] / max(1, ref_series[-1][1])
                evidence["reference_ops"] = [{"n": n, "ops": o} for n, o in ref_series]
                evidence["ops_ratio_vs_reference_at_max_n"] = round(ratio, 2)
                ref_note = f" At n = {ns[-1]} it executes {ratio:.1f}x the reference's line count."
        if hot:
            evidence["hot_lines"] = hot

        if estimate == "unknown":
            return AnalyzerResult(0.5, "growth rate unclear", "The measured growth fits no standard model.",
                                  evidence)
        diff = RANK[estimate] - RANK[required]
        score = 1.0 if diff <= 0 else max(0.0, 1.0 - penalty * diff)
        hot_note = ""
        if hot and diff > 0:
            h = hot[0]
            hot_note = (f" Hottest line {h['line']} (`{h['code']}`) runs {h['count']:,} times at n = {ns[-1]} "
                        f"({h['share']:.0%} of executed lines).")
        if diff <= 0:
            summary = f"{estimate} confirmed" if estimate == required else f"{estimate} (better than {required})"
            feedback = f"Measured growth {estimate}; required {required}.{bound_note}{ref_note}"
            suggestions = []
        else:
            summary = f"{estimate}, required {required}"
            feedback = f"Measured growth {estimate} but the problem requires {required}.{bound_note}{hot_note}{ref_note}"
            suggestions = [f"Replace the {estimate} part of the algorithm with an {required} approach"
                           + (f" (start with line {hot[0]['line']})." if hot else ".")]
        return AnalyzerResult(score, summary, feedback, evidence, suggestions)

    # ------------------------------------------------------------ measurement
    def _measure_gcov(self, ctx, gen, source: Path, tag: str, sizes, limits):
        d = ctx.workdir / tag
        d.mkdir(parents=True, exist_ok=True)
        name = Path(source).name
        shutil.copyfile(source, d / name)
        obj = Path(name).stem + ".o"
        exe = "prog" + ctx.toolchain.exe_suffix
        res = ctx.sandbox.compile([name], obj, f"-O0 --coverage {ctx.rules.std_flag}", cwd=d, compile_only=True)
        if res.ok:
            res = ctx.sandbox.compile([obj], exe, "--coverage", ctx.rules.link_flags, cwd=d)
        if not res.ok:
            return [], (sizes[0], "compile error"), []
        gcda = d / (Path(name).stem + ".gcda")
        series, failure, last_counts = [], None, {}
        seen = set()
        for n in sizes:
            if gcda.exists():
                gcda.unlink()
            case = gen.for_size(n)
            n = _effective_size(gen, case, n)  # sizes above the spec's maximum are clamped: do not repeat them
            if n in seen:
                continue
            seen.add(n)
            run = ctx.sandbox.run([str(d / exe)], case.text, limits)
            if not run.ok or not gcda.exists():
                failure = (n, run.status if not run.ok else "no coverage data")
                break
            counts = self._gcov_counts(ctx, d, name, obj)
            if counts is None:
                failure = (n, "gcov failed")
                break
            series.append((n, sum(c for c, _ in counts.values())))
            last_counts = counts
        hot = []
        total = sum(c for c, _ in last_counts.values()) or 1
        for line, (count, code) in sorted(last_counts.items(), key=lambda kv: (-kv[1][0], kv[0]))[:3]:
            if count:
                hot.append({"line": line, "count": count, "share": round(count / total, 4), "code": code.strip()})
        return series, failure, hot

    @staticmethod
    def _gcov_counts(ctx, d: Path, name: str, obj: str):
        report = d / (name + ".gcov")
        if report.exists():
            report.unlink()
        try:
            subprocess.run([ctx.toolchain.gcov, "-o", obj, name], cwd=str(d), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if not report.exists():
            return None
        return parse_gcov(report.read_text(encoding="utf-8", errors="replace"))

    def _measure_time(self, ctx, gen, sizes, limits):
        series, failure = [], None
        seen = set()
        for n in sizes:
            case = gen.for_size(n)
            n = _effective_size(gen, case, n)
            if n in seen:
                continue
            seen.add(n)
            best = None
            for _ in range(3):
                run = ctx.run(case.text, limits)
                if not run.ok:
                    failure = (n, run.status)
                    break
                best = run.cpu_time_s if best is None else min(best, run.cpu_time_s)
            if failure:
                break
            series.append((n, round(max(best, 1e-4) * 1e6)))  # microseconds
        return series, failure, []
