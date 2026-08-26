"""Create a private, immutable projection of files safe for Graphify."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat

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


@dataclass(frozen=True)
class StagedInput:
    """The complete immutable input set made available to Graphify."""

    root: Path
    source_digest: str
    files: tuple[PurePosixPath, ...]
    projection_digest: str | None = None
    reason_counts: tuple[tuple[str, int], ...] = ()
    coverage_approvals: tuple[str, ...] = ()


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
    from .secrets_scan import (
        SecretExceptionError,
        load_secret_exceptions,
        scan_repository,
    )

    try:
        scanned = scan_repository(root, manifest)
        accepted = load_secret_exceptions(root, scanned)
    except SecretExceptionError as error:
        raise SecretShapeError("secret exception policy is invalid") from error
    if any(item.fingerprint not in accepted for item in scanned):
        raise SecretShapeError("safe input contains secret-shaped material")

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
                _reject_secret_shapes(payload, relative, accepted)
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


def stage_input_with_receipt(
    repo_root: Path,
    manifest: ProjectManifest,
    destination: Path,
    receipt_path: Path,
) -> StagedInput:
    """Create one staged snapshot and its out-of-tree provenance receipt."""
    from .receipt import (
        ReceiptError,
        _open_stable_directory as _open_receipt_directory,
        _require_directory_binding,
        _unlink_staging_receipt_at,
        _write_staging_receipt_at,
    )

    staged_root = _resolved_destination(destination)
    receipt = receipt_path.parent.absolute() / receipt_path.name
    try:
        receipt.relative_to(staged_root)
    except ValueError:
        pass
    else:
        raise StagingError("staging receipt must be outside staged input")

    try:
        receipt_parent, receipt_parent_fd, receipt_parent_identity = (
            _open_receipt_directory(receipt_path.parent, "receipt parent")
        )
    except ReceiptError as error:
        raise StagingError("staging receipt creation failed") from error

    staged: StagedInput | None = None
    staged_fd: int | None = None
    staged_identity: tuple[int, int] | None = None
    receipt_identity: tuple[int, int] | None = None
    try:
        staged = stage_input(repo_root, manifest, destination)
        staged_path, staged_fd, staged_identity = _open_receipt_directory(
            staged.root, "staged input"
        )
        _require_directory_binding(receipt_parent, receipt_parent_identity)
        _require_directory_binding(staged_path, staged_identity)
        if receipt_parent_identity == staged_identity:
            raise ReceiptError("staging receipt parent overlaps staged input")
        _, receipt_identity = _write_staging_receipt_at(
            receipt_parent_fd,
            receipt_path.name,
            staged,
            manifest,
        )
        _require_directory_binding(receipt_parent, receipt_parent_identity)
        _require_directory_binding(staged_path, staged_identity)
        return staged
    except BaseException as error:
        cleanup_error: OSError | ReceiptError | None = None
        if receipt_identity is not None:
            try:
                _unlink_staging_receipt_at(
                    receipt_parent_fd,
                    receipt_path.name,
                    receipt_identity,
                )
            except FileNotFoundError:
                pass
            except (OSError, ReceiptError) as failure:
                cleanup_error = failure
        if staged is not None:
            try:
                if staged_identity is not None:
                    _require_directory_binding(staged.root, staged_identity)
                _remove_incomplete_staging(staged.root)
            except (OSError, ReceiptError) as failure:
                cleanup_error = cleanup_error or failure
        if cleanup_error is not None:
            raise StagingCleanupError(
                "safe input staging cleanup failed; destination requires manual recovery"
            ) from cleanup_error
        if isinstance(error, ReceiptError):
            raise StagingError("staging receipt creation failed") from error
        raise
    finally:
        if staged_fd is not None:
            os.close(staged_fd)
        os.close(receipt_parent_fd)


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


def _reject_secret_shapes(
    payload: bytes,
    path: PurePosixPath = PurePosixPath("source"),
    accepted: frozenset[str] = frozenset(),
) -> None:
    from .secrets_scan import scan_payload

    if any(
        finding.fingerprint not in accepted
        for finding in scan_payload(path, payload)
    ):
        raise SecretShapeError("safe input contains secret-shaped material")
