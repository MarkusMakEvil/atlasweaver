"""Strict, descriptor-read loader for tracked project manifests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import json
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
from typing import Any, Literal
from uuid import UUID, RFC_4122, uuid4

import yaml

from .compatibility import CompatibilityError, resolve_graphify_compatibility
from .locking import (
    LifecycleDescriptors,
    RepositoryAccess,
    RepositoryIdentity,
    _ManifestBinding,
    capture_lifecycle_descriptors,
    capture_lifecycle_repository,
    open_repository_access,
    repository_lifecycle_lock,
)
from .models import ArtifactIntent, FeatureIntent, ProjectManifest


ManifestFailureKind = Literal["invalid", "changed"]


class ManifestError(ValueError):
    """Raised when a project manifest violates its closed wire contract."""

    def __init__(
        self, message: str, *, kind: ManifestFailureKind = "invalid"
    ) -> None:
        if kind not in {"invalid", "changed"}:
            raise ValueError("manifest error kind is invalid")
        super().__init__(message)
        self.kind: ManifestFailureKind = kind


MAX_MANIFEST_BYTES = 262_144
V1_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "display_name",
        "include_roots",
        "output_dir",
        "obsidian_namespace",
        "excludes",
        "track_html",
        "graphify_version",
    }
)
V2_FIELDS = frozenset({*V1_FIELDS, "project_uid", "features", "artifacts"})
FEATURE_FIELDS = frozenset({"atlas", "registry"})
GITHUB_FIELDS = frozenset(
    {
        "provider",
        "host",
        "repository",
        "repository_id",
        "channel",
        "source_ref",
        "signer_workflow",
        "signer_digest",
    }
)
_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}\Z")
_REPOSITORY_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_CHANNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")
_HEX_32 = re.compile(r"[0-9a-f]{32}\Z")

_DISCOVERABLE_ROOTS = (
    "app",
    "cmd",
    "docs",
    "lib",
    "packages",
    "scripts",
    "services",
    "src",
    "tests",
)
_DEFAULT_GRAPHIFYIGNORE = b".env*\n.git/\n.project-knowledge/\ngraphify-out/\n"
_INIT_JOURNAL = "init-transaction.json"
_INIT_SCHEMA = 1
_PREVIEW_UUID = UUID("00000000-0000-4000-8000-000000000000")


@dataclass(frozen=True)
class InitPreview:
    status: Literal[
        "preview", "ambiguous_roots", "already_configured", "already_current"
    ]
    manifest_payload: bytes
    ignore_payload: bytes
    candidate_roots: tuple[PurePosixPath, ...]
    project_uid: UUID | None
    project_id: str
    requested_include_roots: tuple[PurePosixPath, ...]
    repository_identity: RepositoryIdentity


class _ConfigurationConflict(RuntimeError):
    pass


class _StrictLoader(yaml.SafeLoader):
    def compose_node(
        self, parent: yaml.Node | None, index: int | None
    ) -> yaml.Node:
        if self.check_event(yaml.AliasEvent):
            raise ManifestError("YAML aliases are forbidden")
        return super().compose_node(parent, index)

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        if any(key.tag == "tag:yaml.org,2002:merge" for key, _ in node.value):
            raise ManifestError("YAML merge keys are forbidden")
        super().flatten_mapping(node)


def _construct_unique_mapping(
    loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise ManifestError("YAML merge keys are forbidden")
        key = loader.construct_object(key_node, deep=deep)
        if type(key) is not str:
            raise ManifestError("unknown or non-string YAML mapping key")
        if key in result:
            raise ManifestError("duplicate YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_one_yaml(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes or len(payload) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest exceeds its byte cap")
    try:
        value = yaml.load(payload.decode("utf-8"), Loader=_StrictLoader)
    except ManifestError:
        raise
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ManifestError(
            "manifest must be one valid UTF-8 YAML document"
        ) from error
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ManifestError("manifest must be a string-keyed mapping")
    return value


def load_manifest_payload(payload: bytes, repo_root: Path) -> ProjectManifest:
    """Parse already-owned manifest bytes without reopening a path."""
    del repo_root
    values = _load_one_yaml(payload)
    schema_version = values.get("schema_version")
    if type(schema_version) is not int:
        raise ManifestError("schema_version must be an integer")
    if schema_version == 1:
        return _parse_v1(values)
    if schema_version == 2:
        return _parse_v2(values)
    raise ManifestError("schema_version must be 1 or 2")


def load_manifest(
    path: Path,
    repo_root: Path,
    *,
    repository_access: RepositoryAccess | None = None,
) -> ProjectManifest:
    """Descriptor-read the canonical manifest and parse its exact bytes."""
    if path.name != ".graphify-project.yaml":
        raise ManifestError("manifest filename must be .graphify-project.yaml")
    if repository_access is not None:
        payload, binding = _capture_manifest_binding(repository_access.descriptor)
        try:
            parsed = load_manifest_payload(payload, repo_root)
        except BaseException:
            binding.close()
            raise
        repository_access._replace_manifest_binding(binding)
        return parsed
    try:
        root = repo_root.absolute()
        if path.absolute() != root / ".graphify-project.yaml":
            raise ManifestError("manifest must be located within repo_root")
        root_fd = os.open(
            root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except ManifestError:
        raise
    except OSError as error:
        raise ManifestError("unable to read manifest") from error

    descriptor = -1
    try:
        try:
            descriptor = os.open(
                ".graphify-project.yaml",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
            before = os.fstat(descriptor)
            named = os.stat(
                ".graphify-project.yaml", dir_fd=root_fd, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise ManifestError("manifest must be a stable regular file")
            if before.st_size > MAX_MANIFEST_BYTES:
                raise ManifestError("manifest exceeds its byte cap")
            payload = _read_capped(descriptor, MAX_MANIFEST_BYTES)
            after = os.fstat(descriptor)
            if _file_identity(before) != _file_identity(after):
                raise ManifestError("manifest changed while reading")
        except ManifestError:
            raise
        except OSError as error:
            raise ManifestError("unable to read manifest") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(root_fd)
    return load_manifest_payload(payload, root)


def require_current_manifest(
    repo_root: Path,
    supplied: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> ProjectManifest:
    """Pin the current manifest and require the caller's contract to match it."""
    current = load_manifest(
        repo_root / ".graphify-project.yaml",
        repo_root,
        repository_access=repository_access,
    )
    if current != supplied:
        raise ManifestError("project manifest changed", kind="changed")
    return current


