from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path, PurePosixPath
from uuid import UUID

import pytest
import yaml

from project_knowledge.compatibility import resolve_graphify_compatibility
from project_knowledge.coverage import (
    CoverageError,
    apply_coverage_approval,
    load_coverage_approvals,
    omission_is_approved,
    preview_coverage_approval,
)
from project_knowledge.locking import open_repository_access
from project_knowledge.staging import inspect_projection, stage_input
from tests.support import manifest_v2, write_manifest_v2


def repository(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / "src").mkdir(parents=True)
    (repo / "src/unsupported.py").write_text(
        "safe current bytes\n", encoding="utf-8"
    )
    write_manifest_v2(repo)
    return repo


def preview(repo: Path):
    return preview_coverage_approval(
        repo,
        manifest_v2(),
        PurePosixPath("src/unsupported.py"),
        "not_represented_by_graphify",
        "Reviewed: navigation remains useful.",
    )


def test_coverage_preview_is_read_only_and_apply_binds_current_bytes(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)

    candidate = preview(repo)

    assert not (repo / ".atlasweaver-coverage.yaml").exists()
    assert apply_coverage_approval(repo, candidate) == "approved"
    projection = inspect_projection(repo, manifest_v2())
    approvals = load_coverage_approvals(
        repo, projection, resolve_graphify_compatibility("0.9.48")
    )
    assert approvals[0].path == PurePosixPath("src/unsupported.py")
    assert approvals[0].content_sha256 == projection.files[0].sha256

    destination = tmp_path / "staged"
    staged = stage_input(repo, manifest_v2(), destination)
    assert staged.coverage_approvals == approvals
    assert staged.projection_files == projection.files
    assert omission_is_approved(
        PurePosixPath("src/unsupported.py"),
        "not_represented_by_graphify",
        staged,
        "graphify-0.9.48",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "src/*.py"),
        ("path", "src"),
        ("path", "src/database-creds.json"),
        ("reason_code", "made_up_reason"),
        ("rationale", ""),
    ],
)
def test_coverage_rejects_non_exact_or_untracked_authority(
    tmp_path: Path, field: str, value: str
) -> None:
    repo = repository(tmp_path)
    entry = {
        "path": "src/unsupported.py",
        "content_sha256": hashlib.sha256(b"safe current bytes\n").hexdigest(),
        "adapter_id": "graphify-0.9.48",
        "reason_code": "not_represented_by_graphify",
        "rationale": "reviewed",
    }
    entry[field] = value
    (repo / ".atlasweaver-coverage.yaml").write_text(
        yaml.safe_dump(
            {"schema_version": 1, "approvals": [entry]}, sort_keys=False
        ),
        encoding="utf-8",
    )

    with pytest.raises(CoverageError):
        inspect_projection(repo, manifest_v2())


def test_content_change_invalidates_approval(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    assert apply_coverage_approval(repo, preview(repo)) == "approved"
    (repo / "src/unsupported.py").write_text("changed\n", encoding="utf-8")

    with pytest.raises(CoverageError, match="current content"):
        inspect_projection(repo, manifest_v2())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: replace(item, payload=item.payload + b"# forged\n"),
        lambda item: replace(item, requested_rationale="different"),
        lambda item: replace(item, project_uid=UUID("973a04ea-47aa-4ad7-90ad-d7c0d14f55e0")),
        lambda item: replace(item, approval=replace(item.approval, adapter_id="graphify-forged")),
    ],
)
def test_forged_coverage_previews_are_closed_and_non_mutating(
    tmp_path: Path, mutation
) -> None:
    repo = repository(tmp_path)
    forged = mutation(preview(repo))

    assert apply_coverage_approval(repo, forged) == "coverage_conflict"
    assert not (repo / ".atlasweaver-coverage.yaml").exists()


def test_stale_source_manifest_and_control_inode_are_rejected(tmp_path: Path) -> None:
    repo = repository(tmp_path)

    stale_source = preview(repo)
    (repo / "src/unsupported.py").write_text("new safe bytes\n", encoding="utf-8")
    assert apply_coverage_approval(repo, stale_source) == "coverage_conflict"

    (repo / "src/unsupported.py").write_text("safe current bytes\n", encoding="utf-8")
    stale_manifest = preview(repo)
    write_manifest_v2(
        repo, project_uid=UUID("973a04ea-47aa-4ad7-90ad-d7c0d14f55e0")
    )
    assert apply_coverage_approval(repo, stale_manifest) == "coverage_conflict"

    write_manifest_v2(repo)
    stale_control = preview(repo)
    (repo / ".atlasweaver-coverage.yaml").write_text(
        "schema_version: 1\napprovals: []\n", encoding="utf-8"
    )
    assert apply_coverage_approval(repo, stale_control) == "coverage_conflict"


def test_cross_repository_and_root_swap_do_not_create_replacement_state(
    tmp_path: Path,
) -> None:
    first = repository(tmp_path, "first")
    second = repository(tmp_path, "second")
    candidate = preview(first)

    assert apply_coverage_approval(second, candidate) == "coverage_conflict"
    assert not (second / ".atlasweaver-coverage.yaml").exists()
    assert not (second / ".project-knowledge").exists()

    original = tmp_path / "original"
    os.rename(first, original)
    first.mkdir()
    (first / "sentinel").write_text("replacement\n", encoding="utf-8")
    before = (first / "sentinel").read_bytes()

    assert apply_coverage_approval(first, candidate) == "coverage_conflict"
    assert (first / "sentinel").read_bytes() == before
    assert sorted(path.name for path in first.iterdir()) == ["sentinel"]


def test_retained_access_never_reads_or_mutates_replacement_root(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    moved = tmp_path / "moved"

    with open_repository_access(repo) as access:
        os.rename(repo, moved)
        repo.mkdir()
        (repo / "sentinel").write_text("replacement bytes\n", encoding="utf-8")
        candidate = preview_coverage_approval(
            repo,
            manifest_v2(),
            PurePosixPath("src/unsupported.py"),
            "not_represented_by_graphify",
            "reviewed",
            repository_access=access,
        )

    assert candidate.approval.content_sha256 == hashlib.sha256(
        b"safe current bytes\n"
    ).hexdigest()
    assert apply_coverage_approval(repo, candidate) == "coverage_conflict"
    assert sorted(path.name for path in repo.iterdir()) == ["sentinel"]


def test_strict_yaml_secret_shape_and_incomplete_transaction_fail_closed(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    control = repo / ".atlasweaver-coverage.yaml"
    control.write_text(
        "schema_version: 1\napprovals: &items []\ncopy: *items\n",
        encoding="utf-8",
    )
    with pytest.raises(CoverageError, match="schema"):
        inspect_projection(repo, manifest_v2())

    control.write_text(
        "schema_version: 1\napprovals: []\npassword: sk-test_12345678901234567890\n",
        encoding="utf-8",
    )
    with pytest.raises(CoverageError, match="secret-shaped"):
        inspect_projection(repo, manifest_v2())

    control.unlink()
    candidate = preview(repo)
    (repo / ".atlasweaver-coverage.transaction.json").write_text(
        "{}\n", encoding="utf-8"
    )
    assert apply_coverage_approval(repo, candidate) == "coverage_recovery_required"
    assert not control.exists()
