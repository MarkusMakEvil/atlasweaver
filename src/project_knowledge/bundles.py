"""Closed graph bundles, strict stored-ZIP parsing, and atomic installation."""

from __future__ import annotations

from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field
import hashlib
import fcntl
import importlib.metadata
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import struct
import tempfile
import threading
import time
from typing import Any, Iterator, Literal, TYPE_CHECKING
from uuid import UUID
import zlib
import zipfile

from .artifacts import (
    ArtifactValidationError,
    GitIdentity as ArtifactGitIdentity,
    ValidatedGraph,
    promote_graph,
    validate_candidate,
    validate_owned_graph,
)
from .compatibility import resolve_graphify_compatibility
from .evidence import GRAPH_EVIDENCE_MAX_BYTES, parse_graph_evidence
from .locking import (
    RepositoryIdentity,
    capture_lifecycle_repository,
    repository_lifecycle_lock,
)
from .manifest import (
    ManifestError,
    assert_current_manifest_unchanged,
    inspect_init_journal,
    load_manifest,
    require_current_manifest,
)
from .models import ProjectManifest
from .staging import StagedInput, StagingError, stage_input

if TYPE_CHECKING:
    from .github_artifacts import DownloadReceipt, VerifiedAttestation


ARTIFACT_SCHEMA_VERSION = 1
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
ALLOWED_PAYLOADS = (
    PurePosixPath("graphify-out/GRAPH_EVIDENCE.json"),
    PurePosixPath("graphify-out/GRAPH_REPORT.md"),
    PurePosixPath("graphify-out/graph.html"),
    PurePosixPath("graphify-out/graph.json"),
)
_REQUIRED_PAYLOADS = frozenset(
    {
        PurePosixPath("graphify-out/GRAPH_EVIDENCE.json"),
        PurePosixPath("graphify-out/GRAPH_REPORT.md"),
        PurePosixPath("graphify-out/graph.json"),
    }
)
_ARTIFACT_KEYS = frozenset(
    {
        "adapter_id", "atlasweaver_version", "build_epoch", "generation_digest",
        "git", "graph_digest", "graphify_version", "payloads", "project_id",
        "project_uid", "projection_digest", "schema_version", "source_digest",
        "transport",
    }
)
_PAYLOAD_KEYS = frozenset({"byte_length", "path", "sha256"})
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_RECOVERY = re.compile(r"[0-9a-f]{16,128}\Z")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CHANNEL = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")

LOCAL = struct.Struct("<4s5H3L2H")
CENTRAL = struct.Struct("<4s6H3L5H2L")
EOCD = struct.Struct("<4s4H2LH")
LOCAL_MAGIC = b"PK\x03\x04"
CENTRAL_MAGIC = b"PK\x01\x02"
EOCD_MAGIC = b"PK\x05\x06"
UTF8_FLAG = 0x0800
FORBIDDEN_FLAGS = 0x0001 | 0x0008
_CHUNK = 1024 * 1024


def _noop() -> None:
    pass


class BundleError(RuntimeError):
    """Stable, content-free artifact failure."""

    def __init__(
        self, code: str, message: str | None = None, recovery_id: str | None = None
    ) -> None:
        if type(code) is not str or not code.startswith("bundle_") and code != "init_recovery_required":
            raise ValueError("bundle error code is invalid")
        recovery_allowed = code in {
            "bundle_cleanup_failed", "bundle_output_recovery_required"
        }
        if (recovery_id is not None) != recovery_allowed or (
            recovery_id is not None and _RECOVERY.fullmatch(recovery_id) is None
        ):
            raise ValueError("bundle recovery ID is invalid")
        self.code = code
        self.recovery_id = recovery_id
        super().__init__(message or code)


@dataclass(frozen=True)
class GitIdentity:
    commit_oid: str
    algorithm: Literal["sha1", "sha256"]

    def __post_init__(self) -> None:
        expected = _HEX40 if self.algorithm == "sha1" else _HEX64
        if self.algorithm not in {"sha1", "sha256"} or expected.fullmatch(self.commit_oid) is None:
            raise BundleError("bundle_invalid")


@dataclass(frozen=True)
class LocalTransport:
    provider: Literal["none"] = "none"
    channel: None = None


@dataclass(frozen=True)
class GithubTransport:
    provider: Literal["github-release"]
    channel: str
    host: str
    repository: str
    repository_id: int
    source_ref: str


@dataclass(frozen=True)
class PayloadDescriptor:
    path: PurePosixPath
    sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        if (
            type(self.path) is not PurePosixPath
            or self.path not in ALLOWED_PAYLOADS
            or _HEX64.fullmatch(self.sha256) is None
            or type(self.byte_length) is not int
            or self.byte_length < 0
        ):
            raise BundleError("bundle_invalid")


@dataclass(frozen=True)
class ArtifactManifest:
    schema_version: int
    atlasweaver_version: str
    project_id: str
    project_uid: str
    graphify_version: str
    adapter_id: str
    source_digest: str
    projection_digest: str
    graph_digest: str
    generation_digest: str
    git: GitIdentity | None
    build_epoch: int
    transport: LocalTransport | GithubTransport
    payloads: tuple[PayloadDescriptor, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise BundleError("bundle_invalid")
        for value in (self.atlasweaver_version, self.graphify_version):
            if type(value) is not str or _VERSION.fullmatch(value) is None:
                raise BundleError("bundle_invalid")
        for value in (self.project_id, self.adapter_id):
            if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
                raise BundleError("bundle_invalid")
        try:
            uid = UUID(self.project_uid)
        except (ValueError, TypeError, AttributeError):
            raise BundleError("bundle_invalid") from None
        if uid.version != 4 or str(uid) != self.project_uid:
            raise BundleError("bundle_invalid")
        for value in (
            self.source_digest, self.projection_digest, self.graph_digest,
            self.generation_digest,
        ):
            _require_digest(value)
        if type(self.build_epoch) is not int or self.build_epoch <= 0:
            raise BundleError("bundle_invalid")
        if type(self.payloads) is not tuple:
            raise BundleError("bundle_invalid")
        paths = tuple(item.path for item in self.payloads)
        expected = tuple(path for path in ALLOWED_PAYLOADS if path in set(paths))
        if paths != expected or len(paths) != len(set(paths)) or not _REQUIRED_PAYLOADS <= set(paths):
            raise BundleError("bundle_invalid")
        graph = next(item for item in self.payloads if item.path.name == "graph.json")
        if graph.sha256 != self.graph_digest:
            raise BundleError("bundle_invalid")
        _validate_transport(self.transport)

    def to_dict(self) -> dict[str, object]:
        if isinstance(self.transport, LocalTransport):
            transport: dict[str, object] = {"channel": None, "provider": "none"}
        else:
            transport = {
                "channel": self.transport.channel,
                "host": self.transport.host,
                "provider": self.transport.provider,
                "repository": self.transport.repository,
                "repository_id": self.transport.repository_id,
                "source_ref": self.transport.source_ref,
            }
        return {
            "adapter_id": self.adapter_id,
            "atlasweaver_version": self.atlasweaver_version,
            "build_epoch": self.build_epoch,
            "generation_digest": self.generation_digest,
            "git": None if self.git is None else {
                "algorithm": self.git.algorithm, "commit_oid": self.git.commit_oid
            },
            "graph_digest": self.graph_digest,
            "graphify_version": self.graphify_version,
            "payloads": [
                {"byte_length": item.byte_length, "path": item.path.as_posix(), "sha256": item.sha256}
                for item in self.payloads
            ],
            "project_id": self.project_id,
            "project_uid": self.project_uid,
            "projection_digest": self.projection_digest,
            "schema_version": self.schema_version,
            "source_digest": self.source_digest,
            "transport": transport,
        }

    def to_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ArtifactManifest":
        try:
            if type(payload) is not bytes or len(payload) > 65_536:
                raise ValueError
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_pairs,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            )
            if type(value) is not dict or set(value) != _ARTIFACT_KEYS:
                raise ValueError
            payload_values = value["payloads"]
            if type(payload_values) is not list:
                raise ValueError
            descriptors: list[PayloadDescriptor] = []
            for item in payload_values:
                if type(item) is not dict or set(item) != _PAYLOAD_KEYS:
                    raise ValueError
                descriptors.append(
                    PayloadDescriptor(
                        PurePosixPath(_exact_string(item["path"])),
                        _exact_string(item["sha256"]),
                        item["byte_length"],
                    )
                )
            git_value = value["git"]
            if git_value is None:
                git = None
            else:
                if type(git_value) is not dict or set(git_value) != {"algorithm", "commit_oid"}:
                    raise ValueError
                git = GitIdentity(
                    _exact_string(git_value["commit_oid"]),
                    _exact_string(git_value["algorithm"]),  # type: ignore[arg-type]
                )
            transport = _parse_transport(value["transport"])
            result = cls(
                schema_version=value["schema_version"],
                atlasweaver_version=_exact_string(value["atlasweaver_version"]),
                project_id=_exact_string(value["project_id"]),
                project_uid=_exact_string(value["project_uid"]),
                graphify_version=_exact_string(value["graphify_version"]),
                adapter_id=_exact_string(value["adapter_id"]),
                source_digest=_exact_string(value["source_digest"]),
                projection_digest=_exact_string(value["projection_digest"]),
                graph_digest=_exact_string(value["graph_digest"]),
                generation_digest=_exact_string(value["generation_digest"]),
                git=git,
                build_epoch=value["build_epoch"],
                transport=transport,
                payloads=tuple(descriptors),
            )
            if result.to_bytes() != payload:
                raise ValueError
            return result
        except BundleError:
            raise
        except Exception:
            raise BundleError("bundle_invalid") from None


