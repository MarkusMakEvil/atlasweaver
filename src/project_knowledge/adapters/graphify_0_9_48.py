"""Graphify 0.9.48 evidence-boundary adapter."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any

from project_knowledge.compatibility import (
    CompatibilityError,
    GraphifyCompatibility,
    _validate_clustered_document,
    _validate_native_document,
)
from project_knowledge.integrity import (
    IntegrityError,
    analyze_graph,
    canonical_final_edge_id,
    validate_final_graph,
)
from project_knowledge.privacy import GLOBAL_DENY_PATTERNS, is_denied

from .base import (
    AdapterContractError,
    CapturedArtifact,
    NativeGraph,
    NormalizationResult,
    ReasonCount,
)


_PATH_FIELDS = frozenset({"source", "source_file", "source_path", "path", "file"})
_WRAPPER_FIELDS = frozenset(
    {
        "project_id",
        "graphify_version",
        "source_digest",
        "projection_digest",
        "extraction_coverage",
        "graph_health",
        "evidence_digest",
        "extraction_invocation_digest",
        "impact_trust",
        "impact_limitations",
    }
)


@dataclass(frozen=True)
class Graphify0948Adapter:
    contract: GraphifyCompatibility

    def parse_post_dedup(self, artifact: CapturedArtifact) -> NativeGraph:
        document = _strict_json_object(artifact.payload, "native graph")
        if set(document) & _WRAPPER_FIELDS or "links" in document:
            raise AdapterContractError("Graphify 0.9.48 native schema is invalid")
        try:
            fingerprint = _validate_native_document(document)
        except CompatibilityError as error:
            raise AdapterContractError(
                "Graphify 0.9.48 native schema is invalid"
            ) from error
        nodes = document["nodes"]
        edges = document["edges"]
        if (
            not nodes
            or any(not isinstance(node, dict) for node in nodes)
            or any(not isinstance(edge, dict) for edge in edges)
        ):
            raise AdapterContractError("Graphify 0.9.48 native schema is invalid")
        if fingerprint != self.contract.native_schema_fingerprint:
            raise AdapterContractError(
                "Graphify 0.9.48 native schema fingerprint is invalid"
            )
        return NativeGraph(
            document=document,
            nodes=tuple(nodes),
            edges=tuple(edges),
        )

    def normalize_for_cluster(self, graph: NativeGraph) -> NormalizationResult:
        try:
            observed = analyze_graph(
                graph.nodes,
                graph.edges,
                semantics=self.contract.semantics,
            )
        except (IntegrityError, TypeError) as error:
            raise AdapterContractError("native graph integrity is invalid") from error

        node_ids = {node.get(self.contract.semantics.node_id_field) for node in graph.nodes}
        aliases: dict[str, set[str]] = defaultdict(set)
        for node in graph.nodes:
            identifier = node.get(self.contract.semantics.node_id_field)
            if not isinstance(identifier, str) or not identifier:
                raise AdapterContractError("native graph node id is invalid")
            for field in ("label", "norm_label"):
                alias = node.get(field)
                if isinstance(alias, str) and alias:
                    aliases[alias].add(identifier)

        repairs: Counter[str] = Counter()
        quarantines: Counter[str] = Counter()
        kept_edges: list[dict[str, Any]] = []
        for source_edge in graph.edges:
            if not isinstance(source_edge, Mapping):
                raise AdapterContractError("native graph edge is invalid")
            edge = dict(source_edge)
            edge_repairs: Counter[str] = Counter()
            rejected = False
            for field in (
                self.contract.semantics.source_field,
                self.contract.semantics.target_field,
            ):
                replacement, reason, valid = _resolve_endpoint(
                    edge.get(field), node_ids, aliases
                )
                if valid:
                    edge[field] = replacement
                    if reason is not None:
                        edge_repairs[reason] += 1
                else:
                    if reason is None:
                        raise AssertionError("invalid endpoint requires a reason")
                    quarantines[reason] += 1
                    rejected = True
            repairs.update(edge_repairs)
            if not rejected:
                kept_edges.append(edge)

        document = dict(graph.document)
        document.pop("links", None)
        document["nodes"] = [dict(node) for node in graph.nodes]
        document["edges"] = kept_edges
        cluster_input = CapturedArtifact.from_payload(
            PurePosixPath("cluster-input/graph.json"), _canonical_json(document)
        )
        return NormalizationResult(
            cluster_input=cluster_input,
            observed_integrity=observed,
            repairs=_reason_counts(repairs),
            quarantines=_reason_counts(quarantines),
        )

    def adapt_clustered_graph(
        self,
        artifact: CapturedArtifact,
        *,
        staged_files: frozenset[PurePosixPath],
    ) -> bytes:
        _require_staged_files(staged_files)
        document = _strict_json_object(artifact.payload, "clustered graph")
        if "edges" in document or _contains_wrapper_metadata(document):
            raise AdapterContractError("Graphify 0.9.48 clustered schema is invalid")
        try:
            fingerprint = _validate_clustered_document(document)
        except CompatibilityError as error:
            raise AdapterContractError(
                "Graphify 0.9.48 clustered schema is invalid"
            ) from error
        if fingerprint != self.contract.clustered_schema_fingerprint:
            raise AdapterContractError(
                "Graphify 0.9.48 clustered schema fingerprint is invalid"
            )

        nodes = document["nodes"]
        edges = document["links"]
        if (
            not nodes
            or any(not isinstance(node, dict) for node in nodes)
            or any(not isinstance(edge, dict) for edge in edges)
        ):
            raise AdapterContractError("Graphify 0.9.48 clustered schema is invalid")

        _remove_empty_path_sentinels(nodes, edges)
        _validate_source_fields(nodes, edges, staged_files)
        _canonicalize_node_source_aliases(nodes, staged_files)
        try:
            for edge in edges:
                if "atlasweaver_edge_id" in edge:
                    raise AdapterContractError(
                        "clustered graph contains reserved AtlasWeaver identity"
                    )
                edge["atlasweaver_edge_id"] = canonical_final_edge_id(
                    edge, self.contract.semantics
                )
            validate_final_graph(document, self.contract.semantics)
        except IntegrityError as error:
            raise AdapterContractError("clustered graph is structurally invalid") from error
        return _canonical_json(document)


def _strict_json_object(payload: bytes, description: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AdapterContractError(f"{description} JSON contains duplicate keys")
            result[key] = value
        return result

    def finite(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise AdapterContractError(f"{description} JSON contains a non-finite number")
        return parsed

    def constant(value: str) -> object:
        raise AdapterContractError(f"{description} JSON contains a non-finite number")

    try:
        value = json.loads(
            payload,
            object_pairs_hook=unique,
            parse_constant=constant,
            parse_float=finite,
        )
    except AdapterContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AdapterContractError(f"{description} JSON is malformed") from error
    if not isinstance(value, dict):
        raise AdapterContractError(f"{description} JSON must contain an object")
    return value


def _resolve_endpoint(
    endpoint: object,
    node_ids: set[object],
    aliases: Mapping[str, set[str]],
) -> tuple[object, str | None, bool]:
    if isinstance(endpoint, str) and endpoint in node_ids:
        return endpoint, None, True
    if not isinstance(endpoint, str) or not endpoint:
        return endpoint, "missing_endpoint", False
    targets = aliases.get(endpoint, set())
    if len(targets) == 1:
        return next(iter(targets)), "unique_exact_node_alias", True
    if len(targets) > 1:
        return endpoint, "ambiguous_endpoint_alias", False
    return endpoint, "dangling_endpoint", False


def _reason_counts(counts: Counter[str]) -> tuple[ReasonCount, ...]:
    return tuple(ReasonCount(code, counts[code]) for code in sorted(counts) if counts[code])


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _contains_wrapper_metadata(document: Mapping[str, Any]) -> bool:
    return any(
        isinstance(container, Mapping) and bool(set(container) & _WRAPPER_FIELDS)
        for container in (document, document.get("metadata"), document.get("graph"))
    )


def _remove_empty_path_sentinels(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> None:
    def clean(value: Any, *, edge: bool = False) -> None:
        if isinstance(value, dict):
            for key in tuple(value):
                item = value[key]
                if (
                    key in _PATH_FIELDS
                    and not (edge and key in {"source", "target"})
                    and item == ""
                ):
                    del value[key]
                else:
                    clean(item)
        elif isinstance(value, list):
            for item in value:
                clean(item)

    for node in nodes:
        clean(node)
    for edge in edges:
        clean(edge, edge=True)


def _validate_source_fields(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    staged_files: frozenset[PurePosixPath],
) -> None:
    def visit(value: Any, *, edge: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in _PATH_FIELDS and not (edge and key in {"source", "target"}):
                    path = _source_path(item)
                    if path not in staged_files:
                        raise AdapterContractError(
                            "clustered graph source path is not in staged input"
                        )
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for node in nodes:
        visit(node)
    for edge in edges:
        visit(edge, edge=True)


def _canonicalize_node_source_aliases(
    nodes: list[dict[str, Any]], staged_files: frozenset[PurePosixPath]
) -> None:
    for node in nodes:
        source = node.get("source_file")
        if not isinstance(source, str) or PurePosixPath(source) not in staged_files:
            continue
        for key in ("label", "norm_label"):
            alias = node.get(key)
            if isinstance(alias, str) and "/" in alias and source.endswith("/" + alias):
                node[key] = source


def _source_path(value: object) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or value in {"", "."}
        or "\\" in value
        or "\x00" in value
        or re.match(r"^[A-Za-z]:/", value) is not None
    ):
        raise AdapterContractError("clustered graph source path must be confined")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise AdapterContractError("clustered graph source path must be confined")
    return path


def _require_staged_files(staged_files: frozenset[PurePosixPath]) -> None:
    if type(staged_files) is not frozenset:
        raise AdapterContractError("staged files must be an immutable path set")
    for path in staged_files:
        if type(path) is not PurePosixPath:
            raise AdapterContractError("staged files contain an invalid path")
        try:
            confined = _source_path(path.as_posix())
            denied = is_denied(confined, GLOBAL_DENY_PATTERNS)
        except Exception:
            raise AdapterContractError("staged files contain an invalid path") from None
        if denied:
            raise AdapterContractError("staged files contain an invalid path")
