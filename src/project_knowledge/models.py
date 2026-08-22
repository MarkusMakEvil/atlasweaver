"""Immutable data contracts for project knowledge configuration."""

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class ProjectManifest:
    """Validated, repository-local Graphify project configuration."""

    schema_version: int
    project_id: str
    display_name: str
    include_roots: tuple[PurePosixPath, ...]
    output_dir: PurePosixPath
    obsidian_namespace: PurePosixPath
    excludes: tuple[str, ...]
    track_html: bool
    graphify_version: str
