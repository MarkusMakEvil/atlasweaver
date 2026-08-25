"""Prepare, validate, and durably promote Graphify-owned Obsidian exports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import secrets
import stat
import tempfile
import unicodedata
from typing import Any

import yaml

from .models import ProjectManifest
from .privacy import effective_excludes, is_denied
from .locking import ExclusiveFileLock
from .artifacts import ArtifactValidationError, _scan_markdown_references


ATLAS_OWNERSHIP_MANIFEST = ".project-knowledge-atlas.json"
_ATLAS_SCHEMA_VERSION = 1
_TRANSACTION_SCHEMA_VERSION = 1
_RESERVED = frozenset({ATLAS_OWNERSHIP_MANIFEST, "_project.base"})
_ALLOWED_INPUT_SUFFIXES = frozenset({".md", ".canvas"})
_REQUIRED_PROPERTIES = (
    "project_id",
    "source_kind",
    "source_path",
    "graph_node_id",
    "generated_at",
    "generator_version",
    "ownership",
)
_DATAVIEWJS = re.compile(r"(?im)^[ \t]*(?:`{3,}|~{3,})[ \t]*dataviewjs\b")
_WIKI_LINK = re.compile(r"!?\[\[([^\]|#]+)")
_SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto"})
_URI_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")


class AtlasOwnershipError(ValueError):
    """Raised when atlas ownership or path confinement cannot be proven."""


@dataclass(frozen=True)
class PreparedAtlasExport:
    """A staged export annotated with wrapper-owned Obsidian metadata."""

    root: Path
    project_id: str
    generated_at: str
    generator_version: str
    files: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class ValidatedAtlasExport:
    """An immutable summary trusted only in the validating process."""

    root: Path
    project_id: str
    generated_at: str
    generator_version: str
    digest: str
    files: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class AtlasPromotionResult:
    """Result of replacing one generated namespace."""

    target: Path
    backup: Path
    digest: str
    changed: bool


@dataclass(frozen=True)
class _TreeSnapshot:
    files: tuple[tuple[PurePosixPath, str], ...]
    directories: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class _ValidationEvidence:
    candidate: ValidatedAtlasExport
    snapshot: _TreeSnapshot
    payloads: tuple[tuple[PurePosixPath, bytes], ...]


@dataclass
class _AtlasHandles:
    atlas: Path
    projects: Path
    project: Path
    parent_fd: int
    atlas_fd: int
    projects_fd: int
    project_fd: int
    state_fd: int
    staging_fd: int
    rollback_fd: int
    transactions_fd: int
    atlas_name: str
    project_id: str

    def close(self) -> None:
        for descriptor in (
            self.transactions_fd,
            self.rollback_fd,
            self.staging_fd,
            self.state_fd,
            self.project_fd,
            self.projects_fd,
            self.atlas_fd,
            self.parent_fd,
        ):
            os.close(descriptor)


_PREPARED_IN_PROCESS: dict[int, PreparedAtlasExport] = {}
_VALIDATED_IN_PROCESS: dict[int, _ValidationEvidence] = {}


class AtlasFileSystem:
    """Durable filesystem boundary with injectable named fault checkpoints."""

    def checkpoint(self, operation: str) -> None:
        del operation

    def prepare_private_dir(self, path: Path) -> bool:
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            path.mkdir(mode=0o700, exist_ok=False)
            created = True
        else:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise AtlasOwnershipError("atlas directory must not be a symlink")
            created = False
        os.chmod(path, 0o700)
        return created

    def rename_at(
        self,
        source_parent: int,
        source_name: str,
        destination_parent: int,
        destination_name: str,
    ) -> None:
        os.rename(
            source_name,
            destination_name,
            src_dir_fd=source_parent,
            dst_dir_fd=destination_parent,
        )

    def fsync_directory(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag())
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def fsync_open_directory(self, descriptor: int, display_path: Path) -> None:
        del display_path
        os.fsync(descriptor)

    def remove_tree_at(self, parent_descriptor: int, name: str) -> None:
        info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AtlasOwnershipError("transaction directory must not be a symlink")
        descriptor = _open_directory_at(parent_descriptor, name)
        try:
            if _directory_identity_from_stat(os.fstat(descriptor)) != (
                _directory_identity_from_stat(info)
            ):
                raise AtlasOwnershipError("transaction directory changed during removal")
            _remove_tree_contents(descriptor)
            current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if _directory_identity_from_stat(current) != _directory_identity_from_stat(
                os.fstat(descriptor)
            ):
                raise AtlasOwnershipError("transaction directory changed during removal")
        finally:
            os.close(descriptor)
        os.rmdir(name, dir_fd=parent_descriptor)


def _remove_tree_contents(descriptor: int) -> None:
    """Remove one opened tree without relying on Python 3.11's rmtree dir_fd."""
    with os.scandir(os.dup(descriptor)) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            child = _open_directory_at(descriptor, name)
            try:
                if _directory_identity_from_stat(os.fstat(child)) != (
                    _directory_identity_from_stat(info)
                ):
                    raise AtlasOwnershipError(
                        "transaction directory changed during removal"
                    )
                _remove_tree_contents(child)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _directory_identity_from_stat(current) != (
                    _directory_identity_from_stat(os.fstat(child))
                ):
                    raise AtlasOwnershipError(
                        "transaction directory changed during removal"
                    )
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)

REAL_ATLAS_FS = AtlasFileSystem()


def prepare_atlas_candidate(
    candidate_dir: Path,
    manifest: ProjectManifest,
    graphify_version: str,
) -> PreparedAtlasExport:
    """Annotate a staged Graphify export without touching a live vault."""
    _require_manifest_namespace(manifest)
    if graphify_version != manifest.graphify_version:
        raise AtlasOwnershipError("generator version does not match the project manifest")
    root = _require_real_directory(candidate_dir.absolute(), "atlas candidate").resolve()
    captured = _capture_tree(root)
    if not captured:
        raise AtlasOwnershipError("atlas candidate must contain generated files")
    _assert_no_collisions(relative for relative, _ in captured)
    relative_paths = {relative for relative, _ in captured}
    reserved = sorted(
        relative.as_posix()
        for relative in relative_paths
        if relative.as_posix() in _RESERVED
    )
    if reserved:
        raise AtlasOwnershipError(f"candidate contains reserved path: {reserved[0]}")
    if PurePosixPath("graph.canvas") not in relative_paths:
        raise AtlasOwnershipError("atlas candidate must contain graph.canvas")

    generated_at = datetime.now(timezone.utc).isoformat()
    rendered: dict[PurePosixPath, bytes] = {}
    markdown_count = 0
    for relative, payload in captured:
        if relative.suffix.casefold() not in _ALLOWED_INPUT_SUFFIXES:
            raise AtlasOwnershipError(
                f"unsupported generated atlas file: {relative.as_posix()}"
            )
        if relative.suffix.casefold() == ".md":
            markdown_count += 1
            rendered[relative] = _annotate_markdown(
                payload, relative, manifest, graphify_version, generated_at
            )
        else:
            _validate_canvas_payload(payload, relative_paths)
            rendered[relative] = payload
    if not markdown_count:
        raise AtlasOwnershipError("atlas candidate must contain generated Markdown")

    base_relative = PurePosixPath("_project.base")
    rendered[base_relative] = _base_payload(manifest.project_id)
    file_digests = {
        relative.as_posix(): hashlib.sha256(payload).hexdigest()
        for relative, payload in sorted(rendered.items(), key=lambda item: item[0].as_posix())
    }
    digest = _semantic_digest(rendered)
    ownership = {
        "schema_version": _ATLAS_SCHEMA_VERSION,
        "project_id": manifest.project_id,
        "generator_version": graphify_version,
        "generated_at": generated_at,
        "atlas_digest": digest,
        "files": file_digests,
    }
    rendered[PurePosixPath(ATLAS_OWNERSHIP_MANIFEST)] = _json_payload(ownership)

    root_descriptor = _open_directory(root)
    try:
        for relative, payload in sorted(
            rendered.items(), key=lambda item: item[0].as_posix()
        ):
            _atomic_write_relative(root_descriptor, relative, payload)
    finally:
        os.close(root_descriptor)
    _remove_empty_directories(root)
    expected = tuple(sorted(rendered, key=lambda path: path.as_posix()))
    if tuple(relative for relative, _ in _capture_tree(root)) != expected:
        raise AtlasOwnershipError("candidate changed during atlas preparation")
    prepared = PreparedAtlasExport(
        root=root,
        project_id=manifest.project_id,
        generated_at=generated_at,
        generator_version=graphify_version,
        files=expected,
    )
    _PREPARED_IN_PROCESS[id(prepared)] = prepared
    return prepared


