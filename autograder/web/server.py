"""HTTP server for the browser interface.

Standard library only. It serves the single-page app in ``static/`` and a small JSON API.
Grading takes two inputs from the browser, at runtime: the rules (rules.json text, whose
"problem" field holds the problem statement) and the C program.

    POST /api/rules/check   {rules}                               validate the rules, list its analyzers
    POST /api/grade         {rules, filename, source}             start grading
    GET  /api/jobs/<id>                                           progress events, then the report
    GET  /api/status                                              toolchain and LLM availability
    GET  /api/reports                                             stored reports, grouped by folder
    GET  /api/report?file=<group>/<name>.json                     one stored report + text + source

Nothing about a problem is stored in advance. Every grading job wipes ``problems/``
and rebuilds ``problems/<problem>/`` from the two inputs:

    rules.json      the rules as given (the problem statement is its "problem" field)
    <student>.c     the submission
    test_cases.json  [{name, input, expected}]: test cases the LLM designs from the statement + samples

Reports are kept under ``reports/uploads/<problem>/`` with a copy of the graded program.
Every path the rules mention must stay inside the project folder: the server compiles
and runs what they name.

The server binds to 127.0.0.1 by default and refuses cross-site requests: POST bodies
must be JSON, and the Origin and Host headers must name this server.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from ..agent import GradingAgent
from ..analyzers import available_analyzers, load_plugin
from ..analyzers.similarity_flag import corpus_name
from ..inputgen import InputGenerator, InputSpecError
from ..llm import LLMClient
from ..report import render_text, write_outputs
from ..rules import Rules, RulesError, load_rules, rules_from_text
from ..testgen import load_cases
from ..util import natural_key, read_text

log = logging.getLogger("autograder.web")

STATIC = Path(__file__).with_name("static")
MAX_BODY = 1024 * 1024
MAX_SOURCE = 256 * 1024
MAX_RULES = 128 * 1024
MAX_STATEMENT = 64 * 1024
TEST_ANALYZERS = ("io_tests", "llm_tests")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _is_prose(text: str) -> bool:
    return bool(re.search(r"\s", text.strip()))


def _problem_folder(problem: str) -> str:
    """Folder name for a problem: its name, or for a full statement the title on its first line."""
    text = problem.strip()
    if _is_prose(text):
        text = re.sub(r"^problem\s*:\s*", "", text.splitlines()[0].strip(), flags=re.I)
    return _SAFE.sub("_", re.sub(r"\s+", "_", text)).strip("._")[:64] or "problem"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def safe_filename(name: str) -> str:
    name = _SAFE.sub("_", Path(str(name or "")).name)[:64].strip("._") or "submission"
    return name if name.endswith(".c") else name.rsplit(".", 1)[0] + ".c"


class Job:
    def __init__(self, problem: str, filename: str):
        self.id = uuid.uuid4().hex[:12]
        self.problem = problem
        self.filename = filename
        self.state = "queued"
        self.events: List[Dict[str, Any]] = []
        self.error: Optional[str] = None
        self.report: Optional[Dict[str, Any]] = None
        self.text: Optional[str] = None
        self.source: Optional[str] = None
        self.report_file: Optional[str] = None
        self.created = time.time()
        self._lock = threading.Lock()

    def emit(self, event: Dict[str, Any]) -> None:
        with self._lock:
            self.events.append(event)

    def view(self) -> Dict[str, Any]:
        with self._lock:
            out = {"id": self.id, "problem": self.problem, "filename": self.filename, "state": self.state,
                   "events": list(self.events), "error": self.error}
        if self.state == "done":
            out.update(report=self.report, text=self.text, source=self.source, report_file=self.report_file)
        return out


class WebApp:
    def __init__(self, root: Path, toolchain, cache_dir: Path, jobs: Optional[int] = None, warm_models: bool = True):
        self.root = Path(root).resolve()
        self.warm_models = warm_models
        self.problems_dir = self.root / "problems"
        self.reports_dir = self.root / "reports"
        self.submissions_dir = self.root / "submissions"  # graded programs, the originality corpus
        self.toolchain = toolchain
        self.cache_dir = Path(cache_dir)
        self.jobs_per_grade = jobs
        self._jobs: Dict[str, Job] = {}
        self._warmed: Dict[str, float] = {}
        self._lock = threading.Lock()
        # One job at a time: each job owns problems/ while it runs.
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="grader")
        self._status: Dict[str, Any] = {"ready": False, "compiler": f"{toolchain.cc_family} {toolchain.cc_version}"}
        threading.Thread(target=self._probe_status, daemon=True).start()

    def close(self) -> None:
        self._worker.shutdown(wait=False)

    # ------------------------------------------------------------ paths
    def _rel(self, path: Path) -> str:
        rel = Path(path).resolve().relative_to(self.root).as_posix()
        return rel or "."

    # ------------------------------------------------------------ status
    def _probe_status(self) -> None:
        tc = self.toolchain
        with tempfile.TemporaryDirectory() as tmp:
            asan = tc.supports("-fsanitize=address,undefined", Path(tmp))
            trap = False if asan else tc.supports("-fsanitize=undefined -fsanitize-undefined-trap-on-error",
                                                  Path(tmp))
        host = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
        if not host.startswith("http"):
            host = "http://" + host
        models: Optional[List[str]] = None
        try:
            with urllib.request.urlopen(host + "/api/tags", timeout=2) as resp:
                models = [m.get("name") for m in json.loads(resp.read().decode("utf-8")).get("models", [])]
        except (OSError, ValueError):
            models = None
        self._status = {
            "ready": True, "compiler": f"{tc.cc_family} {tc.cc_version}", "platform": tc.platform,
            "gcov": bool(tc.gcov), "cppcheck": tc.cppcheck_version, "valgrind": tc.valgrind_version,
            "sanitizers": "asan" if asan else ("ubsan-trap" if trap else None),
            "llm": {"reachable": models is not None, "models": models or [],
                    "configured": os.environ.get("OLLAMA_MODEL", "").strip() or None},
        }

    def status(self) -> Dict[str, Any]:
        return dict(self._status)

    # ------------------------------------------------------------ the three inputs
    def _inputs(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        """(rules for the workspace, problem folder name) from the browser's rules text."""
        text = payload.get("rules")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "the rules are empty: paste a rules.json or open one")
        if len(text.encode("utf-8")) > MAX_RULES:
            raise ApiError(413, f"the rules are larger than {MAX_RULES // 1024} KB")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RulesError(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
        if not isinstance(raw, dict):
            raise RulesError("the top level of the rules must be an object")
        statement = str(raw.get("problem") or "") if _is_prose(str(raw.get("problem") or "")) \
            else str(raw.get("statement") or "")
        if not statement.strip():
            raise RulesError('the problem statement is missing: put it in the "problem" field of rules.json')
        if len(statement.encode("utf-8")) > MAX_STATEMENT:
            raise ApiError(413, f"the statement is larger than {MAX_STATEMENT // 1024} KB")
        raw = dict(raw)
        # No reference solution and no files next to the rules: neither is read from disk.
        for key in ("statement_file", "reference_solution"):
            raw.pop(key, None)
        return raw, _problem_folder(str(raw.get("problem") or ""))

    def _parse(self, payload: Dict[str, Any]) -> Tuple[Rules, Dict[str, Any], str]:
        raw, pid = self._inputs(payload)
        folder = self.problems_dir / pid
        rules = rules_from_text(json.dumps(raw), folder / "rules.json", folder)
        self._confine(rules)
        for plugin in rules.plugins:
            load_plugin(plugin)
        known = available_analyzers()
        unknown = sorted({m.analyzer for m in rules.metrics if m.analyzer not in known})
        if unknown:
            raise RulesError(f"unknown analyzer(s) {', '.join(unknown)}; available: {', '.join(known)}")
        return rules, raw, pid

    def _confine(self, rules: Rules) -> None:
        """Every file or folder the rules name must be inside the project folder."""
        named = [rules.raw.get(k) for k in ("reference_solution", "generator", "usefulness_limitations_file")]
        named += list(rules.raw.get("plugins", []))
        for m in rules.metrics:
            named += [v for k, v in m.params.items() if isinstance(v, str) and (k.endswith("_dir") or k.endswith("_file"))]
        for value in filter(None, named):
            if not rules.resolve(value).resolve().is_relative_to(self.root):
                raise RulesError(f"{value!r} points outside the project folder; keep every path the rules "
                                 f"name inside {self.root.name}/")

    def _summary(self, rules: Rules) -> Dict[str, Any]:
        classes = available_analyzers()
        warnings = list(rules.warnings)
        if any(m.analyzer in TEST_ANALYZERS for m in rules.metrics) and not any(
                isinstance(s.get("output"), str) for s in rules.samples):
            warnings.append('no "sample" with an "output": the tests are written from the samples, so correctness '
                            'cannot be graded')
        warnings += _input_spec_warnings(rules)
        return {
            "problem": rules.problem, "statement": rules.statement[:6000], "rules_hash": rules.rules_hash,
            "time_limit_s": rules.time_limit_s, "memory_limit_mb": rules.memory_limit_mb,
            "warnings": warnings, "samples": len(rules.samples),
            "folder": f"problems/{rules.base_dir.name}",
            "metrics": [{
                "name": m.name, "analyzer": m.analyzer, "weight": m.weight, "params": m.params,
                "description": getattr(classes.get(m.analyzer), "description", "plugin analyzer"),
                "kind": "execution" if getattr(classes.get(m.analyzer), "needs_binary", False) else "source",
            } for m in rules.metrics],
        }

    def check_rules(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            rules = self._parse(payload)[0]
        except RulesError as exc:
            return {"ok": False, "errors": _error_lines(str(exc))}
        self._warm_models(rules)
        return {"ok": True, **self._summary(rules)}

    def _warm_models(self, rules: Rules) -> None:
        """Load the rules' local models into memory in the background, while the user still pastes the
        program: a cold load can take a minute on a small machine."""
        todo = {}
        for m in rules.metrics if self.warm_models else []:
            if m.analyzer in ("io_tests", "llm_tests", "llm_review") and m.params.get("provider", "ollama") == "ollama" \
                    and m.params.get("mode") != "heuristic":
                client = LLMClient.from_params(m.params, cache_dir=None)
                todo[(client.host, client.model)] = client
        now = time.time()
        with self._lock:
            fresh = {k: c for k, c in todo.items() if now - self._warmed.get(f"{k[0]}|{k[1]}", 0) > 600}
            for host, model in fresh:
                self._warmed[f"{host}|{model}"] = now
        for client in fresh.values():
            threading.Thread(target=client.warm, daemon=True).start()

    # ------------------------------------------------------------ grading
    def submit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            _, raw, pid = self._parse(payload)
        except RulesError as exc:
            raise ApiError(422, str(exc)) from exc
        source = payload.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ApiError(400, "the program is empty: paste C code or open a .c file")
        if len(source.encode("utf-8")) > MAX_SOURCE:
            raise ApiError(413, f"the program is larger than {MAX_SOURCE // 1024} KB")
        name = safe_filename(payload.get("filename", "submission.c"))
        job = Job(pid, name)
        with self._lock:
            self._jobs[job.id] = job
            for old in sorted(self._jobs.values(), key=lambda j: j.created)[:-50]:
                self._jobs.pop(old.id, None)
        self._worker.submit(self._run, job, raw, source)
        return {"id": job.id}

    def _workspace_for(self, pid: str, raw: Dict[str, Any], filename: str, source: str) -> Path:
        """Wipe problems/ and write this job's inputs into problems/<pid>/."""
        if self.problems_dir.exists():
            shutil.rmtree(self.problems_dir)
        folder = self.problems_dir / pid
        folder.mkdir(parents=True)
        (folder / "rules.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
                                           encoding="utf-8")
        path = folder / filename
        path.write_bytes(source.encode("utf-8"))
        return path

    def _run(self, job: Job, raw: Dict[str, Any], source: str) -> None:
        job.state = "running"
        try:
            job.emit({"type": "stage", "stage": "workspace", "folder": f"problems/{job.problem}"})
            path = self._workspace_for(job.problem, raw, job.filename, source)
            rules = load_rules(path.parent / "rules.json")
            job.emit({"type": "stage", "stage": "prepare"})
            agent = GradingAgent(rules, self.toolchain, jobs=self.jobs_per_grade, cache_dir=self.cache_dir,
                                 progress=job.emit, submissions_dir=self.submissions_dir)
            try:
                job.emit({"type": "prepared", **self._prepared(rules, agent)})
                result = agent.grade(path, progress=job.emit)
            finally:
                agent.close()
            paths = write_outputs(result, self.reports_dir / "uploads" / job.problem)
            # problems/ is wiped by the next job: keep the graded program beside its report.
            paths["json"].with_suffix(".c").write_bytes(source.encode("utf-8"))
            # Kept after grading, so a program is compared only with earlier ones. The file name identifies
            # the student: grading the same name again replaces the stored copy.
            kept = self.submissions_dir / corpus_name(rules.problem)
            kept.mkdir(parents=True, exist_ok=True)
            (kept / job.filename).write_bytes(source.encode("utf-8"))
            job.report = result.report
            job.text = render_text(result.report)
            job.source = source
            job.report_file = paths["json"].relative_to(self.reports_dir).as_posix()
            job.state = "done"
        except (ApiError, RulesError) as exc:
            job.error, job.state = str(exc), "error"
        except Exception as exc:  # report, keep serving
            log.exception("grading job %s failed", job.id)
            job.error, job.state = f"{type(exc).__name__}: {exc}", "error"

    def _prepared(self, rules: Rules, agent: GradingAgent) -> Dict[str, Any]:
        folder = rules.base_dir
        tests = sum(1 for _ in folder.rglob("*.in")) + sum(
            len(load_cases(f) or []) for f in folder.rglob("test_cases.json"))
        return {"folder": self._rel(folder), "tests": tests,
                "errors": [f"{name}: {info['error']}" for name, info in agent.preparation.items()
                           if isinstance(info, dict) and info.get("error")]}

    def job(self, job_id: str) -> Dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            raise ApiError(404, "unknown job (the server may have restarted)")
        return job.view()

    # ------------------------------------------------------------ stored reports
    def reports(self) -> Dict[str, Any]:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        if self.reports_dir.is_dir():
            for path in sorted(self.reports_dir.rglob("*.json")):
                try:
                    r = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(r, dict) or "grade" not in r or "metrics" not in r:
                    continue
                group = path.parent.relative_to(self.reports_dir).as_posix()
                groups.setdefault(group, []).append({
                    "file": path.relative_to(self.reports_dir).as_posix(),
                    "submission": r.get("submission"), "problem": r.get("problem"), "grade": r.get("grade"),
                    "flags": [{"type": f.get("type"), "severity": f.get("severity")} for f in r.get("flags", [])],
                    "metrics": [{k: m.get(k) for k in ("name", "weight", "effective_weight", "score",
                                                        "contribution", "status", "summary")}
                                for m in r.get("metrics", [])],
                })
        ordered = sorted(groups.items(), key=lambda kv: (not kv[0].startswith("uploads"), natural_key(kv[0])))
        return {"groups": [{"name": name, "reports": sorted(items, key=lambda x: (-x["grade"], x["submission"]))}
                           for name, items in ordered]}

    def report(self, file: str) -> Dict[str, Any]:
        path = (self.reports_dir / unquote(file)).resolve()
        if not path.is_relative_to(self.reports_dir.resolve()) or path.suffix != ".json" or not path.is_file():
            raise ApiError(404, f"no report {file!r}")
        report = json.loads(path.read_text(encoding="utf-8"))
        txt = path.with_suffix(".txt")
        source = None
        kept = path.with_suffix(".c")  # copy saved by the browser interface
        candidate = (self.root / str(report.get("submission_path", ""))).resolve()
        if kept.is_file():
            source = read_text(kept)
        elif candidate.is_relative_to(self.root) and candidate.is_file():
            source = read_text(candidate)
        return {"report": report, "text": read_text(txt) if txt.is_file() else render_text(report),
                "source": source, "report_file": path.relative_to(self.reports_dir).as_posix()}


def _input_spec_warnings(rules: Rules) -> List[str]:
    """Mistakes in input_spec that would silently leave metrics ungraded."""
    out: List[str] = []
    if rules.input_spec:
        try:
            gen = InputGenerator(rules.input_spec, rules.generation_seed)
        except InputSpecError as exc:
            return [f"input_spec is ignored: {exc}. Tests are still generated from the statement and samples, but "
                    f"the complexity check needs a valid input_spec"]
        for i, s in enumerate(rules.samples, 1):
            if gen.parse(s["input"]) is None:
                layout = " / ".join(" ".join(line) for line in gen.layout)
                out.append(f"input_spec (lines: {layout}) does not match sample {i}'s input; it is ignored when "
                           f"generating tests, and the complexity check built from it will be wrong. Declare every "
                           f"value of the input, e.g. an array as {{\"type\": \"int[]\", \"length\": \"n\"}}")
                break
    return out


def _error_lines(message: str) -> List[str]:
    lines = [l.strip()[2:] if l.strip().startswith("- ") else l.strip() for l in message.splitlines()]
    lines = [l for l in lines if l and not l.startswith("invalid rules file")]
    return lines or [message]


# ---------------------------------------------------------------- HTTP layer
class Handler(BaseHTTPRequestHandler):
    app: WebApp = None  # set by make_server
    server_version = "AutoGrader"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        try:
            self._check_host()
            url = urlparse(self.path)
            parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
            if method == "GET" and (not parts or parts == ["index.html"]):
                return self._send_file(STATIC / "index.html")
            if method == "GET" and parts[0] == "static" and len(parts) == 2:
                return self._send_file(STATIC / parts[1])
            if parts[:1] != ["api"]:
                raise ApiError(404, "not found")
            body = self._read_json() if method == "POST" else None
            self._send_json(self._api(method, parts[1:], parse_qs(url.query), body))
        except ApiError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except RulesError as exc:
            self._send_json({"error": str(exc)}, 422)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("request failed")
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _api(self, method: str, parts: List[str], query, body):
        app = self.app
        q = lambda key: (query.get(key) or [""])[0]  # noqa: E731
        route = (method, "/".join(parts))
        if route == ("GET", "status"):
            return app.status()
        if route == ("POST", "rules/check"):
            return app.check_rules(body or {})
        if route == ("POST", "grade"):
            return app.submit(body or {})
        if method == "GET" and len(parts) == 2 and parts[0] == "jobs":
            return app.job(parts[1])
        if route == ("GET", "reports"):
            return app.reports()
        if route == ("GET", "report"):
            return app.report(q("file"))
        raise ApiError(404, "unknown API route")

    def _check_host(self) -> None:
        """Refuse DNS-rebinding and cross-site requests: this server executes code."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        bound = self.server.server_address[0]
        allowed = {"localhost", "127.0.0.1", "::1", bound}
        if bound not in ("0.0.0.0", "::") and host not in allowed:  # all-interfaces binding is opt-in
            raise ApiError(403, "unexpected Host header")
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            raise ApiError(403, "cross-site request refused")

    def _read_json(self) -> Dict[str, Any]:
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ApiError(415, "send JSON (Content-Type: application/json)")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "request too large")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(400, f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "expected a JSON object")
        return data

    def _send_json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path) -> None:
        path = path.resolve()
        if not path.is_relative_to(STATIC.resolve()) or not path.is_file():
            raise ApiError(404, "not found")
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


def make_server(app: WebApp, host: str, port: int) -> ThreadingHTTPServer:
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
