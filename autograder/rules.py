"""Loading and validating the instructor's rules file.

The rules live outside the agent's source code (JSON, or YAML when PyYAML is
installed). They name the metrics to grade, the analyzer that measures each
metric, its weight and its parameters. Adding a metric or changing a threshold
never requires editing the agent.

Relative paths inside the rules (tests, reference solution, corpus ...) are
resolved against the directory that contains the rules file, so a problem
folder can be moved or zipped as a unit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .util import canonical_json, hash_files, read_text, sha256_text

WEIGHT_TOLERANCE = 1e-6
COMPARE_MODES = ("exact", "lines", "tokens", "float")

KNOWN_TOP_LEVEL = {
    "problem", "description", "version", "statement", "statement_file",
    "time_limit_s", "memory_limit_mb", "output_limit_kb", "compile_flags",
    "link_flags", "reference_solution", "sample", "input_spec", "generator",
    "compare", "metrics", "plugins", "seed", "usefulness_limitations_file",
    "feedback", "nonzero_exit_is_error",
}


class RulesError(ValueError):
    """The rules file is missing, malformed or inconsistent."""


@dataclass
class MetricRule:
    name: str
    analyzer: str
    weight: float
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Rules:
    path: Path
    raw: Dict[str, Any]
    rules_hash: str
    problem: str
    time_limit_s: float
    memory_limit_mb: int
    metrics: List[MetricRule]
    warnings: List[str] = field(default_factory=list)
    base: Optional[Path] = None  # where relative paths resolve; default: the rules file's folder

    # ----------------------------------------------------------- helpers
    @property
    def base_dir(self) -> Path:
        return self.base or self.path.parent

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self.base_dir / p)

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    # ----------------------------------------------------------- typed views
    @property
    def statement(self) -> str:
        if self.raw.get("statement"):
            return str(self.raw["statement"])
        if self.raw.get("statement_file"):
            return read_text(self.resolve(self.raw["statement_file"]))
        text = str(self.raw.get("problem") or "")
        return text if _is_prose(text) else ""  # "problem" may hold the statement itself

    @property
    def compile_flags(self) -> str:
        return str(self.raw.get("compile_flags", "-O2 -Wall -Wextra -std=c11"))

    @property
    def std_flag(self) -> str:
        for flag in self.compile_flags.split():
            if flag.startswith("-std="):
                return flag
        return "-std=c11"

    @property
    def link_flags(self) -> str:
        return str(self.raw.get("link_flags", "-lm"))

    @property
    def reference_path(self) -> Optional[Path]:
        ref = self.raw.get("reference_solution")
        return self.resolve(ref) if ref else None

    @property
    def samples(self) -> List[Dict[str, str]]:
        """``sample`` may be one ``{"input", "output"}`` object or a list of them."""
        sample = self.raw.get("sample")
        if isinstance(sample, dict):
            return [sample]
        return list(sample) if isinstance(sample, list) else []

    @property
    def sample(self) -> Optional[Dict[str, str]]:
        samples = self.samples
        return samples[0] if samples else None

    @property
    def input_spec(self) -> Optional[Dict[str, Any]]:
        return self.raw.get("input_spec")

    @property
    def generator_path(self) -> Optional[Path]:
        gen = self.raw.get("generator")
        return self.resolve(gen) if gen else None

    @property
    def compare(self) -> Dict[str, Any]:
        cfg = {"mode": "lines"}
        cfg.update(self.raw.get("compare") or {})
        return cfg

    @property
    def output_limit_kb(self) -> int:
        return int(self.raw.get("output_limit_kb", 8192))

    @property
    def nonzero_exit_is_error(self) -> bool:
        return bool(self.raw.get("nonzero_exit_is_error", True))

    @property
    def plugins(self) -> List[Path]:
        return [self.resolve(p) for p in self.raw.get("plugins", [])]

    @property
    def feedback(self) -> Dict[str, Any]:
        return dict(self.raw.get("feedback") or {})

    @property
    def generation_seed(self) -> str:
        """Seed material for generated inputs.

        It depends only on the problem and the input specification, so
        re-weighting metrics does not change which edge cases are generated.
        """
        if "seed" in self.raw:
            return f"seed:{self.raw['seed']}"
        return sha256_text(self.problem + canonical_json(self.input_spec or {}))

    def metric(self, name: str) -> Optional[MetricRule]:
        for m in self.metrics:
            if m.name == name:
                return m
        return None

    def asset_files(self) -> List[Path]:
        """Files (other than student submissions) whose content influences grades."""
        files: List[Path] = [self.path]
        for key in ("reference_solution", "statement_file", "generator", "usefulness_limitations_file"):
            if self.raw.get(key):
                files.append(self.resolve(self.raw[key]))
        files.extend(self.plugins)
        for name in ("test_cases.json", "edge_case.json", "input_spec.json"):  # tests an LLM wrote (see testgen)
            generated = self.resolve(name)
            if generated.is_file():
                files.append(generated)
        for m in self.metrics:
            for key, value in sorted(m.params.items()):
                if not isinstance(value, str) or key == "corpus_dir":
                    continue
                if key.endswith("_dir"):
                    d = self.resolve(value)
                    if d.is_dir():
                        files.extend(sorted(p for p in d.rglob("*") if p.is_file()))
                    elif d.is_file():
                        files.append(d)
                elif key.endswith("_file"):
                    files.append(self.resolve(value))
        return files

    def assets_hash(self) -> str:
        return hash_files(self.asset_files(), self.base_dir)


# ---------------------------------------------------------------- loading
def load_rules(path: Path, base_dir: Optional[Path] = None) -> Rules:
    """Load a rules file. Relative paths resolve from ``base_dir`` (default: the file's folder)."""
    path = Path(path)
    if not path.is_file():
        raise RulesError(f"rules file not found: {path}")
    return rules_from_text(read_text(path), path, base_dir)


def rules_from_text(text: str, path: Path, base_dir: Optional[Path] = None) -> Rules:
    """Parse and validate rules given as text (e.g. uploaded in the browser).

    ``path`` names the rules (it need not exist); relative paths inside the rules
    resolve from ``base_dir``, or from ``path``'s folder when no base is given.
    """
    path = Path(path)
    base = Path(base_dir).resolve() if base_dir else path.parent
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RulesError("YAML rules need PyYAML (pip install pyyaml); or use JSON") from exc
        raw = yaml.safe_load(text)
    else:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RulesError(
                f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
    if not isinstance(raw, dict):
        raise RulesError(f"{path}: the top level of the rules file must be an object")

    errors, warnings = _validate(raw, base)
    if errors:
        raise RulesError(f"invalid rules file {path}:\n  - " + "\n  - ".join(errors))

    metrics = [
        MetricRule(
            name=str(m["name"]),
            analyzer=str(m["analyzer"]),
            weight=float(m["weight"]),
            params=dict(m.get("params") or {}),
        )
        for m in raw["metrics"]
    ]
    return Rules(
        path=path.resolve(),
        raw=raw,
        rules_hash="sha256:" + sha256_text(canonical_json(raw)),
        problem=_problem_name(str(raw["problem"]), path, base),
        time_limit_s=float(raw.get("time_limit_s", 1)),
        memory_limit_mb=int(raw.get("memory_limit_mb", 64)),
        metrics=metrics,
        warnings=warnings,
        base=base.resolve() if base_dir else None,
    )


def _is_prose(text: str) -> bool:
    """``problem`` is a short name (``prime_number``) or, when it has whitespace, the problem statement."""
    return bool(re.search(r"\s", text.strip()))


def _problem_name(raw_problem: str, path: Path, base: Path) -> str:
    if not _is_prose(raw_problem):
        return raw_problem.strip()
    folder = (base if base.name else path.resolve().parent).name
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", folder).strip("._") or "problem"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate(raw: Dict[str, Any], base: Path):
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(raw.get("problem"), str) or not raw["problem"].strip():
        errors.append('"problem" must be a non-empty string')
    for key in ("time_limit_s", "memory_limit_mb"):
        if key in raw and (not _is_number(raw[key]) or raw[key] <= 0):
            errors.append(f'"{key}" must be a positive number')

    metrics = raw.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        errors.append('"metrics" must be a non-empty list')
        metrics = []
    names = set()
    total = 0.0
    for i, m in enumerate(metrics):
        where = f"metrics[{i}]"
        if not isinstance(m, dict):
            errors.append(f"{where} must be an object")
            continue
        name = m.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f'{where}: "name" must be a non-empty string')
        elif name in names:
            errors.append(f'{where}: duplicate metric name "{name}"')
        else:
            names.add(name)
        if not isinstance(m.get("analyzer"), str) or not m.get("analyzer"):
            errors.append(f'{where}: "analyzer" must be a non-empty string')
        weight = m.get("weight")
        if not _is_number(weight) or weight < 0:
            errors.append(f'{where}: "weight" must be a number >= 0')
        else:
            total += float(weight)
        if "params" in m and not isinstance(m["params"], dict):
            errors.append(f'{where}: "params" must be an object')
    if metrics and abs(total - 1.0) > WEIGHT_TOLERANCE:
        errors.append(f"metric weights sum to {total:.4f}; they must sum to 1.00")

    for key in ("reference_solution", "statement_file", "generator", "usefulness_limitations_file"):
        if raw.get(key) and not (base / raw[key]).is_file() and not Path(raw[key]).is_file():
            errors.append(f'"{key}" points to a missing file: {raw[key]}')
    for plugin in raw.get("plugins", []):
        if not (base / plugin).is_file():
            errors.append(f'plugin file not found: {plugin}')

    sample = raw.get("sample")
    if sample is not None:
        listed = sample if isinstance(sample, list) else [sample]
        if not listed or any(not isinstance(s, dict) or not isinstance(s.get("input"), str)
                             or not isinstance(s.get("output", ""), str) for s in listed):
            errors.append('"sample" must be an object with a string "input" (and optional "output"), '
                          'or a non-empty list of such objects')

    compare = raw.get("compare") or {}
    if compare.get("mode", "lines") not in COMPARE_MODES:
        errors.append(f'"compare.mode" must be one of {", ".join(COMPARE_MODES)}')

    for key in raw:
        if key not in KNOWN_TOP_LEVEL:
            warnings.append(f'unknown top-level key "{key}" (ignored)')
    return errors, warnings