def validate_atlas_candidate(
    candidate: PreparedAtlasExport,
    manifest: ProjectManifest,
) -> ValidatedAtlasExport:
    """Validate generated ownership, links, and immutable candidate bytes."""
    if _PREPARED_IN_PROCESS.get(id(candidate)) is not candidate:
        raise AtlasOwnershipError("atlas candidate was not prepared in the current process")
    _require_manifest_namespace(manifest)
    if candidate.project_id != manifest.project_id:
        raise AtlasOwnershipError("atlas candidate project ID mismatch")
    if candidate.generator_version != manifest.graphify_version:
        raise AtlasOwnershipError("atlas candidate generator version mismatch")
    root = _require_real_directory(candidate.root, "atlas candidate").resolve()
    initial_payloads, initial_directories = _capture_tree_state(root)
    snapshot = _snapshot_from_payloads(initial_payloads, initial_directories)
    ownership, digest = _validate_owned_tree(root, manifest)
    if ownership["generated_at"] != candidate.generated_at:
        raise AtlasOwnershipError("atlas candidate generated_at mismatch")
    final_payloads, final_directories = _capture_tree_state(root)
    final_snapshot = _snapshot_from_payloads(final_payloads, final_directories)
    if final_snapshot != snapshot:
        raise AtlasOwnershipError("atlas candidate changed during validation")
    files = tuple(relative for relative, _ in final_snapshot.files)
    if files != candidate.files:
        raise AtlasOwnershipError("atlas candidate changed after preparation")

    validated = ValidatedAtlasExport(
        root=root,
        project_id=manifest.project_id,
        generated_at=candidate.generated_at,
        generator_version=manifest.graphify_version,
        digest=digest,
        files=files,
    )
    _VALIDATED_IN_PROCESS[id(validated)] = _ValidationEvidence(
        validated, final_snapshot, final_payloads
    )
    return validated


def load_prepared_atlas_candidate(
    candidate_dir: Path, manifest: ProjectManifest
) -> PreparedAtlasExport:
    """Strictly reload a prepared export for a later CLI process."""
    _require_manifest_namespace(manifest)
    root = _require_real_directory(candidate_dir.absolute(), "atlas candidate").resolve()
    ownership, _ = _validate_owned_tree(root, manifest)
    files = tuple(relative for relative, _ in _snapshot(root).files)
    loaded = PreparedAtlasExport(
        root=root,
        project_id=manifest.project_id,
        generated_at=str(ownership["generated_at"]),
        generator_version=manifest.graphify_version,
        files=files,
    )
    _PREPARED_IN_PROCESS[id(loaded)] = loaded
    return loaded


def generated_root(
    atlas_root: Path,
    manifest: ProjectManifest,
    *,
    create: bool = False,
) -> Path:
    """Return the exact generated namespace, rejecting traversal and symlinks."""
    _require_manifest_namespace(manifest)
    atlas = atlas_root.absolute()
    if create:
        project_root = _ensure_namespace(atlas, manifest, REAL_ATLAS_FS)
        _reject_case_collision(project_root, "Generated")
    else:
        if atlas.exists() or atlas.is_symlink():
            _require_real_directory(atlas, "atlas root")
            _check_namespace_components(atlas, manifest, require_existing=False)
    return atlas.joinpath(*manifest.obsidian_namespace.parts)


