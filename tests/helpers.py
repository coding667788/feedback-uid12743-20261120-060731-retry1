"""Shared test helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from solution.core import Coordinator  # noqa: E402


class CoordinatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sol-test-")
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "state.sqlite3"
        self.coordinator = Coordinator(str(self.db_path))
        self._seq = 0

    def tearDown(self) -> None:
        self.coordinator.close()
        # Child processes and SQLite handles can briefly hold files open on
        # Windows; retry the cleanup instead of failing the test.
        for _ in range(50):
            try:
                self._tmp.cleanup()
                break
            except PermissionError:
                time.sleep(0.1)
        else:
            self._tmp.cleanup()

    # -- request sugar -------------------------------------------------------

    def call(self, **fields) -> dict:
        self._seq += 1
        request = {
            "tenant": fields.pop("tenant", "t"),
            "request_id": fields.pop("request_id", f"req-{self._seq:04d}"),
            "op": fields.pop("op"),
        }
        request.update(fields)
        return self.coordinator.handle(request)

    def ok(self, **fields) -> dict:
        response = self.call(**fields)
        self.assertTrue(
            response.get("ok"), f"expected success, got {json.dumps(response)}"
        )
        return response

    def err(self, code: str, **fields) -> dict:
        response = self.call(**fields)
        self.assertFalse(response.get("ok"), f"expected failure, got {response}")
        self.assertEqual(response.get("code"), code, response)
        return response

    def reopen(self) -> None:
        self.coordinator.close()
        self.coordinator = Coordinator(str(self.db_path))

    def get_object(self, object_id: str, at: str, tenant: str = "t") -> dict:
        return self.ok(op="object.get", object_id=object_id, at=at, tenant=tenant)[
            "object"
        ]

    def export(self, at: str, tenant: str = "t") -> dict:
        return self.ok(op="state.export", at=at, tenant=tenant)


class Worker:
    """A *real* child process driving the coordinator; only tests spawn these."""

    def __init__(self, db_path: Path, workdir: Path, name: str = "worker"):
        self.db_path = Path(db_path)
        self.workdir = Path(workdir)
        self.name = name
        self.requests_path = self.workdir / f"{name}.requests.json"
        self.results_path = self.workdir / f"{name}.results.jsonl"
        self.log_path = self.workdir / f"{name}.log"

    def spawn(self, steps: list[dict], env: dict[str, str] | None = None) -> subprocess.Popen:
        self.requests_path.write_text(json.dumps(steps), encoding="utf-8")
        full_env = dict(os.environ)
        full_env.pop("SOLUTION_CRASH_AT", None)
        full_env["PYTHONPATH"] = str(ROOT) + os.pathsep + full_env.get("PYTHONPATH", "")
        if env:
            full_env.update(env)
        # Redirect to an anonymous temporary file: no handle to leak, so the
        # temporary directory can be removed on Windows even if a process is
        # killed.  The text is copied into ``self.log_path`` after exit.
        log = tempfile.TemporaryFile(mode="w+b")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.worker",
                str(self.db_path),
                str(self.requests_path),
                str(self.results_path),
            ],
            cwd=str(ROOT),
            env=full_env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        process._log_handle = log  # type: ignore[attr-defined]
        return process

    def collect(self, process: subprocess.Popen) -> None:
        """Close the redirected log and copy it where ``log()`` can read it."""

        handle = getattr(process, "_log_handle", None)
        if handle is None:
            return
        try:
            handle.seek(0)
            data = handle.read()
        finally:
            handle.close()
            process._log_handle = None  # type: ignore[attr-defined]
        self.log_path.write_bytes(data)

    def run(self, steps: list[dict], env: dict[str, str] | None = None) -> tuple[int, list[dict]]:
        process = self.spawn(steps, env)
        try:
            code = process.wait(timeout=120)
        finally:
            self.collect(process)
        return code, self.responses()

    def responses(self) -> list[dict]:
        if not self.results_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.results_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8") if self.log_path.exists() else ""
