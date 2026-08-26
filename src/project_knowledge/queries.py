"""Immutable, deterministic and bounded queries over owned graph snapshots."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from types import MappingProxyType
from typing import Any, Literal

from .artifacts import ArtifactValidationError, ValidatedGraph, validate_owned_graph
from .compatibility import GraphifyCompatibility, resolve_graphify_compatibility
from .evidence import (
    EvidenceError,
    FinalEdgeEvidence,
    GraphEvidence,
    index_final_edge_evidence,
    parse_graph_evidence,
)
from .locking import (
    RepositoryAccess,
    RepositoryIdentity,
    TransactionLockError,
    capture_lifecycle_repository,
    open_repository_access,
    repository_lifecycle_lock,
)
from .manifest import (
    ManifestError,
    assert_current_manifest_unchanged,
    require_current_manifest,
)
from .models import ProjectManifest
from .secrets_scan import redact_literals
from .staging import inspect_projection


MAX_QUERY_INPUT_BYTES = 4_096
MAX_QUERY_NODES = 100_000
MAX_QUERY_EDGES = 500_000
MAX_PATH_DEPTH = 32
MAX_AFFECTED_DEPTH = 8
MAX_RESULTS = 100
MAX_NEIGHBORS = 100
MAX_RELATION_FILTERS = 16
MAX_OUTPUT_BYTES = 1_048_576
MAX_GRAPH_BYTES = 128 * 1024 * 1024
DOCUMENTED_QUERY_ERROR_CODES = frozenset({
    "manifest_migration_required",
    "manifest_changed",
    "graph_error",
    "graph_missing",
    "graph_stale",
    "graph_partial_unapproved",
    "query_snapshot_unavailable",
    "query_snapshot_busy",
    "query_cleanup_failed",
    "query_graph_invalid",
    "query_graph_too_large",
    "query_input_invalid",
    "query_input_too_large",
    "query_depth_exceeded",
    "query_relation_cap_exceeded",
    "query_result_cap_invalid",
    "query_output_too_large",
    "node_not_found",
    "ambiguous_node",
})


@dataclass(frozen=True)
class QueryError(Exception):
    code: str
    message: str
    candidates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.code not in DOCUMENTED_QUERY_ERROR_CODES:
            raise ValueError("query error code is invalid")
        Exception.__init__(self, self.message)


@dataclass(frozen=True)
class NodeRecord:
    id: str
    label: str
    attributes: Mapping[str, object]


@dataclass(frozen=True)
class EdgeRecord:
    id: str
    source: str
    target: str
    relation: str
    attributes: Mapping[str, object]
    evidence: FinalEdgeEvidence | None = field(default=None, repr=False)


@dataclass(frozen=True)
class QuerySnapshot:
    root: Path
    validated: ValidatedGraph
    document: Mapping[str, object]
    contract: GraphifyCompatibility
    evidence: GraphEvidence | None
    health: object
    nodes: Mapping[str, NodeRecord]
    edges: tuple[EdgeRecord, ...]
    outgoing: Mapping[str, tuple[EdgeRecord, ...]]
    incoming: Mapping[str, tuple[EdgeRecord, ...]]


@dataclass(frozen=True)
class RegistryQueryRequest:
    command: Literal["query", "path", "explain", "affected"]
    term: str | None = None
    source: str | None = None
    target: str | None = None
    node: str | None = None
    limit: int = 20
    max_depth: int = 32
    depth: int | None = None
    relations: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapturedQueryGraph:
    registry_key: str
    project_id: str
    graph_payload: bytes = field(repr=False)
    evidence_payload: bytes = field(repr=False)
    evidence_digest: str
    contract: GraphifyCompatibility
    trust: Literal["trusted", "navigation"]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class QueryEnvelope:
    command: Literal["query", "path", "explain", "affected"]
    trust: Literal["trusted", "navigation"]
    result: Mapping[str, object]
    limitations: tuple[str, ...]
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        document = {
            "schema_version": 1,
            "command": self.command,
            "trust": self.trust,
            "result": _thaw(self.result),
            "limitations": list(self.limitations),
        }
        redacted = _redact_tree(document)
        payload = json.dumps(
            redacted,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > MAX_OUTPUT_BYTES:
            raise QueryError("query_output_too_large", "query output exceeded its cap")
        assert isinstance(redacted, dict)
        return redacted


@contextmanager
def open_query_snapshot(
    repo_root: Path,
    manifest: ProjectManifest,
    *,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> Iterator[QuerySnapshot]:
    """Capture one validated graph under a noncreating lifecycle lease."""
    if manifest.schema_version != 2 or manifest.project_uid is None:
        raise QueryError(
            "manifest_migration_required", "manifest schema migration is required"
        )
    _preflight_query_paths(
        repo_root, manifest, expected_repository_identity=expected_repository_identity
    )
    temporary = tempfile.mkdtemp(prefix="atlasweaver-query-")
    os.chmod(temporary, 0o700)
    captured_root = Path(temporary) / "graph"
    snapshot: QuerySnapshot | None = None
    try:
        try:
            with repository_lifecycle_lock(
                repo_root,
                timeout=0.05,
                create=False,
                expected_repository_identity=expected_repository_identity,
            ):
                with capture_lifecycle_repository(repo_root) as repository:
                    try:
                        require_current_manifest(
                            repo_root, manifest, repository_access=repository
                        )
                    except ManifestError as error:
                        if error.kind == "changed":
                            raise QueryError(
                                "manifest_changed", "project manifest changed"
                            ) from None
                        raise
                    projection = inspect_projection(
                        repo_root, manifest, repository_access=repository
                    )
                    _capture_owned_output(repository, manifest, captured_root)
                    try:
                        validated = validate_owned_graph(
                            captured_root,
                            manifest,
                            expected_source_digest=projection.source_digest,
                            expected_projection_digest=projection.projection_digest,
                        )
                    except ArtifactValidationError:
                        raise QueryError(
                            "graph_error", "owned graph validation failed"
                        ) from None
                    if validated.unapproved_skips:
                        raise QueryError(
                            "graph_partial_unapproved",
                            "graph contains unapproved extraction omissions",
                        )
                    snapshot = _snapshot_from_owned(captured_root, manifest, validated)
                    assert_current_manifest_unchanged(
                        repo_root, manifest, repository_access=repository
                    )
        except TransactionLockError as error:
            if error.kind == "authority":
                raise
            code = "query_snapshot_busy" if error.kind == "busy" else "query_snapshot_unavailable"
            raise QueryError(code, "query snapshot is unavailable") from None
        except ManifestError as error:
            if error.kind == "changed":
                raise QueryError("manifest_changed", "project manifest changed") from None
            raise QueryError("graph_error", "query admission failed") from None
        assert snapshot is not None
        yield snapshot
    finally:
        try:
            shutil.rmtree(temporary)
        except OSError:
            if snapshot is not None:
                raise QueryError("query_cleanup_failed", "query cleanup failed") from None


def query_nodes(
    snapshot: QuerySnapshot, term: str, *, limit: int = 20
) -> QueryEnvelope:
    _bounded_input(term)
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise QueryError("query_result_cap_invalid", "result limit is invalid")
    folded = term.casefold()
    ranked = sorted(
        snapshot.nodes.values(),
        key=lambda node: (
            0 if node.id == term else 1 if node.label.casefold() == folded
            else 2 if node.label.casefold().startswith(folded) else 3,
            node.label.casefold(),
            node.id,
        ),
    )
    matches = [
        node for node in ranked
        if folded in node.id.casefold()
        or folded in node.label.casefold()
        or folded in str(node.attributes.get("local_id", "")).casefold()
    ]
    return _envelope(
        snapshot, "query", {"nodes": [_node_view(node) for node in matches[:limit]]}
    )


def shortest_path(
    snapshot: QuerySnapshot,
    source: str,
    target: str,
    *,
    max_depth: int = MAX_PATH_DEPTH,
) -> QueryEnvelope:
    if type(max_depth) is not int or not 1 <= max_depth <= MAX_PATH_DEPTH:
        raise QueryError("query_depth_exceeded", "path depth exceeded its cap")
    start, goal = _resolve_one(snapshot, source), _resolve_one(snapshot, target)
    if start == goal:
        return _envelope(
            snapshot, "path", {"nodes": [_node_view(snapshot.nodes[start])], "edges": []}
        )
    parents: dict[str, tuple[str, EdgeRecord]] = {}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    visited = {start}
    found = False
    while queue and not found:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for edge, neighbor in _neighbors(snapshot, current):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            parents[neighbor] = (current, edge)
            if neighbor == goal:
                found = True
                break
            queue.append((neighbor, depth + 1))
    if not found:
        return _envelope(
            snapshot,
            "path",
            {"nodes": [], "edges": []},
            extra_limitations=("no_path_found_not_proof_of_no_impact",),
        )
    node_ids = [goal]
    edges: list[EdgeRecord] = []
    while node_ids[-1] != start:
        parent, edge = parents[node_ids[-1]]
        edges.append(edge)
        node_ids.append(parent)
    node_ids.reverse()
    edges.reverse()
    return _envelope(
        snapshot,
        "path",
        {
            "nodes": [_node_view(snapshot.nodes[node]) for node in node_ids],
            "edges": [_edge_view(edge, snapshot.validated.impact_trust) for edge in edges],
        },
    )


def explain_node(
    snapshot: QuerySnapshot, node: str, *, depth: int = 1
) -> QueryEnvelope:
    if type(depth) is not int or not 1 <= depth <= 2:
        raise QueryError("query_depth_exceeded", "explain depth exceeded its cap")
    selected = _resolve_one(snapshot, node)
    seen = {selected}
    frontier = [selected]
    edge_ids: set[str] = set()
    for _ in range(depth):
        following: list[str] = []
        for current in sorted(frontier):
            for edge, neighbor in _neighbors(snapshot, current)[:MAX_NEIGHBORS]:
                edge_ids.add(edge.id)
                if neighbor not in seen and len(seen) < MAX_RESULTS:
                    seen.add(neighbor)
                    following.append(neighbor)
        frontier = sorted(set(following))
    edges = [edge for edge in snapshot.edges if edge.id in edge_ids]
    result = {
        "node": _node_view(snapshot.nodes[selected]),
        "neighbors": [
            _node_view(snapshot.nodes[value]) for value in sorted(seen - {selected})
        ],
        "edges": [_edge_view(edge, snapshot.validated.impact_trust) for edge in edges],
    }
    return _envelope(snapshot, "explain", result)


def affected_nodes(
    snapshot: QuerySnapshot,
    node: str,
    *,
    depth: int = 2,
    relations: tuple[str, ...] = (),
) -> QueryEnvelope:
    if type(depth) is not int or not 0 <= depth <= MAX_AFFECTED_DEPTH:
        raise QueryError("query_depth_exceeded", "affected depth exceeded its cap")
    if (
        type(relations) is not tuple
        or len(relations) > MAX_RELATION_FILTERS
        or any(type(value) is not str or not value for value in relations)
    ):
        raise QueryError(
            "query_relation_cap_exceeded", "relation filters exceeded their cap"
        )
    selected = _resolve_one(snapshot, node)
    admitted = set(relations)
    seen = {selected}
    frontier = [selected]
    returned: list[str] = []
    edge_ids: set[str] = set()
    for _ in range(depth):
        following: list[str] = []
        for current in sorted(frontier):
            edges = list(snapshot.incoming.get(current, ()))
            if not snapshot.contract.semantics.directed:
                edges.extend(snapshot.outgoing.get(current, ()))
            candidates: list[tuple[EdgeRecord, str]] = []
            for edge in edges:
                if admitted and edge.relation not in admitted:
                    continue
                neighbor = edge.source if edge.target == current else edge.target
                candidates.append((edge, neighbor))
            for edge, neighbor in sorted(
                candidates, key=lambda item: (item[0].relation, item[1], item[0].id)
            ):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                returned.append(neighbor)
                following.append(neighbor)
                edge_ids.add(edge.id)
                if len(returned) >= MAX_RESULTS:
                    break
            if len(returned) >= MAX_RESULTS:
                break
        frontier = sorted(set(following))
        if len(returned) >= MAX_RESULTS:
            break
    result = {
        "root": _node_view(snapshot.nodes[selected]),
        "nodes": [_node_view(snapshot.nodes[value]) for value in returned],
        "edges": [
            _edge_view(edge, snapshot.validated.impact_trust)
            for edge in snapshot.edges if edge.id in edge_ids
        ],
    }
    extra = () if returned else ("no_path_found_not_proof_of_no_impact",)
    return _envelope(snapshot, "affected", result, extra_limitations=extra)


def query_captured_graphs(
    graphs: tuple[CapturedQueryGraph, ...], request: RegistryQueryRequest
) -> QueryEnvelope:
    if type(graphs) is not tuple or not graphs or type(request) is not RegistryQueryRequest:
        raise QueryError("query_input_invalid", "registry query is invalid")
    _validate_registry_request(request)
    combined_nodes: dict[str, NodeRecord] = {}
    combined_edges: list[EdgeRecord] = []
    limitations: set[str] = set()
    trust: Literal["trusted", "navigation"] = "trusted"
    directed: bool | None = None
    contract = graphs[0].contract
    for graph in sorted(graphs, key=lambda item: item.registry_key):
        if type(graph) is not CapturedQueryGraph or graph.contract.version != contract.version:
            raise QueryError("query_graph_invalid", "registry graph contract is invalid")
        parsed = _parse_graph(
            graph.graph_payload,
            graph.contract,
            evidence_payload=graph.evidence_payload,
            evidence_digest=graph.evidence_digest,
            namespace=graph.registry_key,
        )
        if directed is None:
            directed = graph.contract.semantics.directed
        elif directed != graph.contract.semantics.directed:
            raise QueryError("query_graph_invalid", "registry graph semantics differ")
        for node_id, node in parsed[0].items():
            if node_id in combined_nodes:
                raise QueryError("query_graph_invalid", "registry node IDs collide")
            combined_nodes[node_id] = node
        combined_edges.extend(parsed[1])
        limitations.update(graph.limitations)
        if graph.trust == "navigation":
            trust = "navigation"
    if len(combined_nodes) > MAX_QUERY_NODES or len(combined_edges) > MAX_QUERY_EDGES:
        raise QueryError("query_graph_too_large", "registry graph exceeds its cap")
    validated = _synthetic_validated(trust, tuple(sorted(limitations)))
    snapshot = _build_snapshot(
        Path("."), validated, MappingProxyType({}), contract, None, None,
        combined_nodes, tuple(combined_edges),
    )
    if request.command == "query":
        return query_nodes(snapshot, request.term or "", limit=request.limit)
    if request.command == "path":
        return shortest_path(
            snapshot, request.source or "", request.target or "",
            max_depth=request.max_depth,
        )
    if request.command == "explain":
        return explain_node(snapshot, request.node or "", depth=1 if request.depth is None else request.depth)
    return affected_nodes(
        snapshot,
        request.node or "",
        depth=2 if request.depth is None else request.depth,
        relations=request.relations,
    )


def _preflight_query_paths(
    repo_root: Path,
    manifest: ProjectManifest,
    *,
    expected_repository_identity: RepositoryIdentity | None,
) -> None:
    with open_repository_access(
        repo_root, expected_repository_identity=expected_repository_identity
    ) as repository:
        try:
            require_current_manifest(repo_root, manifest, repository_access=repository)
        except ManifestError as error:
            if error.kind == "changed":
                raise QueryError("manifest_changed", "project manifest changed") from None
            raise
        try:
            output_fd = _open_relative_directory(
                repository.descriptor, manifest.output_dir
            )
        except FileNotFoundError:
            raise QueryError("graph_missing", "owned graph is missing") from None
        else:
            os.close(output_fd)
        try:
            state = os.stat(
                ".project-knowledge",
                dir_fd=repository.descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(state.st_mode) or stat.S_ISLNK(state.st_mode):
                raise OSError
            state_fd = os.open(
                ".project-knowledge",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=repository.descriptor,
            )
            try:
                lock = os.stat("refresh.lock", dir_fd=state_fd, follow_symlinks=False)
                if not stat.S_ISREG(lock.st_mode):
                    raise OSError
            finally:
                os.close(state_fd)
        except (FileNotFoundError, OSError):
            raise QueryError(
                "query_snapshot_unavailable", "query lifecycle state is unavailable"
            ) from None


def _capture_owned_output(
    repository: RepositoryAccess,
    manifest: ProjectManifest,
    destination: Path,
) -> None:
    output_fd = _open_relative_directory(repository.descriptor, manifest.output_dir)
    try:
        destination.mkdir(mode=0o700)
        with os.scandir(os.dup(output_fd)) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            info = os.stat(name, dir_fd=output_fd, follow_symlinks=False)
            if name == "cache" and stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise QueryError("query_graph_invalid", "owned graph entry is invalid")
            payload = _read_regular_at(output_fd, name, MAX_GRAPH_BYTES)
            target = destination / name
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                offset = 0
                while offset < len(payload):
                    offset += os.write(descriptor, payload[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(output_fd)


def _snapshot_from_owned(
    root: Path, manifest: ProjectManifest, validated: ValidatedGraph
) -> QuerySnapshot:
    contract = resolve_graphify_compatibility(manifest.graphify_version)
    graph_payload = (root / "graph.json").read_bytes()
    evidence_payload = None
    evidence_digest = None
    if validated.artifact_schema_version == 2:
        evidence_payload = (root / "GRAPH_EVIDENCE.json").read_bytes()
        evidence_digest = validated.evidence_digest
    nodes, edges, document, evidence = _parse_graph(
        graph_payload,
        contract,
        evidence_payload=evidence_payload,
        evidence_digest=evidence_digest,
    )
    health = _query_health(manifest, validated)
    return _build_snapshot(
        root, validated, document, contract, evidence, health, nodes, edges
    )


def _parse_graph(
    graph_payload: bytes,
    contract: GraphifyCompatibility,
    *,
    evidence_payload: bytes | None,
    evidence_digest: str | None,
    namespace: str | None = None,
) -> tuple[
    dict[str, NodeRecord],
    tuple[EdgeRecord, ...],
    Mapping[str, object],
    GraphEvidence | None,
]:
    if type(graph_payload) is not bytes or len(graph_payload) > MAX_GRAPH_BYTES:
        raise QueryError("query_graph_too_large", "query graph exceeds its cap")
    document = _strict_json_mapping(graph_payload)
    raw_nodes = document.get("nodes")
    if ("edges" in document) == ("links" in document):
        raise QueryError("query_graph_invalid", "graph edge member is invalid")
    raw_edges = document.get("edges", document.get("links"))
    if type(raw_nodes) is not list or type(raw_edges) is not list:
        raise QueryError("query_graph_invalid", "graph shape is invalid")
    if len(raw_nodes) > MAX_QUERY_NODES or len(raw_edges) > MAX_QUERY_EDGES:
        raise QueryError("query_graph_too_large", "query graph exceeds its cap")
    nodes: dict[str, NodeRecord] = {}
    local_to_global: dict[str, str] = {}
    for value in raw_nodes:
        if type(value) is not dict:
            raise QueryError("query_graph_invalid", "graph node is invalid")
        local = value.get(contract.semantics.node_id_field)
        if type(local) is not str or not local:
            raise QueryError("query_graph_invalid", "graph node ID is invalid")
        node_id = f"{namespace}::{local}" if namespace else local
        if node_id in nodes:
            raise QueryError("query_graph_invalid", "graph node IDs are duplicated")
        label_value = value.get("label", value.get("name", local))
        label = label_value if type(label_value) is str and label_value else local
        attributes = dict(value)
        attributes["local_id"] = local
        attributes[contract.semantics.node_id_field] = node_id
        nodes[node_id] = NodeRecord(node_id, label, _freeze(attributes))
        local_to_global[local] = node_id
    evidence: GraphEvidence | None = None
    evidence_index: Mapping[str, FinalEdgeEvidence] = MappingProxyType({})
    if evidence_payload is not None or evidence_digest is not None:
        if evidence_payload is None or evidence_digest is None:
            raise QueryError("query_graph_invalid", "graph evidence anchor is incomplete")
        try:
            evidence = parse_graph_evidence(
                evidence_payload, contract, expected_digest=evidence_digest
            )
            evidence_index = index_final_edge_evidence(evidence)
        except EvidenceError:
            raise QueryError("query_graph_invalid", "graph evidence is invalid") from None
    edges: list[EdgeRecord] = []
    edge_ids: set[str] = set()
    for index, value in enumerate(raw_edges):
        if type(value) is not dict:
            raise QueryError("query_graph_invalid", "graph edge is invalid")
        source_local = value.get(contract.semantics.source_field)
        target_local = value.get(contract.semantics.target_field)
        relation = value.get("relation", "related")
        if (
            type(source_local) is not str
            or type(target_local) is not str
            or source_local not in local_to_global
            or target_local not in local_to_global
            or type(relation) is not str
            or not relation
        ):
            raise QueryError("query_graph_invalid", "graph edge endpoint is invalid")
        local_edge_id = value.get("atlasweaver_edge_id")
        if type(local_edge_id) is not str or not local_edge_id:
            local_edge_id = f"edge-{index:09d}"
        edge_id = f"{namespace}::{local_edge_id}" if namespace else local_edge_id
        if edge_id in edge_ids:
            raise QueryError("query_graph_invalid", "graph edge IDs are duplicated")
        edge_ids.add(edge_id)
        attributes = dict(value)
        attributes[contract.semantics.source_field] = local_to_global[source_local]
        attributes[contract.semantics.target_field] = local_to_global[target_local]
        edges.append(EdgeRecord(
            edge_id,
            local_to_global[source_local],
            local_to_global[target_local],
            relation,
            _freeze(attributes),
            evidence_index.get(local_edge_id),
        ))
    return nodes, tuple(sorted(edges, key=lambda item: item.id)), _freeze(document), evidence


def _build_snapshot(
    root: Path,
    validated: ValidatedGraph,
    document: Mapping[str, object],
    contract: GraphifyCompatibility,
    evidence: GraphEvidence | None,
    health: object,
    nodes: Mapping[str, NodeRecord],
    edges: tuple[EdgeRecord, ...],
) -> QuerySnapshot:
    outgoing: dict[str, list[EdgeRecord]] = {node: [] for node in nodes}
    incoming: dict[str, list[EdgeRecord]] = {node: [] for node in nodes}
    for edge in edges:
        outgoing[edge.source].append(edge)
        incoming[edge.target].append(edge)
        if not contract.semantics.directed:
            outgoing[edge.target].append(edge)
            incoming[edge.source].append(edge)
    order = lambda edge: (edge.relation, edge.target, edge.source, edge.id)
    return QuerySnapshot(
        root,
        validated,
        document,
        contract,
        evidence,
        health,
        MappingProxyType(dict(nodes)),
        tuple(edges),
        MappingProxyType({key: tuple(sorted(value, key=order)) for key, value in outgoing.items()}),
        MappingProxyType({key: tuple(sorted(value, key=order)) for key, value in incoming.items()}),
    )


def _resolve_one(snapshot: QuerySnapshot, value: str) -> str:
    _bounded_input(value)
    if value in snapshot.nodes:
        return value
    folded = value.casefold()
    exact = tuple(sorted(
        node_id for node_id, node in snapshot.nodes.items()
        if node.label.casefold() == folded
        or str(node.attributes.get("local_id", "")).casefold() == folded
    ))
    if len(exact) == 1:
        return exact[0]
    candidates = exact or tuple(sorted(
        node_id for node_id, node in snapshot.nodes.items()
        if folded in node_id.casefold() or folded in node.label.casefold()
    ))[:20]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise QueryError("ambiguous_node", "node selection is ambiguous", candidates)
    raise QueryError("node_not_found", "node was not found")


def _neighbors(
    snapshot: QuerySnapshot, node: str
) -> list[tuple[EdgeRecord, str]]:
    values: list[tuple[EdgeRecord, str]] = []
    seen: set[str] = set()
    edges = list(snapshot.outgoing.get(node, ()))
    if not snapshot.contract.semantics.directed:
        edges.extend(snapshot.incoming.get(node, ()))
    for edge in edges:
        if edge.id in seen:
            continue
        seen.add(edge.id)
        neighbor = edge.target if edge.source == node else edge.source
        values.append((edge, neighbor))
    return sorted(values, key=lambda item: (item[0].relation, item[1], item[0].id))


def _envelope(
    snapshot: QuerySnapshot,
    command: Literal["query", "path", "explain", "affected"],
    result: Mapping[str, object],
    *,
    extra_limitations: tuple[str, ...] = (),
) -> QueryEnvelope:
    limitations = set(snapshot.validated.impact_limitations)
    limitations.update(extra_limitations)
    if snapshot.validated.impact_trust == "navigation":
        limitations.add("source_verification_required")
    return QueryEnvelope(
        command,
        snapshot.validated.impact_trust,
        _freeze(dict(result)),
        tuple(sorted(limitations)),
    )


def _node_view(node: NodeRecord) -> dict[str, object]:
    return {"id": node.id, "label": node.label, "attributes": _thaw(node.attributes)}


def _edge_view(
    edge: EdgeRecord, trust: Literal["trusted", "navigation"]
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": edge.id,
        "source": edge.source,
        "target": edge.target,
        "relation": edge.relation,
    }
    if trust == "trusted" and edge.evidence is not None:
        result["confidence"] = edge.evidence.confidence
        result["source_file"] = (
            None if edge.evidence.source_file is None
            else edge.evidence.source_file.as_posix()
        )
        result["source_line"] = edge.evidence.source_line
        result["source_column"] = edge.evidence.source_column
    return result


def _validate_registry_request(request: RegistryQueryRequest) -> None:
    if request.command not in {"query", "path", "explain", "affected"}:
        raise QueryError("query_input_invalid", "registry query command is invalid")
    if type(request.relations) is not tuple:
        raise QueryError("query_input_invalid", "registry query is invalid")
    required = {
        "query": (request.term is not None and request.source is None and request.target is None and request.node is None),
        "path": (request.term is None and request.source is not None and request.target is not None and request.node is None),
        "explain": (request.term is None and request.source is None and request.target is None and request.node is not None and not request.relations),
        "affected": (request.term is None and request.source is None and request.target is None and request.node is not None),
    }[request.command]
    if not required:
        raise QueryError("query_input_invalid", "registry query fields are invalid")
    if request.command != "query" and request.limit != 20:
        raise QueryError("query_input_invalid", "registry query fields are invalid")
    if request.command != "path" and request.max_depth != 32:
        raise QueryError("query_input_invalid", "registry query fields are invalid")
    if request.command in {"query", "path"} and request.depth is not None:
        raise QueryError("query_input_invalid", "registry query fields are invalid")


def _query_health(manifest: ProjectManifest, validated: ValidatedGraph) -> object:
    from .health import FeatureHealth, KnowledgeState, assess_health

    return assess_health(KnowledgeState(
        project_id=manifest.project_id,
        manifest_schema_version=manifest.schema_version,
        artifact_schema_version=validated.artifact_schema_version,
        graph_exists=True,
        graph_valid=True,
        graph_version=manifest.graphify_version,
        current_source_digest=validated.source_digest,
        graph_source_digest=validated.source_digest,
        current_projection_digest=validated.projection_digest,
        graph_projection_digest=validated.projection_digest,
        features={
            "atlas": FeatureHealth("disabled"),
            "registry": FeatureHealth("disabled"),
            "artifacts": FeatureHealth("disabled"),
        },
        coverage_skips=validated.skipped_count,
        unapproved_skips=validated.unapproved_skips,
        impact_trust=validated.impact_trust,
        impact_limitations=validated.impact_limitations,
    ))


def _synthetic_validated(
    trust: Literal["trusted", "navigation"], limitations: tuple[str, ...]
) -> ValidatedGraph:
    return ValidatedGraph(
        Path("."),
        "0" * 64,
        "0" * 64,
        0,
        0,
        artifact_schema_version=2,
        generation_digest="0" * 64,
        impact_trust=trust,
        impact_limitations=limitations,
    )


def _strict_json_mapping(payload: bytes) -> Mapping[str, object]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise QueryError("query_graph_invalid", "query graph JSON is invalid") from None
    if type(value) is not dict:
        raise QueryError("query_graph_invalid", "query graph is invalid")
    _reject_nonfinite(value)
    return value


def _freeze(value: object) -> object:
    copied = deepcopy(value)
    if type(copied) is dict:
        if any(type(key) is not str for key in copied):
            raise QueryError("query_graph_invalid", "query attributes are invalid")
        return MappingProxyType({key: _freeze(item) for key, item in copied.items()})
    if type(copied) is list:
        return tuple(_freeze(item) for item in copied)
    if type(copied) in {str, int, float, bool} or copied is None:
        _reject_nonfinite(copied)
        return copied
    raise QueryError("query_graph_invalid", "query attributes are invalid")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _redact_tree(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            redact_literals(str(key)): _redact_tree(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_tree(item) for item in value]
    if type(value) is str:
        return redact_literals(value)
    if type(value) in {int, bool} or value is None:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise QueryError("query_graph_invalid", "query output contains invalid values")


def _bounded_input(value: object) -> str:
    if type(value) is not str or not value:
        raise QueryError("query_input_invalid", "query input is invalid")
    if len(value.encode("utf-8")) > MAX_QUERY_INPUT_BYTES:
        raise QueryError("query_input_too_large", "query input exceeded its cap")
    return value


def _reject_nonfinite(value: object) -> None:
    if type(value) is float and not math.isfinite(value):
        raise QueryError("query_graph_invalid", "query graph contains non-finite values")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite(key)
            _reject_nonfinite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_nonfinite(item)


def _open_relative_directory(root_fd: int, relative: PurePosixPath) -> int:
    current = os.dup(root_fd)
    try:
        for part in relative.parts:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
            before = os.fstat(child)
            named = os.stat(part, dir_fd=current, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode) or (
                before.st_dev, before.st_ino
            ) != (named.st_dev, named.st_ino):
                os.close(child)
                raise OSError
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _read_regular_at(parent_fd: int, name: str, limit: int) -> bytes:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise QueryError("query_graph_invalid", "query artifact is invalid")
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise QueryError("query_graph_invalid", "query artifact changed")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(1_048_576, limit + 1 - total)):
            total += len(chunk)
            if total > limit:
                raise QueryError("query_graph_too_large", "query artifact exceeds its cap")
            chunks.append(chunk)
        rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            or (rebound.st_dev, rebound.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise QueryError("query_graph_invalid", "query artifact changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)
