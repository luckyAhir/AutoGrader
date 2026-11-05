"""Settings from a ``.env`` file (standard library only).

``.env`` lives in the project folder (next to ``autograder/``); one in the current folder is read too. Lines are
``NAME=value`` (``#`` starts a comment, quotes around the value are removed). A variable that is already set in
the real environment is NOT overridden, so ``OLLAMA_MODEL=... python -m autograder ...`` still wins.

Variables used by the project
    OLLAMA_MODEL   the Ollama model for the whole project (rules' ``model`` values are ignored for Ollama)
    OLLAMA_HOST    where Ollama listens (default http://127.0.0.1:11434)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, Optional

DEFAULT_MODEL = "qwen2.5-coder:3b"
_loaded = False


def parse_env(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        if name:
            out[name] = value
    return out


def load_env(paths: Optional[Iterable[Path]] = None, force: bool = False) -> None:
    """Read the ``.env`` files into ``os.environ`` (once; existing variables are kept)."""
    global _loaded
    if _loaded and not force:
        return
    _loaded = True
    candidates = list(paths) if paths is not None else [Path(__file__).resolve().parent.parent / ".env",
                                                        Path.cwd() / ".env"]
    for path in candidates:
        try:
            values = parse_env(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        for name, value in values.items():
            if value != "" and name not in os.environ:
                os.environ[name] = value


def ollama_model(fallback: Optional[str] = None) -> str:
    """The model set in ``.env`` / ``OLLAMA_MODEL``, else ``fallback``, else the project default."""
    return os.environ.get("OLLAMA_MODEL", "").strip() or fallback or DEFAULT_MODEL
