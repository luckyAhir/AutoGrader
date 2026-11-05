"""Small deterministic helpers shared across the agent.

Everything that feeds into a grade must be deterministic: hashing uses SHA-256
(never Python's randomised ``hash()``), seeds are derived from content, and
rounding uses ROUND_HALF_UP on the decimal representation so that the printed
grade is the one a human would compute by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable, List, Optional

INT32_MIN = -(2 ** 31)
INT32_MAX = 2 ** 31 - 1
INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json(obj: Any) -> str:
    """JSON with sorted keys and no whitespace: identical objects -> identical text."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_seed(*parts: Any) -> int:
    """A 64-bit seed derived only from the given values."""
    return int(sha256_text("\x1f".join(str(p) for p in parts))[:16], 16)


def round_half_up(value: float, places: int = 1) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(repr(float(value))).quantize(quantum, rounding=ROUND_HALF_UP))


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_text(path: Path) -> str:
    return normalize_newlines(Path(path).read_bytes().decode("utf-8", errors="replace"))


_NAT_SPLIT = re.compile(r"(\d+)")


def natural_key(value: Any) -> List[Any]:
    """Sort key so that test2 < test10."""
    return [int(t) if t.isdigit() else t.lower() for t in _NAT_SPLIT.split(str(value))]


def shorten(text: Optional[str], limit: int = 300) -> Optional[str]:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [{len(text) - limit} more chars]"


def one_line(text: Optional[str], limit: int = 80) -> str:
    """Render (possibly multi-line) program I/O on a single display line."""
    if text is None:
        return "(none)"
    if not text:
        return "(nothing)"
    if not text.strip():
        return "(blank line)" if text == "\n" else repr(text)
    flat = text.replace("\r", "\\r").replace("\t", "\\t")
    stripped = flat.rstrip("\n")
    if "\n" in stripped:
        flat = stripped.replace("\n", " \\n ")
    else:
        flat = stripped
    if len(flat) > limit:
        flat = flat[:limit] + f"... ({len(text)} chars)"
    return flat


def display_path(path: Path) -> str:
    """Path relative to the working directory, with forward slashes (stable in reports)."""
    p = Path(path)
    try:
        rel = os.path.relpath(p.resolve(), Path.cwd().resolve())
    except ValueError:  # different drive on Windows
        return p.resolve().as_posix()
    return Path(rel).as_posix()


def hash_files(files: Iterable[Path], root: Optional[Path] = None) -> str:
    """Content hash over a set of files (line endings normalised, order independent)."""
    h = hashlib.sha256()
    entries = []
    for f in files:
        f = Path(f)
        if not f.is_file():
            continue
        try:
            name = f.resolve().relative_to(root.resolve()).as_posix() if root else f.name
        except ValueError:
            name = f.name
        entries.append((name, f))
    for name, f in sorted(entries, key=lambda e: e[0]):
        h.update(name.encode("utf-8") + b"\0")
        h.update(f.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return "sha256:" + h.hexdigest()


def fmt_num(value: float) -> str:
    """12.0 -> '12', 12.5 -> '12.5'."""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.1f}"


def to_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value > INT32_MAX else value


def to_int64(value: int) -> int:
    value &= 0xFFFFFFFFFFFFFFFF
    return value - (1 << 64) if value > INT64_MAX else value
