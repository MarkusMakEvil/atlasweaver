"""Validate Graphify artifacts and durably promote only trusted candidates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import secrets
from typing import Any, Literal
from uuid import UUID

from .adapter import _generation_digest
from .compatibility import (
    CompatibilityError,
    GraphSemantics,
    resolve_graphify_compatibility,
)
from .evidence import (
    EvidenceError,
    GraphEvidence,
    decide_impact_trust,
    index_final_edge_evidence,
    parse_graph_evidence,
)
from .integrity import GraphIntegrity, IntegrityError, analyze_graph, validate_final_graph
from .models import ProjectManifest
from .privacy import effective_excludes, is_denied
from .staging import StagedInput
from .locking import (
    ExclusiveDescriptorLock,
    RepositoryAccess,
    TransactionLockError,
    assert_lifecycle_lock_held,
    capture_lifecycle_descriptors,
    capture_lifecycle_repository,
)


OWNERSHIP_MANIFEST = ".project-knowledge-ownership.json"
_MANAGED_QUERY_CACHE_DIRECTORY = "cache"
_MANAGED_QUERY_CACHE_STAMP = "last_query_stamp"
_OWNERSHIP_SCHEMA_VERSION = 1
_TRANSACTION_SCHEMA_VERSION = 1
_PROVENANCE_VALUES = frozenset({"EXTRACTED", "INFERRED", "AMBIGUOUS"})
_CONFLICT_MARKER = re.compile(r"(?m)^(?:<<<<<<<|=======|>>>>>>>)")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")
_EMBEDDED_ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9])/(?:Users|home|tmp|private|etc|var)/[^\s`'\"<>]+"
)
_HTML_PATH_ATTRIBUTE = re.compile(
    r"(?is)\b(?:href|src)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))"
)
_PATH_ASSIGNMENT = re.compile(
    r"(?i)\b(?:path|file|source|source_file|source_path)\s*=\s*"
    r"(?:\"([^\"]*)\"|'([^']*)'|([^\s,;]+))"
)
_URI_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_SAFE_REFERENCE_SCHEMES = frozenset({"http", "https", "mailto", "data"})
_EXTENSIONLESS_SOURCE_NAMES = frozenset(
    {
        "BUILD",
        "Dockerfile",
        "Justfile",
        "LICENSE",
        "Makefile",
        "Procfile",
        "WORKSPACE",
    }
)
_PATH_FIELDS = frozenset({"source", "source_file", "source_path", "path", "file"})
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_GIT_OID = {
    "sha1": re.compile(r"[0-9a-f]{40}\Z"),
    "sha256": re.compile(r"[0-9a-f]{64}\Z"),
}


class ArtifactValidationError(ValueError):
    """Raised when candidate bytes cannot be trusted for promotion."""


@dataclass(frozen=True)
class GitIdentity:
    commit_oid: str
    algorithm: Literal["sha1", "sha256"]


@dataclass(frozen=True)
class ValidatedGraph:
    """Public summary of a candidate validated in this Python process."""

    root: Path
    source_digest: str
    graph_digest: str
    node_count: int
    edge_count: int
    skipped_count: int = 0
    unapproved_skips: int = 0
    artifact_schema_version: int = 1
    project_uid: UUID | None = None
    adapter_id: str | None = None
    projection_digest: str | None = None
    evidence_digest: str | None = None
    extraction_invocation_digest: str | None = None
    generation_digest: str = ""
    impact_trust: Literal["trusted", "navigation"] = "navigation"
    impact_limitations: tuple[str, ...] = ()
    build_epoch: int | None = None
    git_identity: GitIdentity | None = None

    @property
    def impact_analysis_trusted(self) -> bool:
        return self.impact_trust == "trusted"


@dataclass(frozen=True)
class PromotionResult:
    """Locations and digest produced by a successful graph transaction."""

    target: Path
    backup: Path
    digest: str
    generation_digest: str = ""
    build_epoch: int | None = None
    changed: bool = True


@dataclass(frozen=True)
class _ValidationEvidence:
    candidate: ValidatedGraph
    files: tuple[tuple[PurePosixPath, str], ...]


# Keep strong references so object identities cannot be recycled into trusted
# candidates during the lifetime of this process.  Equality is deliberately not
# sufficient: only the exact object returned by validate_candidate is trusted.
_VALIDATED_IN_PROCESS: dict[int, _ValidationEvidence] = {}


class FileSystem:
    """Durable local-filesystem operations, with named fault checkpoints.

    Tests subclass this boundary and fail one checkpoint at a time while all
    actual reads, writes, renames, and fsyncs remain real.
    """

    def checkpoint(self, operation: str) -> None:
        del operation

    def prepare_private_dir(self, path: Path) -> None:
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            path.mkdir(mode=0o700, parents=True, exist_ok=False)
        else:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ArtifactValidationError(
                    "private directory must not be a symlink"
                )
        os.chmod(path, 0o700)

    def rename(self, source: Path, destination: Path) -> None:
        os.rename(source, destination)

    def fsync_file(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | _nofollow_flag())
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def fsync_directory(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag())
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def remove_tree(self, path: Path) -> None:
        if path.exists() or path.is_symlink():
            shutil.rmtree(path)

    def unlink(self, path: Path) -> None:
        path.unlink(missing_ok=True)


REAL_FS = FileSystem()


def validate_candidate(
    candidate_dir: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    *,
    expected_projection_digest: str | None = None,
    expected_evidence_digest: str | None = None,
    build_epoch: int | None = None,
    git_identity: GitIdentity | None = None,
) -> ValidatedGraph:
    """Validate a complete candidate and attach process-local ownership evidence.

    A supported Graphify native graph shape is accepted, but the wrapper must stamp
    exact ``project_id``, ``graphify_version``, and ``source_digest`` metadata.
    The ownership manifest is written only after all candidate bytes pass.
    """
    try:
        contract = resolve_graphify_compatibility(manifest.graphify_version)
    except CompatibilityError as error:
        raise ArtifactValidationError(str(error)) from error
    root = _require_candidate_directory(candidate_dir)
    required = [root / "graph.json", root / "GRAPH_REPORT.md"]
    if manifest.track_html:
        required.append(root / "graph.html")
    for path in required:
        _require_regular_file(path, "required artifact")
    html = root / "graph.html"
    if not manifest.track_html and (html.exists() or html.is_symlink()):
        raise ArtifactValidationError("HTML policy forbids graph.html")
    if (root / OWNERSHIP_MANIFEST).exists() or (root / OWNERSHIP_MANIFEST).is_symlink():
        raise ArtifactValidationError("candidate already contains reserved ownership manifest")

    artifact_files = _capture_candidate(root)
    excludes = effective_excludes(manifest)
    artifact_text: list[tuple[PurePosixPath, str]] = []
    for relative, _, payload in artifact_files:
        if is_denied(relative, excludes):
            raise ArtifactValidationError("candidate contains an excluded artifact path")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ArtifactValidationError("candidate artifacts must be UTF-8 text") from error
        if _CONFLICT_MARKER.search(text):
            raise ArtifactValidationError(f"conflict marker in {relative.as_posix()}")
        artifact_text.append((relative, text))

    graph_bytes = next(
        payload
        for relative, _, payload in artifact_files
        if relative == PurePosixPath("graph.json")
    )
    document = _load_graph_json(graph_bytes)
    artifact_schema_version = document.get("artifact_schema_version", 1)
    if type(artifact_schema_version) is not int or artifact_schema_version not in {1, 2}:
        raise ArtifactValidationError("artifact schema version is invalid")
    if artifact_schema_version == 2:
        _require_regular_file(root / "GRAPH_EVIDENCE.json", "required artifact")
        allowed = {
            PurePosixPath("graph.json"),
            PurePosixPath("GRAPH_REPORT.md"),
            PurePosixPath("GRAPH_EVIDENCE.json"),
            *((PurePosixPath("graph.html"),) if manifest.track_html else ()),
        }
        if {relative for relative, _, _ in artifact_files} != allowed:
            raise ArtifactValidationError("schema 2 artifact set is invalid")
    elif any(
        value is not None
        for value in (
            expected_projection_digest,
            expected_evidence_digest,
            build_epoch,
            git_identity,
        )
    ):
        raise ArtifactValidationError("legacy artifact validation arguments are invalid")
    _require_identity(document, "project_id", manifest.project_id, "project ID mismatch")
    _require_identity(
        document,
        "graphify_version",
        contract.version,
        "Graphify version mismatch",
    )
    _require_identity(
        document,
        "source_digest",
        staged.source_digest,
        "source digest mismatch",
    )
    nodes, edges = _validate_graph_shape(document, staged, excludes)
    graph_health = _validate_graph_health(
        document,
        nodes,
        edges,
        semantics=contract.semantics,
        collapsed_evidence_available=contract.evidence.pre_dedup_occurrences,
    )
    skipped_count, unapproved_skips = _validate_extraction_coverage(
        document, nodes, edges, staged
    )
    staged_files = frozenset(staged.files)
    for relative, text in artifact_text:
        if relative != PurePosixPath("GRAPH_EVIDENCE.json"):
            _reject_text_path_leaks(text, excludes, staged_files)
    graph_digest = hashlib.sha256(graph_bytes).hexdigest()
    generation_digest = _generation_digest(
        tuple((relative, payload) for relative, _, payload in artifact_files)
    )
    artifact_digests = {
        relative.as_posix(): hashlib.sha256(payload).hexdigest()
        for relative, _, payload in artifact_files
    }

    parsed_evidence: GraphEvidence | None = None
    impact_trust: Literal["trusted", "navigation"] = "navigation"
    impact_limitations: tuple[str, ...] = ("legacy_artifact_schema",)
    if artifact_schema_version == 2:
        if manifest.project_uid is None:
            raise ArtifactValidationError("artifact schema 2 requires project UID")
        _require_positive_epoch(build_epoch)
        git_identity = _validate_git_identity(git_identity)
        expected_projection_digest = _require_digest(
            expected_projection_digest, "expected projection digest"
        )
        expected_evidence_digest = _require_digest(
            expected_evidence_digest, "expected evidence digest"
        )
        if staged.projection_digest != expected_projection_digest:
            raise ArtifactValidationError("projection digest mismatch")
        for forbidden in (
            "impact_trust",
            "impact_limitations",
            "impact_analysis_trusted",
        ):
            if _identity_values(document, forbidden):
                raise ArtifactValidationError("graph contains forged impact trust")
        _require_identity(
            document,
            "projection_digest",
            expected_projection_digest,
            "projection digest mismatch",
        )
        _require_identity(
            document,
            "evidence_digest",
            expected_evidence_digest,
            "evidence digest mismatch",
        )
        evidence_bytes = next(
            payload
            for relative, _, payload in artifact_files
            if relative == PurePosixPath("GRAPH_EVIDENCE.json")
        )
        try:
            parsed_evidence = parse_graph_evidence(
                evidence_bytes, contract, expected_digest=expected_evidence_digest
            )
        except EvidenceError as error:
            raise ArtifactValidationError("graph evidence is invalid") from error
        _validate_graph_evidence_bindings(
            document,
            parsed_evidence,
            staged.source_digest,
            expected_projection_digest,
            graph_health,
            contract.semantics,
        )
        trust = decide_impact_trust(
            parsed_evidence,
            graph_health,
            source_current=True,
            projection_current=True,
            coverage_complete=(unapproved_skips == 0 and skipped_count == 0),
            artifacts_bound=True,
        )
        impact_trust = trust.level
        impact_limitations = trust.limitations
        ownership: dict[str, Any] = {
            "schema_version": 2,
            "artifact_schema_version": 2,
            "project_id": manifest.project_id,
            "project_uid": str(manifest.project_uid),
            "graphify_version": contract.version,
            "adapter_id": contract.adapter_id,
            "source_digest": staged.source_digest,
            "projection_digest": parsed_evidence.projection_digest,
            "extraction_invocation_digest": (
                parsed_evidence.extraction_invocation_digest
            ),
            "evidence_digest": parsed_evidence.digest,
            "graph_digest": graph_digest,
            "generation_digest": generation_digest,
            "build_epoch": build_epoch,
            "impact_trust": impact_trust,
            "impact_limitations": list(impact_limitations),
            "artifacts": {
                relative.as_posix(): {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "byte_length": len(payload),
                }
                for relative, _, payload in artifact_files
            },
        }
        if git_identity is not None:
            ownership["git_commit_oid"] = git_identity.commit_oid
            ownership["git_commit_algorithm"] = git_identity.algorithm
    else:
        generated_at = datetime.now(timezone.utc).isoformat()
        relative_files = sorted(
            (
                OWNERSHIP_MANIFEST,
                *(relative.as_posix() for relative, _, _ in artifact_files),
            )
        )
        ownership = {
            "schema_version": _OWNERSHIP_SCHEMA_VERSION,
            "project_id": manifest.project_id,
            "graphify_version": contract.version,
            "source_digest": staged.source_digest,
            "graph_digest": graph_digest,
            "artifact_digests": artifact_digests,
            "generated_at": generated_at,
            "files": relative_files,
            "skipped_count": skipped_count,
            "unapproved_skips": unapproved_skips,
            "impact_analysis_trusted": False,
        }
    ownership_path = root / OWNERSHIP_MANIFEST
    ownership_payload = _json_payload(ownership)
    _write_new_json(ownership_path, ownership)

    expected_snapshot = tuple(
        sorted(
            (
                *(
                    (relative, hashlib.sha256(payload).hexdigest())
                    for relative, _, payload in artifact_files
                ),
                (
                    PurePosixPath(OWNERSHIP_MANIFEST),
                    hashlib.sha256(ownership_payload).hexdigest(),
                ),
            ),
            key=lambda item: item[0].as_posix(),
        )
    )
    try:
        if _snapshot(root) != expected_snapshot:
            raise ArtifactValidationError("candidate changed during validation")
    except BaseException:
        ownership_path.unlink(missing_ok=True)
        raise

    validated = ValidatedGraph(
        root=root,
        source_digest=staged.source_digest,
        graph_digest=graph_digest,
        node_count=len(nodes),
        edge_count=len(edges),
        skipped_count=skipped_count,
        unapproved_skips=unapproved_skips,
        artifact_schema_version=artifact_schema_version,
        project_uid=(manifest.project_uid if artifact_schema_version == 2 else None),
        adapter_id=(contract.adapter_id if artifact_schema_version == 2 else None),
        projection_digest=(
            None if parsed_evidence is None else parsed_evidence.projection_digest
        ),
        evidence_digest=None if parsed_evidence is None else parsed_evidence.digest,
        extraction_invocation_digest=(
            None
            if parsed_evidence is None
            else parsed_evidence.extraction_invocation_digest
        ),
        generation_digest=generation_digest,
        impact_trust=impact_trust,
        impact_limitations=impact_limitations,
        build_epoch=build_epoch,
        git_identity=git_identity,
    )
    evidence = _ValidationEvidence(validated, expected_snapshot)
    _VALIDATED_IN_PROCESS[id(validated)] = evidence
    return validated


def _require_digest(value: object, description: str) -> str:
    if type(value) is not str or _HEX_DIGEST.fullmatch(value) is None:
        raise ArtifactValidationError(f"{description} is invalid")
    return value


def _require_positive_epoch(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ArtifactValidationError("build epoch is invalid")
    return value


def _validate_git_identity(value: object) -> GitIdentity | None:
    if value is None:
        return None
    if type(value) is not GitIdentity:
        raise ArtifactValidationError("Git identity is invalid")
    pattern = _GIT_OID.get(value.algorithm)
    if pattern is None or type(value.commit_oid) is not str or pattern.fullmatch(
        value.commit_oid
    ) is None:
        raise ArtifactValidationError("Git identity is invalid")
    return value


def _validate_graph_evidence_bindings(
    document: Mapping[str, Any],
    evidence: GraphEvidence,
    source_digest: str,
    projection_digest: str,
    integrity: GraphIntegrity,
    semantics: GraphSemantics,
) -> None:
    if (
        evidence.source_digest != source_digest
        or evidence.projection_digest != projection_digest
        or evidence.extraction_invocation_digest != evidence.invocation.digest
        or evidence.final_integrity != integrity
    ):
        raise ArtifactValidationError("graph evidence binding mismatch")
    _require_identity(
        document,
        "extraction_invocation_digest",
        evidence.extraction_invocation_digest,
        "extraction invocation digest mismatch",
    )
    try:
        computed = validate_final_graph(document, semantics)
        evidence_index = index_final_edge_evidence(evidence)
    except (IntegrityError, EvidenceError) as error:
        raise ArtifactValidationError("graph evidence final edge mismatch") from error
    if computed != integrity:
        raise ArtifactValidationError("graph evidence integrity mismatch")
    member = "links" if "links" in document else "edges"
    actual: dict[str, tuple[object, ...]] = {}
    for edge in document[member]:
        identifier = edge.get("atlasweaver_edge_id")
        line, column = _edge_source_coordinates(edge)
        actual[identifier] = (
            edge.get(semantics.source_field),
            edge.get(semantics.target_field),
            edge.get("relation"),
            edge.get("confidence"),
            edge.get("source_file"),
            line,
            column,
        )
    expected = {
        identifier: (
            item.source,
            item.target,
            item.relation,
            item.confidence,
            None if item.source_file is None else item.source_file.as_posix(),
            item.source_line,
            item.source_column,
        )
        for identifier, item in evidence_index.items()
    }
    if actual != expected:
        raise ArtifactValidationError("graph evidence final edge mismatch")


def _edge_source_coordinates(edge: Mapping[str, Any]) -> tuple[object, object]:
    location = edge.get("source_location")
    if isinstance(location, str):
        match = re.fullmatch(r"L?([1-9][0-9]*)(?::([1-9][0-9]*))?", location)
        if match is None:
            raise ArtifactValidationError("graph evidence final edge mismatch")
        return int(match.group(1)), (
            None if match.group(2) is None else int(match.group(2))
        )
    return edge.get("source_line"), edge.get("source_column")


def validate_owned_graph(
    root: Path,
    manifest: ProjectManifest,
    *,
    expected_source_digest: str | None = None,
    expected_projection_digest: str | None = None,
    repository_access: RepositoryAccess | None = None,
    allow_compatible_configuration: bool = False,
) -> ValidatedGraph:
    """Read and revalidate a complete ownership-bound live graph tree."""
    if type(allow_compatible_configuration) is not bool:
        raise ArtifactValidationError("owned graph compatibility mode is invalid")
    owned_root = _require_candidate_directory(root)
    if repository_access is not None:
        repository_root = _path_for_open_directory(repository_access.descriptor)
        if repository_access.identity != (
            os.fstat(repository_access.descriptor).st_dev,
            os.fstat(repository_access.descriptor).st_ino,
        ) or owned_root != (repository_root / manifest.output_dir).absolute():
            raise ArtifactValidationError("owned graph repository authority mismatch")
    ownership_path = owned_root / OWNERSHIP_MANIFEST
    _require_regular_file(ownership_path, "ownership manifest")
    ownership_payload = _read_regular_bytes(ownership_path)
    if len(ownership_payload) > 1_048_576:
        raise ArtifactValidationError("ownership manifest exceeds size cap")
    ownership = _load_graph_json(ownership_payload)
    schema = ownership.get("schema_version")
    captured = _capture_owned_artifacts(owned_root)
    initial_snapshot = tuple(
        (relative, hashlib.sha256(payload).hexdigest())
        for relative, _, payload in captured
    )
    validation_manifest = manifest
    if allow_compatible_configuration:
        owned_version = ownership.get("graphify_version")
        if type(owned_version) is not str:
            raise ArtifactValidationError("ownership identity mismatch")
        artifacts = ownership.get(
            "artifacts" if schema == 2 else "artifact_digests"
        )
        validation_manifest = replace(
            manifest,
            graphify_version=owned_version,
            track_html=isinstance(artifacts, Mapping) and "graph.html" in artifacts,
        )
    try:
        contract = resolve_graphify_compatibility(validation_manifest.graphify_version)
    except CompatibilityError as error:
        raise ArtifactValidationError(str(error)) from error
    if schema == 2:
        validated = _validate_owned_schema2(
            owned_root,
            captured,
            ownership,
            validation_manifest,
            contract,
            expected_source_digest=expected_source_digest,
            expected_projection_digest=expected_projection_digest,
        )
    elif schema == 1:
        validated = _validate_owned_schema1(
            owned_root,
            captured,
            ownership,
            validation_manifest,
            contract,
            expected_source_digest=expected_source_digest,
        )
    else:
        raise ArtifactValidationError("ownership schema version is invalid")
    _validate_managed_query_cache(owned_root)
    if _snapshot(owned_root, exclude_managed_query_cache=True) != initial_snapshot:
        raise ArtifactValidationError("owned graph changed during validation")
    return validated


def _validate_owned_schema2(
    root: Path,
    captured: tuple[tuple[PurePosixPath, Path, bytes], ...],
    ownership: Mapping[str, Any],
    manifest: ProjectManifest,
    contract: Any,
    *,
    expected_source_digest: str | None,
    expected_projection_digest: str | None,
) -> ValidatedGraph:
    base_keys = {
        "schema_version",
        "artifact_schema_version",
        "project_id",
        "project_uid",
        "graphify_version",
        "adapter_id",
        "source_digest",
        "projection_digest",
        "extraction_invocation_digest",
        "evidence_digest",
        "graph_digest",
        "generation_digest",
        "build_epoch",
        "impact_trust",
        "impact_limitations",
        "artifacts",
    }
    git_keys = {"git_commit_oid", "git_commit_algorithm"}
    if frozenset(ownership) not in {
        frozenset(base_keys),
        frozenset(base_keys | git_keys),
    }:
        raise ArtifactValidationError("ownership schema 2 is invalid")
    if (
        ownership["artifact_schema_version"] != 2
        or manifest.project_uid is None
        or ownership["project_id"] != manifest.project_id
        or ownership["project_uid"] != str(manifest.project_uid)
        or ownership["graphify_version"] != contract.version
        or ownership["adapter_id"] != contract.adapter_id
    ):
        raise ArtifactValidationError("ownership identity mismatch")
    source_digest = _require_digest(ownership["source_digest"], "source digest")
    projection_digest = _require_digest(
        ownership["projection_digest"], "projection digest"
    )
    evidence_digest = _require_digest(ownership["evidence_digest"], "evidence digest")
    invocation_digest = _require_digest(
        ownership["extraction_invocation_digest"], "extraction invocation digest"
    )
    graph_digest = _require_digest(ownership["graph_digest"], "graph digest")
    generation_digest = _require_digest(
        ownership["generation_digest"], "generation digest"
    )
    build_epoch = _require_positive_epoch(ownership["build_epoch"])
    git_identity = _ownership_git_identity(ownership)
    artifacts = ownership["artifacts"]
    if not isinstance(artifacts, dict):
        raise ArtifactValidationError("ownership artifacts are invalid")
    allowed = {
        "graph.json",
        "GRAPH_REPORT.md",
        "GRAPH_EVIDENCE.json",
        *(("graph.html",) if manifest.track_html else ()),
    }
    if set(artifacts) != allowed or OWNERSHIP_MANIFEST in artifacts:
        raise ArtifactValidationError("ownership artifact set is invalid")
    by_path = {relative.as_posix(): payload for relative, _, payload in captured}
    if set(by_path) != allowed | {OWNERSHIP_MANIFEST}:
        raise ArtifactValidationError("owned graph artifact set is invalid")
    artifact_payloads: list[tuple[PurePosixPath, bytes]] = []
    for relative in sorted(allowed):
        descriptor = artifacts[relative]
        if not isinstance(descriptor, dict) or set(descriptor) != {
            "sha256",
            "byte_length",
        }:
            raise ArtifactValidationError("ownership artifact descriptor is invalid")
        payload = by_path[relative]
        expected_digest = _require_digest(
            descriptor["sha256"], "artifact digest"
        )
        if (
            type(descriptor["byte_length"]) is not int
            or descriptor["byte_length"] < 0
        ):
            raise ArtifactValidationError("artifact length is invalid")
        if hashlib.sha256(payload).hexdigest() != expected_digest:
            raise ArtifactValidationError("artifact digest mismatch")
        if len(payload) != descriptor["byte_length"]:
            raise ArtifactValidationError("artifact length mismatch")
        artifact_payloads.append((PurePosixPath(relative), payload))
    if _generation_digest(tuple(artifact_payloads)) != generation_digest:
        raise ArtifactValidationError("generation digest mismatch")
    graph_bytes = by_path["graph.json"]
    if hashlib.sha256(graph_bytes).hexdigest() != graph_digest:
        raise ArtifactValidationError("graph digest mismatch")
    document = _load_graph_json(graph_bytes)
    staged = _owned_staged(document, root, source_digest, projection_digest)
    excludes = effective_excludes(manifest)
    _require_schema2_graph_identity(
        document, manifest, contract, source_digest, projection_digest,
        evidence_digest, invocation_digest,
    )
    nodes, edges = _validate_graph_shape(document, staged, excludes)
    integrity = _validate_graph_health(
        document,
        nodes,
        edges,
        semantics=contract.semantics,
        collapsed_evidence_available=contract.evidence.pre_dedup_occurrences,
    )
    skipped_count, unapproved_skips = _validate_extraction_coverage(
        document, nodes, edges, staged
    )
    try:
        evidence = parse_graph_evidence(
            by_path["GRAPH_EVIDENCE.json"], contract, expected_digest=evidence_digest
        )
    except EvidenceError as error:
        raise ArtifactValidationError("graph evidence is invalid") from error
    _validate_graph_evidence_bindings(
        document,
        evidence,
        source_digest,
        projection_digest,
        integrity,
        contract.semantics,
    )
    generated = decide_impact_trust(
        evidence,
        integrity,
        source_current=True,
        projection_current=True,
        coverage_complete=(unapproved_skips == 0 and skipped_count == 0),
        artifacts_bound=True,
    )
    limitations = ownership["impact_limitations"]
    if (
        ownership["impact_trust"] != generated.level
        or not isinstance(limitations, list)
        or tuple(limitations) != generated.limitations
    ):
        raise ArtifactValidationError("ownership impact trust mismatch")
    _validate_owned_text(captured, manifest, staged)
    source_state = _currency(expected_source_digest, source_digest, "source digest")
    projection_state = _currency(
        expected_projection_digest, projection_digest, "projection digest"
    )
    current = decide_impact_trust(
        evidence,
        integrity,
        source_current=source_state,
        projection_current=projection_state,
        coverage_complete=(unapproved_skips == 0 and skipped_count == 0),
        artifacts_bound=True,
    )
    return ValidatedGraph(
        root=root,
        source_digest=source_digest,
        graph_digest=graph_digest,
        node_count=len(nodes),
        edge_count=len(edges),
        skipped_count=skipped_count,
        unapproved_skips=unapproved_skips,
        artifact_schema_version=2,
        project_uid=manifest.project_uid,
        adapter_id=contract.adapter_id,
        projection_digest=projection_digest,
        evidence_digest=evidence_digest,
        extraction_invocation_digest=invocation_digest,
        generation_digest=generation_digest,
        impact_trust=current.level,
        impact_limitations=current.limitations,
        build_epoch=build_epoch,
        git_identity=git_identity,
    )


def _validate_owned_schema1(
    root: Path,
    captured: tuple[tuple[PurePosixPath, Path, bytes], ...],
    ownership: Mapping[str, Any],
    manifest: ProjectManifest,
    contract: Any,
    *,
    expected_source_digest: str | None,
) -> ValidatedGraph:
    required = {
        "schema_version", "project_id", "graphify_version", "source_digest",
        "graph_digest", "artifact_digests", "generated_at", "files",
        "skipped_count", "unapproved_skips", "impact_analysis_trusted",
    }
    if set(ownership) != required:
        raise ArtifactValidationError("legacy ownership schema is invalid")
    source_digest = _require_digest(ownership["source_digest"], "source digest")
    graph_digest = _require_digest(ownership["graph_digest"], "graph digest")
    if (
        ownership["project_id"] != manifest.project_id
        or ownership["graphify_version"] != contract.version
        or ownership["impact_analysis_trusted"] is not False
    ):
        raise ArtifactValidationError("legacy ownership identity mismatch")
    artifact_digests = ownership["artifact_digests"]
    files = ownership["files"]
    if not isinstance(artifact_digests, dict) or not isinstance(files, list):
        raise ArtifactValidationError("legacy ownership artifacts are invalid")
    by_path = {relative.as_posix(): payload for relative, _, payload in captured}
    if sorted(by_path) != files or set(artifact_digests) != set(by_path) - {
        OWNERSHIP_MANIFEST
    }:
        raise ArtifactValidationError("legacy ownership artifact set is invalid")
    artifact_payloads: list[tuple[PurePosixPath, bytes]] = []
    for relative, expected in artifact_digests.items():
        expected = _require_digest(expected, "artifact digest")
        payload = by_path.get(relative)
        if payload is None or hashlib.sha256(payload).hexdigest() != expected:
            raise ArtifactValidationError("artifact digest mismatch")
        artifact_payloads.append((PurePosixPath(relative), payload))
    graph_bytes = by_path["graph.json"]
    if hashlib.sha256(graph_bytes).hexdigest() != graph_digest:
        raise ArtifactValidationError("graph digest mismatch")
    document = _load_graph_json(graph_bytes)
    staged = _owned_staged(document, root, source_digest, None)
    excludes = effective_excludes(manifest)
    _require_identity(document, "project_id", manifest.project_id, "project ID mismatch")
    _require_identity(document, "graphify_version", contract.version, "Graphify version mismatch")
    _require_identity(document, "source_digest", source_digest, "source digest mismatch")
    nodes, edges = _validate_graph_shape(document, staged, excludes)
    _validate_graph_health(
        document, nodes, edges, semantics=contract.semantics,
        collapsed_evidence_available=contract.evidence.pre_dedup_occurrences,
    )
    skipped, unapproved = _validate_extraction_coverage(document, nodes, edges, staged)
    _validate_owned_text(captured, manifest, staged)
    limitations = {"legacy_artifact_schema", "projection_digest_unverified"}
    if expected_source_digest is None:
        limitations.add("source_digest_unverified")
    elif _require_digest(expected_source_digest, "expected source digest") != source_digest:
        limitations.add("source_digest_stale")
    return ValidatedGraph(
        root=root, source_digest=source_digest, graph_digest=graph_digest,
        node_count=len(nodes), edge_count=len(edges), skipped_count=skipped,
        unapproved_skips=unapproved,
        generation_digest=_generation_digest(tuple(artifact_payloads)),
        impact_limitations=tuple(sorted(limitations)),
    )


def _require_schema2_graph_identity(
    document: Mapping[str, Any], manifest: ProjectManifest, contract: Any,
    source_digest: str, projection_digest: str, evidence_digest: str,
    invocation_digest: str,
) -> None:
    if document.get("artifact_schema_version") != 2:
        raise ArtifactValidationError("artifact schema version mismatch")
    for forbidden in ("impact_trust", "impact_limitations", "impact_analysis_trusted"):
        if _identity_values(document, forbidden):
            raise ArtifactValidationError("graph contains forged impact trust")
    for field, expected, message in (
        ("project_id", manifest.project_id, "project ID mismatch"),
        ("graphify_version", contract.version, "Graphify version mismatch"),
        ("source_digest", source_digest, "source digest mismatch"),
        ("projection_digest", projection_digest, "projection digest mismatch"),
        ("evidence_digest", evidence_digest, "evidence digest mismatch"),
        ("extraction_invocation_digest", invocation_digest, "extraction invocation digest mismatch"),
    ):
        _require_identity(document, field, expected, message)


def _ownership_git_identity(ownership: Mapping[str, Any]) -> GitIdentity | None:
    has_oid = "git_commit_oid" in ownership
    has_algorithm = "git_commit_algorithm" in ownership
    if has_oid != has_algorithm:
        raise ArtifactValidationError("Git identity is invalid")
    if not has_oid:
        return None
    return _validate_git_identity(
        GitIdentity(ownership["git_commit_oid"], ownership["git_commit_algorithm"])
    )


def _owned_staged(
    document: Mapping[str, Any], root: Path, source_digest: str,
    projection_digest: str | None,
) -> StagedInput:
    coverage = document.get("extraction_coverage")
    if not isinstance(coverage, Mapping):
        raise ArtifactValidationError("graph extraction coverage is missing or invalid")
    represented = coverage.get("represented_source_paths")
    skipped = coverage.get("skipped")
    if not isinstance(represented, list) or not isinstance(skipped, list):
        raise ArtifactValidationError("graph extraction coverage lists are invalid")
    paths: list[PurePosixPath] = []
    for value in represented:
        if type(value) is not str:
            raise ArtifactValidationError("graph extraction coverage lists are invalid")
        paths.append(PurePosixPath(value))
    for item in skipped:
        if not isinstance(item, Mapping) or type(item.get("path")) is not str:
            raise ArtifactValidationError("skipped source coverage entry is invalid")
        paths.append(PurePosixPath(item["path"]))
    return StagedInput(root, source_digest, tuple(sorted(paths)), projection_digest)


def _validate_owned_text(
    captured: tuple[tuple[PurePosixPath, Path, bytes], ...],
    manifest: ProjectManifest,
    staged: StagedInput,
) -> None:
    excludes = effective_excludes(manifest)
    staged_files = frozenset(staged.files)
    for relative, _, payload in captured:
        if relative in {
            PurePosixPath(OWNERSHIP_MANIFEST),
            PurePosixPath("GRAPH_EVIDENCE.json"),
        }:
            continue
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ArtifactValidationError("candidate artifacts must be UTF-8 text") from error
        if _CONFLICT_MARKER.search(text):
            raise ArtifactValidationError(f"conflict marker in {relative.as_posix()}")
        _reject_text_path_leaks(text, excludes, staged_files)


def _currency(expected: str | None, actual: str, description: str) -> bool | None:
    if expected is None:
        return None
    return _require_digest(expected, f"expected {description}") == actual


def promote_graph(
    candidate: ValidatedGraph,
    repo_root: Path,
    fs: FileSystem = REAL_FS,
    *,
    repository_access: RepositoryAccess | None = None,
) -> PromotionResult:
    """Stage, journal, backup, and durably promote a validated graph directory.

    Any caught failure after the previous target is moved restores that target.
    A durable journal lets the next invocation recover a process interruption
    before beginning another promotion.
    """
    assert_lifecycle_lock_held(repo_root)
    if repository_access is None:
        with capture_lifecycle_repository(repo_root) as captured:
            return promote_graph(
                candidate,
                repo_root,
                fs,
                repository_access=captured,
            )

    evidence = _VALIDATED_IN_PROCESS.get(id(candidate))
    if evidence is None or evidence.candidate is not candidate:
        raise ArtifactValidationError(
            "candidate was not validated in the current process"
        )
    with capture_lifecycle_descriptors(repo_root) as descriptors:
        try:
            access_info = os.fstat(repository_access.descriptor)
        except OSError:
            raise TransactionLockError(
                "repository access is unavailable", kind="authority"
            ) from None
        if (
            (access_info.st_dev, access_info.st_ino)
            != descriptors.repository_identity
            or repository_access.identity != descriptors.repository_identity
        ):
            raise TransactionLockError(
                "repository identity changed", kind="authority"
            )
        root = _path_for_open_directory(descriptors.repository_descriptor)
        state_root = _path_for_open_directory(descriptors.state_descriptor)
        target = root / "graphify-out"
        if candidate.root == target:
            raise ArtifactValidationError(
                "candidate must be staged outside graphify-out"
            )
        _assert_snapshot(candidate.root, evidence.files)

        staging_parent = state_root / "staging"
        rollback_parent = state_root / "rollback"
        transactions = state_root / "transactions"
        for directory in (staging_parent, rollback_parent, transactions):
            fs.prepare_private_dir(directory)
        fs.fsync_directory(state_root)
        fs.fsync_directory(root)
        transaction_id = secrets.token_hex(16)
        stage = staging_parent / transaction_id
        backup = rollback_parent / transaction_id
        journal = transactions / f"{transaction_id}.json"

        with ExclusiveDescriptorLock(
            descriptors.state_descriptor, "promotion.lock"
        ):
            return _promote_graph_locked(
                candidate,
                evidence,
                root,
                target,
                stage,
                backup,
                journal,
                fs,
            )


def _path_for_open_directory(descriptor: int) -> Path:
    """Return the kernel's current name for an already-authorized directory."""
    try:
        if hasattr(fcntl, "F_GETPATH"):
            payload = fcntl.fcntl(descriptor, fcntl.F_GETPATH, b"\0" * 1024)
            encoded = payload.split(b"\0", 1)[0]
            path = Path(os.fsdecode(encoded))
        else:
            path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        opened = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
    except OSError:
        raise TransactionLockError(
            "repository lifecycle descriptors are unavailable", kind="unavailable"
        ) from None
    if (
        not stat.S_ISDIR(opened.st_mode)
        or stat.S_ISLNK(named.st_mode)
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise TransactionLockError(
            "repository lifecycle descriptors are unavailable", kind="authority"
        )
    return path


def _promote_graph_locked(candidate, evidence, root, target, stage, backup, journal, fs):
    staging_parent, rollback_parent, transactions = stage.parent, backup.parent, journal.parent
    _recover_all_transactions(
        target=target,
        staging_parent=staging_parent,
        rollback_parent=rollback_parent,
        transactions=transactions,
        repo_root=root,
        fs=fs,
    )
    installed = _owned_target_identity(target)
    if installed is not None and installed[1] == candidate.generation_digest:
        return PromotionResult(
            target=target,
            backup=backup,
            digest=installed[0],
            generation_digest=installed[1],
            build_epoch=installed[2],
            changed=False,
        )
    if backup.exists() or backup.is_symlink():
        raise ArtifactValidationError("rollback destination already exists")
    if stage.exists() or stage.is_symlink():
        fs.remove_tree(stage)

    journal_started = False
    try:
        fs.checkpoint("copy-stage")
        _copy_snapshot(candidate.root, stage, evidence.files, fs)
        fs.checkpoint("fsync-stage")
        _fsync_tree(stage, fs)
        fs.fsync_directory(staging_parent)
        _write_journal(journal, candidate.graph_digest, "prepared", fs)
        journal_started = True

        if target.exists() or target.is_symlink():
            _require_real_directory(target, "existing graphify-out")
            fs.checkpoint("rename-backup")
            fs.rename(target, backup)
            _write_journal(journal, candidate.graph_digest, "backed_up", fs)
            fs.checkpoint("fsync-backup")
            fs.fsync_directory(root)
            fs.fsync_directory(rollback_parent)

        fs.checkpoint("rename-candidate")
        fs.rename(stage, target)
        _write_journal(journal, candidate.graph_digest, "promoted", fs)
        fs.checkpoint("fsync-promote")
        fs.fsync_directory(root)
        fs.fsync_directory(staging_parent)
        fs.unlink(journal)
        fs.fsync_directory(transactions)
        if backup.exists():
            fs.remove_tree(backup)
            fs.fsync_directory(rollback_parent)
    except BaseException as error:
        try:
            if journal_started:
                _rollback_transaction(target, stage, backup, journal, root, fs)
            elif stage.exists() or stage.is_symlink():
                fs.remove_tree(stage)
        except BaseException as rollback_error:
            raise ArtifactValidationError(
                "promotion failed and rollback could not be completed"
            ) from rollback_error
        raise error

    installed = _owned_target_identity(target)
    if installed is None or installed[1] != candidate.generation_digest:
        raise ArtifactValidationError("promoted graph ownership is invalid")
    return PromotionResult(
        target=target,
        backup=backup,
        digest=installed[0],
        generation_digest=installed[1],
        build_epoch=installed[2],
    )


def _require_candidate_directory(path: Path) -> Path:
    absolute = path.absolute()
    _require_real_directory(absolute, "candidate")
    return absolute.resolve()


def _require_repository_directory(path: Path) -> Path:
    absolute = path.absolute()
    _require_real_directory(absolute, "repository root")
    return absolute.resolve()


def _require_real_directory(path: Path, description: str) -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise ArtifactValidationError(f"{description} must be a directory") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArtifactValidationError(f"{description} must be a directory, not a symlink")


def _require_regular_file(path: Path, description: str) -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise ArtifactValidationError(f"missing required artifact: {path.name}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ArtifactValidationError(f"{description} must be a regular file: {path.name}")


def _candidate_files(
    root: Path, *, exclude_managed_query_cache: bool = False
) -> tuple[tuple[PurePosixPath, Path], ...]:
    files: list[tuple[PurePosixPath, Path]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if (
            exclude_managed_query_cache
            and relative.parts
            and relative.parts[0] == _MANAGED_QUERY_CACHE_DIRECTORY
        ):
            continue
        info = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise ArtifactValidationError("candidate artifacts must be regular files")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactValidationError("candidate artifacts must be regular files")
        files.append((relative, path))
    return tuple(files)


def _capture_candidate(
    root: Path,
    *,
    exclude_managed_query_cache: bool = False,
) -> tuple[tuple[PurePosixPath, Path, bytes], ...]:
    return tuple(
        (relative, path, _read_regular_bytes(path))
        for relative, path in _candidate_files(
            root, exclude_managed_query_cache=exclude_managed_query_cache
        )
    )


def _capture_owned_artifacts(
    root: Path,
) -> tuple[tuple[PurePosixPath, Path, bytes], ...]:
    """Capture only ownership-bound artifacts after checking Graphify's sidecar."""
    _validate_managed_query_cache(root)
    return _capture_candidate(root, exclude_managed_query_cache=True)


def _validate_managed_query_cache(root: Path) -> None:
    """Accept Graphify's exact query sidecar without treating it as trusted data."""
    try:
        root_fd = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | os.O_CLOEXEC,
        )
    except OSError as error:
        raise ArtifactValidationError("managed graph query cache is invalid") from error
    try:
        try:
            cache_fd = os.open(
                _MANAGED_QUERY_CACHE_DIRECTORY,
                os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag() | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return
        except OSError:
            raise ArtifactValidationError(
                "managed graph query cache is invalid"
            ) from None
        try:
            cache_info = os.fstat(cache_fd)
            names = tuple(sorted(os.listdir(cache_fd)))
            if names != (_MANAGED_QUERY_CACHE_STAMP,):
                raise ArtifactValidationError(
                    "managed graph query cache is invalid"
                )
            stamp_info = os.stat(
                _MANAGED_QUERY_CACHE_STAMP,
                dir_fd=cache_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(stamp_info.st_mode) or not stat.S_ISREG(
                stamp_info.st_mode
            ):
                raise ArtifactValidationError(
                    "managed graph query cache is invalid"
                )
            stamp_fd = os.open(
                _MANAGED_QUERY_CACHE_STAMP,
                os.O_RDONLY
                | _nofollow_flag()
                | os.O_CLOEXEC
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=cache_fd,
            )
            try:
                opened_stamp = os.fstat(stamp_fd)
                if (
                    not stat.S_ISREG(opened_stamp.st_mode)
                    or (opened_stamp.st_dev, opened_stamp.st_ino)
                    != (stamp_info.st_dev, stamp_info.st_ino)
                ):
                    raise ArtifactValidationError(
                        "managed graph query cache is invalid"
                    )
            finally:
                os.close(stamp_fd)
            named_cache = os.stat(
                _MANAGED_QUERY_CACHE_DIRECTORY,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(named_cache.st_mode)
                or (named_cache.st_dev, named_cache.st_ino)
                != (cache_info.st_dev, cache_info.st_ino)
            ):
                raise ArtifactValidationError(
                    "managed graph query cache is invalid"
                )
        except ArtifactValidationError:
            raise
        except OSError:
            raise ArtifactValidationError(
                "managed graph query cache is invalid"
            ) from None
        finally:
            os.close(cache_fd)
    finally:
        os.close(root_fd)


def _read_regular_bytes(path: Path) -> bytes:
    _require_regular_file(path, "artifact")
    descriptor = os.open(path, os.O_RDONLY | _nofollow_flag())
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactValidationError("artifact must remain a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _nofollow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as error:
        raise ArtifactValidationError("platform cannot reject artifact symlinks") from error


def _load_graph_json(payload: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ArtifactValidationError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise ArtifactValidationError(
            f"graph.json contains malformed JSON constant: {value}"
        )

    def parse_finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ArtifactValidationError("graph.json contains a non-finite number")
        return parsed

    try:
        document = json.loads(
            payload,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
            parse_float=parse_finite_float,
        )
    except ArtifactValidationError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ArtifactValidationError("graph.json contains malformed JSON") from error
    if not isinstance(document, dict):
        raise ArtifactValidationError("graph.json must contain a JSON object")
    return document


def _identity_values(document: Mapping[str, Any], field: str) -> list[Any]:
    values: list[Any] = []
    for container in (
        document,
        document.get("metadata"),
        document.get("graph"),
    ):
        if isinstance(container, Mapping) and field in container:
            values.append(container[field])
    return values


def _require_identity(
    document: Mapping[str, Any], field: str, expected: str, message: str
) -> None:
    values = _identity_values(document, field)
    if not values or any(value != expected for value in values):
        raise ArtifactValidationError(message)


def _validate_graph_shape(
    document: Mapping[str, Any],
    staged: StagedInput,
    excludes: tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        raise ArtifactValidationError("graph nodes must be a list")
    if "edges" in document and "links" in document:
        raise ArtifactValidationError("graph must not contain both edges and links")
    edges = document.get("links", document.get("edges"))
    if not isinstance(edges, list):
        raise ArtifactValidationError("graph edges must be a list")
    if not nodes:
        raise ArtifactValidationError("graph must contain at least one node")

    staged_files = frozenset(staged.files)
    identifiers: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ArtifactValidationError(f"node {index} must be an object")
        identifier = node.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ArtifactValidationError(f"node {index} must have a non-empty string id")
        if identifier in identifiers:
            raise ArtifactValidationError(f"node id is duplicated: {identifier}")
        identifiers.add(identifier)
        if "provenance" in node and node["provenance"] not in _PROVENANCE_VALUES:
            raise ArtifactValidationError(f"node {index} has invalid provenance")
        _validate_source_fields(node, staged_files, excludes, in_edge=False)

    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise ArtifactValidationError(f"edge {index} must be an object")
        source = edge.get("source")
        target = edge.get("target")
        if not isinstance(source, str) or source not in identifiers:
            raise ArtifactValidationError(f"edge {index} has an invalid source node")
        if not isinstance(target, str) or target not in identifiers:
            raise ArtifactValidationError(f"edge {index} has an invalid target node")
        relation = _reconcile_edge_alias(
            edge, index, "relation", "type", "relation aliases"
        )
        if not isinstance(relation, str) or not relation:
            raise ArtifactValidationError(f"edge {index} must have a relation")
        provenance = _reconcile_edge_alias(
            edge, index, "provenance", "confidence", "provenance aliases"
        )
        if provenance not in _PROVENANCE_VALUES:
            raise ArtifactValidationError(f"edge {index} has invalid provenance")
        _validate_source_fields(edge, staged_files, excludes, in_edge=True)

    return nodes, edges


def _validate_extraction_coverage(
    document: Mapping[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    staged: StagedInput,
) -> tuple[int, int]:
    coverage = document.get("extraction_coverage")
    if not isinstance(coverage, Mapping) or set(coverage) != {
        "schema_version", "total_staged_files", "represented_source_paths", "skipped"
    }:
        raise ArtifactValidationError("graph extraction coverage is missing or invalid")
    if type(coverage["schema_version"]) is not int or coverage["schema_version"] != 1:
        raise ArtifactValidationError("graph extraction coverage schema is invalid")
    if coverage["total_staged_files"] != len(staged.files):
        raise ArtifactValidationError("graph extraction coverage total does not match staged input")
    represented_raw = coverage["represented_source_paths"]
    skipped_raw = coverage["skipped"]
    if not isinstance(represented_raw, list) or not isinstance(skipped_raw, list):
        raise ArtifactValidationError("graph extraction coverage lists are invalid")
    represented = tuple(PurePosixPath(value) for value in represented_raw if isinstance(value, str))
    if len(represented) != len(represented_raw) or list(represented_raw) != sorted(set(represented_raw)):
        raise ArtifactValidationError("represented source paths must be sorted and unique")
    skipped: dict[PurePosixPath, bool] = {}
    for item in skipped_raw:
        if not isinstance(item, Mapping) or set(item) != {"path", "reason", "approved"}:
            raise ArtifactValidationError("skipped source coverage entry is invalid")
        path, reason, approved = item["path"], item["reason"], item["approved"]
        if not isinstance(path, str) or not isinstance(reason, str) or not reason.strip() or not isinstance(approved, bool):
            raise ArtifactValidationError("skipped source coverage entry is invalid")
        relative = PurePosixPath(path)
        if relative in skipped:
            raise ArtifactValidationError("skipped source paths must be unique")
        skipped[relative] = approved
    staged_set = set(staged.files)
    if set(represented) & set(skipped) or set(represented) | set(skipped) != staged_set:
        raise ArtifactValidationError("graph extraction coverage does not bind the staged input")
    actual: set[PurePosixPath] = set()
    def collect(value: Any, *, edge: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in _PATH_FIELDS and not (edge and key in {"source", "target"}) and isinstance(item, str):
                    actual.add(PurePosixPath(item))
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    for node in nodes:
        collect(node)
    for edge in edges:
        collect(edge, edge=True)
    if set(represented) != actual:
        raise ArtifactValidationError("represented source paths do not match graph provenance")
    return len(skipped), sum(not approved for approved in skipped.values())


def _validate_graph_health(
    document: Mapping[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    semantics: GraphSemantics,
    collapsed_evidence_available: bool,
) -> GraphIntegrity:
    recorded = document.get("graph_health")
    try:
        recorded_integrity = GraphIntegrity.from_dict(recorded)
    except IntegrityError as error:
        raise ArtifactValidationError("graph health is missing or invalid") from error
    if (
        not collapsed_evidence_available
        and recorded_integrity.collapsed_edges is not None
    ):
        raise ArtifactValidationError(
            "navigation-only Graphify collapsed edge evidence must remain unknown"
        )
    try:
        computed = analyze_graph(
            nodes,
            edges,
            semantics=semantics,
            collapsed_edges=recorded_integrity.collapsed_edges,
        )
    except (IntegrityError, TypeError) as error:
        raise ArtifactValidationError("graph health is invalid") from error
    if recorded_integrity != computed:
        raise ArtifactValidationError("graph health does not match graph content")
    if not computed.structurally_valid:
        raise ArtifactValidationError("graph health contains structural defects")
    return computed


def _reconcile_edge_alias(
    edge: Mapping[str, Any],
    index: int,
    primary: str,
    alias: str,
    description: str,
) -> Any:
    if primary in edge and alias in edge and edge[primary] != edge[alias]:
        raise ArtifactValidationError(f"conflicting edge {index} {description}")
    if primary in edge:
        return edge[primary]
    return edge.get(alias)


def _validate_source_fields(
    value: Any,
    staged_files: frozenset[PurePosixPath],
    excludes: tuple[str, ...],
    *,
    in_edge: bool,
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            is_endpoint = in_edge and key in {"source", "target"}
            if key in _PATH_FIELDS and not is_endpoint:
                _validate_source_path(item, staged_files, excludes)
            else:
                _validate_source_fields(
                    item,
                    staged_files,
                    excludes,
                    in_edge=False,
                )
    elif isinstance(value, list):
        for item in value:
            _validate_source_fields(item, staged_files, excludes, in_edge=False)


def _validate_source_path(
    value: Any,
    staged_files: frozenset[PurePosixPath],
    excludes: tuple[str, ...],
) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ArtifactValidationError("source must be a confined relative source path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ArtifactValidationError("source must be a confined relative source path")
    if path in staged_files:
        return
    if is_denied(path, excludes):
        raise ArtifactValidationError("excluded source path")
    raise ArtifactValidationError("source path is not present in staged input")


def _reject_text_path_leaks(
    text: str,
    excludes: tuple[str, ...],
    staged_files: frozenset[PurePosixPath],
) -> None:
    if _EMBEDDED_ABSOLUTE.search(text):
        raise ArtifactValidationError("artifact path leak: absolute path")

    for match in _HTML_PATH_ATTRIBUTE.finditer(text):
        value = next(group for group in match.groups() if group is not None)
        _reject_artifact_path_token(
            value,
            excludes,
            staged_files,
            source_context=True,
            reference_context=True,
        )
    for match in _PATH_ASSIGNMENT.finditer(text):
        value = next(group for group in match.groups() if group is not None)
        _reject_artifact_path_token(
            value,
            excludes,
            staged_files,
            source_context=True,
            reference_context=False,
        )

    markdown_references = _scan_markdown_references(text)
    for value, _, _ in markdown_references:
        _reject_artifact_path_token(
            value,
            excludes,
            staged_files,
            source_context=True,
            reference_context=True,
        )

    scan_characters = list(text)
    for _, start, end in markdown_references:
        scan_characters[start:end] = " " * (end - start)
    scan_text = re.sub(r"</?[A-Za-z][^>]*>", " ", "".join(scan_characters))
    for raw in re.split(r"[\s`'\"<>()\[\]{},;]+", scan_text):
        _reject_artifact_path_token(
            raw,
            excludes,
            staged_files,
            source_context=False,
            reference_context=False,
        )


def _scan_markdown_references(text: str) -> tuple[tuple[str, int, int], ...]:
    """Return Markdown destinations in one forward pass over each line.

    This intentionally recognizes only destination-bearing constructs: inline
    links/images, URI autolinks, and reference definitions. Reference usages do
    not contain paths themselves. Once an inline-link opener is recognized, a
    malformed destination fails closed instead of restarting a suffix scan at
    every later ``](`` marker. That keeps adversarial input linear-time.
    """
    references: list[tuple[str, int, int]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        definition = _reference_definition_destination(content, offset)
        if definition is not None:
            references.append(definition)
        references.extend(_inline_and_autolink_destinations(content, offset))
        offset += len(line)
    return tuple(references)


def _reference_definition_destination(
    line: str, offset: int
) -> tuple[str, int, int] | None:
    cursor = 0
    while cursor < len(line) and cursor < 3 and line[cursor] == " ":
        cursor += 1
    if cursor >= len(line) or line[cursor] != "[":
        return None

    label_end = _find_unescaped(line, "]", cursor + 1)
    if label_end < 0 or label_end + 1 >= len(line) or line[label_end + 1] != ":":
        return None
    if label_end == cursor + 1:
        raise ArtifactValidationError("malformed Markdown reference definition")

    cursor = _skip_horizontal_space(line, label_end + 2)
    if cursor >= len(line):
        raise ArtifactValidationError("malformed Markdown reference definition")
    start, end, cursor = _parse_destination(line, cursor, definition=True)
    cursor = _skip_horizontal_space(line, cursor)
    if cursor < len(line):
        cursor = _parse_markdown_title(line, cursor)
        if _skip_horizontal_space(line, cursor) != len(line):
            raise ArtifactValidationError("malformed Markdown reference definition")
    return line[start:end], offset + start, offset + end


def _inline_and_autolink_destinations(
    line: str, offset: int
) -> list[tuple[str, int, int]]:
    references: list[tuple[str, int, int]] = []
    bracket_depth = 0
    cursor = 0
    while cursor < len(line):
        character = line[cursor]
        if character == "\\" and cursor + 1 < len(line):
            cursor += 2
            continue
        if character == "`":
            run_end = cursor + 1
            while run_end < len(line) and line[run_end] == "`":
                run_end += 1
            delimiter = line[cursor:run_end]
            closing = line.find(delimiter, run_end)
            if closing < 0:
                return references
            cursor = closing + len(delimiter)
            continue
        if character == "<":
            closing = line.find(">", cursor + 1)
            if closing < 0:
                return references
            value = line[cursor + 1 : closing]
            if _URI_SCHEME.match(value) and not any(char.isspace() for char in value):
                references.append((value, offset + cursor + 1, offset + closing))
            cursor = closing + 1
            continue
        if character == "[":
            bracket_depth += 1
            cursor += 1
            continue
        if (
            character == "]"
            and bracket_depth
            and cursor + 1 < len(line)
            and line[cursor + 1] == "("
        ):
            destination_cursor = _skip_horizontal_space(line, cursor + 2)
            if destination_cursor >= len(line):
                raise ArtifactValidationError("malformed Markdown inline reference")
            if line[destination_cursor] == ")":
                references.append(
                    ("", offset + destination_cursor, offset + destination_cursor)
                )
                bracket_depth -= 1
                cursor = destination_cursor + 1
                continue
            start, end, after_destination = _parse_destination(
                line, destination_cursor, definition=False
            )
            closing = _parse_inline_reference_closing(line, after_destination)
            references.append((line[start:end], offset + start, offset + end))
            bracket_depth -= 1
            cursor = closing
            continue
        if character == "]" and bracket_depth:
            bracket_depth -= 1
        cursor += 1
    return references


def _parse_destination(
    line: str, cursor: int, *, definition: bool
) -> tuple[int, int, int]:
    if line[cursor] == "<":
        end = _find_unescaped(line, ">", cursor + 1)
        if end < 0:
            raise ArtifactValidationError("malformed Markdown destination")
        if any(character in "<>\r\n" for character in line[cursor + 1 : end]):
            raise ArtifactValidationError("malformed Markdown destination")
        return cursor + 1, end, end + 1

    start = cursor
    depth = 0
    while cursor < len(line):
        character = line[cursor]
        if character == "\\" and cursor + 1 < len(line):
            cursor += 2
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                if definition:
                    break
                return start, cursor, cursor
            depth -= 1
        elif character in " \t" and depth == 0:
            break
        elif character in "<>\r\n":
            raise ArtifactValidationError("malformed Markdown destination")
        cursor += 1
    if cursor == start or depth:
        raise ArtifactValidationError("malformed Markdown destination")
    return start, cursor, cursor


def _parse_inline_reference_closing(line: str, cursor: int) -> int:
    cursor = _skip_horizontal_space(line, cursor)
    if cursor < len(line) and line[cursor] == ")":
        return cursor + 1
    cursor = _parse_markdown_title(line, cursor)
    cursor = _skip_horizontal_space(line, cursor)
    if cursor >= len(line) or line[cursor] != ")":
        raise ArtifactValidationError("malformed Markdown inline reference")
    return cursor + 1


def _parse_markdown_title(line: str, cursor: int) -> int:
    if cursor >= len(line) or line[cursor] not in "\"'(":
        raise ArtifactValidationError("malformed Markdown reference title")
    opening = line[cursor]
    closing = ")" if opening == "(" else opening
    cursor += 1
    while cursor < len(line):
        character = line[cursor]
        if character == "\\" and cursor + 1 < len(line):
            cursor += 2
            continue
        if character == closing:
            return cursor + 1
        cursor += 1
    raise ArtifactValidationError("malformed Markdown reference title")


def _skip_horizontal_space(text: str, cursor: int) -> int:
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    return cursor


def _find_unescaped(text: str, needle: str, cursor: int) -> int:
    while cursor < len(text):
        if text[cursor] == "\\" and cursor + 1 < len(text):
            cursor += 2
            continue
        if text[cursor] == needle:
            return cursor
        cursor += 1
    return -1


def _reject_artifact_path_token(
    raw: str,
    excludes: tuple[str, ...],
    staged_files: frozenset[PurePosixPath],
    *,
    source_context: bool,
    reference_context: bool,
) -> None:
    reference = raw.strip()
    if not reference:
        return
    if _WINDOWS_ABSOLUTE.match(reference):
        raise ArtifactValidationError("artifact path leak: absolute path")
    scheme_match = _URI_SCHEME.match(reference)
    if (
        scheme_match
        and scheme_match.group(1).casefold() in _SAFE_REFERENCE_SCHEMES
    ):
        return
    if reference_context:
        if reference.startswith("#") or reference.startswith("//"):
            return
        if scheme_match:
            scheme = scheme_match.group(1).casefold()
            raise ArtifactValidationError(
                f"artifact path leak: unsafe URI scheme {scheme}"
            )
    reference = reference.partition("#")[0].partition("?")[0]

    token = reference.lstrip(":!?|#").rstrip(".:!?|#")
    if not token:
        return
    normalized = token.replace("\\", "/")

    path = PurePosixPath(normalized)
    file_like = (
        bool(path.suffix)
        or normalized.startswith(".env")
        or path.name in _EXTENSIONLESS_SOURCE_NAMES
    )
    if normalized.startswith("/"):
        raise ArtifactValidationError("artifact path leak: absolute path")

    staged_roots = {staged.parts[0] for staged in staged_files if staged.parts}
    under_staged_root = bool(path.parts) and path.parts[0] in staged_roots

    looks_like_path = (
        source_context
        or ("/" in normalized and file_like)
        or ("/" in normalized and under_staged_root)
        or normalized.startswith(("./", "../"))
        or normalized.startswith(".env")
        or (
            "." in path.name
            and any(
                word in normalized.casefold()
                for word in ("token", "credential", "secret")
            )
        )
    )
    if not looks_like_path:
        return
    if path in staged_files or (
        len(path.parts) == 1
        and sum(item.name == path.name for item in staged_files) == 1
    ):
        return
    if is_denied(path, excludes):
        raise ArtifactValidationError("artifact path leak: excluded path")
    raise ArtifactValidationError("artifact path leak: unstaged source path")


def _write_new_json(path: Path, document: Mapping[str, Any]) -> None:
    payload = _json_payload(document)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)


def _json_payload(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _snapshot(
    root: Path, *, exclude_managed_query_cache: bool = False
) -> tuple[tuple[PurePosixPath, str], ...]:
    return tuple(
        (relative, hashlib.sha256(_read_regular_bytes(path)).hexdigest())
        for relative, path in _candidate_files(
            root, exclude_managed_query_cache=exclude_managed_query_cache
        )
    )


def _assert_snapshot(
    root: Path, expected: tuple[tuple[PurePosixPath, str], ...]
) -> None:
    try:
        actual = _snapshot(root)
    except (OSError, ArtifactValidationError) as error:
        raise ArtifactValidationError("candidate changed after validation") from error
    if actual != expected:
        raise ArtifactValidationError("candidate changed after validation")


def _copy_snapshot(
    source: Path,
    destination: Path,
    expected: tuple[tuple[PurePosixPath, str], ...],
    fs: FileSystem,
) -> None:
    fs.prepare_private_dir(destination)
    try:
        for relative, expected_digest in expected:
            payload = _read_regular_bytes(source.joinpath(*relative.parts))
            if hashlib.sha256(payload).hexdigest() != expected_digest:
                raise ArtifactValidationError("candidate changed after validation")
            parent = destination
            for component in relative.parts[:-1]:
                parent = parent / component
                fs.prepare_private_dir(parent)
            target = destination.joinpath(*relative.parts)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                _write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _assert_snapshot(destination, expected)
    except BaseException:
        fs.remove_tree(destination)
        raise


def _fsync_tree(root: Path, fs: FileSystem) -> None:
    directories = [root]
    for path in root.rglob("*"):
        if path.is_dir():
            directories.append(path)
        else:
            fs.fsync_file(path)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        fs.fsync_directory(directory)


def _write_journal(
    journal: Path, graph_digest: str, state: str, fs: FileSystem
) -> None:
    payload = json.dumps(
        {
            "schema_version": _TRANSACTION_SCHEMA_VERSION,
            "state": state,
            "graph_digest": graph_digest,
        },
        sort_keys=True,
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{journal.name}.", suffix=".tmp", dir=journal.parent
    )
    temporary = Path(temporary_name)
    try:
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, journal)
    finally:
        temporary.unlink(missing_ok=True)
    fs.fsync_directory(journal.parent)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("filesystem write made no progress")
        remaining = remaining[written:]


def _read_journal(journal: Path, graph_digest: str) -> str:
    try:
        document = json.loads(_read_regular_bytes(journal))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ArtifactValidationError("promotion journal is malformed") from error
    if not isinstance(document, dict):
        raise ArtifactValidationError("promotion journal is malformed")
    if document.get("schema_version") != _TRANSACTION_SCHEMA_VERSION:
        raise ArtifactValidationError("promotion journal has an unsupported schema")
    if document.get("graph_digest") != graph_digest:
        raise ArtifactValidationError("promotion journal digest mismatch")
    state = document.get("state")
    if state not in {"prepared", "backed_up", "promoted"}:
        raise ArtifactValidationError("promotion journal has an invalid state")
    return str(state)


def _recover_all_transactions(
    *,
    target: Path,
    staging_parent: Path,
    rollback_parent: Path,
    transactions: Path,
    repo_root: Path,
    fs: FileSystem,
) -> None:
    for journal in sorted(transactions.glob("*.json")):
        info = journal.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactValidationError("promotion journal must be a regular file")
        transaction_id = journal.stem
        if len(transaction_id) not in {32, 64} or any(
            character not in "0123456789abcdef" for character in transaction_id
        ):
            raise ArtifactValidationError("promotion journal has an invalid name")
        try:
            document = json.loads(_read_regular_bytes(journal))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ArtifactValidationError("promotion journal is malformed") from error
        if not isinstance(document, dict) or not isinstance(document.get("graph_digest"), str):
            raise ArtifactValidationError("promotion journal is malformed")
        graph_digest = document["graph_digest"]
        _recover_interrupted(
            target=target,
            stage=staging_parent / transaction_id,
            backup=rollback_parent / transaction_id,
            journal=journal,
            graph_digest=graph_digest,
            repo_root=repo_root,
            fs=fs,
        )


def _recover_interrupted(
    *,
    target: Path,
    stage: Path,
    backup: Path,
    journal: Path,
    graph_digest: str,
    repo_root: Path,
    fs: FileSystem,
) -> None:
    if not journal.exists():
        return
    state = _read_journal(journal, graph_digest)
    if state == "prepared" and backup.exists() and not target.exists():
        _require_real_directory(backup, "rollback graph")
        fs.rename(backup, target)
        fs.checkpoint("fsync-recovery")
        fs.fsync_directory(repo_root)
        fs.fsync_directory(backup.parent)
    elif state in {"backed_up", "promoted"} and backup.exists():
        _require_real_directory(backup, "rollback graph")
        if target.exists() or target.is_symlink():
            _require_real_directory(target, "interrupted promoted target")
            fs.remove_tree(target)
        fs.rename(backup, target)
        fs.checkpoint("fsync-recovery")
        fs.fsync_directory(repo_root)
        fs.fsync_directory(backup.parent)
    elif (
        state in {"prepared", "promoted"}
        and not backup.exists()
        and not stage.exists()
        and target.exists()
        and _owned_target_digest(target) == graph_digest
    ):
        # A first promotion has no previous graph and therefore no backup.  If
        # the candidate rename became visible before the process died, remove
        # the ownership-proven uncommitted target to recover the prior absence.
        _require_real_directory(target, "interrupted promoted target")
        fs.remove_tree(target)
        fs.checkpoint("fsync-recovery")
        fs.fsync_directory(repo_root)
    if stage.exists() or stage.is_symlink():
        fs.remove_tree(stage)
    fs.unlink(journal)
    fs.fsync_directory(journal.parent)


def _owned_target_digest(target: Path) -> str | None:
    identity = _owned_target_identity(target)
    return None if identity is None else identity[0]


def _owned_target_identity(target: Path) -> tuple[str, str, int | None] | None:
    manifest_path = target / OWNERSHIP_MANIFEST
    try:
        document = _load_graph_json(_read_regular_bytes(manifest_path))
    except (OSError, ValueError, ArtifactValidationError):
        return None
    value = document.get("graph_digest")
    if type(value) is not str or _HEX_DIGEST.fullmatch(value) is None:
        return None
    try:
        captured = _capture_owned_artifacts(target)
        by_path = {relative.as_posix(): payload for relative, _, payload in captured}
        if document.get("schema_version") == 2:
            artifacts = document.get("artifacts")
            generation = document.get("generation_digest")
            epoch = document.get("build_epoch")
            if (
                not isinstance(artifacts, dict)
                or type(generation) is not str
                or _HEX_DIGEST.fullmatch(generation) is None
                or type(epoch) is not int
                or epoch < 1
                or set(by_path) != set(artifacts) | {OWNERSHIP_MANIFEST}
            ):
                return None
            payloads: list[tuple[PurePosixPath, bytes]] = []
            for relative, descriptor in artifacts.items():
                if not isinstance(descriptor, dict) or set(descriptor) != {
                    "sha256",
                    "byte_length",
                }:
                    return None
                payload = by_path.get(relative)
                if (
                    payload is None
                    or type(descriptor["sha256"]) is not str
                    or hashlib.sha256(payload).hexdigest() != descriptor["sha256"]
                    or type(descriptor["byte_length"]) is not int
                    or len(payload) != descriptor["byte_length"]
                ):
                    return None
                payloads.append((PurePosixPath(relative), payload))
            if (
                hashlib.sha256(by_path["graph.json"]).hexdigest() != value
                or _generation_digest(tuple(payloads)) != generation
            ):
                return None
            return value, generation, epoch

        files = document.get("files")
        artifact_digests = document.get("artifact_digests")
        if not isinstance(files, list) or not isinstance(artifact_digests, dict):
            return None
        expected_artifacts = sorted(path for path in files if path != OWNERSHIP_MANIFEST)
        if sorted(by_path) != files or sorted(artifact_digests) != expected_artifacts:
            return None
        payloads = []
        for relative, expected_digest in artifact_digests.items():
            payload = by_path.get(relative)
            if (
                type(expected_digest) is not str
                or payload is None
                or hashlib.sha256(payload).hexdigest() != expected_digest
            ):
                return None
            payloads.append((PurePosixPath(relative), payload))
        if artifact_digests.get("graph.json") != value:
            return None
        return value, _generation_digest(tuple(payloads)), None
    except (OSError, ArtifactValidationError, KeyError):
        return None


def _rollback_transaction(
    target: Path,
    stage: Path,
    backup: Path,
    journal: Path,
    repo_root: Path,
    fs: FileSystem,
) -> None:
    if backup.exists() or backup.is_symlink():
        _require_real_directory(backup, "rollback graph")
        if target.exists() or target.is_symlink():
            _require_real_directory(target, "failed promoted target")
            fs.remove_tree(target)
        fs.rename(backup, target)
        fs.fsync_directory(repo_root)
        fs.fsync_directory(backup.parent)
    elif not stage.exists() and (target.exists() or target.is_symlink()):
        # With no previous graph, the candidate rename consumes ``stage``.  A
        # later failure must remove that uncommitted target so failure leaves
        # the pre-transaction state (no graph) intact.
        _require_real_directory(target, "failed promoted target")
        fs.remove_tree(target)
        fs.fsync_directory(repo_root)
    if stage.exists() or stage.is_symlink():
        fs.remove_tree(stage)
    fs.unlink(journal)
    fs.fsync_directory(journal.parent)
