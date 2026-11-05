"""Copy detection by winnowed k-gram fingerprints (the MOSS algorithm).

1. Normalise the code: drop comments and whitespace, replace identifiers and
   literals by placeholders (library names are kept). Renaming variables or
   rewriting comments therefore changes nothing.
2. Hash every k consecutive tokens; in every window of w hashes keep the
   minimum (winnowing). Any shared run of at least k + w - 1 tokens is
   guaranteed to produce a shared fingerprint.
3. Remove "boilerplate" fingerprints: those in the instructor's reference
   solution and those present in a large share of the corpus. Every correct
   solution of a tiny problem looks alike, and that must not count as copying.
4. similarity(A, B) = |F_A ∩ F_B| / min(|F_A|, |F_B|) (overlap coefficient),
   high when one program is contained in the other.

Matched fingerprints are mapped back to line ranges for the instructor.
"""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .csource import CSource, normalized_tokens


@dataclass
class Fingerprints:
    tokens: List[str]
    lines: List[int]
    hashes: Dict[int, List[int]] = field(default_factory=dict)  # hash -> token positions

    @property
    def keys(self) -> Set[int]:
        return set(self.hashes)


def fingerprint(src: CSource, k: int = 5, w: int = 4) -> Fingerprints:
    pairs = normalized_tokens(src)
    tokens = [p[0] for p in pairs]
    lines = [p[1] for p in pairs]
    fp = Fingerprints(tokens, lines)
    if len(tokens) < k:
        return fp
    grams = []
    for i in range(len(tokens) - k + 1):
        digest = hashlib.sha1("\x1f".join(tokens[i:i + k]).encode()).digest()
        grams.append((int.from_bytes(digest[:8], "big"), i))
    if len(grams) <= w:
        h, pos = min(grams)
        fp.hashes.setdefault(h, []).append(pos)
        return fp
    selected = set()
    for start in range(len(grams) - w + 1):
        window = grams[start:start + w]
        best = min(window, key=lambda g: (g[0], -g[1]))  # rightmost minimum
        selected.add(best)
    for h, pos in sorted(selected, key=lambda g: g[1]):
        fp.hashes.setdefault(h, []).append(pos)
    return fp


def overlap(a: Set[int], b: Set[int]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def sequence_ratio(a: Fingerprints, b: Fingerprints) -> float:
    return difflib.SequenceMatcher(None, a.tokens, b.tokens, autojunk=False).ratio()


def matched_line_ranges(fp: Fingerprints, shared: Iterable[int], k: int) -> List[Tuple[int, int]]:
    lines: Set[int] = set()
    for h in shared:
        for pos in fp.hashes.get(h, []):
            lines.update(fp.lines[pos:pos + k])
    return _ranges(sorted(lines))


def _ranges(lines: List[int]) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for ln in lines:
        if out and ln <= out[-1][1] + 2:
            out[-1] = (out[-1][0], ln)
        else:
            out.append((ln, ln))
    return out


def boilerplate(base: Optional[Fingerprints], corpus: List[Fingerprints], common_fraction: float,
                min_corpus: int = 4) -> Set[int]:
    """Fingerprints that should not count as evidence of copying."""
    common: Set[int] = set(base.keys) if base else set()
    if len(corpus) >= min_corpus:
        counts: Dict[int, int] = {}
        for fp in corpus:
            for h in fp.keys:
                counts[h] = counts.get(h, 0) + 1
        limit = common_fraction * len(corpus)
        common.update(h for h, c in counts.items() if c > limit)
    return common
