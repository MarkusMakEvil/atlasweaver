"""Closed graph bundles, strict stored-ZIP parsing, and atomic installation."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tempfile
from typing import Any, Literal
from uuid import UUID
import zlib


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


__all__ = [
    "ALLOWED_PAYLOADS", "ARTIFACT_SCHEMA_VERSION", "ArchiveLimits",
    "ArtifactManifest", "BundleError", "GithubTransport", "GitIdentity",
    "LocalTransport", "ParsedBundle", "PayloadBinding", "PayloadDescriptor",
    "V1_LIMITS", "inspect_bundle_manifest", "parse_bundle",
]
