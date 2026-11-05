"""Discovery of the external tools the analyzers rely on.

Only a C compiler is mandatory. gcov (complexity), cppcheck (structure) and
valgrind (memory safety fallback) are optional: an analyzer whose tool is
missing either falls back to a weaker method or reports that it could not run
(and the coordinator redistributes its weight, recording a flag). Every report
lists the tool versions used, because a different compiler can legitimately
produce a different verdict.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


class ToolchainError(RuntimeError):
    pass


def _first_line(cmd: List[str]) -> Optional[str]:
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = out.stdout.decode("utf-8", "replace").strip()
    return text.splitlines()[0].strip() if text else None


def _version_number(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
    return m.group(1) if m else text


@dataclass
class Toolchain:
    cc: str
    cc_version: str
    cc_family: str
    gcov: Optional[str] = None
    cppcheck: Optional[str] = None
    cppcheck_version: Optional[str] = None
    valgrind: Optional[str] = None
    valgrind_version: Optional[str] = None
    _probes: Dict[str, bool] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def is_windows(self) -> bool:
        return os.name == "nt"

    @property
    def exe_suffix(self) -> str:
        return ".exe" if self.is_windows else ""

    @property
    def platform(self) -> str:
        return "windows" if self.is_windows else sys.platform

    def versions(self) -> Dict[str, Optional[str]]:
        return {
            self.cc_family: self.cc_version,
            "gcov": self.cc_version if self.gcov else None,
            "cppcheck": self.cppcheck_version,
            "valgrind": self.valgrind_version,
            "python": platform.python_version(),
            "platform": self.platform,
        }

    def supports(self, flags: str, scratch: Path) -> bool:
        """Can we compile *and run* a tiny program with these flags? (cached)"""
        with self._lock:
            if flags in self._probes:
                return self._probes[flags]
        import shlex

        tag = hashlib.sha256(flags.encode()).hexdigest()[:10]
        d = Path(scratch) / f"probe_{tag}"
        d.mkdir(parents=True, exist_ok=True)
        src = d / "probe.c"
        src.write_text(
            "#include <stdio.h>\n#include <stdlib.h>\n"
            "int main(void){int *p=malloc(sizeof *p); if(!p) return 1; *p=41;"
            " printf(\"%d\\n\", *p+1); free(p); return 0;}\n"
        )
        exe = d / ("probe" + self.exe_suffix)
        ok = False
        try:
            res = subprocess.run(
                [self.cc] + shlex.split(flags) + [str(src), "-o", str(exe)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
            )
            if res.returncode == 0 and exe.exists():
                run = subprocess.run([str(exe)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
                ok = run.returncode == 0 and run.stdout.strip() == b"42"
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        with self._lock:
            self._probes[flags] = ok
        return ok


def detect(cc: Optional[str] = None) -> Toolchain:
    cc = cc or os.environ.get("AUTOGRADER_CC") or shutil.which("gcc") or shutil.which("cc") or shutil.which("clang")
    if not cc:
        raise ToolchainError("no C compiler found: install gcc or pass --cc / set AUTOGRADER_CC")
    resolved = shutil.which(cc) or cc
    if not Path(resolved).exists():
        raise ToolchainError(f"C compiler not found: {cc}")

    banner = _first_line([resolved, "--version"]) or ""
    family = "clang" if "clang" in banner.lower() else "gcc"
    number = _first_line([resolved, "-dumpfullversion", "-dumpversion"]) or _version_number(banner) or "unknown"

    tc = Toolchain(cc=resolved, cc_version=number.strip(), cc_family=family)

    # gcov must match the compiler version, so look next to the compiler first.
    cc_path = Path(resolved)
    suffix = ".exe" if os.name == "nt" else ""
    stem = cc_path.stem  # e.g. gcc, gcc-13, x86_64-w64-mingw32-gcc
    candidates = [cc_path.with_name(stem.replace("gcc", "gcov") + suffix), cc_path.with_name("gcov" + suffix)]
    gcov = next((str(c) for c in candidates if "gcc" in stem and c.exists()), None)
    if gcov is None and family == "gcc":
        gcov = shutil.which("gcov")
    tc.gcov = gcov

    tc.cppcheck = os.environ.get("AUTOGRADER_CPPCHECK") or shutil.which("cppcheck")
    if tc.cppcheck:
        tc.cppcheck_version = _version_number(_first_line([tc.cppcheck, "--version"]))
        if tc.cppcheck_version is None:
            tc.cppcheck = None
    if os.name != "nt":
        tc.valgrind = os.environ.get("AUTOGRADER_VALGRIND") or shutil.which("valgrind")
        if tc.valgrind:
            tc.valgrind_version = _version_number(_first_line([tc.valgrind, "--version"]))
    return tc
