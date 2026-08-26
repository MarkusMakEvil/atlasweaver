"""Strict, project-agnostic fleet configuration and repository admission."""

from __future__ import annotations

from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, is_dataclass
import json
from collections.abc import Mapping
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
from typing import Literal, Protocol, TypeAlias
from uuid import UUID

from .locking import RepositoryAccess, RepositoryIdentity, open_repository_access
from .manifest import (
    MAX_MANIFEST_BYTES,
    ManifestError,
    _load_one_yaml,
    inspect_init_journal,
    load_manifest_payload,
)
from .models import ProjectManifest
from .queries import RegistryQueryRequest


MAX_FLEET_BYTES = 262_144
MAX_GIT_FILE_BYTES = 4_096
_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}\Z")
_TOP_FIELDS = frozenset({"schema_version", "projects", "defaults"})
_PROJECT_FIELDS = frozenset({"id", "repository"})
_DEFAULT_FIELDS = frozenset({"max_parallel"})


class FleetConfigError(ValueError):
    """A path-free, stable fleet admission failure."""

    def __init__(self, code: str, public_message: str | None = None) -> None:
        if type(code) is not str or not code.startswith("fleet_") and code != "init_recovery_required":
            raise ValueError("fleet error code is invalid")
        self.code = code
        self.public_message = public_message or code
        super().__init__(self.public_message)


@dataclass(frozen=True)
class FleetProject:
    id: str
    repository: PurePosixPath
    root: Path
    repository_identity: RepositoryIdentity
    project_uid: UUID


@dataclass(frozen=True)
class FleetWorkspace:
    schema_version: int
    root: Path
    projects: tuple[FleetProject, ...]
    max_parallel: int


@dataclass(frozen=True)
class FleetAliasKey:
    kind: Literal["git", "repository"]
    identity: RepositoryIdentity


def load_fleet_workspace(path: Path) -> FleetWorkspace:
    """Descriptor-capture and validate one closed universal fleet document."""
    if not isinstance(path, Path):
        raise FleetConfigError("fleet_path_invalid")
    stack = ExitStack()
    try:
        parent_fd, root = _open_absolute_directory(path.absolute().parent, stack)
        payload, _ = _read_regular_at(
            parent_fd, path.name, MAX_FLEET_BYTES, "fleet_document"
        )
        try:
            document = _load_one_yaml(payload)
        except ManifestError:
            raise FleetConfigError("fleet_document_invalid") from None
        entries, max_parallel = _parse_workspace(document)
        paths = tuple(item[1].parts for item in entries)
        _reject_nested(paths)

        projects: list[FleetProject] = []
        aliases: set[FleetAliasKey] = set()
        identities: set[RepositoryIdentity] = set()
        uids: set[UUID] = set()
        lexical: set[str] = set()
        for project_id, relative in entries:
            key = relative.as_posix()
            if key in lexical:
                raise FleetConfigError("fleet_repository_duplicate")
            lexical.add(key)
            repository_fd = _open_relative_directory(parent_fd, relative, stack)
            info = os.fstat(repository_fd)
            identity = (info.st_dev, info.st_ino)
            if identity in identities:
                raise FleetConfigError("fleet_repository_duplicate")
            identities.add(identity)
            access = RepositoryAccess(os.dup(repository_fd), identity)
            stack.callback(access.__exit__, None, None, None)
            try:
                if inspect_init_journal(root / relative, repository_access=access) != "none":
                    raise FleetConfigError("init_recovery_required")
                manifest_payload, _ = _read_regular_at(
                    access.descriptor,
                    ".graphify-project.yaml",
                    MAX_MANIFEST_BYTES,
                    "fleet_manifest",
                )
                manifest = load_manifest_payload(manifest_payload, root / relative)
            except FleetConfigError:
                raise
            except (ManifestError, OSError, ValueError):
                raise FleetConfigError("fleet_manifest_invalid") from None
            if (
                manifest.schema_version != 2
                or manifest.project_uid is None
                or manifest.project_id != project_id
            ):
                raise FleetConfigError("fleet_manifest_mismatch")
            if manifest.project_uid in uids:
                raise FleetConfigError("fleet_project_uid_duplicate")
            uids.add(manifest.project_uid)
            alias = _git_alias_key(access)
            if alias in aliases:
                raise FleetConfigError("fleet_worktree_alias")
            aliases.add(alias)
            projects.append(FleetProject(
                project_id,
                relative,
                root.joinpath(*relative.parts),
                identity,
                manifest.project_uid,
            ))
        return FleetWorkspace(1, root, tuple(projects), max_parallel)
    except FleetConfigError:
        raise
    except (OSError, ValueError, TypeError):
        raise FleetConfigError("fleet_repository_invalid") from None
    finally:
        stack.close()


