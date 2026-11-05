"""Runtime test generation with an LLM (used by the ``io_tests`` / ``llm_tests`` analyzers).

There is no reference solution. The model reads the problem statement and the sample tests from the rules and
returns a few short, general test cases. They are saved next to the rules as ``test_cases.json``, always in
this shape, whatever the problem is::

    [{"name": "positive_numbers", "input": "5 7\\n", "expected": "12\\n"}, ...]

The samples in the rules are ground truth: they are always the first cases (``sample_1`` ...). A model-written
case is dropped when it is malformed, empty, a duplicate, too long, or does not satisfy the rules'
``input_spec`` (checked only when the spec describes the samples).

The file is reused by every later run, so the grade stays the same and the file enters ``assets_hash``.
Model replies are cached (temperature 0, fixed seeds), so regenerating reproduces the same file as long as the
cache is kept.

A small model is good at writing a short C solution but poor at working out outputs in its head, so the
expected outputs of the model's cases are not taken from its answer when it can be avoided: the model also
writes a solution, which is run on the samples (the ground truth). When it reproduces every sample, it is
trusted to compute the expected output of every other case, and a case it cannot run is dropped. When it does
not (or there is no compiler), the model's own expected outputs are used, as before.

The prompt is kept small on purpose, for a ~3B model: one short instruction, the statement, the samples and a
format example; the reply is capped in length and the call has a timeout, so the model cannot ramble or stall.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .compare import outputs_equal
from .llm import LLMClient, LLMError, extract_code
from .sandbox import Limits
from .util import normalize_newlines, read_text, shorten

log = logging.getLogger("autograder")

DEFAULT_DIR = "test_cases.json"
EDGE_FILE = "edge_case.json"
SPEC_FILE = "input_spec.json"
SAMPLE_PREFIX = "sample_"

# Defaults tuned for a small local model: short window, capped reply, bounded wait.
SMALL_MODEL = {"model": "qwen2.5-coder:3b", "num_ctx": 2048, "num_predict": 900, "timeout_s": 120}

TESTS_SYSTEM = """You write test cases for a C program. Reply with ONLY a JSON object: {"tests": [ ... ]}
Each item has exactly: "name" (short snake_case), "input" (the stdin text), "expected" (the stdout text).
Rules:
- "input" follows the input format of the problem exactly and respects its constraints.
- "expected" is exactly what a correct program prints, in the same format as the sample outputs.
- Keep every input short and simple. Cover general situations: a typical case, the smallest valid input,
  a boundary value, and special values (zero, negative, even/odd, ...) only when the problem allows them.
- Include a case only if you are sure of its answer. Do not repeat the samples or each other.
- Write newlines as \\n and end "input" and "expected" with \\n. No explanations.
Format example (a different problem, adding two numbers; do not copy it):
{"tests": [{"name": "negative_numbers", "input": "-5 -3\\n", "expected": "-8\\n"},
{"name": "zero", "input": "0 0\\n", "expected": "0\\n"}]}"""

EDGE_SYSTEM = """You write EDGE CASES for a C program. Reply with ONLY a JSON object: {"tests": [ ... ]}
Each item has exactly: "name" (short snake_case), "input" (the stdin text), "expected" (the stdout text).
Rules:
- An edge case is a valid input at the extreme of what the problem allows. Pick the ones that fit this problem:
  the smallest allowed value or size, the largest allowed value, zero, a negative value, a single element,
  all values equal, already sorted / reverse sorted data.
