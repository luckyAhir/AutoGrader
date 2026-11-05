"""Rendering and writing reports.

* ``<stem>.json`` - the complete machine-readable report (evidence included)
* ``<stem>.txt``  - what the student reads
* ``<stem>_counterexamples/CE-NN.in|.expected`` - full failing inputs, ready to
  reproduce with ``./prog < CE-01.in``
* ``summary.csv`` / ``summary.md`` - one row per submission in batch mode
"""

from __future__ import annotations

import csv
import io
import json
import textwrap
from pathlib import Path
from typing import Any, Dict, List

from .util import fmt_num, one_line

WIDTH = 92


def _io(text, total, limit: int = 70) -> str:
    """One display line of program I/O, with the true size when it is long."""
    if text is None:
        return "(none)"
    if total and total > limit:
        return one_line(text[:limit], limit + 10) + f" ... ({total:,} chars in file)"
    return one_line(text, limit)


def _wrap(text: str, indent: str, first: str = "") -> List[str]:
    return textwrap.wrap(text, width=WIDTH, initial_indent=first or indent, subsequent_indent=indent,
                         break_long_words=False, break_on_hyphens=False) or [first.rstrip()]


def render_text(report: Dict[str, Any]) -> str:
    L: List[str] = []
    fp = report["first_pass"]
    L.append(f"===== Grading report for {report['submission']} =====")
    L.append(f"Overall grade: {report['grade']:.1f} / {report['max_grade']}")
    L.append(f"Problem: {report['problem']}    Rules: {report['rules_file']} ({report['rules_hash'][:19]}...)")
    smoke = fp.get("smoke_verdict", "not run")
    L.append(f"First pass: compiled={'yes' if fp['compiled'] else 'NO'}  smoke run={smoke}  "
             f"suspected_infinite_loop={'YES' if fp['suspected_infinite_loop'] else 'no'}")
    for err in fp.get("compile_errors", [])[:5]:
        L.append(f"  compiler: {err}")
    L.append("")
    L.append(f"{'Metric':<15}{'Weight':>6}  {'Score':>11}  {'Contribution':>12}  Verdict")
    for m in report["metrics"]:
        weight = fmt_num(100 * m["weight"])
        if m["score"] is None:
            score_s, contrib_s = "n/a", "-"
        else:
            pts = 100 * m["effective_weight"]
            score_s = f"{fmt_num(round(pts * m['score'], 1))}/{fmt_num(round(pts, 1))}"
            contrib_s = f"{m['contribution']:.1f}"
        prefix = {"skipped_environment": "not evaluated: ", "error": "analyzer error: "}.get(m["status"], "")
        summary = " ".join((prefix + m["summary"]).split())
        if len(summary) > 62:
            summary = summary[:59].rstrip() + "..."
        L.append(f"{m['name']:<15}{weight:>6}  {score_s:>11}  {contrib_s:>12}  {summary}")

    if report["flags"]:
        L.append("")
        L.append("--- Flags ---")
        for f in report["flags"]:
            L.extend(_wrap(f"[{f['severity']}] {f['type']}: {f['message']}", "    ", "  - "))

    if report["counterexamples"]:
        L.append("")
        L.append("--- Where you failed ---")
        for ce in report["counterexamples"]:
            title = ce["label"] if ce["label"].startswith("Test") else f"{ce['id']} [{ce['source']}" + \
                (", minimized" if ce["minimized"] else "") + "]"
            sizes = ce.get("sizes", {})
            L.append(f"{title}  ({ce['verdict']})")
            L.append(f"    INPUT:    {_io(ce['input'], sizes.get('input'))}")
            if ce.get("expected") is not None:
                L.append(f"    EXPECTED: {_io(ce['expected'], sizes.get('expected'))}")
            L.append(f"    GOT:      {_io(ce['got'], sizes.get('got'))}")
            L.extend(_wrap(ce["why"], " " * 14, "    WHY:      "))
            if ce.get("hint"):
                L.extend(_wrap(ce["hint"], " " * 14, "    FIX:      "))
            if ce.get("original_input"):
                L.append(f"    (shrunk from: {one_line(ce['original_input'], 56)})")
            L.append(f"    reproduce: ./prog < {ce['files']['input']}")

    mem = next((m for m in report["metrics"] if m["analyzer"] == "sanitizer_run" and m["evidence"].get("findings")),
               None)
    if mem:
        L.append("")
        L.append("--- Memory & undefined-behaviour diagnostics ---")
        for f in mem["evidence"]["findings"]:
            where = f["location"] or "location unknown"
            L.extend(_wrap(f"{f['kind']} at {where}: {f['message']} (first seen on {f['trigger']}: "
                           f"{f['trigger_input']})", "    ", "  - "))

    cx = next((m for m in report["metrics"] if m["analyzer"] == "complexity_estimator"
               and m["evidence"].get("measurements")), None)
    if cx:
        ev = cx["evidence"]
        L.append("")
        L.append("--- Complexity & profile (gcov operation counts) ---" if ev["method"].startswith("gcov")
                 else "--- Complexity (CPU time, approximate) ---")
        L.append("  n:   " + "  ".join(f"{p['n']:>10}" for p in ev["measurements"]))
        L.append("  ops: " + "  ".join(f"{p['ops']:>10}" for p in ev["measurements"]))
        if ev.get("reference_ops"):
            L.append("  ref: " + "  ".join(f"{p['ops']:>10}" for p in ev["reference_ops"]))
        L.append(f"  estimated {ev['estimated']} (required {ev['required']}); log-log slope "
                 f"{ev['fit'].get('loglog_slope')}")
        for h in ev.get("hot_lines", [])[:3]:
            L.append(f"  hot line {h['line']:>3}: {h['count']:>12,} executions ({h['share']:.0%})  {h['code'][:50]}")

    L.append("")
    L.append("--- Feedback by metric ---")
    for m in report["metrics"]:
        L.extend(_wrap(m["feedback"], " " * 17, f"  {m['name']:<15}"))

    if report["suggestions"]:
        L.append("")
        L.append("--- Suggestions to raise the grade (most points first) ---")
        for s in report["suggestions"]:
            L.extend(_wrap(f"{s['suggestion']} [{s['metric']}, up to {fmt_num(s['points_at_stake'])} pts]",
                           "    ", "  - "))

    ul = report.get("usefulness_limitations") or {}
    if ul:
        L.append("")
        L.append("--- Usefulness & limitations of this evaluation ---")
        L.append("Useful for:")
        for i, u in enumerate(ul.get("usefulness", []), 1):
            L.append(f"  {i}. {u['use_case']}")
            L.extend(_wrap("Example: " + u["example"], "     "))
            L.extend(_wrap("Benefit: " + u["benefit"], "     "))
        L.append("Limits of this report:")
        for i, lim in enumerate(ul.get("limitations", []), 1):
            L.append(f"  {i}. {lim['limitation']}")
            L.extend(_wrap("Example: " + lim["example"], "     "))
            L.extend(_wrap("Mitigation: " + lim["mitigation"], "     "))

    L.append("")
    L.append("--- Reproducibility ---")
    L.append(f"  submission {report['submission_sha256'][:23]}...  rules {report['rules_hash'][:23]}...")
    L.append(f"  assets {report['assets_hash'][:27]}...  grade fingerprint {report['grade_fingerprint'][:23]}...")
    tools = ", ".join(f"{k} {v}" for k, v in report["tool_versions"].items() if v)
    L.extend(_wrap(f"tools: {tools}", "    ", "  "))
    return "\n".join(L) + "\n"


