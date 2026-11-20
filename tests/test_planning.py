"""Plan snapshot, staleness rules and apply semantics."""

from __future__ import annotations

import json
import unittest

from tests.helpers import CoordinatorTestCase

T0 = "2026-04-01T00:00:00Z"
T1 = "2026-04-01T01:00:00Z"
T2 = "2026-04-01T02:00:00Z"
T3 = "2026-04-01T03:00:00Z"


class PlanStalenessTests(CoordinatorTestCase):
    def setUp(self):
        super().setUp()
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
        self.ok(op="object.create", object_id="other", baseline=["other.perm"], at=T0)

    def make_removal_plan(self):
        self.ok(op="object.update", object_id="base", baseline=[], at=T1)
        # Scope names only `base`; the downstream `app` is pulled in by the
        # cascade.  `other` has no dependency relation and stays out of the
        # basis, which is what makes the "irrelevant change" cases meaningful.
        return self.ok(op="plan.create", planned_at=T1, objects=["base"])

    def assert_nothing_applied(self, at=T3):
        export = self.export(at)
        plans = {plan["plan_id"]: plan for plan in export["plans"]}
        return plans

    def test_apply_succeeds_and_is_recorded(self):
        plan = self.make_removal_plan()
        applied = self.ok(op="plan.apply", plan_id=plan["plan_id"], applied_at=T1)
        self.assertTrue(applied["applied"])
        export = self.export(T1)
        self.assertEqual(len(export["plans"]), 1)
        self.assertTrue(export["plans"][0]["applied"])
        self.assertEqual(
            [(event["action"], event["object_id"]) for event in export["plans"][0]["events"]],
            [("remove", "app"), ("remove", "base")],
        )

    def test_second_apply_rejected_without_changes(self):
        plan = self.make_removal_plan()
        self.ok(op="plan.apply", plan_id=plan["plan_id"], applied_at=T1)
        before = self.export(T2)
        self.err(
            "plan_already_applied",
            op="plan.apply",
            plan_id=plan["plan_id"],
            applied_at=T2,
        )
        after = self.export(T2)
        self.assertEqual(
            [item["materialized"] for item in before["objects"]],
            [item["materialized"] for item in after["objects"]],
        )

    def test_irrelevant_object_change_does_not_invalidate(self):
        plan = self.make_removal_plan()
        # `other` is unrelated to the plan's objects and dependencies.
        self.ok(
            op="object.update", object_id="other", baseline=["extra"], at=T2
        )
        self.ok(op="plan.apply", plan_id=plan["plan_id"], applied_at=T2)

    def test_irrelevant_lease_does_not_invalidate(self):
        plan = self.make_removal_plan()
        self.ok(
            op="lease.grant",
            lease_id="LX",
            object_id="other",
            permissions=["other.perm"],
            start=T1,
            end=T3,
            at=T1,
        )
        self.ok(op="plan.apply", plan_id=plan["plan_id"], applied_at=T2)

    def test_relevant_object_change_is_stale(self):
        plan = self.make_removal_plan()
        self.ok(op="object.update", object_id="base", baseline=["base.perm"], at=T2)
        response = self.err(
            "stale", op="plan.apply", plan_id=plan["plan_id"], applied_at=T3
        )
        self.assertFalse(response["detail"]["applied"])
        kinds = {reason["kind"] for reason in response["detail"]["reasons"]}
        self.assertIn("object_changed", kinds)
        # Nothing changed.
        self.assertEqual(
            set(self.get_object("base", T3)["materialized"]), {"base.perm"}
        )
        self.assertEqual(set(self.get_object("app", T3)["materialized"]), {"app.perm"})

    def test_recreated_object_is_stale(self):
        plan = self.make_removal_plan()
        self.ok(op="object.recreate", object_id="app", baseline=["app.perm"], at=T2)
        response = self.err(
            "stale", op="plan.apply", plan_id=plan["plan_id"], applied_at=T3
        )
        kinds = {reason["kind"] for reason in response["detail"]["reasons"]}
        self.assertIn("object_recreated", kinds)

    def test_dependency_rewrite_is_stale(self):
        plan = self.make_removal_plan()
        self.ok(
            op="dependency.remove",
            from_object="app",
            from_permission="app.perm",
            to_object="base",
            to_permission="base.perm",
            at=T2,
        )
        response = self.err(
            "stale", op="plan.apply", plan_id=plan["plan_id"], applied_at=T3
        )
        kinds = {reason["kind"] for reason in response["detail"]["reasons"]}
        self.assertIn("dependency_changed", kinds)

    def test_relevant_lease_grant_is_stale(self):
        plan = self.make_removal_plan()
        self.ok(
            op="lease.grant",
            lease_id="LB",
            object_id="base",
            permissions=["base.perm"],
            start=T2,
            end=T3,
            at=T2,
        )
        self.err("stale", op="plan.apply", plan_id=plan["plan_id"], applied_at=T3)

    def test_lease_expiry_between_planning_and_apply_is_stale(self):
        self.ok(op="object.create", object_id="temp", baseline=[], at=T0)
        self.ok(
            op="lease.grant",
            lease_id="LT",
            object_id="temp",
            permissions=["temp.perm"],
            start=T0,
            end=T2,
            at=T0,
        )
        plan = self.ok(op="plan.create", planned_at=T1)
        self.assertEqual(
            plan["additions"], [{"object_id": "temp", "permission": "temp.perm"}]
        )
        response = self.err(
            "stale", op="plan.apply", plan_id=plan["plan_id"], applied_at=T2
        )
        reasons = response["detail"]["reasons"]
        self.assertEqual(reasons[0]["kind"], "target_changed")
        self.assertEqual(
            reasons[0]["expired_leases"],
            [{"lease_id": "LT", "end": T2}],
        )
        self.assertEqual(set(self.get_object("temp", T2)["materialized"]), set())

    def test_lease_expiry_after_apply_instant_is_fine(self):
        self.ok(op="object.create", object_id="temp", baseline=[], at=T0)
        self.ok(
            op="lease.grant",
            lease_id="LT",
            object_id="temp",
            permissions=["temp.perm"],
            start=T0,
            end=T2,
            at=T0,
        )
        plan = self.ok(op="plan.create", planned_at=T1)
        self.ok(
            op="plan.apply",
            plan_id=plan["plan_id"],
            applied_at="2026-04-01T01:59:59.999999Z",
        )

    def test_stale_apply_leaves_no_plan_receipt(self):
        plan = self.make_removal_plan()
        self.ok(op="object.update", object_id="base", baseline=["base.perm"], at=T2)
        self.err("stale", op="plan.apply", plan_id=plan["plan_id"], applied_at=T3)
        export = self.export(T3)
        self.assertFalse(export["plans"][0]["applied"])
        self.assertEqual(
            [plan["applied"] for plan in export["plans"]], [False]
        )
        # The stale rejection is still a persisted business receipt, so a
        # retry with the same request id sees the same answer.
        receipts = {item["request_id"]: item for item in export["receipts"]}
        apply_receipts = [
            item for item in receipts.values() if item["op"] == "plan.apply"
        ]
        self.assertEqual(len(apply_receipts), 1)
        self.assertEqual(apply_receipts[0]["code"], "stale")

    def test_apply_records_planned_versions_in_basis(self):
        plan = self.make_removal_plan()
        export = self.export(T1)
        stored = self.coordinator.store.conn.execute(
            "SELECT basis FROM plans WHERE plan_id = ?", (plan["plan_id"],)
        ).fetchone()
        basis = json.loads(stored["basis"])
        self.assertEqual(set(basis["objects"]), {"app", "base"})
        self.assertEqual(set(basis["objects"]["base"]), {"generation", "object_version"})
        self.assertEqual(
            basis["dependencies"],
            {"app|app.perm|base|base.perm": {"version": 1, "removed": False}},
        )
        self.assertEqual(sorted(basis["leases"]), [])

    def test_unknown_plan_not_found(self):
        self.err("not_found", op="plan.apply", plan_id="p999999", applied_at=T1)

    def test_tenant_isolation(self):
        plan = self.make_removal_plan()
        self.err(
            "not_found",
            op="plan.apply",
            plan_id=plan["plan_id"],
            applied_at=T1,
            tenant="other-tenant",
        )