- "input" follows the input format exactly and NEVER goes outside the constraints of the problem.
- Keep every input short: for a "largest" case use a large value, not a long input.
- "expected" is exactly what a correct program prints, in the same format as the sample outputs.
- Include a case only if you are sure of its answer. Do not repeat the samples or each other.
- Write newlines as \\n and end "input" and "expected" with \\n. No explanations.
Format example (a different problem, adding two numbers; do not copy it):
{"tests": [{"name": "largest_values", "input": "1000000000 1000000000\\n", "expected": "2000000000\\n"},
{"name": "smallest_values", "input": "0 0\\n", "expected": "0\\n"}]}"""

SOLUTION_SYSTEM = """You are an expert C programmer. Write a correct, simple C program (C11, only stdio.h and
stdlib.h) that solves the problem. It reads stdin and prints to stdout in exactly the format of the sample
outputs: no prompts, no extra text. Only the output matters, not the method: use the simplest approach that is
certainly correct, even if the problem names an algorithm (for a sorting problem use plain nested loops, not
recursion). Declare every function before it is used. Reply with ONLY the C code in one ```c block."""

SPEC_SYSTEM = """You describe the INPUT FORMAT of a programming problem as JSON, so a program can build inputs of
any size. Reply with ONLY: {"variables": [...], "layout": [[...], ...]}
- "variables": every value the program reads, in reading order. Each has "name", "type" (int, float, str or
  char; add [] for a list, [][] for a matrix) and "min"/"max" from the constraints of the problem.
- A list needs "length" (a number, or the name of an EARLIER variable that holds the count). A matrix needs
  "rows" and "cols" the same way.
- Mark the variable that sets the size of the input with "role": "size": the count of items, or the number
  itself when the input is a single number.
- "layout": one list of variable names per input line, in order.
Example (n, then n integers):
{"variables": [{"name": "n", "type": "int", "min": 1, "max": 100000, "role": "size"},
{"name": "a", "type": "int[]", "length": "n", "min": -1000, "max": 1000}], "layout": [["n"], ["a"]]}
Example (a single number):
{"variables": [{"name": "n", "type": "int", "min": 1, "max": 1000000000, "role": "size"}], "layout": [["n"]]}"""

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class TestGenError(RuntimeError):
    pass


# ---------------------------------------------------------------- entry point
def prepare(agent, params: Dict[str, Any], kind: str = "tests") -> Dict[str, Any]:
    """Make sure ``test_cases.json`` (``kind="edge"``: ``edge_case.json``) exists. Returns ``{}`` or ``{"error": ...}``."""
    rules = agent.rules
    with _lock_for(rules.base_dir):  # one generator per problem folder at a time
        try:
            ensure_tests(rules, agent.cache_dir, params, agent.inputgen, getattr(agent, "progress", None),
                         getattr(agent, "sandbox", None), kind)
            return {}
        except (TestGenError, LLMError) as exc:
            log.warning("llm_tests: %s", exc)
            return {"error": str(exc)}


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(Path(path).resolve()), threading.Lock())


def _client(params: Dict[str, Any], seed: int, cache_dir: Path) -> LLMClient:
    return LLMClient.from_params({**SMALL_MODEL, **params}, cache_dir=Path(cache_dir) / "llm", seed=seed)


def _block(text: str) -> str:
    return "```\n" + text + ("" if text.endswith("\n") else "\n") + "```"


def _samples_text(samples: List[Dict[str, str]]) -> str:
    parts = []
    for i, s in enumerate(samples, 1):
        parts.append(f"Sample {i} input:\n{_block(s['input'])}\nSample {i} output:\n{_block(s['output'])}")
    return "\n".join(parts)


def _clean(text: str) -> str:
    text = normalize_newlines(text)
    if "\n" not in text and "\\n" in text:  # the model escaped its newlines twice
        text = text.replace("\\n", "\n")
    return text if not text.strip() or text.endswith("\n") else text + "\n"


def _name(raw: Any, taken: set, fallback: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", str(raw or "").lower()).strip("_")[:40] or fallback
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def parse_cases(text: str) -> List[Any]:
    """The list of case objects in a model reply: a bare array or an object holding one."""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    for candidate in (cleaned, cleaned[cleaned.find("["):cleaned.rfind("]") + 1],
                      cleaned[cleaned.find("{"):cleaned.rfind("}") + 1]):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            value = value.get("tests") if isinstance(value.get("tests"), list) else next(
                (v for v in value.values() if isinstance(v, list)), [])
        if isinstance(value, list):
            return value
    return []


def load_cases(path: Path) -> Optional[List[Dict[str, str]]]:
    """The cases of an existing ``test_cases.json``; None if it is missing or not in the expected shape."""
    try:
        data = json.loads(read_text(path))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list) or not data:
        return None
    if not all(isinstance(c, dict) and isinstance(c.get("input"), str) and isinstance(c.get("expected"), str)
               for c in data):
        return None
    return [{"name": str(c.get("name") or f"case_{i}"), "input": c["input"], "expected": c["expected"]}
            for i, c in enumerate(data, 1)]


# ---------------------------------------------------------------- the tests
def _tidy(text: str) -> str:
    """Program output as a test's expected output: no trailing spaces, one final newline."""
    lines = [l.rstrip() for l in normalize_newlines(text).split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def _model_solution(rules, params: Dict[str, Any], cache_dir: Path, sandbox, samples, statement: str):
    """A function input -> output (None if the program fails) from a C solution the model wrote, or None.

    The solution is accepted only if it reproduces every sample output.
    """
    if sandbox is None:
        return None
    mode = rules.compare.get("mode", "lines")
    abs_eps, rel_eps = float(rules.compare.get("abs_eps", 1e-6)), float(rules.compare.get("rel_eps", 1e-6))
    limits = Limits(cpu_time_s=2.0, wall_time_s=6.0, memory_mb=256)
    task = f"Problem:\n{statement[:1800]}\n\n{_samples_text(samples[:3])}"
    seed = int(params.get("seed", 42))
    for attempt in range(max(1, int(params.get("max_attempts", 3)))):
        client = LLMClient.from_params({**SMALL_MODEL, **params, "num_predict": 700}, cache_dir=Path(cache_dir) / "llm",
                                       seed=seed + attempt, json_mode=False)
        try:
            code = extract_code(client.chat(SOLUTION_SYSTEM, task).text)
        except LLMError as exc:
            if "cannot reach" in str(exc):
                raise
            continue
        work = Path(tempfile.mkdtemp(prefix="solution_", dir=str(sandbox.scratch)))
        try:
            built = None
            for repair in range(2):  # the second round gives the model the compiler's complaints
                (work / "solution.c").write_text(code, encoding="utf-8")
                exe = "solution" + sandbox.toolchain.exe_suffix
                built = sandbox.compile(["solution.c"], exe, flags=f"-O2 {rules.std_flag}",
                                        link_flags=rules.link_flags, cwd=work)
                if built.ok or repair:
                    break
                try:
                    fixed = client.chat(SOLUTION_SYSTEM, f"{task}\n\nYour code:\n```c\n{code}```\n"
                                        f"It does not compile:\n{shorten(built.output, 600)}\n"
                                        f"Reply with the complete corrected C code.")
                except LLMError as exc:
                    if "cannot reach" in str(exc):
                        raise
                    break
                code = extract_code(fixed.text)
            if built is None or not built.ok:
                shutil.rmtree(work, ignore_errors=True)
                continue

            def solve(text: str, exe_path=built.exe) -> Optional[str]:
                run = sandbox.run([str(exe_path)], text, limits)
                return _tidy(run.stdout) if run.ok and run.stdout.strip() else None

            if all(outputs_equal(s["output"], solve(s["input"]) or "", mode, abs_eps, rel_eps) for s in samples):
                # the executable lives in `work`: keep it until the tests are written
                return solve, work
        except OSError:
            pass
        shutil.rmtree(work, ignore_errors=True)
    return None


def ensure_input_spec(rules, cache_dir: Path, params: Dict[str, Any], sandbox=None):
    """An ``InputGenerator`` for rules that give no ``input_spec``: the model describes the input format.

    The description is saved as ``input_spec.json`` next to the rules and reused. It is accepted only if it is a
    valid spec and it parses every sample input (the samples are the ground truth for the format) and the
    inputs it builds for a size parse again. Returns None when the model cannot produce such a spec.
    """
    from .inputgen import InputGenerator, InputSpecError

    samples = [s for s in rules.samples if isinstance(s.get("input"), str)]
    statement = rules.statement.strip()
    target = rules.resolve(params.get("input_spec_file", SPEC_FILE))

    def accept(spec: Any):
        try:
            gen = InputGenerator(spec, rules.generation_seed)
            if not gen.vars or not samples or any(gen.parse(smp["input"]) is None for smp in samples):
                return None
            probe = gen.for_size(10)
            return gen if gen.parse(probe.text) is not None else None
        except (InputSpecError, ValueError, KeyError, TypeError, OverflowError):
            return None

    try:
        gen = accept(json.loads(read_text(target)))
        if gen is not None:
            return gen
    except (OSError, ValueError):
        pass
    if not statement or not samples:
        return None
    task = f"Problem:\n{statement[:1800]}\n\n{_samples_text([dict(s, output=s.get('output') or '') for s in samples][:3])}"
    seed = int(params.get("seed", 42))
    for attempt in range(max(1, int(params.get("max_attempts", 3)))):
        client = _client(params, seed + attempt, cache_dir)
        log.warning("complexity: asking %s for the input format (attempt %d)", client.model, attempt + 1)
        try:
            spec, _ = client.chat_json(SPEC_SYSTEM, task)
        except LLMError as exc:
            if "cannot reach" in str(exc):
                return None
            continue
        gen = accept(spec)
        if gen is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            log.warning("complexity: wrote the input format to %s", target)
            return gen
    log.warning("complexity: the model's input format does not match the samples; the check is skipped")
    return None


def ensure_tests(rules, cache_dir: Path, params: Dict[str, Any], inputgen, progress=None, sandbox=None,
                 kind: str = "tests") -> None:
    """Write ``test_cases.json`` (kind ``tests``) or ``edge_case.json`` (kind ``edge``) unless it already exists.

    Edge cases use the same format and the same checks; they are only the extremes of the input, and the
    samples are not part of the file (they are still what the model's solution is checked against).
    """
    edge = kind == "edge"
    default = EDGE_FILE if edge else DEFAULT_DIR
    target = rules.resolve(params.get("edge_file" if edge else "testset_dir", default))
    if target.suffix != ".json":
        target = target / default
    if load_cases(target) is not None:
        return
    statement = rules.statement.strip()
    if not statement:
        raise TestGenError('cannot generate tests: the rules have no problem statement (put it in "problem")')
    samples = [s for s in rules.samples if isinstance(s.get("output"), str)]
    if not samples:
        raise TestGenError('cannot generate tests: the rules need at least one "sample" with an "output"')

    want = int(params.get("num_edge_cases", 5) if edge else params.get("num_tests", 8))
    attempts = max(1, int(params.get("max_attempts", 3)))
    max_chars = int(params.get("max_input_chars", 200))
    seed = int(params.get("seed", 42))

    # Filter by input_spec only when it describes the samples: a spec that rejects the instructor's own
    # sample is wrong, and would otherwise reject every correct test the model writes.
    spec = inputgen if inputgen is not None and inputgen.vars else None
    if spec is not None and any(spec.parse(s["input"]) is None for s in samples):
        spec = None
        log.warning("llm_tests: the input_spec in the rules does not match the sample input; not using it")

    cases: List[Dict[str, str]] = []
    names: set = set()
    seen: set = set()
    for i, s in enumerate(samples, 1):
        text = _clean(s["input"])
        seen.add(text)
        if not edge:
            cases.append({"name": _name(f"{SAMPLE_PREFIX}{i}", names, f"{SAMPLE_PREFIX}{i}"),
                          "input": text, "expected": _clean(s["output"])})
    fixed = len(cases)  # the samples: ground truth, never changed

    task = f"Problem:\n{statement[:1800]}\n\n{_samples_text(samples)}\n\n"
    if edge:
        hints = [str(c) for c in params.get("cases") or [] if isinstance(c, str)]  # the rules' category names
        task += f"Write {want} edge cases." + (f" Useful situations: {', '.join(hints)}." if hints else "")
    else:
        task += f"Write {want} test cases."
    designed, rejected = 0, 0
    for attempt in range(attempts):
        if designed >= min(want, 4):  # enough for general coverage; do not keep a small model busy
            break
        client = _client(params, seed + attempt, cache_dir)
        if progress is not None:
            progress({"type": "generating", "step": attempt, "of": attempts,
                      "theme": "loading the model (the first call can take a minute)" if attempt == 0
                      else "asking again", "kept": designed})
        log.warning("llm_tests: asking %s for test cases (attempt %d/%d)", client.model, attempt + 1, attempts)
        try:
            items = parse_cases(client.chat(EDGE_SYSTEM if edge else TESTS_SYSTEM, task).text)
        except LLMError as exc:
            if "cannot reach" in str(exc):  # the model is down: stop asking
                raise
            log.warning("llm_tests: no usable reply (%s)", shorten(str(exc), 120))
            continue
        for item in items:
            if designed >= want:
                break
            if not isinstance(item, dict):
                continue
            text, out = item.get("input"), item.get("expected", item.get("output"))
            if isinstance(out, (int, float)) and not isinstance(out, bool):
                out = str(out)
            if not isinstance(text, str) or not isinstance(out, str):
                rejected += 1
                continue
            text, out = _clean(text), _clean(out)
            if (not text.strip() or not out.strip() or len(text) > max_chars or text in seen
                    or (spec is not None and spec.parse(text) is None)):
                rejected += 1
                continue
            seen.add(text)
            cases.append({"name": _name(item.get("name"), names, f"case_{designed + 1}"),
                          "input": text, "expected": out})
            designed += 1

    # Replace the model's own answers by the output of a solution it wrote, when that solution is right on
    # every sample: a 3B model rarely computes outputs correctly in its head, but it writes short programs well.
    solution = _model_solution(rules, params, cache_dir, sandbox, samples, statement) if designed else None
    if solution is not None:
        solve, work = solution
        try:
            kept, changed = [], 0
            for idx, c in enumerate(cases):
                if idx < fixed:
                    kept.append(c)
                    continue
                out = solve(c["input"])
                if out is None:  # the solution cannot run this input: it is probably not a valid input
                    continue
                changed += out != c["expected"]
                kept.append(dict(c, expected=out))
            log.warning("llm_tests: expected outputs computed by the model's solution (%d corrected, %d dropped)",
                        changed, len(cases) - len(kept))
            cases, designed = kept, len(kept) - fixed
        finally:
            shutil.rmtree(work, ignore_errors=True)
    elif designed and sandbox is not None:
        log.warning("llm_tests: the model's solution does not match the samples; using the model's own outputs")

    if designed == 0 and edge:
        log.warning("llm_tests: no usable edge case from the model (%d rejected); no edge_case.json written", rejected)
        return
    if designed == 0:
        # The samples are certainly right: grade on them rather than drop the metric.
        log.warning("llm_tests: no usable test case from the model (%d rejected); only the samples are used",
                    rejected)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log.warning("llm_tests: wrote %d tests to %s", len(cases), target)
