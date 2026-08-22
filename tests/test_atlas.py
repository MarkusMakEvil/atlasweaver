from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil

import pytest
import yaml

import project_knowledge.atlas as atlas_module

from project_knowledge.atlas import (
    ATLAS_OWNERSHIP_MANIFEST,
    AtlasFileSystem,
    AtlasOwnershipError,
    PreparedAtlasExport,
    ValidatedAtlasExport,
    generated_root,
    load_prepared_atlas_candidate,
    prepare_atlas_candidate,
    promote_atlas,
    validate_atlas_candidate,
)
from project_knowledge.models import ProjectManifest


@pytest.fixture
def manifest() -> ProjectManifest:
    return ProjectManifest(
        schema_version=1,
        project_id="demo",
        display_name="Demo",
        include_roots=(PurePosixPath("src"),),
        output_dir=PurePosixPath("graphify-out"),
        obsidian_namespace=PurePosixPath("Projects/demo/Generated"),
        excludes=(),
        track_html=True,
        graphify_version="0.9.48",
    )


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def raw_export(tmp_path: Path, name: str = "candidate") -> Path:
    root = tmp_path / name
    write(
        root / "Components/Auth.md",
        "---\nsource_kind: component\nsource_path: src/auth.py\n"
        "graph_node_id: component:auth\n---\n# Auth\n\n[[Flows/Login]]\n",
    )
    write(
        root / "Flows/Login.md",
        "---\nsource_kind: flow\nsource_path: src/login.py\n"
        "graph_node_id: flow:login\n---\n# Login\n\n[Auth](../Components/Auth.md)\n",
    )
    write(
        root / "graph.canvas",
        json.dumps(
            {
                "nodes": [
                    {"id": "1", "type": "file", "file": "Components/Auth.md"},
                    {"id": "2", "type": "file", "file": "Flows/Login.md"},
                ],
                "edges": [{"id": "e", "fromNode": "1", "toNode": "2"}],
            }
        ),
    )
    return root


def prepared(tmp_path: Path, manifest: ProjectManifest, name: str = "candidate") -> PreparedAtlasExport:
    return prepare_atlas_candidate(raw_export(tmp_path, name), manifest, "0.9.48")


def validated(tmp_path: Path, manifest: ProjectManifest, name: str = "candidate") -> ValidatedAtlasExport:
    return validate_atlas_candidate(prepared(tmp_path, manifest, name), manifest)


def validated_with_marker(
    tmp_path: Path, manifest: ProjectManifest, name: str, marker: str
) -> ValidatedAtlasExport:
    root = raw_export(tmp_path, name)
    note = root / "Components/Auth.md"
    note.write_text(note.read_text() + f"\n{marker}\n")
    return validate_atlas_candidate(
        prepare_atlas_candidate(root, manifest, "0.9.48"), manifest
    )


def frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n", 2)[1])


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative + b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def test_prepare_adds_stable_graphify_frontmatter_and_core_base(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    candidate = prepared(tmp_path, manifest)

    metadata = frontmatter(candidate.root / "Components/Auth.md")
    assert metadata == {
        "generated_at": candidate.generated_at,
        "generator_version": "0.9.48",
        "graph_node_id": "component:auth",
        "ownership": "graphify",
        "project_id": "demo",
        "source_kind": "component",
        "source_path": "src/auth.py",
    }
    base = yaml.safe_load((candidate.root / "_project.base").read_text(encoding="utf-8"))
    assert base["filters"] == {
        "and": ['project_id == "demo"', 'ownership == "graphify"']
    }
    assert base["views"][0]["type"] == "table"
    assert "dataview" not in (candidate.root / "_project.base").read_text().casefold()


def test_prepare_creates_digest_ownership_manifest(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    candidate = prepared(tmp_path, manifest)

    ownership = json.loads((candidate.root / ATLAS_OWNERSHIP_MANIFEST).read_text())
    assert ownership["schema_version"] == 1
    assert ownership["project_id"] == "demo"
    assert ownership["generator_version"] == "0.9.48"
    assert ownership["generated_at"] == candidate.generated_at
    assert sorted(ownership["files"]) == [
        "Components/Auth.md",
        "Flows/Login.md",
        "_project.base",
        "graph.canvas",
    ]
    for relative, digest in ownership["files"].items():
        assert digest == hashlib.sha256((candidate.root / relative).read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("relative", "content", "message"),
    [
        ("Bad.md", "```dataviewjs\ndv.pages()\n```\n", "DataviewJS"),
        ("Bad.md", "---\nownership: human\n---\nno\n", "ownership"),
        ("Bad.md", "---\nsource_path: ../secret\n---\nno\n", "source_path"),
        ("_project.base", "filters: true\n", "reserved"),
        (ATLAS_OWNERSHIP_MANIFEST, "{}\n", "reserved"),
    ],
)
def test_prepare_rejects_unsafe_or_reserved_input(
    tmp_path: Path,
    manifest: ProjectManifest,
    relative: str,
    content: str,
    message: str,
) -> None:
    root = raw_export(tmp_path)
    write(root / relative, content)

    with pytest.raises(AtlasOwnershipError, match=message):
        prepare_atlas_candidate(root, manifest, "0.9.48")


@pytest.mark.parametrize("fence", ["````dataviewjs", "~~~dataviewjs", "~~~~ dataviewjs"])
def test_prepare_rejects_all_executable_dataviewjs_fence_forms(
    tmp_path: Path, manifest: ProjectManifest, fence: str
) -> None:
    root = raw_export(tmp_path)
    write(root / "Bad.md", f"{fence}\ndv.pages()\n{fence[:3]}\n")

    with pytest.raises(AtlasOwnershipError, match="DataviewJS"):
        prepare_atlas_candidate(root, manifest, "0.9.48")


def test_prepare_rejects_symlinks_and_casefold_collisions(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    root = raw_export(tmp_path)
    write(root / "components/auth.md", "# collision\n")
    os.symlink(root / "Flows/Login.md", root / "leak.md")

    with pytest.raises(AtlasOwnershipError, match="symlink|collision"):
        prepare_atlas_candidate(root, manifest, "0.9.48")


def test_prepare_rejects_globally_denied_source_metadata(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    root = raw_export(tmp_path)
    note = root / "Components/Auth.md"
    note.write_text(note.read_text().replace("src/auth.py", ".env"))

    with pytest.raises(AtlasOwnershipError, match="denied source_path"):
        prepare_atlas_candidate(root, manifest, "0.9.48")


def test_validate_rejects_missing_ownership_and_broken_internal_links(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    candidate = prepared(tmp_path, manifest)
    note = candidate.root / "Flows/Login.md"
    note.write_text(note.read_text().replace("ownership: graphify\n", ""))

    with pytest.raises(AtlasOwnershipError, match="ownership"):
        validate_atlas_candidate(candidate, manifest)


def test_validate_rejects_broken_wiki_link(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    candidate = prepared(tmp_path, manifest, "candidate-2")
    note = candidate.root / "Flows/Login.md"
    note.write_text(note.read_text() + "\n[[Missing/Note]]\n")
    with pytest.raises(AtlasOwnershipError, match="internal link"):
        validate_atlas_candidate(candidate, manifest)


def test_validate_handles_reference_style_links_and_rejects_broken_definition(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    root = raw_export(tmp_path)
    auth = root / "Components/Auth.md"
    auth.write_text(auth.read_text() + "\n[login][flow]\n\n[flow]: ../Flows/Login.md\n")
    validate_atlas_candidate(
        prepare_atlas_candidate(root, manifest, "0.9.48"), manifest
    )

    root = raw_export(tmp_path, "broken-reference")
    auth = root / "Components/Auth.md"
    auth.write_text(auth.read_text() + "\n[missing][ref]\n\n[ref]: ../Missing.md\n")
    with pytest.raises(AtlasOwnershipError, match="internal link"):
        validate_atlas_candidate(
            prepare_atlas_candidate(root, manifest, "0.9.48"), manifest
        )


def test_validate_rejects_unsafe_internal_autolink_scheme(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    root = raw_export(tmp_path)
    auth = root / "Components/Auth.md"
    auth.write_text(auth.read_text() + "\n<file:///Users/example/private.md>\n")

    with pytest.raises(AtlasOwnershipError, match="unsafe.*scheme"):
        validate_atlas_candidate(
            prepare_atlas_candidate(root, manifest, "0.9.48"), manifest
        )


def test_validate_rejects_broken_canvas_reference_and_candidate_mutation(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    candidate = prepared(tmp_path, manifest)
    canvas = candidate.root / "graph.canvas"
    canvas.write_text('{"nodes":[{"id":"1","type":"file","file":"Missing.md"}],"edges":[]}')

    with pytest.raises(AtlasOwnershipError, match="canvas"):
        validate_atlas_candidate(candidate, manifest)

    candidate = prepared(tmp_path, manifest, "candidate-2")
    trusted = validate_atlas_candidate(candidate, manifest)
    write(candidate.root / "late.md", "late\n")
    with pytest.raises(AtlasOwnershipError, match="changed"):
        promote_atlas(trusted, tmp_path / "vault", manifest)


@pytest.mark.parametrize(
    "payload",
    [
        '{"nodes":[],"nodes":[],"edges":[]}',
        '{"nodes":[],"edges":[],"weight":NaN}',
        '{"nodes":[],"edges":[],"weight":1e400}',
    ],
)
def test_prepare_rejects_duplicate_or_nonfinite_canvas_json(
    tmp_path: Path, manifest: ProjectManifest, payload: str
) -> None:
    root = raw_export(tmp_path)
    (root / "graph.canvas").write_text(payload)

    with pytest.raises(AtlasOwnershipError, match="duplicate|constant|malformed"):
        prepare_atlas_candidate(root, manifest, "0.9.48")


def test_prepared_candidate_can_be_loaded_and_validated_in_a_later_process(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    first_process = prepared(tmp_path, manifest)
    reloaded = load_prepared_atlas_candidate(first_process.root, manifest)

    assert reloaded is not first_process
    promoted = promote_atlas(
        validate_atlas_candidate(reloaded, manifest), tmp_path / "vault", manifest
    )
    assert promoted.changed is True


def test_generated_root_rejects_traversal_namespace_and_symlinked_vault(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    unsafe = replace(
        manifest,
        obsidian_namespace=PurePosixPath("Projects/demo/../Notes"),
    )
    with pytest.raises(AtlasOwnershipError, match="namespace"):
        generated_root(tmp_path / "vault", unsafe)

    real = tmp_path / "real-vault"
    real.mkdir()
    linked = tmp_path / "vault"
    os.symlink(real, linked)
    with pytest.raises(AtlasOwnershipError, match="symlink"):
        generated_root(linked, manifest)


def test_refresh_preserves_human_notes_and_obsidian_configuration(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    human = write(
        atlas / "Projects/demo/Notes/decision.md",
        "---\nownership: human\n---\nkeep me\n",
    )
    obsidian = write(atlas / ".obsidian/app.json", '{"keep":true}\n')
    before_human = human.read_bytes()
    before_obsidian = obsidian.read_bytes()

    promote_atlas(validated(tmp_path, manifest), atlas, manifest)

    assert human.read_bytes() == before_human
    assert obsidian.read_bytes() == before_obsidian


def test_unknown_or_unmanifested_generated_file_fails_closed(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    target = generated_root(atlas, manifest, create=True)
    write(target / "manual.md", "not owned\n")

    with pytest.raises(AtlasOwnershipError, match="manual.md|manifest"):
        promote_atlas(validated(tmp_path, manifest), atlas, manifest)


def test_unknown_empty_generated_directory_fails_closed(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    promote_atlas(validated(tmp_path, manifest, "old"), atlas, manifest)
    (generated_root(atlas, manifest) / "manual-empty").mkdir()

    with pytest.raises(AtlasOwnershipError, match="manual-empty"):
        promote_atlas(validated(tmp_path, manifest, "new"), atlas, manifest)


def test_owned_tree_rejects_new_unapproved_file_type(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    promote_atlas(validated(tmp_path, manifest, "old"), atlas, manifest)
    target = generated_root(atlas, manifest)
    script = write(target / "run.js", "dangerous()\n")
    ownership_path = target / ATLAS_OWNERSHIP_MANIFEST
    ownership = json.loads(ownership_path.read_text())
    ownership["files"]["run.js"] = hashlib.sha256(script.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for relative, file_digest in sorted(ownership["files"].items()):
        digest.update(relative.encode() + b"\0" + file_digest.encode() + b"\0")
    ownership["atlas_digest"] = digest.hexdigest()
    ownership_path.write_text(json.dumps(ownership))

    with pytest.raises(AtlasOwnershipError, match="unsupported"):
        promote_atlas(validated(tmp_path, manifest, "new"), atlas, manifest)


def test_refresh_removes_only_stale_owned_files(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    first = validated(tmp_path, manifest, "first")
    promote_atlas(first, atlas, manifest)
    assert (generated_root(atlas, manifest) / "Flows/Login.md").exists()

    second_root = raw_export(tmp_path, "second")
    (second_root / "Flows/Login.md").unlink()
    auth = second_root / "Components/Auth.md"
    auth.write_text(auth.read_text().replace("[[Flows/Login]]", ""))
    canvas = second_root / "graph.canvas"
    canvas.write_text(
        json.dumps(
            {"nodes": [{"id": "1", "type": "file", "file": "Components/Auth.md"}], "edges": []}
        )
    )
    second = validate_atlas_candidate(
        prepare_atlas_candidate(second_root, manifest, "0.9.48"), manifest
    )

    promote_atlas(second, atlas, manifest)

    target = generated_root(atlas, manifest)
    assert not (target / "Flows/Login.md").exists()
    assert (target / "Components/Auth.md").exists()


def test_identical_refresh_is_idempotent(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    candidate = validated(tmp_path, manifest)
    first = promote_atlas(candidate, atlas, manifest)
    before = tree_digest(atlas)

    second = promote_atlas(candidate, atlas, manifest)

    assert first.digest == second.digest
    assert second.changed is False
    assert tree_digest(atlas) == before


def test_fresh_prepare_of_identical_content_preserves_timestamp_and_is_idempotent(
    tmp_path: Path, manifest: ProjectManifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamps = iter(
        [
            datetime.fromisoformat("2026-08-22T01:00:00+00:00"),
            datetime.fromisoformat("2026-08-22T02:00:00+00:00"),
        ]
    )

    class ControlledDateTime:
        @classmethod
        def now(cls, tz: object) -> datetime:
            del tz
            return next(timestamps)

    monkeypatch.setattr(atlas_module, "datetime", ControlledDateTime)
    atlas = tmp_path / "vault"
    first = validated(tmp_path, manifest, "first")
    promote_atlas(first, atlas, manifest)
    target_note = generated_root(atlas, manifest) / "Components/Auth.md"
    first_timestamp = frontmatter(target_note)["generated_at"]

    second = validated(tmp_path, manifest, "second")
    assert second.generated_at != first.generated_at
    result = promote_atlas(second, atlas, manifest)

    assert result.changed is False
    assert result.digest == first.digest == second.digest
    assert frontmatter(target_note)["generated_at"] == first_timestamp


def test_revisiting_historical_content_never_collides_with_retained_backup(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    candidates = {
        marker: validated_with_marker(tmp_path, manifest, marker, marker)
        for marker in ("A", "B", "C")
    }

    for marker in ("A", "B", "C", "B"):
        promote_atlas(candidates[marker], atlas, manifest)
        rollback = atlas / "Projects/demo/.project-knowledge-atlas/rollback"
        assert not tuple(rollback.iterdir())

    assert "B" in (generated_root(atlas, manifest) / "Components/Auth.md").read_text()


def test_promotion_rejects_forged_validated_object_and_candidate_inside_vault(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    forged = ValidatedAtlasExport(
        root=tmp_path / "forged",
        project_id="demo",
        generated_at="2026-01-01T00:00:00+00:00",
        generator_version="0.9.48",
        digest="0" * 64,
        files=(),
    )
    with pytest.raises(AtlasOwnershipError, match="current process"):
        promote_atlas(forged, tmp_path / "vault", manifest)

    atlas = tmp_path / "vault-2"
    atlas.mkdir()
    candidate = validate_atlas_candidate(
        prepare_atlas_candidate(raw_export(atlas, "staged"), manifest, "0.9.48"),
        manifest,
    )
    with pytest.raises(AtlasOwnershipError, match="outside the atlas"):
        promote_atlas(candidate, atlas, manifest)


def test_promotion_rejects_namespace_case_collision_and_symlinked_target(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    (atlas / "projects").mkdir(parents=True)
    with pytest.raises(AtlasOwnershipError, match="collision"):
        promote_atlas(validated(tmp_path, manifest), atlas, manifest)

    atlas = tmp_path / "vault-2"
    (atlas / "Projects/demo/generated").mkdir(parents=True)
    with pytest.raises(AtlasOwnershipError, match="collision"):
        promote_atlas(validated(tmp_path, manifest, "candidate-2"), atlas, manifest)

    atlas = tmp_path / "vault-3"
    external = tmp_path / "external"
    external.mkdir()
    (atlas / "Projects/demo").mkdir(parents=True)
    os.symlink(external, atlas / "Projects/demo/Generated")
    with pytest.raises(AtlasOwnershipError, match="symlink"):
        promote_atlas(validated(tmp_path, manifest, "candidate-3"), atlas, manifest)


class FaultingFS(AtlasFileSystem):
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.seen: list[str] = []
        self.fsynced: list[Path] = []

    def checkpoint(self, operation: str) -> None:
        self.seen.append(operation)
        if self.failure == operation:
            self.failure = None
            raise OSError(operation)

    def fsync_directory(self, path: Path) -> None:
        self.fsynced.append(path)
        super().fsync_directory(path)

    def fsync_open_directory(self, descriptor: int, display_path: Path) -> None:
        self.fsynced.append(display_path)
        super().fsync_open_directory(descriptor, display_path)


class AddUnknownBeforeBackupFS(AtlasFileSystem):
    def __init__(self, target: Path) -> None:
        self.target = target

    def checkpoint(self, operation: str) -> None:
        if operation == "rename-backup":
            write(self.target / "concurrent.md", "human concurrent file\n")


class AddEmptyDirectoryBeforeBackupFS(AtlasFileSystem):
    def __init__(self, target: Path) -> None:
        self.target = target

    def checkpoint(self, operation: str) -> None:
        if operation == "rename-backup":
            (self.target / "human-empty").mkdir()


class SwapProjectRootBeforeBackupFS(AtlasFileSystem):
    def __init__(self, project: Path, displaced: Path, external: Path) -> None:
        self.project = project
        self.displaced = displaced
        self.external = external

    def checkpoint(self, operation: str) -> None:
        if operation == "rename-backup":
            os.rename(self.project, self.displaced)
            os.symlink(self.external, self.project)


class SwapNamespaceAncestorBeforeBackupFS(AtlasFileSystem):
    def __init__(
        self,
        visible: Path,
        displaced: Path,
        external: Path,
    ) -> None:
        self.visible = visible
        self.displaced = displaced
        self.external = external

    def checkpoint(self, operation: str) -> None:
        if operation == "rename-backup":
            os.rename(self.visible, self.displaced)
            os.symlink(self.external, self.visible)


class RecordingFS(AtlasFileSystem):
    def __init__(self) -> None:
        self.events: list[tuple[str, Path, Path | None]] = []

    def rename_at(
        self,
        source_parent: int,
        source_name: str,
        destination_parent: int,
        destination_name: str,
    ) -> None:
        self.events.append(("rename", Path(source_name), Path(destination_name)))
        super().rename_at(
            source_parent,
            source_name,
            destination_parent,
            destination_name,
        )

    def fsync_directory(self, path: Path) -> None:
        self.events.append(("fsync", path, None))
        super().fsync_directory(path)

    def fsync_open_directory(self, descriptor: int, display_path: Path) -> None:
        self.events.append(("fsync", display_path, None))
        super().fsync_open_directory(descriptor, display_path)


@pytest.mark.parametrize(
    "failure",
    [
        "copy-stage",
        "fsync-stage",
        "rename-backup",
        "fsync-backup",
        "rename-candidate",
        "fsync-promote",
    ],
)
def test_failed_refresh_rolls_back_previous_generated_tree(
    tmp_path: Path, manifest: ProjectManifest, failure: str
) -> None:
    atlas = tmp_path / "vault"
    old = validated_with_marker(tmp_path, manifest, "old", "OLD")
    promote_atlas(old, atlas, manifest)
    before = tree_digest(generated_root(atlas, manifest))
    new_root = raw_export(tmp_path, "new")
    note = new_root / "Components/Auth.md"
    note.write_text(note.read_text() + "\nchanged\n")
    new = validate_atlas_candidate(
        prepare_atlas_candidate(new_root, manifest, "0.9.48"), manifest
    )

    with pytest.raises(OSError, match=failure):
        promote_atlas(new, atlas, manifest, fs=FaultingFS(failure))

    assert tree_digest(generated_root(atlas, manifest)) == before


def test_failed_first_promotion_leaves_generated_absent(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    with pytest.raises(OSError, match="fsync-promote"):
        promote_atlas(
            validated(tmp_path, manifest),
            atlas,
            manifest,
            fs=FaultingFS("fsync-promote"),
        )
    assert not (atlas / "Projects/demo/Generated").exists()


def test_concurrent_unknown_file_after_validation_blocks_install_and_is_preserved(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    old = validated_with_marker(tmp_path, manifest, "old", "OLD")
    promote_atlas(old, atlas, manifest)
    target = generated_root(atlas, manifest)
    new = validated_with_marker(tmp_path, manifest, "new", "NEW")

    with pytest.raises(AtlasOwnershipError, match="changed|unknown"):
        promote_atlas(new, atlas, manifest, fs=AddUnknownBeforeBackupFS(target))

    assert (target / "concurrent.md").read_text() == "human concurrent file\n"
    assert "OLD" in (target / "Components/Auth.md").read_text()


def test_concurrent_empty_directory_after_validation_blocks_install_and_is_preserved(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    old = validated_with_marker(tmp_path, manifest, "old", "OLD")
    promote_atlas(old, atlas, manifest)
    target = generated_root(atlas, manifest)
    new = validated_with_marker(tmp_path, manifest, "new", "NEW")

    with pytest.raises(AtlasOwnershipError, match="changed|unknown"):
        promote_atlas(
            new,
            atlas,
            manifest,
            fs=AddEmptyDirectoryBeforeBackupFS(target),
        )

    assert (target / "human-empty").is_dir()
    assert not any((target / "human-empty").iterdir())
    assert "OLD" in (target / "Components/Auth.md").read_text()


def test_project_root_symlink_swap_never_deletes_or_mutates_external_tree(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    promote_atlas(validated_with_marker(tmp_path, manifest, "old", "OLD"), atlas, manifest)
    project = atlas / "Projects/demo"
    displaced = atlas / "Projects/demo-displaced"
    external = tmp_path / "external-project"
    sentinel = write(external / "Generated/sentinel.md", "external must survive\n")
    before = tree_digest(external)

    with pytest.raises(AtlasOwnershipError):
        promote_atlas(
            validated_with_marker(tmp_path, manifest, "new", "NEW"),
            atlas,
            manifest,
            fs=SwapProjectRootBeforeBackupFS(project, displaced, external),
        )

    assert sentinel.read_text() == "external must survive\n"
    assert tree_digest(external) == before


@pytest.mark.parametrize("ancestor", ["atlas", "projects"])
def test_namespace_ancestor_swap_fails_closed_without_updating_displaced_tree(
    tmp_path: Path, manifest: ProjectManifest, ancestor: str
) -> None:
    atlas = tmp_path / "vault"
    promote_atlas(
        validated_with_marker(tmp_path, manifest, "old", "OLD"), atlas, manifest
    )
    if ancestor == "atlas":
        visible = atlas
        displaced = tmp_path / "vault-displaced"
        displaced_generated = displaced / "Projects/demo/Generated"
    else:
        visible = atlas / "Projects"
        displaced = atlas / "Projects-displaced"
        displaced_generated = displaced / "demo/Generated"
    before_displaced = tree_digest(visible)
    external = tmp_path / f"external-{ancestor}"
    sentinel = write(external / "sentinel.md", "external must survive\n")
    before_external = tree_digest(external)

    with pytest.raises(AtlasOwnershipError, match="root|namespace|changed"):
        promote_atlas(
            validated_with_marker(tmp_path, manifest, "new", "NEW"),
            atlas,
            manifest,
            fs=SwapNamespaceAncestorBeforeBackupFS(visible, displaced, external),
        )

    assert visible.is_symlink()
    assert sentinel.read_text() == "external must survive\n"
    assert tree_digest(external) == before_external
    assert tree_digest(displaced) == before_displaced
    assert "OLD" in (displaced_generated / "Components/Auth.md").read_text()


def test_first_use_state_and_namespace_parents_are_durably_synced(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    fs = FaultingFS()

    promote_atlas(validated(tmp_path, manifest), atlas, manifest, fs=fs)

    resolved = atlas.resolve()
    assert resolved in fs.fsynced
    assert resolved / "Projects" in fs.fsynced
    assert resolved / "Projects/demo" in fs.fsynced


def test_rename_parents_are_durable_before_journal_state_advances(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    promote_atlas(validated_with_marker(tmp_path, manifest, "old", "OLD"), atlas, manifest)
    fs = RecordingFS()
    promote_atlas(
        validated_with_marker(tmp_path, manifest, "new", "NEW"),
        atlas,
        manifest,
        fs=fs,
    )
    events = fs.events
    backup_rename = next(
        index
        for index, event in enumerate(events)
        if event[0] == "rename" and event[1].name == "Generated"
    )
    candidate_rename = next(
        index
        for index, event in enumerate(events)
        if event[0] == "rename" and event[2] is not None and event[2].name == "Generated"
    )
    transactions = atlas.resolve() / "Projects/demo/.project-knowledge-atlas/transactions"
    project = atlas.resolve() / "Projects/demo"
    rollback = atlas.resolve() / "Projects/demo/.project-knowledge-atlas/rollback"
    staging = atlas.resolve() / "Projects/demo/.project-knowledge-atlas/staging"

    after_backup = events[backup_rename + 1 : candidate_rename]
    assert ("fsync", project, None) in after_backup
    assert ("fsync", rollback, None) in after_backup
    first_transaction_fsync = after_backup.index(("fsync", transactions, None))
    assert after_backup.index(("fsync", project, None)) < first_transaction_fsync
    assert after_backup.index(("fsync", rollback, None)) < first_transaction_fsync

    after_candidate = events[candidate_rename + 1 :]
    first_transaction_fsync = after_candidate.index(("fsync", transactions, None))
    assert after_candidate.index(("fsync", project, None)) < first_transaction_fsync
    assert after_candidate.index(("fsync", staging, None)) < first_transaction_fsync


def test_interrupted_backup_is_recovered_before_new_work(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    old = validated(tmp_path, manifest, "old")
    promote_atlas(old, atlas, manifest)
    old_digest = tree_digest(generated_root(atlas, manifest))
    new = validated_with_marker(tmp_path, manifest, "new", "NEW")
    state = atlas / "Projects/demo/.project-knowledge-atlas"
    backup = state / "rollback" / new.digest
    backup.parent.mkdir(parents=True, exist_ok=True)
    os.rename(generated_root(atlas, manifest), backup)
    transaction = state / "transactions" / f"{new.digest}.json"
    write(
        transaction,
        json.dumps(
            {"schema_version": 1, "state": "backed_up", "atlas_digest": new.digest}
        ),
    )

    with pytest.raises(OSError, match="copy-stage"):
        promote_atlas(new, atlas, manifest, fs=FaultingFS("copy-stage"))

    assert tree_digest(generated_root(atlas, manifest)) == old_digest
    assert not transaction.exists()


def test_first_promotion_promoted_journal_with_stage_and_no_target_recovers_absence(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    candidate = validated(tmp_path, manifest)
    state = atlas / "Projects/demo/.project-knowledge-atlas"
    stage = state / "staging" / candidate.digest
    shutil.copytree(candidate.root, stage)
    (state / "rollback").mkdir()
    journal = write(
        state / "transactions" / f"{candidate.digest}.json",
        json.dumps(
            {
                "schema_version": 1,
                "state": "promoted",
                "atlas_digest": candidate.digest,
            }
        ),
    )

    with pytest.raises(OSError, match="copy-stage"):
        promote_atlas(candidate, atlas, manifest, fs=FaultingFS("copy-stage"))

    assert not generated_root(atlas, manifest).exists()
    assert not stage.exists()
    assert not journal.exists()


def test_recovery_is_idempotent_after_fsync_failure_restored_previous_tree(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    old = validated_with_marker(tmp_path, manifest, "old-recovery", "OLD")
    promote_atlas(old, atlas, manifest)
    old_digest = tree_digest(generated_root(atlas, manifest))
    new = validated_with_marker(tmp_path, manifest, "new-recovery", "NEW")
    state = atlas / "Projects/demo/.project-knowledge-atlas"
    backup = state / "rollback" / new.digest
    os.rename(generated_root(atlas, manifest), backup)
    journal = write(
        state / "transactions" / f"{new.digest}.json",
        json.dumps(
            {"schema_version": 1, "state": "backed_up", "atlas_digest": new.digest}
        ),
    )

    with pytest.raises(OSError, match="fsync-recovery"):
        promote_atlas(new, atlas, manifest, fs=FaultingFS("fsync-recovery"))
    assert tree_digest(generated_root(atlas, manifest)) == old_digest
    assert journal.exists()

    with pytest.raises(OSError, match="copy-stage"):
        promote_atlas(new, atlas, manifest, fs=FaultingFS("copy-stage"))
    assert tree_digest(generated_root(atlas, manifest)) == old_digest
    assert not journal.exists()


def test_symlinked_transaction_journal_fails_closed(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    candidate = validated(tmp_path, manifest)
    transactions = atlas / "Projects/demo/.project-knowledge-atlas/transactions"
    transactions.mkdir(parents=True)
    outside = write(tmp_path / "outside.json", "{}")
    os.symlink(outside, transactions / f"{candidate.digest}.json")

    with pytest.raises(AtlasOwnershipError, match="journal|symlink"):
        promote_atlas(candidate, atlas, manifest)
    assert outside.read_text() == "{}"


def test_inconsistent_interrupted_transaction_remains_for_recovery(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    candidate = validated(tmp_path, manifest)
    state = atlas / "Projects/demo/.project-knowledge-atlas"
    (state / "transactions").mkdir(parents=True)
    (state / "staging").mkdir()
    (state / "rollback").mkdir()
    journal = write(
        state / "transactions" / f"{candidate.digest}.json",
        json.dumps(
            {
                "schema_version": 1,
                "state": "backed_up",
                "atlas_digest": candidate.digest,
            }
        ),
    )

    with pytest.raises(AtlasOwnershipError, match="inconsistent"):
        promote_atlas(candidate, atlas, manifest)
    assert journal.exists()


def test_transaction_journal_rejects_boolean_schema_version(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    candidate = validated(tmp_path, manifest)
    state = atlas / "Projects/demo/.project-knowledge-atlas"
    (state / "transactions").mkdir(parents=True)
    (state / "staging").mkdir()
    (state / "rollback").mkdir()
    journal = write(
        state / "transactions" / f"{candidate.digest}.json",
        json.dumps(
            {
                "schema_version": True,
                "state": "backed_up",
                "atlas_digest": candidate.digest,
            }
        ),
    )

    with pytest.raises(AtlasOwnershipError, match="schema"):
        promote_atlas(candidate, atlas, manifest)
    assert journal.exists()


def test_existing_generated_manifest_digest_mismatch_fails_closed(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    promote_atlas(validated(tmp_path, manifest, "old"), atlas, manifest)
    target = generated_root(atlas, manifest)
    note = target / "Components/Auth.md"
    note.write_text(note.read_text() + "tampered\n")

    with pytest.raises(AtlasOwnershipError, match="digest|changed"):
        promote_atlas(validated(tmp_path, manifest, "new"), atlas, manifest)


def test_existing_generated_manifest_rejects_duplicate_json_keys(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    promote_atlas(validated(tmp_path, manifest, "old"), atlas, manifest)
    ownership_path = generated_root(atlas, manifest) / ATLAS_OWNERSHIP_MANIFEST
    original = ownership_path.read_text()
    ownership_path.write_text(original.replace('"project_id": "demo",', '"project_id": "demo",\n  "project_id": "demo",'))

    with pytest.raises(AtlasOwnershipError, match="duplicate"):
        promote_atlas(validated(tmp_path, manifest, "new"), atlas, manifest)


def test_existing_generated_manifest_rejects_boolean_schema_version(
    tmp_path: Path, manifest: ProjectManifest
) -> None:
    atlas = tmp_path / "vault"
    promote_atlas(validated(tmp_path, manifest, "old"), atlas, manifest)
    ownership_path = generated_root(atlas, manifest) / ATLAS_OWNERSHIP_MANIFEST
    ownership = json.loads(ownership_path.read_text())
    ownership["schema_version"] = True
    ownership_path.write_text(json.dumps(ownership))

    with pytest.raises(AtlasOwnershipError, match="schema"):
        promote_atlas(validated(tmp_path, manifest, "new"), atlas, manifest)
