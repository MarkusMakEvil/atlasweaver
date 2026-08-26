from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import subprocess
from uuid import UUID

import pytest

import project_knowledge.fleet as fleet_module
from project_knowledge.fleet import (
    FleetAliasKey,
    FleetConfigError,
    _git_alias_key,
    load_fleet_workspace,
    select_fleet_projects,
)
from project_knowledge.locking import open_repository_access
from tests.support import write_manifest_v2
from tests.test_manifest import write_manifest


API_UID = UUID("4ed9af24-5aa2-4eac-8d0a-3f622cc74948")
DOCS_UID = UUID("abf38b85-953d-4548-911d-8c1f90b12fc5")


def _write_project(repo: Path, project_id: str, uid: UUID) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    write_manifest_v2(
        repo,
        project_id=project_id,
        project_uid=uid,
        display_name=project_id.title(),
        obsidian_namespace=PurePosixPath(f"Projects/{project_id}/Generated"),
    )


def _write_workspace(tmp_path: Path, payload: str, name: str = "fleet.yaml") -> Path:
    path = tmp_path / name
    path.write_text(payload, encoding="utf-8")
    return path


def _workspace_payload(
    entries: tuple[tuple[str, str], ...] = (
        ("api", "repos/api"),
        ("documentation", "repos/docs"),
    ),
    *,
    defaults: str = "defaults:\n  max_parallel: 2\n",
) -> str:
    projects = "".join(
        f"  - id: {project_id}\n    repository: {repository}\n"
        for project_id, repository in entries
    )
    return f"schema_version: 1\nprojects:\n{projects}{defaults}"


def _valid_workspace(tmp_path: Path, *, defaults: bool = True) -> Path:
    _write_project(tmp_path / "repos/api", "api", API_UID)
    _write_project(tmp_path / "repos/docs", "documentation", DOCS_UID)
    return _write_workspace(
        tmp_path,
        _workspace_payload(defaults="defaults:\n  max_parallel: 2\n" if defaults else ""),
    )


def _opened_identity(path: Path) -> tuple[int, int]:
    with open_repository_access(path) as repository:
        return repository.identity


def test_fleet_workspace_is_project_agnostic_and_preserves_order(
    tmp_path: Path,
) -> None:
    workspace = load_fleet_workspace(_valid_workspace(tmp_path))

    assert workspace.schema_version == 1
    assert workspace.root == tmp_path.resolve()
    assert workspace.max_parallel == 2
    assert [project.id for project in workspace.projects] == [
        "api",
        "documentation",
    ]
    assert [project.repository for project in workspace.projects] == [
        PurePosixPath("repos/api"),
        PurePosixPath("repos/docs"),
    ]
    assert [project.project_uid for project in workspace.projects] == [
        API_UID,
        DOCS_UID,
    ]
    assert all(project.root.is_relative_to(tmp_path.resolve()) for project in workspace.projects)
    assert all(
        project.repository_identity == _opened_identity(project.root)
        for project in workspace.projects
    )
    assert not any((project.root / ".project-knowledge").exists() for project in workspace.projects)


def test_max_parallel_defaults_to_two(tmp_path: Path) -> None:
    workspace = load_fleet_workspace(_valid_workspace(tmp_path, defaults=False))

    assert workspace.max_parallel == 2


def test_select_fleet_projects_returns_workspace_order(tmp_path: Path) -> None:
    workspace = load_fleet_workspace(_valid_workspace(tmp_path))

    selected = select_fleet_projects(workspace, ("documentation", "api"))

    assert [project.id for project in selected] == ["api", "documentation"]
    assert select_fleet_projects(workspace, ()) == workspace.projects


@pytest.mark.parametrize(
    ("project_ids", "code"),
    [
        (("api", "api"), "fleet_selection_duplicate"),
        (("missing",), "fleet_project_unknown"),
        (("API",), "fleet_selection_invalid"),
    ],
)
def test_select_fleet_projects_rejects_duplicate_unknown_or_invalid_values(
    tmp_path: Path, project_ids: tuple[str, ...], code: str
) -> None:
    workspace = load_fleet_workspace(_valid_workspace(tmp_path))

    with pytest.raises(FleetConfigError) as raised:
        select_fleet_projects(workspace, project_ids)

    assert raised.value.code == code


