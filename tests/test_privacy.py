from dataclasses import replace
from pathlib import PurePosixPath

import pytest

from project_knowledge.models import ProjectManifest
from project_knowledge.privacy import PrivacyError, effective_excludes, is_denied


@pytest.fixture
def manifest() -> ProjectManifest:
    return ProjectManifest(
        schema_version=1,
        project_id="demo",
        display_name="Demo",
        include_roots=(PurePosixPath("src"),),
        output_dir=PurePosixPath("graphify-out"),
        obsidian_namespace=PurePosixPath("Projects/demo/Generated"),
        excludes=("generated/**",),
        track_html=True,
        graphify_version="0.9.48",
    )


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".git/config",
        ".worktrees/x/a.py",
        "workspace/draft.md",
        "src/runtime/job.json",
        "src/drafts/post.md",
        "src/snapshots/state.json",
        "src/.env.local",
        "src/signing.pem",
        "config/auth-token.yaml",
        "Projects/demo/Notes/private.md",
        "node_modules/x.js",
    ],
)
def test_global_denies_cannot_be_reincluded(
    path: str, manifest: ProjectManifest
) -> None:
    with pytest.raises(PrivacyError, match="negated include"):
        effective_excludes(replace(manifest, excludes=(f"!{path}",)))


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".git/config",
        ".worktrees/x/a.py",
        "workspace/draft.md",
        "config/auth-token.yaml",
        "config/database-creds.json",
        "Projects/demo/Notes/private.md",
        "node_modules/x.js",
        "AUTH-TOKEN.yaml",
        "nested/PRIVATE/plan.md",
    ],
)
def test_global_sensitive_and_runtime_paths_are_denied(
    path: str, manifest: ProjectManifest
) -> None:
    assert is_denied(PurePosixPath(path), effective_excludes(manifest))


@pytest.mark.parametrize("path", ["contest.py", "src/notebook.py", "Projects/demo/Generated/a.md"])
def test_component_aware_denies_preserve_safe_paths(
    path: str, manifest: ProjectManifest
) -> None:
    assert not is_denied(PurePosixPath(path), effective_excludes(manifest))


def test_project_excludes_match_complete_path_components(manifest: ProjectManifest) -> None:
    excludes = effective_excludes(replace(manifest, excludes=("cache/*.json",)))

    assert is_denied(PurePosixPath("cache/index.json"), excludes)
    assert not is_denied(PurePosixPath("cache/nested/index.json"), excludes)


def test_repository_ignore_files_use_gitignore_semantics(
    tmp_path, manifest: ProjectManifest
) -> None:
    from project_knowledge.privacy import repository_ignores

    (tmp_path / ".gitignore").write_text("build/\n*.local\n!safe.local\n")
    (tmp_path / ".graphifyignore").write_text("/reports/private.md\n")
    ignored = repository_ignores(tmp_path)

    assert ignored(PurePosixPath("src/build/result.txt"))
    assert ignored(PurePosixPath("src/config.local"))
    assert not ignored(PurePosixPath("src/safe.local"))
    assert ignored(PurePosixPath("reports/private.md"))
    assert not ignored(PurePosixPath("src/reports/private.md"))


def test_nested_gitignore_takes_precedence_over_root_negation(
    tmp_path, manifest: ProjectManifest
) -> None:
    from project_knowledge.privacy import repository_ignores

    (tmp_path / "src").mkdir()
    (tmp_path / ".gitignore").write_text("!private.txt\n")
    (tmp_path / "src/.gitignore").write_text("private.txt\n")

    assert repository_ignores(tmp_path)(PurePosixPath("src/private.txt"))
