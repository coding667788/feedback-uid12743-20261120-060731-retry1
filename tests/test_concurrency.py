"""Two-process interleaving and crash tests.

Every child process is one this test spawned itself, and only those are
terminated.  Ordering between the two processes is pinned with sync points
(``solution.syncpoint`` via ``SOLUTION_GATE_*``), never with sleeps.

Because the coordinator takes ``BEGIN IMMEDIATE`` for every mutation, a second
writer blocks *before* its revalidation.  The deterministic interleaving is
therefore: process A revalidates inside its write transaction, process B
parks before opening its transaction, A commits, B proceeds and observes the
committed result.
"""

from __future__ import annotations

import unittest

from tests.helpers import CoordinatorTestCase, Worker
from solution.syncpoint import release, wait_arrived

T0 = "2026-07-01T00:00:00Z"
T1 = "2026-07-01T01:00:00Z"
T2 = "2026-07-01T02:00:00Z"


class TwoProcessTests(CoordinatorTestCase):
    def setUp(self):
        super().setUp()
        self.barrier = str(self.tmp / "barrier")
        (self.tmp / "barrier").mkdir()

    # -- helpers -------------------------------------------------------------

    def seed_chain(self):
        self.ok(op="object.create", object_id="base", baseline=["base.perm"], at=T0)
        self.ok(op="object.create", object_id="app", baseline=["app.perm"], at=T0)
        self.ok(
            op="dependency.add",
            from_object="app",
            from_permission="app.perm",
            to_object="base",
            to_permission="base.perm",
            at=T0,
        )

    def plan_for_removal(self) -> dict:
        self.ok(op="object.update", object_id="base", baseline=[], at=T1)
        return self.ok(op="plan.create", planned_at=T1, objects=["base"])

    def spawn(self, name: str, steps: list[dict], **env) -> tuple[Worker, object]:
        worker = Worker(self.db_path, self.tmp, name)
        base_env = {"SOLUTION_GATE_DIR": self.barrier, "SOLUTION_GATE_NAME": name}
        base_env.update(env)
        process = worker.spawn(steps, env=base_env)
        self.addCleanup(self._reap, process)
        return worker, process

    @staticmethod
    def _reap(process) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=60)
        finally:
            handle = getattr(process, "_log_handle", None)
            if handle is not None and not handle.closed:
                handle.close()
    # -- deterministic interleaving of two applies ---------------------------

    def test_interleaved_applies_second_sees_committed_plan(self):
        self.seed_chain()
        plan = self.plan_for_removal()
        apply_request = {
            "tenant": "t",
            "op": "plan.apply",
            "plan_id": plan["plan_id"],
            "applied_at": T2,
        }
        # A parks inside its write transaction, after revalidation.
        worker_a, process_a = self.spawn(
            "a",
            [dict(apply_request, request_id="apply-a")],
            SOLUTION_GATE_POINTS="plan_apply.revalidated",
        )
        wait_arrived(self.barrier, "a")
        # B parks outside any transaction, so it cannot overtake A.
        worker_b, process_b = self.spawn(
            "b",
            [dict(apply_request, request_id="apply-b")],
            SOLUTION_GATE_POINTS="before:plan.apply",
        )
        wait_arrived(self.barrier, "b")

        release(self.barrier, "a")
        code_a = process_a.wait(timeout=120)
        worker_a.collect(process_a)
        self.assertEqual(code_a, 0, worker_a.log())
        release(self.barrier, "b")
        code_b = process_b.wait(timeout=120)
        worker_b.collect(process_b)
        self.assertEqual(code_b, 0, worker_b.log())

        responses_a = worker_a.responses()
        responses_b = worker_b.responses()
        self.assertEqual(len(responses_a), 1, worker_a.log())
        self.assertEqual(len(responses_b), 1, worker_b.log())
        self.assertTrue(responses_a[0]["ok"], responses_a)
        # The loser never wrote a second application receipt.
        self.assertFalse(responses_b[0]["ok"], responses_b)
        self.assertEqual(responses_b[0]["code"], "plan_already_applied")
        self.assertEqual(
            responses_b[0]["detail"]["apply_request_id"], "apply-a"
        )

        export = self.export(T2)
        self.assertEqual(len(export["plans"]), 1)
        applied = [entry for entry in export["plans"] if entry["applied"]]
        self.assertEqual(len(applied), 1)
        # Exactly one sequence of events exists for the plan.
        rows = self.coordinator.store.conn.execute(
            "SELECT COUNT(*) AS n, MIN(seq) AS lo, MAX(seq) AS hi,"
            " COUNT(DISTINCT seq) AS distinct_seq FROM apply_events"
        ).fetchone()
        self.assertEqual(rows["n"], rows["distinct_seq"])
        self.assertEqual(rows["lo"], 1)
        self.assertEqual(rows["n"], rows["hi"])
        # And the permissions really are gone.
        self.assertEqual(
            {item["object_id"]: item["materialized"] for item in export["objects"]},
            {"app": [], "base": []},
        )

    def test_overlapping_plans_second_becomes_stale(self):
        """Two plans over different objects that share a dependency.

        Plan 1 removes base.perm (cascading to app).  Plan 2, planned before
        plan 1 applies, tries to add a permission on `other`.  Plan 1 commits
        first and bumps the shared object versions, so plan 2 must be rejected
        as stale rather than apply on a moved basis.
        """

        self.seed_chain()
        self.ok(op="object.create", object_id="other", baseline=[], at=T0)
        self.ok(
            op="dependency.add",
            from_object="other",
            from_permission="other.perm",
            to_object="base",
            to_permission="base.perm",
            at=T0,
        )
        # Plan 2 is built while base.perm is still available, so it would add
        # other.perm.  Plan 1 is built afterwards and will remove base.perm
        # (and therefore other.perm as well).
        self.ok(op="lease.grant", lease_id="LO", object_id="other",
                permissions=["other.perm"], start=T0, end=T2, at=T0)
        plan_two = self.ok(op="plan.create", planned_at=T0, objects=["other"])
        self.assertEqual(
            plan_two["additions"], [{"object_id": "other", "permission": "other.perm"}]
        )
        plan_one = self.plan_for_removal()

        barrier = self.barrier
        worker_a, process_a = self.spawn(
            "pa",
            [{"tenant": "t", "request_id": "apply-one", "op": "plan.apply",
              "plan_id": plan_one["plan_id"], "applied_at": T2}],
            SOLUTION_GATE_POINTS="plan_apply.revalidated",
        )
        wait_arrived(barrier, "pa")
        worker_b, process_b = self.spawn(
            "pb",
            [{"tenant": "t", "request_id": "apply-two", "op": "plan.apply",
              "plan_id": plan_two["plan_id"], "applied_at": T2}],
            SOLUTION_GATE_POINTS="before:plan.apply",
        )
        wait_arrived(barrier, "pb")
        release(barrier, "pa")
        self.assertEqual(process_a.wait(timeout=120), 0, worker_a.log())
        worker_a.collect(process_a)
        release(barrier, "pb")
        self.assertEqual(process_b.wait(timeout=120), 0, worker_b.log())
        worker_b.collect(process_b)

        self.assertTrue(worker_a.responses()[0]["ok"])
        second = worker_b.responses()[0]
        self.assertFalse(second["ok"], second)
        self.assertEqual(second["code"], "stale")
        kinds = {reason["kind"] for reason in second["detail"]["reasons"]}
        self.assertTrue(
            kinds & {"object_changed", "target_changed", "dependency_changed"}, kinds
        )
        export = self.export(T2)
        self.assertEqual(
            {item["object_id"]: item["materialized"] for item in export["objects"]},
            {"app": [], "base": [], "other": []},
        )

    # -- real termination inside the transaction: no half plan ---------------

    def assert_no_half_plan(self, plan_id: str, before: dict) -> None:
        self.reopen()
        export = self.export(T2)
        entry = [plan for plan in export["plans"] if plan["plan_id"] == plan_id][0]
        self.assertFalse(entry["applied"])
        rows = self.coordinator.store.conn.execute(
            "SELECT COUNT(*) AS n FROM apply_events WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        self.assertEqual(rows["n"], 0)
        self.assertEqual(
            {item["object_id"]: item["materialized"] for item in export["objects"]},
            {key: value["materialized"] for key, value in before.items()},
        )
        receipts = [
            item for item in export["receipts"] if item["op"] == "plan.apply"
        ]
        self.assertEqual(receipts, [])

    def test_terminated_before_first_write(self):
        self.seed_chain()
        plan = self.plan_for_removal()
        before = {
            item["object_id"]: item for item in self.export(T2)["objects"]
        }
        worker, process = self.spawn(
            "prewrite",
            [
                {
                    "tenant": "t",
                    "request_id": "apply-pre",
                    "op": "plan.apply",
                    "plan_id": plan["plan_id"],
                    "applied_at": T2,
                }
            ],
            SOLUTION_GATE_POINTS="plan_apply.revalidated",
        )
        wait_arrived(self.barrier, "prewrite", timeout=60)
        process.terminate()
        returncode = process.wait(timeout=60)
        worker.collect(process)
        self.assertNotEqual(returncode, 0)
        self.assertEqual(worker.responses(), [])

        self.assert_no_half_plan(plan["plan_id"], before)

        # The untouched plan is still applicable in a fresh process.
        worker_retry = Worker(self.db_path, self.tmp, "retry")
        code, responses = worker_retry.run(
            [
                {
                    "tenant": "t",
                    "request_id": "apply-retry",
                    "op": "plan.apply",
                    "plan_id": plan["plan_id"],
                    "applied_at": T2,
                }
            ]
        )
        self.assertEqual(code, 0, worker_retry.log())
        self.assertTrue(responses[0]["ok"], responses)
        final = self.export(T2)
        self.assertEqual(
            {item["object_id"]: item["materialized"] for item in final["objects"]},
            {"app": [], "base": []},
        )

    def test_terminated_after_writes_before_commit(self):
        self.seed_chain()
        plan = self.plan_for_removal()
        before = {
            item["object_id"]: item for item in self.export(T2)["objects"]
        }
        worker, process = self.spawn(
            "postwrite",
            [
                {
                    "tenant": "t",
                    "request_id": "apply-post",
                    "op": "plan.apply",
                    "plan_id": plan["plan_id"],
                    "applied_at": T2,
                }
            ],
            SOLUTION_GATE_POINTS="plan_apply.written",
        )
        wait_arrived(self.barrier, "postwrite", timeout=60)
        process.terminate()
        returncode = process.wait(timeout=60)
        worker.collect(process)
        self.assertNotEqual(returncode, 0)
        self.assertEqual(worker.responses(), [])

        # Every object write was rolled back with the uncommitted transaction.
        self.assert_no_half_plan(plan["plan_id"], before)

        worker_retry = Worker(self.db_path, self.tmp, "retry-post")
        code, responses = worker_retry.run(
            [
                {
                    "tenant": "t",
                    "request_id": "apply-retry-post",
                    "op": "plan.apply",
                    "plan_id": plan["plan_id"],
                    "applied_at": T2,
                }
            ]
        )
        self.assertEqual(code, 0, worker_retry.log())
        self.assertTrue(responses[0]["ok"], responses)

    # -- crash after commit: receipt and state both durable ------------------

    def test_terminated_after_commit_keeps_exactly_one_result(self):
        self.seed_chain()
        plan = self.plan_for_removal()
        worker = Worker(self.db_path, self.tmp, "committed")
        code, responses = worker.run(
            [
                {
                    "tenant": "t",
                    "request_id": "apply-committed",
                    "op": "plan.apply",
                    "plan_id": plan["plan_id"],
                    "applied_at": T2,
                },
                {"crash": "request_after_commit"},
                {
                    "tenant": "t",
                    "request_id": "apply-committed-second",
                    "op": "state.export",
                    "at": T2,
                },
            ]
        )
        # The first step committed and its response was written; the process
        # then died at the explicit crash point of the second request.
        self.assertNotEqual(code, 0)
        self.assertEqual(len(responses), 1, worker.log())
        self.assertTrue(responses[0]["ok"], responses)

        self.reopen()
        export = self.export(T2)
        self.assertTrue(export["plans"][0]["applied"])
        self.assertEqual(len(export["plans"][0]["events"]), 2)

        # The identical retry replays the persisted result instead of applying
        # again, and the plan stays applied exactly once.
        replay = self.ok(
            op="plan.apply",
            plan_id=plan["plan_id"],
            applied_at=T2,
            request_id="apply-committed",
        )
        self.assertEqual(replay, responses[0])
        final = self.export(T2)
        self.assertEqual(sum(1 for plan in final["plans"] if plan["applied"]), 1)

    def test_interrupted_before_commit_is_judged_by_restarted_database(self):
        self.seed_chain()
        # Arm the crash before the create: the write transaction starts, the
        # process dies inside it, and nothing is committed.
        worker = Worker(self.db_path, self.tmp, "uncommitted")
        code, responses = worker.run(
            [
                {"crash": "request_before_commit"},
                {
                    "tenant": "t",
                    "request_id": "never-committed",
                    "op": "object.create",
                    "object_id": "never",
                    "baseline": ["never.perm"],
                    "at": T1,
                },
            ]
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(responses, [], worker.log())

        # The only trustworthy judge is the restarted database: neither the
        # object nor its receipt exists.
        self.reopen()
        export = self.export(T1)
        ids = {item["object_id"] for item in export["objects"]}
        self.assertEqual(ids, {"base", "app"})
        request_ids = {item["request_id"] for item in export["receipts"]}
        self.assertNotIn("never-committed", request_ids)
        # Reusing the request id now behaves as a first attempt.
        created = self.ok(
            op="object.create",
            object_id="never",
            baseline=["never.perm"],
            at=T1,
            request_id="never-committed",
        )
        self.assertEqual(created["object"]["object_id"], "never")

    # -- same request id in two processes -----------------------------------

    def test_concurrent_identical_request_yields_one_state_change(self):
        self.ok(op="object.create", object_id="a", baseline=["read"], at=T0)
        request = {
            "tenant": "t",
            "request_id": "shared",
            "op": "lease.grant",
            "lease_id": "L1",
            "object_id": "a",
            "permissions": ["read"],
            "start": T0,
            "end": T2,
            "at": T0,
        }
        worker_a, process_a = self.spawn(
            "ra", [request], SOLUTION_GATE_POINTS="before:lease.grant"
        )
        wait_arrived(self.barrier, "ra")
        worker_b, process_b = self.spawn(
            "rb", [request], SOLUTION_GATE_POINTS="before:lease.grant"
        )
        wait_arrived(self.barrier, "rb")
        release(self.barrier, "ra", "rb")
        self.assertEqual(process_a.wait(timeout=120), 0, worker_a.log())
        worker_a.collect(process_a)
        self.assertEqual(process_b.wait(timeout=120), 0, worker_b.log())
        worker_b.collect(process_b)

        responses = worker_a.responses() + worker_b.responses()
        self.assertEqual(len(responses), 2, (worker_a.log(), worker_b.log()))
        self.assertEqual(responses[0], responses[1])
        self.assertTrue(responses[0]["ok"], responses)
        export = self.export(T1)
        self.assertEqual(len(export["leases"]), 1)
        self.assertEqual(export["leases"][0]["version"], 1)


if __name__ == "__main__":
    unittest.main()
