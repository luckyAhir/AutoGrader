"""Shared fixtures for the test-suite (plain unittest; pytest also works)."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADD_RULES = ROOT / "tests" / "fixtures" / "add_two_integers" / "rules.json"
SORT_RULES = ROOT / "tests" / "fixtures" / "sort_integers" / "rules.json"


def toolchain_or_skip():
    from autograder.toolchain import ToolchainError, detect

    try:
        return detect(os.environ.get("AUTOGRADER_CC"))
    except ToolchainError as exc:  # pragma: no cover
        raise unittest.SkipTest(f"no C compiler: {exc}")


def offline_rules(src: Path, tmp: Path) -> Path:
    """Copy a problem folder's rules with the LLM metric switched to heuristic mode."""
    problem_dir = tmp / src.parent.name
    shutil.copytree(src.parent, problem_dir)
    raw = json.loads(src.read_text(encoding="utf-8"))
    for m in raw["metrics"]:
        if m["analyzer"] == "llm_review":
            m["params"]["mode"] = "heuristic"
        if m["analyzer"] == "similarity_flag":
            m["params"]["corpus_dir"] = str(ROOT / "submissions" / src.parent.name)
    path = problem_dir / "rules.json"
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return path


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agtest_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_c(self, name: str, code: str) -> Path:
        path = self.tmp / name
        path.write_text(code, encoding="utf-8")
        return path
