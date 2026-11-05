"""OLLAMA_MODEL from .env switches the model for the whole project."""

import os
import unittest
from unittest import mock

from autograder import env
from autograder.llm import LLMClient

from .helpers import TempDirCase


class EnvFileTest(TempDirCase):
    def test_parse(self):
        values = env.parse_env('# c\nOLLAMA_MODEL = "qwen2.5-coder:7b"  \nexport A=1 # note\n\nbad line\nB=\n')
        self.assertEqual(values, {"OLLAMA_MODEL": "qwen2.5-coder:7b", "A": "1", "B": ""})

    def test_file_sets_variables_but_real_environment_wins(self):
        path = self.tmp / ".env"
        path.write_text("OLLAMA_MODEL=qwen2.5-coder:7b\nOTHER_X=1\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"OTHER_X": "keep"}, clear=False):
            os.environ.pop("OLLAMA_MODEL", None)
            env.load_env([path], force=True)
            self.assertEqual(os.environ["OLLAMA_MODEL"], "qwen2.5-coder:7b")
            self.assertEqual(os.environ["OTHER_X"], "keep")
            os.environ.pop("OLLAMA_MODEL")

    def test_one_setting_changes_the_model_everywhere(self):
        rules_params = {"provider": "ollama", "model": "qwen2.5-coder:3b"}
        with mock.patch.dict(os.environ, {"OLLAMA_MODEL": "qwen2.5-coder:7b"}):
            self.assertEqual(LLMClient.from_params(rules_params, cache_dir=None).model, "qwen2.5-coder:7b")
            self.assertEqual(LLMClient.from_params({}, cache_dir=None).model, "qwen2.5-coder:7b")
        with mock.patch.dict(os.environ, {"OLLAMA_MODEL": "qwen2.5-coder:3b"}):
            self.assertEqual(LLMClient.from_params({"model": "qwen2.5-coder:7b"}, cache_dir=None).model,
                             "qwen2.5-coder:3b")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OLLAMA_MODEL", None)   # nothing set: the rules decide, as before
            self.assertEqual(LLMClient.from_params({"model": "qwen2.5-coder:7b"}, cache_dir=None).model,
                             "qwen2.5-coder:7b")

    def test_other_providers_are_not_affected(self):
        with mock.patch.dict(os.environ, {"OLLAMA_MODEL": "qwen2.5-coder:3b"}):
            self.assertEqual(LLMClient.from_params({"provider": "groq", "model": "llama-3"}, cache_dir=None).model,
                             "llama-3")


if __name__ == "__main__":
    unittest.main()
