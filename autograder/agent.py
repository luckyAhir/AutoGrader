"""The coordinator ("main agent").

Pipeline for one submission
---------------------------
1. **First pass** (cheap sanity sweep): compile with the rule's flags, check
   that main exists and that input is read and output written, then do a
   smoke run on the sample input under the time/memory limits. A timeout (or
   endless output) sets ``suspected_infinite_loop``. The execution-based
   analyzers are then skipped and score 0, while the source-only analyzers
   still run, giving a partial report.
2. **Dispatch**: one expert analyzer per metric, in parallel threads. Each gets
   the shared context and its own ``params`` slice.
3. **Collate**: weighted grade, counterexamples (deduplicated across
   analyzers, with the full input/expected files saved), suggestions ranked by
   how many points they can recover, flags, the usefulness/limitations note,
   and a reproducibility block (hashes and tool versions).

Contract: the same submission + the same rules (+ the same toolchain) give the
same grade. Every input to the grade is hashed into ``grade_fingerprint``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__
from .analyzers import available_analyzers, get_analyzer, load_plugin
from .analyzers.base import ERROR, SKIPPED_ENVIRONMENT, SKIPPED_SUBMISSION, AnalyzerResult
from .compare import outputs_equal
from .context import AnalysisContext
from .csource import READER_CALLS, WRITER_CALLS, parse
from .inputgen import InputGenerator, InputSpecError
from .judge import Oracle
from .rules import Rules, RulesError
from .sandbox import Sandbox
from .toolchain import Toolchain, detect
from .util import (canonical_json, clamp01, display_path, normalize_newlines, read_text, round_half_up,
                   sha256_bytes, sha256_text, shorten)

log = logging.getLogger("autograder")

NOT_COUNTED = (SKIPPED_ENVIRONMENT, ERROR)


@dataclass
class GradeResult:
    report: Dict[str, Any]
    artifacts: Dict[str, str] = field(default_factory=dict)  # relative path -> content

    @property
    def grade(self) -> float:
        return self.report["grade"]


def load_usefulness(rules: Rules) -> Dict[str, Any]:
    if rules.get("usefulness_limitations_file"):
        path = rules.resolve(rules.get("usefulness_limitations_file"))
    else:
        path = Path(__file__).with_name("data") / "usefulness_limitations.json"
    return json.loads(read_text(path))


class GradingAgent:
    def __init__(self, rules: Rules, toolchain: Optional[Toolchain] = None, *, jobs: Optional[int] = None,
                 cache_dir: Optional[Path] = None, keep_workdir: bool = False,
                 progress: Optional[Callable[[Dict[str, Any]], None]] = None,
                 submissions_dir: Optional[Path] = None):
        self.rules = rules
        self.submissions_dir = submissions_dir  # where graded programs are kept; the originality corpus
        self.progress = progress  # receives events while the one-time setup (test generation) runs
        self.toolchain = toolchain or detect()
        for plugin in rules.plugins:
            load_plugin(plugin)
        known = available_analyzers()
        unknown = sorted({m.analyzer for m in rules.metrics if m.analyzer not in known})
        if unknown:
            raise RulesError(f"unknown analyzer(s) {', '.join(unknown)}; available: {', '.join(known)}")
        self.jobs = jobs or min(4, os.cpu_count() or 2)
        self.cache_dir = Path(cache_dir) if cache_dir else Path(".autograder_cache")
        self.keep_workdir = keep_workdir
        self.scratch = Path(tempfile.mkdtemp(prefix="autograder_"))
        self.sandbox = Sandbox(self.toolchain, self.scratch)
        self.oracle = Oracle(self.sandbox, rules, self.scratch / "oracle") if rules.reference_path else None
        self.inputgen: Optional[InputGenerator] = None
        self.input_spec_error: Optional[str] = None
        self.size_inputgen: Optional[InputGenerator] = None
        if rules.input_spec or rules.generator_path:
            try:
                self.inputgen = InputGenerator(rules.input_spec, rules.generation_seed, rules.generator_path)
            except InputSpecError as exc:
                # A spec the generator cannot use must not stop grading: the metrics that need generated
                # inputs (edge cases, complexity) are skipped and flagged; the others run as usual.
                self.input_spec_error = str(exc)
                log.warning("input_spec ignored: %s", exc)
        # One-time analyzer setup (e.g. llm_tests writes the reference and tests) before assets are hashed.
        self.preparation: Dict[str, Any] = {}
        for m in rules.metrics:
            try:
                prepared = get_analyzer(m.analyzer).prepare(self, dict(m.params))
            except Exception as exc:  # setup failure is reported by the metric, not fatal
                log.exception("preparing %s failed", m.analyzer)
                prepared = {"error": f"setup failed: {type(exc).__name__}: {exc}"}
            if prepared is not None:
                self.preparation[m.name] = prepared
        self.assets_hash = rules.assets_hash()
        self.usefulness = load_usefulness(rules)

    # ------------------------------------------------------------ lifecycle
    def close(self) -> None:
        if not self.keep_workdir:
            shutil.rmtree(self.scratch, ignore_errors=True)

    def __enter__(self) -> "GradingAgent":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ main entry
    def grade(self, submission: Path, progress: Optional[Callable[[Dict[str, Any]], None]] = None) -> GradeResult:
        """Grade one submission. ``progress`` (optional) receives event dicts as the work advances."""
        def emit(event: Dict[str, Any]) -> None:
            if progress is not None:
                try:
                    progress(event)
                except Exception:  # a UI callback must never break grading
                    log.exception("progress callback failed")

        submission = Path(submission)
        raw = submission.read_bytes()
        text = normalize_newlines(raw.decode("utf-8", errors="replace"))
        workdir = Path(tempfile.mkdtemp(prefix="sub_", dir=str(self.scratch)))
        ctx = AnalysisContext(submission=submission, source_text=text, csource=parse(text), rules=self.rules,
                              toolchain=self.toolchain, sandbox=self.sandbox, workdir=workdir,
                              oracle=self.oracle, inputgen=self.inputgen, cache_dir=self.cache_dir,
                              size_inputgen=self.size_inputgen, submissions_dir=self.submissions_dir)
        try:
            log.info("[%s] first pass", ctx.name)
            emit({"type": "stage", "stage": "first_pass"})
            first_pass, flags = self._first_pass(ctx)
            if self.input_spec_error:
                flags.append({"type": "input_spec_ignored", "severity": "medium",
                              "message": f"the input_spec could not be used ({self.input_spec_error}); the complexity "
                                         f"check was not run"})
            ctx.first_pass = first_pass
            emit({"type": "first_pass", "compiled": first_pass["compiled"],
                  "smoke": first_pass.get("smoke_verdict"),
                  "suspected_infinite_loop": first_pass["suspected_infinite_loop"]})
            log.info("[%s] dispatching %d analyzers", ctx.name, len(self.rules.metrics))
            results = self._dispatch(ctx, first_pass, emit)
            emit({"type": "stage", "stage": "collate"})
            return self._collate(ctx, raw, first_pass, flags, results)
        finally:
            if not self.keep_workdir:
                shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------ 1. first pass
    def _first_pass(self, ctx: AnalysisContext) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        rules = self.rules
        flags: List[Dict[str, Any]] = []
        res = ctx.build("main", rules.compile_flags)
        fp: Dict[str, Any] = {
            "compiled": res.ok,
            "compile_command": f"cc {rules.compile_flags} {ctx.name} -o prog {rules.link_flags}".strip(),
            "compiler_warnings": res.output.count("warning:"),
        }
        if not res.ok:
            errors = [line for line in res.output.splitlines() if "error" in line][:8] or [res.output.strip()[:300]]
            fp["compile_errors"] = errors
            flags.append({"type": "compile_error", "severity": "high", "message": errors[0]})
        ctx.binary = res.exe if res.ok else None

        src = ctx.csource
        calls = src.called_names()
        main = src.function("main")
        sanity = {
            "has_main": main is not None,
            "reads_input": bool(calls & READER_CALLS),
            "writes_output": bool(calls & WRITER_CALLS),
            "main_has_return": main is not None and any(t.text == "return" for t in src.body_tokens(main)),
        }
        fp["static_sanity"] = sanity
        if main is not None and not sanity["reads_input"]:
            flags.append({"type": "no_input_read", "severity": "medium",
                          "message": "the program never reads input - its output may be hard-coded"})
        if main is not None and not sanity["writes_output"]:
            flags.append({"type": "no_output", "severity": "medium", "message": "the program never prints anything"})

        fp.update({"smoke_ok": False, "suspected_infinite_loop": False})
        if res.ok:
            sample = ctx.sample_input()
            first = ctx.run(sample)
            fp["smoke_verdict"] = first.status
            if first.status in ("TLE", "OLE"):
                fp["suspected_infinite_loop"] = True
                flags.append({"type": "suspected_infinite_loop", "severity": "high",
                              "message": f"smoke run on the sample input: {first.detail}; execution-based "
                                         f"analyzers were skipped (partial report)"})
            elif first.status == "OK":
                fp["smoke_ok"] = True
                second = ctx.run(sample)
                if second.ok and second.stdout != first.stdout:
                    fp["nondeterministic_output"] = True
                    flags.append({"type": "nondeterministic_output", "severity": "medium",
                                  "message": "two runs on the same input printed different output "
                                             "(uninitialised variable or unseeded randomness?) - grades may vary"})
                expected = (rules.sample or {}).get("output")
                if expected is not None:
                    fp["sample_output_matches"] = outputs_equal(expected, first.stdout, rules.compare["mode"])
            else:
                fp["smoke_detail"] = first.detail
                flags.append({"type": "smoke_run_failed", "severity": "medium",
                              "message": f"on the sample input the program {first.detail}"})
        fp["execution_ok"] = bool(res.ok and not fp["suspected_infinite_loop"])
        return fp, flags

    # ------------------------------------------------------------ 2. dispatch
    def _dispatch(self, ctx: AnalysisContext, fp: Dict[str, Any], emit: Callable[[Dict[str, Any]], None]):
        metrics = self.rules.metrics
        results: List[Optional[AnalyzerResult]] = [None] * len(metrics)

        def done(i: int) -> None:
            r = results[i]
            emit({"type": "metric", "name": metrics[i].name, "status": r.status,
                  "score": round(clamp01(r.score), 4), "summary": ctx.clean(r.summary)})

        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            futures = {}
            for i, m in enumerate(metrics):
                analyzer = get_analyzer(m.analyzer)
                if analyzer.needs_binary and not fp["execution_ok"]:
                    reason = ("does not compile" if not fp["compiled"]
                              else "suspected infinite loop in the smoke run")
                    results[i] = AnalyzerResult.skipped(SKIPPED_SUBMISSION, f"not run: {reason}")
                    done(i)
                else:
                    futures[pool.submit(self._run_analyzer, analyzer, ctx, m)] = i
            for future in as_completed(futures):  # results are stored by rule order regardless
                i = futures[future]
                results[i] = future.result()
                done(i)
        return list(zip(metrics, results))

    def _run_analyzer(self, analyzer, ctx, metric) -> AnalyzerResult:
        params = dict(metric.params)
        if metric.name in self.preparation:
            params["_prepared"] = self.preparation[metric.name]
        try:
            result = analyzer.run(ctx, params)
            log.info("[%s] %-14s score=%.3f (%s)", ctx.name, metric.name, result.score, result.status)
            return result
        except Exception as exc:  # an analyzer bug must not kill the whole grading run
            log.exception("analyzer %s failed", metric.analyzer)
            return AnalyzerResult(0.0, "analyzer error", f"Internal error in {metric.analyzer}: "
                                  f"{type(exc).__name__}: {exc}", {"exception": f"{type(exc).__name__}: {exc}"},
                                  status=ERROR)

    # ------------------------------------------------------------ 3. collate
    def _collate(self, ctx, raw: bytes, fp, flags, results) -> GradeResult:
        counted_weight = sum(m.weight for m, r in results if r.status not in NOT_COUNTED)
        metrics_out = []
        for m, r in results:
            counted = r.status not in NOT_COUNTED
            eff = m.weight / counted_weight if counted and counted_weight > 0 else 0.0
            score = round(clamp01(r.score), 4)
            metrics_out.append({
                "name": m.name, "analyzer": m.analyzer, "weight": m.weight,
                "effective_weight": round(eff, 6), "score": score if counted else None,
                "contribution": round_half_up(100 * eff * score, 2) if counted else 0.0,
                "status": r.status, "summary": ctx.clean(r.summary), "feedback": ctx.clean(r.feedback),
                "evidence": r.evidence, "suggestions": r.suggestions,
            })
            for flag in r.flags:
                flags.append(dict(flag, metric=m.name))
            if r.status == SKIPPED_ENVIRONMENT:
                flags.append({"type": "metric_not_evaluated", "severity": "medium", "metric": m.name,
                              "message": f"{m.name} could not be evaluated here ({r.summary}); its weight was "
                                         f"redistributed over the other metrics"})
            elif r.status == ERROR:
                flags.append({"type": "analyzer_error", "severity": "high", "metric": m.name,
                              "message": f"{r.feedback} - weight redistributed; please report"})
        grade = round_half_up(sum(x["contribution"] for x in metrics_out), 1)

        counterexamples, artifacts = self._counterexamples(ctx, results)
        suggestions = self._suggestions(results)
        sha = sha256_bytes(raw.replace(b"\r\n", b"\n"))
        fingerprint = "sha256:" + sha256_text(canonical_json({
            "submission": sha, "rules": self.rules.rules_hash, "assets": self.assets_hash,
            "toolchain": self.toolchain.versions(),
            "scores": [(x["name"], x["score"], x["status"]) for x in metrics_out], "grade": grade}))
        report = {
            "submission": ctx.name,
            "submission_path": display_path(ctx.submission),
            "submission_sha256": "sha256:" + sha,
            "problem": self.rules.problem,
            "rules_file": display_path(self.rules.path),
            "rules_hash": self.rules.rules_hash,
            "assets_hash": self.assets_hash,
            "grade": grade,
            "max_grade": 100,
            "first_pass": fp,
            "metrics": metrics_out,
            "counterexamples": counterexamples,
            "flags": flags,
            "suggestions": suggestions,
            "usefulness_limitations": self.usefulness,
            "tool_versions": self.toolchain.versions(),
            "agent_version": __version__,
            "grade_fingerprint": fingerprint,
        }
        if self.rules.warnings:
            report["rules_warnings"] = self.rules.warnings
        return GradeResult(json.loads(json.dumps(report)), artifacts)

    def _counterexamples(self, ctx, results):
        limit = int(self.rules.feedback.get("max_counterexamples", 8))
        out, artifacts, seen = [], {}, set()
        folder = f"{ctx.submission.stem}_counterexamples"
        for m, r in results:
            for f in r.failures:
                if f["input"] in seen or len(out) >= limit:
                    continue
                seen.add(f["input"])
                cid = f"CE-{len(out) + 1:02d}"
                files = {"input": f"{folder}/{cid}.in"}
                artifacts[files["input"]] = f["input"]
                if f.get("expected") is not None:
                    files["expected"] = f"{folder}/{cid}.expected"
                    artifacts[files["expected"]] = f["expected"]
                entry = {
                    "id": cid, "metric": m.name, "source": f["source"], "label": f.get("label", ""),
                    "verdict": f["verdict"], "input": shorten(f["input"], 500),
                    "expected": shorten(f.get("expected"), 500), "got": shorten(f.get("got"), 500),
                    "sizes": {k: len(f[k]) for k in ("input", "expected", "got") if f.get(k) is not None},
                    "why": f["why"], "hint": f.get("hint"), "minimized": f.get("minimized", False),
                    "files": files,
                }
                if f.get("original_input"):
                    entry["original_input"] = shorten(f["original_input"], 300)
                out.append(entry)
        return out, artifacts

    @staticmethod
    def _suggestions(results) -> List[Dict[str, Any]]:
        ranked = []
        for order, (m, r) in enumerate(results):
            if r.status in NOT_COUNTED:
                continue
            gain = round(100 * m.weight * (1 - clamp01(r.score)), 2)
            if gain <= 0:
                continue
            for s in r.suggestions:
                ranked.append((-gain, order, s, m.name, gain))
        ranked.sort(key=lambda x: (x[0], x[1]))
        out, seen = [], set()
        for _, _, text, metric, gain in ranked:
            if text in seen:
                continue
            seen.add(text)
            out.append({"metric": metric, "points_at_stake": gain, "suggestion": text})
        return out[:10]
