"""correctness on tests generated at runtime: ``io_tests`` with ``test_cases.json`` as its test set.

``io_tests`` already generates a test set whenever its folder is empty; this name makes that intent explicit
in the rules and keeps generated tests apart from hand-written ``tests/``.
"""

from __future__ import annotations

from .. import testgen
from .base import register
from .io_tests import IOTests


@register
class LLMTests(IOTests):
    name = "llm_tests"
    description = "Correctness on tests an LLM designs from the statement and samples (no reference solution)"
    default_dir = testgen.DEFAULT_DIR
