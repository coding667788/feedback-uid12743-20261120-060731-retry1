"""One-shot generator for cases.json (kept for reviewability of the file)."""

from __future__ import annotations

import json
import pathlib


def T(h, m, s=0):
    return f"2026-08-01T{h:02d}:{m:02d}:{s:02d}Z"


cases = []


def case(name, db, requests, expect):
    cases.append({"name": name, "db": db, "requests": requests, "expect": expect})


case(
    "cascade-and-multi-source",
    "case1.sqlite3",
    [
        {"tenant": "t", "request_id": "c1-01", "op": "object.create", "object_id": "db", "baseline": ["db.read"], "at": T(0, 0)},
        {"tenant": "t", "request_id": "c1-02", "op": "object.create", "object_id": "app", "baseline": ["app.read"], "at": T(0, 0)},
        {"tenant": "t", "request_id": "c1-03", "op": "dependency.add", "from_object": "app", "from_permission": "app.write", "to_object": "db", "to_permission": "db.read", "at": T(0, 0)},
        {"tenant": "t", "request_id": "c1-04", "op": "lease.grant", "lease_id": "L1", "object_id": "app", "permissions": ["app.write"], "start": T(0, 0), "end": T(2, 0), "at": T(0, 0)},
        {"tenant": "t", "request_id": "c1-05", "op": "object.get", "object_id": "app", "at": T(1, 0)},
        {"tenant": "t", "request_id": "c1-06", "op": "plan.create", "planned_at": T(1, 0)},
        {"tenant": "t", "request_id": "c1-07", "op": "plan.apply", "plan_id": "p000001", "applied_at": T(1, 30)},
        {"tenant": "t", "request_id": "c1-08", "op": "object.update", "object_id": "db", "baseline": [], "at": T(1, 45)},
        {"tenant": "t", "request_id": "c1-09", "op": "plan.create", "planned_at": T(2, 0)},
        {"tenant": "t", "request_id": "c1-10", "op": "plan.apply", "plan_id": "p000002", "applied_at": T(2, 10)},
        {"tenant": "t", "request_id": "c1-11", "op": "object.get", "object_id": "app", "at": T(2, 20)},
    ],
    [
        {"ok": True},
        {"ok": True},
        {"ok": True},
        {"ok": True},
        {"ok": True, "equals": {"object.target": ["app.read", "app.write"]}},
        {"ok": True, "equals": {"summary.add_count": 1, "summary.remove_count": 0}},
        {"ok": True},
        {"ok": True},
        # The lease ends exactly at 02:00, so the target at the planning
        # instant no longer contains app.write; db.read drops with it.
        {"ok": True, "equals": {"summary.remove_count": 2}},
        {"ok": True, "equals": {"events.0.object_id": "app", "events.1.object_id": "db"}},
        {"ok": True, "equals": {"object.target": ["app.read"]}},
    ],
)

case(
    "stale-on-expiry",
    "case2.sqlite3",
    [
        {"tenant": "t", "request_id": "c2-01", "op": "object.create", "object_id": "tmp", "at": T(0, 0)},
        {"tenant": "t", "request_id": "c2-02", "op": "lease.grant", "lease_id": "L", "object_id": "tmp", "permissions": ["run"], "start": T(0, 0), "end": T(1, 0), "at": T(0, 0)},
        {"tenant": "t", "request_id": "c2-03", "op": "plan.create", "planned_at": T(0, 30), "objects": ["tmp"]},
        {"tenant": "t", "request_id": "c2-04", "op": "plan.apply", "plan_id": "p000001", "applied_at": T(1, 0)},
        {"tenant": "t", "request_id": "c2-05", "op": "object.get", "object_id": "tmp", "at": T(1, 0)},
    ],
    [
        {"ok": True},
        {"ok": True},
        {"ok": True, "equals": {"additions": [{"object_id": "tmp", "permission": "run"}]}},
        {
            "ok": False,
            "code": "stale",
            "equals": {
                "detail.applied": False,
                "detail.reasons.0.kind": "target_changed",
                "detail.reasons.0.expired_leases.0.lease_id": "L",
            },
        },
        {"ok": True, "equals": {"object.materialized": []}},
    ],
)

case(
    "stale-on-recreate",
    "case3.sqlite3",
    [
        {"tenant": "t", "request_id": "c3-01", "op": "object.create", "object_id": "a", "baseline": ["read"], "at": T(0, 0)},
        {"tenant": "t", "request_id": "c3-02", "op": "object.recreate", "object_id": "a", "baseline": ["read"], "at": T(0, 10)},
        {"tenant": "t", "request_id": "c3-03", "op": "plan.create", "planned_at": T(0, 20), "objects": ["a"]},
        {"tenant": "t", "request_id": "c3-04", "op": "object.recreate", "object_id": "a", "baseline": ["read"], "at": T(0, 30)},
        {"tenant": "t", "request_id": "c3-05", "op": "plan.apply", "plan_id": "p000001", "applied_at": T(0, 40)},
    ],
    [
        {"ok": True},
        {"ok": True},
        {"ok": True},
        {"ok": True},
        {"ok": False, "code": "stale", "equals": {"detail.reasons.0.kind": "object_recreated"}},
    ],
)