@dataclass(frozen=True)
class ArchiveLimits:
    total_bytes: int = 268_435_456
    payload_bytes: int = 267_386_880
    entry_bytes: int = 134_217_728


V1_LIMITS = ArchiveLimits()


@dataclass(frozen=True)
class PayloadBinding:
    path: PurePosixPath
    candidate_name: str
    sha256: str
    byte_length: int
    device: int
    inode: int


@dataclass
class ParsedBundle(AbstractContextManager["ParsedBundle"]):
    root: Path
    artifact: ArtifactManifest
    payloads: tuple[PayloadBinding, ...]
    archive_sha256: str
    archive_size: int
    _descriptor: int = field(repr=False)

    def __enter__(self) -> "ParsedBundle":
        if self._descriptor < 0:
            raise BundleError("bundle_invalid")
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        descriptor, self._descriptor = self._descriptor, -1
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                if not descriptor < 0:
                    raise BundleError("bundle_invalid") from None

    def read_payload(self, path: PurePosixPath, max_bytes: int) -> bytes:
        if self._descriptor < 0 or type(max_bytes) is not int or max_bytes < 0:
            raise BundleError("bundle_invalid")
        binding = next((item for item in self.payloads if item.path == path), None)
        if binding is None or binding.byte_length > max_bytes:
            raise BundleError("bundle_invalid")
        try:
            payload, info = _read_bound_file_at(
                self._descriptor, binding.candidate_name, max_bytes
            )
        except Exception:
            raise BundleError("bundle_invalid") from None
        if (
            (info.st_dev, info.st_ino) != (binding.device, binding.inode)
            or len(payload) != binding.byte_length
            or hashlib.sha256(payload).hexdigest() != binding.sha256
        ):
            raise BundleError("bundle_invalid")
        return payload


@dataclass(frozen=True)
class _ZipEntry:
    name: str
    crc32: int
    size: int
    data_offset: int


def parse_bundle(bundle: Path, destination: Path) -> ParsedBundle:
    if destination.exists() or destination.is_symlink():
        raise BundleError("bundle_invalid")
    capture_root: Path | None = None
    destination_created = False
    try:
        capture_root = Path(tempfile.mkdtemp(prefix="atlasweaver-bundle-"))
        capture_root.chmod(0o700)
        captured = capture_root / "archive.zip"
        archive_sha256, archive_size = _capture_archive(bundle, captured)
        descriptor = os.open(captured, os.O_RDONLY | _nofollow())
        try:
            entries = _parse_zip_structure(descriptor, archive_size)
            artifact_entry = entries[0]
            artifact_payload = _read_entry(descriptor, artifact_entry, 65_536)
            artifact = ArtifactManifest.from_bytes(artifact_payload)
            expected_names = ("artifact.json", *(item.path.as_posix() for item in artifact.payloads))
            if tuple(item.name for item in entries) != expected_names:
                raise BundleError("bundle_invalid")
            descriptors = {item.path: item for item in artifact.payloads}
            verified: list[tuple[_ZipEntry, PayloadDescriptor]] = []
            for entry in entries[1:]:
                declared = descriptors[PurePosixPath(entry.name)]
                payload = _read_entry(descriptor, entry, V1_LIMITS.entry_bytes)
                if len(payload) != declared.byte_length or hashlib.sha256(payload).hexdigest() != declared.sha256:
                    raise BundleError("bundle_invalid")
                verified.append((entry, declared))

            destination.mkdir(mode=0o700, parents=False, exist_ok=False)
            destination.chmod(0o700)
            destination_created = True
            destination_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | _nofollow())
            bindings: list[PayloadBinding] = []
            try:
                for entry, declared in verified:
                    candidate_name = declared.path.name
                    payload = _read_entry(descriptor, entry, V1_LIMITS.entry_bytes)
                    file_fd = os.open(
                        candidate_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow(),
                        0o600,
                        dir_fd=destination_fd,
                    )
                    try:
                        os.fchmod(file_fd, 0o600)
                        _write_all(file_fd, payload)
                        os.fsync(file_fd)
                        info = os.fstat(file_fd)
                    finally:
                        os.close(file_fd)
                    bindings.append(
                        PayloadBinding(
                            declared.path, candidate_name, declared.sha256,
                            declared.byte_length, info.st_dev, info.st_ino,
                        )
                    )
                os.fsync(destination_fd)
            except BaseException:
                os.close(destination_fd)
                raise
            return ParsedBundle(
                destination,
                artifact,
                tuple(bindings),
                archive_sha256,
                archive_size,
                destination_fd,
            )
        finally:
            os.close(descriptor)
    except BundleError:
        if destination_created:
            _remove_fresh_destination(destination)
        raise
    except Exception:
        if destination_created:
            _remove_fresh_destination(destination)
        raise BundleError("bundle_invalid") from None
    finally:
        if capture_root is not None:
            shutil.rmtree(capture_root, ignore_errors=True)


def inspect_bundle_manifest(bundle: Path) -> ArtifactManifest:
    capture_root: Path | None = None
    try:
        capture_root = Path(tempfile.mkdtemp(prefix="atlasweaver-inspect-"))
        capture_root.chmod(0o700)
        captured = capture_root / "archive.zip"
        _, size = _capture_archive(bundle, captured)
        descriptor = os.open(captured, os.O_RDONLY | _nofollow())
        try:
            entries = _parse_zip_structure(descriptor, size)
            artifact = ArtifactManifest.from_bytes(_read_entry(descriptor, entries[0], 65_536))
            expected = ("artifact.json", *(item.path.as_posix() for item in artifact.payloads))
            if tuple(item.name for item in entries) != expected:
                raise BundleError("bundle_invalid")
            return artifact
        finally:
            os.close(descriptor)
    except BundleError:
        raise
    except Exception:
        raise BundleError("bundle_invalid") from None
    finally:
        if capture_root is not None:
            shutil.rmtree(capture_root, ignore_errors=True)


