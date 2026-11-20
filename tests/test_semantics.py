"""Target semantics, DAG pruning, ordering and multi-source explanation."""

from __future__ import annotations

import unittest

from tests import reference
from tests.helpers import CoordinatorTestCase

T0 = "2026-03-01T00:00:00Z"
T1 = "2026-03-01T01:00:00Z"
T2 = "2026-03-01T02:00:00Z"


class TargetTests(CoordinatorTestCase):
    def create(self, object_id, baseline=(), attributes=None, at=T0):
        return self.ok(
            op="object.create",
            object_id=object_id,
            baseline=list(baseline),
            attributes=attributes or {},
            at=at,
        )

    def test_baseline_union_with_active_lease(self):
        self.create("a", baseline=["read"])
        self.ok(
            op="lease.grant",
            lease_id="L1",
            object_id="a",
            permissions=["write"],
            start=T0,
            end=T2,
            at=T0,
        )
        target = set(self.get_object("a", T1)["target"])
        self.assertEqual(target, {"read", "write"})
        # Independently derived.
        ref_objects = {"a": reference.RefObject(baseline={"read"})}
        ref_leases = {
            "L1": reference.RefLease(
                "L1", "a", 1, {"write"}, {}, 1, 2
            )
        }
        self.assertEqual(
            target,
            reference.target(ref_objects, ref_leases, [], "a", 1),
        )

    def test_permission_without_prerequisite_is_dropped(self):
        self.create("dep", baseline=[])
        self.create("app", baseline=["app.write"])
        self.ok(
            op="dependency.add",
            from_object="app",
            from_permission="app.write",
            to_object="dep",
            to_permission="dep.read",
            at=T0,
        )
        obj = self.get_object("app", T1)
        self.assertEqual(obj["target"], [])
        self.assertEqual(
            obj["explanation"]["app.write"]["missing_prerequisites"],
            [{"object_id": "dep", "permission": "dep.read"}],
        )
        self.assertEqual(obj["explanation"]["app.write"]["reason"], "missing_prerequisite")

    def test_dependency_chain_prunes_transitively(self):
        self.create("a", baseline=["p1"])
        self.create("b", baseline=["p2"])
        self.create("c", baseline=["p3"])
        self.ok(
            op="dependency.add",
            from_object="a",
            from_permission="p1",
            to_object="b",
            to_permission="p2",
            at=T0,
        )
        self.ok(
            op="dependency.add",
            from_object="b",
            from_permission="p2",
            to_object="c",
            to_permission="p3",
            at=T0,
        )
        # Dependencies constrain, they do not grant: each object keeps only
        # its own sourced permissions, all of which are satisfiable.
        targets = {
            object_id: set(self.get_object(object_id, T1)["target"])
            for object_id in ("a", "b", "c")
        }
        self.assertEqual(targets, {"a": {"p1"}, "b": {"p2"}, "c": {"p3"}})
        # Removing c's source invalidates the whole chain.
        self.ok(op="object.update", object_id="c", baseline=[], at=T1)
        export = self.export(T1)
        targets = {item["object_id"]: set(item["target"]) for item in export["objects"]}
        self.assertEqual(targets, {"a": set(), "b": set(), "c": set()})

    def test_cycle_rejected(self):
        self.create("a")
        self.create("b")
        self.ok(
            op="dependency.add",
            from_object="a",
            from_permission="p",
            to_object="b",
            to_permission="q",
            at=T0,
        )
        response = self.err(
            "dependency_cycle",
            op="dependency.add",
            from_object="b",
            from_permission="q",
            to_object="a",
            to_permission="p",
            at=T0,
        )
        self.assertIn("cycle", response["message"])

    def test_multi_source_explained_per_item(self):
        self.create("a", baseline=["read"])
        self.ok(
            op="lease.grant",
            lease_id="L1",
            object_id="a",
            permissions=["read"],
            start=T0,
            end=T2,
            at=T0,
        )
        self.ok(
            op="lease.grant",
            lease_id="L2",
            object_id="a",
            permissions=["read", "write"],
            start=T0,
            end=T2,
            at=T0,
        )
        explanation = self.get_object("a", T1)["explanation"]
        labels = [
            ("baseline" if source["kind"] == "baseline" else source["lease_id"])
            for source in explanation["read"]["sources"]
        ]
        self.assertEqual(labels, ["baseline", "L1", "L2"])
        write_labels = [source["lease_id"] for source in explanation["write"]["sources"]]
        self.assertEqual(write_labels, ["L2"])
        self.assertTrue(explanation["read"]["kept"])

    def test_multi_source_kept_when_one_source_expires(self):
        self.create("a", baseline=["read"])
        self.ok(
            op="lease.grant",
            lease_id="L1",
            object_id="a",
            permissions=["read"],
            start=T0,
            end=T1,
            at=T0,
        )
        self.assertEqual(set(self.get_object("a", T1)["target"]), {"read"})
        explanation = self.get_object("a", T1)["explanation"]["read"]["sources"]
        # Every source is accounted for individually: the baseline (still
        # active) and the expired lease, which is listed with its reason.
        self.assertEqual(
            [
                (source["kind"], source.get("lease_id"), source.get("active"))
                for source in explanation
            ],
            [("baseline", None, True), ("lease", "L1", False)],
        )
        self.assertEqual(explanation[1]["inactive_reason"], "expired")

    def test_overlap_of_persistent_and_temporary_sources(self):
        """Persistent baseline + temporary lease + expiring lease, one permission."""

        self.create("a", baseline=["shared"])
        self.ok(
            op="lease.grant",
            lease_id="LT",
            object_id="a",
            permissions=["shared"],
            start=T0,
            end=T1,
            at=T0,
        )
        self.ok(
            op="lease.grant",
            lease_id="LL",
            object_id="a",
            permissions=["shared"],
            start=T0,
            end=T2,
            at=T0,
        )
        # Before the short lease expires: three separate sources.
        sources = self.get_object("a", "2026-03-01T00:30:00Z")["explanation"]["shared"][
            "sources"
        ]
        self.assertEqual(
            [(source["kind"], source.get("lease_id"), source["active"]) for source in sources],
            [("baseline", None, True), ("lease", "LL", True), ("lease", "LT", True)],
        )
        # Exactly at the short lease's end it is expired but the permission
        # stays, supplied by the two remaining sources.
        sources = self.get_object("a", T1)["explanation"]["shared"]["sources"]
        self.assertEqual(
            [(source["kind"], source.get("lease_id"), source["active"]) for source in sources],
            [("baseline", None, True), ("lease", "LL", True), ("lease", "LT", False)],
        )
        self.assertEqual(sources[2]["inactive_reason"], "expired")
        self.assertEqual(set(self.get_object("a", T1)["target"]), {"shared"})

    def test_attribute_reference_gates_lease(self):
        self.create("a", baseline=[], attributes={"tier": "gold"})
        self.ok(
            op="lease.grant",
            lease_id="L1",
            object_id="a",
            permissions=["admin"],
            attributes={"tier": "gold"},
            start=T0,
            end=T2,
            at=T0,
        )
        self.assertEqual(set(self.get_object("a", T1)["target"]), {"admin"})
        self.ok(op="object.update", object_id="a", attributes={"tier": "silver"}, at=T1)
        self.assertEqual(
            set(self.get_object("a", "2026-03-01T01:59:59.999999Z")["target"]), set()
        )

    def test_recreate_invalidates_old_leases(self):
        self.create("a", baseline=["read"])
        self.ok(
            op="lease.grant",
            lease_id="L1",
            object_id="a",
            permissions=["write"],
            start=T0,
            end=T2,
            at=T0,
        )
        self.assertEqual(set(self.get_object("a", T1)["target"]), {"read", "write"})
        self.ok(op="object.recreate", object_id="a", baseline=["read"], at=T1)
        obj = self.get_object("a", T1)
        self.assertEqual(obj["generation"], 2)
        self.assertEqual(set(obj["target"]), {"read"})

    def test_deleted_object_not_found(self):
        self.create("a", baseline=["read"])
        self.ok(op="object.delete", object_id="a", at=T1)
        self.err("not_found", op="object.get", object_id="a", at=T2)