case(
    "idempotency",
    "case4.sqlite3",
    [
        {"tenant": "t", "request_id": "c4-01", "op": "object.create", "object_id": "x", "baseline": ["r"], "at": T(0, 0)},
        {"tenant": "t", "request_id": "c4-01", "op": "object.create", "object_id": "x", "baseline": ["r"], "at": T(0, 0)},
        {"tenant": "t", "request_id": "c4-01", "op": "object.create", "object_id": "x", "baseline": ["w"], "at": T(0, 0)},
        {"tenant": "t", "request_id": "c4-02", "op": "object.create", "object_id": "x", "baseline": ["r"], "at": T(0, 0)},
        {"tenant": "t", "request_id": "c4-03", "op": "object.get", "object_id": "x", "at": T(0, 0)},
    ],
    [
        {"ok": True},
        {"ok": True, "equals": {"object.object_version": 1}},
        {"ok": False, "code": "request_id_conflict"},
        {"ok": False, "code": "already_exists"},
        {"ok": True, "equals": {"object.object_version": 1}},
    ],
)

case(
    "exact-expiry-and-overlap",
    "case5.sqlite3",
    [
        {"tenant": "t", "request_id": "c5-01", "op": "object.create", "object_id": "o", "baseline": ["keep"], "at": T(0, 0)},
        {"tenant": "t", "request_id": "c5-02", "op": "lease.grant", "lease_id": "LT", "object_id": "o", "permissions": ["keep", "temp"], "start": T(0, 0), "end": T(1, 0), "at": T(0, 0)},
        {"tenant": "t", "request_id": "c5-03", "op": "object.get", "object_id": "o", "at": T(0, 59, 59)},
        {"tenant": "t", "request_id": "c5-04", "op": "object.get", "object_id": "o", "at": T(1, 0)},
        {"tenant": "t", "request_id": "c5-05", "op": "lease.revoke", "lease_id": "LT", "at": T(1, 1)},
        {"tenant": "t", "request_id": "c5-06", "op": "object.get", "object_id": "o", "at": T(1, 2)},
    ],
    [
        {"ok": True},
        {"ok": True},
        {"ok": True, "equals": {"object.target": ["keep", "temp"]}},
        {"ok": True, "equals": {"object.target": ["keep"]}},
        {"ok": True},
        {"ok": True, "equals": {"object.target": ["keep"]}},
    ],
)

case(
    "attribute-reference",
    "case6.sqlite3",
    [
        {"tenant": "t", "request_id": "c6-01", "op": "object.create", "object_id": "v", "attributes": {"tier": "gold"}, "at": T(0, 0)},
        {"tenant": "t", "request_id": "c6-02", "op": "lease.grant", "lease_id": "LV", "object_id": "v", "permissions": ["admin"], "attributes": {"tier": "gold"}, "start": T(0, 0), "end": T(2, 0), "at": T(0, 0)},
        {"tenant": "t", "request_id": "c6-03", "op": "object.get", "object_id": "v", "at": T(0, 30)},
        {"tenant": "t", "request_id": "c6-04", "op": "object.update", "object_id": "v", "attributes": {"tier": "silver"}, "at": T(1, 0)},
        {"tenant": "t", "request_id": "c6-05", "op": "object.get", "object_id": "v", "at": T(1, 30)},
    ],
    [
        {"ok": True},
        {"ok": True},
        {"ok": True, "equals": {"object.target": ["admin"]}},
        {"ok": True},
        {"ok": True, "equals": {"object.target": []}},
    ],
)

case(
    "cycle-rejected",
    "case7.sqlite3",
    [
        {"tenant": "t", "request_id": "c7-01", "op": "object.create", "object_id": "a", "at": T(0, 0)},
        {"tenant": "t", "request_id": "c7-02", "op": "object.create", "object_id": "b", "at": T(0, 0)},
        {"tenant": "t", "request_id": "c7-03", "op": "dependency.add", "from_object": "a", "from_permission": "p", "to_object": "b", "to_permission": "q", "at": T(0, 0)},
        {"tenant": "t", "request_id": "c7-04", "op": "dependency.add", "from_object": "b", "from_permission": "q", "to_object": "a", "to_permission": "p", "at": T(0, 0)},
    ],
    [
        {"ok": True},
        {"ok": True},
        {"ok": True},
        {"ok": False, "code": "dependency_cycle"},
    ],
)

# 51 objects, each created with a permission and then stripped of it, so the
# plan would have to adjust 51 objects — one over the limit.
_requests = []
for index in range(51):
    _requests.append(
        {
            "tenant": "t",
            "request_id": f"c8-{index:02d}a",
            "op": "object.create",
            "object_id": f"z{index:02d}",
            "baseline": ["keep"],
            "at": T(0, 0),
        }
    )
    _requests.append(
        {
            "tenant": "t",
            "request_id": f"c8-{index:02d}b",
            "op": "object.update",
            "object_id": f"z{index:02d}",
            "baseline": [],
            "at": T(0, 10),
        }
    )
_requests.append(
    {"tenant": "t", "request_id": "c8-plan", "op": "plan.create", "planned_at": T(0, 30)}
)
case(
    "plan-limit",
    "case8.sqlite3",
    _requests,
    [{"ok": True}] * 102 + [{"ok": False, "code": "plan_limit_exceeded"}],
)

pathlib.Path("cases.json").write_text(
    json.dumps({"cases": cases}, indent=2) + "\n", encoding="utf-8"
)
print("cases", len(cases), "requests", sum(len(item["requests"]) for item in cases))
