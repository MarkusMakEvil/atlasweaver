"""Immutable data contracts for project knowledge configuration."""

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID


FeatureSwitch = Literal["disabled", "enabled"]
ArtifactProvider = Literal["none", "github-release"]


@dataclass(frozen=True)
class FeatureIntent:
    """Optional repository integrations, disabled unless explicitly opted in."""

    atlas: FeatureSwitch = "disabled"
    registry: FeatureSwitch = "disabled"


@dataclass(frozen=True)
class ArtifactIntent:
    """Pinned remote artifact transport identity."""

    provider: ArtifactProvider = "none"
    host: str | None = None
    repository: str | None = None
    repository_id: int | None = None
    channel: str | None = None
    source_ref: str | None = None
    signer_workflow: str | None = None
    signer_digest: str | None = None


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
    project_uid: UUID | None = None
    features: FeatureIntent = field(default_factory=FeatureIntent)
    artifacts: ArtifactIntent = field(default_factory=ArtifactIntent)