def select_fleet_projects(
    workspace: FleetWorkspace, project_ids: tuple[str, ...]
) -> tuple[FleetProject, ...]:
    if type(workspace) is not FleetWorkspace or type(project_ids) is not tuple:
        raise FleetConfigError("fleet_selection_invalid")
    if not project_ids:
        return workspace.projects
    if any(type(item) is not str or _ID.fullmatch(item) is None for item in project_ids):
        raise FleetConfigError("fleet_selection_invalid")
    requested = set(project_ids)
    if len(requested) != len(project_ids):
        raise FleetConfigError("fleet_selection_duplicate")
    available = {project.id for project in workspace.projects}
    if not requested <= available:
        raise FleetConfigError("fleet_project_unknown")
    return tuple(project for project in workspace.projects if project.id in requested)


def _parse_workspace(
    document: dict[str, object],
) -> tuple[tuple[tuple[str, PurePosixPath], ...], int]:
    if set(document) not in ({"schema_version", "projects"}, _TOP_FIELDS):
        raise FleetConfigError("fleet_schema_invalid")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise FleetConfigError("fleet_schema_invalid")
    projects = document["projects"]
    if type(projects) is not list or not projects:
        raise FleetConfigError("fleet_projects_invalid")
    parsed: list[tuple[str, PurePosixPath]] = []
    ids: set[str] = set()
    for value in projects:
        if type(value) is not dict or set(value) != _PROJECT_FIELDS:
            raise FleetConfigError("fleet_project_invalid")
        project_id = value["id"]
        repository = value["repository"]
        if type(project_id) is not str or _ID.fullmatch(project_id) is None:
            raise FleetConfigError("fleet_project_invalid")
        if project_id in ids:
            raise FleetConfigError("fleet_project_duplicate")
        ids.add(project_id)
        parsed.append((project_id, _repository_path(repository)))
    defaults = document.get("defaults", {})
    if type(defaults) is not dict or set(defaults) - _DEFAULT_FIELDS:
        raise FleetConfigError("fleet_defaults_invalid")
    max_parallel = defaults.get("max_parallel", 2)
    if type(max_parallel) is not int or not 1 <= max_parallel <= 8:
        raise FleetConfigError("fleet_parallel_invalid")
    return tuple(parsed), max_parallel


def _repository_path(value: object) -> PurePosixPath:
    if type(value) is not str or not value or "\\" in value or len(value.encode("utf-8")) > 4096:
        raise FleetConfigError("fleet_repository_path_invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or any(
            part in {"", ".", ".."}
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            for part in path.parts
        )
    ):
        raise FleetConfigError("fleet_repository_path_invalid")
    return path


def _reject_nested(paths: tuple[tuple[str, ...], ...]) -> None:
    for index, first in enumerate(paths):
        for second in paths[index + 1:]:
            shorter, longer = sorted((first, second), key=len)
            if longer[:len(shorter)] == shorter:
                raise FleetConfigError("fleet_repository_nested")


def _open_absolute_directory(path: Path, stack: ExitStack) -> tuple[int, Path]:
    absolute = path.absolute()
    if not absolute.is_absolute():
        raise FleetConfigError("fleet_path_invalid")
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    stack.callback(os.close, current)
    for part in absolute.parts[1:]:
        current = _open_child_directory(current, part, stack)
    return current, absolute


