"""Persist and verify the immutable source snapshot given to Graphify."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from .models import ProjectionFile, ProjectManifest
from .staging import StagedInput


_V1_FIELDS = frozenset(
    {"schema_version", "project_id", "graphify_version", "source_digest", "files"}
)
_V2_FIELDS = frozenset({
    "schema_version", "project_id", "project_uid", "graphify_version",
    "source_digest", "projection_digest", "files", "reason_counts",
})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class ReceiptError(ValueError):
    """Raised when a staging receipt or its bound source tree is untrusted."""


@dataclass(frozen=True)
class StagingReceipt:
    """Serializable identity for the exact source snapshot given to Graphify."""

    project_id: str
    graphify_version: str
    source_digest: str
    files: tuple[PurePosixPath, ...]
    schema_version: int = 1
    project_uid: str | None = None
    projection_digest: str | None = None
    reason_counts: tuple[tuple[str, int], ...] = ()


def write_staging_receipt(
    path: Path, staged: StagedInput, manifest: ProjectManifest
) -> StagingReceipt:
    """Exclusively write a private receipt without embedding local paths."""
    parent, parent_fd, parent_identity = _open_stable_directory(
        path.parent, "receipt parent"
    )
    created_identity: tuple[int, int] | None = None
    try:
        receipt, created_identity = _write_staging_receipt_at(
            parent_fd, path.name, staged, manifest
        )
        _require_directory_binding(parent, parent_identity)
        return receipt
    except BaseException:
        if created_identity is not None:
            try:
                _unlink_staging_receipt_at(
                    parent_fd, path.name, created_identity
                )
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                raise ReceiptError(
                    "staging receipt cleanup failed and requires manual recovery"
                ) from cleanup_error
        raise
    finally:
        os.close(parent_fd)


def _write_staging_receipt_at(
    parent_fd: int,
    name: str,
    staged: StagedInput,
    manifest: ProjectManifest,
) -> tuple[StagingReceipt, tuple[int, int]]:
    """Write through an already-bound parent directory descriptor."""
    if name in {"", ".", ".."} or "/" in name or "\\" in name:
        raise ReceiptError("staging receipt name is invalid")
    schema_version = 2 if manifest.schema_version == 2 else 1
    if schema_version == 2 and (
        manifest.project_uid is None or staged.projection_digest is None
    ):
        raise ReceiptError("staging receipt projection is required")
    receipt = StagingReceipt(
        project_id=manifest.project_id,
        graphify_version=manifest.graphify_version,
        source_digest=staged.source_digest,
        files=staged.files,
        schema_version=schema_version,
        project_uid=(None if manifest.project_uid is None else str(manifest.project_uid)),
        projection_digest=staged.projection_digest,
        reason_counts=staged.reason_counts,
    )
    document: dict[str, object] = {
        "schema_version": schema_version,
        "project_id": receipt.project_id,
        "graphify_version": receipt.graphify_version,
        "source_digest": receipt.source_digest,
        "files": [item.as_posix() for item in receipt.files],
    }
    if schema_version == 2:
        document.update({
            "project_uid": receipt.project_uid,
            "projection_digest": receipt.projection_digest,
            "reason_counts": dict(receipt.reason_counts),
        })
    payload = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor: int | None = None
    created_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag(),
            0o600,
            dir_fd=parent_fd,
        )
        created_identity = _inode_identity(os.fstat(descriptor))
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.fsync(parent_fd)
    except BaseException as error:
        if descriptor is not None:
            os.close(descriptor)
        cleanup_error: OSError | None = None
        if created_identity is not None:
            try:
                _unlink_staging_receipt_at(parent_fd, name, created_identity)
            except FileNotFoundError:
                cleanup_error = OSError("staging receipt disappeared during cleanup")
            except OSError as failure:
                cleanup_error = failure
        if cleanup_error is not None:
            raise ReceiptError(
                "staging receipt cleanup failed and requires manual recovery"
            ) from cleanup_error
        if isinstance(error, FileExistsError):
            raise ReceiptError("staging receipt already exists") from error
        if isinstance(error, OSError):
            raise ReceiptError("unable to create staging receipt") from error
        raise
    if created_identity is None:
        raise ReceiptError("unable to create staging receipt")
    return receipt, created_identity


def _unlink_staging_receipt_at(
    parent_fd: int, name: str, expected: tuple[int, int]
) -> None:
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode) or _inode_identity(current) != expected:
        raise OSError("staging receipt path changed during cleanup")
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def load_staging_receipt(path: Path, manifest: ProjectManifest) -> StagingReceipt:
    """Strictly load a receipt bound to the selected project contract."""
    try:
        payload = _read_regular_path(path).decode("utf-8")
        document = json.loads(payload, object_pairs_hook=_unique_object)
    except ReceiptError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReceiptError("staging receipt is invalid") from error
    if not isinstance(document, Mapping):
        raise ReceiptError("staging receipt schema is invalid")
    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise ReceiptError("staging receipt schema is invalid")
    if set(document) != (_V1_FIELDS if schema_version == 1 else _V2_FIELDS):
        raise ReceiptError("staging receipt schema is invalid")
    if document["project_id"] != manifest.project_id:
        raise ReceiptError("staging receipt project mismatch")
    if document["graphify_version"] != manifest.graphify_version:
        raise ReceiptError("staging receipt Graphify version mismatch")
    source_digest = document["source_digest"]
    if not isinstance(source_digest, str) or not _DIGEST.fullmatch(source_digest):
        raise ReceiptError("staging receipt source digest is invalid")
    raw_files = document["files"]
    if not isinstance(raw_files, list):
        raise ReceiptError("staging receipt files are invalid")
    files = tuple(_confined_path(item) for item in raw_files)
    if list(raw_files) != sorted(set(raw_files)):
        raise ReceiptError("staging receipt files must be sorted and unique")
    project_uid: str | None = None
    projection_digest: str | None = None
    reason_counts: tuple[tuple[str, int], ...] = ()
    if schema_version == 2:
        project_uid = document["project_uid"]
        if manifest.project_uid is None or project_uid != str(manifest.project_uid):
            raise ReceiptError("staging receipt project mismatch")
        projection_digest = document["projection_digest"]
        if type(projection_digest) is not str or _DIGEST.fullmatch(projection_digest) is None:
            raise ReceiptError("staging receipt projection digest is invalid")
        raw_counts = document["reason_counts"]
        if type(raw_counts) is not dict or any(
            type(name) is not str or type(count) is not int or count < 0
            for name, count in raw_counts.items()
        ):
            raise ReceiptError("staging receipt reason counts are invalid")
        if list(raw_counts) != sorted(raw_counts):
            raise ReceiptError("staging receipt reason counts are invalid")
        reason_counts = tuple(raw_counts.items())
    return StagingReceipt(
        project_id=manifest.project_id,
        graphify_version=manifest.graphify_version,
        source_digest=source_digest,
        files=files,
        schema_version=schema_version,
        project_uid=project_uid,
        projection_digest=projection_digest,
        reason_counts=reason_counts,
    )


def verify_staged_input(root: Path, receipt: StagingReceipt) -> StagedInput:
    """Recompute the staged tree and require an exact receipt match."""
    stage = _real_directory(root, "staged input")
    files = _capture_tree(stage)
    paths = tuple(path for path, _ in files)
    if paths != receipt.files:
        raise ReceiptError("staged input does not match receipt")
    if receipt.schema_version == 2:
        from .staging import _source_digest

        value = _source_digest(tuple(
            ProjectionFile(path, hashlib.sha256(payload).hexdigest(), len(payload))
            for path, payload in files
        ))
    else:
        digest = hashlib.sha256()
        for path, payload in files:
            digest.update(path.as_posix().encode("utf-8") + b"\0" + payload + b"\0")
        value = digest.hexdigest()
    if value != receipt.source_digest:
        raise ReceiptError("staged input does not match receipt")
    return StagedInput(
        root=stage, source_digest=value, files=paths,
        projection_digest=receipt.projection_digest,
        reason_counts=receipt.reason_counts,
    )


def _capture_tree(root: Path) -> tuple[tuple[PurePosixPath, bytes], ...]:
    descriptor = _open_directory(root)
    try:
        found = _capture_directory(descriptor, PurePosixPath())
    finally:
        os.close(descriptor)
    return tuple(sorted(found, key=lambda item: item[0].as_posix()))


def _capture_directory(
    descriptor: int, parent: PurePosixPath
) -> list[tuple[PurePosixPath, bytes]]:
    with os.scandir(os.dup(descriptor)) as entries:
        names = sorted(entry.name for entry in entries)
    found: list[tuple[PurePosixPath, bytes]] = []
    for name in names:
        relative = parent / name
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            child = os.open(name, _directory_flags(), dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if _identity(before) != _identity(opened):
                    raise ReceiptError("staged input changed during verification")
                found.extend(_capture_directory(child, relative))
                after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _identity(opened) != _identity(after):
                    raise ReceiptError("staged input changed during verification")
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(before.st_mode):
            raise ReceiptError("staged input must contain only regular files")
        file_descriptor = os.open(
            name, os.O_RDONLY | _nofollow_flag(), dir_fd=descriptor
        )
        try:
            opened = os.fstat(file_descriptor)
            if _identity(before) != _identity(opened):
                raise ReceiptError("staged input changed during verification")
            chunks: list[bytes] = []
            while chunk := os.read(file_descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if _identity(opened) != _identity(after):
                raise ReceiptError("staged input changed during verification")
        finally:
            os.close(file_descriptor)
        found.append((relative, b"".join(chunks)))
    return found


def _confined_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ReceiptError("staging receipt files are invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReceiptError("staging receipt files are invalid")
    return path


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError("duplicate JSON key in staging receipt")
        result[key] = value
    return result


def _read_regular_path(path: Path) -> bytes:
    parent = _real_directory(path.parent, "receipt parent")
    parent_fd = _open_directory(parent)
    try:
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise ReceiptError("staging receipt must be a regular file")
        descriptor = os.open(
            path.name, os.O_RDONLY | _nofollow_flag(), dir_fd=parent_fd
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
                raise ReceiptError("staging receipt changed during read")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if _identity(opened) != _identity(after):
                raise ReceiptError("staging receipt changed during read")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except ReceiptError:
        raise
    except OSError as error:
        raise ReceiptError("staging receipt is invalid") from error
    finally:
        os.close(parent_fd)


def _real_directory(path: Path, description: str) -> Path:
    absolute, descriptor, _ = _open_stable_directory(path, description)
    os.close(descriptor)
    return absolute


def _open_stable_directory(
    path: Path, description: str
) -> tuple[Path, int, tuple[int, int]]:
    absolute = path.absolute()
    try:
        info = absolute.stat(follow_symlinks=False)
    except OSError as error:
        raise ReceiptError(f"{description} is unavailable") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReceiptError(f"{description} must be a real directory")
    descriptor = _open_directory(absolute)
    try:
        opened = os.fstat(descriptor)
        after = absolute.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _inode_identity(info) != _inode_identity(opened)
            or _inode_identity(opened) != _inode_identity(after)
        ):
            raise ReceiptError(f"{description} changed during open")
        return absolute, descriptor, _inode_identity(opened)
    except BaseException:
        os.close(descriptor)
        raise


def _require_directory_binding(path: Path, expected: tuple[int, int]) -> None:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ReceiptError("receipt parent changed during write") from error
    if not stat.S_ISDIR(current.st_mode) or _inode_identity(current) != expected:
        raise ReceiptError("receipt parent changed during write")


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, _directory_flags())
    except OSError as error:
        raise ReceiptError("unable to open staged directory safely") from error


def _directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    except AttributeError as error:
        raise ReceiptError("platform lacks safe directory operations") from error


def _nofollow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as error:
        raise ReceiptError("platform lacks safe file operations") from error


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev


def _inode_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino
