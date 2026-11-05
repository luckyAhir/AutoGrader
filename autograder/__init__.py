"""AutoGrader: a rule-driven, reproducible evaluator for C programs.

The package is organised around one coordinator (``agent.GradingAgent``) that
reads an instructor's rules file, performs a cheap first pass over a
submission, dispatches one expert analyzer per metric and collates their
results into a single weighted grade plus student-facing feedback.
"""

__version__ = "1.0.0"

from .env import load_env  # noqa: E402

load_env()  # settings from .env (OLLAMA_MODEL, OLLAMA_HOST); real environment variables win
