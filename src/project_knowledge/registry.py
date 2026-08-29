"""Atomic, evidence-bound per-user project registry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import tempfile
from typing import Any, Literal
from uuid import UUID

from .artifacts import (
    OWNERSHIP_MANIFEST,
    ArtifactValidationError,
    ValidatedGraph,
    validate_owned_graph,
)
from .compatibility import (
    CompatibilityError,
    render_graphify_global_add,
    resolve_graphify_compatibility,
)
from .graphify import (
    CommandRunner,
    GraphifyError,
    ResolvedGraphifyExecutable,
    SubprocessCommandRunner,
    resolve_graphify_executable,
    run_graphify_operation,
)
from .locking import (
    ExclusiveDescriptorLock,
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
    load_manifest_payload,
    render_manifest_v2,
    require_current_manifest,
)
from .models import ProjectManifest, ProjectionSnapshot
from .queries import (
    CapturedQueryGraph,
    QueryEnvelope,
    RegistryQueryRequest,
    query_captured_graphs,
)
from .staging import StagingError, inspect_projection


REGISTRY_SCHEMA_VERSION = 1
REGISTRY_NAME = "registry.json"
REGISTRY_LOCK = "registry.lock"
REGISTRY_JOURNAL = "registry-transaction.json"
MAX_REGISTRY_SNAPSHOT_BYTES = 268_435_456
GRAPHIFY_PROJECTION_DOMAIN = b"atlasweaver-graphify-projection-v1\0"
REGISTRY_SNAPSHOT_DOMAIN = b"atlasweaver-registry-snapshot-v1\0"

DOCUMENTED_REGISTRY_ERROR_CODES = frozenset(
    {
        "registry_disabled",
        "registry_busy",
        "registry_unmanaged_state",
        "registry_recovery_required",
        "registry_source_changed",
        "manifest_migration_required",
        "registry_snapshot_missing",
        "registry_snapshot_stale",
        "registry_snapshot_mismatch",
        "registry_snapshot_too_large",
        "registry_snapshot_busy",
    }
)

_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REGISTRY_KEY = re.compile(
    r"atlasweaver/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_ENTRY_FIELDS = frozenset(
    {
        "project_uid",
        "project_id",
        "graphify_version",
        "adapter_id",
        "source_digest",
        "projection_digest",
        "graph_digest",
        "evidence_digest",
        "generation_digest",
        "build_epoch",
        "impact_trust",
        "impact_limitations",
        "ownership_sha256",
        "manifest_sha256",
        "snapshot_digest",
        "snapshot",
    }
)


@dataclass
class RegistryError(Exception):
    code: str
    message: str

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)


@dataclass(frozen=True)
class RegistrySnapshotEntry:
    project_uid: UUID
    project_id: str
    registry_key: str
    graphify_version: str
    adapter_id: str
    graph_digest: str
    evidence_digest: str
    generation_digest: str
    snapshot_digest: str
    source_digest: str
    projection_digest: str
    graph_payload: bytes = field(repr=False)
    evidence_payload: bytes = field(repr=False)
    impact_trust: Literal["trusted", "navigation"]
    impact_limitations: tuple[str, ...]


@dataclass(frozen=True)
class RegistrySnapshot:
    generation: int
    registry_digest: str
    graphify_projection_digest: str
    entries: tuple[RegistrySnapshotEntry, ...]


@dataclass(frozen=True)
class RegistryStatus:
    status: Literal["disabled", "missing", "stale", "mismatch", "current"]
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegistrySyncResult:
    status: Literal["synced", "unchanged"]
    key: str
    generation: int
    graph_digest: str
    generation_digest: str
    snapshot_digest: str


class RegistryFileSystem:
    """Durable filesystem seam with named interruption checkpoints."""

    def checkpoint(self, operation: str) -> None:
        del operation


REAL_REGISTRY_FS = RegistryFileSystem()


@dataclass(frozen=True)
class _CapturedGeneration:
    manifest: ProjectManifest
    manifest_payload: bytes = field(repr=False)
    manifest_sha256: str
    ownership_payload: bytes = field(repr=False)
    ownership_sha256: str
    files: Mapping[str, bytes] = field(repr=False)
    projection: ProjectionSnapshot
    validated: ValidatedGraph


@dataclass(frozen=True)
class _OpenedDirectory:
    path: Path
    descriptor: int

    def close(self) -> None:
        os.close(self.descriptor)


def registry_key(manifest: ProjectManifest) -> str:
    if (
        type(manifest) is not ProjectManifest
        or manifest.schema_version != 2
        or type(manifest.project_uid) is not UUID
        or manifest.project_uid.version != 4
    ):
        raise RegistryError(
            "manifest_migration_required", "portable identity is required"
        )
    return f"atlasweaver/{manifest.project_uid}"


def registry_status(
    repo_root: Path,
    manifest: ProjectManifest,
    *,
    user_root: Path | None = None,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> RegistryStatus:
    """Inspect one registry entry and projection without creating state."""
    with open_repository_access(
        repo_root,
        expected_repository_identity=expected_repository_identity,
    ) as repository:
        selected = require_current_manifest(
            repo_root, manifest, repository_access=repository
        )
        if selected.features.registry == "disabled":
            assert_current_manifest_unchanged(
                repo_root, selected, repository_access=repository
            )
            return RegistryStatus("disabled")
        try:
            key = registry_key(selected)
        except RegistryError:
            raise

        home = _user_root(user_root)
        atlas_path = home / "atlasweaver"
        if not _path_exists(atlas_path):
            assert_current_manifest_unchanged(
                repo_root, selected, repository_access=repository
            )
            return RegistryStatus("missing")
        try:
            atlas = _open_directory(atlas_path, exact_mode=0o700)
        except RegistryError:
            return _status_after_manifest_check(
                repo_root, selected, repository, "mismatch"
            )
        try:
            if not _entry_exists(atlas.descriptor, REGISTRY_NAME):
                issue = (
                    ("registry_recovery_required",)
                    if _entry_exists(atlas.descriptor, REGISTRY_JOURNAL)
                    else ()
                )
                result = RegistryStatus("mismatch", issue) if issue else RegistryStatus("missing")
                assert_current_manifest_unchanged(
                    repo_root, selected, repository_access=repository
                )
                return result
            if not _entry_exists(atlas.descriptor, REGISTRY_LOCK):
                return _status_after_manifest_check(
                    repo_root, selected, repository, "mismatch"
                )
            try:
                with ExclusiveDescriptorLock(
                    atlas.descriptor,
                    REGISTRY_LOCK,
                    timeout=0.0,
                    create=False,
                    shared=True,
                ):
                    return _registry_status_locked(
                        repo_root,
                        selected,
                        repository,
                        home,
                        atlas,
                        key,
                    )
            except TransactionLockError as error:
                if error.kind == "busy":
                    return _status_after_manifest_check(
                        repo_root,
                        selected,
                        repository,
                        "mismatch",
                        ("registry_busy",),
                    )
                return _status_after_manifest_check(
                    repo_root, selected, repository, "mismatch"
                )
        finally:
            atlas.close()


def registry_sync(
    repo_root: Path,
    manifest: ProjectManifest,
    *,
    user_root: Path | None = None,
    runner: CommandRunner | None = None,
    graphify_binary: Path | None = None,
    fs: RegistryFileSystem = REAL_REGISTRY_FS,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> RegistrySyncResult:
    """Capture one healthy owned generation and atomically project all entries."""
    if not isinstance(fs, RegistryFileSystem):
        raise RegistryError(
            "registry_recovery_required", "registry filesystem is invalid"
        )
    with open_repository_access(
        repo_root,
        expected_repository_identity=expected_repository_identity,
    ) as admission_repository:
        selected = require_current_manifest(
            repo_root, manifest, repository_access=admission_repository
        )
        if selected.schema_version != 2 or selected.project_uid is None:
            raise RegistryError(
                "manifest_migration_required", "portable identity is required"
            )
        if selected.features.registry != "enabled":
            raise RegistryError("registry_disabled", "registry is disabled")
        key = registry_key(selected)
        admitted_identity = admission_repository.identity
        assert_current_manifest_unchanged(
            repo_root, selected, repository_access=admission_repository
        )

    executable = resolve_graphify_executable(test_override=graphify_binary)
    selected_runner = runner or SubprocessCommandRunner()
    home = _user_root(user_root)
    with repository_lifecycle_lock(
        repo_root,
        expected_repository_identity=admitted_identity,
    ), capture_lifecycle_repository(repo_root) as repository:
        selected = require_current_manifest(
            repo_root, selected, repository_access=repository
        )
        admission = _capture_live_generation(repo_root, selected, repository)
        assert_current_manifest_unchanged(
            repo_root, selected, repository_access=repository
        )
        atlas = _open_or_create_atlas_root(home)
        try:
            try:
                with ExclusiveDescriptorLock(
                    atlas.descriptor, REGISTRY_LOCK, timeout=5.0, create=True
                ):
                    return _registry_sync_locked(
                        repo_root,
                        selected,
                        repository,
                        admission,
                        home,
                        atlas,
                        key,
                        executable,
                        selected_runner,
                        fs,
                    )
            except TransactionLockError as error:
                if error.kind == "busy":
                    raise RegistryError("registry_busy", "registry is busy") from None
                raise RegistryError(
                    "registry_recovery_required", "registry lock is unavailable"
                ) from None
        finally:
            atlas.close()


def capture_registry_snapshot(
    project_uids: tuple[UUID, ...],
    *,
    require_graphify_projection: bool,
    user_root: Path | None = None,
) -> RegistrySnapshot:
    """Atomically capture validated registry generations without live paths."""
    if (
        type(project_uids) is not tuple
        or not project_uids
        or type(require_graphify_projection) is not bool
        or any(
            type(uid) is not UUID or uid.version != 4 or str(UUID(str(uid))) != str(uid)
            for uid in project_uids
        )
        or len(set(project_uids)) != len(project_uids)
    ):
        raise RegistryError(
            "registry_snapshot_mismatch", "registry snapshot request is invalid"
        )
    home = _user_root(user_root)
    atlas_path = home / "atlasweaver"
    if not _path_exists(atlas_path):
        raise RegistryError(
            "registry_snapshot_missing", "registry snapshot is missing"
        )
    try:
        atlas = _open_directory(atlas_path, exact_mode=0o700)
    except RegistryError:
        raise RegistryError(
            "registry_snapshot_mismatch", "registry snapshot is invalid"
        ) from None
    try:
        if not _entry_exists(atlas.descriptor, REGISTRY_NAME) or not _entry_exists(
            atlas.descriptor, REGISTRY_LOCK
        ):
            raise RegistryError(
                "registry_snapshot_missing", "registry snapshot is missing"
            )
        try:
            with ExclusiveDescriptorLock(
                atlas.descriptor,
                REGISTRY_LOCK,
                timeout=0.0,
                create=False,
                shared=True,
            ):
                if _entry_exists(atlas.descriptor, REGISTRY_JOURNAL):
                    raise RegistryError(
                        "registry_snapshot_stale", "registry recovery is pending"
                    )
                try:
                    document, registry_payload = _read_registry(atlas.descriptor)
                except RegistryError:
                    raise RegistryError(
                        "registry_snapshot_mismatch", "registry snapshot is invalid"
                    ) from None
                expected_projection = (
                    document["graphify_projection"]["global_graph_sha256"],
                    document["graphify_projection"]["global_manifest_sha256"],
                )
                before_projection: tuple[str, str] | None = None
                if require_graphify_projection:
                    try:
                        before_projection = _capture_graphify_hashes(home)
                    except (RegistryError, OSError):
                        raise RegistryError(
                            "registry_snapshot_mismatch",
                            "Graphify projection is invalid",
                        ) from None
                    if before_projection != expected_projection:
                        raise RegistryError(
                            "registry_snapshot_mismatch",
                            "Graphify projection is invalid",
                        )
                entries: list[RegistrySnapshotEntry] = []
                retained = 0
                raw_entries = document["entries"]
                assert isinstance(raw_entries, Mapping)
                for uid in project_uids:
                    key = f"atlasweaver/{uid}"
                    entry = raw_entries.get(key)
                    if not isinstance(entry, Mapping):
                        raise RegistryError(
                            "registry_snapshot_missing",
                            "registry snapshot entry is missing",
                        )
                    with tempfile.TemporaryDirectory(
                        prefix="atlasweaver-registry-capture-"
                    ) as temporary:
                        captured, byte_length = _capture_snapshot_entry(
                            atlas.path,
                            key,
                            entry,
                            Path(temporary),
                            MAX_REGISTRY_SNAPSHOT_BYTES - retained,
                        )
                    retained += byte_length
                    if retained > MAX_REGISTRY_SNAPSHOT_BYTES:
                        raise RegistryError(
                            "registry_snapshot_too_large",
                            "registry snapshot is too large",
                        )
                    entries.append(captured)
                _, after_payload = _read_registry(atlas.descriptor)
                if after_payload != registry_payload:
                    raise RegistryError(
                        "registry_snapshot_stale", "registry changed during capture"
                    )
                if require_graphify_projection:
                    try:
                        after_projection = _capture_graphify_hashes(home)
                    except (RegistryError, OSError):
                        raise RegistryError(
                            "registry_snapshot_mismatch",
                            "Graphify projection changed during capture",
                        ) from None
                    if after_projection != before_projection:
                        raise RegistryError(
                            "registry_snapshot_mismatch",
                            "Graphify projection changed during capture",
                        )
                projection_digest = hashlib.sha256(
                    GRAPHIFY_PROJECTION_DOMAIN
                    + _canonical_json(
                        {
                            "global_graph_sha256": expected_projection[0],
                            "global_manifest_sha256": expected_projection[1],
                        }
                    )
                ).hexdigest()
                return RegistrySnapshot(
                    generation=document["generation"],
                    registry_digest=_digest(registry_payload),
                    graphify_projection_digest=projection_digest,
                    entries=tuple(entries),
                )
        except TransactionLockError as error:
            if error.kind == "busy":
                raise RegistryError(
                    "registry_snapshot_busy", "registry snapshot is busy"
                ) from None
            raise RegistryError(
                "registry_snapshot_mismatch", "registry lock is unavailable"
            ) from None
    finally:
        atlas.close()


def query_registry(
    snapshot: RegistrySnapshot,
    request: RegistryQueryRequest,
) -> QueryEnvelope:
    """Run a bounded query over only already-captured registry bytes."""
    if type(snapshot) is not RegistrySnapshot or type(snapshot.entries) is not tuple:
        from .queries import QueryError

        raise QueryError("query_input_invalid", "registry query is invalid")
    graphs = tuple(
        CapturedQueryGraph(
            registry_key=entry.registry_key,
            project_id=entry.project_id,
            graph_payload=entry.graph_payload,
            evidence_payload=entry.evidence_payload,
            evidence_digest=entry.evidence_digest,
            contract=resolve_graphify_compatibility(entry.graphify_version),
            trust=entry.impact_trust,
            limitations=entry.impact_limitations,
        )
        for entry in snapshot.entries
    )
    return query_captured_graphs(graphs, request)


def _registry_sync_locked(
    repo_root: Path,
    manifest: ProjectManifest,
    repository: RepositoryAccess,
    admission: _CapturedGeneration,
    home: Path,
    atlas: _OpenedDirectory,
    key: str,
    executable: ResolvedGraphifyExecutable,
    runner: CommandRunner,
    fs: RegistryFileSystem,
) -> RegistrySyncResult:
    _recover_registry_transaction(home, atlas)
    existing_document: dict[str, object] | None = None
    existing_payload: bytes | None = None
    if _entry_exists(atlas.descriptor, REGISTRY_NAME):
        existing_document, existing_payload = _read_registry(atlas.descriptor)
        expected_hashes = (
            existing_document["graphify_projection"]["global_graph_sha256"],
            existing_document["graphify_projection"]["global_manifest_sha256"],
        )
        try:
            if _capture_graphify_hashes(home) != expected_hashes:
                raise RegistryError(
                    "registry_recovery_required",
                    "managed Graphify projection is inconsistent",
                )
        except (RegistryError, OSError):
            raise RegistryError(
                "registry_recovery_required",
                "managed Graphify projection is inconsistent",
            ) from None
    elif _graphify_projection_exists(home):
        raise RegistryError(
            "registry_unmanaged_state",
            "existing Graphify global state is not managed by AtlasWeaver",
        )

    fs.checkpoint("copy-project-snapshot")
    snapshot_digest = _snapshot_digest(
        admission.validated.generation_digest,
        admission.ownership_sha256,
        admission.manifest_sha256,
    )
    snapshot_relative = PurePosixPath(
        "snapshots", str(manifest.project_uid), snapshot_digest
    )
    snapshot_path, snapshot_created = _install_snapshot(
        atlas.path,
        snapshot_relative,
        admission,
    )
    try:
        fs.checkpoint("validate-project-snapshot")
        _validate_snapshot_directory(snapshot_path, _entry_from_admission(
            key, admission, snapshot_digest, snapshot_relative
        ))
        new_entry = _entry_from_admission(
            key, admission, snapshot_digest, snapshot_relative
        )
        existing_entries: dict[str, dict[str, object]] = {}
        old_generation = 0
        if existing_document is not None:
            old_generation = existing_document["generation"]
            assert isinstance(old_generation, int)
            raw_entries = existing_document["entries"]
            assert isinstance(raw_entries, Mapping)
            for existing_key, raw_entry in raw_entries.items():
                assert isinstance(existing_key, str) and isinstance(raw_entry, Mapping)
                existing_entries[existing_key] = dict(raw_entry)
                with tempfile.TemporaryDirectory(
                    prefix="atlasweaver-registry-existing-"
                ) as temporary:
                    _capture_snapshot_entry(
                        atlas.path,
                        existing_key,
                        raw_entry,
                        Path(temporary),
                        MAX_REGISTRY_SNAPSHOT_BYTES,
                    )

        if (
            existing_document is not None
            and existing_entries.get(key) == new_entry
        ):
            _require_registry_project_admission_current(
                repo_root, manifest, repository, admission
            )
            assert existing_payload is not None
            if snapshot_created:
                _remove_snapshot(snapshot_path, atlas.path)
            return RegistrySyncResult(
                "unchanged",
                key,
                old_generation,
                admission.validated.graph_digest,
                admission.validated.generation_digest,
                snapshot_digest,
            )

        entries = {**existing_entries, key: new_entry}
        fs.checkpoint("build-graphify-projection")
        graph_payload, manifest_payload = _build_graphify_projection(
            repo_root,
            manifest,
            repository,
            atlas.path,
            entries,
            executable,
            runner,
        )
        _require_registry_project_admission_current(
            repo_root, manifest, repository, admission
        )
        graph_hash = _digest(graph_payload)
        graphify_manifest_hash = _digest(manifest_payload)
        generation = old_generation + 1
        registry_document = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "generation": generation,
            "graphify_projection": {
                "global_graph_sha256": graph_hash,
                "global_manifest_sha256": graphify_manifest_hash,
            },
            "entries": {name: entries[name] for name in sorted(entries)},
        }
        registry_payload = _canonical_json(registry_document)
        _commit_registry_transaction(
            home,
            atlas,
            registry_payload,
            graph_payload,
            manifest_payload,
            snapshot_relative,
            snapshot_created,
            fs,
        )
        current_registry, current_payload = _read_registry(atlas.descriptor)
        if (
            current_payload != registry_payload
            or current_registry["generation"] != generation
            or _capture_graphify_hashes(home)
            != (graph_hash, graphify_manifest_hash)
        ):
            raise RegistryError(
                "registry_recovery_required", "registry commit verification failed"
            )
        assert_current_manifest_unchanged(
            repo_root, manifest, repository_access=repository
        )
        return RegistrySyncResult(
            "synced",
            key,
            generation,
            admission.validated.graph_digest,
            admission.validated.generation_digest,
            snapshot_digest,
        )
    except (ManifestError, GraphifyError):
        if not _entry_exists(atlas.descriptor, REGISTRY_JOURNAL) and snapshot_created:
            _remove_snapshot(snapshot_path, atlas.path)
        raise
    except RegistryError:
        if not _entry_exists(atlas.descriptor, REGISTRY_JOURNAL) and snapshot_created:
            _remove_snapshot(snapshot_path, atlas.path)
        raise
    except (OSError, TypeError, ValueError) as error:
        if not _entry_exists(atlas.descriptor, REGISTRY_JOURNAL) and snapshot_created:
            _remove_snapshot(snapshot_path, atlas.path)
        raise RegistryError(
            "registry_recovery_required", "registry synchronization failed"
        ) from error


def _capture_live_generation(
    repo_root: Path,
    manifest: ProjectManifest,
    repository: RepositoryAccess,
) -> _CapturedGeneration:
    try:
        projection = inspect_projection(
            repo_root, manifest, repository_access=repository
        )
        if projection.projection_digest is None:
            raise ValueError("projection digest missing")
        live = validate_owned_graph(
            _repository_output_path(repository, manifest),
            manifest,
            expected_source_digest=projection.source_digest,
            expected_projection_digest=projection.projection_digest,
            repository_access=repository,
        )
        if (
            live.artifact_schema_version != 2
            or live.project_uid != manifest.project_uid
            or live.skipped_count != 0
            or live.unapproved_skips != 0
            or live.evidence_digest is None
            or live.build_epoch is None
        ):
            raise ValueError("owned graph is not healthy")
        files = _capture_owned_files(repository, manifest.output_dir)
        ownership_payload = files[OWNERSHIP_MANIFEST]
        manifest_payload = render_manifest_v2(manifest)
        with tempfile.TemporaryDirectory(
            prefix="atlasweaver-registry-admission-"
        ) as temporary:
            root = Path(temporary) / "snapshot"
            _write_snapshot_tree(root, files, manifest_payload)
            copied = validate_owned_graph(
                root / "owned",
                manifest,
                expected_source_digest=projection.source_digest,
                expected_projection_digest=projection.projection_digest,
            )
        if _validated_signature(copied) != _validated_signature(live):
            raise ValueError("owned graph capture mismatch")
        assert_current_manifest_unchanged(
            repo_root, manifest, repository_access=repository
        )
        return _CapturedGeneration(
            manifest,
            manifest_payload,
            _digest(manifest_payload),
            ownership_payload,
            _digest(ownership_payload),
            dict(files),
            projection,
            live,
        )
    except ManifestError:
        raise
    except (ArtifactValidationError, OSError, StagingError, TypeError, ValueError):
        raise RegistryError(
            "registry_source_changed", "project changed during registry sync"
        ) from None


def _require_registry_project_admission_current(
    repo_root: Path,
    manifest: ProjectManifest,
    repository: RepositoryAccess,
    admitted: _CapturedGeneration,
) -> None:
    assert_current_manifest_unchanged(
        repo_root, manifest, repository_access=repository
    )
    try:
        current = _capture_live_generation(repo_root, manifest, repository)
    except ManifestError:
        raise
    except RegistryError:
        raise RegistryError(
            "registry_source_changed", "project changed during registry sync"
        ) from None
    if (
        current.projection != admitted.projection
        or current.manifest_sha256 != admitted.manifest_sha256
        or current.ownership_sha256 != admitted.ownership_sha256
        or _validated_signature(current.validated)
        != _validated_signature(admitted.validated)
    ):
        raise RegistryError(
            "registry_source_changed", "project changed during registry sync"
        )
    assert_current_manifest_unchanged(
        repo_root, manifest, repository_access=repository
    )


def _validated_signature(value: ValidatedGraph) -> tuple[object, ...]:
    return (
        value.artifact_schema_version,
        value.project_uid,
        value.adapter_id,
        value.source_digest,
        value.projection_digest,
        value.graph_digest,
        value.evidence_digest,
        value.extraction_invocation_digest,
        value.generation_digest,
        value.node_count,
        value.edge_count,
        value.skipped_count,
        value.unapproved_skips,
        value.impact_trust,
        value.impact_limitations,
        value.build_epoch,
        value.git_identity,
    )


def _capture_owned_files(
    repository: RepositoryAccess, output: PurePosixPath
) -> dict[str, bytes]:
    descriptor = _open_directory_components(repository.descriptor, output.parts)
    try:
        ownership, _ = _read_file_at(
            descriptor, OWNERSHIP_MANIFEST, 1_048_576
        )
        document = _strict_json(ownership)
        if (
            not isinstance(document, Mapping)
            or document.get("schema_version") != 2
            or not isinstance(document.get("artifacts"), Mapping)
        ):
            raise OSError("ownership invalid")
        artifact_names = tuple(sorted(document["artifacts"]))
        if any(not _safe_root_file_name(name) for name in artifact_names):
            raise OSError("ownership invalid")
        expected = {OWNERSHIP_MANIFEST, *artifact_names}
        actual = set(os.listdir(descriptor))
        if "cache" in actual:
            _validate_managed_query_cache(descriptor)
            actual.remove("cache")
        if actual != expected:
            raise OSError("owned tree mismatch")
        result = {OWNERSHIP_MANIFEST: ownership}
        total = len(ownership)
        for name in artifact_names:
            payload, _ = _read_file_at(
                descriptor, name, MAX_REGISTRY_SNAPSHOT_BYTES - total
            )
            total += len(payload)
            if total > MAX_REGISTRY_SNAPSHOT_BYTES:
                raise OSError("owned tree too large")
            result[name] = payload
        return result
    finally:
        os.close(descriptor)


def _validate_managed_query_cache(output_fd: int) -> None:
    """Reject all runtime sidecars except Graphify's exact query stamp."""
    try:
        cache_fd = os.open(
            "cache",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=output_fd,
        )
    except OSError:
        raise OSError("managed query cache is invalid") from None
    try:
        names = tuple(sorted(os.listdir(cache_fd)))
        if names != ("last_query_stamp",):
            raise OSError("managed query cache is invalid")
        info = os.stat(
            "last_query_stamp",
            dir_fd=cache_fd,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError("managed query cache is invalid")
    finally:
        os.close(cache_fd)


def _open_directory_components(parent_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(parent_fd)
    try:
        for name in parts:
            if not _safe_component(name):
                raise OSError("unsafe component")
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
            info = os.fstat(child)
            named = os.stat(name, dir_fd=current, follow_symlinks=False)
            if (
                not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
            ):
                os.close(child)
                raise OSError("directory changed")
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _safe_component(value: object) -> bool:
    return (
        type(value) is str
        and value not in {"", ".", ".."}
        and "/" not in value
        and "\\" not in value
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _safe_root_file_name(value: object) -> bool:
    return _safe_component(value)


def _snapshot_digest(
    generation_digest: str, ownership_sha256: str, manifest_sha256: str
) -> str:
    for value in (generation_digest, ownership_sha256, manifest_sha256):
        _require_digest(value)
    return hashlib.sha256(
        REGISTRY_SNAPSHOT_DOMAIN
        + _canonical_json(
            {
                "generation_digest": generation_digest,
                "ownership_sha256": ownership_sha256,
                "manifest_sha256": manifest_sha256,
            }
        )
    ).hexdigest()


def _entry_from_admission(
    key: str,
    admission: _CapturedGeneration,
    snapshot_digest: str,
    snapshot_relative: PurePosixPath,
) -> dict[str, object]:
    validated = admission.validated
    assert validated.project_uid is not None
    assert validated.adapter_id is not None
    assert validated.projection_digest is not None
    assert validated.evidence_digest is not None
    assert validated.build_epoch is not None
    return {
        "project_uid": str(validated.project_uid),
        "project_id": admission.manifest.project_id,
        "graphify_version": admission.manifest.graphify_version,
        "adapter_id": validated.adapter_id,
        "source_digest": validated.source_digest,
        "projection_digest": validated.projection_digest,
        "graph_digest": validated.graph_digest,
        "evidence_digest": validated.evidence_digest,
        "generation_digest": validated.generation_digest,
        "build_epoch": validated.build_epoch,
        "impact_trust": validated.impact_trust,
        "impact_limitations": list(validated.impact_limitations),
        "ownership_sha256": admission.ownership_sha256,
        "manifest_sha256": admission.manifest_sha256,
        "snapshot_digest": snapshot_digest,
        "snapshot": snapshot_relative.as_posix(),
    }


def _open_or_create_atlas_root(home: Path) -> _OpenedDirectory:
    _ensure_directory(home, 0o700, exact_mode=False)
    atlas = home / "atlasweaver"
    _ensure_directory(atlas, 0o700, exact_mode=True)
    return _open_directory(atlas, exact_mode=0o700)


def _ensure_directory(path: Path, mode: int, *, exact_mode: bool) -> None:
    try:
        path.mkdir(mode=mode, parents=False, exist_ok=False)
        created = True
    except FileExistsError:
        created = False
    except FileNotFoundError:
        path.parent.mkdir(mode=mode, parents=True, exist_ok=True)
        path.mkdir(mode=mode, exist_ok=True)
        created = True
    try:
        info = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid()
            or (exact_mode and stat.S_IMODE(info.st_mode) != mode)
            or (not exact_mode and stat.S_IMODE(info.st_mode) & 0o022)
        ):
            raise OSError("unsafe directory")
        if created:
            os.chmod(path, mode, follow_symlinks=False)
            _fsync_directory(path.parent)
    except OSError:
        raise RegistryError(
            "registry_recovery_required", "registry directory is unsafe"
        ) from None


def _install_snapshot(
    atlas_root: Path,
    relative: PurePosixPath,
    admission: _CapturedGeneration,
) -> tuple[Path, bool]:
    snapshots = atlas_root / "snapshots"
    uid_root = snapshots / str(admission.manifest.project_uid)
    _ensure_directory(snapshots, 0o700, exact_mode=True)
    _ensure_directory(uid_root, 0o700, exact_mode=True)
    target = atlas_root.joinpath(*relative.parts)
    if _path_exists(target):
        _require_snapshot_bytes(target, admission.files, admission.manifest_payload)
        return target, False
    token = secrets.token_hex(16)
    pending = uid_root / f".snapshot-{token}.new"
    try:
        _write_snapshot_tree(pending, admission.files, admission.manifest_payload)
        os.replace(pending, target)
        _fsync_directory(uid_root)
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    return target, True


def _write_snapshot_tree(
    root: Path, files: Mapping[str, bytes], manifest_payload: bytes
) -> None:
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    owned = root / "owned"
    owned.mkdir(mode=0o700)
    for name in sorted(files):
        if not _safe_root_file_name(name) or type(files[name]) is not bytes:
            raise OSError("snapshot artifact invalid")
        _write_new_file(owned / name, files[name], 0o600)
    _write_new_file(root / "manifest.yaml", manifest_payload, 0o600)
    _fsync_directory(owned)
    _fsync_directory(root)


def _require_snapshot_bytes(
    root: Path, files: Mapping[str, bytes], manifest_payload: bytes
) -> None:
    captured = _read_snapshot_tree(root, MAX_REGISTRY_SNAPSHOT_BYTES)
    if captured[0] != dict(files) or captured[1] != manifest_payload:
        raise RegistryError(
            "registry_recovery_required", "snapshot digest collision detected"
        )


def _read_snapshot_tree(
    root: Path, cap: int
) -> tuple[dict[str, bytes], bytes, int]:
    opened = _open_directory(root, exact_mode=0o700)
    try:
        actual = set(os.listdir(opened.descriptor))
        if actual != {"owned", "manifest.yaml"}:
            raise OSError("snapshot tree invalid")
        manifest_payload, _ = _read_file_at(opened.descriptor, "manifest.yaml", cap)
        owned_fd = _open_directory_components(opened.descriptor, ("owned",))
        try:
            names = tuple(sorted(os.listdir(owned_fd)))
            if not names or any(not _safe_root_file_name(name) for name in names):
                raise OSError("snapshot tree invalid")
            total = len(manifest_payload)
            files: dict[str, bytes] = {}
            for name in names:
                payload, _ = _read_file_at(owned_fd, name, cap - total)
                total += len(payload)
                if total > cap:
                    raise RegistryError(
                        "registry_snapshot_too_large", "registry snapshot is too large"
                    )
                files[name] = payload
            return files, manifest_payload, total
        finally:
            os.close(owned_fd)
    finally:
        opened.close()


def _validate_snapshot_directory(
    root: Path, entry: Mapping[str, object]
) -> ValidatedGraph:
    files, manifest_payload, _ = _read_snapshot_tree(
        root, MAX_REGISTRY_SNAPSHOT_BYTES
    )
    manifest = load_manifest_payload(manifest_payload, Path("."))
    validated = validate_owned_graph(
        root / "owned",
        manifest,
        expected_source_digest=entry["source_digest"],
        expected_projection_digest=entry["projection_digest"],
    )
    ownership = files.get(OWNERSHIP_MANIFEST)
    if ownership is None:
        raise RegistryError(
            "registry_snapshot_mismatch", "snapshot ownership is missing"
        )
    if (
        _digest(ownership) != entry["ownership_sha256"]
        or _digest(manifest_payload) != entry["manifest_sha256"]
        or _snapshot_digest(
            validated.generation_digest,
            _digest(ownership),
            _digest(manifest_payload),
        )
        != entry["snapshot_digest"]
    ):
        raise RegistryError(
            "registry_snapshot_mismatch", "snapshot binding is invalid"
        )
    _require_entry_matches_validated(
        f"atlasweaver/{manifest.project_uid}", entry, validated
    )
    if entry["project_id"] != manifest.project_id:
        raise RegistryError(
            "registry_snapshot_mismatch", "snapshot project identity is invalid"
        )
    return validated


def _remove_snapshot(snapshot: Path, atlas_root: Path) -> None:
    try:
        if snapshot.is_relative_to(atlas_root / "snapshots"):
            shutil.rmtree(snapshot)
            _fsync_directory(snapshot.parent)
            for parent in (snapshot.parent, snapshot.parent.parent):
                try:
                    parent.rmdir()
                    _fsync_directory(parent.parent)
                except OSError:
                    break
    except OSError:
        pass


def _build_graphify_projection(
    repo_root: Path,
    manifest: ProjectManifest,
    repository: RepositoryAccess,
    atlas_root: Path,
    entries: Mapping[str, Mapping[str, object]],
    executable: ResolvedGraphifyExecutable,
    runner: CommandRunner,
) -> tuple[bytes, bytes]:
    with tempfile.TemporaryDirectory(
        prefix="atlasweaver-graphify-registry-"
    ) as temporary:
        private_home = Path(temporary)
        os.chmod(private_home, 0o700)
        environment = {
            "HOME": str(private_home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.defpath,
        }
        for key in sorted(entries):
            entry = entries[key]
            graph = atlas_root.joinpath(*PurePosixPath(entry["snapshot"]).parts) / "owned/graph.json"
            if not graph.is_file():
                raise RegistryError(
                    "registry_recovery_required", "registry snapshot graph is missing"
                )
            contract = resolve_graphify_compatibility(entry["graphify_version"])
            rendered = render_graphify_global_add(
                contract,
                binary=executable.path,
                graph=graph,
                registry_key=key,
            )
            if Path(rendered.argv[3]) != graph:
                raise RegistryError(
                    "registry_recovery_required", "registry projection path is invalid"
                )
            run_graphify_operation(
                runner,
                executable,
                rendered.argv,
                env=environment,
                timeout=60.0,
                operation=rendered.operation,
                before_exec=lambda: assert_current_manifest_unchanged(
                    repo_root, manifest, repository_access=repository
                ),
            )
        generated = _open_directory(private_home / ".graphify")
        try:
            graph_payload, _ = _read_file_at(
                generated.descriptor,
                "global-graph.json",
                MAX_REGISTRY_SNAPSHOT_BYTES,
            )
            manifest_payload, _ = _read_file_at(
                generated.descriptor,
                "global-manifest.json",
                16 * 1024 * 1024,
            )
        finally:
            generated.close()
        _validate_graphify_projection(
            graph_payload, manifest_payload, atlas_root, entries
        )
        return graph_payload, manifest_payload


def _validate_graphify_projection(
    graph_payload: bytes,
    manifest_payload: bytes,
    atlas_root: Path,
    entries: Mapping[str, Mapping[str, object]],
) -> None:
    graph = _strict_json(graph_payload)
    projected_manifest = _strict_json(manifest_payload)
    if (
        not isinstance(projected_manifest, Mapping)
        or set(projected_manifest) != {"version", "repos"}
        or projected_manifest["version"] != 1
        or not isinstance(projected_manifest["repos"], Mapping)
        or set(projected_manifest["repos"]) != set(entries)
    ):
        raise RegistryError(
            "registry_recovery_required", "Graphify manifest projection is invalid"
        )
    for key, entry in entries.items():
        item = projected_manifest["repos"][key]
        graph_path = atlas_root.joinpath(*PurePosixPath(entry["snapshot"]).parts) / "owned/graph.json"
        if (
            not isinstance(item, Mapping)
            or set(item)
            != {"added_at", "source_path", "node_count", "edge_count", "source_hash"}
            or type(item["source_path"]) is not str
            or Path(item["source_path"]) != graph_path.resolve()
            or item["source_hash"] != entry["graph_digest"][:16]
            or re.fullmatch(r"[0-9a-f]{16}", item["source_hash"] or "") is None
            or type(item["added_at"]) is not str
            or not item["added_at"]
            or any(
                isinstance(item[name], bool)
                or not isinstance(item[name], int)
                or item[name] < 0
                for name in ("node_count", "edge_count")
            )
        ):
            raise RegistryError(
                "registry_recovery_required", "Graphify manifest projection is invalid"
            )
    if (
        not isinstance(graph, Mapping)
        or not isinstance(graph.get("nodes"), list)
        or not isinstance(graph.get("links"), list)
    ):
        raise RegistryError(
            "registry_recovery_required", "Graphify graph projection is invalid"
        )
    keys = tuple(entries)
    node_ids: set[str] = set()
    for node in graph["nodes"]:
        if not isinstance(node, Mapping) or type(node.get("id")) is not str or not node["id"]:
            raise RegistryError(
                "registry_recovery_required", "Graphify graph projection is invalid"
            )
        identifier = node["id"]
        if identifier in node_ids:
            raise RegistryError(
                "registry_recovery_required", "Graphify graph projection is invalid"
            )
        node_ids.add(identifier)
        owned = any(identifier.startswith(f"{key}::") for key in keys)
        external = not node.get("source_file") and type(node.get("label")) is str and bool(node["label"])
        if not (owned or external):
            raise RegistryError(
                "registry_recovery_required", "Graphify graph projection is invalid"
            )
    for edge in graph["links"]:
        if (
            not isinstance(edge, Mapping)
            or type(edge.get("source")) is not str
            or type(edge.get("target")) is not str
            or edge["source"] not in node_ids
            or edge["target"] not in node_ids
        ):
            raise RegistryError(
                "registry_recovery_required", "Graphify graph projection is invalid"
            )


def _graphify_projection_exists(home: Path) -> bool:
    root = home / ".graphify"
    if not _path_exists(root):
        return False
    try:
        opened = _open_directory(root)
    except RegistryError:
        return True
    try:
        return any(
            _entry_exists(opened.descriptor, name)
            for name in ("global-graph.json", "global-manifest.json")
        )
    finally:
        opened.close()


def _commit_registry_transaction(
    home: Path,
    atlas: _OpenedDirectory,
    registry_payload: bytes,
    graph_payload: bytes,
    manifest_payload: bytes,
    snapshot_relative: PurePosixPath,
    snapshot_created: bool,
    fs: RegistryFileSystem,
) -> None:
    graphify_path = home / ".graphify"
    graphify_created = not _path_exists(graphify_path)
    _ensure_directory(graphify_path, 0o700, exact_mode=False)
    graphify = _open_directory(graphify_path)
    token = secrets.token_hex(16)
    staged = {
        "registry": f".registry-{token}.new",
        "graph": f".global-graph-{token}.new",
        "manifest": f".global-manifest-{token}.new",
    }
    backups = {
        "registry": f".registry-{token}.backup",
        "graph": f".global-graph-{token}.backup",
        "manifest": f".global-manifest-{token}.backup",
    }
    targets = {
        "registry": (atlas, REGISTRY_NAME, registry_payload),
        "graph": (graphify, "global-graph.json", graph_payload),
        "manifest": (graphify, "global-manifest.json", manifest_payload),
    }
    old_payloads: dict[str, bytes | None] = {}
    journal_written = False
    try:
        for logical, (parent, target, payload) in targets.items():
            old_payloads[logical] = (
                _read_file_at(parent.descriptor, target, MAX_REGISTRY_SNAPSHOT_BYTES)[0]
                if _entry_exists(parent.descriptor, target)
                else None
            )
            _write_new_file_at(parent.descriptor, staged[logical], payload, 0o600)
        journal = {
            "schema_version": 1,
            "transaction_id": token,
            "phase": "prepared",
            "snapshot": snapshot_relative.as_posix(),
            "snapshot_created": snapshot_created,
            "graphify_directory_created": graphify_created,
            "files": {
                logical: {
                    "target": target,
                    "staged": staged[logical],
                    "backup": backups[logical],
                    "old_sha256": (
                        None
                        if old_payloads[logical] is None
                        else _digest(old_payloads[logical])
                    ),
                    "new_sha256": _digest(payload),
                }
                for logical, (_, target, payload) in targets.items()
            },
        }
        _write_journal(atlas, journal)
        journal_written = True
        journal["phase"] = "snapshot_installed"
        _write_journal(atlas, journal, replace=True)

        fs.checkpoint("backup-graphify-graph")
        fs.checkpoint("backup-graphify-manifest")
        for logical, (parent, _, _) in targets.items():
            previous = old_payloads[logical]
            if previous is not None:
                _write_new_file_at(
                    parent.descriptor, backups[logical], previous, 0o600
                )

        fs.checkpoint("write-atlas-registry")
        _replace_at(atlas.descriptor, staged["registry"], REGISTRY_NAME)
        journal["phase"] = "atlas_registry_installed"
        _write_journal(atlas, journal, replace=True)

        fs.checkpoint("promote-graphify-graph")
        _replace_at(
            graphify.descriptor, staged["graph"], "global-graph.json"
        )
        journal["phase"] = "graphify_graph_installed"
        _write_journal(atlas, journal, replace=True)

        fs.checkpoint("promote-graphify-manifest")
        _replace_at(
            graphify.descriptor, staged["manifest"], "global-manifest.json"
        )
        journal["phase"] = "graphify_manifest_installed"
        _write_journal(atlas, journal, replace=True)

        fs.checkpoint("commit-generation")
        journal["phase"] = "committed"
        _write_journal(atlas, journal, replace=True)
        _cleanup_transaction_files(atlas, graphify, journal, remove_journal=True)
    except BaseException:
        if not journal_written:
            for logical, (parent, _, _) in targets.items():
                _unlink_at(parent.descriptor, staged[logical])
                _unlink_at(parent.descriptor, backups[logical])
            if graphify_created:
                graphify.close()
                try:
                    graphify_path.rmdir()
                    _fsync_directory(graphify_path.parent)
                except OSError:
                    pass
                raise
        raise
    finally:
        if graphify.descriptor >= 0:
            graphify.close()


def _recover_registry_transaction(home: Path, atlas: _OpenedDirectory) -> None:
    if not _entry_exists(atlas.descriptor, REGISTRY_JOURNAL):
        return
    try:
        payload, _ = _read_file_at(
            atlas.descriptor, REGISTRY_JOURNAL, 1_048_576
        )
        journal = _strict_json(payload)
        if _canonical_json(journal) != payload:
            raise ValueError("journal not canonical")
        _validate_journal(journal)
        assert isinstance(journal, dict)
        graphify_path = home / ".graphify"
        graphify = _open_directory(graphify_path)
    except (RegistryError, OSError, TypeError, ValueError):
        raise RegistryError(
            "registry_recovery_required", "registry recovery is required"
        ) from None
    try:
        parents = {"registry": atlas, "graph": graphify, "manifest": graphify}
        all_new = True
        for logical, item in journal["files"].items():
            parent = parents[logical]
            target = item["target"]
            try:
                current, _ = _read_file_at(
                    parent.descriptor, target, MAX_REGISTRY_SNAPSHOT_BYTES
                )
            except (FileNotFoundError, OSError):
                all_new = False
                continue
            if _digest(current) != item["new_sha256"]:
                all_new = False
        if all_new:
            _cleanup_transaction_files(
                atlas, graphify, journal, remove_journal=True
            )
            return

        for logical, item in journal["files"].items():
            parent = parents[logical]
            target = item["target"]
            old_digest = item["old_sha256"]
            current: bytes | None
            try:
                current, _ = _read_file_at(
                    parent.descriptor, target, MAX_REGISTRY_SNAPSHOT_BYTES
                )
            except (FileNotFoundError, OSError):
                current = None
            if old_digest is None:
                if current is not None:
                    if _digest(current) != item["new_sha256"]:
                        raise RegistryError(
                            "registry_recovery_required",
                            "registry recovery is required",
                        )
                    _unlink_at(parent.descriptor, target)
            elif current is None or _digest(current) != old_digest:
                backup, _ = _read_file_at(
                    parent.descriptor,
                    item["backup"],
                    MAX_REGISTRY_SNAPSHOT_BYTES,
                )
                if _digest(backup) != old_digest:
                    raise RegistryError(
                        "registry_recovery_required",
                        "registry recovery is required",
                    )
                temporary = f".{target}-{journal['transaction_id']}.restore"
                _write_new_file_at(parent.descriptor, temporary, backup, 0o600)
                _replace_at(parent.descriptor, temporary, target)
        if journal["snapshot_created"]:
            snapshot = atlas.path.joinpath(
                *PurePosixPath(journal["snapshot"]).parts
            )
            _remove_snapshot(snapshot, atlas.path)
        _cleanup_transaction_files(atlas, graphify, journal, remove_journal=True)
        if journal["graphify_directory_created"]:
            graphify.close()
            try:
                graphify_path.rmdir()
                _fsync_directory(graphify_path.parent)
            except OSError:
                pass
    except RegistryError:
        raise
    except (OSError, TypeError, ValueError):
        raise RegistryError(
            "registry_recovery_required", "registry recovery is required"
        ) from None
    finally:
        if graphify.descriptor >= 0:
            graphify.close()


def _validate_journal(document: object) -> None:
    phases = {
        "prepared",
        "snapshot_installed",
        "atlas_registry_installed",
        "graphify_graph_installed",
        "graphify_manifest_installed",
        "committed",
    }
    if (
        not isinstance(document, Mapping)
        or set(document)
        != {
            "schema_version",
            "transaction_id",
            "phase",
            "snapshot",
            "snapshot_created",
            "graphify_directory_created",
            "files",
        }
        or document["schema_version"] != 1
        or type(document["transaction_id"]) is not str
        or re.fullmatch(r"[0-9a-f]{32}", document["transaction_id"]) is None
        or document["phase"] not in phases
        or type(document["snapshot"]) is not str
        or type(document["snapshot_created"]) is not bool
        or type(document["graphify_directory_created"]) is not bool
        or not isinstance(document["files"], Mapping)
        or set(document["files"]) != {"registry", "graph", "manifest"}
    ):
        raise ValueError("journal invalid")
    token = document["transaction_id"]
    expected_targets = {
        "registry": REGISTRY_NAME,
        "graph": "global-graph.json",
        "manifest": "global-manifest.json",
    }
    for logical, target in expected_targets.items():
        item = document["files"][logical]
        if (
            not isinstance(item, Mapping)
            or set(item)
            != {"target", "staged", "backup", "old_sha256", "new_sha256"}
            or item["target"] != target
            or item["staged"]
            != {
                "registry": f".registry-{token}.new",
                "graph": f".global-graph-{token}.new",
                "manifest": f".global-manifest-{token}.new",
            }[logical]
            or item["backup"]
            != {
                "registry": f".registry-{token}.backup",
                "graph": f".global-graph-{token}.backup",
                "manifest": f".global-manifest-{token}.backup",
            }[logical]
            or (
                item["old_sha256"] is not None
                and _HEX_DIGEST.fullmatch(item["old_sha256"] or "") is None
            )
            or _HEX_DIGEST.fullmatch(item["new_sha256"] or "") is None
        ):
            raise ValueError("journal invalid")
    snapshot = PurePosixPath(document["snapshot"])
    if (
        snapshot.is_absolute()
        or len(snapshot.parts) != 3
        or snapshot.parts[0] != "snapshots"
        or any(not _safe_component(part) for part in snapshot.parts)
        or _HEX_DIGEST.fullmatch(snapshot.parts[2]) is None
    ):
        raise ValueError("journal invalid")


def _cleanup_transaction_files(
    atlas: _OpenedDirectory,
    graphify: _OpenedDirectory,
    journal: Mapping[str, object],
    *,
    remove_journal: bool,
) -> None:
    parents = {"registry": atlas, "graph": graphify, "manifest": graphify}
    for logical, item in journal["files"].items():
        parent = parents[logical]
        _unlink_at(parent.descriptor, item["staged"])
        _unlink_at(parent.descriptor, item["backup"])
    if remove_journal:
        _unlink_at(atlas.descriptor, REGISTRY_JOURNAL)


def _write_journal(
    atlas: _OpenedDirectory,
    document: Mapping[str, object],
    *,
    replace: bool = False,
) -> None:
    payload = _canonical_json(document)
    if not replace:
        _write_new_file_at(atlas.descriptor, REGISTRY_JOURNAL, payload, 0o600)
        return
    token = document["transaction_id"]
    temporary = f".registry-journal-{token}.new"
    _write_new_file_at(atlas.descriptor, temporary, payload, 0o600)
    _replace_at(atlas.descriptor, temporary, REGISTRY_JOURNAL)


def _write_new_file(path: Path, payload: bytes, mode: int) -> None:
    parent = _open_directory(path.parent)
    try:
        _write_new_file_at(parent.descriptor, path.name, payload, mode)
    finally:
        parent.close()


def _write_new_file_at(
    parent_fd: int, name: str, payload: bytes, mode: int
) -> None:
    if not _safe_root_file_name(name) or type(payload) is not bytes:
        raise OSError("file write invalid")
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
        dir_fd=parent_fd,
    )
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("file write made no progress")
            remaining = remaining[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(parent_fd)


def _replace_at(parent_fd: int, source: str, target: str) -> None:
    if not _safe_root_file_name(source) or not _safe_root_file_name(target):
        raise OSError("replace name invalid")
    os.replace(source, target, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    os.fsync(parent_fd)


def _unlink_at(parent_fd: int, name: str) -> None:
    if not _safe_root_file_name(name):
        raise OSError("unlink name invalid")
    try:
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileNotFoundError:
        pass


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _registry_status_locked(
    repo_root: Path,
    manifest: ProjectManifest,
    repository: RepositoryAccess,
    home: Path,
    atlas: _OpenedDirectory,
    key: str,
) -> RegistryStatus:
    if _entry_exists(atlas.descriptor, REGISTRY_JOURNAL):
        return _status_after_manifest_check(
            repo_root,
            manifest,
            repository,
            "mismatch",
            ("registry_recovery_required",),
        )
    try:
        document, registry_payload = _read_registry(atlas.descriptor)
        entry = document["entries"].get(key)
        if entry is None:
            return _status_after_manifest_check(
                repo_root, manifest, repository, "missing"
            )
        graphify_hashes = _capture_graphify_hashes(home)
        if graphify_hashes != (
            document["graphify_projection"]["global_graph_sha256"],
            document["graphify_projection"]["global_manifest_sha256"],
        ):
            return _status_after_manifest_check(
                repo_root, manifest, repository, "mismatch"
            )
        projection = inspect_projection(
            repo_root, manifest, repository_access=repository
        )
        output = _repository_output_path(repository, manifest)
        try:
            owned = validate_owned_graph(
                output,
                manifest,
                expected_source_digest=projection.source_digest,
                expected_projection_digest=projection.projection_digest,
                repository_access=repository,
            )
        except ArtifactValidationError:
            observed = validate_owned_graph(
                output,
                manifest,
                repository_access=repository,
            )
            if (
                observed.source_digest != projection.source_digest
                or observed.projection_digest != projection.projection_digest
            ):
                return _status_after_manifest_check(
                    repo_root, manifest, repository, "stale"
                )
            raise
        if (
            owned.source_digest != projection.source_digest
            or owned.projection_digest != projection.projection_digest
        ):
            return _status_after_manifest_check(
                repo_root, manifest, repository, "stale"
            )
        _require_entry_matches_validated(key, entry, owned)
        with tempfile.TemporaryDirectory(prefix="atlasweaver-registry-status-") as temporary:
            _capture_snapshot_entry(
                atlas.path,
                key,
                entry,
                Path(temporary),
                MAX_REGISTRY_SNAPSHOT_BYTES,
            )
        if hashlib.sha256(registry_payload).hexdigest() != _digest(registry_payload):
            raise RegistryError("registry_snapshot_mismatch", "registry state is invalid")
    except (ArtifactValidationError, CompatibilityError, RegistryError, StagingError, OSError, TypeError, ValueError):
        return _status_after_manifest_check(
            repo_root, manifest, repository, "mismatch"
        )
    return _status_after_manifest_check(
        repo_root, manifest, repository, "current"
    )


def _status_after_manifest_check(
    repo_root: Path,
    manifest: ProjectManifest,
    repository: RepositoryAccess,
    status: Literal["missing", "stale", "mismatch", "current"],
    issues: tuple[str, ...] = (),
) -> RegistryStatus:
    assert_current_manifest_unchanged(
        repo_root, manifest, repository_access=repository
    )
    return RegistryStatus(status, issues)


def _user_root(value: Path | None) -> Path:
    if value is not None and not isinstance(value, Path):
        raise RegistryError("registry_recovery_required", "registry root is invalid")
    try:
        return (Path.home() if value is None else value).absolute()
    except OSError:
        raise RegistryError(
            "registry_recovery_required", "registry root is unavailable"
        ) from None


def _path_exists(path: Path) -> bool:
    try:
        path.stat(follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _open_directory(path: Path, *, exact_mode: int | None = None) -> _OpenedDirectory:
    descriptor = -1
    try:
        named = path.stat(follow_symlinks=False)
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(named.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_uid != os.geteuid()
            or (exact_mode is not None and stat.S_IMODE(opened.st_mode) != exact_mode)
        ):
            raise OSError("unsafe directory")
        return _OpenedDirectory(path, descriptor)
    except (OSError, ValueError):
        if descriptor >= 0:
            os.close(descriptor)
        raise RegistryError(
            "registry_recovery_required", "registry state is unavailable"
        ) from None


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _read_file_at(parent_fd: int, name: str, cap: int) -> tuple[bytes, os.stat_result]:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        before = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            or before.st_size < 0
            or before.st_size > cap
        ):
            raise OSError("unsafe file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                raise OSError("file too large")
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or total != before.st_size
        ):
            raise OSError("file changed")
        return b"".join(chunks), before
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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
    except (TypeError, ValueError):
        raise RegistryError(
            "registry_recovery_required", "registry document is invalid"
        ) from None


def _strict_json(payload: bytes) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, value in pairs:
            if type(name) is not str or name in result:
                raise ValueError("duplicate key")
            result[name] = value
        return result

    try:
        return json.loads(
            payload,
            object_pairs_hook=object_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise RegistryError(
            "registry_recovery_required", "registry document is invalid"
        ) from None


def _read_registry(parent_fd: int) -> tuple[dict[str, object], bytes]:
    try:
        payload, _ = _read_file_at(parent_fd, REGISTRY_NAME, 16 * 1024 * 1024)
        document = _strict_json(payload)
        if _canonical_json(document) != payload:
            raise RegistryError(
                "registry_recovery_required", "registry document is not canonical"
            )
        _validate_registry_document(document)
        assert isinstance(document, dict)
        return document, payload
    except RegistryError:
        raise
    except OSError:
        raise RegistryError(
            "registry_recovery_required", "registry document is unavailable"
        ) from None


def _validate_registry_document(document: object) -> None:
    if (
        not isinstance(document, Mapping)
        or set(document) != {"schema_version", "generation", "graphify_projection", "entries"}
        or document["schema_version"] != REGISTRY_SCHEMA_VERSION
        or isinstance(document["generation"], bool)
        or not isinstance(document["generation"], int)
        or document["generation"] < 1
        or not isinstance(document["entries"], Mapping)
    ):
        raise RegistryError(
            "registry_recovery_required", "registry document is invalid"
        )
    projection = document["graphify_projection"]
    if (
        not isinstance(projection, Mapping)
        or set(projection) != {"global_graph_sha256", "global_manifest_sha256"}
    ):
        raise RegistryError(
            "registry_recovery_required", "registry document is invalid"
        )
    _require_digest(projection["global_graph_sha256"])
    _require_digest(projection["global_manifest_sha256"])
    for key, entry in document["entries"].items():
        _validate_entry(key, entry)


def _validate_entry(key: object, entry: object) -> None:
    if type(key) is not str or _REGISTRY_KEY.fullmatch(key) is None:
        raise RegistryError(
            "registry_recovery_required", "registry entry is invalid"
        )
    try:
        uid = UUID(key.removeprefix("atlasweaver/"))
    except (ValueError, AttributeError):
        raise RegistryError(
            "registry_recovery_required", "registry entry is invalid"
        ) from None
    if uid.version != 4 or str(uid) != key.removeprefix("atlasweaver/"):
        raise RegistryError(
            "registry_recovery_required", "registry entry is invalid"
        )
    if not isinstance(entry, Mapping) or set(entry) != _ENTRY_FIELDS:
        raise RegistryError(
            "registry_recovery_required", "registry entry is invalid"
        )
    for name in (
        "source_digest",
        "projection_digest",
        "graph_digest",
        "evidence_digest",
        "generation_digest",
        "ownership_sha256",
        "manifest_sha256",
        "snapshot_digest",
    ):
        _require_digest(entry[name])
    if (
        entry["project_uid"] != str(uid)
        or type(entry["project_id"]) is not str
        or not entry["project_id"]
        or type(entry["graphify_version"]) is not str
        or type(entry["adapter_id"]) is not str
        or entry["impact_trust"] not in {"trusted", "navigation"}
        or isinstance(entry["build_epoch"], bool)
        or not isinstance(entry["build_epoch"], int)
        or entry["build_epoch"] < 1
        or type(entry["impact_limitations"]) is not list
        or any(type(item) is not str or not item for item in entry["impact_limitations"])
        or entry["impact_limitations"] != sorted(set(entry["impact_limitations"]))
        or entry["snapshot"]
        != f"snapshots/{uid}/{entry['snapshot_digest']}"
    ):
        raise RegistryError(
            "registry_recovery_required", "registry entry is invalid"
        )
    try:
        contract = resolve_graphify_compatibility(entry["graphify_version"])
    except (CompatibilityError, TypeError):
        raise RegistryError(
            "registry_recovery_required", "registry entry is invalid"
        ) from None
    if entry["adapter_id"] != contract.adapter_id:
        raise RegistryError(
            "registry_recovery_required", "registry entry is invalid"
        )


def _require_digest(value: object) -> str:
    if type(value) is not str or _HEX_DIGEST.fullmatch(value) is None:
        raise RegistryError(
            "registry_recovery_required", "registry digest is invalid"
        )
    return value


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _repository_output_path(
    repository: RepositoryAccess, manifest: ProjectManifest
) -> Path:
    try:
        if hasattr(fcntl, "F_GETPATH"):
            payload = fcntl.fcntl(
                repository.descriptor, fcntl.F_GETPATH, b"\0" * 1024
            )
            root = Path(os.fsdecode(payload.split(b"\0", 1)[0]))
        else:
            root = Path(os.readlink(f"/proc/self/fd/{repository.descriptor}"))
        opened = os.fstat(repository.descriptor)
        named = root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or repository.identity != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("repository identity mismatch")
    except OSError:
        raise RegistryError(
            "registry_source_changed", "project changed during registry sync"
        ) from None
    return root.joinpath(*manifest.output_dir.parts).absolute()


def _capture_graphify_hashes(home: Path) -> tuple[str, str]:
    root = _open_directory(home / ".graphify")
    try:
        graph, _ = _read_file_at(
            root.descriptor, "global-graph.json", MAX_REGISTRY_SNAPSHOT_BYTES
        )
        manifest, _ = _read_file_at(
            root.descriptor, "global-manifest.json", 16 * 1024 * 1024
        )
        return _digest(graph), _digest(manifest)
    finally:
        root.close()


def _require_entry_matches_validated(
    key: str, entry: Mapping[str, object], validated: ValidatedGraph
) -> None:
    expected = {
        "project_uid": str(validated.project_uid),
        "adapter_id": validated.adapter_id,
        "source_digest": validated.source_digest,
        "projection_digest": validated.projection_digest,
        "graph_digest": validated.graph_digest,
        "evidence_digest": validated.evidence_digest,
        "generation_digest": validated.generation_digest,
        "build_epoch": validated.build_epoch,
        "impact_trust": validated.impact_trust,
        "impact_limitations": list(validated.impact_limitations),
    }
    if (
        validated.artifact_schema_version != 2
        or validated.project_uid is None
        or key != f"atlasweaver/{validated.project_uid}"
        or any(entry[name] != value for name, value in expected.items())
    ):
        raise RegistryError(
            "registry_snapshot_mismatch", "registry entry does not match graph"
        )


def _capture_snapshot_entry(
    atlas_root: Path,
    key: str,
    entry: Mapping[str, object],
    temporary_root: Path,
    remaining: int,
) -> tuple[RegistrySnapshotEntry, int]:
    try:
        _validate_entry(key, entry)
        relative = PurePosixPath(entry["snapshot"])
        expected_relative = PurePosixPath(
            "snapshots", entry["project_uid"], entry["snapshot_digest"]
        )
        if relative != expected_relative:
            raise ValueError("snapshot path mismatch")
        source = atlas_root.joinpath(*relative.parts)
        files, manifest_payload, total = _read_snapshot_tree(source, remaining)
        if total > remaining:
            raise RegistryError(
                "registry_snapshot_too_large", "registry snapshot is too large"
            )
        destination = temporary_root / "snapshot"
        _write_snapshot_tree(destination, files, manifest_payload)
        manifest = load_manifest_payload(manifest_payload, Path("."))
        validated = validate_owned_graph(
            destination / "owned",
            manifest,
            expected_source_digest=entry["source_digest"],
            expected_projection_digest=entry["projection_digest"],
        )
        ownership_payload = files.get(OWNERSHIP_MANIFEST)
        graph_payload = files.get("graph.json")
        evidence_payload = files.get("GRAPH_EVIDENCE.json")
        if (
            ownership_payload is None
            or graph_payload is None
            or evidence_payload is None
            or _digest(ownership_payload) != entry["ownership_sha256"]
            or _digest(manifest_payload) != entry["manifest_sha256"]
            or _digest(graph_payload) != entry["graph_digest"]
            or _digest(evidence_payload) != entry["evidence_digest"]
            or manifest.project_uid is None
            or str(manifest.project_uid) != entry["project_uid"]
            or manifest.project_id != entry["project_id"]
            or manifest.graphify_version != entry["graphify_version"]
            or _snapshot_digest(
                validated.generation_digest,
                _digest(ownership_payload),
                _digest(manifest_payload),
            )
            != entry["snapshot_digest"]
        ):
            raise ValueError("snapshot binding mismatch")
        _require_entry_matches_validated(key, entry, validated)
        return (
            RegistrySnapshotEntry(
                project_uid=manifest.project_uid,
                project_id=manifest.project_id,
                registry_key=key,
                graphify_version=manifest.graphify_version,
                adapter_id=entry["adapter_id"],
                graph_digest=entry["graph_digest"],
                evidence_digest=entry["evidence_digest"],
                generation_digest=entry["generation_digest"],
                snapshot_digest=entry["snapshot_digest"],
                source_digest=entry["source_digest"],
                projection_digest=entry["projection_digest"],
                graph_payload=graph_payload,
                evidence_payload=evidence_payload,
                impact_trust=entry["impact_trust"],
                impact_limitations=tuple(entry["impact_limitations"]),
            ),
            total,
        )
    except RegistryError as error:
        if error.code == "registry_snapshot_too_large":
            raise
        raise RegistryError(
            "registry_snapshot_mismatch", "registry snapshot is invalid"
        ) from None
    except (ArtifactValidationError, CompatibilityError, ManifestError, OSError, TypeError, ValueError):
        raise RegistryError(
            "registry_snapshot_mismatch", "registry snapshot is invalid"
        ) from None


__all__ = [
    "DOCUMENTED_REGISTRY_ERROR_CODES",
    "GRAPHIFY_PROJECTION_DOMAIN",
    "MAX_REGISTRY_SNAPSHOT_BYTES",
    "REAL_REGISTRY_FS",
    "REGISTRY_JOURNAL",
    "REGISTRY_LOCK",
    "REGISTRY_NAME",
    "REGISTRY_SCHEMA_VERSION",
    "REGISTRY_SNAPSHOT_DOMAIN",
    "RegistryError",
    "RegistryFileSystem",
    "RegistryQueryRequest",
    "RegistrySnapshot",
    "RegistrySnapshotEntry",
    "RegistryStatus",
    "RegistrySyncResult",
    "capture_registry_snapshot",
    "query_registry",
    "registry_key",
    "registry_status",
    "registry_sync",
]
