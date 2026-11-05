"""The browser interface's HTTP API: rules (with the statement) + program, graded end to end.

Nothing is stored in advance: each job rebuilds problems/<problem>/ from the inputs and
generates the test cases (the LLM is faked; the C code really runs).
"""

import json
import re
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from autograder.llm import LLMClient, LLMReply
from autograder.web.server import WebApp, make_server

from .helpers import TempDirCase, toolchain_or_skip

STATEMENT = "Read two integers a and b (32-bit) and print their sum. The sum may not fit in 32 bits.\n"
RULES = {
    "problem": "add_two_integers",
    "statement": STATEMENT,
    "sample": [{"input": "7 5\n", "output": "12\n"}, {"input": "2147483647 1\n", "output": "2147483648\n"}],
    "input_spec": {"variables": [{"name": "a", "type": "int"}, {"name": "b", "type": "int"}], "layout": [["a", "b"]]},
    "metrics": [
        {"name": "correctness", "analyzer": "llm_tests", "weight": 0.6, "params": {}},
        {"name": "overflow", "analyzer": "edge_case_runner", "weight": 0.4, "params": {"cases": ["int_overflow"]}},
    ],
}
INT_ADD = '#include <stdio.h>\nint main(void){int a,b; if(scanf("%d %d",&a,&b)!=2) return 1; printf("%d\\n",a+b); return 0;}\n'


def fake_chat(self, system, user):
    tests = [{"name": "two negatives", "input": "-3 -4\n", "expected": "-7\n"},
             {"name": "zero and a negative", "input": "0 -9\n", "expected": "-9\n"}]
    return LLMReply(json.dumps({"tests": tests}), False, self.provider, self.model, "k")


