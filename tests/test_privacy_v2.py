from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from project_knowledge.compatibility import resolve_graphify_compatibility
from project_knowledge.privacy import PrivacyError, classify_path
from project_knowledge.locking import open_repository_access
from project_knowledge.receipt import load_staging_receipt, verify_staged_input
from project_knowledge.staging import inspect_projection, stage_input_with_receipt
from tests.support import manifest_v2


@pytest.mark.parametrize(("relative", "action", "rule_id"), [
    ("src/credentials.py", "scan", "sensitive_source_name"),
    ("src/SECRET.tsx", "scan", "sensitive_source_name"),
    ("src/apiToken.ts", "scan", "sensitive_source_name"),
    ("src/databaseCredentials.py", "scan", "sensitive_source_name"),
    ("src/database-creds.json", "deny", "sensitive_data_name"),
    ("src/auth-token.yaml", "deny", "sensitive_data_name"),
    ("src/.ENV.local", "deny", "environment_file"),
    ("src/signing.KEY", "deny", "private_key_material"),
    ("src/session/store.py", "deny", "session_store"),
    ("src/app.py", "allow", "ordinary_source"),
])
def test_policy_v2_precedence(relative: str, action: str, rule_id: str) -> None:
    decision = classify_path(
        PurePosixPath(relative), project_excludes=(),
        sensitive_source_suffixes=resolve_graphify_compatibility(
            "0.9.48"
        ).sensitive_source_suffixes,
    )
    assert (decision.action, decision.rule_id) == (action, rule_id)


@pytest.mark.parametrize(("relative", "rule_id"), [
    (".git/config", "git_metadata"),
    (".worktrees/x/source.py", "worktree_metadata"),
    ("workspace/source.py", "root_workspace"),
    ("src/runtime/job.json", "runtime_state"),
    ("src/drafts/post.md", "draft_content"),
    ("src/snapshots/state.json", "snapshot_state"),
    ("src/cookies/data.sqlite", "cookie_store"),
    ("src/sessions/data.sqlite", "session_store"),
    ("src/identity.yaml", "identity_control"),
    ("src/disclosure.yaml", "disclosure_control"),
    ("src/current-state.yaml", "runtime_control"),
    ("Projects/demo/Notes/manual.md", "human_notes"),
])
def test_policy_v2_preserves_non_bypassable_rules(
    relative: str, rule_id: str,
) -> None:
    decision = classify_path(
        PurePosixPath(relative), project_excludes=(),
        sensitive_source_suffixes=resolve_graphify_compatibility(
            "0.9.48"
        ).sensitive_source_suffixes,
    )
    assert (decision.action, decision.rule_id) == ("deny", rule_id)


def test_root_workspace_rule_is_not_broadened() -> None:
    decision = classify_path(
        PurePosixPath("src/workspace/tool.py"), project_excludes=(),
        sensitive_source_suffixes=resolve_graphify_compatibility(
            "0.9.48"
        ).sensitive_source_suffixes,
    )
    assert decision.action == "allow"


@pytest.mark.parametrize("pattern", [
    "!src/private.py", "/absolute/**", "../escape/**", "src\\private/**",
    "src//private/**", "src/./private/**", "C:/private/**", "src/bad\nname/**",
])
def test_project_excludes_reject_noncanonical_patterns(pattern: str) -> None:
    with pytest.raises(PrivacyError):
        classify_path(
            PurePosixPath("src/app.py"), project_excludes=(pattern,),
            sensitive_source_suffixes=resolve_graphify_compatibility(
                "0.9.48"
            ).sensitive_source_suffixes,
        )


