"""Strict, project-agnostic fleet configuration and repository admission."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Literal
from uuid import UUID

from .locking import RepositoryAccess, RepositoryIdentity
from .manifest import (
    MAX_MANIFEST_BYTES,
    ManifestError,
    _load_one_yaml,
    inspect_init_journal,
    load_manifest_payload,
)


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
