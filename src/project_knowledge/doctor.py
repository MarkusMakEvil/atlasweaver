"""Deterministic, path-free, read-only repository diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata, resources
from pathlib import Path
from typing import Literal

from .compatibility import resolve_graphify_compatibility
from .graphify import (
    CommandRunner,
    GraphifyContractError,
    GraphifyError,
    SubprocessCommandRunner,
    probe_graphify,
    resolve_graphify_executable,
)
from .health import assess_health, inspect_project_state
from .locking import RepositoryIdentity, open_repository_access
from .manifest import (
    ManifestError,
    assert_current_manifest_unchanged,
    inspect_init_journal,
    load_manifest,
)
from .models import ProjectManifest
from .staging import StagingError, inspect_projection


DiagnosticSeverity = Literal["error", "warning"]
_DIAGNOSTIC_ORDER = {
    code: index
    for index, code in enumerate(
        (
            "init_recovery_required",
            "manifest_invalid",
            "projection_invalid",
            "graphify_unavailable",
            "graphify_contract_mismatch",
            "agent_skill_unavailable",
            "agent_skill_mismatch",
            "graph_missing",
            "graph_invalid",
            "source_stale",
            "projection_stale",
            "artifact_provider_invalid",
        )
    )
}


@dataclass(frozen=True)
class DoctorDiagnostic:
    code: str
    severity: DiagnosticSeverity

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTIC_ORDER or self.severity not in {
            "error",
            "warning",
        }:
            raise ValueError("doctor diagnostic is invalid")


@dataclass(frozen=True)
class DoctorResult:
    status: Literal["ready", "issues"]
    package: Mapping[str, object]
    manifest: Mapping[str, object]
    projection: Mapping[str, object]
    graphify: Mapping[str, object]
    skill: Mapping[str, object]
    health: Mapping[str, object]
    diagnostics: tuple[DoctorDiagnostic, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "command": "doctor",
            "status": self.status,
            "package": dict(self.package),
            "manifest": dict(self.manifest),
            "projection": dict(self.projection),
            "graphify": dict(self.graphify),
            "skill": dict(self.skill),
            "health": dict(self.health),
            "diagnostics": [
                {"code": item.code, "severity": item.severity}
                for item in self.diagnostics
            ],
        }


def doctor_project(
    repo_root: Path,
    *,
    graphify_binary: Path | None = None,
    runner: CommandRunner | None = None,
    package_version: str | None = None,
    expected_repository_identity: RepositoryIdentity | None = None,
    expected_manifest: ProjectManifest | None = None,
) -> DoctorResult:
    """Inspect one retained repository authority without creating any state."""
    package = _package_document(package_version)
    selected_runner = runner or SubprocessCommandRunner()
    with open_repository_access(
        repo_root,
        expected_repository_identity=expected_repository_identity,
    ) as repository:
        if inspect_init_journal(repo_root, repository_access=repository) != "none":
            return _result(
                package,
                diagnostics=(DoctorDiagnostic("init_recovery_required", "error"),),
            )

        try:
            manifest = load_manifest(
                repo_root / ".graphify-project.yaml",
                repo_root,
                repository_access=repository,
            )
            if expected_manifest is not None and manifest != expected_manifest:
                raise ManifestError("project manifest changed", kind="changed")
        except ManifestError as error:
            if expected_manifest is not None or error.kind == "changed":
                raise
            return _result(
                package,
                diagnostics=(DoctorDiagnostic("manifest_invalid", "error"),),
            )

        manifest_document = _manifest_document(manifest)
        diagnostics: list[DoctorDiagnostic] = []
        projection_document: Mapping[str, object] = {}
        try:
            projection = inspect_projection(
                repo_root, manifest, repository_access=repository
            )
            projection_document = {
                "source_digest": projection.source_digest,
                "projection_digest": projection.projection_digest,
                "safe_file_count": len(projection.files),
            }
        except (OSError, StagingError, ValueError):
            diagnostics.append(DoctorDiagnostic("projection_invalid", "error"))
            projection = None
        assert_current_manifest_unchanged(
            repo_root, manifest, repository_access=repository
        )

        graphify_document: Mapping[str, object]
        try:
            executable = resolve_graphify_executable(graphify_binary)
        except GraphifyContractError:
            graphify_document = {"status": "unavailable", "version": None}
            diagnostics.append(DoctorDiagnostic("graphify_unavailable", "error"))
        else:
            try:
                capabilities = probe_graphify(
                    executable,
                    resolve_graphify_compatibility(manifest.graphify_version),
                    selected_runner,
                    before_exec=lambda: assert_current_manifest_unchanged(
                        repo_root, manifest, repository_access=repository
                    ),
                )
            except GraphifyError:
                graphify_document = {"status": "mismatch", "version": None}
                diagnostics.append(
                    DoctorDiagnostic("graphify_contract_mismatch", "error")
                )
            else:
                graphify_document = {
                    "status": "available",
                    "version": capabilities.version,
                }

        assert_current_manifest_unchanged(
            repo_root, manifest, repository_access=repository
        )
        skill_document, skill_diagnostic = _skill_document()
        if skill_diagnostic is not None:
            diagnostics.append(skill_diagnostic)

        if not _artifact_provider_valid(manifest):
            diagnostics.append(
                DoctorDiagnostic("artifact_provider_invalid", "error")
            )

        health_document: Mapping[str, object] = {}
        if projection is not None:
            state = inspect_project_state(
                repo_root,
                manifest,
                repository_access=repository,
            )
            health = assess_health(state)
            health_document = health.to_dict()
            if not state.graph_exists:
                diagnostics.append(DoctorDiagnostic("graph_missing", "warning"))
            elif not state.graph_valid:
                diagnostics.append(DoctorDiagnostic("graph_invalid", "error"))
            else:
                if not state.source_matches:
                    diagnostics.append(DoctorDiagnostic("source_stale", "warning"))
                if not state.projection_matches:
                    diagnostics.append(
                        DoctorDiagnostic("projection_stale", "warning")
                    )

        assert_current_manifest_unchanged(
            repo_root, manifest, repository_access=repository
        )
        return DoctorResult(
            status="issues" if diagnostics else "ready",
            package=package,
            manifest=manifest_document,
            projection=projection_document,
            graphify=graphify_document,
            skill=skill_document,
            health=health_document,
            diagnostics=_ordered_diagnostics(diagnostics),
        )


def _package_document(package_version: str | None) -> Mapping[str, object]:
    selected_version = (
        metadata.version("atlasweaver")
        if package_version is None
        else package_version
    )
    if type(selected_version) is not str or not selected_version:
        raise ValueError("package version is invalid")
    return {
        "version": selected_version,
        "manifest_schema": 2,
        "ownership_schema": 2,
        "evidence_schema": 1,
        "query_schema": 1,
    }


def _manifest_document(manifest: ProjectManifest) -> Mapping[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "project_id": manifest.project_id,
        "project_uid": (
            None if manifest.project_uid is None else str(manifest.project_uid)
        ),
        "graphify_version": manifest.graphify_version,
    }


def _skill_document() -> tuple[Mapping[str, object], DoctorDiagnostic | None]:
    try:
        skill = resources.files("project_knowledge").joinpath(
            "resources/skills/using-project-knowledge-graphs/SKILL.md"
        )
        payload = skill.read_bytes()
        if not payload or len(payload) > 262_144:
            raise ValueError("invalid packaged skill")
        payload.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return (
            {"status": "unavailable"},
            DoctorDiagnostic("agent_skill_unavailable", "warning"),
        )
    return {"status": "available", "schema_version": 1}, None


def _artifact_provider_valid(manifest: ProjectManifest) -> bool:
    intent = manifest.artifacts
    if intent.provider == "none":
        return all(
            value is None
            for value in (
                intent.host,
                intent.repository,
                intent.repository_id,
                intent.channel,
                intent.source_ref,
                intent.signer_workflow,
                intent.signer_digest,
            )
        )
    if intent.provider != "github-release":
        return False
    return all(
        value is not None
        for value in (
            intent.host,
            intent.repository,
            intent.repository_id,
            intent.channel,
            intent.source_ref,
            intent.signer_workflow,
            intent.signer_digest,
        )
    )


def _ordered_diagnostics(
    values: list[DoctorDiagnostic],
) -> tuple[DoctorDiagnostic, ...]:
    unique: dict[str, DoctorDiagnostic] = {}
    for value in values:
        unique.setdefault(value.code, value)
    return tuple(sorted(unique.values(), key=lambda item: _DIAGNOSTIC_ORDER[item.code]))


def _result(
    package: Mapping[str, object],
    *,
    diagnostics: tuple[DoctorDiagnostic, ...],
) -> DoctorResult:
    return DoctorResult(
        status="issues",
        package=package,
        manifest={},
        projection={},
        graphify={},
        skill={},
        health={},
        diagnostics=diagnostics,
    )