def _open_relative_directory(
    parent_fd: int, relative: PurePosixPath, stack: ExitStack
) -> int:
    current = os.dup(parent_fd)
    stack.callback(os.close, current)
    for part in relative.parts:
        current = _open_child_directory(current, part, stack)
    return current


def _open_child_directory(parent_fd: int, name: str, stack: ExitStack) -> int:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise FleetConfigError("fleet_repository_invalid")
    child = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_fd,
    )
    stack.callback(os.close, child)
    opened = os.fstat(child)
    after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if _identity(opened) != _identity(before) or _identity(opened) != _identity(after):
        raise FleetConfigError("fleet_repository_changed")
    return child


def _read_regular_at(
    parent_fd: int, name: str, limit: int, code_prefix: str
) -> tuple[bytes, os.stat_result]:
    if not name or "/" in name or "\\" in name:
        raise FleetConfigError(f"{code_prefix}_invalid")
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise FleetConfigError(f"{code_prefix}_invalid")
        if before.st_size > limit:
            raise FleetConfigError(f"{code_prefix}_too_large")
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
        )
    except FleetConfigError:
        raise
    except FileNotFoundError:
        raise FleetConfigError(f"{code_prefix}_missing") from None
    except OSError:
        raise FleetConfigError(f"{code_prefix}_invalid") from None
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise FleetConfigError(f"{code_prefix}_changed")
        first = _pread_capped(descriptor, limit, code_prefix)
        middle = os.fstat(descriptor)
        second = _pread_capped(descriptor, limit, code_prefix)
        after = os.fstat(descriptor)
        rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _file_identity(before) != _file_identity(middle)
            or _file_identity(before) != _file_identity(after)
            or _identity(before) != _identity(rebound)
            or first != second
        ):
            raise FleetConfigError(f"{code_prefix}_changed")
        return first, opened
    finally:
        os.close(descriptor)


def _pread_capped(descriptor: int, limit: int, prefix: str) -> bytes:
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
        raise FleetConfigError(f"{prefix}_too_large")
    return payload


