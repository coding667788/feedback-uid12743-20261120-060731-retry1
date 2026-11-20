"""Independent reference model of the target semantics.

Not imported by ``solution``.  It re-derives the expected target from the
declarative inputs alone (baseline, lease intervals, attribute references,
dependency edges) using a *recursive prerequisite walk*, i.e. a different
algorithm from the implementation's iterative blocking loop.  Differential
tests over randomised cases check the two agree.

Interval convention matches the README: leases are half-open ``[start, end)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RefObject:
    generation: int = 1
    baseline: set[str] = field(default_factory=set)
    attributes: dict[str, object] = field(default_factory=dict)
    deleted: bool = False


@dataclass
class RefLease:
    lease_id: str
    object_id: str
    generation: int
    permissions: set[str]
    attributes: dict[str, object]
    start: int
    end: int
    revoked: bool = False


def sources_at(
    objects: dict[str, RefObject],
    leases: dict[str, RefLease],
    object_id: str,
    t: int,
) -> dict[str, list[str]]:
    """Permission -> source labels ("baseline", "lease:<id>") at instant t."""

    obj = objects.get(object_id)
    result: dict[str, list[str]] = {}
    if obj is None or obj.deleted:
        return result
    for permission in obj.baseline:
        result.setdefault(permission, []).append("baseline")
    for lease in sorted(leases.values(), key=lambda item: item.lease_id):
        if lease.object_id != object_id:
            continue
        if lease.revoked or lease.generation != obj.generation:
            continue
        if not (lease.start <= t < lease.end):
            continue
        if any(obj.attributes.get(k) != v for k, v in lease.attributes.items()):
            continue
        for permission in lease.permissions:
            result.setdefault(permission, []).append("lease:" + lease.lease_id)
    for labels in result.values():
        labels.sort()
    return result


def target(
    objects: dict[str, RefObject],
    leases: dict[str, RefLease],
    edges: list[tuple[str, str, str, str]],
    object_id: str,
    t: int,
) -> set[str]:
    """Independent target of one object at instant ``t``.

    ``wanted`` restricts the walk to permissions the object actually has a
    source for.  A cycle makes the node unsatisfiable (both sides drop), which
    matches the documented "dependencies form a DAG" precondition.
    """

    prerequisites: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for from_object, from_permission, to_object, to_permission in edges:
        prerequisites.setdefault((from_object, from_permission), []).append(
            (to_object, to_permission)
        )
    memo: dict[tuple[str, str], bool] = {}

    def kept(node: tuple[str, str], stack: tuple[tuple[str, str], ...]) -> bool:
        if node in memo:
            return memo[node]
        object_key, permission = node
        if permission not in sources_at(objects, leases, object_key, t):
            memo[node] = False
            return False
        if node in stack:
            return False
        okay = all(
            kept(prerequisite, stack + (node,))
            for prerequisite in prerequisites.get(node, [])
        )
        memo[node] = okay
        return okay

    wanted = sources_at(objects, leases, object_id, t)
    return {permission for permission in wanted if kept((object_id, permission), ())}


def global_target(
    objects: dict[str, RefObject],
    leases: dict[str, RefLease],
    edges: list[tuple[str, str, str, str]],
    t: int,
) -> dict[str, set[str]]:
    return {
        object_id: target(objects, leases, edges, object_id, t)
        for object_id in objects
        if not objects[object_id].deleted
    }


def assert_dependency_consistent(
    case,
    targets: dict[str, set[str]],
    edges: list[tuple[str, str, str, str]],
) -> None:
    """Assert the invariant directly, without recomputing any target."""

    for from_object, from_permission, to_object, to_permission in edges:
        if from_permission in targets.get(from_object, set()):
            case.assertIn(
                to_permission,
                targets.get(to_object, set()),
                f"{from_object}:{from_permission} kept without "
                f"{to_object}:{to_permission}",
            )