class WebApiTest(TempDirCase):
    @classmethod
    def setUpClass(cls):
        cls.toolchain = toolchain_or_skip()

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(LLMClient, "chat", fake_chat)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = WebApp(self.tmp, self.toolchain, self.tmp / "cache", warm_models=False)  # an empty project folder
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        super().tearDown()

    def call(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def wait(self, job_id, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, job = self.call(f"/api/jobs/{job_id}")
            if job["state"] in ("done", "error"):
                return job
            time.sleep(0.2)
        self.fail("grading did not finish")

    def grade(self, rules, source=INT_ADD, filename="add.c"):
        _, started = self.call("/api/grade", {"rules": json.dumps(rules),
                                              "filename": filename, "source": source})
        job = self.wait(started["id"])
        self.assertEqual(job["state"], "done", job.get("error"))
        return job

    def test_index_has_no_workspace_page(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as resp:
            page = resp.read()
        self.assertIn(b"Class results", page)
        self.assertNotIn(b"Workspace", page)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/workspace", timeout=10)
        self.assertEqual(ctx.exception.code, 404)

    def test_static_assets_are_served_and_the_interface_is_light_only(self):
        base = f"http://127.0.0.1:{self.port}"
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            page = resp.read()
        self.assertIn(b"/static/style.css", page)
        self.assertNotIn(b"theme", page)  # no theme switch
        for name, expected in (("style.css", b"--royal"), ("app.js", b"gauge-arc")):
            with urllib.request.urlopen(f"{base}/static/{name}", timeout=10) as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read()
                self.assertIn(expected, body, name)
                self.assertNotIn(b'data-theme="dark"', body, name)

    def test_rules_check(self):
        _, ok = self.call("/api/rules/check", {"rules": json.dumps(RULES)})
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["folder"], "problems/add_two_integers")
        self.assertFalse(self.app.problems_dir.exists())  # checking writes nothing

        nameless = {k: v for k, v in RULES.items() if k != "statement"}  # a name, but no statement anywhere
        _, res = self.call("/api/rules/check", {"rules": json.dumps(nameless)})
        self.assertFalse(res["ok"])
        self.assertTrue(any("statement is missing" in e for e in res["errors"]))

        titled = dict(nameless, problem="Problem: Add Two Integers\n\n" + STATEMENT)  # the statement is "problem"
        _, res = self.call("/api/rules/check", {"rules": json.dumps(titled)})
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["problem"], res["folder"]), ("Add_Two_Integers", "problems/Add_Two_Integers"))

        bad = json.loads(json.dumps(RULES))
        bad["metrics"][0]["weight"] = 0.5
        _, res = self.call("/api/rules/check", {"rules": json.dumps(bad)})
        self.assertTrue(any("must sum to 1.00" in e for e in res["errors"]))

        outside = json.loads(json.dumps(RULES))
        outside["metrics"][0]["params"]["testset_dir"] = str(Path(__file__).resolve().parent)
        _, res = self.call("/api/rules/check", {"rules": json.dumps(outside)})
        self.assertTrue(any("outside the project folder" in e for e in res["errors"]))

        _, res = self.call("/api/rules/check", {"rules": "{oops"})
        self.assertTrue(any("invalid JSON" in e for e in res["errors"]))

    def test_grade_builds_the_problem_folder_from_the_inputs(self):
        job = self.grade(RULES, filename="../evil name.c")
        report = job["report"]
        folder = self.tmp / "problems" / "add_two_integers"
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["edge_case.json", "evil_name.c", "rules.json", "test_cases.json"])  # no reference.c
        self.assertEqual(json.loads((folder / "rules.json").read_text())["statement"], STATEMENT)
        self.assertEqual(report["metrics"][0]["evidence"]["tests_total"], 4)  # 2 samples + 2 designed
        self.assertEqual(report["counterexamples"][0]["expected"], "2147483648\n")  # from the sample
        prepared = next(e for e in job["events"] if e["type"] == "prepared")
        self.assertEqual((prepared["tests"], prepared["errors"]), (4, []))

        generated = json.loads((folder / "test_cases.json").read_text())
        self.assertEqual([c["name"] for c in generated][2:], ["two_negatives", "zero_and_a_negative"])

        stored = self.tmp / "reports" / "uploads" / "add_two_integers"
        self.assertEqual((stored / "evil_name.c").read_text(), INT_ADD)  # kept beside the report
        _, again = self.call(f"/api/report?file=uploads/add_two_integers/evil_name.json")
        self.assertEqual(again["source"], INT_ADD)

    def test_graded_programs_are_kept_as_the_originality_corpus(self):
        self.grade(RULES, source=INT_ADD, filename="alice.c")
        renamed = INT_ADD.replace("a+b", "b+a").replace("int a,b", "int b,a").replace("&a,&b", "&b,&a")
        self.grade(RULES, source=renamed, filename="bob.c")
        kept = self.tmp / "submissions" / "add_two_integers"
        self.assertEqual(sorted(p.name for p in kept.iterdir()), ["alice.c", "bob.c"])
        self.assertEqual((kept / "alice.c").read_text(), INT_ADD)
        self.grade(RULES, source=INT_ADD, filename="alice.c")   # the same name is a resubmission: replaced
        self.assertEqual(sorted(p.name for p in kept.iterdir()), ["alice.c", "bob.c"])

    def test_next_problem_replaces_the_previous_one(self):
        self.grade(RULES)
        other = dict(RULES, problem="sum_again")
        other["metrics"] = [dict(RULES["metrics"][0], analyzer="io_tests", params={}),  # no tests: generated
                            RULES["metrics"][1]]
        job = self.grade(other)
        self.assertEqual([p.name for p in (self.tmp / "problems").iterdir()], ["sum_again"])
        self.assertTrue((self.tmp / "problems" / "sum_again" / "tests" / "test_cases.json").is_file())
        self.assertEqual(job["report"]["metrics"][0]["evidence"]["tests_total"], 4)
        _, listing = self.call("/api/reports")  # past reports are kept
        self.assertEqual(sorted(g["name"] for g in listing["groups"]),
                         ["uploads/add_two_integers", "uploads/sum_again"])

    def test_cross_site_and_non_json_requests_are_refused(self):
        status, _ = self.call("/api/rules/check", {"rules": "{}"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/grade", data=b"x", method="POST",
                                     headers={"Content-Type": "text/plain"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 415)


if __name__ == "__main__":
    unittest.main()
