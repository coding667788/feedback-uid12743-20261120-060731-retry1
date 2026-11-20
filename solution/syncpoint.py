"""Deterministic synchronisation points used by the multi-process tests.

A *barrier* is a directory containing one file per participant plus an
optional ``order.log``.  Arriving creates ``<name>.arrived`` and appends
``<name>`` to ``order.log`` (one ``flock``-protected append per participant,
so the log is a faithful arrival order).  Releasing is a separate file
``<name>.released`` written by the controller.

Only the tests use this module; the coordinator itself never blocks on it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def _fsync_write(path: Path, text: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def arrive(barrier_dir: str | Path, name: str, timeout: float = 60.0) -> str:
    """Record arrival of ``name`` and wait for its release.

    Returns the arrival order as recorded in ``order.log`` (newline separated
    names in arrival order, excluding the empty trailing entry).
    """

    directory = Path(barrier_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _fsync_write(directory / "order.log", name + "\n")
    (directory / f"{name}.arrived").write_text("1", encoding="utf-8")
    deadline = time.monotonic() + timeout
    released = directory / f"{name}.released"
    while not released.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"barrier release timed out for {name}")
        time.sleep(0.002)
    return read_order(directory)


def release(barrier_dir: str | Path, *names: str) -> None:
    directory = Path(barrier_dir)
    for name in names:
        (directory / f"{name}.released").write_text("1", encoding="utf-8")


def wait_arrived(barrier_dir: str | Path, *names: str, timeout: float = 60.0) -> None:
    directory = Path(barrier_dir)
    deadline = time.monotonic() + timeout
    targets = [directory / f"{name}.arrived" for name in names]
    while not all(target.exists() for target in targets):
        if time.monotonic() > deadline:
            missing = [t.name for t in targets if not t.exists()]
            raise TimeoutError(f"barrier arrival timed out; missing {missing}")
        time.sleep(0.002)


def read_order(barrier_dir: str | Path) -> list[str]:
    path = Path(barrier_dir) / "order.log"
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]
