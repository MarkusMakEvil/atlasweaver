"""Read-only freshness and integrity checks for project knowledge artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Literal

from .artifacts import OWNERSHIP_MANIFEST
from .compatibility import GraphSemantics, resolve_graphify_compatibility
from .integrity import GraphIntegrity, IntegrityError, analyze_graph
from .models import ProjectManifest
from .privacy import effective_excludes, is_denied
from .secrets_scan import load_secret_exceptions, scan_payload
from .staging import iter_safe_files


HealthStatus = Literal["error", "missing", "stale", "partial", "healthy"]
_STABLE_ERROR_CODES = frozenset(
    {"source_inspection_failed", "registry_inspection_failed", "inspection_failed"}
)


@dataclass(frozen=True)
class SafeInputSnapshot:
    """A read-only digest of the same safe file projection staging would copy."""

    source_digest: str
    files: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class KnowledgeState:
    """Sanitized facts consumed by the deterministic health classifier."""

    project_id: str
    graph_exists: bool
    graph_valid: bool
    graph_version: str | None
    current_source_digest: str | None
    graph_source_digest: str | None
    atlas_available: bool
    registry_matches: bool
    coverage_skips: int = 0
    unapproved_skips: int = 0
    impact_analysis_trusted: bool = False
    errors: tuple[str, ...] = ()

    @property
    def source_matches(self) -> bool:
        return (
            self.current_source_digest is not None
            and self.graph_source_digest is not None
            and self.current_source_digest == self.graph_source_digest
        )


@dataclass(frozen=True)
class KnowledgeHealth:
    """Stable, path-free health result safe for CLI JSON and text output."""

    status: HealthStatus
    project_id: str
    graph_version: str | None
    source_matches: bool
    atlas_available: bool
    registry_matches: bool
    impact_analysis_trusted: bool
    issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "project_id": self.project_id,
            "graph_version": self.graph_version,
            "source_matches": self.source_matches,
            "atlas_available": self.atlas_available,
            "registry_matches": self.registry_matches,
            "impact_analysis_trusted": self.impact_analysis_trusted,
            "issues": list(self.issues),
        }


def assess_health(state: KnowledgeState) -> KnowledgeHealth:
    """Classify facts with error→missing→stale→partial→healthy precedence."""
    issues: list[str] = []
    if state.errors:
        for issue in state.errors:
            stable = issue if issue in _STABLE_ERROR_CODES else "inspection_failed"
            if stable not in issues:
                issues.append(stable)
        status: HealthStatus = "error"
    elif not state.graph_exists or not state.graph_valid:
        issues.append("graph_missing" if not state.graph_exists else "graph_invalid")
        status = "missing"
    else:
        if not state.source_matches:
            issues.append("source_digest_mismatch")
        if not state.atlas_available:
            issues.append("atlas_unavailable")
        if not state.registry_matches:
            issues.append("registry_mismatch")
        if state.coverage_skips:
            issues.append("extraction_coverage_partial")
        if state.unapproved_skips:
            issues.append("extraction_coverage_unapproved")
        if not state.impact_analysis_trusted:
            issues.append("graph_integrity_degraded")
        if not state.source_matches:
            status = "stale"
        elif state.unapproved_skips:
            status = "error"
        elif (
            not state.atlas_available
            or not state.registry_matches
            or state.coverage_skips
            or not state.impact_analysis_trusted
        ):
            status = "partial"
        else:
            status = "healthy"
    return KnowledgeHealth(
        status=status,
        project_id=state.project_id,
        graph_version=state.graph_version,
        source_matches=state.source_matches,
        atlas_available=state.atlas_available,
        registry_matches=state.registry_matches,
        impact_analysis_trusted=state.impact_analysis_trusted,
        issues=tuple(issues),
    )


def safe_input_snapshot(repo_root: Path, manifest: ProjectManifest) -> SafeInputSnapshot:
    """Recompute the safe-input digest without creating a staging directory."""
    root_fd = _open_directory(repo_root.absolute())
    try:
        files = set(iter_safe_files(repo_root, manifest))
        digest = hashlib.sha256()
        ordered = tuple(sorted(files))
        findings = []
        for relative in ordered:
            payload = _read_stable_regular(root_fd, relative)
            findings.extend(scan_payload(relative, payload))
            digest.update(relative.as_posix().encode("utf-8") + b"\0" + payload + b"\0")
    finally:
        os.close(root_fd)
    finding_tuple = tuple(findings)
    accepted = load_secret_exceptions(repo_root, finding_tuple)
    if any(finding.fingerprint not in accepted for finding in finding_tuple):
        from .staging import SecretShapeError

        raise SecretShapeError("safe input contains secret-shaped material")
    return SafeInputSnapshot(digest.hexdigest(), ordered)


def inspect_project_state(
    repo_root: Path,
    manifest: ProjectManifest,
    *,
    atlas_available: bool = False,
    registry_matches: bool = False,
) -> KnowledgeState:
    """Inspect current source and promoted graph without writing either tree."""
    try:
        current = safe_input_snapshot(repo_root, manifest)
    except (OSError, ValueError):
        return KnowledgeState(
            project_id=manifest.project_id,
            graph_exists=False,
            graph_valid=False,
            graph_version=None,
            current_source_digest=None,
            graph_source_digest=None,
            atlas_available=atlas_available,
            registry_matches=registry_matches,
            errors=("source_inspection_failed",),
        )

    output = repo_root.absolute().joinpath(*manifest.output_dir.parts)
    exists = output.exists() or output.is_symlink()
    if not exists:
        return KnowledgeState(
            project_id=manifest.project_id,
            graph_exists=False,
            graph_valid=False,
            graph_version=None,
            current_source_digest=current.source_digest,
            graph_source_digest=None,
            atlas_available=atlas_available,
            registry_matches=registry_matches,
        )

    try:
        ownership, graph_payload = _read_valid_graph_metadata(output, manifest)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return KnowledgeState(
            project_id=manifest.project_id,
            graph_exists=True,
            graph_valid=False,
            graph_version=None,
            current_source_digest=current.source_digest,
            graph_source_digest=None,
            atlas_available=atlas_available,
            registry_matches=registry_matches,
        )

    del graph_payload
    return KnowledgeState(
        project_id=manifest.project_id,
        graph_exists=True,
        graph_valid=True,
        graph_version=str(ownership["graphify_version"]),
        current_source_digest=current.source_digest,
        graph_source_digest=str(ownership["source_digest"]),
        atlas_available=atlas_available,
        registry_matches=registry_matches,
        coverage_skips=int(ownership["skipped_count"]),
        unapproved_skips=int(ownership["unapproved_skips"]),
        impact_analysis_trusted=bool(ownership["impact_analysis_trusted"]),
    )


def _walk_safe_files(
    parent_fd: int,
    parts: tuple[str, ...],
    parent_relative: PurePosixPath,
    excludes: tuple[str, ...],
) -> tuple[PurePosixPath, ...]:
    if not parts:
        return ()
    name, *remaining = parts
    relative = parent_relative / name
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return ()
    if is_denied(relative, excludes) or stat.S_ISLNK(info.st_mode):
        return ()
    if remaining:
        if not stat.S_ISDIR(info.st_mode):
            return ()
        child_fd = _open_directory_at(parent_fd, name)
        try:
            if _has_git_marker(child_fd):
                return ()
            return _walk_safe_files(
                child_fd, tuple(remaining), relative, excludes
            )
        finally:
            os.close(child_fd)
    if stat.S_ISREG(info.st_mode):
        return (relative,)
    if not stat.S_ISDIR(info.st_mode):
        return ()
    child_fd = _open_directory_at(parent_fd, name)
    try:
        if _has_git_marker(child_fd):
            return ()
        with os.scandir(os.dup(child_fd)) as entries:
            children = sorted(entry.name for entry in entries)
        found: list[PurePosixPath] = []
        for child in children:
            found.extend(_walk_safe_files(child_fd, (child,), relative, excludes))
        return tuple(found)
    finally:
        os.close(child_fd)


def _read_stable_regular(root_fd: int, relative: PurePosixPath) -> bytes:
    parent_fd = os.dup(root_fd)
    try:
        for component in relative.parts[:-1]:
            child_fd = _open_directory_at(parent_fd, component)
            os.close(parent_fd)
            parent_fd = child_fd
        before = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(
            relative.name, os.O_RDONLY | _nofollow_flag(), dir_fd=parent_fd
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
                raise OSError("source changed during inspection")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
            if _identity(opened) != _identity(after):
                raise OSError("source changed during inspection")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _read_valid_graph_metadata(
    output: Path, manifest: ProjectManifest
) -> tuple[dict[str, Any], bytes]:
    output_fd = _open_directory(output)
    try:
        names: list[str] = []
        with os.scandir(os.dup(output_fd)) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if entry.name == "cache" and stat.S_ISDIR(info.st_mode):
                    _validate_query_cache(output_fd)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("invalid graph output entry")
                names.append(entry.name)
        ownership_payload = _read_regular_at(output_fd, OWNERSHIP_MANIFEST)
        ownership = json.loads(
            ownership_payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
        if not isinstance(ownership, dict):
            raise ValueError("invalid ownership metadata")
        expected_fields = {
            "schema_version",
            "project_id",
            "graphify_version",
            "source_digest",
            "graph_digest",
            "artifact_digests",
            "generated_at",
            "files",
            "skipped_count",
            "unapproved_skips",
            "impact_analysis_trusted",
        }
        if set(ownership) != expected_fields:
            raise ValueError("invalid ownership fields")
        if ownership["schema_version"] != 1:
            raise ValueError("invalid ownership schema")
        if not isinstance(ownership["impact_analysis_trusted"], bool):
            raise ValueError("invalid impact analysis trust state")
        if ownership["project_id"] != manifest.project_id:
            raise ValueError("project mismatch")
        if ownership["graphify_version"] != manifest.graphify_version:
            raise ValueError("version mismatch")
        for field in ("source_digest", "graph_digest"):
            value = ownership[field]
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError("invalid digest")
        files = ownership["files"]
        if (
            not isinstance(files, list)
            or any(not isinstance(value, str) for value in files)
            or files != sorted(names)
        ):
            raise ValueError("ownership file set mismatch")
        required = {OWNERSHIP_MANIFEST, "GRAPH_REPORT.md", "graph.json"}
        if manifest.track_html:
            required.add("graph.html")
        if not required.issubset(names):
            raise ValueError("required graph artifact missing")
        if not manifest.track_html and "graph.html" in names:
            raise ValueError("unexpected graph HTML")
        graph_payload = _read_regular_at(output_fd, "graph.json")
        if hashlib.sha256(graph_payload).hexdigest() != ownership["graph_digest"]:
            raise ValueError("graph digest mismatch")
        artifact_digests = ownership["artifact_digests"]
        artifact_names = sorted(name for name in names if name != OWNERSHIP_MANIFEST)
        if not isinstance(artifact_digests, dict) or sorted(artifact_digests) != artifact_names:
            raise ValueError("artifact digest set mismatch")
        for name, expected_digest in artifact_digests.items():
            if (
                not isinstance(expected_digest, str)
                or len(expected_digest) != 64
                or any(character not in "0123456789abcdef" for character in expected_digest)
                or hashlib.sha256(_read_regular_at(output_fd, name)).hexdigest()
                != expected_digest
            ):
                raise ValueError("artifact digest mismatch")
        if artifact_digests.get("graph.json") != ownership["graph_digest"]:
            raise ValueError("graph ownership mismatch")
        graph = json.loads(
            graph_payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
        _reject_nonfinite(graph)
        if not isinstance(graph, dict):
            raise ValueError("invalid graph")
        contract = resolve_graphify_compatibility(manifest.graphify_version)
        _validate_graph_integrity_metadata(graph, semantics=contract.semantics)
        impact_analysis_trusted = False
        if ownership["impact_analysis_trusted"] is not impact_analysis_trusted:
            raise ValueError("graph integrity ownership mismatch")
        coverage = graph.get("extraction_coverage")
        if not isinstance(coverage, dict) or not isinstance(coverage.get("skipped"), list):
            raise ValueError("invalid graph extraction coverage")
        skipped = coverage["skipped"]
        unapproved = sum(isinstance(item, dict) and item.get("approved") is False for item in skipped)
        if ownership["skipped_count"] != len(skipped) or ownership["unapproved_skips"] != unapproved:
            raise ValueError("graph extraction coverage ownership mismatch")
        return ownership, graph_payload
    finally:
        os.close(output_fd)


def _validate_graph_integrity_metadata(
    graph: dict[str, Any], *, semantics: GraphSemantics
) -> None:
    nodes = graph.get("nodes")
    if "edges" in graph and "links" in graph:
        raise ValueError("ambiguous graph edges")
    edges = graph.get("links", graph.get("edges"))
    if (
        not isinstance(nodes, list)
        or not nodes
        or any(not isinstance(node, dict) for node in nodes)
        or not isinstance(edges, list)
        or any(not isinstance(edge, dict) for edge in edges)
    ):
        raise ValueError("invalid graph shape")
    recorded = graph.get("graph_health")
    try:
        recorded_integrity = GraphIntegrity.from_dict(recorded)
        if recorded_integrity.collapsed_edges is not None:
            raise ValueError("unsupported collapsed edge evidence")
        computed = analyze_graph(
            nodes,
            edges,
            semantics=semantics,
            collapsed_edges=recorded_integrity.collapsed_edges,
        )
    except (IntegrityError, TypeError) as error:
        raise ValueError("invalid graph integrity metadata") from error
    if recorded_integrity != computed:
        raise ValueError("graph integrity metadata mismatch")
    if not computed.structurally_valid:
        raise ValueError("invalid graph structure")


def _validate_query_cache(output_fd: int) -> None:
    """Allow only the exact runtime sidecar written by ``graphify query``."""
    cache_fd = _open_directory_at(output_fd, "cache")
    try:
        with os.scandir(os.dup(cache_fd)) as entries:
            names = sorted(entry.name for entry in entries)
        if names != ["last_query_stamp"]:
            raise ValueError("invalid graph query cache")
        _read_regular_at(cache_fd, "last_query_stamp")
    finally:
        os.close(cache_fd)


def _read_regular_at(parent_fd: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | _nofollow_flag(), dir_fd=parent_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("expected regular file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite(key)
            _reject_nonfinite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_nonfinite(item)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _open_directory(path: Path) -> int:
    descriptor = os.open(path, _directory_flags())
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("expected directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_at(parent_fd: int, name: str) -> int:
    return os.open(name, _directory_flags(), dir_fd=parent_fd)


def _directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    except AttributeError as error:
        raise OSError("safe directory traversal unavailable") from error


def _nofollow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as error:
        raise OSError("safe file traversal unavailable") from error


def _has_git_marker(directory_fd: int) -> bool:
    try:
        os.stat(".git", dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev
