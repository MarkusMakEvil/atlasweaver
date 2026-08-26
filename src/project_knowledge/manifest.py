"""Strict, descriptor-read loader for tracked project manifests."""

from __future__ import annotations

from collections.abc import Mapping
import json
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Literal
from uuid import UUID, RFC_4122

import yaml

from .compatibility import CompatibilityError, resolve_graphify_compatibility
from .locking import RepositoryAccess, _ManifestBinding
from .models import ArtifactIntent, FeatureIntent, ProjectManifest


ManifestFailureKind = Literal["invalid", "changed"]


class ManifestError(ValueError):
    """Raised when a project manifest violates its closed wire contract."""

    def __init__(
        self, message: str, *, kind: ManifestFailureKind = "invalid"
    ) -> None:
        if kind not in {"invalid", "changed"}:
            raise ValueError("manifest error kind is invalid")
        super().__init__(message)
        self.kind: ManifestFailureKind = kind


MAX_MANIFEST_BYTES = 262_144
V1_FIELDS = frozenset(
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
V2_FIELDS = frozenset({*V1_FIELDS, "project_uid", "features", "artifacts"})
FEATURE_FIELDS = frozenset({"atlas", "registry"})
GITHUB_FIELDS = frozenset(
    {
        "provider",
        "host",
        "repository",
        "repository_id",
        "channel",
        "source_ref",
        "signer_workflow",
        "signer_digest",
    }
)
_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}\Z")
_REPOSITORY_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_CHANNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")


class _StrictLoader(yaml.SafeLoader):
    def compose_node(
        self, parent: yaml.Node | None, index: int | None
    ) -> yaml.Node:
        if self.check_event(yaml.AliasEvent):
            raise ManifestError("YAML aliases are forbidden")
        return super().compose_node(parent, index)

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        if any(key.tag == "tag:yaml.org,2002:merge" for key, _ in node.value):
            raise ManifestError("YAML merge keys are forbidden")
        super().flatten_mapping(node)


