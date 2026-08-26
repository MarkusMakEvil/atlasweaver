"""Deterministic structural integrity for adapter-declared graph semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from .compatibility import GraphSemantics


class IntegrityError(ValueError):
    """Raised when graph identity or structural evidence is malformed."""


@dataclass(frozen=True)
class GraphIntegrity:
    """Versioned structural observations without an impact-trust decision."""

    node_count: int
    edge_count: int
    missing_endpoint_edges: int
    dangling_endpoint_edges: int
    invalid_self_loop_edges: int
    exact_duplicate_edges: int
    conflicting_relation_edges: int
    collapsed_edges: int | None
    structurally_valid: bool
    schema_version: int = 2

    def __post_init__(self) -> None:
        counters = (
            self.node_count,
            self.edge_count,
            self.missing_endpoint_edges,
            self.dangling_endpoint_edges,
            self.invalid_self_loop_edges,
            self.exact_duplicate_edges,
            self.conflicting_relation_edges,
        )
        if any(type(value) is not int or value < 0 for value in counters):
            raise IntegrityError("graph integrity counter is invalid")
        if self.collapsed_edges is not None and (
            type(self.collapsed_edges) is not int or self.collapsed_edges < 0
        ):
            raise IntegrityError("graph integrity counter is invalid")
        if type(self.structurally_valid) is not bool:
            raise IntegrityError("structural validity is invalid")
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise IntegrityError("graph integrity schema version is invalid")
        expected_validity = (
            self.missing_endpoint_edges == 0
            and self.dangling_endpoint_edges == 0
            and self.invalid_self_loop_edges == 0
            and self.exact_duplicate_edges == 0
            and self.conflicting_relation_edges == 0
            and self.collapsed_edges in (None, 0)
        )
        if self.structurally_valid is not expected_validity:
            raise IntegrityError("graph integrity structural validity is inconsistent")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "missing_endpoint_edges": self.missing_endpoint_edges,
            "dangling_endpoint_edges": self.dangling_endpoint_edges,
            "invalid_self_loop_edges": self.invalid_self_loop_edges,
            "exact_duplicate_edges": self.exact_duplicate_edges,
            "conflicting_relation_edges": self.conflicting_relation_edges,
            "collapsed_edges": self.collapsed_edges,
            "structurally_valid": self.structurally_valid,
        }

    @classmethod
    def from_dict(cls, document: object) -> GraphIntegrity:
        """Parse the closed schema using exact scalar types."""
        expected_fields = {
            "schema_version",
            "node_count",
            "edge_count",
            "missing_endpoint_edges",
            "dangling_endpoint_edges",
            "invalid_self_loop_edges",
            "exact_duplicate_edges",
            "conflicting_relation_edges",
            "collapsed_edges",
            "structurally_valid",
        }
        if not isinstance(document, Mapping) or set(document) != expected_fields:
            raise IntegrityError("graph integrity schema is invalid")
        return cls(
            node_count=document["node_count"],
            edge_count=document["edge_count"],
            missing_endpoint_edges=document["missing_endpoint_edges"],
            dangling_endpoint_edges=document["dangling_endpoint_edges"],
            invalid_self_loop_edges=document["invalid_self_loop_edges"],
            exact_duplicate_edges=document["exact_duplicate_edges"],
            conflicting_relation_edges=document["conflicting_relation_edges"],
            collapsed_edges=document["collapsed_edges"],
            structurally_valid=document["structurally_valid"],
            schema_version=document["schema_version"],
        )


def canonical_final_edge_id(
    edge: Mapping[str, Any], semantics: GraphSemantics
) -> str:
    """Return the domain-separated identity of one adapter-final edge."""
    if "atlasweaver_edge_id" in edge:
        raise IntegrityError("edge contains reserved AtlasWeaver identity")
    values = {
        field: _identity_value(edge.get(field), field)
        for field in semantics.edge_identity_fields
    }
    source = values.get(semantics.source_field)
    target = values.get(semantics.target_field)
    if not isinstance(source, str) or not source:
        raise IntegrityError(
            f"edge identity field {semantics.source_field} is invalid"
        )
    if not isinstance(target, str) or not target:
        raise IntegrityError(
            f"edge identity field {semantics.target_field} is invalid"
        )
    if not semantics.directed and target < source:
        values[semantics.source_field], values[semantics.target_field] = target, source
    payload = json.dumps(
        values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(b"atlasweaver-final-edge-v1\0" + payload).hexdigest()
    return "edge-" + digest


def analyze_graph(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    semantics: GraphSemantics,
    collapsed_edges: int | None = None,
) -> GraphIntegrity:
    """Measure defects using only the selected adapter's declared invariants."""
    if collapsed_edges is not None and (
        type(collapsed_edges) is not int or collapsed_edges < 0
    ):
        raise IntegrityError("collapsed edge counter is invalid")

    identifiers: set[str] = set()
    for node in nodes:
        if not isinstance(node, Mapping):
            raise IntegrityError("invalid graph node")
        identifier = node.get(semantics.node_id_field)
        if not isinstance(identifier, str) or not identifier:
            raise IntegrityError("invalid node id")
        if identifier in identifiers:
            raise IntegrityError("duplicate node id")
        identifiers.add(identifier)

    missing = 0
    dangling = 0
    invalid_self_loops = 0
    exact_duplicates = 0
    seen_identities: set[tuple[object, ...]] = set()
    parallel_groups: dict[tuple[object, ...], set[tuple[object, ...]]] = {}
    relation_field = "relation"

    for edge in edges:
        if not isinstance(edge, Mapping):
            raise IntegrityError("invalid graph edge")
        source = edge.get(semantics.source_field)
        target = edge.get(semantics.target_field)
        endpoints_complete = (
            isinstance(source, str)
            and bool(source)
            and isinstance(target, str)
            and bool(target)
        )
        if not endpoints_complete:
            missing += 1
        else:
            if source not in identifiers or target not in identifiers:
                dangling += 1
            relation = edge.get(relation_field)
            if source == target and relation not in semantics.allowed_self_loop_relations:
                invalid_self_loops += 1

        identity_values = _analysis_identity_values(edge, semantics)
        if not semantics.directed:
            _canonicalize_endpoint_values(identity_values, semantics)
        identity = tuple(
            identity_values[field] for field in semantics.edge_identity_fields
        )
        if identity in seen_identities:
            exact_duplicates += 1
        else:
            seen_identities.add(identity)

        if endpoints_complete:
            parallel_values = {
                field: _analysis_field_value(edge, field, semantics)
                for field in semantics.parallel_key_fields
            }
            if not semantics.directed:
                _canonicalize_endpoint_values(parallel_values, semantics)
            parallel_key = tuple(
                parallel_values[field] for field in semantics.parallel_key_fields
            )
            parallel_groups.setdefault(parallel_key, set()).add(identity)

    conflicts = _parallel_conflicts(parallel_groups, semantics)
    structurally_valid = (
        missing == 0
        and dangling == 0
        and invalid_self_loops == 0
        and exact_duplicates == 0
        and conflicts == 0
        and collapsed_edges in (None, 0)
    )
    return GraphIntegrity(
        node_count=len(nodes),
        edge_count=len(edges),
        missing_endpoint_edges=missing,
        dangling_endpoint_edges=dangling,
        invalid_self_loop_edges=invalid_self_loops,
        exact_duplicate_edges=exact_duplicates,
        conflicting_relation_edges=conflicts,
        collapsed_edges=collapsed_edges,
        structurally_valid=structurally_valid,
    )