def test_sensitive_source_is_scanned_but_sensitive_data_is_not_staged(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/credentials.py").write_text(
        "TOKEN_NAME = getenv('TOKEN_NAME')\n", encoding="utf-8"
    )
    (repo / "src/database-creds.json").write_text(
        '{"url":"postgres://u:' + 'long-password@db"}\n', encoding="utf-8"
    )
    staged = stage_input_with_receipt(
        repo, manifest_v2(), tmp_path / "stage", tmp_path / "receipt.json"
    )
    assert staged.files == (PurePosixPath("src/credentials.py"),)
    assert staged.projection_digest is not None
    assert not (staged.root / "src/database-creds.json").exists()
    receipt = load_staging_receipt(tmp_path / "receipt.json", manifest_v2())
    assert receipt.projection_digest == staged.projection_digest
    assert verify_staged_input(staged.root, receipt).source_digest == staged.source_digest


def test_projection_binds_ignore_policy_but_not_denied_payload(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("safe\n", encoding="utf-8")
    denied = repo / "src/private-token.json"
    denied.write_text("first denied bytes\n", encoding="utf-8")
    first = inspect_projection(repo, manifest_v2())
    denied.write_text("second denied bytes\n", encoding="utf-8")
    second = inspect_projection(repo, manifest_v2())
    assert first.source_digest == second.source_digest
    assert first.projection_digest == second.projection_digest
    (repo / ".graphifyignore").write_text(
        "src/generated.py\n", encoding="utf-8"
    )
    third = inspect_projection(repo, manifest_v2())
    assert third.source_digest == second.source_digest
    assert third.projection_digest != second.projection_digest


@pytest.mark.parametrize(("relative", "rule_id"), [
    ("src/__pycache__/app.cpython-313.pyc", "bytecode_cache"),
    ("src/.venv/lib/site.py", "virtual_environment"),
    ("src/node_modules/tool/index.js", "dependency_tree"),
    ("src/dist/app.js", "build_output"),
])
def test_ambient_generated_denies_are_audited_without_changing_projection_identity(
    tmp_path: Path, relative: str, rule_id: str,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("safe\n", encoding="utf-8")
    before = inspect_projection(repo, manifest_v2())

    denied = repo / relative
    denied.parent.mkdir(parents=True, exist_ok=True)
    denied.write_text("generated locally\n", encoding="utf-8")
    after = inspect_projection(repo, manifest_v2())

    assert after.source_digest == before.source_digest
    assert after.projection_digest == before.projection_digest
    assert after.decisions != before.decisions
    assert (f"deny:{rule_id}", 1) in after.reason_counts


@pytest.mark.parametrize(("relative", "rule_id", "project_excludes"), [
    ("src/private/note.md", "private_content", ()),
    ("src/auth-token.yaml", "sensitive_data_name", ()),
    ("src/runtime/job.json", "runtime_state", ()),
    ("src/generated/result.md", "project_exclude", ("src/generated/**",)),
])
def test_security_and_project_denies_remain_projection_identity_bearing(
    tmp_path: Path,
    relative: str,
    rule_id: str,
    project_excludes: tuple[str, ...],
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("safe\n", encoding="utf-8")
    manifest = replace(manifest_v2(), excludes=project_excludes)
    before = inspect_projection(repo, manifest)

    denied = repo / relative
    denied.parent.mkdir(parents=True, exist_ok=True)
    denied.write_text("denied material\n", encoding="utf-8")
    after = inspect_projection(repo, manifest)

    assert after.source_digest == before.source_digest
    assert after.projection_digest != before.projection_digest
    assert (f"deny:{rule_id}", 1) in after.reason_counts


def test_projection_and_stage_stay_on_open_root_after_path_swap(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("ORIGINAL = True\n", encoding="utf-8")
    with open_repository_access(repo) as repository:
        expected = inspect_projection(
            repo, manifest_v2(), repository_access=repository
        )
        repo.rename(tmp_path / "original")
        (repo / "src").mkdir(parents=True)
        (repo / "src/app.py").write_text(
            "TOKEN = 'replacement-secret'\n", encoding="utf-8"
        )
        staged = stage_input_with_receipt(
            repo, manifest_v2(), tmp_path / "stage", tmp_path / "receipt.json",
            repository_access=repository,
        )
    assert staged.source_digest == expected.source_digest
    assert (staged.root / "src/app.py").read_text(encoding="utf-8") == (
        "ORIGINAL = True\n"
    )
