"""Deterministic readability measurements.

These measurements back the ``llm_review`` analyzer. In ``heuristic`` mode
they are the score. In ``hybrid`` mode (the default) they are blended with the
LLM's rubric score, which anchors the LLM's tendency to be lenient. If the LLM
is unreachable they are the fallback.

Sub-scores (each 0..1):
* naming      - share of declared names that are descriptive (>= 3 chars, or a
                loop counter such as i/j/k)
* comments    - comment density against a target, plus a header comment
* formatting  - spaces around binary operators and after commas, line length,
                indentation inside blocks, one statement per line
* clarity     - few "magic numbers" in function bodies
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from .csource import CSource

SHORT_OK = {"i", "j", "k", "n", "m", "x", "y"}  # conventional in small algorithmic code
SPACED_OPS = {"=", "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "%=", "&&", "||", "<", ">",
              "+", "-", "*", "/", "%"}
SUB_WEIGHTS = {"naming": 0.30, "comments": 0.25, "formatting": 0.25, "clarity": 0.20}
COMMENT_TARGET = 0.15  # comment lines per code line
MAX_LINE = 100
OK_LITERALS = {"0", "1", "2", "-1", "0.0", "1.0", "10", "100", "0.5"}


def readability(src: CSource) -> Dict[str, Any]:
    naming, naming_ev = _naming(src)
    comments, comments_ev = _comments(src)
    formatting, formatting_ev = _formatting(src)
    clarity, clarity_ev = _clarity(src)
    subs = {"naming": naming, "comments": comments, "formatting": formatting, "clarity": clarity}
    score = sum(SUB_WEIGHTS[k] * v for k, v in subs.items())
    return {
        "score": round(score, 4),
        "subscores": {k: round(v, 4) for k, v in subs.items()},
        "weights": SUB_WEIGHTS,
        "details": {"naming": naming_ev, "comments": comments_ev, "formatting": formatting_ev,
                    "clarity": clarity_ev},
    }


def _naming(src: CSource):
    names = [d for d in src.declarations if d.kind in ("local", "param", "global", "function")]
    seen, poor, good = set(), [], 0
    for d in names:
        if (d.name, d.function) in seen:
            continue
        seen.add((d.name, d.function))
        if len(d.name) >= 3 or (d.is_loop_counter and d.name in SHORT_OK):
            good += 1
        else:
            poor.append(d.name)
    total = good + len(poor)
    score = good / total if total else 1.0
    return score, {"declared_names": total, "descriptive": good, "terse": sorted(set(poor))}


def _comments(src: CSource):
    code_lines = len(src.code_lines())
    comment_lines = len(src.comment_lines())
    ratio = comment_lines / code_lines if code_lines else 0.0
    first_code = min(src.code_lines()) if src.code_lines() else 1
    header = any(c.line <= first_code for c in src.comments) or any(
        _comment_precedes(src, f.start_line) for f in src.functions)
    score = 0.7 * min(1.0, ratio / COMMENT_TARGET) + 0.3 * (1.0 if header else 0.0)
    return score, {"comment_lines": comment_lines, "code_lines": code_lines,
                   "ratio": round(ratio, 3), "target_ratio": COMMENT_TARGET, "header_comment": header}


def _comment_precedes(src: CSource, line: int) -> bool:
    return any(c.end_line in (line - 1, line - 2) for c in src.comments)


def _formatting(src: CSource):
    toks = src.tokens
    spaced = total_ops = 0
    commas = commas_spaced = 0
    line_text = src.lines
    for i, t in enumerate(toks):
        if t.kind != "op":
            continue
        if t.text == ",":
            commas += 1
            line = line_text[t.line - 1]
            if _followed_by_space(line, t, toks, i):
                commas_spaced += 1
        elif t.text in SPACED_OPS and _is_binary(toks, i):
            total_ops += 1
            if _surrounded_by_space(line_text[t.line - 1], toks, i):
                spaced += 1
    op_ratio = spaced / total_ops if total_ops else 1.0
    comma_ratio = commas_spaced / commas if commas else 1.0
    lines = [l for l in src.lines if l.strip()]
    long_lines = sum(1 for l in lines if len(l.expandtabs(4)) > MAX_LINE)
    length_ratio = 1 - long_lines / len(lines) if lines else 1.0
    indent_ratio, mixed = _indentation(src)
    multi = _multi_statement_lines(src)
    stmt_ratio = 1 - multi / max(1, len(src.code_lines()))
    score = (0.35 * op_ratio + 0.15 * comma_ratio + 0.15 * length_ratio + 0.2 * indent_ratio
             + 0.15 * stmt_ratio) - (0.1 if mixed else 0.0)
    return max(0.0, score), {
        "operators_spaced": f"{spaced}/{total_ops}", "commas_followed_by_space": f"{commas_spaced}/{commas}",
        "long_lines": long_lines, "indented_block_lines": round(indent_ratio, 3),
        "mixed_tabs_spaces": mixed, "lines_with_several_statements": multi,
    }


def _is_binary(toks, i) -> bool:
    if i == 0:
        return False
    prev = toks[i - 1]
    if toks[i].text in ("*", "-", "+", "&"):
        # unary if preceded by an operator/keyword (e.g. "= -1", "return *p", "(int *)")
        return prev.kind in ("id", "num", "chr", "str") or prev.text in (")", "]")
    if toks[i].text in ("<", ">") and prev.text == "include":
        return False
    return True


def _surrounded_by_space(line: str, toks, i) -> bool:
    t = toks[i]
    prev, nxt = toks[i - 1], toks[i + 1] if i + 1 < len(toks) else None
    if prev.line != t.line or nxt is None or nxt.line != t.line:
        return True
    pattern = re.escape(prev.text[-1:]) + r"(\s*)" + re.escape(t.text) + r"(\s*)" + re.escape(nxt.text[:1])
    m = re.search(pattern, line)
    return bool(m and m.group(1) and m.group(2))


def _followed_by_space(line: str, t, toks, i) -> bool:
    if i + 1 >= len(toks) or toks[i + 1].line != t.line:
        return True
    nxt = toks[i + 1]
    m = re.search(r",(\s*)" + re.escape(nxt.text[:1]), line)
    return bool(m and m.group(1))


def _indentation(src: CSource):
    depth_at_line: Dict[int, int] = {}
    depth = 0
    for t in src.tokens:
        if t.text == "}":
            depth -= 1
        depth_at_line.setdefault(t.line, max(depth, 0))
        if t.text == "{":
            depth += 1
    inside = [ln for ln, d in depth_at_line.items() if d > 0]
    ok = sum(1 for ln in inside if src.lines[ln - 1][:1] in (" ", "\t"))
    indents = {src.lines[ln - 1][:1] for ln in inside if src.lines[ln - 1][:1] in (" ", "\t")}
    return (ok / len(inside) if inside else 1.0), len(indents) > 1


def _multi_statement_lines(src: CSource) -> int:
    counts: Dict[int, int] = {}
    paren = 0
    for t in src.tokens:
        if t.text == "(":
            paren += 1
        elif t.text == ")":
            paren -= 1
        elif t.text == ";" and paren == 0:
            counts[t.line] = counts.get(t.line, 0) + 1
    return sum(1 for c in counts.values() if c > 1)


def _clarity(src: CSource):
    magic: List[str] = []
    for fn in src.functions:
        for t in src.body_tokens(fn):
            if t.kind == "num" and t.text.rstrip("uUlLfF") not in OK_LITERALS:
                magic.append(t.text)
    unique = sorted(set(magic))
    score = 1.0 / (1.0 + 0.2 * len(unique))
    return score, {"magic_numbers": unique[:10], "count": len(unique)}
