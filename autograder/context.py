"""The context the coordinator hands to every analyzer.

It bundles the submission, the parsed source, the rules, the sandbox and the
shared services (reference oracle, input generator, LLM cache). It also
provides thread-safe helpers that several analyzers need: building a variant
of the submission with different flags, loading a test set, and judging one
run.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .csource import CSource
from .judge import Judgement, Oracle, judge_run
from .sandbox import CompileResult, Limits, RunResult, Sandbox
from .util import natural_key, normalize_newlines, read_text


@dataclass
class TestCase:
    name: str
    input: str
    expected: Optional[str]
    path: Path


class AnalysisContext:
    def __init__(self, *, submission: Path, source_text: str, csource: CSource, rules, toolchain,
                 sandbox: Sandbox, workdir: Path, oracle: Optional[Oracle], inputgen, cache_dir: Path, size_inputgen=None,
                 submissions_dir: Optional[Path] = None):
        self.submission = Path(submission)
        self.source_text = source_text
        self.csource = csource
        self.rules = rules
        self.toolchain = toolchain
        self.sandbox = sandbox
        self.workdir = Path(workdir)
        self.oracle = oracle
        self.inputgen = inputgen
        self.submissions_dir = Path(submissions_dir) if submissions_dir else None  # kept graded programs
        self.size_inputgen = size_inputgen  # input format the model described (only when the rules have no spec)
        self.cache_dir = Path(cache_dir)
        self.binary: Optional[Path] = None
        self.first_pass: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._build_locks: Dict[str, threading.Lock] = {}
        self._builds: Dict[str, CompileResult] = {}
        self._testsets: Dict[str, List[TestCase]] = {}

    @property
    def name(self) -> str:
        return self.submission.name

    # ------------------------------------------------------------ limits & running
    @property
    def limits(self) -> Limits:
        tl = self.rules.time_limit_s
        return Limits(cpu_time_s=tl, wall_time_s=max(3 * tl, tl + 2.0),
                      memory_mb=self.rules.memory_limit_mb,
                      output_bytes=self.rules.output_limit_kb * 1024)

    def run(self, input_text: str, limits: Optional[Limits] = None, exe: Optional[Path] = None,
            env: Optional[Dict[str, str]] = None) -> RunResult:
        target = exe or self.binary
        if target is None:
            raise RuntimeError("the submission has not been compiled")
        limits = limits or self.limits
        result = self.sandbox.run([str(target)], input_text, limits, env_extra=env)
        # Out of wall-clock time while using little CPU: the machine was busy (e.g. a local LLM running),
        # not an infinite loop, which burns CPU. Run again so the verdict does not depend on the load.
        for _ in range(2):
            if result.status != "TLE" or result.cpu_time_s >= 0.5 * limits.cpu_time_s:
                break
            result = self.sandbox.run([str(target)], input_text, limits, env_extra=env)
        return result

    def compare_config(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cfg = dict(self.rules.compare)
        if params and params.get("compare"):
            override = params["compare"]
            cfg.update(override if isinstance(override, dict) else {"mode": override})
        return cfg

    def judge(self, input_text: str, expected: Optional[str], *, limits: Optional[Limits] = None,
              compare: Optional[Dict[str, Any]] = None, exe: Optional[Path] = None) -> Judgement:
        run = self.run(input_text, limits, exe)
        return judge_run(run, expected, compare or self.compare_config(), self.rules.nonzero_exit_is_error)

    # ------------------------------------------------------------ builds
    def build(self, tag: str, flags: str, link_flags: Optional[str] = None,
              coverage: bool = False) -> CompileResult:
        """Compile the submission into ``workdir/tag`` (once per tag, thread-safe).

        Sources are compiled by relative name from inside the build directory,
        so diagnostics read ``add.c:3:5`` instead of a temporary path.
        """
        with self._lock:
            lock = self._build_locks.setdefault(tag, threading.Lock())
        with lock:
            if tag in self._builds:
                return self._builds[tag]
            d = self.workdir / tag
            d.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.submission, d / self.name)
            exe = "prog" + self.toolchain.exe_suffix
            link = self.rules.link_flags if link_flags is None else link_flags
            if coverage:
                obj = self.submission.stem + ".o"
                res = self.sandbox.compile([self.name], obj, flags, cwd=d, compile_only=True)
                if res.ok:
                    res = self.sandbox.compile([obj], exe, "--coverage", link, cwd=d)
            else:
                res = self.sandbox.compile([self.name], exe, flags, link, cwd=d)
            res.output = self.clean(res.output)
            self._builds[tag] = res
            return res

    # ------------------------------------------------------------ tests
    def testset(self, testset_dir: str) -> List[TestCase]:
        """``NN.in`` + ``NN.out`` (or ``.ans``) pairs, in natural order.

        A missing expected file is filled in from the reference solution.
        """
        with self._lock:
            if testset_dir in self._testsets:
                return self._testsets[testset_dir]
        d = self.rules.resolve(testset_dir)
        cases: List[TestCase] = []
        if d.suffix == ".json":  # test_cases.json: [{"name", "input", "expected"}, ...]
            from .testgen import load_cases
            cases = [TestCase(c["name"], c["input"], c["expected"], d) for c in (load_cases(d) or [])]
        elif d.is_dir():
            for inp in sorted(d.glob("*.in"), key=lambda p: natural_key(p.name)):
                expected = None
                for ext in (".out", ".ans", ".expected"):
                    candidate = inp.with_suffix(ext)
                    if candidate.is_file():
                        expected = read_text(candidate)
                        break
                text = inp.read_bytes().decode("utf-8", "replace")
                if expected is None and self.oracle is not None:
                    expected = self.oracle.expected(text)
                cases.append(TestCase(inp.stem, text, expected, inp))
            generated = d / "test_cases.json"
            if not cases and generated.is_file():
                from .testgen import load_cases
                cases = [TestCase(c["name"], c["input"], c["expected"], generated)
                         for c in (load_cases(generated) or [])]
        with self._lock:
            self._testsets[testset_dir] = cases
        return cases

    def sample_input(self) -> str:
        sample = self.rules.sample
        if sample and isinstance(sample.get("input"), str):
            return sample["input"]
        for m in self.rules.metrics:
            if m.analyzer == "io_tests" and m.params.get("testset_dir"):
                tests = self.testset(m.params["testset_dir"])
                if tests:
                    return tests[0].input
        return ""

    # ------------------------------------------------------------ misc
    def clean(self, text: str) -> str:
        """Remove temporary paths from tool output (keeps reports byte-identical)."""
        if not text:
            return text
        text = normalize_newlines(text)
        roots = {str(self.workdir), self.workdir.as_posix(), str(self.sandbox.scratch),
                 self.sandbox.scratch.as_posix()}
        # Longest first: the work directory lives inside the scratch directory.
        for root in sorted(roots, key=len, reverse=True):
            text = text.replace(root + "/", "").replace(root + "\\", "").replace(root, "")
        return text
