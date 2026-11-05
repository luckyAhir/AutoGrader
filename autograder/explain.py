"""Turning a failed run into an explanation a student can act on.

The explanations are rule-based (deterministic). They look at the verdict, the
relationship between the expected and the actual output (wrapped 32-bit value,
same numbers in a different order, off by one, extra prompt text ...), the
kind of input (extreme values, empty input, unusual whitespace) and, when
helpful, at well-known bug patterns in the source (``return a - b``
comparators, ``while (scanf(...) != 0)`` loops).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .inputgen import WHITESPACE_VARIANTS
from .sandbox import RunResult
from .util import INT32_MAX, INT32_MIN, INT64_MAX, INT64_MIN, to_int32, to_int64


@dataclass
class Explanation:
    why: str
    hint: Optional[str]
    tag: str


_COMPARATOR_SUB = re.compile(
    r"return\s*\(?\s*\*\s*\(\s*(?:const\s+)?(?:int|long(?:\s+long)?)\s*\*\s*\)\s*\w+\s*\)?\s*-\s*"
    r"\(?\s*\*\s*\(\s*(?:const\s+)?(?:int|long(?:\s+long)?)\s*\*\s*\)\s*\w+")
_COMPARATOR_SUB_VARS = re.compile(r"return\s+\(?\s*\w+\s*-\s*\w+\s*\)?\s*;")
_SCANF_NOT_ZERO = re.compile(r"while\s*\(\s*scanf\s*\([^;]*\)\s*!=\s*0\s*\)")
_SCANF_TRUTHY = re.compile(r"while\s*\(\s*scanf\s*\([^;]*\)\s*\)")
_WHILE_TRUE = re.compile(r"while\s*\(\s*(?:1|true)\s*\)|for\s*\(\s*;\s*;\s*\)")


def _ints(text: Optional[str]) -> Optional[List[int]]:
    if text is None:
        return None
    try:
        return [int(t) for t in text.split()]
    except ValueError:
        return None


def _fits32(x: int) -> bool:
    return INT32_MIN <= x <= INT32_MAX


def _near_int_limits(values: Optional[List[int]]) -> bool:
    return bool(values) and any(abs(v) >= 2 ** 30 for v in values)


def explain(verdict: str, input_text: str, expected: Optional[str], got: str,
            run: Optional[RunResult] = None, *, category: Optional[str] = None,
            variant: str = "normal", size: Optional[int] = None, source: str = "",
            time_limit_s: float = 1.0, memory_limit_mb: Optional[int] = None) -> Explanation:
    if verdict == "TLE":
        return _explain_tle(input_text, size, source, time_limit_s)
    if verdict == "RE":
        return _explain_re(run, input_text, size, memory_limit_mb)
    if verdict == "MLE":
        return Explanation(
            f"The program used more than the {memory_limit_mb} MB memory limit on this input.",
            "Avoid allocating arrays sized for the worst case many times over; free memory you no "
            "longer need and size buffers from n.", "memory_limit")
    if verdict == "OLE":
        return Explanation(
            "The program printed far more output than expected; it is probably printing inside "
            "an endless loop.",
            "Check the loop that prints results: does its condition ever become false?", "output_flood")
    if verdict == "PE":
        return _explain_pe(expected or "", got)
    return _explain_wa(input_text, expected or "", got, category, variant, source)


# ---------------------------------------------------------------- per verdict
def _explain_tle(input_text: str, size: Optional[int], source: str, time_limit_s: float) -> Explanation:
    small = len(input_text) < 200 and (size is None or size <= 100)
    if small:
        why = (f"The program did not finish within {time_limit_s:g} s on a tiny input, so it is "
               f"almost certainly stuck in an infinite loop (or waiting for input that never comes).")
        if _SCANF_NOT_ZERO.search(source):
            hint = ("`while (scanf(...) != 0)` never stops: at end of input scanf returns EOF (-1), "
                    "which is != 0. Compare with the number of items you expect, e.g. "
                    "`while (scanf(\"%d\", &x) == 1)`.")
        elif _SCANF_TRUTHY.search(source):
            hint = ("`while (scanf(...))` treats EOF (-1) as true and loops forever at end of input; "
                    "use `while (scanf(...) == 1)`.")
        elif _WHILE_TRUE.search(source):
            hint = "Check that every `while (1)` / `for (;;)` loop has a reachable break."
        else:
            hint = "Check that each loop variable moves toward the loop's exit condition."
        return Explanation(why, hint, "infinite_loop")
    where = f" (n = {size})" if size is not None else ""
    return Explanation(
        f"The program exceeded the {time_limit_s:g} s CPU limit on a large input{where}: the "
        f"algorithm is too slow for the problem's constraints.",
        "Use an asymptotically faster algorithm (see the complexity metric for the measured growth "
        "rate and the hottest lines).", "too_slow")


def _explain_re(run: Optional[RunResult], input_text: str, size: Optional[int],
                memory_limit_mb: Optional[int]) -> Explanation:
    detail = run.detail if run else "runtime error"
    if run and run.signal_name is None and run.exit_code:
        return Explanation(
            f"main() returned exit status {run.exit_code}; a non-zero status is treated as a runtime error.",
            "Return 0 from main when the program succeeds (and only return non-zero for real errors).",
            "nonzero_exit")
    context = ""
    if size == 0 or not input_text.split():
        context = " It happens on an empty input - check the n = 0 case before indexing arrays."
    elif size is not None and size >= 1000:
        context = (f" It happens only on a large input (n = {size}): a fixed-size array may be too small, "
                   f"or a large local array / deep recursion overflows the stack.")
    hint = None
    if run and run.signal_name == "SIGFPE":
        hint = "Guard every division and modulo against a zero divisor (and INT_MIN / -1)."
    elif run and run.signal_name == "SIGSEGV":
        hint = ("Check array bounds (use i < n, not i <= n), allocate arrays from n instead of a fixed "
                "size, and check pointers for NULL before using them.")
    elif run and run.signal_name == "SIGABRT":
        hint = "Look for a failing assert(), a double free, or writing past the end of a malloc'ed block."
    if memory_limit_mb and run and run.signal_name == "SIGSEGV" and size and size >= 1000:
        hint = (hint or "") + f" Remember the {memory_limit_mb} MB memory limit."
    return Explanation(f"The program {detail}.{context}", hint, "crash")


def _explain_pe(expected: str, got: str) -> Explanation:
    from .compare import whitespace_difference

    return Explanation(
        f"The values are right but the formatting differs: {whitespace_difference(expected, got)}.",
        "Print exactly the format in the statement (one space between values, newline at the end).",
        "presentation")


def _explain_wa(input_text: str, expected: str, got: str, category: Optional[str], variant: str,
                source: str) -> Explanation:
    exp_t, got_t = expected.split(), got.split()
    in_ints = _ints(input_text)

    if not got.strip():
        return Explanation(
            "The program printed nothing for this input.",
            "Make sure the result is printed on every path (including early returns) and that the "
            "input is read in the stated format.", "no_output")

    extra_words = [t for t in got_t if re.search(r"[A-Za-z]{2,}", t) and t not in exp_t]
    if extra_words and not any(re.search(r"[A-Za-z]", t) for t in exp_t):
        shown = " ".join(extra_words[:4])
        return Explanation(
            f"Your output contains extra text ('{shown}') in addition to the answer. The grader "
            f"compares output exactly, so prompts such as \"Enter two numbers:\" make every test fail.",
            "Print only what the statement asks for - no prompts, labels or messages.", "extra_text")

    whitespace_note = ""
    if variant in WHITESPACE_VARIANTS:
        whitespace_note = (f" This input uses {WHITESPACE_VARIANTS[variant]}; the same values pass with "
                           f"single spaces, so the input parsing depends on exact whitespace.")

    exp_i, got_i = _ints(expected), _ints(got)
    if exp_i is not None and got_i is not None:
        if len(exp_i) == len(got_i):
            if sorted(exp_i) == sorted(got_i):
                why = "Your output contains the right numbers but in the wrong order."
                hint = None
                if _near_int_limits(in_ints) or _COMPARATOR_SUB.search(source) or _COMPARATOR_SUB_VARS.search(source):
                    why += (" The input contains values near INT_MIN/INT_MAX; a comparator written as "
                            "`return a - b` overflows for such values and reports the wrong order.")
                    hint = ("Write the comparator without subtraction: "
                            "`return (x > y) - (x < y);`.")
                return Explanation(why + whitespace_note, hint, "wrong_order")
            idx = next(i for i, (e, g) in enumerate(zip(exp_i, got_i)) if e != g)
            e, g = exp_i[idx], got_i[idx]
            pos = f"value #{idx + 1}" if len(exp_i) > 1 else "the result"
            if not _fits32(e) and to_int32(e) == g:
                return Explanation(
                    f"Expected {e}, got {g}: the correct result does not fit in a 32-bit int "
                    f"(range {INT32_MIN}..{INT32_MAX}), so the computation overflowed and wrapped around.",
                    "Use a wider type (long long with %lld) for the computation, or check "
                    "`b > INT_MAX - a` before adding.", "int_overflow")
            if not (INT64_MIN <= e <= INT64_MAX) and to_int64(e) == g:
                return Explanation(
                    f"Expected {e}, got {g}: the result overflows even a 64-bit long long.",
                    "Detect the overflow explicitly or use arbitrary-precision arithmetic.", "int64_overflow")
            if _near_int_limits(in_ints):
                return Explanation(
                    f"For {pos} expected {e}, got {g}. The input contains values near INT_MIN/INT_MAX, "
                    f"so an intermediate computation probably overflowed." + whitespace_note,
                    "Use long long for intermediate results.", "intermediate_overflow")
            if abs(e - g) == 1:
                return Explanation(f"For {pos} expected {e}, got {g} (off by one)." + whitespace_note,
                                   "Check loop bounds and < versus <= comparisons.", "off_by_one")
            if g == -e:
                return Explanation(f"For {pos} expected {e}, got {g} (wrong sign)." + whitespace_note,
                                   "Check how negative numbers are handled.", "sign")
            neg = " The input contains negative numbers." if in_ints and any(v < 0 for v in in_ints) else ""
            return Explanation(f"For {pos} expected {e}, got {g}.{neg}" + whitespace_note, None, "wrong_value")
        if len(got_i) < len(exp_i):
            return Explanation(
                f"Your output has {len(got_i)} number(s) but {len(exp_i)} are expected." + whitespace_note,
                "Check that every input value is read and printed (empty and single-element inputs too).",
                "missing_values")
        return Explanation(
            f"Your output has {len(got_i)} number(s) but only {len(exp_i)} are expected." + whitespace_note,
            "Do not print debug output or extra values.", "extra_values")

    from .compare import first_difference

    return Explanation(f"Output differs - {first_difference(expected, got)}." + whitespace_note, None, "wrong_output")
