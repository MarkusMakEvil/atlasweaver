"""Closed, content-free local operation records and CI summaries."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any

from .locking import (
    ExclusiveDescriptorLock,
    RepositoryAccess,
    RepositoryIdentity,
    TransactionLockError,
    assert_lifecycle_lock_held,
    capture_lifecycle_descriptors,
    capture_lifecycle_repository,
    open_repository_access,
    repository_lifecycle_lock,
)


STATE_SCHEMA_VERSION = 1
MAX_STATE_BYTES = 1_048_576
MAX_SUMMARY_INPUT_BYTES = 1_048_576
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_OPERATIONS = frozenset({
    "refresh", "artifact_pack", "artifact_install", "artifact_pull",
    "registry_sync", "fleet_refresh", "fleet_pull",
})
_FORBIDDEN_SUMMARY_KEY = re.compile(
    r"(?:path|file|query|environment|fingerprint|message)", re.IGNORECASE
)
_SUMMARY_KEYS = frozenset({
    "schema_version", "project_id", "project_uid", "operation", "status",
    "represented", "approved_omissions", "denied", "safe_file_count",
    "duration_ms", "source_digest", "projection_digest", "graph_digest",
    "generation_digest", "build_epoch", "artifact_channel", "channel",
    "trust", "limitations", "backend", "model", "deep",
    "credential_bound", "core_status", "counts",
})


class OperationStateError(RuntimeError):
    """Stable operation-state failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OperationStateFileSystem:
    """Named fault checkpoints around otherwise real descriptor operations."""

    def checkpoint(self, operation: str) -> None:
        del operation


REAL_OPERATION_STATE_FS = OperationStateFileSystem()


@dataclass(frozen=True)
class CoverageMetrics:
    represented: int
    approved_omissions: int
    denied: int

    def __post_init__(self) -> None:
        for value in (self.represented, self.approved_omissions, self.denied):
            if type(value) is not int or value < 0:
                raise OperationStateError("state_invalid")


@dataclass(frozen=True)
class SuccessfulOperation:
    operation: str
    duration_ms: int
    safe_file_count: int
    coverage: CoverageMetrics
    source_digest: str
    projection_digest: str
    graph_digest: str | None
    generation_digest: str | None
    build_epoch: int | None
    artifact_channel: str | None

    def __post_init__(self) -> None:
        if self.operation not in _OPERATIONS:
            raise OperationStateError("state_invalid")
        if type(self.duration_ms) is not int or self.duration_ms < 0:
            raise OperationStateError("state_invalid")
        if type(self.safe_file_count) is not int or self.safe_file_count < 0:
            raise OperationStateError("state_invalid")
        for value in (self.source_digest, self.projection_digest):
            _require_digest(value)
        if (self.graph_digest is None) != (self.generation_digest is None):
            raise OperationStateError("state_invalid")
        if self.operation in {
            "artifact_pack", "artifact_install", "artifact_pull"
        } and self.graph_digest is None:
            raise OperationStateError("state_invalid")
        if self.graph_digest is not None:
            _require_digest(self.graph_digest)
            _require_digest(self.generation_digest)
            if type(self.build_epoch) is not int or self.build_epoch <= 0:
                raise OperationStateError("state_invalid")
        elif self.build_epoch is not None:
            raise OperationStateError("state_invalid")
        if self.artifact_channel is not None and (
            type(self.artifact_channel) is not str
            or _NAME.fullmatch(self.artifact_channel) is None
        ):
            raise OperationStateError("state_invalid")


@dataclass(frozen=True)
class FailureRecord:
    operation: str
    failure_code: str

    def __post_init__(self) -> None:
        if self.operation not in _OPERATIONS or type(self.failure_code) is not str:
            raise OperationStateError("state_invalid")
        if _NAME.fullmatch(self.failure_code) is None:
            raise OperationStateError("state_invalid")


@dataclass(frozen=True)
class OperationState:
    schema_version: int = STATE_SCHEMA_VERSION
    last_success: SuccessfulOperation | None = None
    last_failure: FailureRecord | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise OperationStateError("state_invalid")


def record_success(
    repo_root: Path,
    operation: SuccessfulOperation,
    *,
    repository_access: RepositoryAccess | None = None,
    expected_repository_identity: RepositoryIdentity | None = None,
    fs: OperationStateFileSystem = REAL_OPERATION_STATE_FS,
) -> None:
    _record_update(
        repo_root,
        success=operation,
        failure=None,
        repository_access=repository_access,
        expected_repository_identity=expected_repository_identity,
        fs=fs,
    )


def record_failure(
    repo_root: Path,
    failure: FailureRecord,
    *,
    repository_access: RepositoryAccess | None = None,
    expected_repository_identity: RepositoryIdentity | None = None,
    fs: OperationStateFileSystem = REAL_OPERATION_STATE_FS,
) -> None:
    _record_update(
        repo_root,
        success=None,
        failure=failure,
        repository_access=repository_access,
        expected_repository_identity=expected_repository_identity,
        fs=fs,
    )


