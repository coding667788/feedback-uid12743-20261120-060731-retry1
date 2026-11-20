"""Explicit crash points for the interruption tests.

The coordinator calls :func:`crash_point` at named places.  If the
environment variable ``SOLUTION_CRASH_AT`` equals the point name, the
process terminates itself with :func:`os._exit` *without* unwinding or
committing, which is exactly the "uncommitted interruption" the tests need.

Only the coordinator's own child process exits; nothing else is touched.
"""

from __future__ import annotations

import os
import sys


def crash_point(name: str) -> None:
    if os.environ.get("SOLUTION_CRASH_AT") != name:
        return
    sys.stdout.flush()
    sys.stderr.flush()
    # Hard exit: no ``finally`` blocks, no SQLite commit, no atexit handlers.
    os._exit(70)