def promote_atlas(
    candidate: ValidatedAtlasExport,
    atlas_root: Path,
    manifest: ProjectManifest,
    fs: AtlasFileSystem = REAL_ATLAS_FS,
) -> AtlasPromotionResult:
    """Transactionally replace only a proven Graphify-owned Generated tree."""
    evidence = _VALIDATED_IN_PROCESS.get(id(candidate))
    if evidence is None or evidence.candidate is not candidate:
        raise AtlasOwnershipError("atlas candidate was not validated in the current process")
    _require_manifest_namespace(manifest)
    if candidate.project_id != manifest.project_id:
        raise AtlasOwnershipError("atlas candidate project ID mismatch")
    atlas = atlas_root.absolute()
    _assert_snapshot(candidate.root, evidence.snapshot)
    if _is_within(candidate.root, atlas):
        raise AtlasOwnershipError("atlas candidate must be staged outside the atlas")
    handles = _open_atlas_handles(atlas, manifest, fs)
    transaction_id = secrets.token_hex(16)
    stage_name = transaction_id
    backup_name = transaction_id
    journal_name = f"{transaction_id}.json"
    target = handles.project / "Generated"
    backup = handles.project / ".project-knowledge-atlas/rollback" / backup_name
    journal_started = False
    backup_moved = False
    candidate_installed = False
    committed = False
    lock = ExclusiveFileLock(handles.project / ".project-knowledge-atlas/promotion.lock")
    try:
        lock.__enter__()
        _recover_all_transactions(handles, manifest, fs)
        _assert_namespace_binding(handles)
        current_snapshot: _TreeSnapshot | None = None
        current_digest: str | None = None
        if _entry_exists(handles.project_fd, "Generated"):
            try:
                target_fd = _open_directory_at(handles.project_fd, "Generated")
            except OSError as error:
                raise AtlasOwnershipError(
                    "generated namespace must be a directory, not a symlink"
                ) from error
            try:
                current_payloads, current_directories = _capture_directory(
                    target_fd, PurePosixPath()
                )
                _, current_digest = _validate_owned_capture(
                    tuple(sorted(current_payloads, key=lambda item: item[0].as_posix())),
                    current_directories,
                    manifest,
                )
                current_snapshot = _snapshot_from_payloads(
                    tuple(sorted(current_payloads, key=lambda item: item[0].as_posix())),
                    current_directories,
                )
            finally:
                os.close(target_fd)
        if current_digest == candidate.digest:
            _assert_namespace_binding(handles)
            return AtlasPromotionResult(target, backup, candidate.digest, False)

        had_target = current_snapshot is not None
        journal = _new_journal_document(
            transaction_id, candidate.digest, "initializing", had_target
        )
        _write_journal_at(handles, journal_name, journal, fs)
        journal_started = True
        fs.checkpoint("copy-stage")
        _assert_namespace_binding(handles)
        _copy_snapshot_at(
            handles,
            handles.staging_fd,
            stage_name,
            evidence.snapshot,
            evidence.payloads,
            fs,
            handles.project / ".project-knowledge-atlas/staging" / stage_name,
        )
        fs.checkpoint("fsync-stage")
        _fsync_open_tree(
            handles.staging_fd,
            stage_name,
            fs,
            handles.project / ".project-knowledge-atlas/staging" / stage_name,
        )
        _fsync_atlas_dir(
            fs,
            handles.staging_fd,
            handles.project / ".project-knowledge-atlas/staging",
        )
        journal["state"] = "prepared"
        _write_journal_at(handles, journal_name, journal, fs)

        if had_target:
            fs.checkpoint("rename-backup")
            _assert_namespace_binding(handles)
            _bound_rename(
                handles,
                fs,
                handles.project_fd,
                "Generated",
                handles.rollback_fd,
                backup_name,
            )
            backup_moved = True
            _fsync_atlas_dir(fs, handles.project_fd, handles.project)
            _fsync_atlas_dir(
                fs,
                handles.rollback_fd,
                handles.project / ".project-knowledge-atlas/rollback",
            )
            backup_snapshot = _snapshot_directory_at(
                handles.rollback_fd, backup_name
            )
            if backup_snapshot != current_snapshot:
                _assert_namespace_binding(handles)
                _bound_rename(
                    handles,
                    fs,
                    handles.rollback_fd,
                    backup_name,
                    handles.project_fd,
                    "Generated",
                )
                backup_moved = False
                _fsync_atlas_dir(fs, handles.rollback_fd, handles.project / ".project-knowledge-atlas/rollback")
                _fsync_atlas_dir(fs, handles.project_fd, handles.project)
                raise AtlasOwnershipError("generated namespace changed after validation")
            fs.checkpoint("fsync-backup")
            journal["state"] = "backed_up"
            _write_journal_at(handles, journal_name, journal, fs)

        fs.checkpoint("rename-candidate")
        _assert_namespace_binding(handles)
        if backup_moved and _snapshot_directory_at(
            handles.rollback_fd, backup_name
        ) != current_snapshot:
            raise AtlasOwnershipError("generated namespace changed before install")
        _bound_rename(
            handles,
            fs,
            handles.staging_fd,
            stage_name,
            handles.project_fd,
            "Generated",
        )
        candidate_installed = True
        _fsync_atlas_dir(
            fs,
            handles.staging_fd,
            handles.project / ".project-knowledge-atlas/staging",
        )
        _fsync_atlas_dir(fs, handles.project_fd, handles.project)
        fs.checkpoint("fsync-promote")
        _assert_namespace_binding(handles)
        installed_fd = _open_directory_at(handles.project_fd, "Generated")
        try:
            installed_payloads, installed_directories = _capture_directory(
                installed_fd, PurePosixPath()
            )
        finally:
            os.close(installed_fd)
        if _snapshot_from_payloads(
            tuple(sorted(installed_payloads, key=lambda item: item[0].as_posix())),
            installed_directories,
        ) != evidence.snapshot:
            raise AtlasOwnershipError("staged atlas changed during promotion")
        if backup_moved and _snapshot_directory_at(
            handles.rollback_fd, backup_name
        ) != current_snapshot:
            raise AtlasOwnershipError("generated namespace changed during promotion")
        journal["state"] = "promoted"
        _write_journal_at(handles, journal_name, journal, fs)
        _assert_namespace_binding(handles)
        journal["state"] = "committed"
        _write_journal_at(handles, journal_name, journal, fs)
        committed = True

        if backup_moved:
            _assert_namespace_binding(handles)
            _bound_remove_tree(handles, fs, handles.rollback_fd, backup_name)
            backup_moved = False
            _fsync_atlas_dir(
                fs,
                handles.rollback_fd,
                handles.project / ".project-knowledge-atlas/rollback",
            )
        _assert_namespace_binding(handles)
        _bound_unlink(handles, handles.transactions_fd, journal_name)
        journal_started = False
        _fsync_atlas_dir(
            fs,
            handles.transactions_fd,
            handles.project / ".project-knowledge-atlas/transactions",
        )
        _assert_namespace_binding(handles)
        return AtlasPromotionResult(target, backup, candidate.digest, True)
    except BaseException as error:
        try:
            _rollback_active_transaction(
                handles=handles,
                stage_name=stage_name,
                backup_name=backup_name,
                journal_name=journal_name,
                journal_started=journal_started,
                backup_moved=backup_moved,
                candidate_installed=candidate_installed,
                committed=committed,
                fs=fs,
            )
        except BaseException as rollback_error:
            raise AtlasOwnershipError(
                "atlas promotion failed and rollback could not be completed"
            ) from rollback_error
        raise error
    finally:
        lock.__exit__(None, None, None)
        handles.close()


def _require_manifest_namespace(manifest: ProjectManifest) -> None:
    expected = ("Projects", manifest.project_id, "Generated")
    namespace = manifest.obsidian_namespace
    if namespace.is_absolute() or namespace.parts != expected or ".." in namespace.parts:
        raise AtlasOwnershipError(
            "atlas namespace must be Projects/{project_id}/Generated"
        )


def _open_atlas_handles(
    atlas: Path, manifest: ProjectManifest, fs: AtlasFileSystem
) -> _AtlasHandles:
    parent_fd = _open_directory(atlas.parent)
    opened: list[int] = [parent_fd]
    try:
        _reject_case_collision_at(parent_fd, atlas.name)
        if not _entry_exists(parent_fd, atlas.name):
            os.mkdir(atlas.name, 0o700, dir_fd=parent_fd)
            _fsync_atlas_dir(fs, parent_fd, atlas.parent)
        atlas_fd = _open_directory_at(parent_fd, atlas.name)
        opened.append(atlas_fd)
        projects_fd = _ensure_open_child(atlas_fd, "Projects", atlas, fs)
        opened.append(projects_fd)
        project_fd = _ensure_open_child(
            projects_fd, manifest.project_id, atlas / "Projects", fs
        )
        opened.append(project_fd)
        _reject_case_collision_at(project_fd, "Generated")
        state_fd = _ensure_open_child(
            project_fd,
            ".project-knowledge-atlas",
            atlas / "Projects" / manifest.project_id,
            fs,
        )
        opened.append(state_fd)
        staging_fd = _ensure_open_child(
            state_fd,
            "staging",
            atlas / "Projects" / manifest.project_id / ".project-knowledge-atlas",
            fs,
        )
        opened.append(staging_fd)
        rollback_fd = _ensure_open_child(
            state_fd,
            "rollback",
            atlas / "Projects" / manifest.project_id / ".project-knowledge-atlas",
            fs,
        )
        opened.append(rollback_fd)
        transactions_fd = _ensure_open_child(
            state_fd,
            "transactions",
            atlas / "Projects" / manifest.project_id / ".project-knowledge-atlas",
            fs,
        )
        opened.append(transactions_fd)
        _fsync_atlas_dir(
            fs,
            state_fd,
            atlas / "Projects" / manifest.project_id / ".project-knowledge-atlas",
        )
        return _AtlasHandles(
            atlas=atlas,
            projects=atlas / "Projects",
            project=atlas / "Projects" / manifest.project_id,
            parent_fd=parent_fd,
            atlas_fd=atlas_fd,
            projects_fd=projects_fd,
            project_fd=project_fd,
            state_fd=state_fd,
            staging_fd=staging_fd,
            rollback_fd=rollback_fd,
            transactions_fd=transactions_fd,
            atlas_name=atlas.name,
            project_id=manifest.project_id,
        )
    except BaseException:
        for descriptor in reversed(opened):
            os.close(descriptor)
        raise


def _ensure_open_child(
    parent_fd: int,
    name: str,
    parent_path: Path,
    fs: AtlasFileSystem,
) -> int:
    _reject_case_collision_at(parent_fd, name)
    if not _entry_exists(parent_fd, name):
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        _fsync_atlas_dir(fs, parent_fd, parent_path)
    try:
        return _open_directory_at(parent_fd, name)
    except OSError as error:
        raise AtlasOwnershipError(f"atlas directory must not be a symlink: {name}") from error


def _reject_case_collision_at(parent_fd: int, expected: str) -> None:
    wanted = _portable_name(expected)
    with os.scandir(os.dup(parent_fd)) as entries:
        for entry in entries:
            if _portable_name(entry.name) == wanted and entry.name != expected:
                raise AtlasOwnershipError(
                    f"atlas namespace collision: {entry.name} conflicts with {expected}"
                )


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _snapshot_directory_at(parent_fd: int, name: str) -> _TreeSnapshot:
    descriptor = _open_directory_at(parent_fd, name)
    try:
        payloads, directories = _capture_directory(descriptor, PurePosixPath())
        return _snapshot_from_payloads(
            tuple(sorted(payloads, key=lambda item: item[0].as_posix())),
            directories,
        )
    finally:
        os.close(descriptor)


def _assert_namespace_binding(handles: _AtlasHandles) -> None:
    _assert_directory_binding(
        handles.parent_fd,
        handles.atlas_name,
        handles.atlas_fd,
        "atlas root changed during promotion",
    )
    _assert_directory_binding(
        handles.atlas_fd,
        "Projects",
        handles.projects_fd,
        "atlas Projects namespace changed during promotion",
    )
    _assert_directory_binding(
        handles.projects_fd,
        handles.project_id,
        handles.project_fd,
        "atlas project root changed during promotion",
    )


def _assert_directory_binding(
    parent_fd: int,
    name: str,
    opened_fd: int,
    message: str,
) -> None:
    try:
        bound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise AtlasOwnershipError(message) from error
    opened = os.fstat(opened_fd)
    if (
        stat.S_ISLNK(bound.st_mode)
        or not stat.S_ISDIR(bound.st_mode)
        or _directory_identity_from_stat(bound) != _directory_identity_from_stat(opened)
    ):
        raise AtlasOwnershipError(message)


def _fsync_atlas_dir(
    fs: AtlasFileSystem, descriptor: int, display_path: Path
) -> None:
    fs.fsync_open_directory(descriptor, display_path)


def _ensure_namespace(
    atlas: Path, manifest: ProjectManifest, fs: AtlasFileSystem
) -> Path:
    parent = atlas.parent
    _require_real_directory(parent, "atlas parent")
    if atlas.exists() or atlas.is_symlink():
        _require_real_directory(atlas, "atlas root")
    else:
        fs.prepare_private_dir(atlas)
        fs.fsync_directory(parent)
    current = atlas
    for component in ("Projects", manifest.project_id):
        _reject_case_collision(current, component)
        child = current / component
        if child.exists() or child.is_symlink():
            _require_real_directory(child, "atlas namespace")
        else:
            fs.prepare_private_dir(child)
            fs.fsync_directory(current)
        current = child
    return current


def _check_namespace_components(
    atlas: Path, manifest: ProjectManifest, *, require_existing: bool
) -> None:
    current = atlas
    for component in ("Projects", manifest.project_id, "Generated"):
        _reject_case_collision(current, component)
        current = current / component
        if current.exists() or current.is_symlink():
            _require_real_directory(current, "atlas namespace")
        elif require_existing:
            raise AtlasOwnershipError("atlas namespace is missing")
        else:
            return


def _reject_case_collision(parent: Path, expected: str) -> None:
    if not parent.exists():
        return
    wanted = _portable_name(expected)
    for entry in parent.iterdir():
        if _portable_name(entry.name) == wanted and entry.name != expected:
            raise AtlasOwnershipError(
                f"atlas namespace collision: {entry.name} conflicts with {expected}"
            )


def _annotate_markdown(
    payload: bytes,
    relative: PurePosixPath,
    manifest: ProjectManifest,
    graphify_version: str,
    generated_at: str,
) -> bytes:
    text = _decode_utf8(payload, relative)
    if _DATAVIEWJS.search(text):
        raise AtlasOwnershipError(f"DataviewJS is forbidden: {relative.as_posix()}")
    metadata, body = _split_frontmatter(text, relative)
    prior_ownership = metadata.get("ownership")
    if prior_ownership not in {None, "graphify"}:
        raise AtlasOwnershipError(f"candidate ownership conflict: {relative.as_posix()}")

    source_path = metadata.get("source_path", relative.as_posix())
    source_path = _confined_text_path(source_path, "source_path")
    if is_denied(PurePosixPath(source_path), effective_excludes(manifest)):
        raise AtlasOwnershipError(f"denied source_path: {source_path}")
    source_kind = metadata.get("source_kind", "generated-note")
    graph_node_id = metadata.get("graph_node_id", relative.with_suffix("").as_posix())
    if not isinstance(source_kind, str) or not source_kind.strip():
        raise AtlasOwnershipError("source_kind must be a non-empty string")
    if not isinstance(graph_node_id, str) or not graph_node_id.strip():
        raise AtlasOwnershipError("graph_node_id must be a non-empty string")

    reserved = {
        "project_id": manifest.project_id,
        "source_kind": source_kind,
        "source_path": source_path,
        "graph_node_id": graph_node_id,
        "generated_at": generated_at,
        "generator_version": graphify_version,
        "ownership": "graphify",
    }
    metadata.update(reserved)
    frontmatter = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
    )
    return f"---\n{frontmatter}---\n{body}".encode("utf-8")


def _split_frontmatter(
    text: str, relative: PurePosixPath
) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    closing = text.find("\n---\n", 4)
    if closing < 0:
        raise AtlasOwnershipError(f"malformed frontmatter: {relative.as_posix()}")
    raw = text[4:closing]
    try:
        loaded = yaml.load(raw, Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, AtlasOwnershipError) as error:
        raise AtlasOwnershipError(f"malformed frontmatter: {relative.as_posix()}") from error
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict) or any(not isinstance(key, str) for key in loaded):
        raise AtlasOwnershipError(f"frontmatter must be a string-keyed mapping: {relative}")
    return dict(loaded), text[closing + 5 :]


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise AtlasOwnershipError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _base_payload(project_id: str) -> bytes:
    document = {
        "filters": {
            "and": [
                f'project_id == "{project_id}"',
                'ownership == "graphify"',
            ]
        },
        "views": [
            {
                "type": "table",
                "name": "Generated",
                "order": [
                    "file.name",
                    "source_kind",
                    "source_path",
                    "generated_at",
                ],
            }
        ],
    }
    return yaml.safe_dump(document, sort_keys=False).encode("utf-8")


def _validate_owned_tree(
    root: Path, manifest: ProjectManifest
) -> tuple[dict[str, Any], str]:
    _require_real_directory(root, "generated atlas tree")
    descriptor = _open_directory(root)
    try:
        captured_list, actual_directories = _capture_directory(
            descriptor, PurePosixPath()
        )
    finally:
        os.close(descriptor)
    captured = tuple(sorted(captured_list, key=lambda item: item[0].as_posix()))
    return _validate_owned_capture(captured, actual_directories, manifest)


