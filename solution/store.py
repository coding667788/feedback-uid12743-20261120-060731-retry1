"""SQLite persistence.

One connection per process.  Every mutating request runs inside
``BEGIN IMMEDIATE`` so writers serialise across processes (WAL mode and a
busy timeout handle the wait).  Object mutations and the request receipt are
committed together, which is what makes an interrupted apply leave no trace.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS objects (
        tenant         TEXT NOT NULL,
        object_id      TEXT NOT NULL,
        generation     INTEGER NOT NULL,
        object_version INTEGER NOT NULL,
        deleted        INTEGER NOT NULL,
        baseline       TEXT NOT NULL,
        attributes     TEXT NOT NULL,
        materialized   TEXT NOT NULL,
        created_seq    INTEGER NOT NULL,
        PRIMARY KEY (tenant, object_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS leases (
        tenant         TEXT NOT NULL,
        lease_id       TEXT NOT NULL,
        object_id      TEXT NOT NULL,
        generation     INTEGER NOT NULL,
        permissions    TEXT NOT NULL,
        attribute_refs TEXT NOT NULL,
        start_us       INTEGER NOT NULL,
        end_us         INTEGER NOT NULL,
        version        INTEGER NOT NULL,
        revoked        INTEGER NOT NULL,
        created_seq    INTEGER NOT NULL,
        PRIMARY KEY (tenant, lease_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dependencies (
        tenant          TEXT NOT NULL,
        from_object     TEXT NOT NULL,
        from_permission TEXT NOT NULL,
        to_object       TEXT NOT NULL,
        to_permission   TEXT NOT NULL,
        version         INTEGER NOT NULL,
        removed         INTEGER NOT NULL,
        created_seq     INTEGER NOT NULL,
        PRIMARY KEY (tenant, from_object, from_permission, to_object, to_permission)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plans (
        plan_id       TEXT PRIMARY KEY,
        tenant        TEXT NOT NULL,
        planned_at_us INTEGER NOT NULL,
        scope         TEXT NOT NULL,
        basis         TEXT NOT NULL,
        result        TEXT NOT NULL,
        created_seq   INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_applications (
        plan_id          TEXT PRIMARY KEY,
        tenant           TEXT NOT NULL,
        apply_request_id TEXT NOT NULL,
        applied_at_us    INTEGER NOT NULL,
        receipt          TEXT NOT NULL,
        created_seq      INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS apply_events (
        plan_id      TEXT NOT NULL,
        seq          INTEGER NOT NULL,
        action       TEXT NOT NULL,
        object_id    TEXT NOT NULL,
        permission   TEXT NOT NULL,
        applied_at_us INTEGER NOT NULL,
        PRIMARY KEY (plan_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS receipts (
        tenant       TEXT NOT NULL,
        request_id   TEXT NOT NULL,
        op           TEXT NOT NULL,
        params_hash  TEXT NOT NULL,
        response     TEXT NOT NULL,
        created_seq  INTEGER NOT NULL,
        PRIMARY KEY (tenant, request_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_leases_object ON leases (tenant, object_id)",
)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=60.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 60000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = FULL")
        self._initialise()

    def _initialise(self) -> None:
        # Opening a database must not take the write lock: two processes may
        # legitimately open the same file concurrently, and only mutations
        # need to serialise.  Read the schema first and write only if absent.
        present = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'receipts'"
        ).fetchone()
        if present is not None:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in SCHEMA_STATEMENTS:
                self.conn.execute(statement)
            self.conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('plan_seq', '0')"
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('event_seq', '0')"
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    @contextmanager
    def savepoint(self, name: str) -> Iterator[sqlite3.Connection]:
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute(f"ROLLBACK TO {name}")
            self.conn.execute(f"RELEASE {name}")
            raise
        else:
            self.conn.execute(f"RELEASE {name}")

    def next_meta(self, key: str) -> int:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise sqlite3.DatabaseError(f"missing meta counter {key!r}")
        value = int(row["value"]) + 1
        self.conn.execute("UPDATE meta SET value = ? WHERE key = ?", (str(value), key))
        return value

    def next_event_seq(self) -> int:
        return self.next_meta("event_seq")

    def next_plan_id(self) -> str:
        return f"p{self.next_meta('plan_seq'):06d}"