class PlanLimitTests(CoordinatorTestCase):
    def test_more_than_fifty_changed_objects_rejected(self):
        # 51 objects each losing a permission: 51 objects would be adjusted.
        for index in range(51):
            self.ok(
                op="object.create",
                object_id=f"o{index:03d}",
                baseline=["keep"],
                at=T0,
            )
            self.ok(
                op="object.update", object_id=f"o{index:03d}", baseline=[], at=T0
            )
        response = self.err("plan_limit_exceeded", op="plan.create", planned_at=T1)
        self.assertEqual(response["detail"]["limit"], 50)
        self.assertEqual(response["detail"]["changed_objects"], 51)

    def test_fifty_objects_allowed(self):
        for index in range(50):
            self.ok(
                op="object.create",
                object_id=f"o{index:03d}",
                baseline=["keep"],
                at=T0,
            )
            self.ok(
                op="object.update", object_id=f"o{index:03d}", baseline=[], at=T0
            )
        plan = self.ok(op="plan.create", planned_at=T1)
        self.assertEqual(plan["summary"]["objects_in_scope"], 50)
        self.assertEqual(plan["summary"]["objects_changed"], 50)

    def test_many_unchanged_objects_do_not_count_as_adjusted(self):
        # The limit bounds what a plan *adjusts*, not how many objects are
        # read: 60 untouched objects are a no-op plan.
        for index in range(60):
            self.ok(op="object.create", object_id=f"o{index:03d}", at=T0)
        plan = self.ok(op="plan.create", planned_at=T1)
        self.assertEqual(plan["summary"]["objects_changed"], 0)
        self.assertEqual(plan["removals"], [])
        self.assertEqual(plan["additions"], [])

    def test_cascade_limit_uses_changed_objects(self):
        # 60 objects, only 30 with a change: the plan is allowed.
        for index in range(60):
            self.ok(op="object.create", object_id=f"o{index:03d}", at=T0)
        for index in range(30):
            self.ok(
                op="object.update", object_id=f"o{index:03d}", baseline=[], at=T0
            )
            self.ok(
                op="object.update",
                object_id=f"o{index:03d}",
                baseline=[f"p{index}"],
                at=T0,
            )
        plan = self.ok(
            op="plan.create",
            planned_at=T1,
            objects=[f"o{index:03d}" for index in range(30)],
        )
        self.assertEqual(plan["summary"]["objects_changed"], 30)
        self.assertEqual(plan["summary"]["remove_count"], 0)
        self.assertEqual(plan["summary"]["add_count"], 30)

    def test_unknown_object_in_scope(self):
        self.err(
            "not_found", op="plan.create", planned_at=T1, objects=["ghost"]
        )