def _validate_owned_capture(
    captured: tuple[tuple[PurePosixPath, bytes], ...],
    actual_directories: set[PurePosixPath],
    manifest: ProjectManifest,
) -> tuple[dict[str, Any], str]:
    actual = {relative: payload for relative, payload in captured}
    ownership_path = PurePosixPath(ATLAS_OWNERSHIP_MANIFEST)
    if ownership_path not in actual:
        unknown = next(iter(sorted(actual, key=lambda path: path.as_posix())), None)
        suffix = f": {unknown.as_posix()}" if unknown else ""
        raise AtlasOwnershipError(f"missing generated ownership manifest{suffix}")
    ownership = _load_ownership(actual[ownership_path], manifest)
    recorded = ownership["files"]
    assert isinstance(recorded, dict)
    expected_paths = {PurePosixPath(path) for path in recorded}
    for relative in expected_paths:
        if relative == PurePosixPath("_project.base"):
            continue
        if relative.suffix.casefold() not in _ALLOWED_INPUT_SUFFIXES:
            raise AtlasOwnershipError(
                f"unsupported generated atlas file: {relative.as_posix()}"
            )
    actual_paths = set(actual) - {ownership_path}
    unknown = sorted(actual_paths - expected_paths, key=lambda path: path.as_posix())
    if unknown:
        raise AtlasOwnershipError(f"unknown generated file: {unknown[0].as_posix()}")
    missing = sorted(expected_paths - actual_paths, key=lambda path: path.as_posix())
    if missing:
        raise AtlasOwnershipError(f"owned generated file is missing: {missing[0].as_posix()}")
    expected_directories = {
        parent
        for relative in expected_paths
        for parent in relative.parents
        if parent != PurePosixPath(".")
    }
    unknown_directories = sorted(
        actual_directories - expected_directories, key=lambda path: path.as_posix()
    )
    if unknown_directories:
        raise AtlasOwnershipError(
            f"unknown generated directory: {unknown_directories[0].as_posix()}"
        )
    markdown_paths = {path for path in expected_paths if path.suffix.casefold() == ".md"}
    for relative in sorted(markdown_paths, key=lambda path: path.as_posix()):
        metadata, text = _split_frontmatter(_decode_utf8(actual[relative], relative), relative)
        _validate_note_properties(metadata, relative, ownership, manifest)
        if _DATAVIEWJS.search(text):
            raise AtlasOwnershipError(f"DataviewJS is forbidden: {relative.as_posix()}")
        _validate_internal_links(text, relative, markdown_paths)
    _validate_base(actual, manifest)
    canvas = PurePosixPath("graph.canvas")
    if canvas not in actual:
        raise AtlasOwnershipError("generated graph.canvas is missing")
    _validate_canvas_payload(actual[canvas], expected_paths)
    for relative in sorted(expected_paths, key=lambda path: path.as_posix()):
        expected_digest = recorded[relative.as_posix()]
        actual_digest = hashlib.sha256(actual[relative]).hexdigest()
        if actual_digest != expected_digest:
            raise AtlasOwnershipError(f"generated file digest changed: {relative.as_posix()}")
    semantic_payloads = {
        relative: actual[relative]
        for relative in expected_paths
    }
    digest = _semantic_digest(semantic_payloads)
    if digest != ownership["atlas_digest"]:
        raise AtlasOwnershipError("generated ownership digest mismatch")
    return ownership, digest