def _capture_archive(source: Path, destination: Path) -> tuple[str, int]:
    source_fd = -1
    target_fd = -1
    try:
        source_fd = os.open(source, os.O_RDONLY | _nofollow() | getattr(os, "O_CLOEXEC", 0))
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise BundleError("bundle_invalid")
        if before.st_size > V1_LIMITS.total_bytes:
            raise BundleError("bundle_too_large")
        target_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow(),
            0o600,
        )
        os.fchmod(target_fd, 0o600)
        first = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source_fd, _CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > V1_LIMITS.total_bytes:
                raise BundleError("bundle_too_large")
            first.update(chunk)
            _write_all(target_fd, chunk)
        os.fsync(target_fd)
        middle = os.fstat(source_fd)
        os.lseek(source_fd, 0, os.SEEK_SET)
        second = hashlib.sha256()
        second_total = 0
        while True:
            chunk = os.read(source_fd, _CHUNK)
            if not chunk:
                break
            second_total += len(chunk)
            second.update(chunk)
        after = os.fstat(source_fd)
        bindings = tuple(
            (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            for item in (before, middle, after)
        )
        if (
            len(set(bindings)) != 1
            or total != before.st_size
            or second_total != total
            or first.digest() != second.digest()
        ):
            raise BundleError("bundle_changed_during_capture")
        return first.hexdigest(), total
    except BundleError:
        raise
    except OSError:
        raise BundleError("bundle_invalid") from None
    finally:
        if target_fd >= 0:
            os.close(target_fd)
        if source_fd >= 0:
            os.close(source_fd)


def _parse_zip_structure(descriptor: int, size: int) -> tuple[_ZipEntry, ...]:
    if size < EOCD.size:
        raise BundleError("bundle_invalid")
    eocd_offset = size - EOCD.size
    eocd = EOCD.unpack(_pread_exact(descriptor, EOCD.size, eocd_offset))
    if eocd[0] != EOCD_MAGIC:
        raise BundleError("bundle_invalid")
    _, disk, central_disk, disk_entries, total_entries, central_size, central_offset, comment = eocd
    if (
        disk != 0 or central_disk != 0 or disk_entries != total_entries
        or total_entries == 0 or comment != 0
        or total_entries == 0xFFFF or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
        or central_offset + central_size != eocd_offset
    ):
        raise BundleError("bundle_invalid")
    position = central_offset
    records: list[tuple[str, int, int, int, int, int, int, int]] = []
    aggregate = 0
    for _ in range(total_entries):
        fields = CENTRAL.unpack(_pread_exact(descriptor, CENTRAL.size, position))
        if fields[0] != CENTRAL_MAGIC:
            raise BundleError("bundle_invalid")
        (
            _, made, needed, flags, method, mod_time, mod_date, crc, compressed,
            uncompressed, name_len, extra_len, comment_len, disk_start,
            internal_attr, external_attr, local_offset,
        ) = fields
        position += CENTRAL.size
        name_bytes = _pread_exact(descriptor, name_len, position)
        position += name_len + extra_len + comment_len
        if extra_len or comment_len or disk_start or internal_attr:
            raise BundleError("bundle_invalid")
        if compressed > V1_LIMITS.entry_bytes or uncompressed > V1_LIMITS.entry_bytes:
            raise BundleError("bundle_too_large")
        aggregate += uncompressed
        if aggregate > V1_LIMITS.payload_bytes:
            raise BundleError("bundle_too_large")
        name = _decode_name(name_bytes, flags)
        if (
            method != 0 or compressed != uncompressed or flags & FORBIDDEN_FLAGS
            or flags & ~UTF8_FLAG or made >> 8 != 3
            or mod_time != 0 or mod_date != 33
            or stat.S_IFMT(external_attr >> 16) != stat.S_IFREG
            or stat.S_IMODE(external_attr >> 16) != 0o600
            or local_offset == 0xFFFFFFFF
        ):
            raise BundleError("bundle_invalid")
        records.append(
            (name, crc, uncompressed, local_offset, flags, method, mod_time, mod_date)
        )
    if position != eocd_offset:
        raise BundleError("bundle_invalid")
    if not records or records[0][0] != "artifact.json":
        raise BundleError("bundle_invalid")
    allowed_names = {"artifact.json", *(path.as_posix() for path in ALLOWED_PAYLOADS)}
    if len({item[0] for item in records}) != len(records) or any(item[0] not in allowed_names for item in records):
        raise BundleError("bundle_invalid")

    entries: list[_ZipEntry] = []
    expected_offset = 0
    for (
        name, crc, entry_size, local_offset, central_flags, central_method,
        central_time, central_date,
    ) in records:
        if local_offset != expected_offset:
            raise BundleError("bundle_invalid")
        local = LOCAL.unpack(_pread_exact(descriptor, LOCAL.size, local_offset))
        if local[0] != LOCAL_MAGIC:
            raise BundleError("bundle_invalid")
        _, needed, flags, method, mod_time, mod_date, local_crc, compressed, uncompressed, name_len, extra_len = local
        name_bytes = _pread_exact(descriptor, name_len, local_offset + LOCAL.size)
        local_name = _decode_name(name_bytes, flags)
        if (
            local_name != name or flags != central_flags or method != central_method
            or local_crc != crc or compressed != entry_size or uncompressed != entry_size
            or mod_time != central_time or mod_date != central_date
            or extra_len or method != 0
        ):
            raise BundleError("bundle_invalid")
        data_offset = local_offset + LOCAL.size + name_len
        end = data_offset + entry_size
        if end > central_offset:
            raise BundleError("bundle_invalid")
        entries.append(_ZipEntry(name, crc, entry_size, data_offset))
        expected_offset = end
    if expected_offset != central_offset:
        raise BundleError("bundle_invalid")
    return tuple(entries)


def _read_entry(descriptor: int, entry: _ZipEntry, limit: int) -> bytes:
    if entry.size > limit:
        raise BundleError("bundle_too_large")
    remaining = entry.size
    position = entry.data_offset
    chunks: list[bytes] = []
    crc = 0
    while remaining:
        chunk = _pread_exact(descriptor, min(_CHUNK, remaining), position)
        chunks.append(chunk)
        crc = zlib.crc32(chunk, crc)
        remaining -= len(chunk)
        position += len(chunk)
    if crc & 0xFFFFFFFF != entry.crc32:
        raise BundleError("bundle_invalid")
    return b"".join(chunks)


def _decode_name(payload: bytes, flags: int) -> str:
    try:
        name = payload.decode("utf-8" if flags & UTF8_FLAG else "ascii")
    except UnicodeError:
        raise BundleError("bundle_invalid") from None
    if (
        not name or name.startswith("/") or "\\" in name or "\x00" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise BundleError("bundle_invalid")
    path = PurePosixPath(name)
    if path.as_posix() != name or any(part in {"", ".", ".."} for part in path.parts):
        raise BundleError("bundle_invalid")
    return name


def _validate_transport(value: object) -> None:
    if isinstance(value, LocalTransport):
        if value.provider != "none" or value.channel is not None:
            raise BundleError("bundle_invalid")
        return
    if not isinstance(value, GithubTransport):
        raise BundleError("bundle_invalid")
    if (
        value.provider != "github-release" or value.host != "github.com"
        or _CHANNEL.fullmatch(value.channel) is None
        or type(value.repository_id) is not int or value.repository_id <= 0
        or type(value.repository) is not str or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value.repository) is None
        or type(value.source_ref) is not str or not value.source_ref
        or len(value.source_ref) > 256 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value.source_ref)
    ):
        raise BundleError("bundle_invalid")


def _parse_transport(value: object) -> LocalTransport | GithubTransport:
    if type(value) is not dict:
        raise BundleError("bundle_invalid")
    if value.get("provider") == "none":
        if set(value) != {"channel", "provider"} or value["channel"] is not None:
            raise BundleError("bundle_invalid")
        return LocalTransport()
    if value.get("provider") == "github-release":
        if set(value) != {"channel", "host", "provider", "repository", "repository_id", "source_ref"}:
            raise BundleError("bundle_invalid")
        result = GithubTransport(
            "github-release",
            _exact_string(value["channel"]),
            _exact_string(value["host"]),
            _exact_string(value["repository"]),
            value["repository_id"],
            _exact_string(value["source_ref"]),
        )
        _validate_transport(result)
        return result
    raise BundleError("bundle_invalid")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite")


def _finite_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("non-finite")
    return value


def _exact_string(value: object) -> str:
    if type(value) is not str:
        raise ValueError("string required")
    return value


