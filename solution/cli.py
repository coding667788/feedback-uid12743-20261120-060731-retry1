"""Command line front end.

Usage::

    python -m solution --input cases.json --output result.json

The input file describes one or more *cases*.  Each case owns a database file
inside the work directory and a list of requests.  See README.md for the full
schema and for the error codes.

Replay contract: the run is **fresh** — every case database named by the
input is deleted (with its ``-wal``/``-shm`` companions) before that case
starts.  Running the same input twice, even against the same work directory,
produces identical output; that is what makes ``cases.json`` a regression
oracle rather than a one-shot script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .core import Coordinator


def _reset_database(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()


def run_case(case: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    name = case.get("name")
    db_name = case.get("db") or f"{name or 'case'}.sqlite3"
    if Path(db_name).name != db_name:
        raise ValueError(f"case {name!r}: 'db' must be a plain file name")
    db_path = (base_dir / db_name).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _reset_database(db_path)

    responses: list[dict[str, Any]] = []
    coordinator = Coordinator(str(db_path))
    try:
        for request in case["requests"]:
            # ``handle`` turns every rejection into a response dictionary; an
            # invalid envelope is still a response, never an exception.
            responses.append(coordinator.handle(request))
    finally:
        coordinator.close()
    return {
        "name": name,
        "db": db_name,
        "count": len(responses),
        "responses": responses,
    }


def run_input(payload: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("input must be a JSON object")
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("input must contain a 'cases' list")
    results = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"case #{index} must be an object")
        if not isinstance(case.get("requests"), list):
            raise ValueError(f"case #{index} must contain a 'requests' list")
        results.append(run_case(case, base_dir))
    return {"ok": True, "case_count": len(results), "cases": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m solution")
    parser.add_argument("--input", required=True, help="path to cases.json")
    parser.add_argument("--output", required=True, help="path to write result.json")
    parser.add_argument(
        "--workdir",
        default=None,
        help="directory for case database files (default: alongside the output)",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input).resolve()
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read input: {exc}", file=sys.stderr)
        return 2

    output_path = Path(args.output).resolve()
    base_dir = (
        Path(args.workdir).resolve() if args.workdir else output_path.parent / "work"
    )
    base_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = run_input(payload, base_dir)
    except ValueError as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
