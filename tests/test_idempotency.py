"""Idempotency, request-id conflicts, error codes and validation."""

from __future__ import annotations

import unittest

from tests.helpers import CoordinatorTestCase

T0 = "2026-05-01T00:00:00Z"
T1 = "2026-05-01T01:00:00Z"


class IdempotencyTests(CoordinatorTestCase):
    def test_same_id_same_params_returns_first_result(self):
        first = self.ok(
            op="object.create",
            object_id="a",
            baseline=["read"],
            at=T0,
            request_id="create-a",
        )
        second = self.ok(
            op="object.create",
            object_id="a",
            baseline=["read"],
            at=T0,
            request_id="create-a",
        )
        self.assertEqual(first, second)
        export = self.export(T1)
        self.assertEqual(len(export["objects"]), 1)
        self.assertEqual(export["objects"][0]["object_version"], 1)

    def test_same_id_different_params_rejected(self):
        self.ok(
            op="object.create",
            object_id="a",
            baseline=["read"],
            at=T0,
            request_id="only-once",
        )
        response = self.call(
            op="object.create",
            object_id="a",
            baseline=["read"],
            at=T1,
            request_id="only-once",
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "request_id_conflict")
        # The conflicting call did not write anything either.
        self.assertEqual(self.get_object("a", T1)["object_version"], 1)

    def test_request_id_uuid_within_tenant_only(self):
        # Same request id, same params but a different tenant: independent.
        self.ok(op="object.create", object_id="a", at=T0, tenant="alpha")
        reply = self.ok(op="object.create", object_id="a", at=T0, tenant="beta")
        self.assertEqual(reply["object"]["object_id"], "a")
        self.assertEqual(
            len(self.export(T1, tenant="alpha")["objects"]), 1
        )
        self.assertEqual(len(self.export(T1, tenant="beta")["objects"]), 1)

    def test_failed_result_is_replayed(self):
        self.ok(op="object.create", object_id="a", at=T0)
        first = self.err(
            "already_exists",
            op="object.create",
            object_id="a",
            at=T0,
            request_id="dup",
        )
        second = self.call(
            op="object.create", object_id="a", at=T0, request_id="dup"
        )
        self.assertEqual(first, second)
        self.assertEqual(second["code"], "already_exists")
        # The failure receipt is distinguishable from a business state
        # change: object_version did not move.
        self.assertEqual(self.get_object("a", T1)["object_version"], 1)

    def test_failure_receipt_differs_from_success_receipt(self):
        success = self.ok(op="object.create", object_id="a", at=T0)
        failure = self.call(
            op="object.create", object_id="a", at=T0, request_id="other"
        )
        self.assertTrue(success["ok"])
        self.assertFalse(failure["ok"])
        self.assertIn("code", failure)
        self.assertNotIn("code", success)

    def test_replay_survives_reopen(self):
        self.ok(op="object.create", object_id="a", at=T0, request_id="stable")
        self.reopen()
        replay = self.ok(op="object.create", object_id="a", at=T0, request_id="stable")
        self.assertEqual(replay["object"]["object_version"], 1)


class ValidationTests(CoordinatorTestCase):
    def test_missing_envelope_fields(self):
        self.assertEqual(
            self.coordinator.handle({"op": "object.create"})["code"],
            "invalid_request",
        )
        self.assertEqual(
            self.coordinator.handle(
                {"tenant": "t", "request_id": "x", "op": "object.nope"}
            )["code"],
            "unknown_op",
        )

    def test_extra_fields_rejected(self):
        self.err(
            "invalid_argument",
            op="object.create",
            object_id="a",
            at=T0,
            surprise=True,
        )

    def test_bad_instant(self):
        self.err(
            "invalid_argument", op="object.create", object_id="a", at="tomorrow"
        )
        self.err(
            "invalid_argument",
            op="object.create",
            object_id="a",
            at="2026-02-30T00:00:00Z",
        )

    def test_lease_window_requires_positive_duration(self):
        self.ok(op="object.create", object_id="a", at=T0)
        self.err(
            "invalid_argument",
            op="lease.grant",
            lease_id="L",
            object_id="a",
            permissions=["read"],
            start=T1,
            end=T1,
            at=T0,
        )

    def test_leasing_missing_object(self):
        self.err(
            "not_found",
            op="lease.grant",
            lease_id="L",
            object_id="ghost",
            permissions=["read"],
            start=T0,
            end=T1,
            at=T0,
        )

    def test_duplicate_permissions_rejected(self):
        self.ok(op="object.create", object_id="a", at=T0)
        self.err(
            "invalid_argument",
            op="lease.grant",
            lease_id="L",
            object_id="a",
            permissions=["read", "read"],
            start=T0,
            end=T1,
            at=T0,
        )

    def test_delete_then_create_is_allowed(self):
        self.ok(op="object.create", object_id="a", at=T0)
        self.ok(op="object.delete", object_id="a", at=T1)
        created = self.ok(op="object.create", object_id="a", at=T1)
        # Generation is monotonic across delete/recreate: a stale plan that
        # remembered generation 1 cannot be applied to this re-created object.
        self.assertEqual(created["object"]["generation"], 2)


if __name__ == "__main__":
    unittest.main()
