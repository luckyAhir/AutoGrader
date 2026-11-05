"""structure: per-function complexity/size/nesting plus static defect checks.

Sub-scores, averaged:
* cyclomatic    - share of functions with McCabe complexity <= max_cyclomatic
* length        - share of functions with NLOC <= max_function_length
* nesting       - share of functions with brace nesting <= max_nesting
* documentation - share of functions with a header comment just above them
                  (a file header comment also documents main)
* hygiene       - 1 - penalties for risky constructs (built-in checks) and for
                  cppcheck findings, when cppcheck is installed
"""

from __future__ import annotations

import re
import subprocess

from ..csource import find_calls
from .base import Analyzer, AnalyzerResult, register

UNSAFE_CALLS = {
    "gets": ("error", 0.5, "gets() cannot limit the input length (removed from C11); use fgets()"),
    "system": ("warning", 0.3, "system() runs a shell command; not acceptable in a submission"),
    "strcpy": ("warning", 0.1, "strcpy() can overflow the destination; prefer snprintf/strncpy with a size"),
    "strcat": ("warning", 0.1, "strcat() can overflow the destination"),
    "sprintf": ("warning", 0.1, "sprintf() can overflow the destination; use snprintf()"),
}
CPPCHECK_PENALTY = {"error": 0.25, "warning": 0.1, "performance": 0.05, "portability": 0.05, "style": 0.02}
CPPCHECK_IGNORE = {"missingIncludeSystem", "missingInclude", "checkersReport", "unmatchedSuppression",
                   "normalCheckLevelMaxBranches", "toomanyconfigs", "unusedFunction", "noValidConfiguration"}


