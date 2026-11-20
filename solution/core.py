"""Core coordinator: state model, target computation, plans and application.

Semantics in one paragraph
--------------------------
An object has a *generation* (bumped only when it is recreated), an *object
version* (bumped by every state change), a *baseline* permission set,
*attributes* and a *materialized* permission set (what is currently held).
A lease grants permissions for a half-open interval and is effective only
while it is not revoked, while its generation still matches the object, and
while every attribute it references still equals the object's value.  The
*target* of an object is the union of its baseline and its effective leases,
pruned until every remaining permission has all of its dependency
prerequisites.  Plans turn that target into ordered removals (downstream
first) and additions (upstream first), and applications revalidate the
planning basis inside the writing transaction before touching anything.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .clock import InstantError, format_instant, parse_instant
from .crashpoint import crash_point
from .errors import ReclaimError
from .gates import gate
from .store import Store

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_PERMISSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.*:-]{0,127}$")
MAX_PLAN_OBJECTS = 50

_OPS = {
    "object.create",
    "object.recreate",
    "object.update",
    "object.delete",
    "object.get",
    "lease.grant",
    "lease.revoke",
    "dependency.add",
    "dependency.remove",
    "plan.create",
    "plan.apply",
    "state.export",
}

_OP_FIELDS = {
    "object.create": {"object_id", "at", "baseline", "attributes"},
    "object.recreate": {"object_id", "at", "baseline", "attributes"},
    "object.update": {"object_id", "at", "baseline", "attributes"},
    "object.delete": {"object_id", "at"},
    "object.get": {"object_id", "at"},
    "lease.grant": {
        "lease_id",
        "object_id",
        "permissions",
        "attributes",
        "start",
        "end",
        "at",
    },
    "lease.revoke": {"lease_id", "at"},
    "dependency.add": {
        "from_object",
        "from_permission",
        "to_object",
        "to_permission",
        "at",
    },
    "dependency.remove": {
        "from_object",
        "from_permission",
        "to_object",
        "to_permission",
        "at",
    },
    "plan.create": {"planned_at", "objects"},
    "plan.apply": {"plan_id", "applied_at"},
    "state.export": {"at"},
}


# --------------------------------------------------------------------------
# validation helpers


def _require(data: dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ReclaimError("invalid_argument", f"missing field {key!r}", {"field": key})
    return data[key]


def _as_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ReclaimError(
            "invalid_argument",
            f"{field} must match {_ID_RE.pattern}",
            {"field": field},
        )
    return value


def _as_permission(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _PERMISSION_RE.match(value):
        raise ReclaimError(
            "invalid_argument",
            f"{field} must match {_PERMISSION_RE.pattern}",
            {"field": field},
        )
    return value


def _as_permission_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReclaimError("invalid_argument", f"{field} must be a list", {"field": field})
    result = [_as_permission(item, field) for item in value]
    if len(set(result)) != len(result):
        raise ReclaimError(
            "invalid_argument", f"{field} contains duplicates", {"field": field}
        )
    return sorted(result)


def _as_attributes(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ReclaimError(
            "invalid_argument", f"{field} must be an object", {"field": field}
        )
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ReclaimError(
                "invalid_argument",
                f"{field} keys must be non-empty strings",
                {"field": field},
            )
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise ReclaimError(
                "invalid_argument",
                f"{field}.{key} must be a string, number or boolean",
                {"field": f"{field}.{key}"},
            )
        result[key] = item
    return dict(sorted(result.items()))


def _as_instant(data: dict[str, Any], key: str) -> tuple[int, str]:
    raw = _require(data, key)
    try:
        return parse_instant(raw), raw
    except InstantError as exc:
        raise ReclaimError("invalid_argument", f"{key}: {exc}", {"field": key}) from exc


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_params(request: dict[str, Any]) -> str:
    """Stable digest of the request without its id, for idempotency checks.

    Robust against values that plain JSON cannot encode (a caller may hand us
    an arbitrary Python object): those fall back to their ``repr`` so the
    digest still exists and a mismatch is still detected.
    """

    payload = {key: value for key, value in request.items() if key != "request_id"}
    try:
        text = _canonical(payload)
    except (TypeError, ValueError):
        text = json.dumps(payload, sort_keys=True, default=repr)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# in-memory rows


@dataclass
class ObjectRow:
    object_id: str
    generation: int
    object_version: int
    deleted: bool
    baseline: list[str]
    attributes: dict[str, Any]
    materialized: list[str]

    @classmethod
    def from_row(cls, row: Any) -> "ObjectRow":
        return cls(
            object_id=row["object_id"],
            generation=row["generation"],
            object_version=row["object_version"],
            deleted=bool(row["deleted"]),
            baseline=json.loads(row["baseline"]),
            attributes=json.loads(row["attributes"]),
            materialized=json.loads(row["materialized"]),
        )

    def public(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "generation": self.generation,
            "object_version": self.object_version,
            "deleted": self.deleted,
            "baseline": list(self.baseline),
            "attributes": dict(self.attributes),
            "materialized": list(self.materialized),
        }


@dataclass
class LeaseRow:
    lease_id: str
    object_id: str
    generation: int
    permissions: list[str]
    attribute_refs: dict[str, Any]
    start_us: int
    end_us: int
    version: int
    revoked: bool

    @classmethod
    def from_row(cls, row: Any) -> "LeaseRow":
        return cls(
            lease_id=row["lease_id"],
            object_id=row["object_id"],
            generation=row["generation"],
            permissions=json.loads(row["permissions"]),
            attribute_refs=json.loads(row["attribute_refs"]),
            start_us=row["start_us"],
            end_us=row["end_us"],
            version=row["version"],
            revoked=bool(row["revoked"]),
        )

    def inactive_reason(self, obj: ObjectRow | None, at_us: int) -> str | None:
        """Why this lease is not effective at ``at_us``; ``None`` if it is.

        Half-open intervals: at ``end_us`` exactly the lease is already
        inactive, and that is reported as ``expired`` (not "not started").
        """

        if obj is None or obj.deleted:
            return "object_absent"
        if self.revoked:
            return "revoked"
        if self.generation != obj.generation:
            return "object_recreated"
        if at_us < self.start_us:
            return "not_started"
        if at_us >= self.end_us:
            return "expired"
        for key, expected in self.attribute_refs.items():
            if key not in obj.attributes or obj.attributes[key] != expected:
                return "attribute_mismatch"
        return None

    def active_at(self, obj: ObjectRow | None, at_us: int) -> bool:
        return self.inactive_reason(obj, at_us) is None

    def public(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "object_id": self.object_id,
            "generation": self.generation,
            "permissions": list(self.permissions),
            "attributes": dict(self.attribute_refs),
            "start": format_instant(self.start_us),
            "end": format_instant(self.end_us),
            "version": self.version,
            "revoked": self.revoked,
        }


@dataclass(frozen=True)
class Edge:
    from_object: str
    from_permission: str
    to_object: str
    to_permission: str
    version: int
    removed: bool

    @property
    def dependent(self) -> tuple[str, str]:
        return (self.from_object, self.from_permission)

    @property
    def prerequisite(self) -> tuple[str, str]:
        return (self.to_object, self.to_permission)

    @property
    def key(self) -> str:
        return "|".join(
            (self.from_object, self.from_permission, self.to_object, self.to_permission)
        )

    def public(self) -> dict[str, Any]:
        return {
            "from_object": self.from_object,
            "from_permission": self.from_permission,
            "to_object": self.to_object,
            "to_permission": self.to_permission,
            "version": self.version,
        }


def _node_key(node: tuple[str, str]) -> str:
    return f"{node[0]}\x1f{node[1]}"


# --------------------------------------------------------------------------
# coordinator


class Coordinator:
    def __init__(self, db_path: str):
        self.store = Store(db_path)

    def close(self) -> None:
        self.store.close()

    # -- public request entry ------------------------------------------------

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Execute one request and always return a response dictionary.

        A well formed envelope (tenant, request_id, op, fields) yields a
        persisted receipt even when the business operation fails — that is
        how an identical retry observes the same business failure instead of
        running the operation again.  A malformed envelope has no trustworthy
        request identity, so it is answered without persistence.
        """

        try:
            return self._handle(request)
        except ReclaimError as exc:
            return exc.response()

    def _handle(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ReclaimError("invalid_request", "request must be an object")
        tenant = request.get("tenant")
        request_id = request.get("request_id")
        if not isinstance(tenant, str) or not _ID_RE.match(tenant):
            raise ReclaimError("invalid_request", "tenant is required and must be an id")
        if not isinstance(request_id, str) or not _ID_RE.match(request_id):
            raise ReclaimError(
                "invalid_request", "request_id is required and must be an id"
            )

        params_hash = _hash_params(request)
        cached = self._lookup_receipt(tenant, request_id)
        if cached is not None:
            return self._replay(cached, params_hash)

        op = request.get("op")
        known_op = isinstance(op, str) and op in _OPS
        if known_op:
            # Cross-process interleaving gate: deliberately outside the write
            # transaction so another process can commit before this one starts.
            gate("before:" + op)

        try:
            with self.store.write_transaction():
                cached = self._lookup_receipt(tenant, request_id)
                if cached is not None:
                    return self._replay(cached, params_hash)
                if not known_op:
                    raise ReclaimError("unknown_op", f"unknown op: {op!r}", {"op": op})
                allowed = _OP_FIELDS[op] | {"tenant", "request_id", "op"}
                extra = sorted(set(request) - allowed)
                if extra:
                    raise ReclaimError(
                        "invalid_argument",
                        f"unexpected fields for {op}: {extra}",
                        {"fields": extra},
                    )
                handler = getattr(self, "_op_" + op.replace(".", "_"))
                response = handler(tenant, request)
                self._store_receipt(tenant, request_id, op, params_hash, response)
                crash_point("request_before_commit")
        except ReclaimError as exc:
            response = exc.response()
            with self.store.write_transaction():
                cached = self._lookup_receipt(tenant, request_id)
                if cached is not None:
                    # A concurrent twin won the race; its result is the
                    # persisted first result.
                    return self._replay(cached, params_hash)
                stored_op = op if isinstance(op, str) else ""
                self._store_receipt(
                    tenant, request_id, stored_op, params_hash, response
                )
        crash_point("request_after_commit")
        return response

    def _lookup_receipt(self, tenant: str, request_id: str) -> Any:
        return self.store.conn.execute(
            "SELECT * FROM receipts WHERE tenant = ? AND request_id = ?",
            (tenant, request_id),
        ).fetchone()

    def _replay(self, row: Any, params_hash: str) -> dict[str, Any]:
        if row["params_hash"] != params_hash:
            return {
                "ok": False,
                "code": "request_id_conflict",
                "message": "request_id was already used with different parameters",
                "detail": {"request_id": row["request_id"], "op": row["op"]},
            }
        return json.loads(row["response"])

    def _store_receipt(
        self,
        tenant: str,
        request_id: str,
        op: str,
        params_hash: str,
        response: dict[str, Any],
    ) -> None:
        self.store.conn.execute(
            "INSERT INTO receipts (tenant, request_id, op, params_hash, response, created_seq)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                tenant,
                request_id,
                op,
                params_hash,
                _canonical(response),
                self.store.next_event_seq(),
            ),
        )

    # -- reading helpers -----------------------------------------------------

    def _object(self, tenant: str, object_id: str) -> ObjectRow | None:
        row = self.store.conn.execute(
            "SELECT * FROM objects WHERE tenant = ? AND object_id = ?",
            (tenant, object_id),
        ).fetchone()
        if row is None or row["deleted"]:
            return None
        return ObjectRow.from_row(row)

    def _object_any(self, tenant: str, object_id: str) -> ObjectRow | None:
        row = self.store.conn.execute(
            "SELECT * FROM objects WHERE tenant = ? AND object_id = ?",
            (tenant, object_id),
        ).fetchone()
        if row is None or row["deleted"]:
            return None
        return ObjectRow.from_row(row)

    def _object_row_any(self, tenant: str, object_id: str) -> ObjectRow | None:
        """The stored row even if deleted; used to preserve generation history."""

        row = self.store.conn.execute(
            "SELECT * FROM objects WHERE tenant = ? AND object_id = ?",
            (tenant, object_id),
        ).fetchone()
        return None if row is None else ObjectRow.from_row(row)

    def _objects(self, tenant: str) -> dict[str, ObjectRow]:
        rows = self.store.conn.execute(
            "SELECT * FROM objects WHERE tenant = ? AND deleted = 0 ORDER BY object_id",
            (tenant,),
        ).fetchall()
        return {row["object_id"]: ObjectRow.from_row(row) for row in rows}

    def _leases_for(
        self, tenant: str, object_ids: Iterable[str] | None = None
    ) -> list[LeaseRow]:
        if object_ids is None:
            rows = self.store.conn.execute(
                "SELECT * FROM leases WHERE tenant = ? ORDER BY lease_id", (tenant,)
            ).fetchall()
        else:
            ids = sorted(set(object_ids))
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            rows = self.store.conn.execute(
                f"SELECT * FROM leases WHERE tenant = ? AND object_id IN ({marks})"
                " ORDER BY lease_id",
                (tenant, *ids),
            ).fetchall()
        return [LeaseRow.from_row(row) for row in rows]

    def _lease(self, tenant: str, lease_id: str) -> LeaseRow | None:
        row = self.store.conn.execute(
            "SELECT * FROM leases WHERE tenant = ? AND lease_id = ?",
            (tenant, lease_id),
        ).fetchone()
        return None if row is None else LeaseRow.from_row(row)

    def _edges(
        self,
        tenant: str,
        object_ids: Iterable[str] | None = None,
        include_removed: bool = False,
    ) -> list[Edge]:
        sql = "SELECT * FROM dependencies WHERE tenant = ?"
        params: list[Any] = [tenant]
        if object_ids is not None:
            ids = sorted(set(object_ids))
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            sql += f" AND (from_object IN ({marks}) OR to_object IN ({marks}))"
            params.extend(ids)
            params.extend(ids)
        if not include_removed:
            sql += " AND removed = 0"
        sql += " ORDER BY from_object, from_permission, to_object, to_permission"
        rows = self.store.conn.execute(sql, params).fetchall()
        return [
            Edge(
                from_object=row["from_object"],
                from_permission=row["from_permission"],
                to_object=row["to_object"],
                to_permission=row["to_permission"],
                version=row["version"],
                removed=bool(row["removed"]),
            )
            for row in rows
        ]

    def _edge_exists(self, tenant: str, edge_key: tuple[str, str, str, str]) -> bool:
        row = self.store.conn.execute(
            "SELECT removed FROM dependencies WHERE tenant = ? AND from_object = ?"
            " AND from_permission = ? AND to_object = ? AND to_permission = ?",
            (tenant, *edge_key),
        ).fetchone()
        return row is not None and not row["removed"]

    # -- target computation --------------------------------------------------

    def _raw_sources(
        self,
        obj: ObjectRow,
        leases: list[LeaseRow],
        at_us: int,
    ) -> dict[str, list[dict[str, Any]]]:
        """Union of baseline and effective leases, with per-source provenance."""

        sources: dict[str, list[dict[str, Any]]] = {}
        for permission in obj.baseline:
            sources.setdefault(permission, []).append({"kind": "baseline"})
        for lease in leases:
            if lease.object_id != obj.object_id or not lease.active_at(obj, at_us):
                continue
            for permission in lease.permissions:
                sources.setdefault(permission, []).append(
                    {
                        "kind": "lease",
                        "lease_id": lease.lease_id,
                        "version": lease.version,
                        "start": format_instant(lease.start_us),
                        "end": format_instant(lease.end_us),
                        "attributes": dict(lease.attribute_refs),
                    }
                )
        for items in sources.values():
            items.sort(
                key=lambda item: (item["kind"] != "baseline", item.get("lease_id", ""))
            )
        return sources

    def _prune(
        self,
        sources: dict[str, dict[str, list[dict[str, Any]]]],
        edges: list[Edge],
    ) -> dict[str, set[str]]:
        """Greatest fixed point where every kept permission has its prereqs kept.

        ``sources`` maps object id -> permission -> provenance list; the
        returned mapping holds only permissions that survive.  Pruning is
        monotone, so starting from "everything sourced" and discarding blocked
        permissions converges to the greatest fixed point.
        """

        keep: dict[str, set[str]] = {
            object_id: set(permissions) for object_id, permissions in sources.items()
        }
        prerequisites: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for edge in edges:
            if edge.removed:
                continue
            prerequisites.setdefault(edge.dependent, []).append(edge.prerequisite)

        changed = True
        while changed:
            changed = False
            for object_id in sorted(keep):
                for permission in sorted(keep[object_id]):
                    for target_object, target_permission in prerequisites.get(
                        (object_id, permission), ()
                    ):
                        if target_permission not in keep.get(target_object, set()):
                            keep[object_id].discard(permission)
                            changed = True
                            break

        return keep

    def _compute_state(
        self,
        tenant: str,
        at_us: int,
        object_ids: Iterable[str] | None = None,
        pending: ObjectRow | None = None,
    ) -> tuple[
        dict[str, ObjectRow],
        dict[str, set[str]],
        dict[str, dict[str, list[dict[str, Any]]]],
        list[Edge],
    ]:
        """Compute the target for the requested objects.

        The dependency closure is followed so that prerequisites are
        evaluated against their own sources, not against an empty set.  A
        ``pending`` object (being created or recreated) participates as if it
        were stored; its leases are the stored ones, filtered by generation.

        ``sources`` holds only the *currently effective* sources; the
        explanation helper adds inactive lease records separately.
        """

        stored = self._objects(tenant)
        objects = dict(stored)
        if pending is not None:
            objects[pending.object_id] = pending
        if object_ids is not None:
            wanted = set(object_ids)
            objects = {key: value for key, value in objects.items() if key in wanted}

        edges = self._edges(tenant)
        closure = set(objects)
        changed = True
        while changed:
            changed = False
            for edge in edges:
                if edge.removed:
                    continue
                if edge.from_object in closure and edge.to_object not in closure:
                    closure.add(edge.to_object)
                    changed = True
                    if edge.to_object in stored:
                        objects[edge.to_object] = stored[edge.to_object]

        leases = self._leases_for(tenant, closure)
        by_object: dict[str, list[LeaseRow]] = {}
        for lease in leases:
            by_object.setdefault(lease.object_id, []).append(lease)

        sources: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for object_id, obj in objects.items():
            sources[object_id] = self._raw_sources(obj, by_object.get(object_id, []), at_us)

        relevant = [
            edge
            for edge in edges
            if not edge.removed
            and (edge.from_object in closure or edge.to_object in closure)
        ]
        keep = self._prune(sources, relevant)
        return objects, keep, sources, relevant

    def _target_explanation(
        self,
        tenant: str,
        obj: ObjectRow,
        keep: set[str],
        sources: dict[str, list[dict[str, Any]]],
        edges: list[Edge],
        keep_by_object: dict[str, set[str]],
        at_us: int,
    ) -> dict[str, Any]:
        """Per-permission provenance and, for blocked permissions, the cause.

        Every permission ever seen on the object — held now, sourced, or
        merely materialised — gets an entry, so a caller can explain why a
        permission is kept and through which sources, or exactly which
        prerequisite is missing.  When one permission has several sources
        (baseline plus one or more leases) they are listed one by one.
        """

        prerequisites: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for edge in edges:
            if edge.removed:
                continue
            prerequisites.setdefault(edge.dependent, []).append(edge.prerequisite)

        # Include every permission this object has ever been offered, so an
        # expired or generation-stale lease is explained rather than hidden.
        # Each inactive lease is appended to that permission's source list
        # with its own ``inactive_reason`` so the list stays exhaustive.
        offered = set(sources) | set(obj.materialized)
        inactive: dict[str, list[dict[str, Any]]] = {}
        for lease in self._leases_for(tenant, [obj.object_id]):
            offered.update(lease.permissions)
            reason = lease.inactive_reason(obj, at_us)
            if reason is None:
                continue
            for permission in lease.permissions:
                inactive.setdefault(permission, []).append(
                    {
                        "kind": "lease",
                        "lease_id": lease.lease_id,
                        "version": lease.version,
                        "start": format_instant(lease.start_us),
                        "end": format_instant(lease.end_us),
                        "attributes": dict(lease.attribute_refs),
                        "active": False,
                        "inactive_reason": reason,
                    }
                )

        result: dict[str, Any] = {}
        for permission in sorted(offered):
            kept = permission in keep
            entry: dict[str, Any] = {
                "kept": kept,
                "sources": [
                    dict(item, active=True) for item in sources.get(permission, [])
                ]
                + sorted(
                    inactive.get(permission, []), key=lambda item: item["lease_id"]
                ),
            }
            if not kept:
                missing = [
                    {"object_id": target_object, "permission": target_permission}
                    for target_object, target_permission in sorted(
                        prerequisites.get((obj.object_id, permission), [])
                    )
                    if target_permission not in keep_by_object.get(target_object, set())
                ]
                entry["missing_prerequisites"] = missing
                if not sources.get(permission):
                    entry["reason"] = "no_active_source"
                else:
                    entry["reason"] = "missing_prerequisite"
            result[permission] = entry
        return result

    # -- ordering ------------------------------------------------------------

    @staticmethod
    def _order_nodes(
        nodes: Iterable[tuple[str, str]],
        edges: list[Edge],
        prereq_first: bool,
    ) -> list[tuple[str, str]]:
        import heapq

        node_set = set(nodes)
        successors: dict[tuple[str, str], set[tuple[str, str]]] = {
            node: set() for node in node_set
        }
        for edge in edges:
            if edge.removed:
                continue
            dependent = edge.dependent
            prerequisite = edge.prerequisite
            if dependent in node_set and prerequisite in node_set:
                if prereq_first:
                    successors[prerequisite].add(dependent)
                else:
                    successors[dependent].add(prerequisite)
        indegree = {node: 0 for node in node_set}
        for targets in successors.values():
            for target in targets:
                indegree[target] += 1
        ready = [(_node_key(node), node) for node in node_set if indegree[node] == 0]
        heapq.heapify(ready)
        ordered: list[tuple[str, str]] = []
        while ready:
            _, node = heapq.heappop(ready)
            ordered.append(node)
            for successor in sorted(successors[node], key=_node_key):
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    heapq.heappush(ready, (_node_key(successor), successor))
        if len(ordered) != len(node_set):
            raise ReclaimError("dependency_cycle", "dependency graph contains a cycle")
        return ordered

    # -- object operations ---------------------------------------------------

    def _op_object_create(self, tenant: str, request: dict[str, Any]) -> dict[str, Any]:
        object_id = _as_id(_require(request, "object_id"), "object_id")
        at_us, at_raw = _as_instant(request, "at")
        baseline = _as_permission_list(request.get("baseline", []), "baseline")
        attributes = _as_attributes(request.get("attributes", {}), "attributes")
        if self._object_any(tenant, object_id) is not None:
            raise ReclaimError(
                "already_exists",
                f"object already exists: {object_id}",
                {"object_id": object_id},
            )
        existing = self._object_row_any(tenant, object_id)
        generation = 1 if existing is None else existing.generation + 1
        pending = ObjectRow(
            object_id=object_id,
            generation=generation,
            object_version=1,
            deleted=False,
            baseline=baseline,
            attributes=attributes,
            materialized=[],
        )
        _, keep, _, _ = self._compute_state(tenant, at_us, [object_id], pending)
        materialized = sorted(keep.get(object_id, set()))
        if existing is None:
            self.store.conn.execute(
                "INSERT INTO objects (tenant, object_id, generation, object_version, deleted,"
                " baseline, attributes, materialized, created_seq)"
                " VALUES (?, ?, ?, 1, 0, ?, ?, ?, ?)",
                (
                    tenant,
                    object_id,
                    generation,
                    _canonical(baseline),
                    _canonical(attributes),
                    _canonical(materialized),
                    self.store.next_event_seq(),
                ),
            )
        else:
            # Re-creating a deleted id keeps its generation history, so an old
            # plan that recorded generation N is stale rather than applicable.
            self.store.conn.execute(
                "UPDATE objects SET generation = ?, object_version = 1, deleted = 0,"
                " baseline = ?, attributes = ?, materialized = ?, created_seq = ?"
                " WHERE tenant = ? AND object_id = ?",
                (
                    generation,
                    _canonical(baseline),
                    _canonical(attributes),
                    _canonical(materialized),
                    self.store.next_event_seq(),
                    tenant,
                    object_id,
                ),
            )
        return {
            "ok": True,
            "op": "object.create",
            "at": at_raw,
            "object": {
                "object_id": object_id,
                "generation": generation,
                "object_version": 1,
                "baseline": baseline,
                "attributes": attributes,
                "materialized": materialized,
            },
        }

    def _op_object_recreate(self, tenant: str, request: dict[str, Any]) -> dict[str, Any]:
        object_id = _as_id(_require(request, "object_id"), "object_id")
        at_us, at_raw = _as_instant(request, "at")
        baseline = _as_permission_list(request.get("baseline", []), "baseline")
        attributes = _as_attributes(request.get("attributes", {}), "attributes")
        existing = self._object_any(tenant, object_id)
        if existing is None:
            raise ReclaimError(
                "not_found", f"object not found: {object_id}", {"object_id": object_id}
            )
        generation = existing.generation + 1
        object_version = existing.object_version + 1
        pending = ObjectRow(
            object_id=object_id,
            generation=generation,
            object_version=object_version,
            deleted=False,
            baseline=baseline,
            attributes=attributes,
            materialized=[],
        )
        _, keep, _, _ = self._compute_state(tenant, at_us, [object_id], pending)
        materialized = sorted(keep.get(object_id, set()))
        self.store.conn.execute(
            "UPDATE objects SET generation = ?, object_version = ?, deleted = 0, baseline = ?,"
            " attributes = ?, materialized = ?, created_seq = ?"
            " WHERE tenant = ? AND object_id = ?",
            (
                generation,
                object_version,
                _canonical(baseline),
                _canonical(attributes),
                _canonical(materialized),
                self.store.next_event_seq(),
                tenant,
                object_id,
            ),
        )
        return {
            "ok": True,
            "op": "object.recreate",
            "at": at_raw,
            "object": {
                "object_id": object_id,
                "generation": generation,
                "object_version": object_version,
                "baseline": baseline,
                "attributes": attributes,
                "materialized": materialized,
            },
        }

    def _op_object_update(self, tenant: str, request: dict[str, Any]) -> dict[str, Any]:
        object_id = _as_id(_require(request, "object_id"), "object_id")
        at_us, at_raw = _as_instant(request, "at")
        existing = self._object(tenant, object_id)
        if existing is None:
            raise ReclaimError(
                "not_found", f"object not found: {object_id}", {"object_id": object_id}
            )
        if "baseline" in request:
            baseline = _as_permission_list(request["baseline"], "baseline")
        else:
            baseline = existing.baseline
        if "attributes" in request:
            attributes = _as_attributes(request["attributes"], "attributes")
        else:
            attributes = existing.attributes
        object_version = existing.object_version + 1
        # Baseline is a source, not an immediate grant: an update only bumps
        # the version, the next plan materialises the difference.
        self.store.conn.execute(
            "UPDATE objects SET object_version = ?, baseline = ?, attributes = ?,"
            " created_seq = ? WHERE tenant = ? AND object_id = ?",
            (
                object_version,
                _canonical(baseline),
                _canonical(attributes),
                self.store.next_event_seq(),
                tenant,
                object_id,
            ),
        )
        return {
            "ok": True,
            "op": "object.update",
            "at": at_raw,
            "object": {
                "object_id": object_id,
                "generation": existing.generation,
                "object_version": object_version,
                "baseline": baseline,
                "attributes": attributes,
                "materialized": list(existing.materialized),
            },
        }

    def _op_object_delete(self, tenant: str, request: dict[str, Any]) -> dict[str, Any]:
        object_id = _as_id(_require(request, "object_id"), "object_id")
        at_us, at_raw = _as_instant(request, "at")
        existing = self._object_any(tenant, object_id)
        if existing is None or existing.deleted:
            raise ReclaimError(
                "not_found", f"object not found: {object_id}", {"object_id": object_id}
            )
        self.store.conn.execute(
            "UPDATE objects SET deleted = 1, materialized = '[]',"
            " object_version = object_version + 1, created_seq = ?"
            " WHERE tenant = ? AND object_id = ?",
            (self.store.next_event_seq(), tenant, object_id),
        )
        # A deleted object cannot be a prerequisite, and cannot depend on one.
        # Retire every incident edge with a version bump so plans that recorded
        # it observe a dependency change rather than crashing on a dangling
        # reference.  The rows are kept (``removed = 1``) for auditability.
        incident = self.store.conn.execute(
            "SELECT * FROM dependencies WHERE tenant = ? AND removed = 0"
            " AND (from_object = ? OR to_object = ?)",
            (tenant, object_id, object_id),
        ).fetchall()
        retired = []
        for row in incident:
            version = row["version"] + 1
            self.store.conn.execute(
                "UPDATE dependencies SET removed = 1, version = ?, created_seq = ?"
                " WHERE tenant = ? AND from_object = ? AND from_permission = ?"
                " AND to_object = ? AND to_permission = ?",
                (
                    version,
                    self.store.next_event_seq(),
                    tenant,
                    row["from_object"],
                    row["from_permission"],
                    row["to_object"],
                    row["to_permission"],
                ),
            )
            retired.append(
                {
                    "from_object": row["from_object"],
                    "from_permission": row["from_permission"],
                    "to_object": row["to_object"],
                    "to_permission": row["to_permission"],
                    "version": version,
                }
            )
        return {
            "ok": True,
            "op": "object.delete",
            "at": at_raw,
            "object": {"object_id": object_id, "deleted": True},
            "retired_dependencies": retired,
        }

    def _op_object_get(self, tenant: str, request: dict[str, Any]) -> dict[str, Any]:
        object_id = _as_id(_require(request, "object_id"), "object_id")
        at_us, at_raw = _as_instant(request, "at")
        obj = self._object(tenant, object_id)
        if obj is None:
            raise ReclaimError(
                "not_found", f"object not found: {object_id}", {"object_id": object_id}
            )
        _, keep, sources, edges = self._compute_state(tenant, at_us, [object_id])
        target = keep.get(object_id, set())
        return {
            "ok": True,
            "op": "object.get",
            "at": at_raw,
            "object": {
                **obj.public(),
                "target": sorted(target),
                "explanation": self._target_explanation(
                    tenant, obj, target, sources.get(object_id, {}), edges, keep, at_us
                ),
            },
        }

    # -- leases --------------------------------------------------------------

    def _op_lease_grant(self, tenant: str, request: dict[str, Any]) -> dict[str, Any]:
        lease_id = _as_id(_require(request, "lease_id"), "lease_id")
        object_id = _as_id(_require(request, "object_id"), "object_id")
        permissions = _as_permission_list(
            _require(request, "permissions"), "permissions"
        )
        attribute_refs = _as_attributes(request.get("attributes", {}), "attributes")
        start_us, start_raw = _as_instant(request, "start")
        end_us, end_raw = _as_instant(request, "end")
        at_us, at_raw = _as_instant(request, "at")
        if end_us <= start_us:
            raise ReclaimError(
                "invalid_argument",
                "end must be strictly after start",
                {"start": start_raw, "end": end_raw},
            )
        if not permissions:
            raise ReclaimError("invalid_argument", "permissions must not be empty")
        obj = self._object(tenant, object_id)
        if obj is None:
            raise ReclaimError(
                "not_found", f"object not found: {object_id}", {"object_id": object_id}
            )
        if self._lease(tenant, lease_id) is not None:
            raise ReclaimError(
                "already_exists",
                f"lease already exists: {lease_id}",
                {"lease_id": lease_id},
            )
        self.store.conn.execute(
            "INSERT INTO leases (tenant, lease_id, object_id, generation, permissions,"
            " attribute_refs, start_us, end_us, version, revoked, created_seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)",
            (
                tenant,
                lease_id,
                object_id,
                obj.generation,
                _canonical(permissions),
                _canonical(attribute_refs),
                start_us,
                end_us,
                self.store.next_event_seq(),
            ),
        )
        return {
            "ok": True,
            "op": "lease.grant",
            "at": at_raw,
            "lease": {
                "lease_id": lease_id,
                "object_id": object_id,
                "generation": obj.generation,
                "permissions": permissions,
                "attributes": attribute_refs,
                "start": start_raw,
                "end": end_raw,
                "version": 1,
                "revoked": False,
                "active_at": start_us <= at_us < end_us,
            },
        }

    def _op_lease_revoke(self, tenant: str, request: dict[str, Any]) -> dict[str, Any]:
        lease_id = _as_id(_require(request, "lease_id"), "lease_id")
        at_us, at_raw = _as_instant(request, "at")
        lease = self._lease(tenant, lease_id)
        if lease is None:
            raise ReclaimError(
                "not_found", f"lease not found: {lease_id}", {"lease_id": lease_id}
            )
        if lease.revoked:
            raise ReclaimError(
                "already_exists",
                f"lease already revoked: {lease_id}",
                {"lease_id": lease_id},
            )
        version = lease.version + 1
        self.store.conn.execute(
            "UPDATE leases SET revoked = 1, version = ?, created_seq = ?"
            " WHERE tenant = ? AND lease_id = ?",
            (version, self.store.next_event_seq(), tenant, lease_id),
        )
        return {
            "ok": True,
            "op": "lease.revoke",
            "at": at_raw,
            "lease": {
                "lease_id": lease_id,
                "object_id": lease.object_id,
                "version": version,
                "revoked": True,
            },
        }

    # -- dependencies --------------------------------------------------------

    def _edge_request(
        self, request: dict[str, Any]
    ) -> tuple[str, str, str, str, int, str]:
        from_object = _as_id(_require(request, "from_object"), "from_object")
        from_permission = _as_permission(
            _require(request, "from_permission"), "from_permission"
        )
        to_object = _as_id(_require(request, "to_object"), "to_object")
        to_permission = _as_permission(
            _require(request, "to_permission"), "to_permission"
        )
        at_us, at_raw = _as_instant(request, "at")
        return from_object, from_permission, to_object, to_permission, at_us, at_raw

    def _would_cycle(
        self, tenant: str, from_node: tuple[str, str], to_node: tuple[str, str]
    ) -> bool:
        """True if adding from -> to closes a cycle (i.e. to reaches from)."""

        if from_node == to_node:
            return True
        adjacency: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for edge in self._edges(tenant):
            adjacency.setdefault(edge.dependent, set()).add(edge.prerequisite)
        stack = [to_node]
        seen: set[tuple[str, str]] = set()
        while stack:
            node = stack.pop()
            if node == from_node:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adjacency.get(node, ()))
        return False

    def _op_dependency_add(self, tenant: str, request: dict[str, Any]) -> dict[str, Any]:
        from_object, from_permission, to_object, to_permission, at_us, at_raw = (
            self._edge_request(request)
        )
        for object_id in (from_object, to_object):
            if self._object(tenant, object_id) is None:
                raise ReclaimError(
                    "not_found",
                    f"object not found: {object_id}",
                    {"object_id": object_id},
                )
        key = (from_object, from_permission, to_object, to_permission)
        if self._edge_exists(tenant, key):
            raise ReclaimError(
                "already_exists",
                "dependency already exists",
                {
                    "from_object": from_object,
                    "from_permission": from_permission,
                    "to_object": to_object,
                    "to_permission": to_permission,
                },
            )
        if self._would_cycle(
            tenant, (from_object, from_permission), (to_object, to_permission)
        ):
            raise ReclaimError(
                "dependency_cycle",
                "dependency would close a cycle",
                {
                    "from_object": from_object,
                    "from_permission": from_permission,
                    "to_object": to_object,
                    "to_permission": to_permission,
                },
            )
        version = 1
        existing = self.store.conn.execute(
            "SELECT version FROM dependencies WHERE tenant = ? AND from_object = ?"
            " AND from_permission = ? AND to_object = ? AND to_permission = ?",
            (tenant, *key),
        ).fetchone()
        if existing is not None:
            # Re-adding after a removal bumps the version so stale plans see it.
            version = existing["version"] + 1
            self.store.conn.execute(
                "UPDATE dependencies SET removed = 0, version = ?, created_seq = ?"
                " WHERE tenant = ? AND from_object = ? AND from_permission = ?"
                " AND to_object = ? AND to_permission = ?",
                (version, self.store.next_event_seq(), tenant, *key),
            )
        else:
            self.store.conn.execute(
                "INSERT INTO dependencies (tenant, from_object, from_permission, to_object,"
                " to_permission, version, removed, created_seq)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (tenant, *key, version, self.store.next_event_seq()),
            )
        return {
            "ok": True,
            "op": "dependency.add",
            "at": at_raw,
            "dependency": {
                "from_object": from_object,
                "from_permission": from_permission,
                "to_object": to_object,
                "to_permission": to_permission,
                "version": version,
            },
        }

    def _op_dependency_remove(
        self, tenant: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        from_object, from_permission, to_object, to_permission, at_us, at_raw = (
            self._edge_request(request)
        )
        key = (from_object, from_permission, to_object, to_permission)
        if not self._edge_exists(tenant, key):
            raise ReclaimError(
                "not_found",
                "dependency not found",
                {
                    "from_object": from_object,
                    "from_permission": from_permission,
                    "to_object": to_object,
                    "to_permission": to_permission,
                },
            )
        row = self.store.conn.execute(
            "SELECT version FROM dependencies WHERE tenant = ? AND from_object = ?"
            " AND from_permission = ? AND to_object = ? AND to_permission = ?",
            (tenant, *key),
        ).fetchone()
        version = row["version"] + 1
        self.store.conn.execute(
            "UPDATE dependencies SET removed = 1, version = ?, created_seq = ?"
            " WHERE tenant = ? AND from_object = ? AND from_permission = ?"
            " AND to_object = ? AND to_permission = ?",
            (version, self.store.next_event_seq(), tenant, *key),
        )
        return {
            "ok": True,
            "op": "dependency.remove",
            "at": at_raw,
            "dependency": {
                "from_object": from_object,
                "from_permission": from_permission,
                "to_object": to_object,
                "to_permission": to_permission,
                "version": version,
            },
        }

    # -- planning ------------------------------------------------------------

    def _basis_relevant_edges(
        self, tenant: str, basis_object_ids: Iterable[str], include_removed: bool
    ) -> list[Edge]:
        """Edges whose dependent is inside the planning basis.

        An edge *into* a basis object from an unrelated object cannot change
        the target of the basis, and its later modification is deliberately
        allowed to proceed without invalidating the plan.  An edge *out of* a
        basis object names a prerequisite the plan depends on, so it is
        recorded and watched.
        """

        basis_ids = set(basis_object_ids)
        return [
            edge
            for edge in self._edges(tenant, basis_ids, include_removed=include_removed)
            if edge.from_object in basis_ids
        ]

    def _component_closure(self, scope: set[str], edges: list[Edge]) -> set[str]:
        """All objects connected to ``scope`` in the dependency graph.

        Both directions are needed:

        * *downstream* — removing a permission from an object can invalidate
          every dependent that still references it, so those objects must be
          adjusted by the same plan, otherwise the invariant "a permission
          lacking its prerequisite cannot stay" would break;
        * *upstream* — if a permission is being added and one of its
          prerequisites is itself only in the target, not yet materialised,
          the prerequisite must be added first, in the same plan, or the
          addition order could not be honoured.

        Everything not connected to ``scope`` stays out of the basis, which is
        what allows unrelated changes to proceed without staling the plan.
        """

        adjacency: dict[str, set[str]] = {}
        for edge in edges:
            if edge.removed:
                continue
            adjacency.setdefault(edge.from_object, set()).add(edge.to_object)
            adjacency.setdefault(edge.to_object, set()).add(edge.from_object)
        closure = set(scope)
        stack = list(scope)
        while stack:
            object_id = stack.pop()
            for neighbour in adjacency.get(object_id, ()):
                if neighbour not in closure:
                    closure.add(neighbour)
                    stack.append(neighbour)
        return closure

    def _op_plan_create(self, tenant: str, request: dict[str, Any]) -> dict[str, Any]:
        planned_at_us, planned_at_raw = _as_instant(request, "planned_at")
        raw_scope = request.get("objects")
        if raw_scope is None:
            scope = sorted(self._objects(tenant))
        else:
            if not isinstance(raw_scope, list):
                raise ReclaimError(
                    "invalid_argument",
                    "objects must be a list of object ids",
                    {"field": "objects"},
                )
            scope = sorted({_as_id(item, "objects") for item in raw_scope})
            for object_id in scope:
                if self._object(tenant, object_id) is None:
                    raise ReclaimError(
                        "not_found",
                        f"object not found: {object_id}",
                        {"object_id": object_id},
                    )

        edges = self._edges(tenant)
        # Planning a subset expands downstream so cascading removals are
        # included; prerequisite objects are pulled in as read-only basis
        # entries by `_compute_state`.
        closure = self._component_closure(set(scope), edges)
        for object_id in closure:
            if self._object(tenant, object_id) is None:
                raise ReclaimError(
                    "not_found",
                    f"object not found: {object_id}",
                    {"object_id": object_id},
                )

        objects, keep, sources, edges_in_basis = self._compute_state(
            tenant, planned_at_us, sorted(closure)
        )
        edges = edges_in_basis

        object_entries: list[dict[str, Any]] = []
        removals: list[tuple[str, str]] = []
        additions: list[tuple[str, str]] = []
        changed_ids: list[str] = []

        for object_id in sorted(closure):
            obj = objects.get(object_id)
            if obj is None:
                raise ReclaimError(
                    "not_found",
                    f"object not found: {object_id}",
                    {"object_id": object_id},
                )
            target = keep.get(object_id, set())
            before = set(obj.materialized)
            remove = before - target
            add = target - before
            if remove or add:
                changed_ids.append(object_id)
                removals.extend((object_id, permission) for permission in remove)
                additions.extend((object_id, permission) for permission in add)
            object_entries.append(
                {
                    "object_id": object_id,
                    "in_requested_scope": object_id in set(scope),
                    "generation": obj.generation,
                    "object_version": obj.object_version,
                    "materialized_before": sorted(before),
                    "target": sorted(target),
                    "remove": sorted(remove),
                    "add": sorted(add),
                    "explanation": self._target_explanation(
                        tenant,
                        obj,
                        target,
                        sources.get(object_id, {}),
                        edges,
                        keep,
                        planned_at_us,
                    ),
                }
            )

        if len(changed_ids) > MAX_PLAN_OBJECTS:
            raise ReclaimError(
                "plan_limit_exceeded",
                f"a plan may adjust at most {MAX_PLAN_OBJECTS} objects",
                {"changed_objects": len(changed_ids), "limit": MAX_PLAN_OBJECTS},
            )

        ordered_removals = self._order_nodes(removals, edges, prereq_first=False)
        ordered_additions = self._order_nodes(additions, edges, prereq_first=True)

        basis_ids = set(objects)
        basis = {
            "objects": {
                object_id: {
                    "generation": objects[object_id].generation,
                    "object_version": objects[object_id].object_version,
                }
                for object_id in sorted(basis_ids)
            },
            "leases": {
                lease.lease_id: {
                    "version": lease.version,
                    "revoked": lease.revoked,
                    "object_id": lease.object_id,
                }
                for lease in self._leases_for(tenant, basis_ids)
            },
            "dependencies": {
                edge.key: {"version": edge.version, "removed": edge.removed}
                for edge in self._basis_relevant_edges(tenant, basis_ids, True)
            },
        }

        plan_id = self.store.next_plan_id()
        result = {
            "ok": True,
            "op": "plan.create",
            "plan_id": plan_id,
            "tenant": tenant,
            "planned_at": planned_at_raw,
            "scope": scope,
            "planned_objects": sorted(closure),
            "objects": object_entries,
            "removals": [
                {"object_id": object_id, "permission": permission}
                for object_id, permission in ordered_removals
            ],
            "additions": [
                {"object_id": object_id, "permission": permission}
                for object_id, permission in ordered_additions
            ],
            "summary": {
                "objects_in_scope": len(scope),
                "objects_planned": len(closure),
                "objects_changed": len(changed_ids),
                "remove_count": len(ordered_removals),
                "add_count": len(ordered_additions),
            },
            "basis": basis,
        }
        self.store.conn.execute(
            "INSERT INTO plans (plan_id, tenant, planned_at_us, scope, basis, result, created_seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                tenant,
                planned_at_us,
                _canonical(scope),
                _canonical(basis),
                _canonical(result),
                self.store.next_event_seq(),
            ),
        )
        return result

    # -- applying ------------------------------------------------------------

    def _stale(
        self, reasons: list[dict[str, Any]], plan_id: str, at_raw: str
    ) -> ReclaimError:
        return ReclaimError(
            "stale",
            "planning basis changed; nothing was applied",
            {
                "plan_id": plan_id,
                "applied": False,
                "applied_at": at_raw,
                "reasons": reasons,
            },
        )

    def _op_plan_apply(self, tenant: str, request: dict[str, Any]) -> dict[str, Any]:
        plan_id = _as_id(_require(request, "plan_id"), "plan_id")
        applied_at_us, applied_at_raw = _as_instant(request, "applied_at")

        row = self.store.conn.execute(
            "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None or row["tenant"] != tenant:
            raise ReclaimError(
                "not_found", f"plan not found: {plan_id}", {"plan_id": plan_id}
            )

        already = self.store.conn.execute(
            "SELECT * FROM plan_applications WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if already is not None:
            raise ReclaimError(
                "plan_already_applied",
                "plan was already applied",
                {
                    "plan_id": plan_id,
                    "applied_at": format_instant(already["applied_at_us"]),
                    "apply_request_id": already["apply_request_id"],
                },
            )

        basis = json.loads(row["basis"])
        stored_result = json.loads(row["result"])
        reasons = self._revalidate(tenant, basis, stored_result, applied_at_us)
        if reasons:
            raise self._stale(reasons, plan_id, applied_at_raw)
        gate("plan_apply.revalidated")
        crash_point("apply_after_revalidate")

        events: list[dict[str, Any]] = []
        seq = 0

        def emit(action: str, object_id: str, permission: str) -> None:
            nonlocal seq
            seq += 1
            events.append(
                {
                    "seq": seq,
                    "action": action,
                    "object_id": object_id,
                    "permission": permission,
                }
            )
            self.store.conn.execute(
                "INSERT INTO apply_events (plan_id, seq, action, object_id, permission,"
                " applied_at_us) VALUES (?, ?, ?, ?, ?, ?)",
                (plan_id, seq, action, object_id, permission, applied_at_us),
            )

        touched: set[str] = set()
        for item in stored_result["removals"]:
            touched.add(item["object_id"])
        for item in stored_result["additions"]:
            touched.add(item["object_id"])

        # Removals run downstream-first, additions upstream-first; the stored
        # order was computed by the planner and is applied verbatim.  Before
        # each removal, prove that no *kept* dependent still references it:
        # that is the invariant "a permission lacking a prerequisite cannot
        # stay", and the ordering alone does not guarantee it when the plan
        # only touches part of a chain.
        kept_after = {
            entry["object_id"]: set(entry["target"]) for entry in stored_result["objects"]
        }
        active_edges = [
            edge
            for edge in self._edges(tenant, sorted(touched))
            if not edge.removed
        ]

        for item in stored_result["removals"]:
            object_id = item["object_id"]
            permission = item["permission"]
            blockers = sorted(
                f"{edge.from_object}:{edge.from_permission}"
                for edge in active_edges
                if edge.prerequisite == (object_id, permission)
                and edge.from_permission in kept_after.get(edge.from_object, set())
            )
            if blockers:
                raise ReclaimError(
                    "ordering_violation",
                    "refusing to remove a permission still required downstream",
                    {
                        "plan_id": plan_id,
                        "object_id": object_id,
                        "permission": permission,
                        "blocked_by": blockers,
                    },
                )
            obj = self._object(tenant, object_id)
            if obj is None:
                raise self._stale(
                    [{"kind": "object_missing", "object_id": object_id}],
                    plan_id,
                    applied_at_raw,
                )
            materialized = set(obj.materialized)
            if permission in materialized:
                materialized.discard(permission)
                self._write_materialized(tenant, object_id, materialized)
            emit("remove", object_id, permission)

        for item in stored_result["additions"]:
            object_id = item["object_id"]
            permission = item["permission"]
            obj = self._object(tenant, object_id)
            if obj is None:
                raise self._stale(
                    [{"kind": "object_missing", "object_id": object_id}],
                    plan_id,
                    applied_at_raw,
                )
            materialized = set(obj.materialized)
            if permission not in materialized:
                materialized.add(permission)
                self._write_materialized(tenant, object_id, materialized)
            emit("add", object_id, permission)

        # A successful apply is itself a state change: bump the version of
        # every object it touched so overlapping plans become stale.
        for object_id in sorted(touched):
            self.store.conn.execute(
                "UPDATE objects SET object_version = object_version + 1, created_seq = ?"
                " WHERE tenant = ? AND object_id = ?",
                (self.store.next_event_seq(), tenant, object_id),
            )

        current = []
        for entry in stored_result["objects"]:
            obj = self._object(tenant, entry["object_id"])
            if obj is not None:
                current.append(
                    {
                        "object_id": obj.object_id,
                        "materialized": list(obj.materialized),
                    }
                )

        response = {
            "ok": True,
            "op": "plan.apply",
            "plan_id": plan_id,
            "applied_at": applied_at_raw,
            "applied": True,
            "events": events,
            "objects": current,
        }
        self.store.conn.execute(
            "INSERT INTO plan_applications (plan_id, tenant, apply_request_id, applied_at_us,"
            " receipt, created_seq) VALUES (?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                tenant,
                request["request_id"],
                applied_at_us,
                _canonical(response),
                self.store.next_event_seq(),
            ),
        )
        gate("plan_apply.written")
        crash_point("apply_after_writes")
        return response

    def _write_materialized(
        self, tenant: str, object_id: str, materialized: set[str]
    ) -> None:
        self.store.conn.execute(
            "UPDATE objects SET materialized = ? WHERE tenant = ? AND object_id = ?",
            (_canonical(sorted(materialized)), tenant, object_id),
        )

    def _revalidate(
        self,
        tenant: str,
        basis: dict[str, Any],
        stored_result: dict[str, Any],
        applied_at_us: int,
    ) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        basis_object_ids = set(basis["objects"])

        for object_id, recorded in sorted(basis["objects"].items()):
            obj = self._object(tenant, object_id)
            if obj is None:
                reasons.append(
                    {
                        "kind": "object_missing",
                        "object_id": object_id,
                        "planned_generation": recorded["generation"],
                    }
                )
                continue
            if obj.generation != recorded["generation"]:
                reasons.append(
                    {
                        "kind": "object_recreated",
                        "object_id": object_id,
                        "planned_generation": recorded["generation"],
                        "current_generation": obj.generation,
                    }
                )
            elif obj.object_version != recorded["object_version"]:
                reasons.append(
                    {
                        "kind": "object_changed",
                        "object_id": object_id,
                        "planned_object_version": recorded["object_version"],
                        "current_object_version": obj.object_version,
                    }
                )

        current_leases = {
            lease.lease_id: lease
            for lease in self._leases_for(tenant, basis_object_ids)
        }
        for lease_id, recorded in sorted(basis["leases"].items()):
            lease = current_leases.get(lease_id)
            if lease is None:
                reasons.append({"kind": "lease_missing", "lease_id": lease_id})
            elif lease.version != recorded["version"] or lease.revoked != recorded[
                "revoked"
            ]:
                reasons.append(
                    {
                        "kind": "lease_changed",
                        "lease_id": lease_id,
                        "object_id": lease.object_id,
                        "planned_version": recorded["version"],
                        "current_version": lease.version,
                    }
                )
        for lease_id in sorted(set(current_leases) - set(basis["leases"])):
            lease = current_leases[lease_id]
            reasons.append(
                {
                    "kind": "lease_added",
                    "lease_id": lease_id,
                    "object_id": lease.object_id,
                    "version": lease.version,
                }
            )

        current_edges = {
            edge.key: edge
            for edge in self._basis_relevant_edges(
                tenant, basis_object_ids, include_removed=True
            )
        }
        for key, recorded in sorted(basis["dependencies"].items()):
            edge = current_edges.get(key)
            if edge is None:
                reasons.append({"kind": "dependency_missing", "edge": key})
            elif edge.version != recorded["version"] or edge.removed != recorded[
                "removed"
            ]:
                reasons.append(
                    {
                        "kind": "dependency_changed",
                        "edge": key,
                        "planned_version": recorded["version"],
                        "current_version": edge.version,
                        "planned_removed": recorded["removed"],
                        "current_removed": edge.removed,
                    }
                )
        for key in sorted(set(current_edges) - set(basis["dependencies"])):
            reasons.append({"kind": "dependency_added", "edge": key})

        if reasons:
            return reasons

        # The basis is structurally unchanged.  Re-derive the target at the
        # *transaction* instant: a lease may have expired since planning even
        # though nothing was written.
        scope = stored_result["planned_objects"]
        _, keep, _, _ = self._compute_state(tenant, applied_at_us, scope)
        for entry in stored_result["objects"]:
            object_id = entry["object_id"]
            planned = set(entry["target"])
            current = keep.get(object_id, set())
            if current == planned:
                continue
            detail: dict[str, Any] = {
                "kind": "target_changed",
                "object_id": object_id,
                "target_added": sorted(current - planned),
                "target_removed": sorted(planned - current),
            }
            expired = []
            for lease in self._leases_for(tenant, [object_id]):
                if lease.start_us <= applied_at_us and lease.end_us <= applied_at_us:
                    if set(lease.permissions) & (planned - current):
                        expired.append(
                            {
                                "lease_id": lease.lease_id,
                                "end": format_instant(lease.end_us),
                            }
                        )
            if expired:
                detail["expired_leases"] = expired
            reasons.append(detail)
        return reasons

    # -- export --------------------------------------------------------------

    def _op_state_export(self, tenant: str, request: dict[str, Any]) -> dict[str, Any]:
        at_us, at_raw = _as_instant(request, "at")
        objects = self._objects(tenant)
        _, keep, sources, edges = self._compute_state(
            tenant, at_us, sorted(objects)
        )
        object_entries = []
        for object_id in sorted(objects):
            obj = objects[object_id]
            object_entries.append(
                {
                    **obj.public(),
                    "target": sorted(keep.get(object_id, set())),
                    "explanation": self._target_explanation(
                        tenant,
                        obj,
                        keep.get(object_id, set()),
                        sources.get(object_id, {}),
                        edges,
                        keep,
                        at_us,
                    ),
                }
            )
        lease_entries = [lease.public() for lease in self._leases_for(tenant)]

        plan_rows = self.store.conn.execute(
            "SELECT plan_id, planned_at_us, result FROM plans"
            " WHERE tenant = ? ORDER BY created_seq",
            (tenant,),
        ).fetchall()
        applied_rows = self.store.conn.execute(
            "SELECT * FROM plan_applications WHERE tenant = ? ORDER BY created_seq",
            (tenant,),
        ).fetchall()
        applied_by_plan = {row["plan_id"]: row for row in applied_rows}
        plan_entries = []
        for row in plan_rows:
            result = json.loads(row["result"])
            application = applied_by_plan.get(row["plan_id"])
            entry: dict[str, Any] = {
                "plan_id": row["plan_id"],
                "planned_at": format_instant(row["planned_at_us"]),
                "applied": application is not None,
                "summary": result["summary"],
                "scope": result["scope"],
            }
            if application is not None:
                entry["applied_at"] = format_instant(application["applied_at_us"])
                entry["apply_request_id"] = application["apply_request_id"]
                entry["events"] = [
                    {
                        "seq": event["seq"],
                        "action": event["action"],
                        "object_id": event["object_id"],
                        "permission": event["permission"],
                    }
                    for event in self.store.conn.execute(
                        "SELECT * FROM apply_events WHERE plan_id = ? ORDER BY seq",
                        (row["plan_id"],),
                    ).fetchall()
                ]
            plan_entries.append(entry)

        receipt_rows = self.store.conn.execute(
            "SELECT request_id, op, response FROM receipts WHERE tenant = ? ORDER BY created_seq",
            (tenant,),
        ).fetchall()
        receipt_entries = []
        for row in receipt_rows:
            response = json.loads(row["response"])
            receipt_entries.append(
                {
                    "request_id": row["request_id"],
                    "op": row["op"],
                    "ok": bool(response.get("ok")),
                    "code": response.get("code"),
                }
            )

        return {
            "ok": True,
            "op": "state.export",
            "tenant": tenant,
            "at": at_raw,
            "objects": object_entries,
            "leases": lease_entries,
            "dependencies": [edge.public() for edge in self._edges(tenant)],
            "plans": plan_entries,
            "receipts": receipt_entries,
        }
