"""memory_safety: run the program under AddressSanitizer + UBSan (+ LeakSanitizer).

Backends are tried in order (``"backend": "auto"``):
1. ``asan``     - compile with the rule's ``-fsanitize=`` flags; detects buffer
                  overflows, use-after-free, double free, leaks (LSan) and
                  undefined behaviour such as signed overflow (UBSan).
2. ``valgrind`` - memcheck on a ``-g -O0`` build, if valgrind is installed.
3. ``ubsan-trap`` - ``-fsanitize=undefined -fsanitize-undefined-trap-on-error``,
                  which needs no runtime library (works on MinGW). It only
                  shows *that* undefined behaviour happened.
If none works, the metric is reported as not evaluated (environment).

Findings are deduplicated by (kind, source line). Each unique finding costs
a penalty (memory error 0.5, leak 0.25, undefined behaviour 0.2 by default),
and the score is ``1 - sum(penalties)``, floored at 0. Addresses and PIDs are
stripped, so the evidence is identical between runs.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from ..util import one_line
from .base import SKIPPED_ENVIRONMENT, Analyzer, AnalyzerResult, register

DEFAULT_PENALTIES = {"memory_error": 0.5, "leak": 0.25, "undefined_behavior": 0.2}

ASAN_KIND_INFO = {
    "heap-buffer-overflow": "read/write past the end of a malloc'ed block - check loop bounds (i < n) and allocation sizes",
    "stack-buffer-overflow": "read/write past the end of a local array - the array is too small or an index runs past it",
    "global-buffer-overflow": "read/write past the end of a global/static array",
    "heap-use-after-free": "memory used after free() - set pointers to NULL after freeing and do not reuse them",
    "double-free": "the same block was freed twice",
    "attempting double-free": "the same block was freed twice",
    "SEGV": "invalid memory access (NULL or garbage pointer)",
    "stack-overflow": "stack overflow - recursion too deep or a huge local array",
    "alloc-dealloc-mismatch": "memory released with the wrong function",
    "bad-free": "free() called on memory that was not allocated with malloc",
}

_ASAN_ERR = re.compile(r"ERROR: AddressSanitizer: ([\w-]+(?: on unknown address)?)")
_ASAN_FREE = re.compile(r"ERROR: AddressSanitizer: attempting (double-free|free on address which was not malloc)")
_FRAME = re.compile(r"#\d+\s+0x[0-9a-fA-F]+\s+in\s+(\S+)\s+(\S+?):(\d+)")
_LEAK = re.compile(r"(Direct|Indirect) leak of (\d+) byte\(s\) in (\d+) object\(s\) allocated from:")
_UBSAN = re.compile(r"^(\S+?):(\d+):(?:\d+:)?\s*runtime error:\s*(.+)$", re.M)
_ADDR = re.compile(r"0x[0-9a-fA-F]+")


@register
class SanitizerRun(Analyzer):
    name = "sanitizer_run"
    description = "Memory errors, leaks and undefined behaviour (ASan/UBSan/LSan, valgrind fallback)"
    needs_binary = True

    def run(self, ctx, params):
        flags = str(params.get("flags", "-fsanitize=address,undefined"))
        backend_req = params.get("backend", "auto")
        penalties = dict(DEFAULT_PENALTIES, **(params.get("penalties") or {}))
        inputs = self._inputs(ctx, params)
        if not inputs:
            return AnalyzerResult.skipped(SKIPPED_ENVIRONMENT, "no inputs to run under sanitizers")

        backend, exe, note = self._prepare(ctx, flags, backend_req)
        if backend is None:
            return AnalyzerResult.skipped(SKIPPED_ENVIRONMENT, note)

        limits = ctx.limits.scaled(5.0 if backend != "valgrind" else 30.0, memory_mb=None)
        mem = ctx.rules.memory_limit_mb
        env = {
            "ASAN_OPTIONS": f"detect_leaks=1:halt_on_error=1:abort_on_error=0:exitcode=86:symbolize=1:"
                            f"allocator_may_return_null=1:detect_stack_use_after_return=0:"
                            f"hard_rss_limit_mb={max(256, 4 * mem)}",
            "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=0:report_error_type=1",
            "LSAN_OPTIONS": "exitcode=87",
        }
        findings: Dict[tuple, Dict] = {}
        runs = 0
        for label, text in inputs:
            if backend == "valgrind":
                xml_path = ctx.workdir / "valgrind" / f"vg_{runs}.xml"
                argv = [ctx.toolchain.valgrind, "--leak-check=full", "--errors-for-leak-kinds=definite",
                        "--error-exitcode=99", "--xml=yes", f"--xml-file={xml_path}", str(exe)]
                run = ctx.sandbox.run(argv, text, limits)
                found = self._parse_valgrind(ctx, xml_path)
            else:
                run = ctx.sandbox.run([str(exe)], text, limits, env_extra=env)
                found = self._parse_sanitizers(ctx, run.stderr) if backend == "asan" else []
                if backend == "ubsan-trap" and run.signal_name == "SIGILL":
                    found = [{"kind": "undefined behaviour (trapped)", "category": "undefined_behavior",
                              "location": None, "message": "UBSan trap: details unavailable in trap mode"}]
            runs += 1
            for f in found:
                key = (f["kind"], f["location"])
                if key not in findings:
                    findings[key] = dict(f, trigger=label, trigger_input=one_line(text, 60), occurrences=0)
                findings[key]["occurrences"] += 1
            if "LeakSanitizer has encountered a fatal error" in run.stderr:
                note = "LeakSanitizer could not run here (ptrace restricted); leaks not checked"

        ordered = sorted(findings.values(), key=lambda f: (f["category"], f["location"] or "", f["kind"]))
        total_penalty = sum(penalties.get(f["category"], 0.2) for f in ordered)
        score = max(0.0, 1.0 - total_penalty)
        evidence = {"backend": backend, "flags": flags if backend == "asan" else None, "runs": runs,
                    "findings": ordered}
        if note:
            evidence["note"] = note
        tool = {"asan": "ASan/UBSan", "valgrind": "valgrind memcheck", "ubsan-trap": "UBSan (trap mode)"}[backend]
        if not ordered:
            return AnalyzerResult(1.0, f"{tool} clean", f"No memory errors, leaks or undefined behaviour "
                                  f"detected by {tool} on {runs} run(s).", evidence)
        parts = []
        for f in ordered[:3]:
            where = f" at {f['location']}" if f["location"] else ""
            parts.append(f"{f['kind']}{where}")
        summary = "; ".join(parts)
        feedback = f"{tool} reports {len(ordered)} problem(s): " + "; ".join(
            f"{f['kind']}{' at ' + f['location'] if f['location'] else ''} ({f['message']}), "
            f"triggered by {f['trigger']}" for f in ordered[:4]) + "."
        suggestions = []
        for f in ordered:
            tip = self._advice(f)
            if tip and tip not in suggestions:
                suggestions.append(tip)
        flags_out = [{"type": "memory_leak", "severity": "medium",
                      "message": f"memory leak: {f['message']} ({f['location'] or 'unknown location'})"}
                     for f in ordered if f["category"] == "leak"]
        return AnalyzerResult(score, summary, feedback, evidence, suggestions, flags=flags_out)

    # ------------------------------------------------------------ setup
    def _inputs(self, ctx, params):
        wanted = params.get("inputs", ["sample", "tests"])
        max_runs = int(params.get("max_runs", 20))
        max_bytes = int(params.get("max_input_bytes", 200_000))
        out, seen = [], set()

        def add(label, text):
            if text is None or len(text) > max_bytes or text in seen or len(out) >= max_runs:
                return
            seen.add(text)
            out.append((label, text))

        if "sample" in wanted:
            add("the sample input", ctx.sample_input())
        if "tests" in wanted:
            for m in ctx.rules.metrics:
                if m.analyzer == "io_tests" and m.params.get("testset_dir"):
                    for tc in ctx.testset(m.params["testset_dir"]):
                        add(f"hidden test {tc.name}", tc.input)
        if "edge_cases" in wanted:
            for m in ctx.rules.metrics:
                if m.analyzer == "edge_case_runner":
                    for tc in ctx.testset(m.params.get("edge_file", "edge_case.json")):
                        add(f"edge case {tc.name}", tc.input)
        return out

    def _prepare(self, ctx, flags, backend_req):
        std = ctx.rules.std_flag
        notes = []
        if backend_req in ("auto", "asan"):
            if ctx.toolchain.supports(flags, ctx.sandbox.scratch):
                res = ctx.build("sanitize", f"{flags} -g -O1 -fno-omit-frame-pointer {std}")
                if res.ok:
                    return "asan", res.exe, None
                notes.append("the sanitizer build failed")
            else:
                notes.append(f"the compiler cannot build/run with {flags} on this platform")
        if backend_req in ("auto", "valgrind") and ctx.toolchain.valgrind:
            res = ctx.build("valgrind", f"-g -O0 {std}")
            if res.ok:
                (ctx.workdir / "valgrind").mkdir(exist_ok=True)
                return "valgrind", res.exe, "; ".join(notes) or None
        if backend_req in ("auto", "ubsan-trap"):
            trap = "-fsanitize=undefined -fsanitize-undefined-trap-on-error"
            if ctx.toolchain.supports(trap, ctx.sandbox.scratch):
                res = ctx.build("ubsan_trap", f"{trap} -O1 {std}")
                if res.ok:
                    notes.append("ASan/valgrind unavailable: only undefined behaviour is checked (no leak or "
                                 "buffer-overflow detection)")
                    return "ubsan-trap", res.exe, "; ".join(notes)
        return None, None, "no sanitizer backend available (" + ("; ".join(notes) or "none requested") + ")"

    # ------------------------------------------------------------ parsing
    def _user_frame(self, ctx, lines: List[str]) -> Optional[str]:
        for line in lines:
            m = _FRAME.search(line)
            if m and m.group(2).replace("\\", "/").split("/")[-1] == ctx.name:
                return f"{ctx.name}:{m.group(3)} in {m.group(1)}()"
        return None

    def _parse_sanitizers(self, ctx, stderr: str) -> List[Dict]:
        found = []
        lines = stderr.splitlines()
        for i, line in enumerate(lines):
            m = _ASAN_FREE.search(line) or _ASAN_ERR.search(line)
            if m and "LeakSanitizer" not in line:
                kind = m.group(1).replace(" on unknown address", "")
                if kind == "free on address which was not malloc":
                    kind = "bad-free"
                found.append({"kind": kind, "category": "memory_error",
                              "location": self._user_frame(ctx, lines[i + 1:i + 40]),
                              "message": ASAN_KIND_INFO.get(kind, "memory error")})
            m = _LEAK.search(line)
            if m:
                found.append({"kind": f"memory leak ({m.group(1).lower()})", "category": "leak",
                              "location": self._user_frame(ctx, lines[i + 1:i + 20]),
                              "message": f"{m.group(2)} byte(s) in {m.group(3)} allocation(s) never freed"})
        for m in _UBSAN.finditer(stderr):
            file = m.group(1).replace("\\", "/").split("/")[-1]
            message = _ADDR.sub("0x...", m.group(3).strip())
            kind = message.split(":")[0]
            found.append({"kind": kind, "category": "undefined_behavior",
                          "location": f"{file}:{m.group(2)}", "message": message})
        return found

    def _parse_valgrind(self, ctx, xml_path) -> List[Dict]:
        try:
            root = ET.parse(str(xml_path)).getroot()
        except (ET.ParseError, OSError):
            return []
        found = []
        for err in root.findall("error"):
            kind = err.findtext("kind", "error")
            what = err.findtext("what") or err.findtext("xwhat/text") or kind
            location = None
            for frame in err.findall("stack/frame"):
                if frame.findtext("file") == ctx.name:
                    location = f"{ctx.name}:{frame.findtext('line')} in {frame.findtext('fn')}()"
                    break
            category = "leak" if kind.startswith("Leak_") else "memory_error"
            found.append({"kind": kind, "category": category, "location": location,
                          "message": _ADDR.sub("0x...", what)})
        return found

    @staticmethod
    def _advice(f) -> Optional[str]:
        where = f" ({f['location']})" if f.get("location") else ""
        if f["category"] == "leak":
            return f"Free every malloc'ed block before returning{where}."
        if f["category"] == "undefined_behavior":
            if "overflow" in f["kind"]:
                return f"Avoid signed integer overflow{where}: use a wider type or check the range first."
            return f"Remove the undefined behaviour{where}: {f['message']}."
        return f"Fix the memory error{where}: {f['message']}."