@register
class StaticAnalysis(Analyzer):
    name = "static_analysis"
    description = "Function complexity, length, nesting and static defect checks (built-in + cppcheck)"

    def run(self, ctx, params):
        src = ctx.csource
        max_cc = int(params.get("max_cyclomatic", 10))
        max_len = int(params.get("max_function_length", 50))
        max_nest = int(params.get("max_nesting", 4))
        use_cppcheck = params.get("cppcheck", "auto")

        functions = [{"name": f.name, "line": f.start_line, "nloc": f.nloc, "cyclomatic": f.cyclomatic,
                      "nesting": f.max_nesting, "params": len([p for p in f.params if p])}
                     for f in src.functions]
        if not functions:
            return AnalyzerResult(0.0, "no functions found", "No function definitions could be found.",
                                  {"functions": []})

        def share(pred):
            return sum(1 for f in functions if pred(f)) / len(functions)

        first_code = min(src.code_lines()) if src.tokens else 1
        first_directive = min((d.line for d in src.directives), default=first_code)
        file_header = any(c.line < min(first_code, first_directive) for c in src.comments)
        for f, info in zip(src.functions, functions):
            above = any(f.start_line - 3 <= c.end_line < f.start_line for c in src.comments)
            info["header_comment"] = above or (f.name == "main" and file_header)
        subs = {
            "cyclomatic": share(lambda f: f["cyclomatic"] <= max_cc),
            "length": share(lambda f: f["nloc"] <= max_len),
            "nesting": share(lambda f: f["nesting"] <= max_nest),
            "documentation": share(lambda f: f["header_comment"]),
        }

        issues = self._builtin_issues(ctx)
        cppcheck_used = None
        if use_cppcheck and ctx.toolchain.cppcheck:
            cppcheck_used = ctx.toolchain.cppcheck_version
            issues.extend(self._cppcheck(ctx))
        penalty = sum(i["penalty"] for i in issues)
        subs["hygiene"] = max(0.0, 1.0 - penalty)
        score = sum(subs.values()) / len(subs)

        problems, suggestions = [], []
        for f in functions:
            if f["cyclomatic"] > max_cc:
                problems.append(f"{f['name']}() has cyclomatic complexity {f['cyclomatic']} (max {max_cc})")
                suggestions.append(f"Split {f['name']}() into smaller functions to reduce its branching.")
            if f["nloc"] > max_len:
                problems.append(f"{f['name']}() is {f['nloc']} lines long (max {max_len})")
                suggestions.append(f"Shorten {f['name']}() by extracting helper functions.")
            if f["nesting"] > max_nest:
                problems.append(f"{f['name']}() nests blocks {f['nesting']} deep (max {max_nest})")
                suggestions.append(f"Reduce nesting in {f['name']}() with early returns or helpers.")
        undocumented = [f["name"] + "()" for f in functions if not f["header_comment"]]
        if undocumented:
            problems.append("no header comment on " + ", ".join(undocumented[:4]))
            suggestions.append("Put a one-line comment above each function saying what it does "
                               "(and a file header comment for the program).")
        for issue in issues[:5]:
            problems.append(f"line {issue['line']}: {issue['message']}")
            if issue.get("fix") and issue["fix"] not in suggestions:
                suggestions.append(issue["fix"])

        if len(functions) == 1 and functions[0]["name"] == "main":
            problems.insert(0, "single-block code (everything in main)")
        summary = "; ".join(problems[:2]) if problems else "functions small and simple; no static issues"
        feedback = ("Structure is clean." if not problems else "Structure issues: " + "; ".join(problems[:5]) + ".")
        return AnalyzerResult(score, summary, feedback, {
            "functions": functions,
            "limits": {"max_cyclomatic": max_cc, "max_function_length": max_len, "max_nesting": max_nest},
            "subscores": {k: round(v, 4) for k, v in subs.items()},
            "issues": issues,
            "cppcheck": cppcheck_used or "not available",
        }, suggestions)

    # ------------------------------------------------------------ checks
    def _builtin_issues(self, ctx):
        src = ctx.csource
        issues = []
        for name, lines in sorted(find_calls(src, set(UNSAFE_CALLS)).items()):
            sev, pen, msg = UNSAFE_CALLS[name]
            issues.append({"line": lines[0], "severity": sev, "id": f"unsafe-{name}", "message": msg,
                           "penalty": pen, "fix": msg[0].upper() + msg[1:] + "."})
        # scanf whose return value is never inspected
        toks = src.tokens
        for i, t in enumerate(toks[:-1]):
            if t.text in ("scanf", "fscanf") and toks[i + 1].text == "(" and i > 0 \
                    and toks[i - 1].text in (";", "{", "}", ")"):
                close = src.partner[i + 1]
                if close > 0 and close + 1 < len(toks) and toks[close + 1].text == ";":
                    issues.append({"line": t.line, "severity": "style", "id": "unchecked-input",
                                   "message": f"return value of {t.text}() is not checked (malformed input goes unnoticed)",
                                   "penalty": 0.1,
                                   "fix": "Check how many items scanf() read, e.g. `if (scanf(...) != 2)`."})
                    break
        mutable_globals = [d for d in src.globals if not d.is_const]
        for d in mutable_globals[:3]:
            issues.append({"line": d.line, "severity": "style", "id": "global-variable",
                           "message": f"mutable global variable '{d.name}'", "penalty": 0.1,
                           "fix": "Pass data through function parameters instead of global variables."})
        for t in toks:
            if t.text == "goto":
                issues.append({"line": t.line, "severity": "style", "id": "goto", "message": "goto statement",
                               "penalty": 0.1, "fix": "Replace goto with structured loops/returns."})
                break
        m = re.search(r"\bvoid\s+main\s*\(", src.text)
        if m:
            issues.append({"line": src.text[:m.start()].count("\n") + 1, "severity": "warning", "id": "void-main",
                           "message": "main must return int", "penalty": 0.2, "fix": "Declare `int main(void)`."})
        if re.search(r"fflush\s*\(\s*stdin\s*\)", src.text):
            issues.append({"line": 0, "severity": "warning", "id": "fflush-stdin",
                           "message": "fflush(stdin) is undefined behaviour", "penalty": 0.2,
                           "fix": "Do not flush stdin; read and discard input instead."})
        if "conio.h" in src.includes:
            issues.append({"line": 0, "severity": "portability", "id": "conio",
                           "message": "<conio.h> is not standard C", "penalty": 0.2,
                           "fix": "Remove <conio.h>/getch(); use standard I/O."})
        return issues

    def _cppcheck(self, ctx):
        d = ctx.workdir / "cppcheck"
        d.mkdir(exist_ok=True)
        (d / ctx.name).write_bytes(ctx.submission.read_bytes())
        cmd = [ctx.toolchain.cppcheck, "--enable=warning,style,performance,portability", "--inline-suppr",
               "--std=c11", "--quiet", "--suppress=missingIncludeSystem",
               "--template={line}|{severity}|{id}|{message}", ctx.name]
        try:
            proc = subprocess.run(cmd, cwd=str(d), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return []
        issues = []
        for line in proc.stderr.decode("utf-8", "replace").splitlines():
            parts = line.split("|", 3)
            if len(parts) != 4 or parts[2] in CPPCHECK_IGNORE:
                continue
            sev = parts[1].strip()
            if sev not in CPPCHECK_PENALTY:
                continue
            issues.append({"line": int(parts[0]) if parts[0].isdigit() else 0, "severity": sev,
                           "id": f"cppcheck:{parts[2]}", "message": parts[3].strip(),
                           "penalty": CPPCHECK_PENALTY[sev], "fix": None})
        issues.sort(key=lambda i: (i["line"], i["id"]))
        return issues
