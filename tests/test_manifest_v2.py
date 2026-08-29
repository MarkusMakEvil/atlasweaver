from dataclasses import replace
import os
from pathlib import Path, PurePosixPath
from uuid import UUID

import pytest

from project_knowledge.manifest import (
    InitPreview,
    ManifestError,
    apply_init,
    apply_manifest_migration,
    inspect_init_journal,
    load_manifest,
    load_manifest_payload,
    preview_init,
    preview_manifest_migration,
    render_manifest_v2,
)
from project_knowledge.locking import TransactionLockError
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


def test_init_preview_is_read_only_and_apply_generates_uuid_once(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    before = tuple(repo.iterdir())
    preview = preview_init(repo, "demo")
    calls = 0

    def allocate() -> UUID:
        nonlocal calls
        calls += 1
        return DEMO_UID

    assert preview.status == "preview"
    assert preview.project_uid is None
    assert b"<generated-on-apply>" in preview.manifest_payload
    assert tuple(repo.iterdir()) == before

    assert apply_init(repo, preview, uuid_factory=allocate) == "initialized"
    assert calls == 1
    initialized = load_manifest(repo / ".graphify-project.yaml", repo)
    assert initialized.project_uid == DEMO_UID
    assert initialized.graphify_version == "0.9.51"
    assert inspect_init_journal(repo) == "none"


def test_init_requires_explicit_roots_when_discovery_is_ambiguous(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir()

    preview = preview_init(repo, "demo")

    assert preview.status == "ambiguous_roots"
    assert preview.candidate_roots == (
        PurePosixPath("docs"),
        PurePosixPath("src"),
    )
    with pytest.raises(ManifestError, match="ambiguous_roots"):
        apply_init(repo, preview, uuid_factory=lambda: DEMO_UID)
    assert not (repo / ".project-knowledge").exists()


def test_init_never_overwrites_and_rolls_back_only_its_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    preview = preview_init(
        repo, "demo", include_roots=(PurePosixPath("src"),)
    )
    original_link = os.link
    calls = 0

    def fail_second(source: str, target: str, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second create failure")
        original_link(source, target, **kwargs)

    monkeypatch.setattr(os, "link", fail_second)

    with pytest.raises(ManifestError, match="configuration transaction failed"):
        apply_init(repo, preview, uuid_factory=lambda: DEMO_UID)

    assert not (repo / ".graphify-project.yaml").exists()
    assert not (repo / ".graphifyignore").exists()
    assert inspect_init_journal(repo) == "recoverable"


def test_init_conflict_preserves_file_created_after_preview(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    preview = preview_init(repo, "demo")
    ignore = repo / ".graphifyignore"
    ignore.write_bytes(b"caller-owned\n")

    assert apply_init(repo, preview, uuid_factory=lambda: DEMO_UID) == "init_conflict"
    assert ignore.read_bytes() == b"caller-owned\n"
    assert not (repo / ".graphify-project.yaml").exists()


def test_init_rederives_roots_and_rejects_a_forged_preview(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    preview = preview_init(repo, "demo")
    forged = replace(preview, manifest_payload=preview.manifest_payload + b"# forged\n")

    assert apply_init(repo, forged, uuid_factory=lambda: DEMO_UID) == "init_conflict"
    assert not (repo / ".graphify-project.yaml").exists()

    (repo / "docs").mkdir()
    assert apply_init(repo, preview, uuid_factory=lambda: DEMO_UID) == "init_conflict"
    assert not (repo / ".graphify-project.yaml").exists()


def test_init_repository_replacement_fails_before_state_creation(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    preview = preview_init(repo, "demo")
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "caller").write_bytes(b"untouched")
    repo.rename(original)
    os.symlink(replacement, repo, target_is_directory=True)

    with pytest.raises(TransactionLockError) as raised:
        apply_init(repo, preview, uuid_factory=lambda: DEMO_UID)

    assert raised.value.kind in {"authority", "unavailable"}
    assert (replacement / "caller").read_bytes() == b"untouched"
    assert not (replacement / ".project-knowledge").exists()


def test_v1_migration_changes_only_manifest(tmp_path: Path) -> None:
    from tests.test_manifest import write_manifest

    repo = tmp_path
    write_manifest(repo)
    ignore = repo / ".graphifyignore"
    ignore.write_bytes(b"caller-reviewed-ignore/**\n")
    graph = repo / "graphify-out/graph.json"
    graph.parent.mkdir()
    graph.write_text("caller bytes\n", encoding="utf-8")

    preview = preview_manifest_migration(repo, project_uid=DEMO_UID)

    assert apply_manifest_migration(repo, preview) == "migrated"
    assert load_manifest(repo / ".graphify-project.yaml", repo).schema_version == 2
    assert ignore.read_bytes() == b"caller-reviewed-ignore/**\n"
    assert graph.read_text(encoding="utf-8") == "caller bytes\n"
    assert inspect_init_journal(repo) == "none"


def test_migration_rejects_manifest_mutation_after_preview(tmp_path: Path) -> None:
    from tests.test_manifest import write_manifest

    path = write_manifest(tmp_path)
    preview = preview_manifest_migration(tmp_path, project_uid=DEMO_UID)
    path.write_bytes(path.read_bytes().replace(b"track_html: true", b"track_html: false"))

    assert apply_manifest_migration(tmp_path, preview) == "init_conflict"
    assert load_manifest(path, tmp_path).schema_version == 1


def test_inspect_corrupt_journal_is_read_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = repo / ".project-knowledge"
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    journal = state / "init-transaction.json"
    journal.write_bytes(b"not json\n")
    journal.chmod(0o600)
    before = journal.stat()

    assert inspect_init_journal(repo) == "corrupt"
    after = journal.stat()
    assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)
