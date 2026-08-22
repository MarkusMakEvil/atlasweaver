"""Create a private, immutable projection of files safe for Graphify."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import re

from .models import ProjectManifest
from .privacy import effective_excludes, is_denied, repository_ignores


class StagingError(ValueError):
    """Raised when a private projection cannot be created safely."""


class SourceChangedError(StagingError):
    """Raised when a source file changes while its projection is being made."""


class SecretShapeError(StagingError):
    """Raised when high-confidence secret material appears in an allowed file."""


class StagingCleanupError(StagingError):
    """Raised when a failed projection remains and needs explicit recovery."""


_SECRET_SHAPES = (
    re.compile(rb"(?<![0-9])[0-9]{8,10}:[A-Za-z0-9_-]{30,}"),
    re.compile(
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s*[\r\n]+[A-Za-z0-9+/=]{32,}"
    ),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(
        rb"(?i)\b(?:password|passwd|client_secret|aws_secret_access_key|"
        rb"access_token|api_key)\b\s*[:=]\s*[\"']?[^\s\"']{16,}"
    ),
    re.compile(rb"(?i)\bauthorization\b\s*[:=]\s*[\"']?Bearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(rb"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]{12,}@[^\s/]+"),
)


@dataclass(frozen=True)
class StagedInput:
    """The complete immutable input set made available to Graphify."""

    root: Path
    source_digest: str
    files: tuple[PurePosixPath, ...]


def stage_input(
    repo_root: Path, manifest: ProjectManifest, destination: Path
) -> StagedInput:
    """Copy the manifest's safe, regular source files into a private directory.

    The destination is created from scratch so no output can be mistaken for a
    source.  A failure removes it, preventing a later process from using a
    partial projection.
    """
    root = repo_root.absolute()
    _require_directory(root, "repository root")
    staged_root = _resolved_destination(destination)
    _reject_destination_in_inputs(root, manifest, staged_root)

    staged_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(staged_root, 0o700)
    try:
        digest = hashlib.sha256()
        copied: list[PurePosixPath] = []
        root_descriptor = _open_directory(root)
        try:
            for relative in sorted(set(iter_safe_files(root, manifest))):
                before = _stat_relative(root_descriptor, relative)
                if not stat.S_ISREG(before.st_mode):
                    continue
                payload = _read_regular_file(relative, root_descriptor, before)
                _reject_secret_shapes(payload)
                try:
                    after = _stat_relative(root_descriptor, relative)
                except FileNotFoundError as error:
                    raise SourceChangedError(relative.as_posix()) from error
                if _identity(before) != _identity(after):
                    raise SourceChangedError(relative.as_posix())
                _write_private_file(staged_root, relative, payload)
                digest.update(
                    relative.as_posix().encode("utf-8") + b"\0" + payload + b"\0"
                )
                copied.append(relative)
        finally:
            os.close(root_descriptor)
        return StagedInput(staged_root, digest.hexdigest(), tuple(copied))
    except BaseException as error:
        try:
            _remove_incomplete_staging(staged_root)
        except OSError as cleanup_error:
            raise StagingCleanupError(
                "safe input staging cleanup failed; destination requires manual recovery"
            ) from cleanup_error
        raise


def iter_safe_files(repo_root: Path, manifest: ProjectManifest):
    """Yield safe regular files below manifest roots without descending links.

    Git directories and gitfile-based submodules are repository boundaries;
    their entire tree is deliberately omitted.
    """
    excludes = effective_excludes(manifest)
    ignored = repository_ignores(repo_root)
    root = repo_root.absolute()
    root_descriptor = _open_directory(root)
    try:
        for include_root in manifest.include_roots:
            yield from _walk_safe_files(
                root_descriptor, include_root.parts, PurePosixPath(), excludes, ignored
            )
    finally:
        os.close(root_descriptor)


def _walk_safe_files(
    parent_descriptor: int,
    parts: tuple[str, ...],
    parent_relative: PurePosixPath,
    excludes: tuple[str, ...],
    ignored,
):
    if not parts:
        return
    name, *remaining = parts
    relative = parent_relative / name
    try:
        info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        is_denied(relative, excludes)
        or ignored(relative, is_directory=stat.S_ISDIR(info.st_mode))
        or stat.S_ISLNK(info.st_mode)
    ):
        return
    if remaining:
        if not stat.S_ISDIR(info.st_mode):
            return
        directory_descriptor = _open_directory_at(parent_descriptor, name)
        try:
            if _is_nested_git_repository(directory_descriptor):
                return
            yield from _walk_safe_files(
                directory_descriptor, tuple(remaining), relative, excludes, ignored
            )
        finally:
            os.close(directory_descriptor)
        return
    if stat.S_ISREG(info.st_mode):
        yield relative
        return
    if not stat.S_ISDIR(info.st_mode):
        return

    directory_descriptor = _open_directory_at(parent_descriptor, name)
    try:
        if _is_nested_git_repository(directory_descriptor):
            return
        with os.scandir(os.dup(directory_descriptor)) as entries:
            children = sorted(entry.name for entry in entries)
        for child_name in children:
            yield from _walk_safe_files(
                directory_descriptor, (child_name,), relative, excludes, ignored
            )
    finally:
        os.close(directory_descriptor)


def _is_nested_git_repository(directory_descriptor: int) -> bool:
    """Return whether a directory has a nested repository marker."""
    try:
        os.stat(".git", dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _read_regular_file(
    relative: PurePosixPath, root_descriptor: int, before: os.stat_result
) -> bytes:
    """Read a previously-lstat regular file without following a final symlink."""
    parent_descriptor = _open_parent_directory(root_descriptor, relative)
    try:
        descriptor = os.open(
            relative.name, os.O_RDONLY | _nofollow_flag(), dir_fd=parent_descriptor
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
                raise SourceChangedError(relative.as_posix())
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _stat_relative(root_descriptor: int, relative: PurePosixPath) -> os.stat_result:
    parent_descriptor = _open_parent_directory(root_descriptor, relative)
    try:
        return os.stat(relative.name, dir_fd=parent_descriptor, follow_symlinks=False)
    finally:
        os.close(parent_descriptor)


def _open_parent_directory(root_descriptor: int, relative: PurePosixPath) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for component in relative.parts[:-1]:
            child_descriptor = _open_directory_at(descriptor, component)
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory(path: Path) -> int:
    descriptor = os.open(path, _directory_flags())
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise StagingError("repository root must be a directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_at(parent_descriptor: int, name: str) -> int:
    return os.open(name, _directory_flags(), dir_fd=parent_descriptor)


def _directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    except AttributeError as error:
        raise StagingError("platform cannot safely open directories without following links") from error


def _nofollow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as error:
        raise StagingError("platform cannot safely open files without following links") from error


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev)


def _write_private_file(staged_root: Path, relative: PurePosixPath, payload: bytes) -> None:
    target = staged_root / relative
    parent = staged_root
    for component in relative.parts[:-1]:
        parent = parent / component
        parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
    with target.open("xb") as staged_file:
        os.chmod(target, 0o600)
        staged_file.write(payload)


def _resolved_destination(destination: Path) -> Path:
    """Resolve existing parents while retaining a non-existent destination leaf."""
    return destination.parent.resolve() / destination.name


def _reject_destination_in_inputs(
    repo_root: Path, manifest: ProjectManifest, destination: Path
) -> None:
    for include_root in manifest.include_roots:
        included_path = (repo_root.joinpath(*include_root.parts)).resolve()
        try:
            destination.relative_to(included_path)
        except ValueError:
            continue
        raise StagingError("destination must not be inside an include root")


def _require_directory(path: Path, description: str) -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise StagingError(f"{description} does not exist") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise StagingError(f"{description} must be a directory, not a symlink")


def _remove_incomplete_staging(staged_root: Path) -> None:
    shutil.rmtree(staged_root)


def _reject_secret_shapes(payload: bytes) -> None:
    if any(pattern.search(payload) for pattern in _SECRET_SHAPES):
        raise SecretShapeError("safe input contains secret-shaped material")