class OrderingTests(CoordinatorTestCase):
    def setUp(self):
        super().setUp()
        self.ok(op="object.create", object_id="base", baseline=["base.perm"], at=T0)
        self.ok(op="object.create", object_id="mid", baseline=["mid.perm"], at=T0)
        self.ok(op="object.create", object_id="leaf", baseline=["leaf.perm"], at=T0)
        self.ok(
            op="dependency.add",
            from_object="mid",
            from_permission="mid.perm",
            to_object="base",
            to_permission="base.perm",
            at=T0,
        )
        self.ok(
            op="dependency.add",
            from_object="leaf",
            from_permission="leaf.perm",
            to_object="mid",
            to_permission="mid.perm",
            at=T0,
        )

    def test_removals_downstream_first(self):
        self.ok(op="object.update", object_id="base", baseline=[], at=T1)
        plan = self.ok(op="plan.create", planned_at=T1)
        self.assertEqual(
            [(item["object_id"], item["permission"]) for item in plan["removals"]],
            [
                ("leaf", "leaf.perm"),
                ("mid", "mid.perm"),
                ("base", "base.perm"),
            ],
        )
        applied = self.ok(op="plan.apply", plan_id=plan["plan_id"], applied_at=T1)
        self.assertEqual(
            [(event["object_id"], event["permission"]) for event in applied["events"]],
            [
                ("leaf", "leaf.perm"),
                ("mid", "mid.perm"),
                ("base", "base.perm"),
            ],
        )

    def test_additions_upstream_first(self):
        for object_id in ("base", "mid", "leaf"):
            self.ok(op="object.update", object_id=object_id, baseline=[], at=T1)
        drain = self.ok(op="plan.create", planned_at=T1)
        self.ok(op="plan.apply", plan_id=drain["plan_id"], applied_at=T1)
        plan = self.ok(op="plan.create", planned_at=T1)
        self.assertEqual(plan["removals"], [])
        self.ok(
            op="object.update", object_id="base", baseline=["base.perm"], at=T2
        )
        self.ok(op="object.update", object_id="mid", baseline=["mid.perm"], at=T2)
        self.ok(op="object.update", object_id="leaf", baseline=["leaf.perm"], at=T2)
        plan = self.ok(op="plan.create", planned_at=T2)
        self.assertEqual(
            [(item["object_id"], item["permission"]) for item in plan["additions"]],
            [
                ("base", "base.perm"),
                ("mid", "mid.perm"),
                ("leaf", "leaf.perm"),
            ],
        )

    def test_cross_object_precedence_evidence_in_receipt(self):
        self.ok(op="object.update", object_id="base", baseline=[], at=T1)
        plan = self.ok(op="plan.create", planned_at=T1)
        self.ok(op="plan.apply", plan_id=plan["plan_id"], applied_at=T1)
        export = self.export(T2)
        applied = export["plans"][0]
        self.assertTrue(applied["applied"])
        seq = {event["object_id"]: event["seq"] for event in applied["events"]}
        self.assertLess(seq["leaf"], seq["mid"])
        self.assertLess(seq["mid"], seq["base"])