def _git_alias_key(repository: RepositoryAccess) -> FleetAliasKey:
    """Resolve Git/common-dir identity from retained descriptors, never paths."""
    try:
        named = os.stat(".git", dir_fd=repository.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return FleetAliasKey("repository", repository.identity)
    except OSError:
        raise FleetConfigError("fleet_git_changed") from None
    if stat.S_ISDIR(named.st_mode) and not stat.S_ISLNK(named.st_mode):
        git_fd = _open_bound_directory(repository.descriptor, ".git")
    elif stat.S_ISREG(named.st_mode):
        payload, _ = _read_regular_at(
            repository.descriptor, ".git", MAX_GIT_FILE_BYTES, "fleet_git"
        )
        target = _parse_gitdir(payload)
        git_fd = _open_git_target(repository.descriptor, target)
    else:
        raise FleetConfigError("fleet_git_changed")
    try:
        _after_git_alias_open_before_finalize()
        common_fd = _open_common_git_directory(git_fd)
        try:
            info = os.fstat(common_fd)
            return FleetAliasKey("git", (info.st_dev, info.st_ino))
        finally:
            if common_fd != git_fd:
                os.close(common_fd)
    finally:
        os.close(git_fd)


def _parse_gitdir(payload: bytes) -> PurePosixPath:
    try:
        text = payload.decode("utf-8")
    except UnicodeError:
        raise FleetConfigError("fleet_git_changed") from None
    if not text.endswith("\n") or text.count("\n") != 1 or not text.startswith("gitdir: "):
        raise FleetConfigError("fleet_git_changed")
    value = text[8:-1]
    if not value or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise FleetConfigError("fleet_git_changed")
    target = PurePosixPath(value)
    if not target.parts or target.as_posix() != value or value.startswith("//"):
        raise FleetConfigError("fleet_git_changed")
    return target


def _after_git_alias_open_before_finalize() -> None:
    """Private no-op checkpoint used to exercise descriptor-retention races."""


def _open_git_target(repository_fd: int, target: PurePosixPath) -> int:
    if target.is_absolute():
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        parts = target.parts[1:]
    else:
        current = os.dup(repository_fd)
        parts = target.parts
    try:
        for part in parts:
            if part in {"", "."} or any(ord(c) < 32 or ord(c) == 127 for c in part):
                raise FleetConfigError("fleet_git_changed")
            child = _open_bound_directory(current, part)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _open_common_git_directory(git_fd: int) -> int:
    try:
        payload, _ = _read_regular_at(
            git_fd, "commondir", MAX_GIT_FILE_BYTES, "fleet_git"
        )
    except FleetConfigError as error:
        if error.code == "fleet_git_missing":
            return git_fd
        raise FleetConfigError("fleet_git_changed") from None
    try:
        text = payload.decode("utf-8")
    except UnicodeError:
        raise FleetConfigError("fleet_git_changed") from None
    if not text.endswith("\n") or text.count("\n") != 1:
        raise FleetConfigError("fleet_git_changed")
    parts = tuple(PurePosixPath(text[:-1]).parts)
    if not 1 <= len(parts) <= 8 or any(part != ".." for part in parts):
        raise FleetConfigError("fleet_git_changed")
    current = os.dup(git_fd)
    try:
        for part in parts:
            child = _open_bound_directory(current, part)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _open_bound_directory(parent_fd: int, name: str) -> int:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise OSError
        child = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        opened = os.fstat(child)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(opened) != _identity(before) or _identity(opened) != _identity(after):
            os.close(child)
            raise OSError
        return child
    except OSError:
        raise FleetConfigError("fleet_git_changed") from None


def _identity(value: os.stat_result) -> RepositoryIdentity:
    return value.st_dev, value.st_ino


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


# -- Bounded universal fleet operations ----------------------------------


FleetOperation = Literal[
    "doctor", "health", "pull", "refresh", "registry-sync", "query"
]
_OPERATIONS = frozenset(
    {"doctor", "health", "pull", "refresh", "registry-sync", "query"}
)
_BASE_ENVIRONMENT = ("HOME", "LANG", "LC_ALL", "PATH")
_RESULT_LIMIT = 1_048_576


@dataclass(frozen=True)
class FleetProjectAdmission:
    project: FleetProject
    manifest: ProjectManifest


@dataclass(frozen=True)
class FleetProjectResult:
    id: str
    uid: str
    status: str
    result: dict[str, object] | None
    error_code: str | None
    duration_ms: int


@dataclass(frozen=True)
class FleetResult:
    operation: FleetOperation
    status: Literal["ok", "partial_failure", "failed"]
    projects: tuple[FleetProjectResult, ...]
    result: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "operation": self.operation,
            "status": self.status,
            "projects": [asdict(item) for item in self.projects],
            "result": self.result,
        }


@dataclass(frozen=True)
class FleetProjectOutcome:
    status: str
    result: dict[str, object] | None


class FleetProjectRunner(Protocol):
    def __call__(
        self,
        admission: FleetProjectAdmission,
        operation: FleetOperation,
        request: "FleetProjectWorkRequest",
    ) -> FleetProjectOutcome: ...


@dataclass(frozen=True)
class FleetBaseRequest:
    pass


@dataclass(frozen=True)
class PullFleetRequest:
    credentials: object = field(repr=False)


@dataclass(frozen=True)
class ProjectBackendEnvironment:
    project_uid: UUID
    credential_name: str | None = field(repr=False)
    entries: tuple[tuple[str, str], ...] = field(repr=False)

    @classmethod
    def capture(
        cls,
        project_uid: UUID,
        values: Mapping[str, str],
        *,
        canonical_credential_name: str | None,
    ) -> "ProjectBackendEnvironment":
        if type(project_uid) is not UUID or project_uid.version != 4:
            raise FleetConfigError("fleet_invalid")
        if canonical_credential_name is not None and (
            type(canonical_credential_name) is not str
            or not canonical_credential_name
            or canonical_credential_name in _BASE_ENVIRONMENT
        ):
            raise FleetConfigError("fleet_invalid")
        try:
            first = tuple(values.items())
            second = tuple(values.items())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            raise FleetConfigError("fleet_invalid") from None
        if first != second or any(
            type(key) is not str or type(value) is not str
            for key, value in first
        ):
            raise FleetConfigError("fleet_invalid")
        copied = dict(first)
        allowed = {*_BASE_ENVIRONMENT}
        if canonical_credential_name is not None:
            allowed.add(canonical_credential_name)
        if set(copied) != allowed or any(
            not copied[name] or any(ord(char) < 32 for char in copied[name])
            for name in allowed
        ):
            raise FleetConfigError("fleet_invalid")
        ordered = tuple(
            (name, copied[name])
            for name in (*_BASE_ENVIRONMENT,)
            if name in copied
        )
        if canonical_credential_name is not None:
            ordered += ((canonical_credential_name, copied[canonical_credential_name]),)
        return cls(project_uid, canonical_credential_name, ordered)

    def as_mapping(self) -> dict[str, str]:
        return dict(self.entries)


