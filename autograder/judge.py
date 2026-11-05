"""Reference-solution oracle and run judging.

The oracle is the instructor's reference solution, compiled once per grading
session. It supplies expected outputs for *generated* inputs (edge cases,
counterexamples, minimisation). Its answers are cached by input hash, so a
batch of submissions shares the work.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .compare import compare_outputs
from .sandbox import Limits, RunResult, Sandbox
from .util import normalize_newlines, sha256_text


@dataclass
class Judgement:
    verdict: str  # AC | WA | PE | TLE | MLE | RE | OLE | ERR | OK (no oracle, ran cleanly)
    expected: Optional[str]
    got: str
    run: RunResult
    detail: str

    @property
    def passed(self) -> bool:
        return self.verdict in ("AC", "OK")


def judge_run(run: RunResult, expected: Optional[str], compare: Dict, nonzero_exit_is_error: bool = True) -> Judgement:
    got = normalize_newlines(run.stdout)
    if run.status in ("TLE", "MLE", "OLE", "ERR"):
        return Judgement(run.status, expected, got, run, run.detail)
    if run.status == "RE" and (run.signal_name or nonzero_exit_is_error):
        return Judgement("RE", expected, got, run, run.detail)
    if expected is None:
        return Judgement("OK", None, got, run, "no expected output available")
    cmp = compare_outputs(expected, got, compare.get("mode", "lines"),
                          float(compare.get("abs_eps", 1e-6)), float(compare.get("rel_eps", 1e-6)))
    return Judgement(cmp.verdict, expected, got, run, cmp.detail)


class Oracle:
    def __init__(self, sandbox: Sandbox, rules, workdir: Path):
        self.sandbox = sandbox
        self.rules = rules
        self.workdir = Path(workdir)
        self._lock = threading.Lock()
        self._built: Optional[bool] = None
        self._exe: Optional[Path] = None
        self._cache: Dict[str, Optional[str]] = {}
        self.error: Optional[str] = None

    def build(self) -> bool:
        with self._lock:
            if self._built is None:
                ref = self.rules.reference_path
                if not ref.is_file():  # e.g. llm_tests could not generate it
                    self._built, self.error = False, f"reference solution {ref.name} does not exist"
                    return False
                self.workdir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ref, self.workdir / ref.name)
                exe = "reference" + self.sandbox.toolchain.exe_suffix
                res = self.sandbox.compile([ref.name], exe, flags=f"-O2 {self.rules.std_flag}",
                                           link_flags=self.rules.link_flags, cwd=self.workdir)
                self._built = res.ok
                self._exe = res.exe
                self.error = None if res.ok else res.output
            return bool(self._built)

    @property
    def exe(self) -> Optional[Path]:
        return self._exe if self.build() else None

    def limits(self) -> Limits:
        tl = self.rules.time_limit_s
        return Limits(cpu_time_s=max(4 * tl, 2.0), wall_time_s=max(12 * tl, 10.0),
                      memory_mb=4 * self.rules.memory_limit_mb,
                      output_bytes=self.rules.output_limit_kb * 1024 * 2)

    def expected(self, input_text: str) -> Optional[str]:
        """Reference output, or None if the reference rejects the input."""
        if not self.build():
            return None
        key = sha256_text(input_text)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        run = self.sandbox.run([str(self._exe)], input_text, self.limits())
        out = normalize_newlines(run.stdout) if run.ok else None
        with self._lock:
            self._cache[key] = out
        return out
