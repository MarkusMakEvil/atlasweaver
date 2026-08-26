"""Canonical fixtures shared by the core-adoption tests."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from uuid import UUID

from project_knowledge.models import ArtifactIntent, FeatureIntent, ProjectManifest


DEMO_UID = UUID("4ed9af24-5aa2-4eac-8d0a-3f622cc74948")


def manifest_v1() -> ProjectManifest:
    return ProjectManifest(
        schema_version=1,
        project_id="demo",
        display_name="Demo",
        include_roots=(PurePosixPath("src"),),
        output_dir=PurePosixPath("graphify-out"),
        obsidian_namespace=PurePosixPath("Projects/demo/Generated"),
        excludes=(),
        track_html=False,
        graphify_version="0.9.48",
    )


def manifest_v2(**changes: object) -> ProjectManifest:
    values: dict[str, object] = {
        **manifest_v1().__dict__,
        "schema_version": 2,
        "project_uid": DEMO_UID,
        "features": FeatureIntent(),
        "artifacts": ArtifactIntent(),
    }
    values.update(changes)
    return ProjectManifest(**values)  # type: ignore[arg-type]


def write_manifest_v2(repo: Path, **changes: object) -> Path:
    from project_knowledge.manifest import render_manifest_v2

    path = repo / ".graphify-project.yaml"
    path.write_bytes(render_manifest_v2(manifest_v2(**changes)))
    return path