@dataclass(frozen=True)
class RefreshFleetRequest:
    backend: str | None
    model: str | None
    deep: bool
    code_only: bool
    project_environments: tuple[ProjectBackendEnvironment, ...] = field(repr=False)


@dataclass(frozen=True)
class FleetQueryRequest:
    query: RegistryQueryRequest


FleetOperationRequest: TypeAlias = (
    FleetBaseRequest | PullFleetRequest | RefreshFleetRequest | FleetQueryRequest
)


@dataclass(frozen=True)
class BaseProjectWorkRequest:
    pass


@dataclass(frozen=True)
class PullProjectWorkRequest:
    credentials: object = field(repr=False)


@dataclass(frozen=True)
class RefreshProjectWorkRequest:
    options: object
    environment: ProjectBackendEnvironment | None = field(repr=False)


FleetProjectWorkRequest: TypeAlias = (
    BaseProjectWorkRequest | PullProjectWorkRequest | RefreshProjectWorkRequest
)


def _load_fleet_project_manifest(
    project: FleetProject,
    *,
    expected_manifest: ProjectManifest | None = None,
) -> FleetProjectAdmission:
    """Reload a project's complete manifest through its retained root identity."""
    try:
        with open_repository_access(
            project.root,
            expected_repository_identity=project.repository_identity,
        ) as repository:
            if inspect_init_journal(
                project.root, repository_access=repository
            ) != "none":
                raise FleetConfigError("init_recovery_required")
            payload, _ = _read_regular_at(
                repository.descriptor,
                ".graphify-project.yaml",
                MAX_MANIFEST_BYTES,
                "fleet_manifest",
            )
            manifest = load_manifest_payload(payload, project.root)
    except FleetConfigError as error:
        if error.code.startswith("fleet_manifest_"):
            raise FleetConfigError("fleet_manifest_changed") from None
        raise
    except (ManifestError, OSError, ValueError, TypeError):
        raise FleetConfigError("fleet_manifest_changed") from None
    if (
        manifest.schema_version != 2
        or manifest.project_uid != project.project_uid
        or manifest.project_id != project.id
        or (expected_manifest is not None and manifest != expected_manifest)
    ):
        raise FleetConfigError("fleet_manifest_changed")
    return FleetProjectAdmission(project, manifest)


def require_fleet_projects_ready(
    selected: tuple[FleetProject, ...],
) -> tuple[FleetProjectAdmission, ...]:
    if type(selected) is not tuple or not selected or any(
        type(project) is not FleetProject for project in selected
    ):
        raise FleetConfigError("fleet_invalid")
    return tuple(_load_fleet_project_manifest(project) for project in selected)