def _construct_unique_mapping(
    loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise ManifestError("YAML merge keys are forbidden")
        key = loader.construct_object(key_node, deep=deep)
        if type(key) is not str:
            raise ManifestError("unknown or non-string YAML mapping key")
        if key in result:
            raise ManifestError("duplicate YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_one_yaml(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes or len(payload) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest exceeds its byte cap")
    try:
        value = yaml.load(payload.decode("utf-8"), Loader=_StrictLoader)
    except ManifestError:
        raise
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ManifestError(
            "manifest must be one valid UTF-8 YAML document"
        ) from error
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ManifestError("manifest must be a string-keyed mapping")
    return value


def load_manifest_payload(payload: bytes, repo_root: Path) -> ProjectManifest:
    """Parse already-owned manifest bytes without reopening a path."""
    del repo_root
    values = _load_one_yaml(payload)
    schema_version = values.get("schema_version")
    if type(schema_version) is not int:
        raise ManifestError("schema_version must be an integer")
    if schema_version == 1:
        return _parse_v1(values)
    if schema_version == 2:
        return _parse_v2(values)
    raise ManifestError("schema_version must be 1 or 2")


def load_manifest(
    path: Path,
    repo_root: Path,
    *,
    repository_access: RepositoryAccess | None = None,
) -> ProjectManifest:
    """Descriptor-read the canonical manifest and parse its exact bytes."""
    if path.name != ".graphify-project.yaml":
        raise ManifestError("manifest filename must be .graphify-project.yaml")
    if repository_access is not None:
        payload, binding = _capture_manifest_binding(repository_access.descriptor)
        try:
            parsed = load_manifest_payload(payload, repo_root)
        except BaseException:
            binding.close()
            raise
        repository_access._replace_manifest_binding(binding)
        return parsed
    try:
        root = repo_root.absolute()
        if path.absolute() != root / ".graphify-project.yaml":
            raise ManifestError("manifest must be located within repo_root")
        root_fd = os.open(
            root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except ManifestError:
        raise
    except OSError as error:
        raise ManifestError("unable to read manifest") from error

    descriptor = -1
    try:
        try:
            descriptor = os.open(
                ".graphify-project.yaml",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
            before = os.fstat(descriptor)
            named = os.stat(
                ".graphify-project.yaml", dir_fd=root_fd, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise ManifestError("manifest must be a stable regular file")
            if before.st_size > MAX_MANIFEST_BYTES:
                raise ManifestError("manifest exceeds its byte cap")
            payload = _read_capped(descriptor, MAX_MANIFEST_BYTES)
            after = os.fstat(descriptor)
            if _file_identity(before) != _file_identity(after):
                raise ManifestError("manifest changed while reading")
        except ManifestError:
            raise
        except OSError as error:
            raise ManifestError("unable to read manifest") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(root_fd)
    return load_manifest_payload(payload, root)


def require_current_manifest(
    repo_root: Path,
    supplied: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> ProjectManifest:
    """Pin the current manifest and require the caller's contract to match it."""
    current = load_manifest(
        repo_root / ".graphify-project.yaml",
        repo_root,
        repository_access=repository_access,
    )
    if current != supplied:
        raise ManifestError("project manifest changed", kind="changed")
    return current


def assert_current_manifest_unchanged(
    repo_root: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> None:
    """Recheck the retained manifest fd and its descriptor-relative entry."""
    del repo_root
    binding = repository_access._manifest_binding
    if binding is None or binding.descriptor < 0:
        raise ManifestError("project manifest changed", kind="changed")
    candidate = -1
    try:
        retained = os.fstat(binding.descriptor)
        if _binding_metadata(retained) != (
            binding.identity,
            binding.size,
            binding.mtime_ns,
            binding.ctime_ns,
        ):
            raise ManifestError("project manifest changed", kind="changed")
        retained_payload = _pread_capped(binding.descriptor, MAX_MANIFEST_BYTES)
        if hashlib.sha256(retained_payload).hexdigest() != binding.sha256:
            raise ManifestError("project manifest changed", kind="changed")
        candidate = os.open(
            ".graphify-project.yaml",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=repository_access.descriptor,
        )
        info = os.fstat(candidate)
        if _binding_metadata(info) != (
            binding.identity,
            binding.size,
            binding.mtime_ns,
            binding.ctime_ns,
        ):
            raise ManifestError("project manifest changed", kind="changed")
        payload = _pread_capped(candidate, MAX_MANIFEST_BYTES)
        if (
            hashlib.sha256(payload).hexdigest() != binding.sha256
            or load_manifest_payload(payload, Path(".")) != manifest
        ):
            raise ManifestError("project manifest changed", kind="changed")
    except ManifestError as error:
        if error.kind == "changed":
            raise
        raise ManifestError("project manifest changed", kind="changed") from None
    except (OSError, ValueError):
        raise ManifestError("project manifest changed", kind="changed") from None
    finally:
        if candidate >= 0:
            os.close(candidate)


def _capture_manifest_binding(root_descriptor: int) -> tuple[bytes, _ManifestBinding]:
    descriptor = -1
    try:
        descriptor = os.open(
            ".graphify-project.yaml",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_descriptor,
        )
        before = os.fstat(descriptor)
        named = os.stat(
            ".graphify-project.yaml",
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            or before.st_size > MAX_MANIFEST_BYTES
        ):
            raise ManifestError("project manifest changed", kind="changed")
        first = _pread_capped(descriptor, MAX_MANIFEST_BYTES)
        middle = os.fstat(descriptor)
        second = _pread_capped(descriptor, MAX_MANIFEST_BYTES)
        after = os.fstat(descriptor)
        rebound = os.stat(
            ".graphify-project.yaml",
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            _file_identity(before) != _file_identity(middle)
            or _file_identity(before) != _file_identity(after)
            or (before.st_dev, before.st_ino) != (rebound.st_dev, rebound.st_ino)
            or first != second
        ):
            raise ManifestError("project manifest changed", kind="changed")
        digest = hashlib.sha256(first).hexdigest()
        return first, _ManifestBinding(
            descriptor=descriptor,
            identity=(before.st_dev, before.st_ino),
            size=before.st_size,
            mtime_ns=before.st_mtime_ns,
            ctime_ns=before.st_ctime_ns,
            sha256=digest,
        )
    except ManifestError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise ManifestError("project manifest changed", kind="changed") from None


def confined_relative(value: str, field: str) -> PurePosixPath:
    """Return a byte-canonical POSIX relative path confined to its root."""
    value = _nonempty_string(value, field)
    if (
        "\\" in value
        or _DRIVE_PREFIX.match(value)
        or value.startswith("/")
        or _contains_control(value)
    ):
        raise ManifestError(f"{field} must be a confined relative path")
    path = PurePosixPath(value)
    if (
        not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ManifestError(f"{field} must be a confined relative path")
    return path


def validate_project_excludes(values: object) -> tuple[str, ...]:
    """Validate the shared, non-negated canonical glob-pattern boundary."""
    if type(values) not in {list, tuple}:
        raise ManifestError("excludes must be a finite list of patterns")
    result: list[str] = []
    for value in values:
        if type(value) is not str or not value or value.startswith("!"):
            raise ManifestError("excludes contain an invalid pattern")
        if (
            "\\" in value
            or value.startswith("/")
            or _DRIVE_PREFIX.match(value)
            or _contains_control(value)
        ):
            raise ManifestError("excludes contain an invalid pattern")
        path = PurePosixPath(value)
        if (
            not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != value
        ):
            raise ManifestError("excludes contain an invalid pattern")
        result.append(value)
    return tuple(result)


def render_manifest_v2(manifest: ProjectManifest) -> bytes:
    """Render one validated schema-v2 manifest with fixed field ordering."""
    validated = _validate_runtime_manifest_v2(manifest)
    lines = [
        "schema_version: 2",
        f"project_id: {_yaml_scalar(validated.project_id)}",
        f"project_uid: {validated.project_uid}",
        f"display_name: {_yaml_scalar(validated.display_name)}",
        "include_roots:",
        *(
            f"  - {_yaml_scalar(path.as_posix())}"
            for path in validated.include_roots
        ),
        "output_dir: graphify-out",
        f"obsidian_namespace: {_yaml_scalar(validated.obsidian_namespace.as_posix())}",
        *(
            ["excludes: []"]
            if not validated.excludes
            else [
                "excludes:",
                *(f"  - {_yaml_scalar(value)}" for value in validated.excludes),
            ]
        ),
        f"track_html: {'true' if validated.track_html else 'false'}",
        f"graphify_version: {_yaml_scalar(validated.graphify_version)}",
        "features:",
        f"  atlas: {validated.features.atlas}",
        f"  registry: {validated.features.registry}",
        "artifacts:",
        f"  provider: {validated.artifacts.provider}",
    ]
    if validated.artifacts.provider == "github-release":
        for key in (
            "host",
            "repository",
            "repository_id",
            "channel",
            "source_ref",
            "signer_workflow",
            "signer_digest",
        ):
            value = getattr(validated.artifacts, key)
            lines.append(
                f"  {key}: {value if type(value) is int else _yaml_scalar(value)}"
            )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _parse_v1(values: Mapping[str, object]) -> ProjectManifest:
    return ProjectManifest(schema_version=1, **_parse_common(values, V1_FIELDS))


def _parse_v2(values: Mapping[str, object]) -> ProjectManifest:
    common = _parse_common(values, V2_FIELDS)
    return ProjectManifest(
        schema_version=2,
        project_uid=_uuid4(values["project_uid"]),
        features=_parse_features(values["features"]),
        artifacts=_parse_artifacts(values["artifacts"]),
        **common,
    )


def _parse_common(
    values: Mapping[str, object], fields: frozenset[str]
) -> dict[str, Any]:
    unknown = set(values) - fields
    missing = fields - set(values)
    if unknown:
        raise ManifestError(
            f"manifest contains unknown keys: {', '.join(sorted(unknown))}"
        )
    if missing:
        raise ManifestError(
            f"manifest is missing required keys: {', '.join(sorted(missing))}"
        )
    expected_schema = 2 if fields is V2_FIELDS else 1
    if (
        type(values["schema_version"]) is not int
        or values["schema_version"] != expected_schema
    ):
        raise ManifestError(f"schema_version must be {expected_schema}")
    project_id = _nonempty_string(values["project_id"], "project_id")
    if not _PROJECT_ID.fullmatch(project_id):
        raise ManifestError("project_id must match [a-z0-9][a-z0-9-]{1,62}")
    display_name = _nonempty_string(values["display_name"], "display_name")
    include_roots = _path_list(values["include_roots"], "include_roots")
    if not include_roots:
        raise ManifestError("include_roots must not be empty")
    if len(set(include_roots)) != len(include_roots):
        raise ManifestError("include_roots must not contain duplicates")
    output_dir = confined_relative(
        _nonempty_string(values["output_dir"], "output_dir"), "output_dir"
    )
    if output_dir != PurePosixPath("graphify-out"):
        raise ManifestError("output_dir must be graphify-out")
    obsidian_namespace = confined_relative(
        _nonempty_string(values["obsidian_namespace"], "obsidian_namespace"),
        "obsidian_namespace",
    )
    if obsidian_namespace.parts != ("Projects", project_id, "Generated"):
        raise ManifestError(
            "obsidian_namespace must be Projects/{project_id}/Generated"
        )
    excludes = validate_project_excludes(values["excludes"])
    track_html = values["track_html"]
    if type(track_html) is not bool:
        raise ManifestError("track_html must be a boolean")
    graphify_version = _graphify_version(values["graphify_version"])
    return {
        "project_id": project_id,
        "display_name": display_name,
        "include_roots": include_roots,
        "output_dir": output_dir,
        "obsidian_namespace": obsidian_namespace,
        "excludes": excludes,
        "track_html": track_html,
        "graphify_version": graphify_version,
    }


def _parse_features(value: object) -> FeatureIntent:
    if type(value) is not dict or set(value) != FEATURE_FIELDS:
        raise ManifestError("feature intent schema is invalid")
    atlas = value["atlas"]
    registry = value["registry"]
    if type(atlas) is not str or atlas not in {"disabled", "enabled"}:
        raise ManifestError("feature intent is invalid")
    if type(registry) is not str or registry not in {"disabled", "enabled"}:
        raise ManifestError("feature intent is invalid")
    return FeatureIntent(atlas=atlas, registry=registry)


def _parse_artifacts(value: object) -> ArtifactIntent:
    if type(value) is not dict or "provider" not in value:
        raise ManifestError("artifact transport schema is invalid")
    provider = value["provider"]
    if type(provider) is str and provider == "none":
        if set(value) != {"provider"}:
            raise ManifestError("artifact transport schema is invalid")
        return ArtifactIntent()
    if type(provider) is not str or provider != "github-release":
        raise ManifestError("artifact transport schema is invalid")
    if set(value) != GITHUB_FIELDS:
        raise ManifestError("artifact transport schema is invalid")
    host = _nonempty_string(value["host"], "artifact host")
    repository = _nonempty_string(value["repository"], "artifact repository")
    repository_id = value["repository_id"]
    channel = _nonempty_string(value["channel"], "artifact channel")
    source_ref = _nonempty_string(value["source_ref"], "artifact source_ref")
    signer_workflow = _nonempty_string(
        value["signer_workflow"], "artifact signer_workflow"
    )
    signer_digest = _nonempty_string(
        value["signer_digest"], "artifact signer_digest"
    )
    repository_parts = repository.split("/")
    valid_repository = (
        len(repository_parts) == 2
        and all(_REPOSITORY_COMPONENT.fullmatch(part) for part in repository_parts)
        and not _contains_control(repository)
    )
    workflow_prefix = f"{repository}/.github/workflows/"
    workflow_name = signer_workflow.removeprefix(workflow_prefix)
    if (
        host != "github.com"
        or not valid_repository
        or type(repository_id) is not int
        or repository_id <= 0
        or not _CHANNEL.fullmatch(channel)
        or not source_ref.startswith("refs/heads/")
        or not _valid_ref_tail(source_ref.removeprefix("refs/heads/"))
        or not signer_workflow.startswith(workflow_prefix)
        or "/" in workflow_name
        or not workflow_name.endswith(".yml")
        or not _REPOSITORY_COMPONENT.fullmatch(workflow_name)
        or not _SHA1.fullmatch(signer_digest)
    ):
        raise ManifestError("artifact transport identity is invalid")
    return ArtifactIntent(
        provider="github-release",
        host=host,
        repository=repository,
        repository_id=repository_id,
        channel=channel,
        source_ref=source_ref,
        signer_workflow=signer_workflow,
        signer_digest=signer_digest,
    )


def _validate_runtime_manifest_v2(manifest: ProjectManifest) -> ProjectManifest:
    if type(manifest) is not ProjectManifest:
        raise ManifestError("manifest runtime object is invalid")
    if type(manifest.project_uid) is not UUID:
        raise ManifestError("project_uid must be a UUIDv4")
    if type(manifest.features) is not FeatureIntent:
        raise ManifestError("feature intent schema is invalid")
    if type(manifest.artifacts) is not ArtifactIntent:
        raise ManifestError("artifact transport schema is invalid")
    if type(manifest.include_roots) is not tuple:
        raise ManifestError("include_roots must be a tuple of POSIX paths")
    if any(type(path) is not PurePosixPath for path in manifest.include_roots):
        raise ManifestError("include_roots must contain POSIX paths")
    if type(manifest.output_dir) is not PurePosixPath:
        raise ManifestError("output_dir must be a POSIX path")
    if type(manifest.obsidian_namespace) is not PurePosixPath:
        raise ManifestError("obsidian_namespace must be a POSIX path")
    if type(manifest.excludes) is not tuple:
        raise ManifestError("excludes must be a tuple of patterns")
    artifact_values = {"provider": manifest.artifacts.provider}
    if manifest.artifacts.provider == "github-release":
        artifact_values.update(
            {
                key: getattr(manifest.artifacts, key)
                for key in GITHUB_FIELDS - {"provider"}
            }
        )
    elif any(
        getattr(manifest.artifacts, key) is not None
        for key in GITHUB_FIELDS - {"provider"}
    ):
        raise ManifestError("artifact transport schema is invalid")
    values: dict[str, object] = {
        "schema_version": manifest.schema_version,
        "project_id": manifest.project_id,
        "project_uid": str(manifest.project_uid),
        "display_name": manifest.display_name,
        "include_roots": [path.as_posix() for path in manifest.include_roots],
        "output_dir": manifest.output_dir.as_posix(),
        "obsidian_namespace": manifest.obsidian_namespace.as_posix(),
        "excludes": list(manifest.excludes),
        "track_html": manifest.track_html,
        "graphify_version": manifest.graphify_version,
        "features": {
            "atlas": manifest.features.atlas,
            "registry": manifest.features.registry,
        },
        "artifacts": artifact_values,
    }
    return _parse_v2(values)


def _uuid4(value: object) -> UUID:
    if type(value) is not str or not value or _contains_control(value):
        raise ManifestError("project_uid must be a UUIDv4")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError) as error:
        raise ManifestError("project_uid must be a UUIDv4") from error
    if parsed.version != 4 or parsed.variant != RFC_4122 or str(parsed) != value:
        raise ManifestError("project_uid must be a canonical UUIDv4")
    return parsed


def _graphify_version(value: object) -> str:
    version = _nonempty_string(value, "graphify_version")
    try:
        resolve_graphify_compatibility(version)
    except CompatibilityError as error:
        raise ManifestError(str(error)) from error
    return version


def _path_list(value: object, field: str) -> tuple[PurePosixPath, ...]:
    if type(value) is not list:
        raise ManifestError(f"{field} must be a list")
    return tuple(confined_relative(item, field) for item in value)


def _nonempty_string(value: object, field: str) -> str:
    if type(value) is not str or not value.strip() or _contains_control(value):
        raise ManifestError(f"{field} must be a non-empty string")
    return value


def _valid_ref_tail(value: str) -> bool:
    if not value or "\\" in value or _contains_control(value):
        return False
    path = PurePosixPath(value)
    return (
        bool(path.parts)
        and all(part not in {"", ".", ".."} for part in path.parts)
        and path.as_posix() == value
    )


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _yaml_scalar(value: object) -> str:
    if type(value) is not str or not value or _contains_control(value):
        raise ManifestError("manifest scalar is invalid")
    return json.dumps(value, ensure_ascii=False)


def _read_capped(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise ManifestError("manifest exceeds its byte cap")
    return payload


def _pread_capped(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while offset <= limit:
        chunk = os.pread(descriptor, min(65_536, limit + 1 - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise ManifestError("manifest exceeds its byte cap")
    return payload


def _binding_metadata(
    info: os.stat_result,
) -> tuple[tuple[int, int], int, int, int]:
    return (
        (info.st_dev, info.st_ino),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
