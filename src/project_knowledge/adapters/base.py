"""Version-neutral contracts for safely captured Graphify artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Protocol

from project_knowledge.compatibility import GraphifyCompatibility
from project_knowledge.integrity import GraphIntegrity


class AdapterContractError(ValueError):
    """Raised when native artifacts violate an adapter boundary."""


def _require_logical_path(logical_path: PurePosixPath) -> None:
    rendered = logical_path.as_posix() if isinstance(logical_path, PurePosixPath) else ""
    if (
        not isinstance(logical_path, PurePosixPath)
        or rendered in {"", "."}
        or logical_path.is_absolute()
        or "\\" in rendered
        or "\x00" in rendered
        or re.match(r"^[A-Za-z]:/", rendered) is not None
        or any(part in {"", ".", ".."} for part in logical_path.parts)
    ):
        raise AdapterContractError("logical artifact path must be confined")


@dataclass(frozen=True)
class CapturedArtifact:
    logical_path: PurePosixPath
    payload: bytes
    sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        _require_logical_path(self.logical_path)
        if type(self.payload) is not bytes:
            raise AdapterContractError("artifact payload must be bytes")
        expected_digest = hashlib.sha256(self.payload).hexdigest()
        if (
            self.sha256 != expected_digest
            or type(self.byte_length) is not int
            or self.byte_length != len(self.payload)
        ):
            raise AdapterContractError("captured artifact descriptor does not match payload")

    @classmethod
    def from_payload(
        cls, logical_path: PurePosixPath, payload: bytes
    ) -> CapturedArtifact:
        _require_logical_path(logical_path)
        if type(payload) is not bytes:
            raise AdapterContractError("artifact payload must be bytes")
        return cls(
            logical_path=logical_path,
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            byte_length=len(payload),
        )


@dataclass(frozen=True)
class NativeGraph:
    document: Mapping[str, Any]
    nodes: tuple[Mapping[str, Any], ...]
    edges: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ReasonCount:
    code: str
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise AdapterContractError("adapter reason code is invalid")
        if type(self.count) is not int or self.count <= 0:
            raise AdapterContractError("adapter reason count is invalid")


@dataclass(frozen=True)
class NormalizationResult:
    cluster_input: CapturedArtifact
    observed_integrity: GraphIntegrity
    repairs: tuple[ReasonCount, ...]
    quarantines: tuple[ReasonCount, ...]


class GraphifyAdapter(Protocol):
    contract: GraphifyCompatibility

    def parse_post_dedup(self, artifact: CapturedArtifact) -> NativeGraph: ...

    def normalize_for_cluster(self, graph: NativeGraph) -> NormalizationResult: ...

    def adapt_clustered_graph(
        self,
        artifact: CapturedArtifact,
        *,
        staged_files: frozenset[PurePosixPath],
    ) -> bytes: ...


def capture_native_artifact(
    path: Path,
    logical_path: PurePosixPath,
    *,
    max_bytes: int,
) -> CapturedArtifact:
    """Capture a stable no-follow regular file into an immutable descriptor."""
    _require_logical_path(logical_path)
    if type(max_bytes) is not int or max_bytes <= 0:
        raise AdapterContractError("capture size cap is invalid")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise AdapterContractError("platform cannot safely capture native artifacts")

    descriptor = -1
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise AdapterContractError("native artifact must be a stable regular file")
        if before.st_size > max_bytes:
            raise AdapterContractError("native artifact exceeds capture size cap")
        flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _identity(before) != _identity(opened)
            or opened.st_size != before.st_size
        ):
            raise AdapterContractError("native artifact must be a stable regular file")
        if opened.st_size > max_bytes:
            raise AdapterContractError("native artifact exceeds capture size cap")

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise AdapterContractError("native artifact exceeds capture size cap")
            chunks.append(chunk)

        final = os.fstat(descriptor)
        if (
            _identity(final) != _identity(opened)
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
            or total != opened.st_size
        ):
            raise AdapterContractError("native artifact changed during capture")
        return CapturedArtifact.from_payload(logical_path, b"".join(chunks))
    except AdapterContractError:
        raise
    except OSError as error:
        raise AdapterContractError(
            "native artifact must be a stable regular file"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino
