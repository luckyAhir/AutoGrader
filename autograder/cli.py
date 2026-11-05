"""Command-line interface.

    python -m autograder grade SUBMISSION... --rules RULES [--out DIR]
    python -m autograder verify SUBMISSION --rules RULES [--runs 3]
    python -m autograder validate-rules RULES
    python -m autograder doctor
    python -m autograder list-analyzers
    python -m autograder probe-llm --rules RULES [--samples 2]
    python -m autograder originality DIR [--rules RULES]
    python -m autograder serve [--port 8000] [--open]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import List

from . import __version__
from .analyzers import available_analyzers
from .rules import RulesError, load_rules
from .toolchain import ToolchainError, detect


def _submissions(paths: List[str]) -> List[Path]:
    out: List[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out.extend(sorted(path.glob("*.c"), key=lambda x: x.name))
        elif path.is_file():
            out.append(path)
        else:
            raise FileNotFoundError(f"no such submission: {p}")
    return out


def _console_safe() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def cmd_grade(args) -> int:
    from .agent import GradingAgent
    from .report import render_text, write_outputs, write_summary

    rules = load_rules(Path(args.rules))
    for w in rules.warnings:
        print(f"warning: {w}", file=sys.stderr)
    subs = _submissions(args.submissions)
    if not subs:
        print("no .c files to grade", file=sys.stderr)
        return 1
    out_dir = Path(args.out) if args.out else Path("reports") / rules.problem
    toolchain = detect(args.cc)
    reports = []
    kept_root = Path(args.submissions_dir) if args.submissions_dir else None
    with GradingAgent(rules, toolchain, jobs=args.jobs, cache_dir=Path(args.cache_dir),
                      keep_workdir=args.keep_workdir, submissions_dir=kept_root) as agent:
        for sub in subs:
            print(f"grading {sub} ...", file=sys.stderr, flush=True)
            result = agent.grade(sub)
            if kept_root is not None:  # keep the program: it is the originality corpus for the next ones
                import shutil

                from .analyzers.similarity_flag import corpus_name
                kept = kept_root / corpus_name(rules.problem)
                kept.mkdir(parents=True, exist_ok=True)
                if sub.resolve().parent != kept.resolve():
                    shutil.copy2(sub, kept / sub.name)
            paths = write_outputs(result, out_dir)
            reports.append(result.report)
            if len(subs) == 1 and not args.quiet:
                print(render_text(result.report))
            print(f"  -> {result.grade:5.1f}/100   {paths['text'].as_posix()}", file=sys.stderr)
    if len(subs) > 1:
        paths = write_summary(reports, out_dir)
        print(f"summary: {paths['md'].as_posix()}", file=sys.stderr)
        if not args.quiet:
            print(paths["md"].read_text(encoding="utf-8"))
    return 0


def cmd_originality(args) -> int:
    """Compare every program in a folder with every other: who copied from whom."""
    from .csource import parse
    from .similarity import boilerplate, fingerprint, matched_line_ranges, overlap
    from .util import read_text

    params = {}
    base = None
    if args.rules:
        rules = load_rules(Path(args.rules))
        params = next((m.params for m in rules.metrics if m.analyzer == "similarity_flag"), {})
        ref = rules.reference_path
        if params.get("ignore_reference", True) and ref and ref.is_file():
            base = fingerprint(parse(read_text(ref)), 5, 4)
    k, w = int(params.get("k", 5)), int(params.get("window", 4))
    threshold, min_shared = float(params.get("flag_threshold", 0.85)), int(params.get("min_shared", 8))
    min_identical = int(params.get("min_tokens_identical", 50))
    files = sorted(Path(args.dir).rglob("*.c"), key=lambda p: p.as_posix())
    if len(files) < 2:
        print("need at least two .c files to compare", file=sys.stderr)
        return 1
    fps = {f: fingerprint(parse(read_text(f)), k, w) for f in files}
    common = boilerplate(base, list(fps.values()), float(params.get("common_fraction", 0.5)))
    pairs = []
    for i, a in enumerate(files):
        for b in files[i + 1:]:
            fa, fb = fps[a], fps[b]
            ka, kb = fa.keys - common, fb.keys - common
            shared = ka & kb
            exact = fa.tokens == fb.tokens and len(fa.tokens) > 0
            sim = 1.0 if exact else overlap(ka, kb)
            if sim >= threshold and (len(shared) >= min_shared or (exact and min(len(fa.tokens), len(fb.tokens)) >= min_identical)):
                pairs.append((sim, a, b, shared))
    for sim, a, b, shared in sorted(pairs, key=lambda x: (-x[0], x[1].name, x[2].name)):
        ta, tb = a.stat().st_mtime, b.stat().st_mtime
        if abs(ta - tb) <= 1.0:
            line = f"{a.name} ~ {b.name}  (order unknown)"
        else:
            early, late = (a, b) if ta < tb else (b, a)
            line = f"{late.name}  <-  {early.name}  ({late.name} is the likely copy)"
        ranges = ", ".join(f"{x}-{y}" if x != y else str(x) for x, y in matched_line_ranges(fps[a], shared, k)[:4])
        print(f"{sim:.2f}  {line}   lines {ranges or '-'} in {a.name}")
    print(f"{len(pairs)} flagged pair(s) among {len(files)} programs (threshold {threshold:.2f})")
    return 0


def cmd_verify(args) -> int:
    """Grade the same submission several times with fresh agents; compare everything."""
    from .agent import GradingAgent

    rules = load_rules(Path(args.rules))
    toolchain = detect(args.cc)
    fingerprints, texts = [], []
    for i in range(args.runs):
        with GradingAgent(rules, toolchain, jobs=args.jobs, cache_dir=Path(args.cache_dir)) as agent:
            result = agent.grade(Path(args.submission))
        text = json.dumps(result.report, sort_keys=True)
        fingerprints.append(result.report["grade_fingerprint"])
        texts.append(text)
        print(f"run {i + 1}: grade {result.grade:.1f}  fingerprint {result.report['grade_fingerprint'][:23]}...  "
              f"report sha {hashlib.sha256(text.encode()).hexdigest()[:12]}")
    same_grade = len(set(fingerprints)) == 1
    same_report = len(set(texts)) == 1
    print(f"identical grade + scores: {'yes' if same_grade else 'NO'}")
    print(f"byte-identical JSON report: {'yes' if same_report else 'NO'}")
    return 0 if same_grade else 1


def cmd_validate(args) -> int:
    """Check a rules file and its assets: schema, analyzers, reference vs. tests."""
    from .agent import GradingAgent
    from .compare import outputs_equal

    rules = load_rules(Path(args.rules))
    print(f"rules OK: {rules.problem} ({len(rules.metrics)} metrics, weights sum to 1.00)")
    for w in rules.warnings:
        print(f"  warning: {w}")
    toolchain = detect(args.cc)
    problems = 0
    with GradingAgent(rules, toolchain) as agent:
        print("analyzers: " + ", ".join(f"{m.name}->{m.analyzer}" for m in rules.metrics))
        if agent.oracle is None:
            print("no reference solution: generated edge cases are checked for crashes only")
            return 0
        if not agent.oracle.build():
            print("reference solution does NOT compile:\n" + (agent.oracle.error or ""))
            return 1
        print("reference solution compiles")
        for m in rules.metrics:
            if m.analyzer not in ("io_tests", "llm_tests"):
                continue
            error = agent.preparation.get(m.name, {}).get("error")
            if error:
                print(f"{m.name}: test generation failed: {error}")
                problems += 1
                continue
            d = rules.resolve(m.params.get("testset_dir", "tests/" if m.analyzer == "io_tests" else "test_cases.json"))
            for inp in sorted(d.glob("*.in")) if d.is_dir() else []:
                exp_file = inp.with_suffix(".out")
                problems += _check_case(agent, rules, inp.name, inp.read_text(encoding="utf-8"),
                            exp_file.read_text(encoding="utf-8") if exp_file.is_file() else None)
            from .testgen import load_cases
            for c in load_cases(d if d.suffix == ".json" else d / "test_cases.json") or []:
                problems += _check_case(agent, rules, c["name"], c["input"], c["expected"])
            print(f"tests in {d}: {'all consistent with the reference' if not problems else f'{problems} problem(s)'}")
        for m in rules.metrics:
            if m.analyzer == "edge_case_runner":
                edge = rules.resolve(m.params.get("edge_file", "edge_case.json"))
                for c in load_cases(edge) or []:
                    problems += _check_case(agent, rules, c["name"], c["input"], c["expected"])
                print(f"edge cases in {edge}: checked against the reference")
    return 1 if problems else 0


def _check_case(agent, rules, name: str, text: str, expected) -> int:
    """1 if the reference solution fails on this test or disagrees with its expected output."""
    ref = agent.oracle.expected(text)
    if ref is None:
        print(f"  {name}: reference fails on this input")
        return 1
    if expected is not None and not outputs_equal(expected, ref, rules.compare["mode"]):
        print(f"  {name}: reference output disagrees with the expected output")
        return 1
    return 0


def cmd_doctor(args) -> int:
    try:
        tc = detect(args.cc)
    except ToolchainError as exc:
        print(f"compiler: MISSING ({exc})")
        return 1
    import tempfile

    print(f"autograder {__version__}")
    print(f"compiler : {tc.cc} ({tc.cc_family} {tc.cc_version})")
    print(f"gcov     : {tc.gcov or 'missing -> complexity falls back to CPU timing'}")
    print(f"cppcheck : {tc.cppcheck_version or 'missing -> structure uses built-in checks only'}")
    print(f"valgrind : {tc.valgrind_version or 'missing'}")
    with tempfile.TemporaryDirectory() as tmp:
        asan = tc.supports("-fsanitize=address,undefined", Path(tmp))
        trap = tc.supports("-fsanitize=undefined -fsanitize-undefined-trap-on-error", Path(tmp))
    print(f"ASan+UBSan: {'yes' if asan else 'no'}    UBSan trap mode: {'yes' if trap else 'no'}")
    from .env import ollama_model
    from .llm import LLMClient, LLMError

    args.model = args.model or ollama_model()
    try:
        LLMClient(model=args.model, cache_dir=None, timeout_s=30).chat("Reply with {\"ok\": true}", "ping")
        print(f"ollama   : reachable (model {args.model})")
    except LLMError as exc:
        print(f"ollama   : not reachable ({str(exc)[:80]}) -> readability uses cached replies or heuristics")
    return 0


def cmd_list(args) -> int:
    for name, cls in available_analyzers().items():
        kind = "execution" if cls.needs_binary else "source"
        print(f"{name:<22} [{kind:<9}] {cls.description}")
    return 0


def cmd_probe_llm(args) -> int:
    """Ask an LLM to solve the problem; store its answers as known sources for originality checks."""
    from .llm import LLMClient, LLMError, extract_code

    rules = load_rules(Path(args.rules))
    statement = rules.statement.strip()
    if not statement:
        print("the rules have no statement/statement_file", file=sys.stderr)
        return 1
    target = None
    for m in rules.metrics:
        if m.analyzer == "similarity_flag" and m.params.get("known_sources_dir"):
            target = rules.resolve(m.params["known_sources_dir"])
    target = Path(args.out) if args.out else target
    if target is None:
        print("no known_sources_dir in the rules; pass --out", file=sys.stderr)
        return 1
    target.mkdir(parents=True, exist_ok=True)
    from .env import ollama_model

    args.model = args.model or ollama_model()
    model_tag = "".join(c if c.isalnum() else "_" for c in args.model)
    for seed in range(1, args.samples + 1):
        client = LLMClient(provider=args.provider, model=args.model, temperature=args.temperature, seed=seed,
                           json_mode=False, cache_dir=Path(args.cache_dir) / "llm")
        try:
            reply = client.chat("You are a helpful programming assistant.",
                                "Write a complete C program (C11) that solves the following problem. "
                                "Reply with the code only.\n\n" + statement)
        except LLMError as exc:
            print(f"LLM error: {exc}", file=sys.stderr)
            return 1
        path = target / f"llm_{model_tag}_seed{seed}.c"
        header = (f"/* Generated by `autograder probe-llm` for originality checks.\n"
                  f"   provider={args.provider} model={args.model} temperature={args.temperature} seed={seed} */\n")
        path.write_bytes((header + extract_code(reply.text)).encode("utf-8"))
        print(f"wrote {path.as_posix()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autograder", description="Rule-driven, reproducible grader for C programs")
    p.add_argument("--version", action="version", version=f"autograder {__version__}")
    p.add_argument("-v", "--verbose", action="count", default=0)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, rules=True):
        if rules:
            sp.add_argument("--rules", required=True, help="rules file (JSON/YAML)")
        sp.add_argument("--cc", help="C compiler (default: $AUTOGRADER_CC or gcc on PATH)")
        sp.add_argument("--jobs", type=int, default=None, help="analyzers run in parallel (default: min(4, CPUs))")
        sp.add_argument("--cache-dir", default=".autograder_cache", help="LLM response cache directory")

    g = sub.add_parser("grade", help="grade one or more submissions (files or directories)")
    g.add_argument("submissions", nargs="+")
    g.add_argument("--out", help="output directory (default: reports/<problem>)")
    g.add_argument("--quiet", action="store_true", help="do not print the text report")
    g.add_argument("--keep-workdir", action="store_true", help="keep temporary build directories")
    g.add_argument("--submissions-dir", help="keep graded programs in DIR/<problem>/ and use them as the "
                                             "originality corpus (who copied from whom)")
    common(g)
    g.set_defaults(func=cmd_grade)

    v = sub.add_parser("verify", help="grade repeatedly and check the result is reproducible")
    v.add_argument("submission")
    v.add_argument("--runs", type=int, default=3)
    common(v)
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("validate-rules", help="check a rules file, its reference solution and tests")
    r.add_argument("rules")
    r.add_argument("--cc")
    r.set_defaults(func=cmd_validate)

    d = sub.add_parser("doctor", help="show which tools are available")
    d.add_argument("--cc")
    d.add_argument("--model", default=None, help="Ollama model (default: OLLAMA_MODEL from .env)")
    d.set_defaults(func=cmd_doctor)

    og = sub.add_parser("originality", help="compare all programs in a folder: who copied from whom")
    og.add_argument("dir", help="folder with the .c files, e.g. submissions/<problem>")
    og.add_argument("--rules", help="rules file: uses its similarity_flag thresholds and reference solution")
    og.set_defaults(func=cmd_originality)

    la = sub.add_parser("list-analyzers", help="list the registered analyzers")
    la.set_defaults(func=cmd_list)

    pl = sub.add_parser("probe-llm", help="generate LLM solutions to use as known sources for originality")
    pl.add_argument("--rules", required=True)
    pl.add_argument("--provider", default="ollama")
    pl.add_argument("--model", default=None, help="model (default: OLLAMA_MODEL from .env)")
    pl.add_argument("--samples", type=int, default=2)
    pl.add_argument("--temperature", type=float, default=0.7)
    pl.add_argument("--out", help="directory (default: the rules' known_sources_dir)")
    pl.add_argument("--cache-dir", default=".autograder_cache")
    pl.set_defaults(func=cmd_probe_llm)

    s = sub.add_parser("serve", help="open the browser interface")
    s.add_argument("--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1, this machine only)")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--root", default=".", help="project folder; problems/ is rebuilt for every graded program, reports/ keeps the reports")
    s.add_argument("--open", action="store_true", help="open the interface in the default browser")
    common(s, rules=False)
    s.set_defaults(func=cmd_serve)
    return p


def cmd_serve(args) -> int:
    import os
    import webbrowser

    from .web.server import WebApp, make_server

    root = Path(args.root).resolve()
    os.chdir(root)  # report paths are written relative to the project folder
    app = WebApp(root, detect(args.cc), Path(args.cache_dir), jobs=args.jobs)
    server = make_server(app, args.host, args.port)
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0', '::') else args.host}:{args.port}/"
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: listening on {args.host}; anyone who can reach this port can run C code here",
              file=sys.stderr)
    print(f"AutoGrader is running at {url}  (Ctrl+C to stop)", file=sys.stderr, flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0


def main(argv=None) -> int:
    _console_safe()
    args = build_parser().parse_args(argv)
    level = logging.WARNING if args.verbose == 0 else logging.INFO if args.verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")
    try:
        return args.func(args)
    except RulesError as exc:
        print(f"rules error: {exc}", file=sys.stderr)
        return 2
    except ToolchainError as exc:
        print(f"toolchain error: {exc}", file=sys.stderr)
        return 3
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
