"""Read-only freshness and integrity checks for project knowledge artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Iterator, Literal

from .artifacts import OWNERSHIP_MANIFEST, validate_owned_graph
from .compatibility import GraphSemantics, resolve_graphify_compatibility
from .integrity import GraphIntegrity, IntegrityError, analyze_graph
from .models import ProjectManifest
from .locking import RepositoryAccess, RepositoryIdentity, open_repository_access
from .manifest import (
    assert_current_manifest_unchanged,
    require_current_manifest,
)
from .privacy import effective_excludes, is_denied
from .secrets_scan import load_secret_exceptions, scan_payload
from .staging import inspect_projection, iter_safe_files


CoreStatus = Literal["error", "missing", "stale", "partial", "healthy"]
HealthStatus = CoreStatus
FeatureStatus = Literal[
    "disabled", "available", "unavailable", "misconfigured"
]
_STABLE_ERROR_CODES = frozenset(
    {"source_inspection_failed", "registry_inspection_failed", "inspection_failed"}
)


@dataclass(frozen=True)
class SafeInputSnapshot:
    """A read-only digest of the same safe file projection staging would copy."""

    source_digest: str
    files: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class FeatureHealth:
    status: FeatureStatus
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {
            "disabled",
            "available",
            "unavailable",
            "misconfigured",
        } or type(self.issues) is not tuple or any(
            type(item) is not str or not item for item in self.issues
        ):
            raise ValueError("feature health is invalid")


@dataclass(frozen=True)
class TrustHealth:
    impact: Literal["trusted", "navigation"]
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.impact not in {"trusted", "navigation"} or type(
            self.limitations
        ) is not tuple or any(
            type(item) is not str or not item for item in self.limitations
        ):
            raise ValueError("trust health is invalid")


@dataclass(frozen=True)
class KnowledgeState:
    """Sanitized facts consumed by the deterministic schema-v2 classifier."""

    project_id: str
    manifest_schema_version: Literal[1, 2]
    artifact_schema_version: Literal[1, 2] | None
    graph_exists: bool
    graph_valid: bool
    graph_version: str | None
    current_source_digest: str | None
    graph_source_digest: str | None
    current_projection_digest: str | None
    graph_projection_digest: str | None
    features: Mapping[str, FeatureHealth]
    coverage_skips: int = 0
    unapproved_skips: int = 0
    impact_trust: Literal["trusted", "navigation"] = "navigation"
    impact_limitations: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def source_matches(self) -> bool:
        return (
            self.current_source_digest is not None
            and self.current_source_digest == self.graph_source_digest
        )

    @property
    def projection_matches(self) -> bool:
        if (
            self.manifest_schema_version == 1
            and self.artifact_schema_version == 1
            and self.graph_valid
            and self.current_projection_digest is None
            and self.graph_projection_digest is None
        ):
            return True
        return (
            self.current_projection_digest is not None
            and self.current_projection_digest == self.graph_projection_digest
        )


@dataclass(frozen=True)
class KnowledgeHealth:
    """Stable, path-free health result safe for CLI JSON and text output."""

    core_status: CoreStatus
    project_id: str
    graph_version: str | None
    source_matches: bool
    projection_matches: bool
    features: Mapping[str, FeatureHealth]
    trust: TrustHealth
    issues: tuple[str, ...]
    warnings: tuple[str, ...]
    schema_version: int = 2

    @property
    def status(self) -> CoreStatus:
        return self.core_status

    @property
    def atlas_available(self) -> bool:
        return self.features["atlas"].status == "available"

    @property
    def registry_matches(self) -> bool:
        return self.features["registry"].status == "available"

    @property
    def impact_analysis_trusted(self) -> bool:
        return self.trust.impact == "trusted"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "core_status": self.core_status,
            "status": self.core_status,
            "project_id": self.project_id,
            "graph_version": self.graph_version,
            "source_matches": self.source_matches,
            "projection_matches": self.projection_matches,
            "features": {
                name: {"status": value.status, "issues": list(value.issues)}
                for name, value in sorted(self.features.items())
            },
            "trust": {
                "impact": self.trust.impact,
                "limitations": list(self.trust.limitations),
            },
            "issues": list(self.issues),
            "warnings": list(self.warnings),
            "atlas_available": self.atlas_available,
            "registry_matches": self.registry_matches,
            "impact_analysis_trusted": self.impact_analysis_trusted,
        }


def assess_health(state: KnowledgeState) -> KnowledgeHealth:
    """Classify facts with error→missing→stale→partial→healthy precedence."""
    issues: list[str] = []
    warnings: list[str] = []
    if state.errors:
        for issue in state.errors:
            stable = issue if issue in _STABLE_ERROR_CODES else "inspection_failed"
            _append_unique(issues, stable)
    if state.graph_exists and not state.graph_valid:
        _append_unique(issues, "graph_invalid")
    if state.unapproved_skips:
        _append_unique(issues, "extraction_coverage_unapproved")

    if state.errors or (state.graph_exists and not state.graph_valid) or state.unapproved_skips:
        status: CoreStatus = "error"
    elif not state.graph_exists:
        _append_unique(issues, "graph_missing")
        status = "missing"
    elif not state.source_matches or not state.projection_matches:
        if not state.source_matches:
            _append_unique(issues, "source_digest_mismatch")
        if not state.projection_matches:
            _append_unique(issues, "projection_digest_mismatch")
        status = "stale"
    elif state.coverage_skips:
        _append_unique(issues, "extraction_coverage_partial")
        status = "partial"
    else:
        status = "healthy"

    for feature in state.features.values():
        if feature.status in {"unavailable", "misconfigured"}:
            for issue in feature.issues:
                _append_unique(warnings, issue)
    if state.impact_trust == "navigation":
        _append_unique(warnings, "impact_evidence_incomplete")
        for limitation in state.impact_limitations:
            _append_unique(warnings, limitation)

    return KnowledgeHealth(
        core_status=status,
        project_id=state.project_id,
        graph_version=state.graph_version,
        source_matches=state.source_matches,
        projection_matches=state.projection_matches,
        features=dict(state.features),
        trust=TrustHealth(state.impact_trust, state.impact_limitations),
        issues=tuple(issues),
        warnings=tuple(warnings),
    )


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def safe_input_snapshot(
    repo_root: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess | None = None,
) -> SafeInputSnapshot:
    """Recompute the safe-input digest without creating a staging directory."""
    if manifest.schema_version == 2:
        projection = inspect_projection(
            repo_root, manifest, repository_access=repository_access
        )
        return SafeInputSnapshot(
            projection.source_digest,
            tuple(item.path for item in projection.files),
        )
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
    atlas: FeatureHealth | None = None,
    registry: FeatureHealth | None = None,
    artifacts: FeatureHealth | None = None,
    repository_access: RepositoryAccess | None = None,
    expected_repository_identity: RepositoryIdentity | None = None,
    atlas_available: bool | None = None,
    registry_matches: bool | None = None,
) -> KnowledgeState:
    """Inspect current source and promoted graph without creating state."""
    if repository_access is not None and expected_repository_identity is not None:
        raise ValueError("repository authority is ambiguous")
    features = _feature_health(
        manifest,
        atlas=atlas,
        registry=registry,
        artifacts=artifacts,
        atlas_available=atlas_available,
        registry_matches=registry_matches,
    )
    with _use_repository_access(
        repo_root,
        repository_access=repository_access,
        expected_repository_identity=expected_repository_identity,
    ) as repository:
        current_manifest = require_current_manifest(
            repo_root, manifest, repository_access=repository
        )
        try:
            if current_manifest.schema_version == 2:
                projection = inspect_projection(
                    repo_root,
                    current_manifest,
                    repository_access=repository,
                )
                current_source_digest = projection.source_digest
                current_projection_digest = projection.projection_digest
            else:
                snapshot = safe_input_snapshot(repo_root, current_manifest)
                current_source_digest = snapshot.source_digest
                current_projection_digest = None
        except (OSError, ValueError, TypeError):
            assert_current_manifest_unchanged(
                repo_root, current_manifest, repository_access=repository
            )
            return _empty_state(
                current_manifest,
                features,
                errors=("source_inspection_failed",),
            )

        output = repo_root.absolute().joinpath(*current_manifest.output_dir.parts)
        if not _entry_exists(repository.descriptor, current_manifest.output_dir):
            assert_current_manifest_unchanged(
                repo_root, current_manifest, repository_access=repository
            )
            return _empty_state(
                current_manifest,
                features,
                source_digest=current_source_digest,
                projection_digest=current_projection_digest,
            )

        try:
            owned = validate_owned_graph(
                output,
                current_manifest,
                expected_source_digest=current_source_digest,
                expected_projection_digest=current_projection_digest,
                repository_access=repository,
            )
        except (OSError, TypeError, ValueError):
            assert_current_manifest_unchanged(
                repo_root, current_manifest, repository_access=repository
            )
            return _empty_state(
                current_manifest,
                features,
                graph_exists=True,
                source_digest=current_source_digest,
                projection_digest=current_projection_digest,
            )
        assert_current_manifest_unchanged(
            repo_root, current_manifest, repository_access=repository
        )
        artifact_schema = getattr(owned, "artifact_schema_version", 1)
        impact_trust = getattr(owned, "impact_trust", "navigation")
        limitations = getattr(owned, "impact_limitations", ())
        return KnowledgeState(
            project_id=current_manifest.project_id,
            manifest_schema_version=current_manifest.schema_version,
            artifact_schema_version=artifact_schema,
            graph_exists=True,
            graph_valid=True,
            graph_version=current_manifest.graphify_version,
            current_source_digest=current_source_digest,
            graph_source_digest=getattr(owned, "source_digest", None),
            current_projection_digest=current_projection_digest,
            graph_projection_digest=getattr(owned, "projection_digest", None),
            features=features,
            coverage_skips=getattr(owned, "skipped_count", 0),
            unapproved_skips=getattr(owned, "unapproved_skips", 0),
            impact_trust=(
                impact_trust if impact_trust in {"trusted", "navigation"} else "navigation"
            ),
            impact_limitations=(
                limitations if type(limitations) is tuple else ()
            ),
        )


def _empty_state(
    manifest: ProjectManifest,
    features: Mapping[str, FeatureHealth],
    *,
    graph_exists: bool = False,
    source_digest: str | None = None,
    projection_digest: str | None = None,
    errors: tuple[str, ...] = (),
) -> KnowledgeState:
    return KnowledgeState(
        project_id=manifest.project_id,
        manifest_schema_version=manifest.schema_version,
        artifact_schema_version=None,
        graph_exists=graph_exists,
        graph_valid=False,
        graph_version=None,
        current_source_digest=source_digest,
        graph_source_digest=None,
        current_projection_digest=projection_digest,
        graph_projection_digest=None,
        features=features,
        errors=errors,
    )


def _feature_health(
    manifest: ProjectManifest,
    *,
    atlas: FeatureHealth | None,
    registry: FeatureHealth | None,
    artifacts: FeatureHealth | None,
    atlas_available: bool | None,
    registry_matches: bool | None,
) -> Mapping[str, FeatureHealth]:
    if atlas is not None and atlas_available is not None:
        raise ValueError("atlas health is ambiguous")
    if registry is not None and registry_matches is not None:
        raise ValueError("registry health is ambiguous")

    def optional(
        supplied: FeatureHealth | None,
        enabled: bool,
        legacy: bool | None,
        issue: str,
    ) -> FeatureHealth:
        if supplied is not None:
            return supplied
        if legacy is not None:
            return FeatureHealth("available" if legacy else "unavailable", (() if legacy else (issue,)))
        if not enabled:
            return FeatureHealth("disabled")
        return FeatureHealth("unavailable", (issue,))

    artifact_enabled = manifest.artifacts.provider != "none"
    return {
        "atlas": optional(
            atlas,
            manifest.features.atlas == "enabled",
            atlas_available,
            "atlas_unavailable",
        ),
        "registry": optional(
            registry,
            manifest.features.registry == "enabled",
            registry_matches,
            "registry_mismatch",
        ),
        "artifacts": optional(
            artifacts,
            artifact_enabled,
            None,
            "artifact_provider_invalid",
        ),
    }


@contextmanager
def _use_repository_access(
    repo_root: Path,
    *,
    repository_access: RepositoryAccess | None,
    expected_repository_identity: RepositoryIdentity | None,
) -> Iterator[RepositoryAccess]:
    if repository_access is not None:
        yield repository_access
        return
    with open_repository_access(
        repo_root,
        expected_repository_identity=expected_repository_identity,
    ) as opened:
        yield opened


def _entry_exists(repository_descriptor: int, relative: PurePosixPath) -> bool:
    try:
        os.stat(
            relative.as_posix(),
            dir_fd=repository_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


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
