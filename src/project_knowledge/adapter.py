"""Adapt native Graphify output into AtlasWeaver's validated contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

from .integrity import IntegrityError, analyze_graph
from .models import ProjectManifest
from .staging import StagedInput


_WRAPPER_FIELDS = frozenset(
    {
        "project_id",
        "graphify_version",
        "source_digest",
        "extraction_coverage",
        "graph_health",
    }
)
_PATH_FIELDS = frozenset({"source", "source_file", "source_path", "path", "file"})


class AdapterError(ValueError):
    """Raised when raw Graphify output cannot be adapted safely."""


@dataclass(frozen=True)
class AdaptedCandidate:
    """Safe summary of one separately written wrapper candidate."""

    root: Path
    source_digest: str
    node_count: int
    edge_count: int
    skipped_count: int


def adapt_candidate(
    raw_candidate: Path,
    destination: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    *,
    post_write_check: Callable[[], None] | None = None,
) -> AdaptedCandidate:
    """Copy only public graph artifacts and inject deterministic wrapper metadata."""
    raw = _real_directory(raw_candidate, "raw Graphify candidate")
    target = destination.absolute()
    if target.name in {"", ".", ".."}:
        raise AdapterError("adapted candidate destination is invalid")
    parent, parent_fd, parent_identity = _open_stable_directory(
        target.parent, "adapted candidate parent"
    )
    try:
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise AdapterError("adapted candidate destination already exists")

        graph_payload = _read_regular_at(raw, "graph.json")
        report_payload = _read_regular_at(raw, "GRAPH_REPORT.md")
        html_payload = (
            _read_regular_at(raw, "graph.html") if manifest.track_html else None
        )
        document = _load_json(graph_payload)
        _reject_existing_wrapper_metadata(document)
        nodes, edges = _native_shape(document)
        try:
            graph_health = analyze_graph(nodes, edges)
        except IntegrityError as error:
            raise AdapterError("raw graph integrity is invalid") from error
        if graph_health.dangling_edges or graph_health.missing_endpoints:
            raise AdapterError("raw graph has invalid edge endpoints")
        represented = _represented_paths(nodes, edges, frozenset(staged.files))
        skipped = [
            {
                "path": path.as_posix(),
                "reason": f"not represented by Graphify {manifest.graphify_version}",
                "approved": False,
            }
            for path in staged.files
            if path not in represented
        ]
        document.update(
            {
                "project_id": manifest.project_id,
                "graphify_version": manifest.graphify_version,
                "source_digest": staged.source_digest,
                "graph_health": graph_health.to_dict(),
                "extraction_coverage": {
                    "schema_version": 1,
                    "total_staged_files": len(staged.files),
                    "represented_source_paths": [
                        path.as_posix() for path in sorted(represented)
                    ],
                    "skipped": skipped,
                },
            }
        )
        adapted_graph = (
            json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")

        target_fd: int | None = None
        target_identity: tuple[int, int] | None = None
        try:
            os.mkdir(target.name, mode=0o700, dir_fd=parent_fd)
            before = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            target_identity = _inode_identity(before)
            target_fd = os.open(target.name, _directory_flags(), dir_fd=parent_fd)
            opened = os.fstat(target_fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _inode_identity(before) != _inode_identity(opened)
            ):
                raise AdapterError("adapted candidate directory changed during creation")
            os.fchmod(target_fd, 0o700)
            _write_new_at(target_fd, "graph.json", adapted_graph)
            _write_new_at(target_fd, "GRAPH_REPORT.md", report_payload)
            if html_payload is not None:
                _write_new_at(target_fd, "graph.html", html_payload)
            os.fsync(target_fd)
            os.fsync(parent_fd)
            if post_write_check is not None:
                post_write_check()
            _require_directory_binding(parent, parent_identity)
        except BaseException as error:
            cleanup_error: OSError | AdapterError | None = None
            try:
                if target_identity is not None:
                    _remove_created_candidate(
                        parent_fd,
                        target_fd,
                        target.name,
                        target_identity,
                        include_html=html_payload is not None,
                    )
            except (OSError, AdapterError) as failure:
                cleanup_error = failure
            if target_fd is not None:
                os.close(target_fd)
            if cleanup_error is not None:
                raise AdapterError(
                    "adapted candidate cleanup failed and requires manual recovery"
                ) from cleanup_error
            if isinstance(error, AdapterError):
                raise
            if isinstance(error, OSError):
                raise AdapterError("unable to write adapted graph candidate") from error
            raise
        else:
            if target_fd is not None:
                os.close(target_fd)
    finally:
        os.close(parent_fd)

    return AdaptedCandidate(
        root=target,
        source_digest=staged.source_digest,
        node_count=len(nodes),
        edge_count=len(edges),
        skipped_count=len(skipped),
    )


def _reject_existing_wrapper_metadata(document: Mapping[str, Any]) -> None:
    for container in (document, document.get("metadata"), document.get("graph")):
        if isinstance(container, Mapping) and set(container) & _WRAPPER_FIELDS:
            raise AdapterError("raw graph already contains wrapper metadata")


def _native_shape(
    document: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes = document.get("nodes")
    if not isinstance(nodes, list) or not nodes or any(
        not isinstance(node, dict) for node in nodes
    ):
        raise AdapterError("raw graph nodes are invalid")
    if "links" in document and "edges" in document:
        raise AdapterError("raw graph contains conflicting edge collections")
    edges = document.get("links", document.get("edges"))
    if not isinstance(edges, list) or any(not isinstance(edge, dict) for edge in edges):
        raise AdapterError("raw graph edges are invalid")
    return nodes, edges


def _represented_paths(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    staged_files: frozenset[PurePosixPath],
) -> set[PurePosixPath]:
    represented: set[PurePosixPath] = set()

    def collect(value: Any, *, edge: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if (
                    key in _PATH_FIELDS
                    and not (edge and key in {"source", "target"})
                    and isinstance(item, str)
                    and item
                ):
                    path = _source_path(item)
                    if path not in staged_files:
                        raise AdapterError("graph source path is not in staged input")
                    represented.add(path)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for node in nodes:
        collect(node)
    for edge_value in edges:
        collect(edge_value, edge=True)
    return represented


def _source_path(value: str) -> PurePosixPath:
    if "\\" in value:
        raise AdapterError("graph source path must be confined")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise AdapterError("graph source path must be confined")
    return path


def _load_json(payload: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AdapterError("raw graph contains duplicate JSON keys")
            result[key] = value
        return result

    def finite(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise AdapterError("raw graph contains non-finite JSON")
        return parsed

    try:
        document = json.loads(
            payload,
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                AdapterError("raw graph contains malformed JSON")
            ),
            parse_float=finite,
        )
    except AdapterError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterError("raw graph contains malformed JSON") from error
    if not isinstance(document, dict):
        raise AdapterError("raw graph must be a JSON object")
    return document


def _read_regular_at(root: Path, name: str) -> bytes:
    root_fd = _open_directory(root)
    try:
        try:
            before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            descriptor = os.open(
                name, os.O_RDONLY | _nofollow_flag(), dir_fd=root_fd
            )
        except OSError as error:
            raise AdapterError(f"missing required Graphify artifact: {name}") from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
                raise AdapterError("Graphify artifacts must be stable regular files")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if _identity(opened) != _identity(after):
                raise AdapterError("Graphify artifact changed during capture")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(root_fd)


def _write_new_at(parent_fd: int, name: str, payload: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag(),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        raise AdapterError(f"{description} is unavailable") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise AdapterError(f"{description} must be a real directory")
    descriptor = _open_directory(absolute)
    try:
        opened = os.fstat(descriptor)
        after = absolute.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _inode_identity(info) != _inode_identity(opened)
            or _inode_identity(opened) != _inode_identity(after)
        ):
            raise AdapterError(f"{description} changed during open")
        return absolute, descriptor, _inode_identity(opened)
    except BaseException:
        os.close(descriptor)
        raise


def _require_directory_binding(path: Path, expected: tuple[int, int]) -> None:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as error:
        raise AdapterError("adapted candidate parent changed during write") from error
    if not stat.S_ISDIR(current.st_mode) or _inode_identity(current) != expected:
        raise AdapterError("adapted candidate parent changed during write")


def _remove_created_candidate(
    parent_fd: int,
    target_fd: int | None,
    name: str,
    expected: tuple[int, int],
    *,
    include_html: bool,
) -> None:
    if target_fd is not None:
        for artifact in (
            "graph.json",
            "GRAPH_REPORT.md",
            *(("graph.html",) if include_html else ()),
        ):
            try:
                os.unlink(artifact, dir_fd=target_fd)
            except FileNotFoundError:
                pass
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(current.st_mode) or _inode_identity(current) != expected:
        raise AdapterError("adapted candidate directory changed during cleanup")
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, _directory_flags())
    except OSError as error:
        raise AdapterError("unable to open directory safely") from error


def _directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    except AttributeError as error:
        raise AdapterError("platform lacks safe directory operations") from error


def _nofollow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as error:
        raise AdapterError("platform lacks safe file operations") from error


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev


def _inode_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino
