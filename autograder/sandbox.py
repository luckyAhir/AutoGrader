"""Compiling and running untrusted student programs under fixed limits.

Design decisions
----------------
* **CPU time decides TLE.** CPU time barely changes with machine load, so the
  verdict is reproducible. A wall-clock deadline (a multiple of the CPU limit)
  is only a safety net for programs that block or sleep forever.
* **POSIX:** a tiny C launcher (``data/sandbox_launcher.c``, compiled once per
  session) applies ``setrlimit`` for CPU, address space, stack, file size and
  core dumps, then ``exec``s the program in a new session, so the whole process
  group can be killed. Python's ``preexec_fn`` is avoided because it is unsafe
  in a multi-threaded process, and analyzers run in parallel threads.
* **Windows:** the program starts suspended, is placed in a Job Object (memory
  cap, CPU-time cap, at most one active process, kill-on-close, no crash
  dialogs) and is then resumed.
* **Output flood protection:** stdout is capped. A program that prints forever
  is killed and judged OLE instead of exhausting the grader's memory.
* Measured times and memory are kept out of verdict *details*, so that reports
  are byte-identical between runs.
"""

from __future__ import annotations

import math
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from .util import normalize_newlines

IS_WINDOWS = os.name == "nt"

SIGNAL_INFO = {
    "SIGSEGV": "segmentation fault: invalid memory access (array index out of bounds, NULL or "
               "dangling pointer, or stack overflow)",
    "SIGFPE": "arithmetic exception: integer division by zero or INT_MIN / -1",
    "SIGABRT": "abort(): a failed assert, or libc detected heap corruption / double free",
    "SIGBUS": "bus error: misaligned or invalid memory access",
    "SIGILL": "illegal instruction (often a trapped undefined behaviour)",
    "SIGXCPU": "CPU time limit exceeded",
    "SIGXFSZ": "file size limit exceeded (the program wrote too much to a file)",
    "SIGKILL": "killed by the sandbox",
    "SIGPIPE": "broken pipe",
    "MEMORY": "out of memory",
    "LOADER": "the program could not be loaded (often a huge static array)",
}

# NTSTATUS crash codes -> (POSIX-like name, description)
_NTSTATUS = {
    0xC0000005: ("SIGSEGV", SIGNAL_INFO["SIGSEGV"]),
    0xC00000FD: ("SIGSEGV", "stack overflow: very deep or infinite recursion, or a huge local array"),
    0xC0000094: ("SIGFPE", "integer division by zero"),
    0xC0000095: ("SIGFPE", "integer overflow trap (e.g. INT_MIN / -1)"),
    0xC000008E: ("SIGFPE", "floating-point division by zero"),
    0xC000001D: ("SIGILL", "illegal instruction (trapped undefined behaviour)"),
    0xC0000409: ("SIGABRT", "fast-fail abort (buffer overrun detected or abort())"),
    0xC0000017: ("MEMORY", SIGNAL_INFO["MEMORY"]),
    0xC000012D: ("MEMORY", "commit limit reached (out of memory)"),
    0xC0000142: ("LOADER", SIGNAL_INFO["LOADER"]),
    0xC0000018: ("LOADER", SIGNAL_INFO["LOADER"]),
}


@dataclass(frozen=True)
class Limits:
    cpu_time_s: float
    wall_time_s: float
    memory_mb: Optional[int]
    output_bytes: int = 8 << 20
    stderr_bytes: int = 256 << 10
    file_size_bytes: int = 16 << 20

    def scaled(self, factor: float, memory_mb: Union[int, None, str] = "keep") -> "Limits":
        mem = self.memory_mb if memory_mb == "keep" else memory_mb
        return replace(self, cpu_time_s=self.cpu_time_s * factor,
                       wall_time_s=self.wall_time_s * factor, memory_mb=mem)


@dataclass
class RunResult:
    status: str                 # OK | TLE | MLE | RE | OLE | ERR
    exit_code: Optional[int]
    signal_name: Optional[str]
    stdout: str
    stderr: str
    cpu_time_s: float
    wall_time_s: float
    peak_memory_kb: Optional[int]
    detail: str = ""
    output_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "OK"


@dataclass
class CompileResult:
    ok: bool
    exe: Optional[Path]
    command: List[str]
    returncode: int
    output: str


def split_flags(flags: Union[str, Sequence[str], None]) -> List[str]:
    if not flags:
        return []
    if isinstance(flags, str):
        return shlex.split(flags)
    return [str(f) for f in flags]


class _Collector(threading.Thread):
    """Drains a pipe, keeping at most ``cap`` bytes; reports overflow."""

    def __init__(self, stream, cap: int, on_overflow: Optional[threading.Event]):
        super().__init__(daemon=True)
        self.stream = stream
        self.cap = cap
        self.on_overflow = on_overflow
        self.chunks: List[bytes] = []
        self.kept = 0
        self.seen = 0

    def run(self) -> None:
        reader = getattr(self.stream, "read1", self.stream.read)
        try:
            while True:
                chunk = reader(65536)
                if not chunk:
                    break
                self.seen += len(chunk)
                if self.kept < self.cap:
                    take = chunk[: self.cap - self.kept]
                    self.chunks.append(take)
                    self.kept += len(take)
                if self.seen > self.cap and self.on_overflow is not None:
                    self.on_overflow.set()
        except (OSError, ValueError):
            pass

    @property
    def truncated(self) -> bool:
        return self.seen > self.cap

    def text(self) -> str:
        return normalize_newlines(b"".join(self.chunks).decode("utf-8", errors="replace"))


def _feed(stream, data: bytes) -> None:
    try:
        if data:
            stream.write(data)
            stream.flush()
    except (OSError, ValueError):
        pass  # the program exited or closed stdin early - not an error
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


class Sandbox:
    def __init__(self, toolchain, scratch: Path):
        self.toolchain = toolchain
        self.scratch = Path(scratch)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._launcher: Union[Path, bool, None] = None
        if IS_WINDOWS:
            _win_quiet_crashes()

    # ------------------------------------------------------------ compile
    def compile(self, sources: Sequence[Union[str, Path]], output: Union[str, Path], flags="",
                link_flags="", cwd: Optional[Path] = None, compile_only: bool = False,
                timeout: float = 120) -> CompileResult:
        cmd = [self.toolchain.cc] + split_flags(flags)
        if compile_only:
            cmd.append("-c")
        cmd += [str(s) for s in sources] + ["-o", str(output)]
        if not compile_only:
            cmd += split_flags(link_flags)
        env = dict(os.environ, LC_ALL="C", LANG="C")
        try:
            proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=timeout, env=env)
            out, rc = proc.stdout.decode("utf-8", "replace"), proc.returncode
        except subprocess.TimeoutExpired:
            out, rc = f"compiler timed out after {timeout:.0f}s", -1
        except OSError as exc:
            out, rc = f"could not run the compiler: {exc}", -1
        target = Path(output) if Path(output).is_absolute() or cwd is None else Path(cwd) / output
        ok = rc == 0 and target.exists()
        return CompileResult(ok, target if ok else None, cmd, rc, normalize_newlines(out))

    # ------------------------------------------------------------ run
    def run(self, argv: Sequence[Union[str, Path]], stdin_data: Union[str, bytes], limits: Limits,
            cwd: Optional[Path] = None, env_extra: Optional[Dict[str, str]] = None) -> RunResult:
        data = stdin_data.encode("utf-8") if isinstance(stdin_data, str) else (stdin_data or b"")
        own_dir = cwd is None
        run_dir = Path(tempfile.mkdtemp(prefix="run_", dir=str(self.scratch))) if own_dir else Path(cwd)
        env = self._run_env(env_extra)
        try:
            if IS_WINDOWS:
                return self._run_windows([str(a) for a in argv], data, limits, run_dir, env)
            return self._run_posix([str(a) for a in argv], data, limits, run_dir, env)
        finally:
            if own_dir:
                _rmtree_quiet(run_dir)

    def _run_env(self, extra: Optional[Dict[str, str]]) -> Dict[str, str]:
        env = {"LANG": "C", "LC_ALL": "C", "PATH": os.environ.get("PATH", os.defpath)}
        if IS_WINDOWS:
            for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
                if key in os.environ:
                    env[key] = os.environ[key]
            # MinGW runtime DLLs live next to the compiler.
            env["PATH"] = str(Path(self.toolchain.cc).parent) + os.pathsep + env["PATH"]
        env.update(extra or {})
        return env

    # ------------------------------------------------------------ POSIX
    def _launcher_path(self) -> Optional[Path]:
        with self._lock:
            if self._launcher is None:
                src = Path(__file__).with_name("data") / "sandbox_launcher.c"
                exe = self.scratch / "sandbox_launcher"
                res = self.compile([src], exe, flags="-O2")
                self._launcher = exe if res.ok else False
            return self._launcher or None

    def _run_posix(self, argv, data, limits, run_dir, env) -> RunResult:
        cpu_cap = int(math.ceil(limits.cpu_time_s)) + 1
        mem = int(limits.memory_mb * 1024 * 1024) if limits.memory_mb else 0
        launcher = self._launcher_path()
        preexec = None
        status_dir = Path(tempfile.mkdtemp(prefix="st_", dir=str(self.scratch)))
        status_file = status_dir / "status"
        if launcher:
            full = [str(launcher), str(cpu_cap), str(mem), str(limits.file_size_bytes), str(status_file),
                    "--"] + argv
        else:  # pragma: no cover - only when the launcher cannot be compiled
            full = argv
            preexec = _posix_preexec(cpu_cap, mem, limits.file_size_bytes)

        start = time.monotonic()
        try:
            proc = subprocess.Popen(full, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=str(run_dir), env=env,
                                    start_new_session=True, preexec_fn=preexec)
        except OSError as exc:
            return _error_result(f"could not start the program: {exc}")

        flood = threading.Event()
        out = _Collector(proc.stdout, limits.output_bytes, flood)
        err = _Collector(proc.stderr, limits.stderr_bytes, None)
        feeder = threading.Thread(target=_feed, args=(proc.stdin, data), daemon=True)
        for t in (out, err, feeder):
            t.start()

        deadline = start + limits.wall_time_s
        timed_out = flooded = False
        pause = 0.0005
        while True:
            pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
            if pid:
                break
            if flood.is_set() or time.monotonic() >= deadline:
                flooded = flood.is_set()
                timed_out = not flooded
                _killpg(proc.pid)
                pid, status, usage = os.wait4(proc.pid, 0)
                break
            time.sleep(pause)
            pause = min(pause * 2, 0.01)
        wall = time.monotonic() - start
        _killpg(proc.pid)  # anything the program forked
        proc.returncode = os.waitstatus_to_exitcode(status)  # reaped here; Popen must not wait again
        out.join(2.0)
        err.join(2.0)
        feeder.join(0.2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

        measured = _read_status(status_file)
        _rmtree_quiet(status_dir)
        if measured is not None:  # the launcher measured the student process itself
            signaled, value, peak_kb, cpu = measured
        else:  # killed by us (timeout / flood) or no launcher: fall back to our own wait4
            signaled = os.WIFSIGNALED(status)
            value = os.WTERMSIG(status) if signaled else os.WEXITSTATUS(status)
            peak_kb, cpu = None, usage.ru_utime + usage.ru_stime
        if signaled:
            code, sig = None, _signame(value)
            sig_desc = SIGNAL_INFO.get(sig, sig)
        else:
            code, sig, sig_desc = value, None, None
            if code in (125, 126) and "sandbox_launcher:" in err.text():
                return _error_result(err.text().strip())
        return _classify(code, sig, sig_desc, timed_out, flooded or out.truncated, cpu, wall,
                         peak_kb, limits, out, err)

    # ------------------------------------------------------------ Windows
    def _run_windows(self, argv, data, limits, run_dir, env) -> RunResult:  # pragma: no cover - Windows only
        create_suspended, detached = 0x00000004, 0x00000008
        start = time.monotonic()
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=str(run_dir), env=env,
                                    creationflags=create_suspended | detached)
        except OSError as exc:
            return _error_result(f"could not start the program: {exc}")
        job = None
        try:
            job = _WinJob(limits)
            job.assign(proc._handle)  # noqa: SLF001 - documented attribute on Windows
        except OSError as exc:
            proc.kill()
            proc.wait()
            if job:
                job.close()
            return _error_result(f"could not apply resource limits: {exc}")

        flood = threading.Event()
        out = _Collector(proc.stdout, limits.output_bytes, flood)
        err = _Collector(proc.stderr, limits.stderr_bytes, None)
        feeder = threading.Thread(target=_feed, args=(proc.stdin, data), daemon=True)
        for t in (out, err, feeder):
            t.start()
        _resume_process(proc._handle)  # noqa: SLF001

        deadline = start + limits.wall_time_s
        timed_out = flooded = False
        pause = 0.0005
        while proc.poll() is None:
            if flood.is_set() or time.monotonic() >= deadline:
                flooded = flood.is_set()
                timed_out = not flooded
                job.terminate()
                proc.wait()
                break
            time.sleep(pause)
            pause = min(pause * 2, 0.01)
        wall = time.monotonic() - start
        cpu = job.cpu_seconds()
        peak_kb = job.peak_memory_kb()
        job.close()
        out.join(2.0)
        err.join(2.0)
        feeder.join(0.2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

        code = proc.returncode & 0xFFFFFFFF
        sig = sig_desc = None
        if code in _NTSTATUS:
            sig, sig_desc = _NTSTATUS[code]
            code = None
        elif code >= 0x80000000:
            sig, sig_desc = "CRASH", f"terminated with status 0x{code:08X}"
            code = None
        if sig in ("MEMORY", "LOADER") and limits.memory_mb:
            return RunResult("MLE", None, sig, out.text(), err.text(), cpu, wall, peak_kb,
                             f"memory limit ({limits.memory_mb} MB) exceeded: {sig_desc}")
        return _classify(code, sig, sig_desc, timed_out, flooded or out.truncated, cpu, wall,
                         peak_kb, limits, out, err)


# ---------------------------------------------------------------- helpers
def _classify(code, sig, sig_desc, timed_out, flooded, cpu, wall, peak_kb, limits, out, err) -> RunResult:
    def result(status: str, detail: str) -> RunResult:
        return RunResult(status, code, sig, out.text(), err.text(), cpu, wall, peak_kb, detail,
                         out.truncated)

    if flooded:
        return result("OLE", f"output limit exceeded (more than {limits.output_bytes // 1024} KB printed)")
    if timed_out:
        return result("TLE", f"did not finish within the wall-clock limit ({limits.wall_time_s:g} s)")
    if sig == "SIGXCPU" or cpu > limits.cpu_time_s or (sig == "SIGKILL" and cpu >= 0.9 * limits.cpu_time_s):
        return result("TLE", f"CPU time limit exceeded ({limits.cpu_time_s:g} s)")
    if limits.memory_mb and peak_kb and peak_kb >= 0.95 * limits.memory_mb * 1024 and (sig or code):
        return result("MLE", f"memory limit exceeded ({limits.memory_mb} MB)")
    if sig:
        return result("RE", f"crashed with {sig}: {sig_desc}")
    if code:
        return result("RE", f"exited with non-zero status {code}")
    return result("OK", "")


def _read_status(path: Path):
    """Parse the launcher's status line: signaled, value, max RSS (KB), user s, system s."""
    try:
        parts = path.read_text().split()
        signaled, value, rss = int(parts[0]), int(parts[1]), int(parts[2])
        cpu = float(parts[3]) + float(parts[4])
    except (OSError, ValueError, IndexError):
        return None
    if sys.platform == "darwin":
        rss //= 1024  # bytes on macOS
    return bool(signaled), value, rss, cpu


def _error_result(message: str) -> RunResult:
    return RunResult("ERR", None, None, "", message, 0.0, 0.0, None, message)


def _signame(num: int) -> str:
    try:
        return signal.Signals(num).name
    except ValueError:
        return f"SIG{num}"


def _killpg(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _posix_preexec(cpu: int, mem: int, fsize: int):  # pragma: no cover - fallback path
    import resource

    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        if mem:
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def _rmtree_quiet(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------- Windows Job Objects
if IS_WINDOWS:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _BASIC_ACCOUNTING(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.c_void_p]
    _k32.QueryInformationJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.SetErrorMode.argtypes = [wintypes.UINT]
    _k32.SetErrorMode.restype = wintypes.UINT
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long

    _LIMIT_PROCESS_TIME = 0x00000002
    _LIMIT_ACTIVE_PROCESS = 0x00000008
    _LIMIT_PROCESS_MEMORY = 0x00000100
    _LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
    _LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _WinJob:
        def __init__(self, limits: Limits):
            self.handle = _k32.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            info = _EXTENDED_LIMIT()
            flags = (_LIMIT_KILL_ON_JOB_CLOSE | _LIMIT_DIE_ON_UNHANDLED_EXCEPTION
                     | _LIMIT_ACTIVE_PROCESS | _LIMIT_PROCESS_TIME)
            info.BasicLimitInformation.ActiveProcessLimit = 1
            info.BasicLimitInformation.PerProcessUserTimeLimit = int(
                (math.ceil(limits.cpu_time_s) + 1) * 10_000_000)
            if limits.memory_mb:
                flags |= _LIMIT_PROCESS_MEMORY
                info.ProcessMemoryLimit = int(limits.memory_mb) * 1024 * 1024
            info.BasicLimitInformation.LimitFlags = flags
            if not _k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                err = ctypes.get_last_error()
                self.close()
                raise ctypes.WinError(err)

        def assign(self, process_handle) -> None:
            if not _k32.AssignProcessToJobObject(self.handle, int(process_handle)):
                raise ctypes.WinError(ctypes.get_last_error())

        def terminate(self) -> None:
            _k32.TerminateJobObject(self.handle, 1)

        def cpu_seconds(self) -> float:
            acct = _BASIC_ACCOUNTING()
            if not _k32.QueryInformationJobObject(self.handle, 1, ctypes.byref(acct), ctypes.sizeof(acct), None):
                return 0.0
            return (acct.TotalUserTime + acct.TotalKernelTime) / 1e7

        def peak_memory_kb(self) -> Optional[int]:
            info = _EXTENDED_LIMIT()
            if not _k32.QueryInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info), None):
                return None
            return int(info.PeakProcessMemoryUsed) // 1024

        def close(self) -> None:
            if self.handle:
                _k32.CloseHandle(self.handle)
                self.handle = None

    def _resume_process(handle) -> None:
        _ntdll.NtResumeProcess(int(handle))

    def _win_quiet_crashes() -> None:
        # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX:
        # inherited by children, so a crashing submission never opens a dialog
        # that would block an unattended run.
        _k32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
else:
    def _win_quiet_crashes() -> None:
        return None