def _validate_request(
    operation: FleetOperation,
    selected: tuple[FleetProject, ...],
    request: FleetOperationRequest,
) -> None:
    required = {
        "doctor": FleetBaseRequest,
        "health": FleetBaseRequest,
        "registry-sync": FleetBaseRequest,
        "pull": PullFleetRequest,
        "refresh": RefreshFleetRequest,
        "query": FleetQueryRequest,
    }
    if operation not in _OPERATIONS or type(request) is not required.get(operation):
        raise FleetConfigError("fleet_invalid")
    if type(selected) is not tuple or not selected or len(set(selected)) != len(selected):
        raise FleetConfigError("fleet_invalid")
    if type(request) is RefreshFleetRequest:
        if type(request.deep) is not bool or type(request.code_only) is not bool:
            raise FleetConfigError("fleet_invalid")
        if request.code_only:
            if (
                request.backend is not None
                or request.model is not None
                or request.deep
                or request.project_environments
            ):
                raise FleetConfigError("fleet_invalid")
        else:
            if (
                type(request.backend) is not str
                or not request.backend
                or type(request.model) is not str
                or not request.model
                or type(request.project_environments) is not tuple
            ):
                raise FleetConfigError("fleet_invalid")
            expected = {project.project_uid for project in selected}
            actual = {item.project_uid for item in request.project_environments}
            if len(actual) != len(request.project_environments) or actual != expected:
                raise FleetConfigError("fleet_invalid")


def _project_request(
    admission: FleetProjectAdmission,
    request: FleetOperationRequest,
) -> FleetProjectWorkRequest:
    if type(request) is PullFleetRequest:
        return PullProjectWorkRequest(request.credentials)
    if type(request) is RefreshFleetRequest:
        from .lifecycle import RefreshOptions

        environment = next(
            (
                item
                for item in request.project_environments
                if item.project_uid == admission.project.project_uid
            ),
            None,
        )
        return RefreshProjectWorkRequest(
            RefreshOptions(request.backend, request.model, request.deep, request.code_only),
            environment,
        )
    return BaseProjectWorkRequest()


def _serialize_domain_result(value: object) -> dict[str, object]:
    if hasattr(value, "to_dict"):
        document = value.to_dict()  # type: ignore[union-attr]
    elif is_dataclass(value):
        document = asdict(value)
    elif type(value) is dict:
        document = dict(value)
    else:
        raise FleetConfigError("fleet_result_invalid")
    try:
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise FleetConfigError("fleet_result_invalid") from None
    if len(encoded) > _RESULT_LIMIT:
        raise FleetConfigError("fleet_result_invalid")
    return document


def _default_fleet_runner(
    admission: FleetProjectAdmission,
    operation: FleetOperation,
    request: FleetProjectWorkRequest,
) -> FleetProjectOutcome:
    project = admission.project
    if operation == "doctor":
        from .doctor import doctor_project

        result = doctor_project(
            project.root,
            expected_repository_identity=project.repository_identity,
            expected_manifest=admission.manifest,
        )
    elif operation == "health":
        from .health import assess_health, inspect_project_state

        result = assess_health(
            inspect_project_state(
                project.root,
                admission.manifest,
                expected_repository_identity=project.repository_identity,
            )
        )
    elif operation == "refresh":
        from .lifecycle import refresh_project

        assert type(request) is RefreshProjectWorkRequest
        result = refresh_project(
            project.root,
            admission.manifest,
            request.options,
            ambient=(
                request.environment.as_mapping()
                if request.environment is not None
                else {}
            ),
            expected_repository_identity=project.repository_identity,
        )
    elif operation == "pull":
        from .github_artifacts import pull_bundle

        assert type(request) is PullProjectWorkRequest
        result = pull_bundle(
            project.root,
            request.credentials,
            expected_repository_identity=project.repository_identity,
            expected_manifest=admission.manifest,
        )
    elif operation == "registry-sync":
        from .registry import registry_sync

        result = registry_sync(
            project.root,
            admission.manifest,
            expected_repository_identity=project.repository_identity,
        )
    else:
        raise FleetConfigError("fleet_invalid")
    document = _serialize_domain_result(result)
    status = str(document.get("status", "ok"))
    return FleetProjectOutcome(status, document)


DEFAULT_FLEET_RUNNER: FleetProjectRunner = _default_fleet_runner


def _failure_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if type(code) is str and (
        code.startswith("fleet_")
        or code.startswith("registry_")
        or code
        in {
            "init_recovery_required",
            "manifest_migration_required",
            "graph_missing",
            "graph_invalid",
        }
    ):
        return code
    kind = getattr(error, "kind", None)
    if kind in {"authority", "changed"}:
        return "fleet_repository_changed" if kind == "authority" else "fleet_manifest_changed"
    return "fleet_operation_failed"


