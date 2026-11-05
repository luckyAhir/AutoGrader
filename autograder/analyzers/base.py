"""Analyzer interface and registry.

An analyzer measures exactly one metric. It receives the shared
``AnalysisContext`` and its own ``params`` slice from the rules, and returns an
``AnalyzerResult``: a score in [0, 1], a short verdict for the summary table,
feedback text, raw evidence, and optional suggestions, failures
(counterexamples) and flags.

New analyzers can be added without touching the agent. Drop a Python file
next to the rules, list it under ``"plugins"``, and decorate the class with
``@register``.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

# status values
OK = "ok"
SKIPPED_SUBMISSION = "skipped_submission"    # the submission's fault -> scores 0
SKIPPED_ENVIRONMENT = "skipped_environment"  # tool/asset missing -> weight redistributed
ERROR = "error"                              # analyzer crashed -> weight redistributed, flagged


@dataclass
class AnalyzerResult:
    score: float
    summary: str
    feedback: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    suggestions: List[str] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)
    flags: List[Dict[str, Any]] = field(default_factory=list)
    status: str = OK

    @classmethod
    def skipped(cls, status: str, reason: str, **evidence: Any) -> "AnalyzerResult":
        return cls(score=0.0, summary=reason, feedback=reason, evidence=dict(evidence), status=status)


class Analyzer:
    name: str = ""
    description: str = ""
    needs_binary: bool = False  # execution-based: skipped when the first pass fails

    def prepare(self, agent, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """One-time setup per grading session, before any submission is graded.

        Runs before the agent hashes the problem's assets, so files written here
        (e.g. generated tests) become part of ``assets_hash``. Whatever it returns
        reaches ``run`` as ``params["_prepared"]``.
        """
        return None

    def run(self, ctx, params: Dict[str, Any]) -> AnalyzerResult:  # pragma: no cover - interface
        raise NotImplementedError


_REGISTRY: Dict[str, Type[Analyzer]] = {}


def register(cls: Type[Analyzer]) -> Type[Analyzer]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} has no name")
    _REGISTRY[cls.name] = cls
    return cls


def get_analyzer(name: str) -> Analyzer:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise KeyError(f"unknown analyzer {name!r}; available: {', '.join(sorted(_REGISTRY))}") from None


def available_analyzers() -> Dict[str, Type[Analyzer]]:
    return dict(sorted(_REGISTRY.items()))


def load_plugin(path: Path) -> None:
    path = Path(path)
    module_name = "autograder_plugin_" + "".join(c if c.isalnum() else "_" for c in path.stem)
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load analyzer plugin {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
