"""Validate Graphify artifacts and durably promote only trusted candidates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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
from typing import Any

from .compatibility import (
    CompatibilityError,
    GraphSemantics,
    resolve_graphify_compatibility,
)
from .integrity import GraphIntegrity, IntegrityError, analyze_graph
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


class ArtifactValidationError(ValueError):
    """Raised when candidate bytes cannot be trusted for promotion."""


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
    impact_analysis_trusted: bool = False


@dataclass(frozen=True)
class PromotionResult:
    """Locations and digest produced by a successful graph transaction."""

    target: Path
    backup: Path
    digest: str
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
    candidate_dir: Path, staged: StagedInput, manifest: ProjectManifest
) -> ValidatedGraph:
    """Validate a complete candidate and attach process-local ownership evidence.

    Graphify 0.9.48's native graph shape is accepted, but the wrapper must stamp
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
    for _, text in artifact_text:
        _reject_text_path_leaks(text, excludes, staged_files)
    graph_digest = hashlib.sha256(graph_bytes).hexdigest()
    artifact_digests = {
        relative.as_posix(): hashlib.sha256(payload).hexdigest()
        for relative, _, payload in artifact_files
    }

    generated_at = datetime.now(timezone.utc).isoformat()
    relative_files = sorted(
        (OWNERSHIP_MANIFEST, *(relative.as_posix() for relative, _, _ in artifact_files))
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
        impact_analysis_trusted=False,
    )
    evidence = _ValidationEvidence(validated, expected_snapshot)
    _VALIDATED_IN_PROCESS[id(validated)] = evidence
    return validated


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
    if _owned_target_digest(target) == candidate.graph_digest:
        return PromotionResult(target, backup, candidate.graph_digest, False)
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

    return PromotionResult(target=target, backup=backup, digest=candidate.graph_digest)


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


def _candidate_files(root: Path) -> tuple[tuple[PurePosixPath, Path], ...]:
    files: list[tuple[PurePosixPath, Path]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = PurePosixPath(path.relative_to(root).as_posix())
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
) -> tuple[tuple[PurePosixPath, Path, bytes], ...]:
    return tuple(
        (relative, path, _read_regular_bytes(path))
        for relative, path in _candidate_files(root)
    )


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
            "Graphify 0.9.48 collapsed edge evidence must remain unknown"
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
    if is_denied(path, excludes):
        raise ArtifactValidationError("excluded source path")
    if path not in staged_files:
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
    if is_denied(path, excludes):
        raise ArtifactValidationError("artifact path leak: excluded path")
    if path not in staged_files:
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


def _snapshot(root: Path) -> tuple[tuple[PurePosixPath, str], ...]:
    return tuple(
        (relative, hashlib.sha256(_read_regular_bytes(path)).hexdigest())
        for relative, path in _candidate_files(root)
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
    manifest_path = target / OWNERSHIP_MANIFEST
    try:
        document = json.loads(_read_regular_bytes(manifest_path))
    except (OSError, ValueError, ArtifactValidationError):
        return None
    if not isinstance(document, dict):
        return None
    value = document.get("graph_digest")
    files = document.get("files")
    artifact_digests = document.get("artifact_digests")
    if (
        not isinstance(value, str)
        or not isinstance(files, list)
        or not isinstance(artifact_digests, dict)
    ):
        return None
    try:
        actual = sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file())
        expected_artifacts = sorted(path for path in files if path != OWNERSHIP_MANIFEST)
        if actual != files or sorted(artifact_digests) != expected_artifacts:
            return None
        for relative, expected_digest in artifact_digests.items():
            if (
                not isinstance(relative, str)
                or not isinstance(expected_digest, str)
                or hashlib.sha256(_read_regular_bytes(target / relative)).hexdigest()
                != expected_digest
            ):
                return None
        if artifact_digests.get("graph.json") != value:
            return None
    except OSError:
        return None
    return value


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
