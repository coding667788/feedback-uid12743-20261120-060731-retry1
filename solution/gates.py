"""Opt-in synchronisation gates for interleaving tests.

Set ``SOLUTION_GATE_DIR`` to a directory and ``SOLUTION_GATE_POINTS`` to a
comma separated list of point names.  At each listed point the process
announces itself as arriving and blocks until released by a controller.  This
lets tests pin down the exact order between two processes without sleeping or
racing.

Available points:

``plan_apply.revalidated``
    Inside the applying write transaction, after revalidation passed and
    before any object is written.
``plan_apply.written``
    Inside the applying write transaction, after every write, before commit.
``plan_create.planned``
    Inside the planning write transaction, after the target was computed.
``object_updated``
    Inside an object mutation transaction, after the row was written.
"""

from __future__ import annotations

import os

from .syncpoint import arrive


def gate(point: str) -> None:
    directory = os.environ.get("SOLUTION_GATE_DIR")
    if not directory:
        return
    points = {
        item.strip()
        for item in os.environ.get("SOLUTION_GATE_POINTS", "").split(",")
        if item.strip()
    }
    if point not in points:
        return
    name = os.environ.get("SOLUTION_GATE_NAME", "worker")
    arrive(directory, name)
