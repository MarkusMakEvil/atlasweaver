"""Strict loader for tracked Graphify project manifests."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
import re
from typing import Any

import yaml

from .compatibility import CompatibilityError, resolve_graphify_compatibility
from .models import ProjectManifest


class ManifestError(ValueError):
    """Raised when a project manifest violates the privacy contract."""


_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "display_name",
        "include_roots",
        "output_dir",
        "obsidian_namespace",
        "excludes",
        "track_html",
        "graphify_version",
    }
)
_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}\Z")


def confined_relative(value: str, field: str) -> PurePosixPath:
    """Return a non-empty POSIX path that cannot escape a repository."""
    if not isinstance(value, str):
        raise ManifestError(f"{field} must be a confined relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or value in {"", "."}:
        raise ManifestError(f"{field} must be a confined relative path")
    return path


def load_manifest(path: Path, repo_root: Path) -> ProjectManifest:
    """Load a complete, immutable manifest confined to *repo_root*."""
    if path.name != ".graphify-project.yaml":
        raise ManifestError("manifest filename must be .graphify-project.yaml")
    manifest_path = path.resolve()
    root = repo_root.resolve()
    try:
        manifest_path.relative_to(root)
    except ValueError as error:
        raise ManifestError("manifest must be located within repo_root") from error

    try:
        loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ManifestError(f"unable to read manifest: {manifest_path}") from error
    except yaml.YAMLError as error:
        raise ManifestError("manifest must be valid YAML") from error

    if not isinstance(loaded, Mapping):
        raise ManifestError("manifest must be a mapping")
    unknown = [key for key in loaded if not isinstance(key, str) or key not in _FIELDS]
    missing = _FIELDS - set(loaded)
    if unknown:
        raise ManifestError(
            f"manifest contains unknown keys: {', '.join(sorted(map(str, unknown)))}"
        )
    if missing:
        raise ManifestError(f"manifest is missing required keys: {', '.join(sorted(missing))}")

    schema_version = _required_int(loaded, "schema_version")
    if schema_version != 1:
        raise ManifestError("schema_version must be 1")

    project_id = _required_string(loaded, "project_id")
    if not _PROJECT_ID.fullmatch(project_id):
        raise ManifestError("project_id must match [a-z0-9][a-z0-9-]{1,62}")

    display_name = _required_string(loaded, "display_name")
    include_roots = _path_list(loaded, "include_roots")
    if not include_roots:
        raise ManifestError("include_roots must not be empty")
    if len(set(include_roots)) != len(include_roots):
        raise ManifestError("include_roots must not contain duplicates")

    output_dir = confined_relative(_required_string(loaded, "output_dir"), "output_dir")
    if output_dir != PurePosixPath("graphify-out"):
        raise ManifestError("output_dir must be graphify-out")
    obsidian_namespace = confined_relative(
        _required_string(loaded, "obsidian_namespace"), "obsidian_namespace"
    )
    if obsidian_namespace.parts != ("Projects", project_id, "Generated"):
        raise ManifestError(
            "obsidian_namespace must be Projects/{project_id}/Generated"
        )

    excludes = _string_list(loaded, "excludes")
    track_html = loaded["track_html"]
    if not isinstance(track_html, bool):
        raise ManifestError("track_html must be a boolean")

    graphify_version = _required_string(loaded, "graphify_version")
    try:
        resolve_graphify_compatibility(graphify_version)
    except CompatibilityError as error:
        raise ManifestError(str(error)) from error

    return ProjectManifest(
        schema_version=schema_version,
        project_id=project_id,
        display_name=display_name,
        include_roots=include_roots,
        output_dir=output_dir,
        obsidian_namespace=obsidian_namespace,
        excludes=excludes,
        track_html=track_html,
        graphify_version=graphify_version,
    )


def _required_string(values: Mapping[str, Any], field: str) -> str:
    value = values[field]
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field} must be a non-empty string")
    return value


def _required_int(values: Mapping[str, Any], field: str) -> int:
    value = values[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{field} must be an integer")
    return value


def _path_list(values: Mapping[str, Any], field: str) -> tuple[PurePosixPath, ...]:
    value = values[field]
    if not isinstance(value, list):
        raise ManifestError(f"{field} must be a list")
    return tuple(confined_relative(item, field) for item in value)


def _string_list(values: Mapping[str, Any], field: str) -> tuple[str, ...]:
    value = values[field]
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ManifestError(f"{field} must be a list of non-empty strings")
    return tuple(value)
