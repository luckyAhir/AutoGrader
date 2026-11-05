"""Helpers shared by the execution-based analyzers."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..explain import explain
from ..judge import Judgement
from ..util import shorten


def failure_entry(ctx, *, source: str, judgement: Judgement, input_text: str, category: Optional[str] = None,
                  variant: str = "normal", size: Optional[int] = None, minimized: bool = False,
                  original_input: Optional[str] = None, label: str = "") -> Dict[str, Any]:
    exp = explain(judgement.verdict, input_text, judgement.expected, judgement.got, judgement.run,
                  category=category, variant=variant, size=size, source=ctx.source_text,
                  time_limit_s=ctx.rules.time_limit_s, memory_limit_mb=ctx.rules.memory_limit_mb)
    entry: Dict[str, Any] = {
        "source": source,
        "label": label,
        "category": category,
        "verdict": judgement.verdict,
        "input": input_text,
        "expected": judgement.expected,
        "got": judgement.got if judgement.verdict not in ("TLE", "RE", "MLE", "OLE") or judgement.got
        else None,
        "detail": judgement.detail,
        "why": exp.why,
        "hint": exp.hint,
        "tag": exp.tag,
        "minimized": minimized,
    }
    if judgement.verdict in ("TLE", "RE", "MLE", "OLE") and not judgement.got:
        entry["got"] = f"({judgement.detail})"
    if original_input is not None and original_input != input_text:
        entry["original_input"] = original_input
    return entry


def evidence_view(entry: Dict[str, Any], limit: int = 300) -> Dict[str, Any]:
    """Compact form of a failure for the JSON evidence."""
    view = {
        "input": shorten(entry["input"], limit),
        "expected": shorten(entry.get("expected"), limit),
        "got": shorten(entry.get("got"), limit),
        "verdict": entry["verdict"],
        "note": entry["why"],
    }
    if entry.get("label"):
        view = {"case": entry["label"], **view}
    if entry.get("minimized"):
        view["minimized"] = True
    return view


def hints(entries: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for e in entries:
        if e.get("hint") and e["hint"] not in out:
            out.append(e["hint"])
    return out
