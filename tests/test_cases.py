"""The shipped cases.json must replay cleanly and its assertions must be honest.

The assertions inside cases.json are hand-written expectations.  This test only
checks that every one of them holds, and that the CLI contract
(``python -m solution --input ... --output ...``) works end to end.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from solution.__main__ import load_cases, run_case
from tests.support import PROJECT_ROOT, TempDirMixin

CASES = os.path.join(PROJECT_ROOT, "cases.json")


class CaseFileTests(TempDirMixin, unittest.TestCase):
    def test_every_case_passes_its_assertions(self) -> None:
        cases = load_cases(CASES)
        self.assertGreaterEqual(len(cases), 10)
        workdir = self.temp_dir()
        for case in cases:
            with self.subTest(case=case["id"]):
                document = run_case(case, workdir)
                failed = [a for a in document["assertions"] if not a["passed"]]
                self.assertEqual(failed, [], f"case {case['id']} failed: {failed}")
                self.assertTrue(document["passed"])
                self.assertTrue(case.get("why"), "every case explains itself")

    def test_case_ids_are_unique(self) -> None:
        ids = [c["id"] for c in load_cases(CASES)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_cli_writes_the_documented_result_file(self) -> None:
        out = os.path.join(self.temp_dir(), "result.json")
        env = dict(os.environ)
        env["PYTHONPATH"] = PROJECT_ROOT
        process = subprocess.run(
            [sys.executable, "-m", "solution", "--input", CASES, "--output", out],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        with open(out, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertTrue(payload["passed"])
        for case in payload["cases"]:
            self.assertTrue(case["passed"], case["case_id"])
            self.assertIn("final_state", case)
            self.assertIn("receipts", case)

    def test_replay_is_reproducible(self) -> None:
        """Two independent replays of the same file must be identical."""
        workdir = self.temp_dir()
        cases = load_cases(CASES)
        first = [run_case(case, os.path.join(workdir, "one")) for case in cases]
        second = [run_case(case, os.path.join(workdir, "two")) for case in cases]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