def assert_current_manifest_unchanged(
    repo_root: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> None:
    """Recheck the retained manifest fd and its descriptor-relative entry."""
    del repo_root
    binding = repository_access._manifest_binding
    if binding is None or binding.descriptor < 0:
        raise ManifestError("project manifest changed", kind="changed")
    candidate = -1
    try:
        retained = os.fstat(binding.descriptor)
        if _binding_metadata(retained) != (
            binding.identity,
            binding.size,
            binding.mtime_ns,
            binding.ctime_ns,
        ):
            raise ManifestError("project manifest changed", kind="changed")
        retained_payload = _pread_capped(binding.descriptor, MAX_MANIFEST_BYTES)
        if hashlib.sha256(retained_payload).hexdigest() != binding.sha256:
            raise ManifestError("project manifest changed", kind="changed")
        candidate = os.open(
            ".graphify-project.yaml",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=repository_access.descriptor,
        )
        info = os.fstat(candidate)
        if _binding_metadata(info) != (
            binding.identity,
            binding.size,
            binding.mtime_ns,
            binding.ctime_ns,
        ):
            raise ManifestError("project manifest changed", kind="changed")
        payload = _pread_capped(candidate, MAX_MANIFEST_BYTES)
        if (
            hashlib.sha256(payload).hexdigest() != binding.sha256
            or load_manifest_payload(payload, Path(".")) != manifest
        ):
            raise ManifestError("project manifest changed", kind="changed")
    except ManifestError as error:
        if error.kind == "changed":
            raise
        raise ManifestError("project manifest changed", kind="changed") from None
    except (OSError, ValueError):
        raise ManifestError("project manifest changed", kind="changed") from None
    finally:
        if candidate >= 0:
            os.close(candidate)


def _capture_manifest_binding(root_descriptor: int) -> tuple[bytes, _ManifestBinding]:
    descriptor = -1
    try:
        descriptor = os.open(
            ".graphify-project.yaml",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_descriptor,
        )
        before = os.fstat(descriptor)
        named = os.stat(
            ".graphify-project.yaml",
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            or before.st_size > MAX_MANIFEST_BYTES
        ):
            raise ManifestError("project manifest changed", kind="changed")
        first = _pread_capped(descriptor, MAX_MANIFEST_BYTES)
        middle = os.fstat(descriptor)
        second = _pread_capped(descriptor, MAX_MANIFEST_BYTES)
        after = os.fstat(descriptor)
        rebound = os.stat(
            ".graphify-project.yaml",
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            _file_identity(before) != _file_identity(middle)
            or _file_identity(before) != _file_identity(after)
            or (before.st_dev, before.st_ino) != (rebound.st_dev, rebound.st_ino)
            or first != second
        ):
            raise ManifestError("project manifest changed", kind="changed")
        digest = hashlib.sha256(first).hexdigest()
        return first, _ManifestBinding(
            descriptor=descriptor,
            identity=(before.st_dev, before.st_ino),
            size=before.st_size,
            mtime_ns=before.st_mtime_ns,
            ctime_ns=before.st_ctime_ns,
            sha256=digest,
        )
    except ManifestError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise ManifestError("project manifest changed", kind="changed") from None


def preview_init(
    repo_root: Path,
    project_id: str,
    *,
    project_uid: UUID | None = None,
    include_roots: tuple[PurePosixPath, ...] = (),
) -> InitPreview:
    """Return a deterministic, byte-owned initialization preview."""
    with open_repository_access(repo_root) as repository:
        return _preview_init_with_access(
            repository,
            project_id,
            project_uid=project_uid,
            include_roots=include_roots,
            check_journal=True,
        )


def apply_init(
    repo_root: Path,
    preview: InitPreview,
    *,
    uuid_factory: Any = uuid4,
) -> Literal[
    "initialized", "already_configured", "init_conflict", "init_recovery_required"
]:
    """Rebind and apply one no-clobber two-file initialization transaction."""
    _require_preview(preview)
    if preview.status == "ambiguous_roots":
        raise ManifestError("ambiguous_roots requires explicit include roots")
    with repository_lifecycle_lock(
        repo_root,
        expected_repository_identity=preview.repository_identity,
    ):
        with capture_lifecycle_descriptors(repo_root) as descriptors:
            if not _recover_configuration_transaction(descriptors):
                return "init_recovery_required"
        with capture_lifecycle_repository(repo_root) as repository:
            current = _preview_init_with_access(
                repository,
                preview.project_id,
                project_uid=preview.project_uid,
                include_roots=preview.requested_include_roots,
                check_journal=False,
            )
        if current != preview:
            return "init_conflict"
        if current.status == "already_configured":
            return "already_configured"
        if current.status != "preview":
            return "init_conflict"
        uid = preview.project_uid
        if uid is None:
            uid = _require_uuid4_object(uuid_factory())
        else:
            uid = _require_uuid4_object(uid)
        payload = _materialize_preview_manifest(preview.manifest_payload, uid)
        with capture_lifecycle_descriptors(repo_root) as descriptors:
            try:
                _apply_configuration_pair(
                    descriptors,
                    manifest_payload=payload,
                    ignore_payload=preview.ignore_payload,
                )
            except _ConfigurationConflict:
                return "init_conflict"
        return "initialized"


def preview_manifest_migration(
    repo_root: Path,
    *,
    project_uid: UUID | None = None,
) -> InitPreview:
    """Return a deterministic schema-v1 to schema-v2 migration preview."""
    with open_repository_access(repo_root) as repository:
        return _preview_manifest_migration_with_access(
            repository, repo_root, project_uid=project_uid, check_journal=True
        )


def apply_manifest_migration(
    repo_root: Path,
    preview: InitPreview,
    *,
    uuid_factory: Any = uuid4,
) -> Literal[
    "migrated", "already_current", "init_conflict", "init_recovery_required"
]:
    """Rebind and apply an explicit one-file schema-v1 migration."""
    _require_preview(preview)
    with repository_lifecycle_lock(
        repo_root,
        expected_repository_identity=preview.repository_identity,
    ):
        with capture_lifecycle_descriptors(repo_root) as descriptors:
            if not _recover_configuration_transaction(descriptors):
                return "init_recovery_required"
        with capture_lifecycle_repository(repo_root) as repository:
            current = _preview_manifest_migration_with_access(
                repository,
                repo_root,
                project_uid=preview.project_uid,
                check_journal=False,
            )
            if current != preview:
                return "init_conflict"
            if current.status == "already_current":
                return "already_current"
            if current.status != "preview":
                return "init_conflict"
            old_payload, old_info = _read_regular_entry_at(
                repository.descriptor, ".graphify-project.yaml", MAX_MANIFEST_BYTES
            )
        uid = preview.project_uid
        if uid is None:
            uid = _require_uuid4_object(uuid_factory())
        else:
            uid = _require_uuid4_object(uid)
        payload = _materialize_preview_manifest(preview.manifest_payload, uid)
        with capture_lifecycle_descriptors(repo_root) as descriptors:
            try:
                _replace_v1_manifest(
                    descriptors,
                    expected_old_inode=old_info.st_ino,
                    expected_old_sha256=hashlib.sha256(old_payload).hexdigest(),
                    new_payload=payload,
                )
            except _ConfigurationConflict:
                return "init_conflict"
        return "migrated"


def inspect_init_journal(
    repo_root: Path,
    *,
    repository_access: RepositoryAccess | None = None,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> Literal["none", "recoverable", "corrupt"]:
    """Inspect initialization recovery state without creating or mutating it."""
    if repository_access is not None and expected_repository_identity is not None:
        raise ManifestError("repository authority is ambiguous")
    if repository_access is not None:
        return _inspect_init_journal_with_access(repository_access)
    with open_repository_access(
        repo_root,
        expected_repository_identity=expected_repository_identity,
    ) as repository:
        return _inspect_init_journal_with_access(repository)


def _preview_init_with_access(
    repository: RepositoryAccess,
    project_id: str,
    *,
    project_uid: UUID | None,
    include_roots: tuple[PurePosixPath, ...],
    check_journal: bool,
) -> InitPreview:
    if check_journal and _inspect_init_journal_with_access(repository) != "none":
        raise ManifestError("init_recovery_required")
    uid = None if project_uid is None else _require_uuid4_object(project_uid)
    requested = _validate_requested_roots(repository.descriptor, include_roots)
    candidates = _discover_candidate_roots(repository.descriptor)
    configured = _entry_exists_at(repository.descriptor, ".graphify-project.yaml") or _entry_exists_at(
        repository.descriptor, ".graphifyignore"
    )
    if configured:
        return InitPreview(
            "already_configured",
            b"",
            _DEFAULT_GRAPHIFYIGNORE,
            candidates,
            uid,
            _validate_project_id(project_id),
            requested,
            repository.identity,
        )
    if not requested and len(candidates) != 1:
        return InitPreview(
            "ambiguous_roots",
            b"",
            _DEFAULT_GRAPHIFYIGNORE,
            candidates,
            uid,
            _validate_project_id(project_id),
            requested,
            repository.identity,
        )
    selected = requested or candidates
    manifest = _new_v2_manifest(project_id, uid or _PREVIEW_UUID, selected)
    payload = render_manifest_v2(manifest)
    if uid is None:
        payload = payload.replace(str(_PREVIEW_UUID).encode(), b"<generated-on-apply>")
    return InitPreview(
        "preview",
        payload,
        _DEFAULT_GRAPHIFYIGNORE,
        candidates,
        uid,
        manifest.project_id,
        requested,
        repository.identity,
    )


def _preview_manifest_migration_with_access(
    repository: RepositoryAccess,
    repo_root: Path,
    *,
    project_uid: UUID | None,
    check_journal: bool,
) -> InitPreview:
    if check_journal and _inspect_init_journal_with_access(repository) != "none":
        raise ManifestError("init_recovery_required")
    requested_uid = None if project_uid is None else _require_uuid4_object(project_uid)
    current = load_manifest(
        repo_root / ".graphify-project.yaml",
        repo_root,
        repository_access=repository,
    )
    if current.schema_version == 2:
        return InitPreview(
            "already_current",
            render_manifest_v2(current),
            b"",
            current.include_roots,
            current.project_uid,
            current.project_id,
            current.include_roots,
            repository.identity,
        )
    uid = requested_uid or _PREVIEW_UUID
    migrated = replace(current, schema_version=2, project_uid=uid)
    payload = render_manifest_v2(migrated)
    if requested_uid is None:
        payload = payload.replace(str(_PREVIEW_UUID).encode(), b"<generated-on-apply>")
    return InitPreview(
        "preview",
        payload,
        b"",
        current.include_roots,
        requested_uid,
        current.project_id,
        current.include_roots,
        repository.identity,
    )


def _new_v2_manifest(
    project_id: str,
    project_uid: UUID,
    include_roots: tuple[PurePosixPath, ...],
) -> ProjectManifest:
    validated_id = _validate_project_id(project_id)
    return ProjectManifest(
        schema_version=2,
        project_id=validated_id,
        project_uid=_require_uuid4_object(project_uid),
        display_name=validated_id,
        include_roots=include_roots,
        output_dir=PurePosixPath("graphify-out"),
        obsidian_namespace=PurePosixPath(
            f"Projects/{validated_id}/Generated"
        ),
        excludes=(),
        track_html=False,
        graphify_version="0.9.48",
        features=FeatureIntent(),
        artifacts=ArtifactIntent(),
    )


def _validate_project_id(value: object) -> str:
    if type(value) is not str or not _PROJECT_ID.fullmatch(value):
        raise ManifestError("project_id must match [a-z0-9][a-z0-9-]{1,62}")
    return value


def _require_uuid4_object(value: object) -> UUID:
    if type(value) is not UUID or value.version != 4 or value.variant != RFC_4122:
        raise ManifestError("project_uid must be a UUIDv4")
    return value


def _require_preview(value: object) -> InitPreview:
    if type(value) is not InitPreview:
        raise ManifestError("initialization preview is invalid")
    if (
        value.status
        not in {"preview", "ambiguous_roots", "already_configured", "already_current"}
        or type(value.manifest_payload) is not bytes
        or type(value.ignore_payload) is not bytes
        or type(value.candidate_roots) is not tuple
        or type(value.requested_include_roots) is not tuple
        or type(value.repository_identity) is not tuple
        or len(value.repository_identity) != 2
        or any(type(item) is not int for item in value.repository_identity)
    ):
        raise ManifestError("initialization preview is invalid")
    return value


def _materialize_preview_manifest(payload: bytes, uid: UUID) -> bytes:
    marker = b"<generated-on-apply>"
    if marker in payload:
        if payload.count(marker) != 1:
            raise ManifestError("initialization preview is invalid")
        payload = payload.replace(marker, str(uid).encode("ascii"))
    parsed = load_manifest_payload(payload, Path("."))
    if parsed.schema_version != 2 or parsed.project_uid != uid:
        raise ManifestError("initialization preview is invalid")
    return payload


def _discover_candidate_roots(
    root_descriptor: int,
) -> tuple[PurePosixPath, ...]:
    roots: list[PurePosixPath] = []
    for name in _DISCOVERABLE_ROOTS:
        try:
            info = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            raise ManifestError("repository roots are unavailable") from None
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            roots.append(PurePosixPath(name))
    return tuple(roots)


def _validate_requested_roots(
    root_descriptor: int, values: object
) -> tuple[PurePosixPath, ...]:
    if type(values) is not tuple:
        raise ManifestError("include roots must be a tuple")
    roots: list[PurePosixPath] = []
    for value in values:
        if type(value) is not PurePosixPath:
            raise ManifestError("include roots must be POSIX paths")
        path = confined_relative(value.as_posix(), "include_root")
        info = _stat_confined_at(root_descriptor, path)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ManifestError("include root must be a real directory")
        roots.append(path)
    if len(set(roots)) != len(roots):
        raise ManifestError("include roots must not contain duplicates")
    return tuple(roots)


def _stat_confined_at(root_descriptor: int, path: PurePosixPath) -> os.stat_result:
    descriptor = os.dup(root_descriptor)
    try:
        for part in path.parts[:-1]:
            following = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = following
        return os.stat(path.parts[-1], dir_fd=descriptor, follow_symlinks=False)
    except OSError:
        raise ManifestError("include root is unavailable") from None
    finally:
        os.close(descriptor)


def _entry_exists_at(parent_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        raise ManifestError("repository configuration is unavailable") from None
    return True


def _inspect_init_journal_with_access(
    repository: RepositoryAccess,
) -> Literal["none", "recoverable", "corrupt"]:
    state_descriptor = -1
    try:
        try:
            named = os.stat(
                ".project-knowledge",
                dir_fd=repository.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return "none"
        if (
            not stat.S_ISDIR(named.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or named.st_uid != os.geteuid()
            or stat.S_IMODE(named.st_mode) != 0o700
        ):
            return "corrupt"
        state_descriptor = os.open(
            ".project-knowledge",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=repository.descriptor,
        )
        opened = os.fstat(state_descriptor)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            return "corrupt"
        try:
            payload, info = _read_regular_entry_at(
                state_descriptor, _INIT_JOURNAL, MAX_MANIFEST_BYTES
            )
        except FileNotFoundError:
            return "none"
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            return "corrupt"
        _parse_configuration_journal(payload)
        return "recoverable"
    except (ManifestError, OSError, ValueError, TypeError):
        return "corrupt"
    finally:
        if state_descriptor >= 0:
            os.close(state_descriptor)


def _parse_configuration_journal(payload: bytes) -> tuple[str, dict[str, object]]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        document: dict[str, object] = {}
        for key, value in pairs:
            if type(key) is not str or key in document:
                raise ManifestError("configuration journal is malformed")
            document[key] = value
        return document

    try:
        document = json.loads(payload, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, ManifestError) as error:
        raise ManifestError("configuration journal is malformed") from error
    if type(document) is not dict:
        raise ManifestError("configuration journal is malformed")
    if set(document) == {"schema_version", "phase", "files"}:
        _validate_init_journal(document)
        return "init", document
    migration_fields = {
        "destination",
        "old_inode",
        "old_sha256",
        "backup",
        "new_temporary",
        "new_sha256",
        "phase",
    }
    if set(document) == migration_fields:
        _validate_migration_journal(document)
        return "migration", document
    raise ManifestError("configuration journal is malformed")


def _validate_init_journal(document: Mapping[str, object]) -> None:
    if document["schema_version"] != _INIT_SCHEMA or type(document["schema_version"]) is not int:
        raise ManifestError("configuration journal is malformed")
    if document["phase"] not in {"prepared", "committed"}:
        raise ManifestError("configuration journal is malformed")
    files = document["files"]
    if type(files) is not list or len(files) != 2:
        raise ManifestError("configuration journal is malformed")
    destinations = (".graphify-project.yaml", ".graphifyignore")
    transaction_id: str | None = None
    for index, entry in enumerate(files):
        if type(entry) is not dict or set(entry) != {
            "destination",
            "temporary",
            "sha256",
        }:
            raise ManifestError("configuration journal is malformed")
        destination = entry["destination"]
        temporary = entry["temporary"]
        digest = entry["sha256"]
        suffix = "manifest" if index == 0 else "ignore"
        if (
            destination != destinations[index]
            or type(temporary) is not str
            or type(digest) is not str
            or not re.fullmatch(rf"init-([0-9a-f]{{32}})\.{suffix}", temporary)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ManifestError("configuration journal is malformed")
        current_id = temporary.removeprefix("init-").split(".", 1)[0]
        transaction_id = transaction_id or current_id
        if transaction_id != current_id:
            raise ManifestError("configuration journal is malformed")


def _validate_migration_journal(document: Mapping[str, object]) -> None:
    if (
        document["destination"] != ".graphify-project.yaml"
        or type(document["old_inode"]) is not int
        or document["old_inode"] <= 0
        or type(document["old_sha256"]) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", document["old_sha256"])
        or type(document["new_sha256"]) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", document["new_sha256"])
        or type(document["backup"]) is not str
        or type(document["new_temporary"]) is not str
        or document["phase"]
        not in {"prepared", "backed_up", "replaced", "committed"}
    ):
        raise ManifestError("configuration journal is malformed")
    backup = str(document["backup"])
    temporary = str(document["new_temporary"])
    backup_match = re.fullmatch(r"migrate-([0-9a-f]{32})\.backup", backup)
    temporary_match = re.fullmatch(r"migrate-([0-9a-f]{32})\.new", temporary)
    if (
        backup_match is None
        or temporary_match is None
        or backup_match.group(1) != temporary_match.group(1)
    ):
        raise ManifestError("configuration journal is malformed")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _write_all_at(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("configuration write made no progress")
        remaining = remaining[written:]


def _write_new_at(
    parent_descriptor: int, name: str, payload: bytes, mode: int
) -> os.stat_result:
    _require_transaction_basename(name)
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode,
            dir_fd=parent_descriptor,
        )
        created = True
        os.fchmod(descriptor, mode)
        _write_all_at(descriptor, payload)
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != mode
        ):
            raise OSError("configuration temporary is unsafe")
        os.fsync(parent_descriptor)
        return info
    except BaseException:
        if created:
            try:
                os.unlink(name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _replace_journal_at(state_descriptor: int, document: Mapping[str, object]) -> None:
    temporary = f"journal-{secrets.token_hex(16)}.tmp"
    _write_new_at(state_descriptor, temporary, _canonical_json(document), 0o600)
    try:
        os.rename(
            temporary,
            _INIT_JOURNAL,
            src_dir_fd=state_descriptor,
            dst_dir_fd=state_descriptor,
        )
        os.fsync(state_descriptor)
    finally:
        try:
            os.unlink(temporary, dir_fd=state_descriptor)
        except FileNotFoundError:
            pass


def _read_regular_entry_at(
    parent_descriptor: int, name: str, limit: int
) -> tuple[bytes, os.stat_result]:
    _require_transaction_basename(name)
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_descriptor,
    )
    try:
        before = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise ManifestError("configuration entry is unsafe")
        payload = _pread_capped(descriptor, limit)
        after = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(after):
            raise ManifestError("configuration entry changed")
        return payload, before
    finally:
        os.close(descriptor)


def _require_transaction_basename(name: object) -> str:
    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or _contains_control(name)
    ):
        raise ManifestError("configuration transaction name is invalid")
    return name


def _unlink_regular_at(parent_descriptor: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ManifestError("configuration transaction entry is unsafe")
    os.unlink(name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)


def _digest_entry_at(parent_descriptor: int, name: str) -> tuple[str, os.stat_result]:
    payload, info = _read_regular_entry_at(
        parent_descriptor, name, MAX_MANIFEST_BYTES
    )
    return hashlib.sha256(payload).hexdigest(), info


def _apply_configuration_pair(
    descriptors: LifecycleDescriptors,
    *,
    manifest_payload: bytes,
    ignore_payload: bytes,
) -> None:
    state_descriptor = descriptors.state_descriptor
    transaction_id = secrets.token_hex(16)
    manifest_temp = f"init-{transaction_id}.manifest"
    ignore_temp = f"init-{transaction_id}.ignore"
    temporaries = (manifest_temp, ignore_temp)
    journal_written = False
    document: dict[str, object] = {
        "schema_version": _INIT_SCHEMA,
        "phase": "prepared",
        "files": [
            {
                "destination": ".graphify-project.yaml",
                "temporary": manifest_temp,
                "sha256": hashlib.sha256(manifest_payload).hexdigest(),
            },
            {
                "destination": ".graphifyignore",
                "temporary": ignore_temp,
                "sha256": hashlib.sha256(ignore_payload).hexdigest(),
            },
        ],
    }
    try:
        _write_new_at(state_descriptor, manifest_temp, manifest_payload, 0o644)
        _write_new_at(state_descriptor, ignore_temp, ignore_payload, 0o644)
        _write_new_at(
            state_descriptor, _INIT_JOURNAL, _canonical_json(document), 0o600
        )
        journal_written = True
        _install_without_replacement(descriptors, document)
        committed = dict(document)
        committed["phase"] = "committed"
        _replace_journal_at(state_descriptor, committed)
        _remove_init_temporaries(state_descriptor, committed)
        _unlink_regular_at(state_descriptor, _INIT_JOURNAL)
    except FileExistsError as error:
        if journal_written:
            _rollback_init_document(descriptors, document)
        else:
            for name in temporaries:
                _unlink_regular_at(state_descriptor, name)
        raise _ConfigurationConflict("configuration destination appeared") from error
    except BaseException as error:
        if journal_written:
            _rollback_init_document(descriptors, document)
        else:
            for name in temporaries:
                _unlink_regular_at(state_descriptor, name)
        if isinstance(error, ManifestError) and str(error) == "configuration transaction failed":
            raise
        raise ManifestError("configuration transaction failed") from error


def _install_without_replacement(
    descriptors: LifecycleDescriptors, document: Mapping[str, object]
) -> None:
    files = document["files"]
    if type(files) is not list:
        raise ManifestError("configuration transaction failed")
    for entry in files:
        if type(entry) is not dict:
            raise ManifestError("configuration transaction failed")
        destination = str(entry["destination"])
        temporary = str(entry["temporary"])
        expected_digest = str(entry["sha256"])
        os.link(
            temporary,
            destination,
            src_dir_fd=descriptors.state_descriptor,
            dst_dir_fd=descriptors.repository_descriptor,
            follow_symlinks=False,
        )
        temp_digest, temp_info = _digest_entry_at(
            descriptors.state_descriptor, temporary
        )
        destination_digest, destination_info = _digest_entry_at(
            descriptors.repository_descriptor, destination
        )
        if (
            temp_digest != expected_digest
            or destination_digest != expected_digest
            or (temp_info.st_dev, temp_info.st_ino)
            != (destination_info.st_dev, destination_info.st_ino)
            or stat.S_IMODE(destination_info.st_mode) != 0o644
        ):
            raise ManifestError("configuration transaction failed")
        os.fsync(descriptors.repository_descriptor)


def _rollback_init_document(
    descriptors: LifecycleDescriptors, document: Mapping[str, object]
) -> bool:
    files = document.get("files")
    if type(files) is not list:
        return False
    safe = True
    for entry in reversed(files):
        if type(entry) is not dict:
            return False
        destination = str(entry.get("destination"))
        temporary = str(entry.get("temporary"))
        expected_digest = str(entry.get("sha256"))
        try:
            temp_digest, temp_info = _digest_entry_at(
                descriptors.state_descriptor, temporary
            )
        except (FileNotFoundError, ManifestError, OSError):
            safe = False
            continue
        try:
            destination_digest, destination_info = _digest_entry_at(
                descriptors.repository_descriptor, destination
            )
        except FileNotFoundError:
            continue
        except (ManifestError, OSError):
            safe = False
            continue
        if (
            temp_digest == expected_digest
            and destination_digest == expected_digest
            and (temp_info.st_dev, temp_info.st_ino)
            == (destination_info.st_dev, destination_info.st_ino)
        ):
            os.unlink(destination, dir_fd=descriptors.repository_descriptor)
        else:
            safe = False
    try:
        os.fsync(descriptors.repository_descriptor)
    except OSError:
        return False
    return safe


def _remove_init_temporaries(
    state_descriptor: int, document: Mapping[str, object]
) -> None:
    files = document.get("files")
    if type(files) is not list:
        raise ManifestError("configuration journal is malformed")
    for entry in files:
        if type(entry) is not dict:
            raise ManifestError("configuration journal is malformed")
        name = str(entry["temporary"])
        expected = str(entry["sha256"])
        try:
            digest, _ = _digest_entry_at(state_descriptor, name)
        except FileNotFoundError:
            continue
        if digest != expected:
            raise ManifestError("configuration recovery is unsafe")
        _unlink_regular_at(state_descriptor, name)


def _recover_configuration_transaction(descriptors: LifecycleDescriptors) -> bool:
    try:
        payload, info = _read_regular_entry_at(
            descriptors.state_descriptor, _INIT_JOURNAL, MAX_MANIFEST_BYTES
        )
    except FileNotFoundError:
        return True
    except (ManifestError, OSError):
        return False
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        return False
    try:
        kind, document = _parse_configuration_journal(payload)
        if kind == "init":
            return _recover_init_document(descriptors, document)
        return _recover_migration_document(descriptors, document)
    except (ManifestError, OSError, ValueError, TypeError):
        return False


def _recover_init_document(
    descriptors: LifecycleDescriptors, document: Mapping[str, object]
) -> bool:
    phase = document.get("phase")
    if phase == "prepared":
        if not _rollback_init_document(descriptors, document):
            return False
    elif phase == "committed":
        files = document.get("files")
        if type(files) is not list:
            return False
        for entry in files:
            if type(entry) is not dict:
                return False
            try:
                digest, _ = _digest_entry_at(
                    descriptors.repository_descriptor,
                    str(entry["destination"]),
                )
            except (FileNotFoundError, ManifestError, OSError):
                return False
            if digest != entry["sha256"]:
                return False
    else:
        return False
    _remove_init_temporaries(descriptors.state_descriptor, document)
    _unlink_regular_at(descriptors.state_descriptor, _INIT_JOURNAL)
    return True


def _replace_v1_manifest(
    descriptors: LifecycleDescriptors,
    *,
    expected_old_inode: int,
    expected_old_sha256: str,
    new_payload: bytes,
) -> None:
    current_digest, current_info = _digest_entry_at(
        descriptors.repository_descriptor, ".graphify-project.yaml"
    )
    if (
        current_info.st_ino != expected_old_inode
        or current_digest != expected_old_sha256
    ):
        raise _ConfigurationConflict("manifest changed before migration")
    transaction_id = secrets.token_hex(16)
    backup = f"migrate-{transaction_id}.backup"
    new_temporary = f"migrate-{transaction_id}.new"
    new_sha256 = hashlib.sha256(new_payload).hexdigest()
    document: dict[str, object] = {
        "destination": ".graphify-project.yaml",
        "old_inode": expected_old_inode,
        "old_sha256": expected_old_sha256,
        "backup": backup,
        "new_temporary": new_temporary,
        "new_sha256": new_sha256,
        "phase": "prepared",
    }
    journal_written = False
    try:
        _write_new_at(
            descriptors.state_descriptor, new_temporary, new_payload, 0o644
        )
        _write_new_at(
            descriptors.state_descriptor,
            _INIT_JOURNAL,
            _canonical_json(document),
            0o600,
        )
        journal_written = True
        latest_digest, latest_info = _digest_entry_at(
            descriptors.repository_descriptor, ".graphify-project.yaml"
        )
        if (
            latest_info.st_ino != expected_old_inode
            or latest_digest != expected_old_sha256
        ):
            raise _ConfigurationConflict("manifest changed before migration")
        os.rename(
            ".graphify-project.yaml",
            backup,
            src_dir_fd=descriptors.repository_descriptor,
            dst_dir_fd=descriptors.state_descriptor,
        )
        document["phase"] = "backed_up"
        _replace_journal_at(descriptors.state_descriptor, document)
        os.rename(
            new_temporary,
            ".graphify-project.yaml",
            src_dir_fd=descriptors.state_descriptor,
            dst_dir_fd=descriptors.repository_descriptor,
        )
        document["phase"] = "replaced"
        _replace_journal_at(descriptors.state_descriptor, document)
        installed_digest, installed_info = _digest_entry_at(
            descriptors.repository_descriptor, ".graphify-project.yaml"
        )
        if installed_digest != new_sha256 or stat.S_IMODE(installed_info.st_mode) != 0o644:
            raise ManifestError("configuration transaction failed")
        os.fsync(descriptors.repository_descriptor)
        document["phase"] = "committed"
        _replace_journal_at(descriptors.state_descriptor, document)
        _unlink_regular_at(descriptors.state_descriptor, backup)
        _unlink_regular_at(descriptors.state_descriptor, _INIT_JOURNAL)
    except _ConfigurationConflict:
        if journal_written:
            _recover_migration_document(descriptors, document, keep_journal=True)
        else:
            _unlink_regular_at(descriptors.state_descriptor, new_temporary)
        raise
    except BaseException as error:
        if journal_written:
            _recover_migration_document(descriptors, document, keep_journal=True)
        else:
            _unlink_regular_at(descriptors.state_descriptor, new_temporary)
        raise ManifestError("configuration transaction failed") from error


def _recover_migration_document(
    descriptors: LifecycleDescriptors,
    document: Mapping[str, object],
    *,
    keep_journal: bool = False,
) -> bool:
    destination = str(document["destination"])
    backup = str(document["backup"])
    new_temporary = str(document["new_temporary"])
    old_digest = str(document["old_sha256"])
    new_digest = str(document["new_sha256"])
    old_inode = int(document["old_inode"])
    phase = document["phase"]

    if phase == "committed":
        try:
            installed_digest, _ = _digest_entry_at(
                descriptors.repository_descriptor, destination
            )
        except (FileNotFoundError, ManifestError, OSError):
            return False
        if installed_digest != new_digest:
            return False
    else:
        try:
            backup_digest, backup_info = _digest_entry_at(
                descriptors.state_descriptor, backup
            )
            backup_exists = True
        except FileNotFoundError:
            backup_exists = False
            backup_digest = ""
            backup_info = None
        except (ManifestError, OSError):
            return False
        try:
            destination_digest, destination_info = _digest_entry_at(
                descriptors.repository_descriptor, destination
            )
            destination_exists = True
        except FileNotFoundError:
            destination_exists = False
            destination_digest = ""
            destination_info = None
        except (ManifestError, OSError):
            return False
        if backup_exists:
            if (
                backup_info is None
                or backup_info.st_ino != old_inode
                or backup_digest != old_digest
            ):
                return False
            if destination_exists:
                if destination_digest != new_digest:
                    return False
                os.unlink(destination, dir_fd=descriptors.repository_descriptor)
            os.rename(
                backup,
                destination,
                src_dir_fd=descriptors.state_descriptor,
                dst_dir_fd=descriptors.repository_descriptor,
            )
            os.fsync(descriptors.repository_descriptor)
        elif not (
            destination_exists
            and destination_info is not None
            and destination_info.st_ino == old_inode
            and destination_digest == old_digest
        ):
            return False
    try:
        temporary_digest, _ = _digest_entry_at(
            descriptors.state_descriptor, new_temporary
        )
    except FileNotFoundError:
        pass
    except (ManifestError, OSError):
        return False
    else:
        if temporary_digest != new_digest:
            return False
        _unlink_regular_at(descriptors.state_descriptor, new_temporary)
    try:
        _unlink_regular_at(descriptors.state_descriptor, backup)
    except ManifestError:
        return False
    if not keep_journal:
        _unlink_regular_at(descriptors.state_descriptor, _INIT_JOURNAL)
    return True


def confined_relative(value: str, field: str) -> PurePosixPath:
    """Return a byte-canonical POSIX relative path confined to its root."""
    value = _nonempty_string(value, field)
    if (
        "\\" in value
        or _DRIVE_PREFIX.match(value)
        or value.startswith("/")
        or _contains_control(value)
    ):
        raise ManifestError(f"{field} must be a confined relative path")
    path = PurePosixPath(value)
    if (
        not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ManifestError(f"{field} must be a confined relative path")
    return path


def validate_project_excludes(values: object) -> tuple[str, ...]:
    """Validate the shared, non-negated canonical glob-pattern boundary."""
    if type(values) not in {list, tuple}:
        raise ManifestError("excludes must be a finite list of patterns")
    result: list[str] = []
    for value in values:
        if type(value) is not str or not value or value.startswith("!"):
            raise ManifestError("excludes contain an invalid pattern")
        if (
            "\\" in value
            or value.startswith("/")
            or _DRIVE_PREFIX.match(value)
            or _contains_control(value)
        ):
            raise ManifestError("excludes contain an invalid pattern")
        path = PurePosixPath(value)
        if (
            not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != value
        ):
            raise ManifestError("excludes contain an invalid pattern")
        result.append(value)
    return tuple(result)


def render_manifest_v2(manifest: ProjectManifest) -> bytes:
    """Render one validated schema-v2 manifest with fixed field ordering."""
    validated = _validate_runtime_manifest_v2(manifest)
    lines = [
        "schema_version: 2",
        f"project_id: {_yaml_scalar(validated.project_id)}",
        f"project_uid: {validated.project_uid}",
        f"display_name: {_yaml_scalar(validated.display_name)}",
        "include_roots:",
        *(
            f"  - {_yaml_scalar(path.as_posix())}"
            for path in validated.include_roots
        ),
        "output_dir: graphify-out",
        f"obsidian_namespace: {_yaml_scalar(validated.obsidian_namespace.as_posix())}",
        *(
            ["excludes: []"]
            if not validated.excludes
            else [
                "excludes:",
                *(f"  - {_yaml_scalar(value)}" for value in validated.excludes),
            ]
        ),
        f"track_html: {'true' if validated.track_html else 'false'}",
        f"graphify_version: {_yaml_scalar(validated.graphify_version)}",
        "features:",
        f"  atlas: {validated.features.atlas}",
        f"  registry: {validated.features.registry}",
        "artifacts:",
        f"  provider: {validated.artifacts.provider}",
    ]
    if validated.artifacts.provider == "github-release":
        for key in (
            "host",
            "repository",
            "repository_id",
            "channel",
            "source_ref",
            "signer_workflow",
            "signer_digest",
        ):
            value = getattr(validated.artifacts, key)
            lines.append(
                f"  {key}: {value if type(value) is int else _yaml_scalar(value)}"
            )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _parse_v1(values: Mapping[str, object]) -> ProjectManifest:
    return ProjectManifest(schema_version=1, **_parse_common(values, V1_FIELDS))


def _parse_v2(values: Mapping[str, object]) -> ProjectManifest:
    common = _parse_common(values, V2_FIELDS)
    return ProjectManifest(
        schema_version=2,
        project_uid=_uuid4(values["project_uid"]),
        features=_parse_features(values["features"]),
        artifacts=_parse_artifacts(values["artifacts"]),
        **common,
    )


def _parse_common(
    values: Mapping[str, object], fields: frozenset[str]
) -> dict[str, Any]:
    unknown = set(values) - fields
    missing = fields - set(values)
    if unknown:
        raise ManifestError(
            f"manifest contains unknown keys: {', '.join(sorted(unknown))}"
        )
    if missing:
        raise ManifestError(
            f"manifest is missing required keys: {', '.join(sorted(missing))}"
        )
    expected_schema = 2 if fields is V2_FIELDS else 1
    if (
        type(values["schema_version"]) is not int
        or values["schema_version"] != expected_schema
    ):
        raise ManifestError(f"schema_version must be {expected_schema}")
    project_id = _nonempty_string(values["project_id"], "project_id")
    if not _PROJECT_ID.fullmatch(project_id):
        raise ManifestError("project_id must match [a-z0-9][a-z0-9-]{1,62}")
    display_name = _nonempty_string(values["display_name"], "display_name")
    include_roots = _path_list(values["include_roots"], "include_roots")
    if not include_roots:
        raise ManifestError("include_roots must not be empty")
    if len(set(include_roots)) != len(include_roots):
        raise ManifestError("include_roots must not contain duplicates")
    output_dir = confined_relative(
        _nonempty_string(values["output_dir"], "output_dir"), "output_dir"
    )
    if output_dir != PurePosixPath("graphify-out"):
        raise ManifestError("output_dir must be graphify-out")
    obsidian_namespace = confined_relative(
        _nonempty_string(values["obsidian_namespace"], "obsidian_namespace"),
        "obsidian_namespace",
    )
    if obsidian_namespace.parts != ("Projects", project_id, "Generated"):
        raise ManifestError(
            "obsidian_namespace must be Projects/{project_id}/Generated"
        )
    excludes = validate_project_excludes(values["excludes"])
    track_html = values["track_html"]
    if type(track_html) is not bool:
        raise ManifestError("track_html must be a boolean")
    graphify_version = _graphify_version(values["graphify_version"])
    return {
        "project_id": project_id,
        "display_name": display_name,
        "include_roots": include_roots,
        "output_dir": output_dir,
        "obsidian_namespace": obsidian_namespace,
        "excludes": excludes,
        "track_html": track_html,
        "graphify_version": graphify_version,
    }


def _parse_features(value: object) -> FeatureIntent:
    if type(value) is not dict or set(value) != FEATURE_FIELDS:
        raise ManifestError("feature intent schema is invalid")
    atlas = value["atlas"]
    registry = value["registry"]
    if type(atlas) is not str or atlas not in {"disabled", "enabled"}:
        raise ManifestError("feature intent is invalid")
    if type(registry) is not str or registry not in {"disabled", "enabled"}:
        raise ManifestError("feature intent is invalid")
    return FeatureIntent(atlas=atlas, registry=registry)


def _parse_artifacts(value: object) -> ArtifactIntent:
    if type(value) is not dict or "provider" not in value:
        raise ManifestError("artifact transport schema is invalid")
    provider = value["provider"]
    if type(provider) is str and provider == "none":
        if set(value) != {"provider"}:
            raise ManifestError("artifact transport schema is invalid")
        return ArtifactIntent()
    if type(provider) is not str or provider != "github-release":
        raise ManifestError("artifact transport schema is invalid")
    if set(value) != GITHUB_FIELDS:
        raise ManifestError("artifact transport schema is invalid")
    host = _nonempty_string(value["host"], "artifact host")
    repository = _nonempty_string(value["repository"], "artifact repository")
    repository_id = value["repository_id"]
    channel = _nonempty_string(value["channel"], "artifact channel")
    source_ref = _nonempty_string(value["source_ref"], "artifact source_ref")
    signer_workflow = _nonempty_string(
        value["signer_workflow"], "artifact signer_workflow"
    )
    signer_digest = _nonempty_string(
        value["signer_digest"], "artifact signer_digest"
    )
    repository_parts = repository.split("/")
    valid_repository = (
        len(repository_parts) == 2
        and all(_REPOSITORY_COMPONENT.fullmatch(part) for part in repository_parts)
        and not _contains_control(repository)
    )
    workflow_prefix = f"{repository}/.github/workflows/"
    workflow_name = signer_workflow.removeprefix(workflow_prefix)
    if (
        host != "github.com"
        or not valid_repository
        or type(repository_id) is not int
        or repository_id <= 0
        or not _CHANNEL.fullmatch(channel)
        or not source_ref.startswith("refs/heads/")
        or not _valid_ref_tail(source_ref.removeprefix("refs/heads/"))
        or not signer_workflow.startswith(workflow_prefix)
        or "/" in workflow_name
        or not workflow_name.endswith(".yml")
        or not _REPOSITORY_COMPONENT.fullmatch(workflow_name)
        or not _SHA1.fullmatch(signer_digest)
    ):
        raise ManifestError("artifact transport identity is invalid")
    return ArtifactIntent(
        provider="github-release",
        host=host,
        repository=repository,
        repository_id=repository_id,
        channel=channel,
        source_ref=source_ref,
        signer_workflow=signer_workflow,
        signer_digest=signer_digest,
    )


def _validate_runtime_manifest_v2(manifest: ProjectManifest) -> ProjectManifest:
    if type(manifest) is not ProjectManifest:
        raise ManifestError("manifest runtime object is invalid")
    if type(manifest.project_uid) is not UUID:
        raise ManifestError("project_uid must be a UUIDv4")
    if type(manifest.features) is not FeatureIntent:
        raise ManifestError("feature intent schema is invalid")
    if type(manifest.artifacts) is not ArtifactIntent:
        raise ManifestError("artifact transport schema is invalid")
    if type(manifest.include_roots) is not tuple:
        raise ManifestError("include_roots must be a tuple of POSIX paths")
    if any(type(path) is not PurePosixPath for path in manifest.include_roots):
        raise ManifestError("include_roots must contain POSIX paths")
    if type(manifest.output_dir) is not PurePosixPath:
        raise ManifestError("output_dir must be a POSIX path")
    if type(manifest.obsidian_namespace) is not PurePosixPath:
        raise ManifestError("obsidian_namespace must be a POSIX path")
    if type(manifest.excludes) is not tuple:
        raise ManifestError("excludes must be a tuple of patterns")
    artifact_values = {"provider": manifest.artifacts.provider}
    if manifest.artifacts.provider == "github-release":
        artifact_values.update(
            {
                key: getattr(manifest.artifacts, key)
                for key in GITHUB_FIELDS - {"provider"}
            }
        )
    elif any(
        getattr(manifest.artifacts, key) is not None
        for key in GITHUB_FIELDS - {"provider"}
    ):
        raise ManifestError("artifact transport schema is invalid")
    values: dict[str, object] = {
        "schema_version": manifest.schema_version,
        "project_id": manifest.project_id,
        "project_uid": str(manifest.project_uid),
        "display_name": manifest.display_name,
        "include_roots": [path.as_posix() for path in manifest.include_roots],
        "output_dir": manifest.output_dir.as_posix(),
        "obsidian_namespace": manifest.obsidian_namespace.as_posix(),
        "excludes": list(manifest.excludes),
        "track_html": manifest.track_html,
        "graphify_version": manifest.graphify_version,
        "features": {
            "atlas": manifest.features.atlas,
            "registry": manifest.features.registry,
        },
        "artifacts": artifact_values,
    }
    return _parse_v2(values)


def _uuid4(value: object) -> UUID:
    if type(value) is not str or not value or _contains_control(value):
        raise ManifestError("project_uid must be a UUIDv4")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError) as error:
        raise ManifestError("project_uid must be a UUIDv4") from error
    if parsed.version != 4 or parsed.variant != RFC_4122 or str(parsed) != value:
        raise ManifestError("project_uid must be a canonical UUIDv4")
    return parsed


def _graphify_version(value: object) -> str:
    version = _nonempty_string(value, "graphify_version")
    try:
        resolve_graphify_compatibility(version)
    except CompatibilityError as error:
        raise ManifestError(str(error)) from error
    return version


def _path_list(value: object, field: str) -> tuple[PurePosixPath, ...]:
    if type(value) is not list:
        raise ManifestError(f"{field} must be a list")
    return tuple(confined_relative(item, field) for item in value)


def _nonempty_string(value: object, field: str) -> str:
    if type(value) is not str or not value.strip() or _contains_control(value):
        raise ManifestError(f"{field} must be a non-empty string")
    return value


def _valid_ref_tail(value: str) -> bool:
    if not value or "\\" in value or _contains_control(value):
        return False
    path = PurePosixPath(value)
    return (
        bool(path.parts)
        and all(part not in {"", ".", ".."} for part in path.parts)
        and path.as_posix() == value
    )


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _yaml_scalar(value: object) -> str:
    if type(value) is not str or not value or _contains_control(value):
        raise ManifestError("manifest scalar is invalid")
    return json.dumps(value, ensure_ascii=False)


def _read_capped(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise ManifestError("manifest exceeds its byte cap")
    return payload


def _pread_capped(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while offset <= limit:
        chunk = os.pread(descriptor, min(65_536, limit + 1 - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise ManifestError("manifest exceeds its byte cap")
    return payload


def _binding_metadata(
    info: os.stat_result,
) -> tuple[tuple[int, int], int, int, int]:
    return (
        (info.st_dev, info.st_ino),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