def _require_digest(value: object) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise BundleError("bundle_invalid")
    return value


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _pread_exact(descriptor: int, length: int, offset: int) -> bytes:
    if length < 0 or offset < 0:
        raise BundleError("bundle_invalid")
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = os.pread(descriptor, remaining, offset)
        if not chunk:
            raise BundleError("bundle_invalid")
        chunks.append(chunk)
        remaining -= len(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write")
        remaining = remaining[written:]


def _read_bound_file_at(parent_fd: int, name: str, limit: int) -> tuple[bytes, os.stat_result]:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise OSError("unsafe payload")
    descriptor = os.open(name, os.O_RDONLY | _nofollow(), dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
            raise OSError("unstable payload")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise OSError("oversized payload")
        final = os.fstat(descriptor)
        if (
            (final.st_dev, final.st_ino, final.st_size) != (opened.st_dev, opened.st_ino, opened.st_size)
            or final.st_mtime_ns != opened.st_mtime_ns or final.st_ctime_ns != opened.st_ctime_ns
        ):
            raise OSError("unstable payload")
        return b"".join(chunks), final
    finally:
        os.close(descriptor)


def _remove_fresh_destination(destination: Path) -> None:
    try:
        if destination.is_symlink():
            return
        shutil.rmtree(destination)
    except OSError:
        pass


def _nofollow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


# -- Atomic install and same-process pull authorization -------------------


@dataclass(frozen=True)
class InstallResult:
    project_id: str
    project_uid: str
    graph_digest: str | None
    generation_digest: str | None
    build_epoch: int | None
    changed: bool | None
    status: Literal["installed", "already_current", "promoted_but_stale"]
    recovery_id: str | None = None

    def __post_init__(self) -> None:
        complete = (
            self.graph_digest is not None
            and self.generation_digest is not None
            and self.build_epoch is not None
        )
        if (
            type(self.project_id) is not str
            or not self.project_id
            or type(self.project_uid) is not str
            or self.status not in {"installed", "already_current", "promoted_but_stale"}
            or (self.status in {"installed", "already_current"} and not complete)
            or (self.status in {"installed", "already_current"} and self.recovery_id is not None)
            or (self.status == "installed" and self.changed is not True)
            or (self.status == "already_current" and self.changed is not False)
            or (self.recovery_id is not None and _RECOVERY.fullmatch(self.recovery_id) is None)
        ):
            raise BundleError("bundle_invalid")
        if complete:
            _require_digest(self.graph_digest)
            _require_digest(self.generation_digest)
            if type(self.build_epoch) is not int or self.build_epoch <= 0:
                raise BundleError("bundle_invalid")
        elif any(value is not None for value in (
            self.graph_digest, self.generation_digest, self.build_epoch, self.changed
        )):
            raise BundleError("bundle_invalid")


@dataclass(frozen=True)
class _InstallExecution:
    result: InstallResult
    promotion_committed: bool


@dataclass
class _InstallCommitState:
    promotion_committed: bool = False
    project_id: str | None = None
    project_uid: str | None = None
    installed: ValidatedGraph | None = None
    repository_identity: RepositoryIdentity | None = None
    manifest: ProjectManifest | None = None
    staged: StagedInput | None = None
    started_at: float = field(default_factory=time.monotonic)


class OperationTempCleanupError(RuntimeError):
    def __init__(self, operation: str, recovery_id: str) -> None:
        if operation not in {"pack", "install", "pull", "publication"}:
            raise ValueError("operation is invalid")
        if _RECOVERY.fullmatch(recovery_id) is None:
            raise ValueError("recovery ID is invalid")
        self.operation = operation
        self.recovery_id = recovery_id
        super().__init__("private operation cleanup failed")


@dataclass
class ManagedOperationTempRoot:
    path: Path
    cleanup_failed: bool = False


class OperationTempFileSystem:
    def checkpoint(self, operation: str) -> None:
        del operation

    def remove_tree(self, path: Path) -> None:
        shutil.rmtree(path)


REAL_OPERATION_TEMP_FS = OperationTempFileSystem()


@contextmanager
def managed_operation_temp_root(
    operation: str,
    recovery_id: str,
    *,
    fs: OperationTempFileSystem = REAL_OPERATION_TEMP_FS,
) -> Iterator[ManagedOperationTempRoot]:
    if operation not in {"pack", "install", "pull", "publication"} or _RECOVERY.fullmatch(recovery_id) is None:
        raise ValueError("managed operation identity is invalid")
    try:
        path = Path(tempfile.mkdtemp(prefix=f"atlasweaver-{operation}-"))
        path.chmod(0o700)
    except OSError:
        raise OperationTempCleanupError(operation, recovery_id) from None
    descriptor = -1
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | _nofollow() | os.O_CLOEXEC
        )
        opened = os.fstat(descriptor)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            shutil.rmtree(path)
        except OSError:
            pass
        raise OperationTempCleanupError(operation, recovery_id) from None
    managed = ManagedOperationTempRoot(path)
    pending: BaseException | None = None
    try:
        yield managed
    except BaseException as error:
        pending = error
        raise
    finally:
        try:
            fs.checkpoint(f"{operation}-cleanup")
            named = path.stat(follow_symlinks=False)
            current = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(named.st_mode)
                or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise OSError("temporary root identity changed")
            fs.remove_tree(path)
        except Exception:
            managed.cleanup_failed = True
            if pending is not None and isinstance(pending, Exception):
                raise OperationTempCleanupError(operation, recovery_id) from None
        finally:
            try:
                os.close(descriptor)
            except OSError:
                managed.cleanup_failed = True


@dataclass(frozen=True)
class PackRequest:
    repo_root: Path
    output: Path

    def __post_init__(self) -> None:
        if not isinstance(self.repo_root, Path) or not isinstance(self.output, Path):
            raise BundleError("bundle_invalid")


@dataclass(frozen=True)
class PackedBundle:
    path: Path
    sha256: str
    byte_length: int
    artifact: ArtifactManifest


@dataclass(frozen=True)
class CapturedPayload:
    path: PurePosixPath
    source: Path
    sha256: str
    byte_length: int
    device: int
    inode: int


@dataclass(frozen=True)
class CapturedGeneration:
    manifest: ProjectManifest
    validated: ValidatedGraph
    payloads: tuple[CapturedPayload, ...]


@dataclass
class _PackCommitState:
    output_committed: bool = False
    output_recovery_required: bool = False


def installed_atlasweaver_version() -> str:
    try:
        return importlib.metadata.version("atlasweaver")
    except importlib.metadata.PackageNotFoundError:
        raise BundleError("bundle_version_unavailable") from None


def pack_bundle(
    request: PackRequest,
    *,
    expected_repository_identity: RepositoryIdentity | None = None,
    expected_manifest: ProjectManifest | None = None,
) -> PackedBundle:
    if type(request) is not PackRequest:
        raise BundleError("bundle_invalid")
    recovery_id = secrets.token_hex(16)
    commit_state = _PackCommitState()
    packed: PackedBundle | None = None
    output_parent_fd = -1
    repository_identity: RepositoryIdentity | None = None
    projection: Any = None
    started = time.monotonic()
    try:
        output_parent_fd = _open_output_parent(request.output)
        _require_output_absent(output_parent_fd, request.output.name)
        try:
            with managed_operation_temp_root("pack", recovery_id) as temporary:
                with repository_lifecycle_lock(
                    request.repo_root,
                    expected_repository_identity=expected_repository_identity,
                ), capture_lifecycle_repository(request.repo_root) as repository:
                    repository_identity = repository.identity
                    if inspect_init_journal(
                        request.repo_root, repository_access=repository
                    ) != "none":
                        raise BundleError("init_recovery_required")
                    if expected_manifest is None:
                        manifest = load_manifest(
                            request.repo_root / ".graphify-project.yaml",
                            request.repo_root,
                            repository_access=repository,
                        )
                        manifest = require_current_manifest(
                            request.repo_root,
                            manifest,
                            repository_access=repository,
                        )
                    else:
                        manifest = require_current_manifest(
                            request.repo_root,
                            expected_manifest,
                            repository_access=repository,
                        )
                    _reject_pack_output_location(
                        repository.descriptor,
                        manifest,
                        output_parent_fd,
                        request.output.name,
                    )
                    from .staging import inspect_projection

                    projection = inspect_projection(
                        request.repo_root,
                        manifest,
                        repository_access=repository,
                    )
                    try:
                        generation = _capture_owned_generation(
                            request.repo_root,
                            manifest,
                            projection,
                            temporary.path / "generation",
                            repository_access=repository,
                        )
                    except ArtifactValidationError:
                        raise BundleError("bundle_stale") from None
                    after = inspect_projection(
                        request.repo_root,
                        manifest,
                        repository_access=repository,
                    )
                    assert_current_manifest_unchanged(
                        request.repo_root,
                        manifest,
                        repository_access=repository,
                    )
                    if projection != after:
                        raise BundleError("bundle_source_drift")
                packed = _pack_captured_generation(
                    generation,
                    request.output,
                    commit_state=commit_state,
                    output_parent_descriptor=output_parent_fd,
                )
            if temporary.cleanup_failed:
                code = (
                    "bundle_output_recovery_required"
                    if commit_state.output_committed or commit_state.output_recovery_required
                    else "bundle_cleanup_failed"
                )
                raise BundleError(code, recovery_id=recovery_id)
        except OperationTempCleanupError:
            code = (
                "bundle_output_recovery_required"
                if commit_state.output_committed or commit_state.output_recovery_required
                else "bundle_cleanup_failed"
            )
            raise BundleError(code, recovery_id=recovery_id) from None
        except Exception:
            if commit_state.output_committed or commit_state.output_recovery_required:
                raise BundleError(
                    "bundle_output_recovery_required", recovery_id=recovery_id
                ) from None
            if repository_identity is not None:
                _record_failure_quietly(
                    request.repo_root,
                    repository_identity,
                    "artifact_pack",
                    "bundle_pack_failed",
                )
            raise
        assert packed is not None and repository_identity is not None and projection is not None
        try:
            _record_pack_outcome(
                request.repo_root,
                repository_identity,
                generation.manifest,
                projection,
                packed,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        except Exception:
            raise BundleError(
                "bundle_output_recovery_required", recovery_id=recovery_id
            ) from None
        return packed
    finally:
        if output_parent_fd >= 0:
            os.close(output_parent_fd)


def _capture_owned_generation(
    repo_root: Path,
    manifest: ProjectManifest,
    projection: Any,
    destination: Path,
    *,
    repository_access: Any,
) -> CapturedGeneration:
    if manifest.project_uid is None or projection.projection_digest is None:
        raise ArtifactValidationError("portable artifact identity is required")
    root = _path_for_descriptor(repository_access.descriptor)
    owned_root = root / manifest.output_dir
    validated = validate_owned_graph(
        owned_root,
        manifest,
        expected_source_digest=projection.source_digest,
        expected_projection_digest=projection.projection_digest,
        repository_access=repository_access,
    )
    if (
        validated.source_digest != projection.source_digest
        or validated.projection_digest != projection.projection_digest
    ):
        raise ArtifactValidationError("owned generation is stale")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    names = [
        OWNERSHIP_MANIFEST_NAME,
        *(path.name for path in ALLOWED_PAYLOADS if (owned_root / path.name).exists()),
    ]
    for name in names:
        source = owned_root / name
        payload, _ = _read_stable_regular(source, V1_LIMITS.entry_bytes)
        target = destination / name
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
        )
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    captured_validated = validate_owned_graph(
        destination,
        manifest,
        expected_source_digest=projection.source_digest,
        expected_projection_digest=projection.projection_digest,
    )
    if (
        captured_validated.graph_digest != validated.graph_digest
        or captured_validated.generation_digest != validated.generation_digest
        or captured_validated.build_epoch != validated.build_epoch
    ):
        raise ArtifactValidationError("captured generation identity mismatch")
    payloads: list[CapturedPayload] = []
    for transport_path in ALLOWED_PAYLOADS:
        source = destination / transport_path.name
        if not source.exists():
            continue
        payload, info = _read_stable_regular(source, V1_LIMITS.entry_bytes)
        payloads.append(CapturedPayload(
            transport_path,
            source,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            info.st_dev,
            info.st_ino,
        ))
    evidence = next(
        item for item in payloads
        if item.path == PurePosixPath("graphify-out/GRAPH_EVIDENCE.json")
    )
    evidence_payload, _ = _read_captured_payload(evidence)
    parse_graph_evidence(
        evidence_payload,
        resolve_graphify_compatibility(manifest.graphify_version),
        expected_digest=evidence.sha256,
    )
    return CapturedGeneration(manifest, captured_validated, tuple(payloads))


def _capture_validated_generation(
    validated: ValidatedGraph,
    manifest: ProjectManifest,
    destination: Path,
) -> CapturedGeneration:
    """Descriptor-capture one in-process validated candidate for publication."""
    if type(validated) is not ValidatedGraph or validated.project_uid != manifest.project_uid:
        raise ArtifactValidationError("validated generation identity mismatch")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    names = [
        OWNERSHIP_MANIFEST_NAME,
        *(path.name for path in ALLOWED_PAYLOADS if (validated.root / path.name).exists()),
    ]
    for name in names:
        payload, _ = _read_stable_regular(
            validated.root / name, V1_LIMITS.entry_bytes
        )
        descriptor = os.open(
            destination / name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    captured = validate_owned_graph(
        destination,
        manifest,
        expected_source_digest=validated.source_digest,
        expected_projection_digest=validated.projection_digest,
    )
    if (
        captured.graph_digest != validated.graph_digest
        or captured.generation_digest != validated.generation_digest
        or captured.build_epoch != validated.build_epoch
        or captured.git_identity != validated.git_identity
    ):
        raise ArtifactValidationError("captured generation identity mismatch")
    payloads: list[CapturedPayload] = []
    for transport_path in ALLOWED_PAYLOADS:
        source = destination / transport_path.name
        if not source.exists():
            continue
        payload, info = _read_stable_regular(source, V1_LIMITS.entry_bytes)
        payloads.append(CapturedPayload(
            transport_path,
            source,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            info.st_dev,
            info.st_ino,
        ))
    return CapturedGeneration(manifest, captured, tuple(payloads))


OWNERSHIP_MANIFEST_NAME = ".project-knowledge-ownership.json"


def _pack_captured_generation(
    generation: CapturedGeneration,
    output: Path,
    *,
    commit_state: _PackCommitState,
    version_provider: Any = installed_atlasweaver_version,
    precommit_check: Any = _noop,
    output_parent_descriptor: int | None = None,
) -> PackedBundle:
    if commit_state.output_committed:
        raise BundleError("bundle_invalid")
    manifest = generation.manifest
    validated = generation.validated
    payload_descriptors = tuple(
        PayloadDescriptor(item.path, item.sha256, item.byte_length)
        for item in generation.payloads
    )
    config = manifest.artifacts
    if config.provider == "none":
        transport: LocalTransport | GithubTransport = LocalTransport()
    elif config.provider == "github-release":
        if any(value is None for value in (
            config.channel, config.host, config.repository,
            config.repository_id, config.source_ref,
        )):
            raise BundleError("bundle_invalid")
        transport = GithubTransport(
            "github-release",
            config.channel,
            config.host,
            config.repository,
            config.repository_id,
            config.source_ref,
        )
    else:
        raise BundleError("bundle_invalid")
    artifact = ArtifactManifest(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        atlasweaver_version=version_provider(),
        project_id=manifest.project_id,
        project_uid=str(manifest.project_uid),
        graphify_version=manifest.graphify_version,
        adapter_id=validated.adapter_id or "",
        source_digest=validated.source_digest,
        projection_digest=validated.projection_digest or "",
        graph_digest=validated.graph_digest,
        generation_digest=validated.generation_digest,
        git=(
            None
            if validated.git_identity is None
            else GitIdentity(
                validated.git_identity.commit_oid,
                validated.git_identity.algorithm,
            )
        ),
        build_epoch=validated.build_epoch or 0,
        transport=transport,
        payloads=payload_descriptors,
    )
    parent_fd = (
        _open_output_parent(output)
        if output_parent_descriptor is None
        else os.dup(output_parent_descriptor)
    )
    temporary_name = f".{output.name}.{secrets.token_hex(16)}.tmp"
    descriptor = -1
    linked = False
    try:
        _require_output_absent(parent_fd, output.name)
        descriptor = os.open(
            temporary_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(descriptor, "w+b", closefd=False) as stream:
            with zipfile.ZipFile(
                stream, "w", compression=zipfile.ZIP_STORED, allowZip64=False
            ) as archive:
                archive.writestr(_zip_info("artifact.json"), artifact.to_bytes())
                for captured in generation.payloads:
                    payload, _ = _read_captured_payload(captured)
                    archive.writestr(_zip_info(captured.path.as_posix()), payload)
            stream.flush()
            os.fsync(descriptor)
        precommit_check()
        os.link(
            temporary_name,
            output.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        linked = True
        os.fsync(parent_fd)
        commit_state.output_committed = True
        info = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, _CHUNK)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        if (
            not stat.S_ISREG(info.st_mode)
            or (info.st_dev, info.st_ino) != (
                os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino
            )
            or info.st_size != size
        ):
            raise BundleError("bundle_invalid")
        os.unlink(temporary_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return PackedBundle(output, digest.hexdigest(), size, artifact)
    except FileExistsError:
        raise BundleError("bundle_output_exists") from None
    except BaseException:
        if linked and not commit_state.output_committed:
            try:
                named = os.stat(
                    output.name, dir_fd=parent_fd, follow_symlinks=False
                )
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(named.st_mode)
                    or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
                ):
                    raise OSError
                os.unlink(output.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
                linked = False
            except OSError:
                commit_state.output_recovery_required = True
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not commit_state.output_committed:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        elif linked:
            # A committed output is never removed by cleanup code.
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, ZIP_EPOCH)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.internal_attr = 0
    return info


def _open_output_parent(output: Path) -> int:
    try:
        if not output.name or output.name in {".", ".."}:
            raise OSError
        return os.open(
            output.parent.absolute(),
            os.O_RDONLY | os.O_DIRECTORY | _nofollow() | os.O_CLOEXEC,
        )
    except OSError:
        raise BundleError("bundle_output_invalid") from None


def _require_output_absent(parent_fd: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise BundleError("bundle_output_invalid") from None
    raise BundleError("bundle_output_exists")


def _reject_pack_output_location(
    repository_fd: int,
    manifest: ProjectManifest,
    output_parent_fd: int,
    output_name: str,
) -> None:
    root = _path_for_descriptor(repository_fd)
    parent = _path_for_descriptor(output_parent_fd)
    output = parent / output_name
    try:
        relative = output.relative_to(root)
    except ValueError:
        return
    if relative.as_posix() in {
        ".graphify-project.yaml", ".graphifyignore",
        ".graphify-secret-exceptions.yaml", ".atlasweaver-coverage.yaml",
    }:
        raise BundleError("bundle_output_invalid")
    if relative.parts and relative.parts[0] in {
        ".git", ".project-knowledge", "graphify-out"
    }:
        raise BundleError("bundle_output_invalid")
    if any(
        relative == include or include in relative.parents
        for include in manifest.include_roots
    ):
        raise BundleError("bundle_output_invalid")


def _read_stable_regular(path: Path, limit: int) -> tuple[bytes, os.stat_result]:
    descriptor = -1
    try:
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise OSError
        descriptor = os.open(path, os.O_RDONLY | _nofollow() | os.O_CLOEXEC)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev, before.st_ino, before.st_size
        ):
            raise OSError
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise OSError
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise OSError
        return b"".join(chunks), after
    except OSError:
        raise ArtifactValidationError("owned artifact changed during capture") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_captured_payload(
    captured: CapturedPayload,
) -> tuple[bytes, os.stat_result]:
    payload, info = _read_stable_regular(captured.source, captured.byte_length)
    if (
        (info.st_dev, info.st_ino) != (captured.device, captured.inode)
        or len(payload) != captured.byte_length
        or hashlib.sha256(payload).hexdigest() != captured.sha256
    ):
        raise BundleError("bundle_invalid")
    return payload, info


def _record_pack_outcome(
    repo_root: Path,
    repository_identity: RepositoryIdentity,
    manifest: ProjectManifest,
    projection: Any,
    packed: PackedBundle,
    *,
    duration_ms: int,
) -> None:
    from .operation_state import (
        CoverageMetrics,
        SuccessfulOperation,
        record_success,
    )

    denied = sum(
        count for key, count in projection.reason_counts if key.startswith("deny:")
    )
    record_success(
        repo_root,
        SuccessfulOperation(
            operation="artifact_pack",
            duration_ms=duration_ms,
            safe_file_count=len(projection.files),
            coverage=CoverageMetrics(
                represented=max(
                    0, len(projection.files) - len(projection.coverage_approvals)
                ),
                approved_omissions=len(projection.coverage_approvals),
                denied=denied,
            ),
            source_digest=packed.artifact.source_digest,
            projection_digest=packed.artifact.projection_digest,
            graph_digest=packed.artifact.graph_digest,
            generation_digest=packed.artifact.generation_digest,
            build_epoch=packed.artifact.build_epoch,
            artifact_channel=manifest.artifacts.channel,
        ),
        expected_repository_identity=repository_identity,
    )


@dataclass(frozen=True)
class _PullAuthorization:
    nonce: str


_AUTHORIZED_PULLS: dict[int, tuple[_PullAuthorization, str, int, int]] = {}
_AUTHORIZED_PULLS_LOCK = threading.Lock()


def active_pull_authorization_count() -> int:
    with _AUTHORIZED_PULLS_LOCK:
        return len(_AUTHORIZED_PULLS)


@contextmanager
def registered_pull_authorization(
    bundle: Path,
    receipt: "DownloadReceipt",
    verified: "VerifiedAttestation",
    manifest: ProjectManifest,
) -> Iterator[_PullAuthorization]:
    authorization = _new_bound_pull_authorization(
        bundle, receipt, verified, manifest
    )
    entry = (
        authorization,
        receipt.archive_sha256,
        receipt.archive_size,
        receipt.identity.asset_id,
    )
    try:
        with _AUTHORIZED_PULLS_LOCK:
            if id(authorization) in _AUTHORIZED_PULLS:
                raise BundleError("bundle_unattested")
            _AUTHORIZED_PULLS[id(authorization)] = entry
        _after_pull_authorization_insert_checkpoint()
        yield authorization
    finally:
        _discard_pull_authorization(authorization)


def _after_pull_authorization_insert_checkpoint() -> None:
    pass


def _new_bound_pull_authorization(
    bundle: Path,
    receipt: "DownloadReceipt",
    verified: "VerifiedAttestation",
    manifest: ProjectManifest,
) -> _PullAuthorization:
    from .github_artifacts import DownloadReceipt, VerifiedAttestation

    if (
        type(receipt) is not DownloadReceipt
        or type(verified) is not VerifiedAttestation
        or type(manifest) is not ProjectManifest
    ):
        raise BundleError("bundle_unattested")
    digest, size = _hash_stable_bundle(bundle)
    artifact = inspect_bundle_manifest(bundle)
    transport = artifact.transport
    config = manifest.artifacts
    if (
        digest != receipt.archive_sha256
        or size != receipt.archive_size
        or verified.subject_sha256 != digest
        or receipt.identity.asset_digest != digest
        or receipt.identity.asset_size != size
        or artifact.generation_digest != receipt.identity.generation_digest
        or artifact.git is None
        or artifact.git.commit_oid != receipt.identity.source_commit_oid
        or receipt.artifact_git_commit_oid != receipt.identity.source_commit_oid
        or not isinstance(transport, GithubTransport)
        or config.provider != "github-release"
        or transport.host != config.host
        or transport.repository != config.repository
        or transport.repository_id != config.repository_id
        or transport.channel != config.channel
        or transport.source_ref != config.source_ref
        or verified.signer_workflow != config.signer_workflow
        or verified.signer_digest != config.signer_digest
        or verified.source_ref != config.source_ref
        or verified.source_digest != receipt.identity.source_commit_oid
    ):
        raise BundleError("bundle_unattested")
    return _PullAuthorization(secrets.token_hex(32))


def _discard_pull_authorization(authorization: _PullAuthorization) -> None:
    with _AUTHORIZED_PULLS_LOCK:
        entry = _AUTHORIZED_PULLS.get(id(authorization))
        if entry is not None and entry[0] is authorization:
            _AUTHORIZED_PULLS.pop(id(authorization), None)


def _consume_pull_authorization(
    bundle: Path, authorization: _PullAuthorization
) -> None:
    if type(authorization) is not _PullAuthorization:
        raise BundleError("bundle_unattested")
    with _AUTHORIZED_PULLS_LOCK:
        entry = _AUTHORIZED_PULLS.get(id(authorization))
        if entry is None or entry[0] is not authorization:
            raise BundleError("bundle_unattested")
        _AUTHORIZED_PULLS.pop(id(authorization), None)
    digest, size = _hash_stable_bundle(bundle)
    if digest != entry[1] or size != entry[2]:
        raise BundleError("bundle_unattested")


def install_local_bundle(
    repo_root: Path,
    bundle: Path,
    *,
    expected_repository_identity: RepositoryIdentity | None = None,
    expected_manifest: ProjectManifest | None = None,
) -> InstallResult:
    return _install_bundle(
        repo_root,
        bundle,
        None,
        expected_repository_identity=expected_repository_identity,
        expected_manifest=expected_manifest,
    ).result


def _install_verified_pull(
    repo_root: Path,
    bundle: Path,
    authorization: _PullAuthorization,
    *,
    expected_repository_identity: RepositoryIdentity | None = None,
    expected_manifest: ProjectManifest | None = None,
    recovery_id: str | None = None,
) -> InstallResult:
    return _install_verified_pull_execution(
        repo_root,
        bundle,
        authorization,
        expected_repository_identity=expected_repository_identity,
        expected_manifest=expected_manifest,
        recovery_id=recovery_id,
    ).result


def _install_verified_pull_execution(
    repo_root: Path,
    bundle: Path,
    authorization: _PullAuthorization,
    *,
    expected_repository_identity: RepositoryIdentity | None = None,
    expected_manifest: ProjectManifest | None = None,
    recovery_id: str | None = None,
) -> _InstallExecution:
    try:
        return _install_bundle(
            repo_root,
            bundle,
            authorization,
            expected_repository_identity=expected_repository_identity,
            expected_manifest=expected_manifest,
            recovery_id=recovery_id,
            record_state=False,
        )
    finally:
        _discard_pull_authorization(authorization)


def _install_bundle(
    repo_root: Path,
    bundle: Path,
    authorization: _PullAuthorization | None,
    *,
    expected_repository_identity: RepositoryIdentity | None = None,
    expected_manifest: ProjectManifest | None = None,
    recovery_id: str | None = None,
    record_state: bool = True,
) -> _InstallExecution:
    recovery_id = recovery_id or secrets.token_hex(16)
    if _RECOVERY.fullmatch(recovery_id) is None:
        raise BundleError("bundle_invalid")
    commit_state = _InstallCommitState()
    try:
        return _install_bundle_scoped(
            repo_root,
            bundle,
            authorization,
            expected_repository_identity=expected_repository_identity,
            expected_manifest=expected_manifest,
            recovery_id=recovery_id,
            commit_state=commit_state,
            record_state=record_state,
        )
    except OperationTempCleanupError:
        if commit_state.promotion_committed:
            execution = _postcommit_install_execution(
                commit_state, recovery_id=recovery_id
            )
            _record_commit_state_outcome(
                repo_root, commit_state, execution.result, record_state
            )
            return execution
        if record_state:
            _record_install_failure_quietly(
                repo_root, commit_state, "bundle_cleanup_failed"
            )
        raise BundleError(
            "bundle_cleanup_failed", "private install cleanup failed", recovery_id
        ) from None
    except Exception as error:
        if commit_state.promotion_committed:
            execution = _postcommit_install_execution(commit_state)
            _record_commit_state_outcome(
                repo_root, commit_state, execution.result, record_state
            )
            return execution
        if record_state:
            _record_install_failure_quietly(
                repo_root,
                commit_state,
                error.code if isinstance(error, BundleError) else "bundle_install_failed",
            )
        raise


def _install_bundle_scoped(
    repo_root: Path,
    bundle: Path,
    authorization: _PullAuthorization | None,
    *,
    expected_repository_identity: RepositoryIdentity | None,
    expected_manifest: ProjectManifest | None,
    recovery_id: str,
    commit_state: _InstallCommitState,
    record_state: bool,
) -> _InstallExecution:
    started = time.monotonic()
    with repository_lifecycle_lock(
        repo_root, expected_repository_identity=expected_repository_identity
    ), capture_lifecycle_repository(repo_root) as repository:
        commit_state.repository_identity = repository.identity
        if inspect_init_journal(repo_root, repository_access=repository) != "none":
            raise BundleError("init_recovery_required")
        if expected_manifest is None:
            manifest = load_manifest(
                repo_root / ".graphify-project.yaml",
                repo_root,
                repository_access=repository,
            )
            manifest = require_current_manifest(
                repo_root, manifest, repository_access=repository
            )
        else:
            manifest = require_current_manifest(
                repo_root, expected_manifest, repository_access=repository
            )
        commit_state.manifest = manifest
        with managed_operation_temp_root("install", recovery_id) as temporary, ExitStack() as parsed_scope:
            private = temporary.path
            before = stage_input(
                repo_root,
                manifest,
                private / "source-before",
                repository_access=repository,
            )
            commit_state.staged = before
            parsed = parsed_scope.enter_context(
                parse_bundle(bundle, private / "candidate")
            )
            _require_transport_authority(bundle, parsed, manifest, authorization)
            _require_project_snapshot_identity(parsed.artifact, manifest, before)
            compatibility = resolve_graphify_compatibility(manifest.graphify_version)
            evidence_path = PurePosixPath("graphify-out/GRAPH_EVIDENCE.json")
            evidence_binding = next(
                (item for item in parsed.payloads if item.path == evidence_path), None
            )
            if evidence_binding is None:
                raise BundleError("bundle_identity_mismatch")
            try:
                parse_graph_evidence(
                    parsed.read_payload(evidence_path, GRAPH_EVIDENCE_MAX_BYTES),
                    compatibility,
                    expected_digest=evidence_binding.sha256,
                )
                validated = validate_candidate(
                    parsed.root,
                    before,
                    manifest,
                    expected_projection_digest=before.projection_digest,
                    expected_evidence_digest=evidence_binding.sha256,
                    build_epoch=parsed.artifact.build_epoch,
                    git_identity=(
                        None
                        if parsed.artifact.git is None
                        else ArtifactGitIdentity(
                            parsed.artifact.git.commit_oid,
                            parsed.artifact.git.algorithm,
                        )
                    ),
                )
            except Exception as error:
                if isinstance(error, BundleError):
                    raise
                raise BundleError("bundle_identity_mismatch") from None
            if (
                validated.graph_digest != parsed.artifact.graph_digest
                or validated.generation_digest != parsed.artifact.generation_digest
                or validated.build_epoch != parsed.artifact.build_epoch
                or validated.adapter_id != parsed.artifact.adapter_id
                or str(validated.project_uid) != parsed.artifact.project_uid
            ):
                raise BundleError("bundle_identity_mismatch")
            immediately_before = stage_input(
                repo_root,
                manifest,
                private / "source-immediately-before",
                repository_access=repository,
            )
            if not _same_validation_projection(before, immediately_before):
                raise BundleError("bundle_source_drift")
            assert_current_manifest_unchanged(
                repo_root, manifest, repository_access=repository
            )
            promoted = promote_graph(
                validated, repo_root, repository_access=repository
            )
            promotion_committed = promoted.changed is True
            if promotion_committed:
                commit_state.promotion_committed = True
                commit_state.project_id = manifest.project_id
                commit_state.project_uid = str(manifest.project_uid)
            installed = _revalidate_installed_generation(
                repo_root, manifest, repository
            )
            if promotion_committed and installed is not None:
                commit_state.installed = installed
            installed_identity_matches = (
                installed is not None
                and promoted.digest == installed.graph_digest
                and promoted.generation_digest == installed.generation_digest
                and promoted.build_epoch == installed.build_epoch
                and installed.graph_digest == validated.graph_digest
                and installed.generation_digest == validated.generation_digest
                and (not promoted.changed or installed.build_epoch == validated.build_epoch)
            )
            try:
                after = stage_input(
                    repo_root,
                    manifest,
                    private / "source-after",
                    repository_access=repository,
                )
                assert_current_manifest_unchanged(
                    repo_root, manifest, repository_access=repository
                )
                current = installed_identity_matches and _same_validation_projection(before, after)
            except (StagingError, ManifestError):
                current = False
            if current:
                assert installed is not None
                result = InstallResult(
                    manifest.project_id,
                    str(manifest.project_uid),
                    installed.graph_digest,
                    installed.generation_digest,
                    installed.build_epoch,
                    promoted.changed,
                    "installed" if promoted.changed else "already_current",
                )
            else:
                result = InstallResult(
                    manifest.project_id,
                    str(manifest.project_uid),
                    None if installed is None else installed.graph_digest,
                    None if installed is None else installed.generation_digest,
                    None if installed is None else installed.build_epoch,
                    promoted.changed if installed_identity_matches else None,
                    "promoted_but_stale",
                )
        if temporary.cleanup_failed:
            if promotion_committed:
                return _postcommit_install_execution(commit_state, recovery_id=recovery_id)
            raise BundleError(
                "bundle_cleanup_failed", "private install cleanup failed", recovery_id
            )
        if record_state:
            _record_install_outcome(
                repo_root,
                repository,
                manifest,
                before,
                result,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                operation="artifact_install",
            )
        return _InstallExecution(result, promotion_committed)


def _postcommit_install_execution(
    state: _InstallCommitState, *, recovery_id: str | None = None
) -> _InstallExecution:
    assert state.promotion_committed
    assert state.project_id is not None and state.project_uid is not None
    installed = state.installed
    return _InstallExecution(
        InstallResult(
            state.project_id,
            state.project_uid,
            None if installed is None else installed.graph_digest,
            None if installed is None else installed.generation_digest,
            None if installed is None else installed.build_epoch,
            None if installed is None else True,
            "promoted_but_stale",
            recovery_id,
        ),
        True,
    )


def _require_transport_authority(
    bundle: Path,
    parsed: ParsedBundle,
    manifest: ProjectManifest,
    authorization: _PullAuthorization | None,
) -> None:
    if isinstance(parsed.artifact.transport, LocalTransport):
        if authorization is not None:
            raise BundleError("bundle_unattested")
        return
    if not isinstance(parsed.artifact.transport, GithubTransport):
        raise BundleError("bundle_invalid")
    if authorization is None:
        raise BundleError("bundle_pull_required")
    _consume_pull_authorization(bundle, authorization)
    config = manifest.artifacts
    transport = parsed.artifact.transport
    if (
        config.provider != "github-release"
        or transport.host != config.host
        or transport.repository != config.repository
        or transport.repository_id != config.repository_id
        or transport.channel != config.channel
        or transport.source_ref != config.source_ref
    ):
        raise BundleError("bundle_unattested")


def _require_project_snapshot_identity(
    artifact: ArtifactManifest,
    manifest: ProjectManifest,
    staged: StagedInput,
) -> None:
    compatibility = resolve_graphify_compatibility(manifest.graphify_version)
    expected_payloads = {
        PurePosixPath("graphify-out/GRAPH_EVIDENCE.json"),
        PurePosixPath("graphify-out/GRAPH_REPORT.md"),
        PurePosixPath("graphify-out/graph.json"),
    }
    if manifest.track_html:
        expected_payloads.add(PurePosixPath("graphify-out/graph.html"))
    if (
        manifest.schema_version != 2
        or manifest.project_uid is None
        or staged.projection_digest is None
        or artifact.project_id != manifest.project_id
        or artifact.project_uid != str(manifest.project_uid)
        or artifact.graphify_version != manifest.graphify_version
        or artifact.adapter_id != compatibility.adapter_id
        or artifact.source_digest != staged.source_digest
        or artifact.projection_digest != staged.projection_digest
        or {item.path for item in artifact.payloads} != expected_payloads
    ):
        code = (
            "bundle_stale"
            if artifact.source_digest != staged.source_digest
            or artifact.projection_digest != staged.projection_digest
            else "bundle_identity_mismatch"
        )
        raise BundleError(code)


def _same_validation_projection(left: StagedInput, right: StagedInput) -> bool:
    return (
        left.source_digest == right.source_digest
        and left.projection_digest == right.projection_digest
        and left.files == right.files
        and left.projection_files == right.projection_files
        and left.reason_counts == right.reason_counts
        and left.coverage_approvals == right.coverage_approvals
    )


def _revalidate_installed_generation(
    repo_root: Path,
    manifest: ProjectManifest,
    repository: Any,
) -> ValidatedGraph | None:
    try:
        root = _path_for_descriptor(repository.descriptor)
        return validate_owned_graph(
            root / manifest.output_dir,
            manifest,
            repository_access=repository,
        )
    except Exception:
        return None


def _path_for_descriptor(descriptor: int) -> Path:
    try:
        if hasattr(fcntl, "F_GETPATH"):
            payload = fcntl.fcntl(descriptor, fcntl.F_GETPATH, b"\0" * 1024)
            encoded = payload.split(b"\0", 1)[0]
            return Path(os.fsdecode(encoded))
        return Path(os.readlink(f"/proc/self/fd/{descriptor}"))
    except OSError:
        raise BundleError("bundle_source_drift") from None


def _hash_stable_bundle(bundle: Path) -> tuple[str, int]:
    descriptor = -1
    try:
        descriptor = os.open(bundle, os.O_RDONLY | _nofollow() | os.O_CLOEXEC)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > V1_LIMITS.total_bytes:
            raise OSError
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, _CHUNK)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise OSError
        return digest.hexdigest(), total
    except OSError:
        raise BundleError("bundle_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _record_install_outcome(
    repo_root: Path,
    repository: Any | None,
    manifest: ProjectManifest,
    staged: StagedInput,
    result: InstallResult,
    *,
    duration_ms: int,
    operation: Literal["artifact_install", "artifact_pull"],
    expected_repository_identity: RepositoryIdentity | None = None,
) -> None:
    from .operation_state import (
        CoverageMetrics,
        FailureRecord,
        SuccessfulOperation,
        record_failure,
        record_success,
    )

    if result.graph_digest is not None:
        represented = max(0, len(staged.files) - len(staged.coverage_approvals))
        authority = (
            {"repository_access": repository}
            if repository is not None
            else {"expected_repository_identity": expected_repository_identity}
        )
        record_success(
            repo_root,
            SuccessfulOperation(
                operation=operation,
                duration_ms=duration_ms,
                safe_file_count=len(staged.files),
                coverage=CoverageMetrics(
                    represented=represented,
                    approved_omissions=len(staged.coverage_approvals),
                    denied=sum(count for key, count in staged.reason_counts if key.startswith("deny:")),
                ),
                source_digest=staged.source_digest,
                projection_digest=staged.projection_digest or "",
                graph_digest=result.graph_digest,
                generation_digest=result.generation_digest,
                build_epoch=result.build_epoch,
                artifact_channel=manifest.artifacts.channel,
            ),
            **authority,
        )
    if result.status == "promoted_but_stale":
        authority = (
            {"repository_access": repository}
            if repository is not None
            else {"expected_repository_identity": expected_repository_identity}
        )
        record_failure(
            repo_root,
            FailureRecord(operation, "bundle_source_drift"),
            **authority,
        )


def _record_commit_state_outcome(
    repo_root: Path,
    state: _InstallCommitState,
    result: InstallResult,
    enabled: bool,
) -> None:
    if (
        not enabled
        or state.repository_identity is None
        or state.manifest is None
        or state.staged is None
    ):
        return
    try:
        _record_install_outcome(
            repo_root,
            None,
            state.manifest,
            state.staged,
            result,
            duration_ms=max(0, int((time.monotonic() - state.started_at) * 1000)),
            operation="artifact_install",
            expected_repository_identity=state.repository_identity,
        )
    except Exception:
        pass


def _record_install_failure_quietly(
    repo_root: Path, state: _InstallCommitState, code: str
) -> None:
    if state.repository_identity is None:
        return
    _record_failure_quietly(
        repo_root, state.repository_identity, "artifact_install", code
    )


def _record_failure_quietly(
    repo_root: Path,
    repository_identity: RepositoryIdentity,
    operation: Literal["artifact_pack", "artifact_install", "artifact_pull"],
    code: str,
) -> None:
    from .operation_state import FailureRecord, record_failure

    try:
        record_failure(
            repo_root,
            FailureRecord(operation, code),
            expected_repository_identity=repository_identity,
        )
    except Exception:
        pass


__all__ = [
    "ALLOWED_PAYLOADS", "ARTIFACT_SCHEMA_VERSION", "ArchiveLimits",
    "ArtifactManifest", "BundleError", "GithubTransport", "GitIdentity",
    "LocalTransport", "ParsedBundle", "PayloadBinding", "PayloadDescriptor",
    "CapturedGeneration", "CapturedPayload", "InstallResult",
    "ManagedOperationTempRoot", "OperationTempCleanupError", "PackRequest",
    "PackedBundle",
    "OperationTempFileSystem", "REAL_OPERATION_TEMP_FS",
    "V1_LIMITS", "active_pull_authorization_count", "inspect_bundle_manifest",
    "install_local_bundle", "managed_operation_temp_root", "pack_bundle",
    "parse_bundle",
    "registered_pull_authorization",
]