def write_outputs(result, out_dir: Path) -> Dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = result.report
    stem = Path(report["submission"]).stem
    for name in result.artifacts:
        folder = out_dir / Path(name).parent
        if folder.is_dir():  # clear counterexamples from an older run
            for old in folder.glob("CE-*"):
                old.unlink()
    for name, content in result.artifacts.items():
        path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    json_path = out_dir / f"{stem}.json"
    txt_path = out_dir / f"{stem}.txt"
    _write(json_path, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    _write(txt_path, render_text(report))
    return {"json": json_path, "text": txt_path}


def write_summary(reports: List[Dict[str, Any]], out_dir: Path) -> Dict[str, Path]:
    out_dir = Path(out_dir)
    names = [m["name"] for m in reports[0]["metrics"]] if reports else []
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["submission", "grade"] + names + ["flags"])
    for r in reports:
        scores = {m["name"]: m for m in r["metrics"]}
        row = [r["submission"], f"{r['grade']:.1f}"]
        for n in names:
            m = scores.get(n)
            row.append("n/a" if not m or m["score"] is None else f"{m['score']:.3f}")
        row.append(";".join(sorted({f["type"] for f in r["flags"]})))
        writer.writerow(row)
    csv_path = out_dir / "summary.csv"
    _write(csv_path, buf.getvalue())

    md = ["| submission | grade | " + " | ".join(names) + " | flags |",
          "|---|---:|" + "---:|" * len(names) + "---|"]
    for r in sorted(reports, key=lambda r: (-r["grade"], r["submission"])):
        scores = {m["name"]: m for m in r["metrics"]}
        cells = []
        for n in names:
            m = scores.get(n)
            cells.append("n/a" if not m or m["score"] is None else f"{m['contribution']:.1f}")
        flags = ", ".join(sorted({f["type"] for f in r["flags"] if f["severity"] != "info"})) or "-"
        md.append(f"| {r['submission']} | **{r['grade']:.1f}** | " + " | ".join(cells) + f" | {flags} |")
    md_path = out_dir / "summary.md"
    _write(md_path, "\n".join(md) + "\n\n(cells = points contributed to the grade)\n")
    return {"csv": csv_path, "md": md_path}


def _write(path: Path, text: str) -> None:
    """UTF-8 with LF line endings on every platform (reports are byte-identical)."""
    path.write_bytes(text.encode("utf-8"))
