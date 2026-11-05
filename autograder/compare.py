"""Comparing a program's output with the expected output.

Modes (chosen in the rules, ``"compare": {"mode": ...}``):

* ``exact``  - byte-exact after normalising line endings
* ``lines``  - line by line, ignoring trailing spaces and trailing blank lines
               (what most online judges do; the default)
* ``tokens`` - whitespace-insensitive
* ``float``  - token by token with an absolute/relative tolerance for numbers

If the output fails the configured mode but has exactly the expected tokens,
the verdict is PE (presentation error) rather than WA, so feedback can say
"only formatting differs".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .util import normalize_newlines


@dataclass
class Comparison:
    ok: bool
    verdict: str  # AC | WA | PE
    detail: str


def _lines(text: str) -> List[str]:
    lines = [l.rstrip() for l in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _floats_equal(a: str, b: str, abs_eps: float, rel_eps: float) -> bool:
    if a == b:
        return True
    try:
        x, y = float(a), float(b)
    except ValueError:
        return False
    return abs(x - y) <= max(abs_eps, rel_eps * abs(x))


def outputs_equal(expected: str, got: str, mode: str = "lines", abs_eps: float = 1e-6,
                  rel_eps: float = 1e-6) -> bool:
    e, g = normalize_newlines(expected), normalize_newlines(got)
    if mode == "exact":
        return e == g
    if mode == "tokens":
        return e.split() == g.split()
    if mode == "float":
        et, gt = e.split(), g.split()
        return len(et) == len(gt) and all(_floats_equal(a, b, abs_eps, rel_eps) for a, b in zip(et, gt))
    return _lines(e) == _lines(g)


def compare_outputs(expected: str, got: str, mode: str = "lines", abs_eps: float = 1e-6,
                    rel_eps: float = 1e-6) -> Comparison:
    if outputs_equal(expected, got, mode, abs_eps, rel_eps):
        return Comparison(True, "AC", "")
    e, g = normalize_newlines(expected), normalize_newlines(got)
    if mode != "tokens" and e.split() == g.split():
        return Comparison(False, "PE", whitespace_difference(e, g))
    return Comparison(False, "WA", first_difference(e, g))


def whitespace_difference(expected: str, got: str) -> str:
    if expected.rstrip("\n") == got.rstrip("\n"):
        if expected.endswith("\n") and not got.endswith("\n"):
            return "missing newline at the end of the output"
        return "different number of blank lines at the end of the output"
    el, gl = expected.split("\n"), got.split("\n")
    for i, (a, b) in enumerate(zip(el, gl), 1):
        if a != b:
            if a.rstrip() == b.rstrip():
                return f"line {i}: extra spaces at the end of the line"
            return f"line {i}: spacing differs (expected {a!r}, got {b!r})"
    return f"line breaks differ (expected {len(el)} lines, got {len(gl)})"


def first_difference(expected: str, got: str) -> str:
    el, gl = _lines(expected), _lines(got)
    for i, (a, b) in enumerate(zip(el, gl), 1):
        if a != b:
            return f"line {i}: expected {_clip(a)!r}, got {_clip(b)!r}"
    if len(gl) < len(el):
        return f"output ended after {len(gl)} line(s); expected {len(el)}"
    return f"extra output after line {len(el)}: {_clip(gl[len(el)])!r}"


def _clip(s: Optional[str], n: int = 60) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "..."