def load_operation_state(
    repo_root: Path,
    *,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> OperationState:
    try:
        with open_repository_access(
            repo_root,
            expected_repository_identity=expected_repository_identity,
        ) as repository:
            try:
                state_fd = os.open(
                    ".project-knowledge",
                    os.O_RDONLY | os.O_DIRECTORY | _nofollow_flag(),
                    dir_fd=repository.descriptor,
                )
            except FileNotFoundError:
                return OperationState()
            try:
                return _load_state_at(state_fd)
            finally:
                os.close(state_fd)
    except OperationStateError:
        raise
    except TransactionLockError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise OperationStateError("state_invalid") from None


def render_ci_summary(
    results: Sequence[dict[str, object]],
) -> tuple[bytes, str]:
    if isinstance(results, (str, bytes)):
        raise OperationStateError("summary_invalid")
    projected = [_project_summary(item) for item in results]
    machine = _canonical_json({"schema_version": 1, "results": projected})
    lines = ["# AtlasWeaver summary", ""]
    for item in projected:
        project = item.get("project_id", item.get("project_uid", "project"))
        status = item.get("status", item.get("core_status", "unknown"))
        operation = item.get("operation", "check")
        lines.append(f"- {project}: {operation} — {status}")
    return machine, "\n".join(lines) + "\n"


def _project_summary(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise OperationStateError("summary_invalid")
    projected: dict[str, object] = {}
    for key, item in value.items():
        if type(key) is not str or _FORBIDDEN_SUMMARY_KEY.search(key):
            raise OperationStateError("summary_invalid")
        if key not in _SUMMARY_KEYS:
            continue
        projected[key] = _closed_summary_value(key, item)
    return projected


def _closed_summary_value(key: str, value: object) -> object:
    if value is None or type(value) in {str, bool, int}:
        if type(value) is int and value < 0:
            raise OperationStateError("summary_invalid")
        if type(value) is str and len(value.encode("utf-8")) > 4096:
            raise OperationStateError("summary_invalid")
        return value
    if key in {"limitations"} and type(value) in {list, tuple}:
        if len(value) > 64 or any(type(item) is not str for item in value):
            raise OperationStateError("summary_invalid")
        return list(value)
    if key in {"counts"} and type(value) is dict:
        if any(type(name) is not str or type(count) is not int or count < 0
               for name, count in value.items()):
            raise OperationStateError("summary_invalid")
        return dict(sorted(value.items()))
    raise OperationStateError("summary_invalid")


def _record_update(
    repo_root: Path,
    *,
    success: SuccessfulOperation | None,
    failure: FailureRecord | None,
    repository_access: RepositoryAccess | None,
    expected_repository_identity: RepositoryIdentity | None,
    fs: OperationStateFileSystem,
) -> None:
    if repository_access is not None and expected_repository_identity is not None:
        raise OperationStateError("state_authority_invalid")
    if not hasattr(fs, "checkpoint"):
        raise OperationStateError("state_write_failed")
    if repository_access is None:
        try:
            with repository_lifecycle_lock(
                repo_root,
                expected_repository_identity=expected_repository_identity,
            ):
                with capture_lifecycle_repository(repo_root) as captured:
                    _record_update_locked(
                        repo_root,
                        captured,
                        success=success,
                        failure=failure,
                        fs=fs,
                    )
        except OperationStateError:
            raise
        except TransactionLockError:
            raise
        return
    try:
        assert_lifecycle_lock_held(repo_root)
        _record_update_locked(
            repo_root,
            repository_access,
            success=success,
            failure=failure,
            fs=fs,
        )
    except OperationStateError:
        raise
    except TransactionLockError:
        raise OperationStateError("state_authority_invalid") from None


def _record_update_locked(
    repo_root: Path,
    repository: RepositoryAccess,
    *,
    success: SuccessfulOperation | None,
    failure: FailureRecord | None,
    fs: OperationStateFileSystem,
) -> None:
    try:
        with capture_lifecycle_descriptors(repo_root) as descriptors:
            opened = os.fstat(repository.descriptor)
            if (
                repository.identity != descriptors.repository_identity
                or (opened.st_dev, opened.st_ino) != descriptors.repository_identity
            ):
                raise OperationStateError("state_authority_invalid")
            with ExclusiveDescriptorLock(descriptors.state_descriptor, "state.lock"):
                current = _load_state_at(descriptors.state_descriptor)
                updated = OperationState(
                    last_success=(success if success is not None else current.last_success),
                    last_failure=(failure if failure is not None else current.last_failure),
                )
                _write_state_at(descriptors.state_descriptor, updated, fs)
    except OperationStateError:
        raise
    except (OSError, TransactionLockError):
        raise OperationStateError("state_write_failed") from None


def _load_state_at(state_fd: int) -> OperationState:
    try:
        payload = _read_regular_at(state_fd, "state.json", MAX_STATE_BYTES)
    except FileNotFoundError:
        return OperationState()
    try:
        return _parse_state(payload)
    except OperationStateError:
        raise
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise OperationStateError("state_invalid") from None


def _write_state_at(
    state_fd: int,
    state: OperationState,
    fs: OperationStateFileSystem,
) -> None:
    payload = _canonical_json(_state_to_dict(state))
    temporary = f".state-{os.getpid()}-{os.urandom(16).hex()}.tmp"
    prior = _named_binding(state_fd, "state.json")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | _nofollow_flag()
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=state_fd,
        )
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        fs.checkpoint("replace")
        if _named_binding(state_fd, "state.json") != prior:
            raise OSError("state destination changed")
        os.rename(
            temporary,
            "state.json",
            src_dir_fd=state_fd,
            dst_dir_fd=state_fd,
        )
        os.fsync(state_fd)
    except Exception:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary, dir_fd=state_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise OperationStateError("state_write_failed") from None


def _named_binding(state_fd: int, name: str) -> tuple[int, int, int] | None:
    try:
        info = os.stat(name, dir_fd=state_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OSError("state destination is unsafe")
    return (info.st_dev, info.st_ino, info.st_size)


def _read_regular_at(parent_fd: int, name: str, limit: int) -> bytes:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise OperationStateError("state_invalid")
    descriptor = os.open(
        name,
        os.O_RDONLY | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size != before.st_size
        ):
            raise OperationStateError("state_invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise OperationStateError("state_invalid")
        final = os.fstat(descriptor)
        if (
            (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino)
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
            or total != opened.st_size
        ):
            raise OperationStateError("state_invalid")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write")
        remaining = remaining[written:]


def _nofollow_flag() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _state_to_dict(state: OperationState) -> dict[str, object]:
    return {
        "last_failure": None if state.last_failure is None else asdict(state.last_failure),
        "last_success": None if state.last_success is None else asdict(state.last_success),
        "schema_version": state.schema_version,
    }


def _parse_state(payload: bytes) -> OperationState:
    value = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_closed_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )
    if type(value) is not dict or set(value) != {
        "last_failure", "last_success", "schema_version"
    }:
        raise OperationStateError("state_invalid")
    success = value["last_success"]
    failure = value["last_failure"]
    try:
        if success is not None:
            if type(success) is not dict or set(success) != {
                "operation", "duration_ms", "safe_file_count", "coverage",
                "source_digest", "projection_digest", "graph_digest",
                "generation_digest", "build_epoch", "artifact_channel",
            }:
                raise OperationStateError("state_invalid")
            coverage = success["coverage"]
            if type(coverage) is not dict or set(coverage) != {
                "represented", "approved_omissions", "denied"
            }:
                raise OperationStateError("state_invalid")
            parsed_success = SuccessfulOperation(
                operation=success["operation"],
                duration_ms=success["duration_ms"],
                safe_file_count=success["safe_file_count"],
                coverage=CoverageMetrics(**coverage),
                source_digest=success["source_digest"],
                projection_digest=success["projection_digest"],
                graph_digest=success["graph_digest"],
                generation_digest=success["generation_digest"],
                build_epoch=success["build_epoch"],
                artifact_channel=success["artifact_channel"],
            )
        else:
            parsed_success = None
        if failure is not None and (
            type(failure) is not dict
            or set(failure) != {"operation", "failure_code"}
        ):
            raise OperationStateError("state_invalid")
        parsed_failure = None if failure is None else FailureRecord(**failure)
        return OperationState(
            schema_version=value["schema_version"],
            last_success=parsed_success,
            last_failure=parsed_failure,
        )
    except OperationStateError:
        raise
    except (TypeError, ValueError):
        raise OperationStateError("state_invalid") from None


def _closed_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _read_regular(path: Path, limit: int) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
        raise OperationStateError("state_invalid")
    return path.read_bytes()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ) + "\n").encode("utf-8")


def _require_digest(value: object) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise OperationStateError("state_invalid")


def _load_envelope(path: Path) -> dict[str, object]:
    payload = _read_regular(path, MAX_SUMMARY_INPUT_BYTES)
    value = json.loads(
        payload.decode("utf-8"), object_pairs_hook=_closed_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )
    if type(value) is not dict:
        raise OperationStateError("summary_invalid")
    return value


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m project_knowledge.operation_state")
    subparsers = parser.add_subparsers(dest="verb", required=True)
    summary = subparsers.add_parser("ci-summary")
    summary.add_argument("--output-json", type=Path, required=True)
    for name in ("preflight", "doctor", "scan", "health"):
        summary.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    inputs = [_load_envelope(getattr(args, name)) for name in (
        "preflight", "doctor", "scan", "health"
    )]
    machine, markdown = render_ci_summary(inputs)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(args.output_json, flags, 0o600)
    try:
        os.write(descriptor, machine)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