def validate_final_graph(
    document: Mapping[str, Any], semantics: GraphSemantics
) -> GraphIntegrity:
    """Validate a final graph's shape, invariants, and deterministic edge IDs."""
    if not isinstance(document, Mapping):
        raise IntegrityError("final graph document is invalid")
    has_links = "links" in document
    has_edges = "edges" in document
    if has_links == has_edges:
        raise IntegrityError("final graph must contain exactly one edge member")
    nodes = document.get("nodes")
    edges = document["links" if has_links else "edges"]
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise IntegrityError("final graph members must be lists")
    if any(not isinstance(node, Mapping) for node in nodes) or any(
        not isinstance(edge, Mapping) for edge in edges
    ):
        raise IntegrityError("final graph members must contain objects")

    integrity = analyze_graph(nodes, edges, semantics=semantics)
    final_ids: set[str] = set()
    for edge in edges:
        final_id = edge.get("atlasweaver_edge_id")
        if not isinstance(final_id, str) or not final_id:
            raise IntegrityError("final graph edge is missing AtlasWeaver edge identity")
        identity_input = {
            key: value for key, value in edge.items() if key != "atlasweaver_edge_id"
        }
        if final_id != canonical_final_edge_id(identity_input, semantics):
            raise IntegrityError("final graph AtlasWeaver edge identity is invalid")
        if final_id in final_ids:
            raise IntegrityError("final graph contains duplicate AtlasWeaver edge identity")
        final_ids.add(final_id)
    if not integrity.structurally_valid:
        raise IntegrityError("final graph is structurally invalid")
    return integrity


def _identity_value(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise IntegrityError(f"edge identity field {field} is invalid")
    return value


def _analysis_field_value(
    edge: Mapping[str, Any], field: str, semantics: GraphSemantics
) -> str | None:
    value = edge.get(field)
    if field in {semantics.source_field, semantics.target_field} and (
        not isinstance(value, str) or not value
    ):
        return None
    return _identity_value(value, field)


def _analysis_identity_values(
    edge: Mapping[str, Any], semantics: GraphSemantics
) -> dict[str, str | None]:
    return {
        field: _analysis_field_value(edge, field, semantics)
        for field in semantics.edge_identity_fields
    }


def _canonicalize_endpoint_values(
    values: dict[str, Any], semantics: GraphSemantics
) -> None:
    if semantics.source_field not in values or semantics.target_field not in values:
        return
    source = values[semantics.source_field]
    target = values[semantics.target_field]
    if isinstance(source, str) and isinstance(target, str) and target < source:
        values[semantics.source_field], values[semantics.target_field] = target, source


def _parallel_conflicts(
    groups: Mapping[tuple[object, ...], set[tuple[object, ...]]],
    semantics: GraphSemantics,
) -> int:
    if semantics.parallel_policy == "allow":
        return 0
    if semantics.parallel_policy == "forbid":
        return sum(max(0, len(identities) - 1) for identities in groups.values())

    relation_index = (
        semantics.edge_identity_fields.index("relation")
        if "relation" in semantics.edge_identity_fields
        else None
    )
    conflicts = 0
    for identities in groups.values():
        by_relation: dict[object, int] = {}
        for identity in identities:
            relation = identity[relation_index] if relation_index is not None else None
            by_relation[relation] = by_relation.get(relation, 0) + 1
        conflicts += sum(max(0, count - 1) for count in by_relation.values())
    return conflicts
