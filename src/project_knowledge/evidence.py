"""Canonical extraction invocation and graph evidence contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Literal

from project_knowledge.adapters import (
    AdapterContractError,
    CapturedArtifact,
    NormalizationResult,
    ReasonCount,
    adapter_for,
)
from project_knowledge.compatibility import (
    CompatibilityError,
    GraphifyCompatibility,
    RenderedCommand,
    _validate_diagnostic_document,
    admitted_graphify_environment,
    render_graphify_argv,
    resolve_graphify_compatibility,
)
from project_knowledge.integrity import (
    GraphIntegrity,
    IntegrityError,
    validate_final_graph,
)


class EvidenceError(ValueError):
    """Raised when extraction or evidence cannot be verified exactly."""


GRAPH_EVIDENCE_MAX_BYTES = 67_108_864

_INVOCATION_DOMAIN = b"atlasweaver-graphify-pipeline-v1\0"
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FINAL_EDGE_ID = re.compile(r"edge-[0-9a-f]{64}\Z")
_SOURCE_LOCATION = re.compile(r"L?(?P<line>[1-9][0-9]*)(?::(?P<column>[1-9][0-9]*))?\Z")
_DIAGNOSIS_MAX_BYTES = 4_194_304
_BASE_ENVIRONMENT_NAMES = ("HOME", "LANG", "LC_ALL", "PATH")
_BASE_ARTIFACT_PATHS = frozenset(
    {
        PurePosixPath("raw/graph.json"),
        PurePosixPath("raw/diagnose.json"),
        PurePosixPath("cluster-input/graph.json"),
        PurePosixPath("clustered/graph.json"),
        PurePosixPath("clustered/GRAPH_REPORT.md"),
    }
)
_HTML_ARTIFACT_PATH = PurePosixPath("clustered/graph.html")


@dataclass(frozen=True)
class ArtifactBinding:
    path: PurePosixPath
    sha256: str
    byte_length: int


@dataclass(frozen=True)
class ExtractionInvocation:
    adapter_id: str
    graphify_version: str
    executable_sha256: str
    capability_smoke_digest: str
    commands: tuple[tuple[str, ...], ...]
    backend: str | None
    model: str | None
    configuration_sha256: str
    source_digest: str
    projection_digest: str
    environment_names: tuple[str, ...]
    artifacts: tuple[ArtifactBinding, ...]
    payload: bytes
    digest: str


@dataclass(frozen=True)
class FinalEdgeEvidence:
    final_edge_id: str
    source: str
    target: str
    relation: str
    confidence: str | None
    source_file: PurePosixPath | None
    source_line: int | None
    source_column: int | None


@dataclass(frozen=True)
class PreDedupEdgeEvidence:
    occurrence_id: str
    source: str
    target: str
    relation: str
    confidence: str | None
    source_file: PurePosixPath | None
    source_line: int | None
    source_column: int | None
    final_edge_id: str | None
    disposition: Literal["kept", "rewritten", "dropped"]
    reason: str | None


@dataclass(frozen=True)
class GraphEvidence:
    graphify_version: str
    adapter_id: str
    source_digest: str
    projection_digest: str
    extraction_invocation_digest: str
    invocation: ExtractionInvocation
    observed_post_dedup: GraphIntegrity
    pre_dedup: tuple[PreDedupEdgeEvidence, ...] | None
    normalization_repairs: tuple[ReasonCount, ...]
    normalization_quarantines: tuple[ReasonCount, ...]
    final_integrity: GraphIntegrity
    final_edges: tuple[FinalEdgeEvidence, ...]
    evidence_complete: bool
    limitations: tuple[str, ...]
    payload: bytes
    digest: str


@dataclass(frozen=True)
class ImpactTrust:
    level: Literal["trusted", "navigation"]
    limitations: tuple[str, ...]


def build_extraction_invocation(
    contract: GraphifyCompatibility,
    *,
    executable_sha256: str,
    capability_smoke_digest: str,
    commands: tuple[RenderedCommand, ...],
    backend: str | None,
    model: str | None,
    configuration_sha256: str,
    source_digest: str,
    projection_digest: str,
    environment_names: tuple[str, ...],
    artifacts: tuple[CapturedArtifact, ...],
) -> ExtractionInvocation:
    """Bind the complete canonical orchestration without private argv or values."""
    _require_registered_contract(contract)
    for digest in (
        executable_sha256,
        capability_smoke_digest,
        configuration_sha256,
        source_digest,
        projection_digest,
    ):
        _require_digest(digest)

    bindings = _artifact_bindings(artifacts)
    track_html = any(item.path == _HTML_ARTIFACT_PATH for item in bindings)
    _require_canonical_commands(
        contract,
        commands,
        backend=backend,
        model=model,
        track_html=track_html,
    )
    _require_environment_names(contract, backend, environment_names)

    document = {
        "schema_version": 1,
        "adapter_id": contract.adapter_id,
        "graphify_version": contract.version,
        "executable": {"sha256": executable_sha256, "version": contract.version},
        "capability_smoke_digest": capability_smoke_digest,
        "argv": [
            {"operation": command.operation, "items": list(command.canonical_argv)}
            for command in commands
        ],
        "backend": backend,
        "model": model,
        "configuration_sha256": configuration_sha256,
        "source_digest": source_digest,
        "projection_digest": projection_digest,
        "environment_names": list(environment_names),
        "artifacts": [
            {
                "path": item.path.as_posix(),
                "sha256": item.sha256,
                "byte_length": item.byte_length,
            }
            for item in bindings
        ],
    }
    payload = _canonical_json(document)
    return ExtractionInvocation(
        adapter_id=contract.adapter_id,
        graphify_version=contract.version,
        executable_sha256=executable_sha256,
        capability_smoke_digest=capability_smoke_digest,
        commands=tuple(command.canonical_argv for command in commands),
        backend=backend,
        model=model,
        configuration_sha256=configuration_sha256,
        source_digest=source_digest,
        projection_digest=projection_digest,
        environment_names=environment_names,
        artifacts=bindings,
        payload=payload,
        digest=hashlib.sha256(_INVOCATION_DOMAIN + payload).hexdigest(),
    )


def build_graph_evidence(
    contract: GraphifyCompatibility,
    *,
    source_digest: str,
    projection_digest: str,
    invocation: ExtractionInvocation,
    native_graph: CapturedArtifact,
    diagnosis: CapturedArtifact,
    normalization: NormalizationResult,
    clustered_graph: CapturedArtifact,
    final_graph: CapturedArtifact,
) -> GraphEvidence:
    """Build safe evidence by reconciling official captures and adapter results."""
    _require_registered_contract(contract)
    source_digest = _require_digest(source_digest)
    projection_digest = _require_digest(projection_digest)
    if type(normalization) is not NormalizationResult:
        raise EvidenceError("normalization result is invalid")
    _require_artifact_descriptor(final_graph)
    parsed_invocation = _validate_invocation(invocation, contract)
    if parsed_invocation.source_digest != source_digest:
        raise EvidenceError("evidence source digest does not match invocation")
    if parsed_invocation.projection_digest != projection_digest:
        raise EvidenceError("evidence projection digest does not match invocation")
    _require_bound_artifact(parsed_invocation, native_graph, "raw/graph.json")
    _require_bound_artifact(parsed_invocation, diagnosis, "raw/diagnose.json")
    _require_bound_artifact(
        parsed_invocation, normalization.cluster_input, "cluster-input/graph.json"
    )
    _require_bound_artifact(
        parsed_invocation, clustered_graph, "clustered/graph.json"
    )

    try:
        implementation = adapter_for(contract)
        parsed_native = implementation.parse_post_dedup(native_graph)
        observed = implementation.normalize_for_cluster(parsed_native).observed_integrity
    except AdapterContractError as error:
        raise EvidenceError("captured native graph is invalid") from error
    if normalization.observed_integrity != observed:
        raise EvidenceError("normalization integrity does not match captured native graph")
    repairs = _validate_reason_counts(
        normalization.repairs, contract.normalization_reason_codes
    )
    quarantines = _validate_reason_counts(
        normalization.quarantines, contract.normalization_reason_codes
    )
    _validate_diagnosis(diagnosis, observed, contract)

    final_document = _strict_json_object(final_graph.payload, "final graph")
    try:
        final_integrity = validate_final_graph(final_document, contract.semantics)
    except IntegrityError as error:
        raise EvidenceError("final graph integrity is invalid") from error
    edge_member = "links" if "links" in final_document else "edges"
    final_edges = tuple(
        sorted(
            (_safe_final_edge(item, contract) for item in final_document[edge_member]),
            key=lambda item: item.final_edge_id,
        )
    )
    if len({item.final_edge_id for item in final_edges}) != len(final_edges):
        raise EvidenceError("final evidence edge IDs are duplicated")

    limitations = _derive_evidence_limitations(
        contract, quarantines, final_edges, pre_dedup=None
    )
    document = {
        "schema_version": 1,
        "graphify_version": contract.version,
        "adapter_id": contract.adapter_id,
        "source_digest": source_digest,
        "projection_digest": projection_digest,
        "extraction_invocation_digest": parsed_invocation.digest,
        "extraction_invocation": json.loads(parsed_invocation.payload),
        "observed_post_dedup": observed.to_dict(),
        "pre_dedup": None,
        "normalization": {
            "repairs": [_reason_document(item) for item in repairs],
            "quarantines": [_reason_document(item) for item in quarantines],
        },
        "final_integrity": final_integrity.to_dict(),
        "final_edges": [_final_edge_document(item) for item in final_edges],
        "evidence_complete": False,
        "limitations": list(limitations),
    }
    payload = _canonical_json(document)
    _bounded_evidence_payload(payload)
    return GraphEvidence(
        graphify_version=contract.version,
        adapter_id=contract.adapter_id,
        source_digest=source_digest,
        projection_digest=projection_digest,
        extraction_invocation_digest=parsed_invocation.digest,
        invocation=parsed_invocation,
        observed_post_dedup=observed,
        pre_dedup=None,
        normalization_repairs=repairs,
        normalization_quarantines=quarantines,
        final_integrity=final_integrity,
        final_edges=final_edges,
        evidence_complete=False,
        limitations=limitations,
        payload=payload,
        digest=hashlib.sha256(payload).hexdigest(),
    )


def parse_graph_evidence(
    payload: bytes, contract: GraphifyCompatibility
) -> GraphEvidence:
    """Parse the recursively closed evidence schema and require byte identity."""
    _require_registered_contract(contract)
    payload = _bounded_evidence_payload(payload)
    document = _strict_json_object(payload, "graph evidence")
    _require_exact_keys(
        document,
        {
            "schema_version",
            "graphify_version",
            "adapter_id",
            "source_digest",
            "projection_digest",
            "extraction_invocation_digest",
            "extraction_invocation",
            "observed_post_dedup",
            "pre_dedup",
            "normalization",
            "final_integrity",
            "final_edges",
            "evidence_complete",
            "limitations",
        },
        "graph evidence schema",
    )
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise EvidenceError("graph evidence schema version is invalid")
    if document["graphify_version"] != contract.version:
        raise EvidenceError("graph evidence Graphify version is invalid")
    if document["adapter_id"] != contract.adapter_id:
        raise EvidenceError("graph evidence adapter is invalid")
    source_digest = _require_digest(document["source_digest"])
    projection_digest = _require_digest(document["projection_digest"])
    invocation_digest = _require_digest(document["extraction_invocation_digest"])
    invocation = _parse_invocation_document(document["extraction_invocation"], contract)
    if invocation.digest != invocation_digest:
        raise EvidenceError("graph evidence invocation digest is invalid")
    if invocation.source_digest != source_digest:
        raise EvidenceError("graph evidence source digest is inconsistent")
    if invocation.projection_digest != projection_digest:
        raise EvidenceError("graph evidence projection digest is inconsistent")

    observed = _parse_integrity(document["observed_post_dedup"], "observed counter")
    pre_dedup = _parse_pre_dedup(document["pre_dedup"], contract)
    normalization = document["normalization"]
    _require_exact_keys(
        normalization, {"repairs", "quarantines"}, "normalization schema"
    )
    repairs = _parse_reason_documents(
        normalization["repairs"], contract.normalization_reason_codes
    )
    quarantines = _parse_reason_documents(
        normalization["quarantines"], contract.normalization_reason_codes
    )
    final_integrity = _parse_integrity(document["final_integrity"], "final counter")
    if not final_integrity.structurally_valid:
        raise EvidenceError("final integrity counter is invalid")
    final_edges = _parse_final_edges(document["final_edges"], contract)
    if final_integrity.edge_count != len(final_edges):
        raise EvidenceError("final integrity counter does not match edge evidence")
    if type(document["evidence_complete"]) is not bool:
        raise EvidenceError("evidence complete flag is invalid")
    evidence_complete = document["evidence_complete"]
    if (
        not contract.evidence.pre_dedup_occurrences
        or not contract.evidence.total_transform_lineage
    ) and evidence_complete:
        raise EvidenceError("evidence cannot be complete for adapter capabilities")

    limitations_value = document["limitations"]
    if (
        not isinstance(limitations_value, list)
        or any(type(item) is not str or not item for item in limitations_value)
        or limitations_value != sorted(set(limitations_value))
    ):
        raise EvidenceError("evidence limitations must be sorted and unique")
    limitations = tuple(limitations_value)
    expected_limitations = _derive_evidence_limitations(
        contract, quarantines, final_edges, pre_dedup=pre_dedup
    )
    if limitations != expected_limitations:
        raise EvidenceError("evidence limitations are inconsistent")
    if _canonical_json(document) != payload:
        raise EvidenceError("graph evidence JSON is not canonical")

    return GraphEvidence(
        graphify_version=contract.version,
        adapter_id=contract.adapter_id,
        source_digest=source_digest,
        projection_digest=projection_digest,
        extraction_invocation_digest=invocation_digest,
        invocation=invocation,
        observed_post_dedup=observed,
        pre_dedup=pre_dedup,
        normalization_repairs=repairs,
        normalization_quarantines=quarantines,
        final_integrity=final_integrity,
        final_edges=final_edges,
        evidence_complete=evidence_complete,
        limitations=limitations,
        payload=payload,
        digest=hashlib.sha256(payload).hexdigest(),
    )


def _bounded_evidence_payload(payload: bytes) -> bytes:
    if type(payload) is not bytes:
        raise EvidenceError("graph evidence payload must be bytes")
    if len(payload) > GRAPH_EVIDENCE_MAX_BYTES:
        raise EvidenceError("graph evidence exceeds size cap")
    return payload


def _validate_invocation(
    invocation: ExtractionInvocation, contract: GraphifyCompatibility
) -> ExtractionInvocation:
    if type(invocation) is not ExtractionInvocation:
        raise EvidenceError("extraction invocation is invalid")
    document = _strict_json_object(invocation.payload, "extraction invocation")
    parsed = _parse_invocation_document(document, contract)
    if parsed != invocation:
        raise EvidenceError("extraction invocation descriptor is invalid")
    return parsed


def _parse_invocation_document(
    value: object, contract: GraphifyCompatibility
) -> ExtractionInvocation:
    _require_exact_keys(
        value,
        {
            "schema_version",
            "adapter_id",
            "graphify_version",
            "executable",
            "capability_smoke_digest",
            "argv",
            "backend",
            "model",
            "configuration_sha256",
            "source_digest",
            "projection_digest",
            "environment_names",
            "artifacts",
        },
        "extraction invocation schema",
    )
    assert isinstance(value, Mapping)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise EvidenceError("extraction invocation schema version is invalid")
    if value["adapter_id"] != contract.adapter_id:
        raise EvidenceError("extraction invocation adapter is invalid")
    if value["graphify_version"] != contract.version:
        raise EvidenceError("extraction invocation Graphify version is invalid")
    executable = value["executable"]
    _require_exact_keys(executable, {"sha256", "version"}, "executable schema")
    assert isinstance(executable, Mapping)
    if executable["version"] != contract.version:
        raise EvidenceError("extraction invocation executable version is invalid")
    executable_digest = _require_digest(executable["sha256"])
    capability_digest = _require_digest(value["capability_smoke_digest"])
    configuration_digest = _require_digest(value["configuration_sha256"])
    source_digest = _require_digest(value["source_digest"])
    projection_digest = _require_digest(value["projection_digest"])

    backend = value["backend"]
    model = value["model"]
    if backend is not None and type(backend) is not str:
        raise EvidenceError("extraction invocation backend is invalid")
    if model is not None and type(model) is not str:
        raise EvidenceError("extraction invocation model is invalid")
    argv_value = value["argv"]
    if not isinstance(argv_value, list):
        raise EvidenceError("extraction invocation argv schema is invalid")
    rendered: list[RenderedCommand] = []
    for item in argv_value:
        _require_exact_keys(item, {"operation", "items"}, "argv item schema")
        assert isinstance(item, Mapping)
        operation = item["operation"]
        items = item["items"]
        if (
            type(operation) is not str
            or not isinstance(items, list)
            or any(type(token) is not str or not token for token in items)
        ):
            raise EvidenceError("extraction invocation argv item is invalid")
        rendered.append(
            RenderedCommand(operation, tuple(items), tuple(items))  # type: ignore[arg-type]
        )

    environment_value = value["environment_names"]
    if not isinstance(environment_value, list):
        raise EvidenceError("extraction invocation environment names are invalid")
    environment_names = tuple(environment_value)

    artifact_value = value["artifacts"]
    if not isinstance(artifact_value, list):
        raise EvidenceError("extraction invocation artifact schema is invalid")
    bindings: list[ArtifactBinding] = []
    for item in artifact_value:
        _require_exact_keys(
            item, {"path", "sha256", "byte_length"}, "artifact binding schema"
        )
        assert isinstance(item, Mapping)
        path = _confined_path(item["path"])
        digest = _require_digest(item["sha256"])
        byte_length = item["byte_length"]
        if type(byte_length) is not int or byte_length < 0:
            raise EvidenceError("artifact binding byte length is invalid")
        bindings.append(ArtifactBinding(path, digest, byte_length))
    if [item.path.as_posix() for item in bindings] != sorted(
        {item.path.as_posix() for item in bindings}
    ):
        raise EvidenceError("extraction invocation artifact paths are not sorted and unique")
    paths = {item.path for item in bindings}
    expected_paths = set(_BASE_ARTIFACT_PATHS)
    if _HTML_ARTIFACT_PATH in paths:
        expected_paths.add(_HTML_ARTIFACT_PATH)
    if paths != expected_paths:
        raise EvidenceError("extraction invocation artifact paths are incomplete")
    commands = tuple(rendered)
    _require_canonical_commands(
        contract,
        commands,
        backend=backend,
        model=model,
        track_html=_HTML_ARTIFACT_PATH in paths,
    )
    _require_environment_names(contract, backend, environment_names)

    document = dict(value)
    payload = _canonical_json(document)
    digest = hashlib.sha256(_INVOCATION_DOMAIN + payload).hexdigest()
    return ExtractionInvocation(
        adapter_id=contract.adapter_id,
        graphify_version=contract.version,
        executable_sha256=executable_digest,
        capability_smoke_digest=capability_digest,
        commands=tuple(item.canonical_argv for item in commands),
        backend=backend,
        model=model,
        configuration_sha256=configuration_digest,
        source_digest=source_digest,
        projection_digest=projection_digest,
        environment_names=environment_names,
        artifacts=tuple(bindings),
        payload=payload,
        digest=digest,
    )


def _require_bound_artifact(
    invocation: ExtractionInvocation,
    artifact: CapturedArtifact,
    expected_path: str,
) -> None:
    _require_artifact_descriptor(artifact)
    binding = next(
        (item for item in invocation.artifacts if item.path.as_posix() == expected_path),
        None,
    )
    if binding is None or (
        artifact.logical_path != binding.path
        or artifact.sha256 != binding.sha256
        or artifact.byte_length != binding.byte_length
        or hashlib.sha256(artifact.payload).hexdigest() != binding.sha256
        or len(artifact.payload) != binding.byte_length
    ):
        raise EvidenceError("evidence artifact binding is invalid")


def _require_artifact_descriptor(
    artifact: object, *, invocation: bool = False
) -> CapturedArtifact:
    description = (
        "invocation artifact descriptor"
        if invocation
        else "evidence artifact binding"
    )
    if type(artifact) is not CapturedArtifact or (
        type(artifact.payload) is not bytes
        or type(artifact.sha256) is not str
        or _HEX_DIGEST.fullmatch(artifact.sha256) is None
        or hashlib.sha256(artifact.payload).hexdigest() != artifact.sha256
        or type(artifact.byte_length) is not int
        or artifact.byte_length != len(artifact.payload)
    ):
        raise EvidenceError(f"{description} is invalid")
    return artifact


def _validate_diagnosis(
    diagnosis: CapturedArtifact,
    observed: GraphIntegrity,
    contract: GraphifyCompatibility,
) -> None:
    if diagnosis.byte_length > _DIAGNOSIS_MAX_BYTES:
        raise EvidenceError("Graphify diagnosis exceeds diagnosis size cap")
    document = _strict_json_object(diagnosis.payload, "Graphify diagnosis")
    try:
        fingerprint = _validate_diagnostic_document(document)
    except CompatibilityError as error:
        raise EvidenceError("Graphify diagnosis schema is invalid") from error
    if fingerprint != contract.diagnostic_schema_fingerprint:
        raise EvidenceError("Graphify diagnosis schema fingerprint is invalid")
    summary = document["summary"]
    expected = {
        "node_count": observed.node_count,
        "raw_edge_count": observed.edge_count,
        "missing_endpoint_edges": observed.missing_endpoint_edges,
        "dangling_endpoint_edges": observed.dangling_endpoint_edges,
        "self_loop_edges": observed.invalid_self_loop_edges,
        "exact_duplicate_edges": observed.exact_duplicate_edges,
        "effective_directed": contract.semantics.directed,
    }
    if any(summary[name] != wanted for name, wanted in expected.items()):
        raise EvidenceError("Graphify diagnosis does not match captured native graph")


def _validate_reason_counts(
    values: object, allowed: frozenset[str]
) -> tuple[ReasonCount, ...]:
    if type(values) is not tuple:
        raise EvidenceError("normalization reason counts must be immutable")
    result: list[ReasonCount] = []
    for item in values:
        if (
            type(item) is not ReasonCount
            or item.code not in allowed
            or type(item.count) is not int
            or item.count <= 0
        ):
            raise EvidenceError("normalization reason counts are invalid")
        result.append(item)
    if [item.code for item in result] != sorted({item.code for item in result}):
        raise EvidenceError("normalization reason counts must be sorted and unique")
    return tuple(result)


def _parse_reason_documents(
    values: object, allowed: frozenset[str]
) -> tuple[ReasonCount, ...]:
    if not isinstance(values, list):
        raise EvidenceError("normalization reason counts schema is invalid")
    result: list[ReasonCount] = []
    for item in values:
        _require_exact_keys(item, {"code", "count"}, "reason count schema")
        assert isinstance(item, Mapping)
        code = item["code"]
        count = item["count"]
        if type(code) is not str or code not in allowed:
            raise EvidenceError("normalization reason counts contain an invalid code")
        if type(count) is not int or count <= 0:
            raise EvidenceError("normalization reason counts contain an invalid count")
        try:
            result.append(ReasonCount(code, count))
        except AdapterContractError as error:
            raise EvidenceError("normalization reason counts are invalid") from error
    validated = _validate_reason_counts(tuple(result), allowed)
    return validated


def _reason_document(item: ReasonCount) -> dict[str, object]:
    return {"code": item.code, "count": item.count}


def _safe_final_edge(
    edge: object, contract: GraphifyCompatibility
) -> FinalEdgeEvidence:
    if not isinstance(edge, Mapping):
        raise EvidenceError("final graph edge is invalid")
    final_id = edge.get("atlasweaver_edge_id")
    if type(final_id) is not str or _FINAL_EDGE_ID.fullmatch(final_id) is None:
        raise EvidenceError("final graph edge ID is invalid")
    source = _nonempty_string(edge.get(contract.semantics.source_field), "source")
    target = _nonempty_string(edge.get(contract.semantics.target_field), "target")
    relation = _nonempty_string(edge.get("relation"), "relation")
    confidence = _validated_confidence(edge.get("confidence"), contract)
    source_file = (
        None if edge.get("source_file") is None else _confined_path(edge["source_file"])
    )
    source_line, source_column = _source_coordinates(edge)
    return FinalEdgeEvidence(
        final_edge_id=final_id,
        source=source,
        target=target,
        relation=relation,
        confidence=confidence,  # type: ignore[arg-type]
        source_file=source_file,
        source_line=source_line,
        source_column=source_column,
    )


def _parse_final_edges(
    values: object, contract: GraphifyCompatibility
) -> tuple[FinalEdgeEvidence, ...]:
    if not isinstance(values, list):
        raise EvidenceError("final edge evidence schema is invalid")
    result: list[FinalEdgeEvidence] = []
    expected_keys = {
        "final_edge_id",
        "source",
        "target",
        "relation",
        "confidence",
        "source_file",
        "source_line",
        "source_column",
    }
    for item in values:
        _require_exact_keys(item, expected_keys, "final edge evidence schema")
        assert isinstance(item, Mapping)
        final_id = item["final_edge_id"]
        if type(final_id) is not str or _FINAL_EDGE_ID.fullmatch(final_id) is None:
            raise EvidenceError("final evidence edge ID is invalid")
        source_file = (
            None if item["source_file"] is None else _confined_path(item["source_file"])
        )
        line, column = _validated_coordinates(
            item["source_line"], item["source_column"]
        )
        result.append(
            FinalEdgeEvidence(
                final_edge_id=final_id,
                source=_nonempty_string(item["source"], "source"),
                target=_nonempty_string(item["target"], "target"),
                relation=_nonempty_string(item["relation"], "relation"),
                confidence=_validated_confidence(  # type: ignore[arg-type]
                    item["confidence"], contract
                ),
                source_file=source_file,
                source_line=line,
                source_column=column,
            )
        )
    ids = [item.final_edge_id for item in result]
    if ids != sorted(set(ids)):
        raise EvidenceError("final evidence edge IDs must be sorted and unique")
    return tuple(result)


def _final_edge_document(item: FinalEdgeEvidence) -> dict[str, object]:
    return {
        "final_edge_id": item.final_edge_id,
        "source": item.source,
        "target": item.target,
        "relation": item.relation,
        "confidence": item.confidence,
        "source_file": item.source_file.as_posix() if item.source_file else None,
        "source_line": item.source_line,
        "source_column": item.source_column,
    }


def _parse_pre_dedup(
    value: object, contract: GraphifyCompatibility
) -> tuple[PreDedupEdgeEvidence, ...] | None:
    if value is None:
        return None
    if not contract.evidence.pre_dedup_occurrences:
        raise EvidenceError("pre-dedup evidence is inconsistent with adapter capabilities")
    if not isinstance(value, list):
        raise EvidenceError("pre-dedup evidence schema is invalid")
    expected_keys = {
        "occurrence_id",
        "source",
        "target",
        "relation",
        "confidence",
        "source_file",
        "source_line",
        "source_column",
        "final_edge_id",
        "disposition",
        "reason",
    }
    result: list[PreDedupEdgeEvidence] = []
    for item in value:
        _require_exact_keys(item, expected_keys, "pre-dedup edge evidence schema")
        assert isinstance(item, Mapping)
        occurrence_id = _nonempty_string(item["occurrence_id"], "occurrence ID")
        if len(occurrence_id.encode("utf-8")) > 256:
            raise EvidenceError("pre-dedup occurrence ID is too long")
        disposition = item["disposition"]
        if disposition not in {"kept", "rewritten", "dropped"} or type(disposition) is not str:
            raise EvidenceError("pre-dedup disposition is invalid")
        final_id = item["final_edge_id"]
        reason = item["reason"]
        if disposition in {"kept", "rewritten"}:
            if type(final_id) is not str or _FINAL_EDGE_ID.fullmatch(final_id) is None or reason is not None:
                raise EvidenceError("pre-dedup lineage is inconsistent")
        elif (
            final_id is not None
            or type(reason) is not str
            or reason not in contract.lineage_reason_codes
        ):
            raise EvidenceError("pre-dedup lineage is inconsistent")
        source_file = (
            None if item["source_file"] is None else _confined_path(item["source_file"])
        )
        line, column = _validated_coordinates(
            item["source_line"], item["source_column"]
        )
        result.append(
            PreDedupEdgeEvidence(
                occurrence_id=occurrence_id,
                source=_nonempty_string(item["source"], "source"),
                target=_nonempty_string(item["target"], "target"),
                relation=_nonempty_string(item["relation"], "relation"),
                confidence=_validated_confidence(  # type: ignore[arg-type]
                    item["confidence"], contract
                ),
                source_file=source_file,
                source_line=line,
                source_column=column,
                final_edge_id=final_id,
                disposition=disposition,
                reason=reason,
            )
        )
    occurrence_ids = [item.occurrence_id for item in result]
    if occurrence_ids != sorted(set(occurrence_ids)):
        raise EvidenceError("pre-dedup occurrence IDs must be sorted and unique")
    return tuple(result)


def _derive_evidence_limitations(
    contract: GraphifyCompatibility,
    quarantines: tuple[ReasonCount, ...],
    final_edges: tuple[FinalEdgeEvidence, ...],
    *,
    pre_dedup: tuple[PreDedupEdgeEvidence, ...] | None,
) -> tuple[str, ...]:
    limitations: set[str] = set()
    if not contract.evidence.pre_dedup_occurrences or pre_dedup is None:
        limitations.add("pre_dedup_edge_projection_unavailable")
    if quarantines:
        limitations.add("raw_endpoint_unresolved")
    if any(item.source_file is None or item.source_line is None for item in final_edges):
        limitations.add("impact_edge_provenance_incomplete")
    return tuple(sorted(limitations))


def _parse_integrity(value: object, description: str) -> GraphIntegrity:
    try:
        integrity = GraphIntegrity.from_dict(value)
    except (IntegrityError, TypeError, KeyError) as error:
        raise EvidenceError(f"{description} is invalid") from error
    if (
        integrity.missing_endpoint_edges + integrity.dangling_endpoint_edges
        > integrity.edge_count
        or integrity.invalid_self_loop_edges > integrity.edge_count
        or integrity.exact_duplicate_edges > integrity.edge_count
        or integrity.conflicting_relation_edges > integrity.edge_count
    ):
        raise EvidenceError(f"{description} is inconsistent")
    return integrity


def _source_coordinates(edge: Mapping[str, object]) -> tuple[int | None, int | None]:
    has_line = "source_line" in edge
    has_column = "source_column" in edge
    location = edge.get("source_location")
    if location is not None:
        if has_line or has_column or type(location) is not str:
            raise EvidenceError("final graph source location is invalid")
        match = _SOURCE_LOCATION.fullmatch(location)
        if match is None:
            raise EvidenceError("final graph source location is invalid")
        return int(match.group("line")), (
            int(match.group("column")) if match.group("column") else None
        )
    return _validated_coordinates(
        edge.get("source_line"), edge.get("source_column")
    )


def _validated_coordinates(
    line: object, column: object
) -> tuple[int | None, int | None]:
    if line is not None and (type(line) is not int or line < 1):
        raise EvidenceError("source line is invalid")
    if column is not None and (type(column) is not int or column < 1):
        raise EvidenceError("source column is invalid")
    if column is not None and line is None:
        raise EvidenceError("source column requires a source line")
    return line, column


def _validated_confidence(
    value: object, contract: GraphifyCompatibility
) -> object:
    if value is None:
        return None
    if type(value) is bool:
        raise EvidenceError("edge confidence is invalid")
    if type(value) in {int, float} and not math.isfinite(value):
        raise EvidenceError("edge confidence is non-finite")
    declared = any(
        type(candidate) is type(value) and candidate == value
        for candidate in contract.semantics.accepted_confidence
    )
    if type(value) not in {str, int, float} or not declared:
        raise EvidenceError("edge confidence is not approved by the adapter")
    return value


def _confined_path(value: object) -> PurePosixPath:
    if (
        type(value) is not str
        or value in {"", "."}
        or "\\" in value
        or "\x00" in value
        or re.match(r"^[A-Za-z]:/", value) is not None
    ):
        raise EvidenceError("evidence path is not confined")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise EvidenceError("evidence path is not confined")
    return path


def _nonempty_string(value: object, description: str) -> str:
    if type(value) is not str or not value:
        raise EvidenceError(f"evidence {description} is invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise EvidenceError(f"evidence {description} is not valid UTF-8") from error
    return value


def _require_exact_keys(
    value: object, expected: set[str], description: str
) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise EvidenceError(f"{description} is invalid")


def _strict_json_object(payload: bytes, description: str) -> dict[str, Any]:
    if type(payload) is not bytes:
        raise EvidenceError(f"{description} payload must be bytes")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise EvidenceError(f"{description} JSON contains duplicate keys")
            result[key] = item
        return result

    def finite(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise EvidenceError(f"{description} JSON contains a non-finite number")
        return parsed

    def constant(value: str) -> object:
        raise EvidenceError(f"{description} JSON contains a non-finite number")

    try:
        parsed = json.loads(
            payload,
            object_pairs_hook=unique,
            parse_constant=constant,
            parse_float=finite,
        )
    except EvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise EvidenceError(f"{description} JSON is malformed") from error
    if not isinstance(parsed, dict):
        raise EvidenceError(f"{description} JSON must contain an object")
    return parsed


def _artifact_bindings(
    artifacts: tuple[CapturedArtifact, ...],
) -> tuple[ArtifactBinding, ...]:
    if type(artifacts) is not tuple:
        raise EvidenceError("invocation artifacts must be an immutable tuple")
    bindings: list[ArtifactBinding] = []
    paths: set[PurePosixPath] = set()
    for item in artifacts:
        _require_artifact_descriptor(item, invocation=True)
        if item.logical_path in paths:
            raise EvidenceError("invocation artifact paths are duplicated")
        paths.add(item.logical_path)
        bindings.append(ArtifactBinding(item.logical_path, item.sha256, item.byte_length))
    expected = set(_BASE_ARTIFACT_PATHS)
    if _HTML_ARTIFACT_PATH in paths:
        expected.add(_HTML_ARTIFACT_PATH)
    if paths != expected:
        raise EvidenceError("invocation artifact paths are incomplete")
    return tuple(sorted(bindings, key=lambda item: item.path.as_posix()))


def _require_canonical_commands(
    contract: GraphifyCompatibility,
    commands: tuple[RenderedCommand, ...],
    *,
    backend: str | None,
    model: str | None,
    track_html: bool,
) -> None:
    if type(commands) is not tuple or tuple(
        command.operation if type(command) is RenderedCommand else None
        for command in commands
    ) != ("extract", "diagnose", "cluster"):
        raise EvidenceError("invocation command order is invalid")

    binary = Path("/logical/graphify")
    source = Path("/logical/source")
    output = Path("/logical/output")
    graph = Path("/logical/graph.json")
    try:
        extract_variants: list[RenderedCommand]
        if backend is None and model is None:
            extract_variants = [
                render_graphify_argv(
                    contract,
                    "extract",
                    binary=binary,
                    source=source,
                    output=output,
                    code_only=True,
                )
            ]
        elif isinstance(backend, str) and isinstance(model, str):
            extract_variants = [
                render_graphify_argv(
                    contract,
                    "extract",
                    binary=binary,
                    source=source,
                    output=output,
                    backend=backend,
                    model=model,
                    deep=deep,
                )
                for deep in (False, True)
            ]
        else:
            raise EvidenceError("invocation backend and model are inconsistent")
        expected_diagnose = render_graphify_argv(
            contract,
            "diagnose",
            binary=binary,
            source=source,
            output=output,
            graph=graph,
        )
        expected_cluster = render_graphify_argv(
            contract,
            "cluster",
            binary=binary,
            source=source,
            output=output,
            graph=graph,
            track_html=track_html,
        )
    except CompatibilityError as error:
        raise EvidenceError("invocation canonical command is invalid") from error

    if commands[0].canonical_argv not in {
        item.canonical_argv for item in extract_variants
    } or commands[1].canonical_argv != expected_diagnose.canonical_argv or commands[
        2
    ].canonical_argv != expected_cluster.canonical_argv:
        raise EvidenceError("invocation canonical command is invalid")
    for command, path_indexes in zip(
        commands,
        ({0, 2, 4}, {0, 4}, {0, 2, 4}),
        strict=True,
    ):
        if (
            type(command.argv) is not tuple
            or len(command.argv) != len(command.canonical_argv)
            or any(type(token) is not str or not token for token in command.argv)
            or any(
                command.argv[index] != command.canonical_argv[index]
                for index in range(len(command.argv))
                if index not in path_indexes
            )
        ):
            raise EvidenceError("invocation actual command disagrees with canonical argv")


def _require_environment_names(
    contract: GraphifyCompatibility,
    backend: str | None,
    environment_names: tuple[str, ...],
) -> None:
    if (
        type(environment_names) is not tuple
        or any(type(name) is not str or not name for name in environment_names)
        or tuple(sorted(set(environment_names))) != environment_names
    ):
        raise EvidenceError("invocation environment names are invalid")
    candidates = set(_BASE_ENVIRONMENT_NAMES)
    candidates.update(
        name for item in contract.backends for name in item.admitted_environment
    )
    try:
        admitted = admitted_graphify_environment(
            contract, backend, {name: "admitted" for name in candidates}
        )
    except CompatibilityError as error:
        raise EvidenceError("invocation environment names are invalid") from error
    if not set(environment_names) <= set(admitted):
        raise EvidenceError("invocation environment names are invalid")


def _require_registered_contract(contract: GraphifyCompatibility) -> None:
    try:
        registered = resolve_graphify_compatibility(contract.version)
    except (AttributeError, CompatibilityError) as error:
        raise EvidenceError("evidence requires a registered compatibility contract") from error
    if contract != registered:
        raise EvidenceError("evidence requires a registered compatibility contract")


def _require_digest(value: object) -> str:
    if type(value) is not str or _HEX_DIGEST.fullmatch(value) is None:
        raise EvidenceError("evidence digest is invalid")
    return value


def _canonical_json(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise EvidenceError("evidence cannot be serialized canonically") from error
