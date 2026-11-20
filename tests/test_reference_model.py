"""Differential tests: the coordinator versus an independent reference model.

Randomised but deterministic (fixed seeds) generated graphs.  Expected values
come from ``tests.reference``, never from the implementation.
"""

from __future__ import annotations

import random
import unittest

from tests import reference
from tests.helpers import CoordinatorTestCase

INSTANT = "2026-06-01T{:02d}:{:02d}:00Z"


def at(seconds: int) -> str:
    hours, minutes = divmod(seconds // 60, 60)
    return INSTANT.format(hours, minutes)


class RandomGraphTests(CoordinatorTestCase):
    def fresh(self, seed: int) -> None:
        """Each seed gets its own database file and coordinator."""

        self.coordinator.close()
        self.db_path = self.tmp / f"seed-{seed}.sqlite3"
        from solution.core import Coordinator

        self.coordinator = Coordinator(str(self.db_path))

    def build(self, seed: int) -> tuple[dict, dict, list[tuple[str, str, str, str]]]:
        rng = random.Random(seed)
        count = rng.randint(3, 6)
        permissions = ["read", "write", "admin", "share"]
        objects: dict[str, reference.RefObject] = {}
        leases: dict[str, reference.RefLease] = {}
        edges: list[tuple[str, str, str, str]] = []
        names = [f"o{index}" for index in range(count)]

        for name in names:
            baseline = rng.sample(permissions, rng.randint(0, 2))
            attributes = {"tier": rng.choice(["gold", "silver"])}
            objects[name] = reference.RefObject(
                baseline=set(baseline), attributes=attributes
            )
            self.ok(
                op="object.create",
                object_id=name,
                baseline=baseline,
                attributes=attributes,
                at=at(0),
            )

        for index, name in enumerate(names):
            for other in names[index + 1 :]:
                if rng.random() < 0.45:
                    from_permission = rng.choice(permissions)
                    to_permission = rng.choice(permissions)
                    response = self.call(
                        op="dependency.add",
                        from_object=name,
                        from_permission=from_permission,
                        to_object=other,
                        to_permission=to_permission,
                        at=at(0),
                    )
                    if response.get("ok"):
                        edges.append((name, from_permission, other, to_permission))

        for index in range(rng.randint(1, count)):
            name = rng.choice(names)
            lease_id = f"L{index}"
            granted = rng.sample(permissions, rng.randint(1, 2))
            start = rng.choice([60, 120, 180])
            end = start + rng.choice([60, 120])
            attribute_refs = (
                {"tier": objects[name].attributes["tier"]}
                if rng.random() < 0.4
                else {}
            )
            self.ok(
                op="lease.grant",
                lease_id=lease_id,
                object_id=name,
                permissions=granted,
                attributes=attribute_refs,
                start=at(start),
                end=at(end),
                at=at(0),
            )
            leases[lease_id] = reference.RefLease(
                lease_id,
                name,
                objects[name].generation,
                set(granted),
                dict(attribute_refs),
                start,
                end,
            )
        return objects, leases, edges

    def test_random_graphs_match_reference(self):
        for seed in range(40):
            with self.subTest(seed=seed):
                self.fresh(seed)
                objects, leases, edges = self.build(seed)
                # Probe several instants: before, inside and after leases.
                for seconds in (0, 60, 120, 180, 240, 300):
                    export = self.export(at(seconds))
                    produced = {
                        item["object_id"]: set(item["target"])
                        for item in export["objects"]
                    }
                    expected = reference.global_target(
                        objects, leases, edges, seconds
                    )
                    self.assertEqual(produced, expected)
                    reference.assert_dependency_consistent(
                        self, produced, edges
                    )
                # Also check per-object explanations agree with the oracle.
                for name in objects:
                    for seconds in (0, 150):
                        produced = set(
                            self.get_object(name, at(seconds))["target"]
                        )
                        expected = reference.target(
                            objects, leases, edges, name, seconds
                        )
                        self.assertEqual(produced, expected)

    def test_random_graphs_remain_consistent_after_plan(self):
        for seed in range(20):
            with self.subTest(seed=seed):
                self.fresh(seed)
                objects, leases, edges = self.build(seed)
                plan = self.call(op="plan.create", planned_at=at(120))
                if not plan.get("ok"):
                    continue
                self.ok(op="plan.apply", plan_id=plan["plan_id"], applied_at=at(120))
                export = self.export(at(120))
                produced = {
                    item["object_id"]: set(item["materialized"])
                    for item in export["objects"]
                }
                expected = reference.global_target(objects, leases, edges, 120)
                self.assertEqual(produced, expected)
                reference.assert_dependency_consistent(self, produced, edges)


if __name__ == "__main__":
    unittest.main()
