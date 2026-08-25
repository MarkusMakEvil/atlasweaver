"""Deterministic integrity evidence for graph navigation and impact analysis."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


class IntegrityError(ValueError):
    """Raised when graph identity or diagnostic evidence is malformed."""


@dataclass(frozen=True)
class GraphIntegrity:
    """Observable graph defects and the resulting impact-analysis trust state."""

    dangling_edges: int
    missing_endpoints: int
    self_loops: int
    duplicate_edges: int
    collapsed_edges: int | None
    impact_analysis_trusted: bool
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "dangling_edges": self.dangling_edges,
            "missing_endpoints": self.missing_endpoints,
            "self_loops": self.self_loops,
            "duplicate_edges": self.duplicate_edges,
            "collapsed_edges": self.collapsed_edges,
            "impact_analysis_trusted": self.impact_analysis_trusted,
        }


def analyze_graph(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    collapsed_edges: int | None = None,
) -> GraphIntegrity:
    """Calculate reproducible counters without claiming unavailable evidence."""
    if collapsed_edges is not None and (
        type(collapsed_edges) is not int or collapsed_edges < 0
    ):
        raise IntegrityError("collapsed edge counter is invalid")

    identifiers: set[str] = set()
    for node in nodes:
        identifier = node.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise IntegrityError("invalid node id")
        if identifier in identifiers:
            raise IntegrityError("duplicate node id")
        identifiers.add(identifier)

    dangling = 0
    missing = 0
    self_loops = 0
    duplicates = 0
    seen_edges: set[str] = set()
    for edge in edges:
        source = edge.get("source")
        target = edge.get("target")
        if (
            not isinstance(source, str)
            or not source
            or not isinstance(target, str)
            or not target
        ):
            missing += 1
        else:
            if source not in identifiers or target not in identifiers:
                dangling += 1
            if source == target:
                self_loops += 1
        identity = json.dumps(edge, sort_keys=True, separators=(",", ":"))
        if identity in seen_edges:
            duplicates += 1
        else:
            seen_edges.add(identity)

    trusted = (
        collapsed_edges == 0
        and dangling == 0
        and missing == 0
        and self_loops == 0
        and duplicates == 0
    )
    return GraphIntegrity(
        dangling_edges=dangling,
        missing_endpoints=missing,
        self_loops=self_loops,
        duplicate_edges=duplicates,
        collapsed_edges=collapsed_edges,
        impact_analysis_trusted=trusted,
    )
