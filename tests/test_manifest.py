from pathlib import Path, PurePosixPath

import pytest

from project_knowledge.manifest import ManifestError, load_manifest


def write_manifest(tmp_path: Path, **overrides: object) -> Path:
    values: dict[str, object] = {
        "schema_version": 1,
        "project_id": "demo-project",
        "display_name": "Demo Project",
        "include_roots": ["src", "docs"],
        "output_dir": "graphify-out",
        "obsidian_namespace": "Projects/demo-project/Generated",
        "excludes": ["generated/**"],
        "track_html": True,
        "graphify_version": "0.9.48",
    }
    values.update(overrides)
    path = tmp_path / ".graphify-project.yaml"
    lines: list[str] = []
    for key, value in values.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_manifest_accepts_confined_paths(tmp_path: Path) -> None:
    loaded = load_manifest(write_manifest(tmp_path), tmp_path)

    assert loaded.output_dir == PurePosixPath("graphify-out")
    assert loaded.obsidian_namespace == PurePosixPath(
        "Projects/demo-project/Generated"
    )


def test_manifest_requires_the_canonical_filename(tmp_path: Path) -> None:
    canonical = write_manifest(tmp_path)
    alternate = tmp_path / "project.yaml"
    alternate.write_text(canonical.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(ManifestError, match=r"\.graphify-project\.yaml"):
        load_manifest(alternate, tmp_path)


def test_manifest_requires_the_canonical_output_directory(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="output_dir must be graphify-out"):
        load_manifest(write_manifest(tmp_path, output_dir="alternate-output"), tmp_path)


@pytest.mark.parametrize("value", ["../outside", "/tmp/out", "a/../../outside"])
def test_manifest_rejects_escaping_paths(tmp_path: Path, value: str) -> None:
    with pytest.raises(ManifestError, match="confined relative path"):
        load_manifest(write_manifest(tmp_path, output_dir=value), tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("project_id", "Threads_Content_Stack"),
        ("include_roots", []),
        ("include_roots", ["src", "src"]),
        ("obsidian_namespace", "Projects/other/Generated"),
        ("graphify_version", ">=0.9.48"),
    ],
)
def test_manifest_rejects_values_that_weaken_its_contract(
    tmp_path: Path, field: str, value: object
) -> None:
    with pytest.raises(ManifestError):
        load_manifest(write_manifest(tmp_path, **{field: value}), tmp_path)


def test_manifest_rejects_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="unknown"):
        load_manifest(write_manifest(tmp_path, unreviewed_option="enabled"), tmp_path)


def test_manifest_rejects_versions_absent_from_the_compatibility_registry(
    tmp_path: Path,
) -> None:
    with pytest.raises(ManifestError, match="unsupported Graphify version: 0.9.49"):
        load_manifest(write_manifest(tmp_path, graphify_version="0.9.49"), tmp_path)


def test_manifest_rejects_non_string_unknown_keys(tmp_path: Path) -> None:
    path = write_manifest(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + "42: enabled\n", encoding="utf-8")

    with pytest.raises(ManifestError, match="unknown"):
        load_manifest(path, tmp_path)