class LeaseBoundaryTests(CoordinatorTestCase):
    def test_exactly_expiring_is_inactive(self):
        self.ok(op="object.create", object_id="a", at=T0)
        self.ok(
            op="lease.grant",
            lease_id="L1",
            object_id="a",
            permissions=["read"],
            start=T0,
            end="2026-03-01T01:00:00.000000Z",
            at=T0,
        )
        self.assertEqual(set(self.get_object("a", T0)["target"]), {"read"})
        self.assertEqual(
            set(self.get_object("a", "2026-03-01T00:59:59.999999Z")["target"]),
            {"read"},
        )
        self.assertEqual(
            set(self.get_object("a", "2026-03-01T01:00:00Z")["target"]), set()
        )

    def test_start_boundary_is_inclusive(self):
        self.ok(op="object.create", object_id="a", at=T0)
        self.ok(
            op="lease.grant",
            lease_id="L1",
            object_id="a",
            permissions=["read"],
            start=T1,
            end=T2,
            at=T0,
        )
        self.assertEqual(set(self.get_object("a", "2026-03-01T00:59:59.999999Z")["target"]), set())
        self.assertEqual(set(self.get_object("a", T1)["target"]), {"read"})

    def test_revoke_takes_effect_at_once(self):
        self.ok(op="object.create", object_id="a", at=T0)
        self.ok(
            op="lease.grant",
            lease_id="L1",
            object_id="a",
            permissions=["read"],
            start=T0,
            end=T2,
            at=T0,
        )
        self.ok(op="lease.revoke", lease_id="L1", at=T1)
        self.assertEqual(set(self.get_object("a", T1)["target"]), set())
        self.err("already_exists", op="lease.revoke", lease_id="L1", at=T2)


if __name__ == "__main__":
    unittest.main()
