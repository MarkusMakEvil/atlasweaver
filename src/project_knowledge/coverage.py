"""Tracked, byte-bound approvals for known extraction coverage omissions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Literal
from uuid import UUID

from .compatibility import GraphifyCompatibility, resolve_graphify_compatibility
from .locking import (
    RepositoryAccess,
    RepositoryIdentity,
    TransactionLockError,
    open_repository_access,
    repository_lifecycle_lock,
)
from .manifest import (
    ManifestError,
    _load_one_yaml,
    assert_current_manifest_unchanged,
    require_current_manifest,
)
from .models import CoverageApproval, ProjectManifest, ProjectionFile, ProjectionSnapshot


COVERAGE_FILE = PurePosixPath(".atlasweaver-coverage.yaml")
MAX_COVERAGE_BYTES = 262_144
_FIELDS = frozenset({"schema_version", "approvals"})
_ENTRY_FIELDS = frozenset({
    "path", "content_sha256", "adapter_id", "reason_code", "rationale"
})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class CoverageError(ValueError):
    """Stable invalid-coverage contract."""


@dataclass(frozen=True)
class CoveragePreview:
    payload: bytes
    previous_sha256: str | None
    previous_identity: tuple[int, int] | None
    approval: CoverageApproval
    repository_identity: RepositoryIdentity
    project_uid: UUID
    requested_path: PurePosixPath
    requested_reason_code: str
    requested_rationale: str
    manifest: ProjectManifest


def load_coverage_approvals(
    repo_root: Path,
    projection: ProjectionSnapshot,
    contract: GraphifyCompatibility,
    *,
    repository_access: RepositoryAccess | None = None,
) -> tuple[CoverageApproval, ...]:
    """Load the exact current control file without calling projection again."""
    with _access(repo_root, repository_access) as repository:
        payload, _ = _read_control(repository.descriptor)
    if payload is None:
        if projection.coverage_digest is not None:
            raise CoverageError("coverage approval file changed during read")
        return ()
    if hashlib.sha256(payload).hexdigest() != projection.coverage_digest:
        raise CoverageError("coverage approval file changed during read")
    from .secrets_scan import scan_payload

    if scan_payload(COVERAGE_FILE, payload):
        raise CoverageError("coverage approval file contains secret-shaped material")
    try:
        document = _load_one_yaml(payload)
    except ManifestError:
        raise CoverageError("coverage approval schema is invalid") from None
    if type(document) is not dict or set(document) != _FIELDS:
        raise CoverageError("coverage approval schema is invalid")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise CoverageError("coverage approval schema is invalid")
    entries = document["approvals"]
    if type(entries) is not list:
        raise CoverageError("coverage approval schema is invalid")
    safe_files = {item.path: item for item in projection.files}
    approvals = tuple(
        _validate_approval(entry, safe_files=safe_files, contract=contract)
        for entry in entries
    )
    if tuple(item.path for item in approvals) != tuple(sorted(
        {item.path for item in approvals}, key=lambda path: path.as_posix().encode("utf-8")
    )):
        raise CoverageError("coverage approvals must be sorted and unique")
    return approvals


def preview_coverage_approval(
    repo_root: Path,
    manifest: ProjectManifest,
    path: PurePosixPath,
    reason_code: str,
    rationale: str,
    *,
    repository_access: RepositoryAccess | None = None,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> CoveragePreview:
    if repository_access is not None and expected_repository_identity is not None:
        raise CoverageError("coverage authority is invalid")
    if manifest.schema_version != 2 or manifest.project_uid is None:
        raise CoverageError("coverage approval requires manifest schema 2")
    contract = resolve_graphify_compatibility(manifest.graphify_version)
    if reason_code not in contract.coverage_reason_codes:
        raise CoverageError("coverage approval adapter or reason is invalid")
    rationale = _nonempty(rationale)
    requested = _exact_file_path(
        path.as_posix() if type(path) is PurePosixPath else path
    )
    with _access(
        repo_root, repository_access,
        expected_repository_identity=expected_repository_identity,
    ) as repository:
        try:
            require_current_manifest(
                repo_root, manifest, repository_access=repository
            )
        except ManifestError as error:
            raise CoverageError("coverage approval manifest changed") from error
        from .staging import _inspect_projection_base

        projection = _inspect_projection_base(
            repo_root, manifest, repository_access=repository
        )
        current = {item.path: item for item in projection.files}.get(requested)
        if current is None:
            raise CoverageError("coverage approval must name a current safe file")
        approval = CoverageApproval(
            requested, current.sha256, contract.adapter_id, reason_code, rationale
        )
        prior, identity = _read_control(repository.descriptor)
        existing = load_coverage_approvals(
            repo_root, projection, contract, repository_access=repository
        )
        by_path = {item.path: item for item in existing}
        by_path[requested] = approval
        payload = _render(tuple(
            by_path[key] for key in sorted(
                by_path, key=lambda item: item.as_posix().encode("utf-8")
            )
        ))
        try:
            assert_current_manifest_unchanged(
                repo_root, manifest, repository_access=repository
            )
        except ManifestError as error:
            raise CoverageError("coverage approval manifest changed") from error
        return CoveragePreview(
            payload=payload,
            previous_sha256=None if prior is None else hashlib.sha256(prior).hexdigest(),
            previous_identity=identity,
            approval=approval,
            repository_identity=repository.identity,
            project_uid=manifest.project_uid,
            requested_path=requested,
            requested_reason_code=reason_code,
            requested_rationale=rationale,
            manifest=manifest,
        )


def apply_coverage_approval(
    repo_root: Path,
    preview: CoveragePreview,
) -> Literal[
    "approved", "unchanged", "coverage_conflict", "coverage_recovery_required"
]:
    if type(preview) is not CoveragePreview:
        raise CoverageError("coverage preview is invalid")
    try:
        with repository_lifecycle_lock(
            repo_root,
            expected_repository_identity=preview.repository_identity,
        ), open_repository_access(
            repo_root,
            expected_repository_identity=preview.repository_identity,
        ) as repository:
            current = preview_coverage_approval(
                repo_root, preview.manifest, preview.requested_path,
                preview.requested_reason_code, preview.requested_rationale,
                repository_access=repository,
            )
            if current != preview:
                return "coverage_conflict"
            existing, existing_identity = _read_control(repository.descriptor)
            if existing == preview.payload:
                return "unchanged"
            if (
                (None if existing is None else hashlib.sha256(existing).hexdigest())
                != preview.previous_sha256
                or existing_identity != preview.previous_identity
            ):
                return "coverage_conflict"
            if _transaction_exists(repository.descriptor):
                return "coverage_recovery_required"
            _write_control(
                repository.descriptor,
                preview.payload,
                previous_sha256=preview.previous_sha256,
                previous_identity=preview.previous_identity,
            )
            return "approved"
    except CoverageError:
        return "coverage_conflict"
    except TransactionLockError as error:
        if error.kind == "authority":
            return "coverage_conflict"
        return "coverage_recovery_required"
    except Exception:
        return "coverage_recovery_required"


def omission_is_approved(
    path: PurePosixPath,
    reason_code: str,
    staged,
    adapter_id: str,
) -> bool:
    safe_hashes = {item.path: item.sha256 for item in staged.projection_files}
    return any(
        approval.path == path
        and approval.reason_code == reason_code
        and approval.adapter_id == adapter_id
        and approval.content_sha256 == safe_hashes.get(path)
        for approval in staged.coverage_approvals
    )


def _validate_approval(
    entry: Mapping[str, object],
    *,
    safe_files: Mapping[PurePosixPath, ProjectionFile],
    contract: GraphifyCompatibility,
) -> CoverageApproval:
    if type(entry) is not dict or set(entry) != _ENTRY_FIELDS:
        raise CoverageError("coverage approval schema is invalid")
    path = _exact_file_path(entry["path"])
    current = safe_files.get(path)
    if current is None:
        raise CoverageError("coverage approval must name a current safe file")
    adapter_id = _nonempty(entry["adapter_id"])
    reason_code = _nonempty(entry["reason_code"])
    rationale = _nonempty(entry["rationale"])
    if adapter_id != contract.adapter_id or reason_code not in contract.coverage_reason_codes:
        raise CoverageError("coverage approval adapter or reason is invalid")
    if entry["content_sha256"] != current.sha256:
        raise CoverageError("coverage approval does not match current content")
    return CoverageApproval(path, current.sha256, adapter_id, reason_code, rationale)


def _exact_file_path(value: object) -> PurePosixPath:
    if type(value) is not str or not value or "\\" in value or any(
        character in value for character in "*?[]"
    ):
        raise CoverageError("coverage approval path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CoverageError("coverage approval path is invalid")
    return path


def _nonempty(value: object) -> str:
    if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > 4096:
        raise CoverageError("coverage approval value is invalid")
    return value


def _render(approvals: tuple[CoverageApproval, ...]) -> bytes:
    document = {
        "schema_version": 1,
        "approvals": [
            {
                "path": item.path.as_posix(),
                "content_sha256": item.content_sha256,
                "adapter_id": item.adapter_id,
                "reason_code": item.reason_code,
                "rationale": item.rationale,
            }
            for item in approvals
        ],
    }
    lines = ["schema_version: 1", "approvals:"]
    for item in document["approvals"]:
        assert isinstance(item, dict)
        lines.extend((
            f"- path: {json.dumps(item['path'], ensure_ascii=False)}",
            f"  content_sha256: {json.dumps(item['content_sha256'])}",
            f"  adapter_id: {json.dumps(item['adapter_id'])}",
            f"  reason_code: {json.dumps(item['reason_code'])}",
            f"  rationale: {json.dumps(item['rationale'], ensure_ascii=False)}",
        ))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    if len(payload) > MAX_COVERAGE_BYTES:
        raise CoverageError("coverage approval file is too large")
    from .secrets_scan import scan_payload

    if scan_payload(COVERAGE_FILE, payload):
        raise CoverageError("coverage approval file contains secret-shaped material")
    return payload


def _read_control(root_fd: int) -> tuple[bytes | None, tuple[int, int] | None]:
    try:
        before = os.stat(COVERAGE_FILE.name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_COVERAGE_BYTES:
        raise CoverageError("coverage approval file is invalid")
    descriptor = os.open(
        COVERAGE_FILE.name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=root_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise CoverageError("coverage approval file changed during read")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(65_536, MAX_COVERAGE_BYTES + 1 - total)):
            total += len(chunk)
            if total > MAX_COVERAGE_BYTES:
                raise CoverageError("coverage approval file is too large")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if after.st_size != opened.st_size or after.st_mtime_ns != opened.st_mtime_ns:
            raise CoverageError("coverage approval file changed during read")
        return b"".join(chunks), (opened.st_dev, opened.st_ino)
    finally:
        os.close(descriptor)


_JOURNAL = ".atlasweaver-coverage.transaction.json"
_BACKUP = ".atlasweaver-coverage.backup"


def _transaction_exists(root_fd: int) -> bool:
    return any(_entry_exists(root_fd, name) for name in (_JOURNAL, _BACKUP))


def _entry_exists(root_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _write_control(
    root_fd: int,
    payload: bytes,
    *,
    previous_sha256: str | None,
    previous_identity: tuple[int, int] | None,
) -> None:
    name = f".atlasweaver-coverage.{os.getpid()}.{os.urandom(8).hex()}.new"
    journal = json.dumps(
        {
            "schema_version": 1,
            "previous_sha256": previous_sha256,
            "new_sha256": hashlib.sha256(payload).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    _write_new(root_fd, _JOURNAL, journal)
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=root_fd,
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    backup_created = False
    try:
        current, identity = _read_control(root_fd)
        current_sha256 = None if current is None else hashlib.sha256(current).hexdigest()
        if current_sha256 != previous_sha256 or identity != previous_identity:
            raise CoverageError("coverage approval file changed during write")
        if previous_identity is not None:
            os.link(
                COVERAGE_FILE.name,
                _BACKUP,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
                follow_symlinks=False,
            )
            backup_created = True
            backup = os.stat(_BACKUP, dir_fd=root_fd, follow_symlinks=False)
            rebound = os.stat(
                COVERAGE_FILE.name, dir_fd=root_fd, follow_symlinks=False
            )
            if (
                (backup.st_dev, backup.st_ino) != previous_identity
                or (rebound.st_dev, rebound.st_ino) != previous_identity
            ):
                raise CoverageError("coverage approval file changed during write")
        os.rename(name, COVERAGE_FILE.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
        if backup_created:
            os.unlink(_BACKUP, dir_fd=root_fd)
        os.unlink(_JOURNAL, dir_fd=root_fd)
        os.fsync(root_fd)
    except BaseException:
        try:
            os.unlink(name, dir_fd=root_fd)
        except OSError:
            pass
        raise


def _write_new(root_fd: int, name: str, payload: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=root_fd,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(root_fd)


class _BorrowedAccess:
    def __init__(self, access: RepositoryAccess) -> None:
        self.access = access

    def __enter__(self) -> RepositoryAccess:
        return self.access

    def __exit__(self, *args: object) -> None:
        return None


def _access(
    repo_root: Path,
    access: RepositoryAccess | None,
    *,
    expected_repository_identity: RepositoryIdentity | None = None,
):
    if access is not None:
        return _BorrowedAccess(access)
    return open_repository_access(
        repo_root,
        expected_repository_identity=expected_repository_identity,
    )
