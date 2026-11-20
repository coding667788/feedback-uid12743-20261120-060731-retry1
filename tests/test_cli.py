"""End-to-end tests of ``python -m solution`` and the shipped cases file."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.helpers import ROOT

CASES = ROOT / "cases.json"


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="sol-cli-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(
        self, input_path: Path, output_path: Path, workdir: Path
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "solution",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--workdir",
                str(workdir),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )

    def test_cases_file_is_replayable(self):
        self.assertTrue(CASES.exists(), "cases.json is part of the deliverable")
        cases = json.loads(CASES.read_text(encoding="utf-8"))
        self.assertIn("cases", cases)
        self.assertGreater(len(cases["cases"]), 0)

        first = self.tmp / "first"
        second = self.tmp / "second"
        first_result = self.tmp / "first.json"
        second_result = self.tmp / "second.json"
        proc = self.run_cli(CASES, first_result, first)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_cli(CASES, second_result, second)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        payload_a = json.loads(first_result.read_text(encoding="utf-8"))
        payload_b = json.loads(second_result.read_text(encoding="utf-8"))
        self.assertTrue(payload_a["ok"])
        self.assertEqual(payload_a["case_count"], len(cases["cases"]))
        # Replaying from scratch on a clean workdir is byte-for-byte identical.
        self.assertEqual(payload_a, payload_b)

    def test_each_case_declares_expected_assertions(self):
        cases = json.loads(CASES.read_text(encoding="utf-8"))
        for case in cases["cases"]:
            with self.subTest(case=case.get("name")):
                self.assertIn("name", case)
                self.assertIn("requests", case)
                self.assertIn("expect", case)
                self.assertEqual(
                    len(case["expect"]),
                    len(case["requests"]),
                    "one expectation per request keeps the file reviewable",
                )

    def test_expectations_match_run(self):
        cases = json.loads(CASES.read_text(encoding="utf-8"))
        workdir = self.tmp / "work"
        result_path = self.tmp / "result.json"
        proc = self.run_cli(CASES, result_path, workdir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        by_name = {case["name"]: case for case in payload["cases"]}
        for case in cases["cases"]:
            produced = by_name[case["name"]]["responses"]
            for index, expected in enumerate(case["expect"]):
                with self.subTest(case=case["name"], step=index):
                    actual = produced[index]
                    self.assertEqual(actual["ok"], expected["ok"], actual)
                    if not expected["ok"]:
                        self.assertEqual(actual["code"], expected["code"], actual)
                    for key, value in expected.get("equals", {}).items():
                        self.assertEqual(
                            _dig(actual, key), value, f"{key} in {actual}"
                        )
                    for key, value in expected.get("contains", {}).items():
                        self.assertIn(value, _dig(actual, key), actual)

    def test_missing_input_file_returns_nonzero(self):
        proc = self.run_cli(
            self.tmp / "nope.json", self.tmp / "out.json", self.tmp
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cannot read input", proc.stderr)

    def test_malformed_input_shape_returns_nonzero(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"not_cases": []}), encoding="utf-8")
        proc = self.run_cli(bad, self.tmp / "out.json", self.tmp)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid input", proc.stderr)


def _dig(payload, dotted: str):
    value = payload
    for part in dotted.split("."):
        if isinstance(value, list):
            value = value[int(part)]
        else:
            value = value[part]
    return value


if __name__ == "__main__":
    unittest.main()
