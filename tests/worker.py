"""Subprocess worker used by the tests.

Invoked as::

    python -m tests.worker <db_path> <requests.json> <results.json>

``requests.json`` is a list of steps:

* a request object — executed through a fresh ``Coordinator``;
* ``{"crash": "<point>"}`` — arms ``SOLUTION_CRASH_AT`` for all *following*
  steps, so the next request that reaches that point kills this process with
  ``os._exit``.

Only processes started this way are ever terminated by the tests.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solution.core import Coordinator  # noqa: E402


def main(argv: list[str]) -> int:
    db_path, requests_path, results_path = argv[1], argv[2], argv[3]
    steps = json.loads(Path(requests_path).read_text(encoding="utf-8"))
    with open(results_path, "a", encoding="utf-8") as results_file:
        coordinator = Coordinator(db_path)
        try:
            for step in steps:
                if isinstance(step, dict) and "crash" in step:
                    os.environ["SOLUTION_CRASH_AT"] = step["crash"]
                    continue
                response = coordinator.handle(step)
                results_file.write(json.dumps(response, sort_keys=True) + "\n")
                results_file.flush()
                os.fsync(results_file.fileno())
        finally:
            coordinator.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
