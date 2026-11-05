"""originality: graded programs kept in submissions/<problem>/ are the corpus, and the later copy is named."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from autograder.agent import GradingAgent
from autograder.analyzers.similarity_flag import corpus_name
from autograder.llm import LLMClient, LLMError
from autograder.rules import load_rules

from .helpers import ADD_RULES, ROOT, TempDirCase, offline_rules, toolchain_or_skip

ADD = ROOT / "submissions" / "add_two_integers"


class OriginalityCorpusTest(TempDirCase):
    @classmethod
    def setUpClass(cls):
        cls.toolchain = toolchain_or_skip()

    def setUp(self):
        super().setUp()

        def no_llm(client, system, user):
            raise LLMError("cannot reach the model")

        patcher = mock.patch.object(LLMClient, "chat", no_llm)  # only the originality metric matters here
        patcher.start()
        self.addCleanup(patcher.stop)
        self.corpus = self.tmp / "kept"
        raw_path = offline_rules(ADD_RULES, self.tmp)
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        self.corpus_param = None
        for m in raw["metrics"]:
            if m["analyzer"] == "similarity_flag":
                self.corpus_param = m["params"].pop("corpus_dir")  # the rules name no corpus
                m["params"]["flag_threshold"] = 0.85
        raw_path.write_text(json.dumps(raw), encoding="utf-8")
        self.rules_path = raw_path
        self.problem = raw["problem"]
        (self.corpus / corpus_name(self.problem)).mkdir(parents=True)

    def stored(self, name):
        return self.corpus / corpus_name(self.problem) / name

    def grade(self, path, kept=True):
        with GradingAgent(load_rules(self.rules_path), self.toolchain, cache_dir=self.tmp / "cache",
                          submissions_dir=self.corpus if kept else None) as agent:
            return agent.grade(path).report

    def originality(self, report):
        return next(m for m in report["metrics"] if m["name"] == "originality")

    def test_nothing_changes_without_a_submissions_dir(self):
        shutil.copy(ADD / "add_robust.c", self.stored("alice.c"))
        report = self.grade(ADD / "add_copied.c", kept=False)
        self.assertEqual(self.originality(report)["evidence"]["corpus_files"], 0)
        self.assertEqual([f for f in report["flags"] if f["type"] == "suspected_plagiarism"], [])

    def test_the_later_program_is_named_as_the_copy(self):
        shutil.copy(ADD / "add_robust.c", self.stored("alice.c"))
        copy = self.tmp / "bob.c"
        shutil.copy(ADD / "add_copied.c", copy)
        os.utime(self.stored("alice.c"), (1_000_000, 1_000_000))   # alice submitted long before bob
        flag = next(f for f in self.grade(copy)["flags"] if f["type"] == "suspected_plagiarism")
        self.assertEqual((flag["peer"], flag["order"], flag["likely_copied_from"]), ("alice.c", "earlier", "alice.c"))
        self.assertIn("submitted earlier", flag["message"])

    def test_an_earlier_graded_program_that_was_copied_later_is_told_so(self):
        shutil.copy(ADD / "add_copied.c", self.stored("bob.c"))
        original = self.tmp / "alice.c"
        shutil.copy(ADD / "add_robust.c", original)
        os.utime(original, (1_000_000, 1_000_000))
        flag = next(f for f in self.grade(original)["flags"] if f["type"] == "suspected_plagiarism")
        self.assertEqual((flag["peer"], flag["order"], flag["likely_copied_from"]), ("bob.c", "later", "alice.c"))

    def test_the_same_file_name_is_the_same_student(self):
        shutil.copy(ADD / "add_robust.c", self.stored("alice.c"))
        again = self.tmp / "alice.c"
        shutil.copy(ADD / "add_robust.c", again)   # a resubmission, not a copy
        report = self.grade(again)
        self.assertEqual(self.originality(report)["evidence"]["corpus_files"], 0)
        self.assertEqual([f for f in report["flags"] if f["type"] == "suspected_plagiarism"], [])

    def test_explicit_corpus_dir_in_the_rules_still_wins(self):
        raw = json.loads(self.rules_path.read_text(encoding="utf-8"))
        for m in raw["metrics"]:
            if m["analyzer"] == "similarity_flag":
                m["params"]["corpus_dir"] = self.corpus_param
        self.rules_path.write_text(json.dumps(raw), encoding="utf-8")
        report = self.grade(ADD / "add_copied.c")
        self.assertEqual(next(f for f in report["flags"] if f["type"] == "suspected_plagiarism")["peer"],
                         "add_robust.c")

    def test_originality_command_lists_who_copied_from_whom(self):
        folder = self.tmp / "folder"
        folder.mkdir()
        shutil.copy(ADD / "add_robust.c", folder / "alice.c")
        shutil.copy(ADD / "add_copied.c", folder / "bob.c")
        os.utime(folder / "alice.c", (1_000_000, 1_000_000))
        out = subprocess.run([sys.executable, "-m", "autograder", "originality", str(folder)], cwd=str(ROOT),
                             capture_output=True, text=True).stdout
        self.assertIn("bob.c  <-  alice.c  (bob.c is the likely copy)", out)


if __name__ == "__main__":
    unittest.main()
