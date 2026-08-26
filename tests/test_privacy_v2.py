from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from project_knowledge.compatibility import resolve_graphify_compatibility
from project_knowledge.privacy import PrivacyError, classify_path


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