def _invalid_workspace_fixture(tmp_path: Path, case: str) -> Path:
    _write_project(tmp_path / "repos/api", "api", API_UID)
    _write_project(tmp_path / "repos/docs", "documentation", DOCS_UID)
    valid = _workspace_payload()

    payloads = {
        "duplicate_yaml_key": valid.replace(
            "schema_version: 1\n", "schema_version: 1\nschema_version: 1\n"
        ),
        "yaml_alias": (
            "schema_version: 1\n"
            "project: &project {id: api, repository: repos/api}\n"
            "projects:\n  - *project\n"
        ),
        "yaml_merge": (
            "schema_version: 1\nprojects:\n"
            "  - <<: &base {id: api}\n    repository: repos/api\n"
        ),
        "second_document": valid + "---\nschema_version: 1\nprojects: []\n",
        "unknown_top_key": valid + "owner: platform\n",
        "unknown_project_key": valid.replace(
            "    repository: repos/api\n",
            "    repository: repos/api\n    owner: platform\n",
            1,
        ),
        "unknown_defaults_key": valid + "  timeout: 30\n",
        "boolean_schema": valid.replace("schema_version: 1", "schema_version: true"),
        "boolean_parallel": valid.replace("max_parallel: 2", "max_parallel: true"),
        "parallel_zero": valid.replace("max_parallel: 2", "max_parallel: 0"),
        "parallel_nine": valid.replace("max_parallel: 2", "max_parallel: 9"),
        "empty_projects": "schema_version: 1\nprojects: []\n",
        "duplicate_id": _workspace_payload(
            (("api", "repos/api"), ("api", "repos/docs"))
        ),
        "duplicate_lexical_path": _workspace_payload(
            (("api", "repos/api"), ("documentation", "repos/api"))
        ),
        "duplicate_canonical_path": _workspace_payload(
            (("api", "repos/api"), ("documentation", "repos/api/"))
        ),
        "absolute_path": _workspace_payload((("api", str(tmp_path / "repos/api")),)),
        "parent_escape": _workspace_payload((("api", "../api"),)),
        "manifest_id_mismatch": _workspace_payload((("different", "repos/api"),)),
    }

    if case in payloads:
        return _write_workspace(tmp_path, payloads[case])
    if case == "duplicate_uid":
        _write_project(tmp_path / "repos/docs", "documentation", API_UID)
        return _write_workspace(tmp_path, valid)
    if case == "symlink_repo":
        (tmp_path / "repos/link").symlink_to(tmp_path / "repos/api", target_is_directory=True)
        return _write_workspace(
            tmp_path, _workspace_payload((("api", "repos/link"),))
        )
    if case == "non_directory_repo":
        (tmp_path / "repos/not-a-directory").write_text("no\n", encoding="utf-8")
        return _write_workspace(
            tmp_path, _workspace_payload((("api", "repos/not-a-directory"),))
        )
    if case == "nested_repo":
        _write_project(tmp_path / "repos/api/child", "child", DOCS_UID)
        return _write_workspace(
            tmp_path,
            _workspace_payload((("api", "repos/api"), ("child", "repos/api/child"))),
        )
    if case == "worktree_alias":
        common = tmp_path / "shared.git"
        subprocess.run(
            ["git", "init", "--bare", "--quiet", str(common)],
            check=True,
            capture_output=True,
            text=True,
        )
        marker = f"gitdir: {common}\n"
        (tmp_path / "repos/api/.git").write_text(marker, encoding="utf-8")
        (tmp_path / "repos/docs/.git").write_text(marker, encoding="utf-8")
        return _write_workspace(tmp_path, valid)
    if case == "manifest_missing":
        missing = tmp_path / "repos/missing"
        missing.mkdir()
        return _write_workspace(
            tmp_path, _workspace_payload((("missing", "repos/missing"),))
        )
    if case == "manifest_v1_uid_missing":
        repo = tmp_path / "repos/v1-project"
        repo.mkdir()
        write_manifest(
            repo,
            project_id="v1-project",
            display_name="V1 Project",
            obsidian_namespace="Projects/v1-project/Generated",
        )
        return _write_workspace(
            tmp_path, _workspace_payload((("v1-project", "repos/v1-project"),))
        )
    if case == "workspace_symlink":
        actual = _write_workspace(tmp_path, valid, "actual.yaml")
        link = tmp_path / "fleet.yaml"
        link.symlink_to(actual)
        return link
    raise AssertionError(f"unknown invalid fixture: {case}")


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_yaml_key",
        "yaml_alias",
        "yaml_merge",
        "second_document",
        "unknown_top_key",
        "unknown_project_key",
        "unknown_defaults_key",
        "boolean_schema",
        "boolean_parallel",
        "parallel_zero",
        "parallel_nine",
        "empty_projects",
        "duplicate_id",
        "duplicate_uid",
        "duplicate_lexical_path",
        "duplicate_canonical_path",
        "absolute_path",
        "parent_escape",
        "symlink_repo",
        "non_directory_repo",
        "nested_repo",
        "worktree_alias",
        "manifest_missing",
        "manifest_id_mismatch",
        "manifest_v1_uid_missing",
        "workspace_symlink",
    ],
)
def test_fleet_workspace_rejects_invalid_or_aliased_projects(
    tmp_path: Path, case: str
) -> None:
    path = _invalid_workspace_fixture(tmp_path, case)

    with pytest.raises(FleetConfigError) as raised:
        load_fleet_workspace(path)

    assert raised.value.code.startswith("fleet_")
    assert str(tmp_path) not in raised.value.public_message