if __name__ == "__main__":
    unittest.main()


class UpstreamInclusionTests(CoordinatorTestCase):
    def test_prerequisite_added_in_same_plan_before_dependent(self):
        self.ok(op="object.create", object_id="base", at=T0)
        self.ok(op="object.create", object_id="app", at=T0)
        self.ok(
            op="dependency.add",
            from_object="app",
            from_permission="app.use",
            to_object="base",
            to_permission="base.provide",
            at=T0,
        )
        # Both permissions become available only via these updates.
        self.ok(op="object.update", object_id="base", baseline=["base.provide"], at=T1)
        self.ok(op="object.update", object_id="app", baseline=["app.use"], at=T1)
        plan = self.ok(op="plan.create", planned_at=T1, objects=["app"])
        self.assertEqual(
            [(item["object_id"], item["permission"]) for item in plan["additions"]],
            [("base", "base.provide"), ("app", "app.use")],
        )
        applied = self.ok(op="plan.apply", plan_id=plan["plan_id"], applied_at=T1)
        self.assertEqual(
            [(event["object_id"], event["permission"]) for event in applied["events"]],
            [("base", "base.provide"), ("app", "app.use")],
        )

    def test_adding_dependent_without_prerequisite_source_is_noop(self):
        self.ok(op="object.create", object_id="base", at=T0)
        self.ok(op="object.create", object_id="app", at=T0)
        self.ok(
            op="dependency.add",
            from_object="app",
            from_permission="app.use",
            to_object="base",
            to_permission="base.provide",
            at=T0,
        )
        # Only app is sourced; base never is, so app.use can never be kept.
        self.ok(op="object.update", object_id="app", baseline=["app.use"], at=T1)
        plan = self.ok(op="plan.create", planned_at=T1, objects=["app"])
        self.assertEqual(plan["additions"], [])
        obj = self.get_object("app", T1)
        self.assertEqual(obj["target"], [])
        self.assertEqual(
            obj["explanation"]["app.use"]["missing_prerequisites"],
            [{"object_id": "base", "permission": "base.provide"}],
        )

    def test_deleted_prerequisite_retires_dependencies(self):
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
        deleted = self.ok(op="object.delete", object_id="base", at=T1)
        self.assertEqual(len(deleted["retired_dependencies"]), 1)
        # Deleting the prerequisite retires the edge (same semantics as an
        # explicit `dependency.remove`: the constraint no longer exists), so
        # app.perm is no longer blocked and nothing must be removed.  Crucially
        # the plan neither fails nor references the deleted object.
        plan = self.ok(op="plan.create", planned_at=T1, objects=["app"])
        self.assertEqual(plan["removals"], [])
        self.assertEqual(plan["additions"], [])
        self.assertEqual(set(self.get_object("app", T1)["target"]), {"app.perm"})
        self.assertEqual(self.export(T1)["dependencies"], [])