def _run_one(
    admission: FleetProjectAdmission,
    operation: FleetOperation,
    request: FleetOperationRequest,
    runner: FleetProjectRunner,
) -> FleetProjectResult:
    started = time.monotonic_ns()
    try:
        current = _load_fleet_project_manifest(
            admission.project, expected_manifest=admission.manifest
        )
        outcome = runner(current, operation, _project_request(current, request))
        if (
            type(outcome) is not FleetProjectOutcome
            or type(outcome.status) is not str
            or not outcome.status
            or (outcome.result is not None and type(outcome.result) is not dict)
        ):
            raise FleetConfigError("fleet_result_invalid")
        result = None if outcome.result is None else _serialize_domain_result(outcome.result)
        code = None
        status = outcome.status
    except BaseException as error:
        result = None
        code = _failure_code(error)
        status = "error"
    duration = max(0, (time.monotonic_ns() - started) // 1_000_000)
    return FleetProjectResult(
        admission.project.id,
        str(admission.project.project_uid),
        status,
        result,
        code,
        duration,
    )


def run_fleet_operation(
    workspace: FleetWorkspace,
    operation: FleetOperation,
    selected: tuple[FleetProject, ...],
    request: FleetOperationRequest,
    runner: FleetProjectRunner = DEFAULT_FLEET_RUNNER,
    *,
    expected_admissions: tuple[FleetProjectAdmission, ...] | None = None,
) -> FleetResult:
    """Run one project-agnostic operation with bounded, isolated workers."""
    if type(workspace) is not FleetWorkspace:
        raise FleetConfigError("fleet_invalid")
    _validate_request(operation, selected, request)
    fresh = require_fleet_projects_ready(selected)
    if expected_admissions is not None:
        if (
            type(expected_admissions) is not tuple
            or len(expected_admissions) != len(fresh)
            or any(
                type(old) is not FleetProjectAdmission
                or old.project != new.project
                or old.manifest != new.manifest
                for old, new in zip(expected_admissions, fresh)
            )
        ):
            raise FleetConfigError("fleet_manifest_changed")

    if operation == "query":
        from .registry import (
            capture_registry_snapshot,
            query_registry,
            registry_status,
        )

        assert type(request) is FleetQueryRequest
        rows: list[FleetProjectResult] = []
        for admission in fresh:
            started = time.monotonic_ns()
            current = registry_status(
                admission.project.root,
                admission.manifest,
                expected_repository_identity=admission.project.repository_identity,
            )
            duration = max(0, (time.monotonic_ns() - started) // 1_000_000)
            code = None if current.status == "current" else f"registry_{current.status}"
            rows.append(FleetProjectResult(
                admission.project.id,
                str(admission.project.project_uid),
                "current" if code is None else "error",
                None,
                code,
                duration,
            ))
        if any(row.error_code for row in rows):
            return FleetResult(operation, "failed", tuple(rows), None)
        snapshot = capture_registry_snapshot(
            tuple(item.project.project_uid for item in fresh),
            require_graphify_projection=True,
        )
        envelope = query_registry(snapshot, request.query)
        return FleetResult(operation, "ok", tuple(rows), envelope.to_dict())

    rows: list[FleetProjectResult | None] = [None] * len(fresh)
    if operation == "registry-sync":
        for index, admission in enumerate(fresh):
            rows[index] = _run_one(admission, operation, request, runner)
    else:
        with ThreadPoolExecutor(max_workers=workspace.max_parallel) as executor:
            futures = {
                executor.submit(_run_one, admission, operation, request, runner): index
                for index, admission in enumerate(fresh)
            }
            for future in as_completed(futures):
                rows[futures[future]] = future.result()
    completed = tuple(item for item in rows if item is not None)
    failures = sum(item.error_code is not None for item in completed)
    aggregate: Literal["ok", "partial_failure", "failed"]
    if not failures:
        aggregate = "ok"
    elif failures == len(completed):
        aggregate = "failed"
    else:
        aggregate = "partial_failure"
    return FleetResult(operation, aggregate, completed)