def test_fleet_rejects_workspace_or_manifest_over_256_kib(tmp_path: Path) -> None:
    oversized_workspace = tmp_path / "oversized.yaml"
    oversized_workspace.write_bytes(b"x" * 262_145)

    with pytest.raises(FleetConfigError) as workspace_error:
        load_fleet_workspace(oversized_workspace)
    assert workspace_error.value.code == "fleet_document_too_large"

    workspace = _valid_workspace(tmp_path)
    (tmp_path / "repos/api/.graphify-project.yaml").write_bytes(b"x" * 262_145)
    with pytest.raises(FleetConfigError) as manifest_error:
        load_fleet_workspace(workspace)
    assert manifest_error.value.code == "fleet_manifest_too_large"


def _write_init_journal(repo: Path, state: str) -> None:
    private = repo / ".project-knowledge"
    private.mkdir(mode=0o700)
    if state == "corrupt":
        payload = b"not json\n"
    else:
        transaction_id = "0" * 32
        payload = json.dumps(
            {
                "schema_version": 1,
                "phase": "prepared",
                "files": [
                    {
                        "destination": ".graphify-project.yaml",
                        "temporary": f"init-{transaction_id}.manifest",
                        "sha256": "0" * 64,
                    },
                    {
                        "destination": ".graphifyignore",
                        "temporary": f"init-{transaction_id}.ignore",
                        "sha256": "0" * 64,
                    },
                ],
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    journal = private / "init-transaction.json"
    journal.write_bytes(payload)
    journal.chmod(0o600)


@pytest.mark.parametrize("journal_state", ["recoverable", "corrupt"])
def test_fleet_loader_refuses_init_journal_before_manifest_parse(
    tmp_path: Path, journal_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _valid_workspace(tmp_path)
    _write_init_journal(tmp_path / "repos/api", journal_state)
    monkeypatch.setattr(
        fleet_module,
        "load_manifest_payload",
        lambda *args, **kwargs: pytest.fail("manifest parsed before recovery gate"),
    )

    with pytest.raises(FleetConfigError) as raised:
        load_fleet_workspace(workspace)

    assert raised.value.code == "init_recovery_required"


def test_fleet_identity_comes_from_same_descriptor_as_manifest_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_path = _valid_workspace(tmp_path)
    captured: list[tuple[int, int]] = []
    original = fleet_module._read_regular_at

    def capture(
        parent_fd: int, name: str, limit: int, code_prefix: str
    ) -> tuple[bytes, os.stat_result]:
        if name == ".graphify-project.yaml":
            info = os.fstat(parent_fd)
            captured.append((info.st_dev, info.st_ino))
        return original(parent_fd, name, limit, code_prefix)

    monkeypatch.setattr(fleet_module, "_read_regular_at", capture)

    workspace = load_fleet_workspace(workspace_path)

    assert [project.repository_identity for project in workspace.projects] == captured


def test_git_alias_checkpoint_cannot_redirect_opened_git_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    original = tmp_path / "original.git"
    replacement = tmp_path / "replacement.git"
    original.mkdir()
    replacement.mkdir()
    marker = repo / ".git"
    marker.write_text(f"gitdir: {original}\n", encoding="utf-8")
    replacement_marker = repo / "replacement-marker"
    replacement_marker.write_text(f"gitdir: {replacement}\n", encoding="utf-8")
    original_info = original.stat()
    replacement_info = replacement.stat()

    def replace_marker() -> None:
        os.replace(replacement_marker, marker)

    monkeypatch.setattr(
        fleet_module, "_after_git_alias_open_before_finalize", replace_marker
    )

    with open_repository_access(repo) as repository:
        try:
            key = _git_alias_key(repository)
        except FleetConfigError as error:
            assert error.code == "fleet_git_changed"
        else:
            assert key == FleetAliasKey(
                "git", (original_info.st_dev, original_info.st_ino)
            )
            assert key.identity != (replacement_info.st_dev, replacement_info.st_ino)


def test_gitdir_marker_rejects_noncanonical_target(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    target = repo / "targets/git"
    target.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: targets//git\n", encoding="utf-8")

    with open_repository_access(repo) as repository:
        with pytest.raises(FleetConfigError) as raised:
            _git_alias_key(repository)

    assert raised.value.code == "fleet_git_changed"


def _run_git(*arguments: str) -> None:
    subprocess.run(
        ["git", *arguments], check=True, capture_output=True, text=True
    )


def test_real_linked_worktrees_resolve_dotdot_commondir_and_alias(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main"
    feature = tmp_path / "feature"
    _run_git("init", "--quiet", str(main))
    (main / "README.md").write_text("fleet\n", encoding="utf-8")
    _run_git("-C", str(main), "add", "README.md")
    _run_git(
        "-C",
        str(main),
        "-c",
        "user.name=Fleet Test",
        "-c",
        "user.email=fleet@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "initial",
    )
    _run_git(
        "-C", str(main), "worktree", "add", "--quiet", "-b", "feature", str(feature)
    )
    _write_project(main, "main", API_UID)
    _write_project(feature, "feature", DOCS_UID)
    workspace = _write_workspace(
        tmp_path,
        _workspace_payload((("main", "main"), ("feature", "feature"))),
    )

    assert (feature / ".git").read_text(encoding="utf-8").startswith("gitdir: ")
    with pytest.raises(FleetConfigError) as raised:
        load_fleet_workspace(workspace)

    assert raised.value.code == "fleet_worktree_alias"
