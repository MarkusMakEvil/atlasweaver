from dataclasses import replace
from pathlib import Path, PurePosixPath
from uuid import UUID

import pytest

from project_knowledge.manifest import (
    ManifestError,
    load_manifest,
    load_manifest_payload,
    render_manifest_v2,
)
from project_knowledge.models import ArtifactIntent, FeatureIntent
from tests.support import DEMO_UID, manifest_v2, write_manifest_v2


def test_v2_round_trip_is_canonical_and_closed(tmp_path: Path) -> None:
    path = write_manifest_v2(tmp_path)

    loaded = load_manifest(path, tmp_path)

    assert loaded == manifest_v2()
    assert loaded.project_uid == DEMO_UID
    assert loaded.features.registry == "disabled"
    assert loaded.artifacts.provider == "none"
    assert render_manifest_v2(loaded) == path.read_bytes()


def test_byte_owned_manifest_parser_matches_descriptor_loader(tmp_path: Path) -> None:
    payload = render_manifest_v2(manifest_v2())
    path = tmp_path / ".graphify-project.yaml"
    path.write_bytes(payload)

    assert load_manifest_payload(payload, tmp_path) == load_manifest(path, tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        b"schema_version: 2\nschema_version: 2\n",
        b"schema_version: 2\nfeatures: &f {atlas: disabled, registry: disabled}\ncopy: *f\n",
        b"schema_version: 2\nfeatures: {atlas: false, registry: disabled}\n",
        b"schema_version: 2\nfeatures: {atlas: disabled, registry: disabled, extra: x}\n",
        b"schema_version: 2\n---\nschema_version: 2\n",
        b"? [unhashable]\n: value\n",
        b"nested:\n  1: value\n",
        b"base: &base {atlas: disabled}\nfeatures:\n  <<: *base\n",
    ],
)
def test_v2_rejects_duplicates_aliases_wrong_types_unknowns_and_documents(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / ".graphify-project.yaml"
    path.write_bytes(payload)

    with pytest.raises(ManifestError):
        load_manifest(path, tmp_path)
    with pytest.raises(ManifestError):
        load_manifest_payload(payload, tmp_path)


def test_manifest_payload_and_path_enforce_the_same_byte_cap(tmp_path: Path) -> None:
    payload = b"x" * 262_145
    path = tmp_path / ".graphify-project.yaml"
    path.write_bytes(payload)

    with pytest.raises(ManifestError, match="byte cap"):
        load_manifest_payload(payload, tmp_path)
    with pytest.raises(ManifestError, match="byte cap"):
        load_manifest(path, tmp_path)


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        ('  - "src"', '  - "C:/src"'),
        ('  - "src"', '  - "src\\\\nested"'),
        ('  - "src"', '  - "src//nested"'),
        ('  - "src"', '  - "src/./nested"'),
        ('  - "src"', '  - "src/../nested"'),
        ('display_name: "Demo"', 'display_name: "bad\\u0000name"'),
    ],
)
def test_v2_rejects_noncanonical_paths_and_control_characters(
    tmp_path: Path, needle: str, replacement: str
) -> None:
    payload = render_manifest_v2(manifest_v2()).decode("utf-8")
    payload = payload.replace(needle, replacement)

    with pytest.raises(ManifestError):
        load_manifest_payload(payload.encode("utf-8"), tmp_path)


def test_github_provider_requires_exact_transport_identity(tmp_path: Path) -> None:
    path = write_manifest_v2(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        "artifacts:\n  provider: none\n",
        "artifacts:\n  provider: github-release\n  host: github.com\n",
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ManifestError, match="artifact transport"):
        load_manifest(path, tmp_path)


def test_v1_maps_optional_intent_without_portable_identity(tmp_path: Path) -> None:
    from tests.test_manifest import write_manifest

    loaded = load_manifest(write_manifest(tmp_path), tmp_path)

    assert loaded.project_uid is None
    assert loaded.features.atlas == loaded.features.registry == "disabled"
    assert loaded.artifacts.provider == "none"


def test_renderer_revalidates_runtime_objects_instead_of_annotations() -> None:
    bad_values = (
        replace(manifest_v2(), schema_version=True),
        replace(manifest_v2(), project_uid=UUID("00000000-0000-1000-8000-000000000000")),
        replace(manifest_v2(), include_roots=(PurePosixPath("src/../escape"),)),
        replace(manifest_v2(), include_roots=[PurePosixPath("src")]),  # type: ignore[arg-type]
        replace(manifest_v2(), features=FeatureIntent(atlas="sometimes")),  # type: ignore[arg-type]
        replace(manifest_v2(), artifacts=ArtifactIntent(provider="github-release")),
        replace(
            manifest_v2(),
            artifacts=ArtifactIntent(provider="none", host="github.com"),
        ),
        replace(
            manifest_v2(),
            artifacts=ArtifactIntent(
                provider="github-release",
                host="github.com",
                repository="owner/repo",
                repository_id=True,  # type: ignore[arg-type]
                channel="stable",
                source_ref="refs/heads/main",
                signer_workflow="owner/repo/.github/workflows/release.yml",
                signer_digest="a" * 40,
            ),
        ),
    )

    for manifest in bad_values:
        with pytest.raises(ManifestError):
            render_manifest_v2(manifest)


def test_manifest_error_kind_is_closed() -> None:
    assert ManifestError("bad").kind == "invalid"
    assert ManifestError("changed", kind="changed").kind == "changed"
    with pytest.raises(ValueError):
        ManifestError("bad", kind="forged")  # type: ignore[arg-type]