def _load_ownership(payload: bytes, manifest: ProjectManifest) -> dict[str, Any]:
    try:
        document = _load_strict_json(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AtlasOwnershipError("generated ownership manifest is malformed") from error
    if not isinstance(document, dict):
        raise AtlasOwnershipError("generated ownership manifest is malformed")
    expected_keys = {
        "schema_version",
        "project_id",
        "generator_version",
        "generated_at",
        "atlas_digest",
        "files",
    }
    if set(document) != expected_keys:
        raise AtlasOwnershipError("generated ownership manifest has an invalid schema")
    if type(document["schema_version"]) is not int or document["schema_version"] != _ATLAS_SCHEMA_VERSION:
        raise AtlasOwnershipError("generated ownership manifest has an invalid schema")
    if document["project_id"] != manifest.project_id:
        raise AtlasOwnershipError("generated ownership project ID mismatch")
    if document["generator_version"] != manifest.graphify_version:
        raise AtlasOwnershipError("generated ownership generator version mismatch")
    if not isinstance(document["generated_at"], str) or not document["generated_at"]:
        raise AtlasOwnershipError("generated ownership timestamp is invalid")
    if not isinstance(document["atlas_digest"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", document["atlas_digest"]
    ):
        raise AtlasOwnershipError("generated ownership digest is invalid")
    files = document["files"]
    if not isinstance(files, dict) or not files:
        raise AtlasOwnershipError("generated ownership files are invalid")
    normalized: set[str] = set()
    for raw, digest in files.items():
        if not isinstance(raw, str):
            raise AtlasOwnershipError("generated ownership file path is invalid")
        path = _confined_posix_path(raw, "generated ownership file path")
        if path.as_posix() in _RESERVED or path == PurePosixPath(ATLAS_OWNERSHIP_MANIFEST):
            if path != PurePosixPath("_project.base"):
                raise AtlasOwnershipError("generated ownership contains a reserved path")
        portable = _portable_path(path)
        if portable in normalized:
            raise AtlasOwnershipError("generated ownership contains a path collision")
        normalized.add(portable)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AtlasOwnershipError("generated ownership file digest is invalid")
    return document


def _validate_note_properties(
    metadata: Mapping[str, Any],
    relative: PurePosixPath,
    ownership: Mapping[str, Any],
    manifest: ProjectManifest,
) -> None:
    for field in _REQUIRED_PROPERTIES:
        if field not in metadata:
            raise AtlasOwnershipError(
                f"generated Markdown missing {field}: {relative.as_posix()}"
            )
    if metadata["ownership"] != "graphify":
        raise AtlasOwnershipError(f"generated Markdown ownership is invalid: {relative}")
    if metadata["project_id"] != manifest.project_id:
        raise AtlasOwnershipError("generated Markdown project ID mismatch")
    if metadata["generator_version"] != manifest.graphify_version:
        raise AtlasOwnershipError("generated Markdown generator version mismatch")
    if metadata["generated_at"] != ownership["generated_at"]:
        raise AtlasOwnershipError("generated Markdown timestamp mismatch")
    source_path = _confined_text_path(metadata["source_path"], "source_path")
    if is_denied(PurePosixPath(source_path), effective_excludes(manifest)):
        raise AtlasOwnershipError(f"denied source_path: {source_path}")
    for field in ("source_kind", "graph_node_id"):
        if not isinstance(metadata[field], str) or not metadata[field].strip():
            raise AtlasOwnershipError(f"generated Markdown {field} is invalid")


def _validate_internal_links(
    text: str,
    note: PurePosixPath,
    markdown_paths: set[PurePosixPath],
) -> None:
    by_stem: dict[str, list[PurePosixPath]] = {}
    for path in markdown_paths:
        by_stem.setdefault(path.stem.casefold(), []).append(path)
    for match in _WIKI_LINK.finditer(text):
        raw = match.group(1).strip()
        if not raw:
            continue
        if "/" not in raw:
            matches = by_stem.get(PurePosixPath(raw).stem.casefold(), [])
            if len(matches) == 1:
                continue
        candidate = PurePosixPath(raw if raw.casefold().endswith(".md") else f"{raw}.md")
        if candidate not in markdown_paths:
            raise AtlasOwnershipError(f"broken internal link in {note.as_posix()}")
    try:
        destinations = _scan_markdown_references(text)
    except ArtifactValidationError as error:
        raise AtlasOwnershipError(
            f"malformed internal link in {note.as_posix()}"
        ) from error
    for raw, _, _ in destinations:
        raw = raw.strip().strip("<>")
        if not raw or raw.startswith("#"):
            continue
        scheme = _URI_SCHEME.match(raw)
        if scheme:
            name = scheme.group(1).casefold()
            if name not in _SAFE_LINK_SCHEMES:
                raise AtlasOwnershipError(f"unsafe internal link scheme: {name}")
            continue
        raw = raw.partition("#")[0].partition("?")[0]
        if not raw:
            continue
        normalized = posixpath.normpath((note.parent / PurePosixPath(raw)).as_posix())
        target = _confined_posix_path(normalized, "internal link")
        if target not in markdown_paths:
            raise AtlasOwnershipError(f"broken internal link in {note.as_posix()}")


def _validate_base(files: Mapping[PurePosixPath, bytes], manifest: ProjectManifest) -> None:
    relative = PurePosixPath("_project.base")
    if relative not in files:
        raise AtlasOwnershipError("generated _project.base is missing")
    text = _decode_utf8(files[relative], relative)
    if "dataview" in text.casefold():
        raise AtlasOwnershipError("_project.base must use core Obsidian Bases")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise AtlasOwnershipError("_project.base is malformed") from error
    filters = document.get("filters") if isinstance(document, dict) else None
    expected = {
        "and": [
            f'project_id == "{manifest.project_id}"',
            'ownership == "graphify"',
        ]
    }
    if filters != expected:
        raise AtlasOwnershipError("_project.base does not restrict generated ownership")


def _validate_canvas_payload(
    payload: bytes, available_paths: set[PurePosixPath]
) -> None:
    try:
        document = _load_strict_json(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AtlasOwnershipError("graph.canvas is malformed") from error
    if not isinstance(document, dict) or not isinstance(document.get("nodes"), list):
        raise AtlasOwnershipError("graph.canvas is malformed")
    if not isinstance(document.get("edges"), list):
        raise AtlasOwnershipError("graph.canvas is malformed")
    identifiers: set[str] = set()
    for node in document["nodes"]:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            raise AtlasOwnershipError("graph.canvas node is malformed")
        if node["id"] in identifiers:
            raise AtlasOwnershipError("graph.canvas node ID is duplicated")
        identifiers.add(node["id"])
        if node.get("type") == "file":
            path = _confined_posix_path(node.get("file"), "canvas file reference")
            if path not in available_paths:
                raise AtlasOwnershipError("graph.canvas has a broken canvas file reference")
    for edge in document["edges"]:
        if not isinstance(edge, dict):
            raise AtlasOwnershipError("graph.canvas edge is malformed")
        if edge.get("fromNode") not in identifiers or edge.get("toNode") not in identifiers:
            raise AtlasOwnershipError("graph.canvas edge references a missing node")


def _capture_tree(root: Path) -> tuple[tuple[PurePosixPath, bytes], ...]:
    files, _ = _capture_tree_state(root)
    return files


def _capture_tree_state(
    root: Path,
) -> tuple[tuple[tuple[PurePosixPath, bytes], ...], tuple[PurePosixPath, ...]]:
    descriptor = _open_directory(root)
    try:
        files, directories = _capture_directory(descriptor, PurePosixPath())
    finally:
        os.close(descriptor)
    sorted_files = tuple(sorted(files, key=lambda item: item[0].as_posix()))
    sorted_directories = tuple(sorted(directories, key=lambda path: path.as_posix()))
    _assert_no_collisions(
        [relative for relative, _ in sorted_files] + list(sorted_directories)
    )
    return sorted_files, sorted_directories


def _directory_paths(root: Path) -> set[PurePosixPath]:
    descriptor = _open_directory(root)
    try:
        _, directories = _capture_directory(descriptor, PurePosixPath())
        return directories
    finally:
        os.close(descriptor)


def _capture_directory(
    descriptor: int, parent: PurePosixPath
) -> tuple[list[tuple[PurePosixPath, bytes]], set[PurePosixPath]]:
    files: list[tuple[PurePosixPath, bytes]] = []
    directories: set[PurePosixPath] = set()
    with os.scandir(os.dup(descriptor)) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        relative = parent / name
        try:
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError as error:
            raise AtlasOwnershipError("generated atlas changed during traversal") from error
        if stat.S_ISLNK(before.st_mode):
            raise AtlasOwnershipError(f"generated atlas symlink is forbidden: {relative}")
        if stat.S_ISDIR(before.st_mode):
            child = _open_directory_at(descriptor, name)
            try:
                if _directory_identity_from_stat(os.fstat(child)) != _directory_identity_from_stat(
                    before
                ):
                    raise AtlasOwnershipError("generated atlas changed during traversal")
                directories.add(relative)
                child_files, child_directories = _capture_directory(child, relative)
                files.extend(child_files)
                directories.update(child_directories)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(before.st_mode):
            raise AtlasOwnershipError(f"generated atlas file is not regular: {relative}")
        files.append((relative, _read_regular_at(descriptor, name, before, relative)))
    with os.scandir(os.dup(descriptor)) as entries:
        final_names = sorted(entry.name for entry in entries)
    if final_names != names:
        raise AtlasOwnershipError("generated atlas changed during traversal")
    return files, directories


def _remove_empty_directories(root: Path) -> None:
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def _snapshot(root: Path) -> _TreeSnapshot:
    payloads, directories = _capture_tree_state(root)
    return _snapshot_from_payloads(payloads, directories)


def _snapshot_from_payloads(
    payloads: tuple[tuple[PurePosixPath, bytes], ...],
    directories: Any,
) -> _TreeSnapshot:
    normalized_directories = tuple(
        sorted(set(directories), key=lambda path: path.as_posix())
    )
    files = tuple(
        (relative, hashlib.sha256(payload).hexdigest()) for relative, payload in payloads
    )
    _assert_no_collisions(
        [relative for relative, _ in files] + list(normalized_directories)
    )
    return _TreeSnapshot(files=files, directories=normalized_directories)


def _assert_snapshot(root: Path, expected: _TreeSnapshot) -> None:
    try:
        actual = _snapshot(root)
    except (OSError, AtlasOwnershipError) as error:
        raise AtlasOwnershipError("atlas candidate changed after validation") from error
    if actual != expected:
        raise AtlasOwnershipError("atlas candidate changed after validation")


def _copy_snapshot_at(
    handles: _AtlasHandles,
    parent_fd: int,
    name: str,
    expected: _TreeSnapshot,
    payloads: tuple[tuple[PurePosixPath, bytes], ...],
    fs: AtlasFileSystem,
    display_path: Path,
) -> None:
    _assert_namespace_binding(handles)
    if _entry_exists(parent_fd, name):
        raise AtlasOwnershipError("atlas staging destination already exists")
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    destination_fd = _open_directory_at(parent_fd, name)
    try:
        expected_map = dict(expected.files)
        for relative, payload in payloads:
            if hashlib.sha256(payload).hexdigest() != expected_map.get(relative):
                raise AtlasOwnershipError("atlas candidate changed after validation")
            current_fd = os.dup(destination_fd)
            try:
                for component in relative.parts[:-1]:
                    if not _entry_exists(current_fd, component):
                        _assert_namespace_binding(handles)
                        os.mkdir(component, 0o700, dir_fd=current_fd)
                    child_fd = _open_directory_at(current_fd, component)
                    os.close(current_fd)
                    current_fd = child_fd
                _assert_namespace_binding(handles)
                file_fd = os.open(
                    relative.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag(),
                    0o600,
                    dir_fd=current_fd,
                )
                try:
                    _write_all(file_fd, payload)
                    os.fsync(file_fd)
                finally:
                    os.close(file_fd)
            finally:
                os.close(current_fd)
        copied, copied_directories = _capture_directory(
            destination_fd, PurePosixPath()
        )
        if _snapshot_from_payloads(
            tuple(sorted(copied, key=lambda item: item[0].as_posix())),
            copied_directories,
        ) != expected:
            raise AtlasOwnershipError("atlas staged copy is incomplete")
    except BaseException:
        os.close(destination_fd)
        fs.remove_tree_at(parent_fd, name)
        raise
    else:
        os.close(destination_fd)
    _fsync_atlas_dir(fs, parent_fd, display_path.parent)
    _assert_namespace_binding(handles)


def _fsync_open_tree(
    parent_fd: int,
    name: str,
    fs: AtlasFileSystem,
    display_path: Path,
) -> None:
    root_fd = _open_directory_at(parent_fd, name)
    try:
        _fsync_tree_descriptor(root_fd, fs, display_path)
    finally:
        os.close(root_fd)


def _fsync_tree_descriptor(
    descriptor: int, fs: AtlasFileSystem, display_path: Path
) -> None:
    with os.scandir(os.dup(descriptor)) as entries:
        names = sorted(entry.name for entry in entries)
    child_directories: list[tuple[int, Path]] = []
    try:
        for name in names:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise AtlasOwnershipError("transaction tree contains a symlink")
            if stat.S_ISDIR(info.st_mode):
                child = _open_directory_at(descriptor, name)
                child_directories.append((child, display_path / name))
                _fsync_tree_descriptor(child, fs, display_path / name)
            elif stat.S_ISREG(info.st_mode):
                file_fd = os.open(
                    name, os.O_RDONLY | _nofollow_flag(), dir_fd=descriptor
                )
                try:
                    os.fsync(file_fd)
                finally:
                    os.close(file_fd)
            else:
                raise AtlasOwnershipError("transaction tree contains a special file")
        _fsync_atlas_dir(fs, descriptor, display_path)
    finally:
        for child, _ in child_directories:
            os.close(child)


def _new_journal_document(
    transaction_id: str, atlas_digest: str, state: str, had_target: bool
) -> dict[str, Any]:
    return {
        "schema_version": _TRANSACTION_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "state": state,
        "atlas_digest": atlas_digest,
        "had_target": had_target,
    }


def _write_journal_at(
    handles: _AtlasHandles,
    journal_name: str,
    document: Mapping[str, Any],
    fs: AtlasFileSystem,
) -> None:
    _assert_namespace_binding(handles)
    if _entry_exists(handles.transactions_fd, journal_name):
        info = os.stat(
            journal_name,
            dir_fd=handles.transactions_fd,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise AtlasOwnershipError("atlas transaction journal must not be a symlink")
    _atomic_write_relative(
        handles.transactions_fd, PurePosixPath(journal_name), _json_payload(document)
    )
    _fsync_atlas_dir(
        fs,
        handles.transactions_fd,
        handles.project / ".project-knowledge-atlas/transactions",
    )


def _unlink_at(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _bound_rename(
    handles: _AtlasHandles,
    fs: AtlasFileSystem,
    source_parent: int,
    source_name: str,
    destination_parent: int,
    destination_name: str,
) -> None:
    _assert_namespace_binding(handles)
    fs.rename_at(
        source_parent,
        source_name,
        destination_parent,
        destination_name,
    )


def _bound_remove_tree(
    handles: _AtlasHandles,
    fs: AtlasFileSystem,
    parent_fd: int,
    name: str,
) -> None:
    _assert_namespace_binding(handles)
    fs.remove_tree_at(parent_fd, name)


def _bound_unlink(handles: _AtlasHandles, parent_fd: int, name: str) -> None:
    _assert_namespace_binding(handles)
    _unlink_at(parent_fd, name)


def _recover_all_transactions(
    handles: _AtlasHandles,
    manifest: ProjectManifest,
    fs: AtlasFileSystem,
) -> None:
    with os.scandir(os.dup(handles.transactions_fd)) as entries:
        names = sorted(entry.name for entry in entries)
    for journal_name in names:
        info = os.stat(
            journal_name,
            dir_fd=handles.transactions_fd,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise AtlasOwnershipError("atlas transaction journal must not be a symlink")
        if not journal_name.endswith(".json"):
            raise AtlasOwnershipError("atlas transaction state contains an unknown file")
        payload = _read_regular_at(
            handles.transactions_fd,
            journal_name,
            info,
            PurePosixPath(journal_name),
        )
        document = _parse_journal(payload, journal_name)
        transaction_id = str(document["transaction_id"])
        state = str(document["state"])
        atlas_digest = str(document["atlas_digest"])
        stage_exists = _entry_exists(handles.staging_fd, transaction_id)
        backup_exists = _entry_exists(handles.rollback_fd, transaction_id)
        had_target_value = document.get("had_target")
        had_target = (
            bool(had_target_value)
            if isinstance(had_target_value, bool)
            else backup_exists or state == "backed_up"
        )
        target_exists = _entry_exists(handles.project_fd, "Generated")
        target_digest = (
            _owned_digest_at(handles.project_fd, "Generated", manifest)
            if target_exists
            else None
        )
        _assert_namespace_binding(handles)

        if state == "committed":
            if target_digest != atlas_digest:
                raise AtlasOwnershipError("committed atlas transaction is inconsistent")
            if backup_exists:
                _bound_remove_tree(
                    handles, fs, handles.rollback_fd, transaction_id
                )
            if stage_exists:
                _bound_remove_tree(
                    handles, fs, handles.staging_fd, transaction_id
                )
        elif state == "initializing":
            if backup_exists:
                raise AtlasOwnershipError("atlas transaction state is inconsistent")
            if stage_exists:
                _bound_remove_tree(
                    handles, fs, handles.staging_fd, transaction_id
                )
        elif state == "prepared" and not backup_exists:
            if not had_target and target_exists:
                if target_digest != atlas_digest:
                    raise AtlasOwnershipError("atlas transaction state is inconsistent")
                _bound_remove_tree(handles, fs, handles.project_fd, "Generated")
            if had_target and not target_exists:
                raise AtlasOwnershipError("atlas transaction state is inconsistent")
            if stage_exists:
                _bound_remove_tree(
                    handles, fs, handles.staging_fd, transaction_id
                )
        else:
            if had_target:
                if not backup_exists:
                    if (
                        not target_exists
                        or target_digest is None
                        or target_digest == atlas_digest
                    ):
                        raise AtlasOwnershipError("atlas transaction state is inconsistent")
                    # A prior recovery restored the previous valid target but
                    # failed while making that rename durable. Retrying the
                    # parent fsync and journal cleanup is safe and idempotent.
                else:
                    if target_exists:
                        if target_digest != atlas_digest:
                            raise AtlasOwnershipError("atlas transaction state is inconsistent")
                        _bound_remove_tree(
                            handles, fs, handles.project_fd, "Generated"
                        )
                    _bound_rename(
                        handles,
                        fs,
                        handles.rollback_fd,
                        transaction_id,
                        handles.project_fd,
                        "Generated",
                    )
                    backup_exists = False
            else:
                if backup_exists:
                    raise AtlasOwnershipError("atlas transaction state is inconsistent")
                if target_exists:
                    if target_digest != atlas_digest:
                        raise AtlasOwnershipError("atlas transaction state is inconsistent")
                    _bound_remove_tree(
                        handles, fs, handles.project_fd, "Generated"
                    )
            if stage_exists:
                _bound_remove_tree(
                    handles, fs, handles.staging_fd, transaction_id
                )

        fs.checkpoint("fsync-recovery")
        _fsync_atlas_dir(fs, handles.project_fd, handles.project)
        _fsync_atlas_dir(
            fs,
            handles.staging_fd,
            handles.project / ".project-knowledge-atlas/staging",
        )
        _fsync_atlas_dir(
            fs,
            handles.rollback_fd,
            handles.project / ".project-knowledge-atlas/rollback",
        )
        _bound_unlink(handles, handles.transactions_fd, journal_name)
        _fsync_atlas_dir(
            fs,
            handles.transactions_fd,
            handles.project / ".project-knowledge-atlas/transactions",
        )


def _parse_journal(payload: bytes, journal_name: str) -> dict[str, Any]:
    try:
        document = _load_strict_json(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AtlasOwnershipError("atlas transaction journal is malformed") from error
    if not isinstance(document, dict):
        raise AtlasOwnershipError("atlas transaction journal is malformed")
    schema = document.get("schema_version")
    if type(schema) is not int or schema != _TRANSACTION_SCHEMA_VERSION:
        raise AtlasOwnershipError("atlas transaction journal schema is invalid")
    legacy_keys = {"schema_version", "state", "atlas_digest"}
    current_keys = legacy_keys | {"transaction_id", "had_target"}
    if set(document) == legacy_keys:
        transaction_id = journal_name.removesuffix(".json")
        if not re.fullmatch(r"[0-9a-f]{64}", transaction_id):
            raise AtlasOwnershipError("atlas transaction journal name is invalid")
        document = dict(document)
        document["transaction_id"] = transaction_id
    elif set(document) == current_keys:
        transaction_id = document.get("transaction_id")
        if (
            not isinstance(transaction_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", transaction_id)
            or journal_name != f"{transaction_id}.json"
            or not isinstance(document.get("had_target"), bool)
        ):
            raise AtlasOwnershipError("atlas transaction journal identity is invalid")
    else:
        raise AtlasOwnershipError("atlas transaction journal schema is invalid")
    state = document.get("state")
    if state not in {"initializing", "prepared", "backed_up", "promoted", "committed"}:
        raise AtlasOwnershipError("atlas transaction journal state is invalid")
    digest = document.get("atlas_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AtlasOwnershipError("atlas transaction journal digest is invalid")
    return document


def _owned_digest_at(
    project_fd: int,
    target_name: str,
    manifest: ProjectManifest,
) -> str | None:
    try:
        target_fd = _open_directory_at(project_fd, target_name)
    except OSError:
        return None
    try:
        payloads, directories = _capture_directory(target_fd, PurePosixPath())
        _, digest = _validate_owned_capture(
            tuple(sorted(payloads, key=lambda item: item[0].as_posix())),
            directories,
            manifest,
        )
        return digest
    except AtlasOwnershipError:
        return None
    finally:
        os.close(target_fd)


def _rollback_active_transaction(
    *,
    handles: _AtlasHandles,
    stage_name: str,
    backup_name: str,
    journal_name: str,
    journal_started: bool,
    backup_moved: bool,
    candidate_installed: bool,
    committed: bool,
    fs: AtlasFileSystem,
) -> None:
    if committed:
        return
    if candidate_installed and _entry_exists(handles.project_fd, "Generated"):
        fs.remove_tree_at(handles.project_fd, "Generated")
    if backup_moved and _entry_exists(handles.rollback_fd, backup_name):
        fs.rename_at(
            handles.rollback_fd,
            backup_name,
            handles.project_fd,
            "Generated",
        )
    if _entry_exists(handles.staging_fd, stage_name):
        fs.remove_tree_at(handles.staging_fd, stage_name)
    if journal_started:
        _unlink_at(handles.transactions_fd, journal_name)
    _fsync_atlas_dir(fs, handles.project_fd, handles.project)
    _fsync_atlas_dir(
        fs,
        handles.staging_fd,
        handles.project / ".project-knowledge-atlas/staging",
    )
    _fsync_atlas_dir(
        fs,
        handles.rollback_fd,
        handles.project / ".project-knowledge-atlas/rollback",
    )
    _fsync_atlas_dir(
        fs,
        handles.transactions_fd,
        handles.project / ".project-knowledge-atlas/transactions",
    )


def _require_real_directory(path: Path, description: str) -> Path:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise AtlasOwnershipError(f"{description} must be a directory") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise AtlasOwnershipError(f"{description} must be a directory, not a symlink")
    return path


def _read_regular_bytes(path: Path) -> bytes:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise AtlasOwnershipError(f"generated file is missing: {path.name}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise AtlasOwnershipError(f"generated file must be regular: {path.name}")
    descriptor = os.open(path, os.O_RDONLY | _nofollow_flag())
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise AtlasOwnershipError("generated file changed while reading")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_regular_at(
    parent_descriptor: int,
    name: str,
    before: os.stat_result,
    relative: PurePosixPath,
) -> bytes:
    descriptor = os.open(
        name, os.O_RDONLY | _nofollow_flag(), dir_fd=parent_descriptor
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(
            before
        ):
            raise AtlasOwnershipError(
                f"generated atlas changed while reading: {relative.as_posix()}"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _file_identity(after) != _file_identity(opened):
            raise AtlasOwnershipError(
                f"generated atlas changed while reading: {relative.as_posix()}"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _atomic_write_relative(
    root_descriptor: int, relative: PurePosixPath, payload: bytes
) -> None:
    parent = _open_parent_directory(root_descriptor, relative)
    temporary_name = f".{relative.name}.{secrets.token_hex(12)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag(),
            0o600,
            dir_fd=parent,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            relative.name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        os.fsync(parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def _open_directory(path: Path) -> int:
    descriptor = os.open(path, _directory_flags())
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise AtlasOwnershipError("atlas path must remain a directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_at(parent_descriptor: int, name: str) -> int:
    return os.open(name, _directory_flags(), dir_fd=parent_descriptor)


def _open_parent_directory(root_descriptor: int, relative: PurePosixPath) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for component in relative.parts[:-1]:
            child = _open_directory_at(descriptor, component)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    except AttributeError as error:
        raise AtlasOwnershipError(
            "platform cannot safely open atlas directories"
        ) from error


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _directory_identity_from_stat(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _directory_identity(path: Path) -> tuple[int, int]:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise AtlasOwnershipError("atlas directory changed during transaction")
    return _directory_identity_from_stat(info)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("filesystem write made no progress")
        remaining = remaining[written:]


def _json_payload(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_strict_json(payload: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AtlasOwnershipError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise AtlasOwnershipError(f"invalid JSON constant: {value}")

    def parse_finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise AtlasOwnershipError("invalid JSON constant: non-finite number")
        return parsed

    return json.loads(
        payload,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
        parse_float=parse_finite_float,
    )


def _content_digest(files: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for relative, file_digest in sorted(files.items()):
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(file_digest.encode("ascii") + b"\0")
    return digest.hexdigest()


def _semantic_digest(files: Mapping[PurePosixPath, bytes]) -> str:
    normalized_digests: dict[str, str] = {}
    for relative, payload in files.items():
        normalized = payload
        if relative.suffix.casefold() == ".md":
            text = _decode_utf8(payload, relative)
            metadata, body = _split_frontmatter(text, relative)
            metadata.pop("generated_at", None)
            normalized = (
                "---\n"
                + yaml.safe_dump(
                    metadata,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=True,
                )
                + "---\n"
                + body
            ).encode("utf-8")
        normalized_digests[relative.as_posix()] = hashlib.sha256(normalized).hexdigest()
    return _content_digest(normalized_digests)


def _confined_text_path(value: Any, field: str) -> str:
    return _confined_posix_path(value, field).as_posix()


def _confined_posix_path(value: Any, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise AtlasOwnershipError(f"{field} must be a confined relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts or value == ".":
        raise AtlasOwnershipError(f"{field} must be a confined relative path")
    return path


def _assert_no_collisions(paths: Any) -> None:
    seen: dict[str, PurePosixPath] = {}
    for path in paths:
        portable = _portable_path(path)
        if portable in seen and seen[portable] != path:
            raise AtlasOwnershipError(
                f"generated atlas path collision: {seen[portable]} and {path}"
            )
        seen[portable] = path


def _portable_path(path: PurePosixPath) -> str:
    return "/".join(_portable_name(component) for component in path.parts)


def _portable_name(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _decode_utf8(payload: bytes, relative: PurePosixPath) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AtlasOwnershipError(
            f"generated atlas file must be UTF-8: {relative.as_posix()}"
        ) from error


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _nofollow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as error:
        raise AtlasOwnershipError("platform cannot reject atlas symlinks") from error
