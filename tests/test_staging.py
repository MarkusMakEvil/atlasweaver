from dataclasses import replace
import os
import stat
from pathlib import Path, PurePosixPath

import pytest

from project_knowledge.models import ProjectManifest
from project_knowledge.staging import (
    SecretShapeError,
    SourceChangedError,
    StagingCleanupError,
    StagingError,
    stage_input,
)


def project_fixture(tmp_path: Path) -> tuple[Path, Path, ProjectManifest]:
    """Create a repository whose only intended input root is ``src``."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    manifest = ProjectManifest(
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
    return repo, tmp_path / "staged", manifest


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_stage_copies_only_safe_regular_files(tmp_path: Path) -> None:
    """Dropping file-type and deny checks could stage secrets through a link."""
    repo, destination, manifest = project_fixture(tmp_path)
    write(repo / "src/app.py", "print('ok')\n")
    write(repo / ".env", "TOKEN=private\n")
    write(repo / "workspace/draft.md", "private\n")
    os.symlink(repo / ".env", repo / "src/leak")

    staged = stage_input(repo, manifest, destination)

    assert staged.files == (PurePosixPath("src/app.py"),)
    assert (destination / "src/app.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert not (destination / ".env").exists()
    assert not (destination / "src/leak").exists()


def test_stage_does_not_follow_a_directory_replaced_with_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink swap between lstat and open must not redirect staging."""
    repo, destination, manifest = project_fixture(tmp_path)
    source_directory = repo / "src"
    write(source_directory / "app.py", "safe\n")
    external = tmp_path / "external"
    write(external / "exfil.py", "private external source\n")

    from project_knowledge import staging

    original_open_directory_at = staging._open_directory_at
    swap_reached = False

    def replace_after_lstat(parent_descriptor: int, name: str) -> int:
        nonlocal swap_reached
        if name == "src" and not swap_reached:
            swap_reached = True
            source_directory.rename(repo / "original-src")
            os.symlink(external, source_directory, target_is_directory=True)
        return original_open_directory_at(parent_descriptor, name)

    monkeypatch.setattr(staging, "_open_directory_at", replace_after_lstat)

    try:
        staged = stage_input(repo, manifest, destination)
    except OSError:
        assert swap_reached
        assert not destination.exists()
    else:
        assert swap_reached
        assert staged.files == (PurePosixPath("src/app.py"),)
        assert not (destination / "src/exfil.py").exists()



def test_stage_enforces_global_and_project_denies_inside_include_roots(
    tmp_path: Path,
) -> None:
    """Bypassing the shared deny contract would expose protected in-root material."""
    repo, destination, manifest = project_fixture(tmp_path)
    manifest = replace(manifest, excludes=("src/generated/**",))
    write(repo / "src/app.py", "safe\n")
    write(repo / "src/auth-token.yaml", "private\n")
    write(repo / "src/private/draft.md", "private\n")
    write(repo / "src/node_modules/tool.js", "runtime\n")
    write(repo / "src/generated/result.md", "excluded by project\n")

    staged = stage_input(repo, manifest, destination)

    assert staged.files == (PurePosixPath("src/app.py"),)


def test_stage_excludes_nested_git_repositories_and_submodules(tmp_path: Path) -> None:
    """Descending into a nested checkout must not leak third-party repository data."""
    repo, destination, manifest = project_fixture(tmp_path)
    write(repo / "src/app.py", "safe\n")
    write(repo / "src/vendor/.git/config", "nested repository\n")
    write(repo / "src/vendor/private.py", "do not stage\n")
    write(repo / "src/module/.git", "gitdir: ../.git/modules/module\n")
    write(repo / "src/module/private.py", "do not stage\n")

    staged = stage_input(repo, manifest, destination)

    assert staged.files == (PurePosixPath("src/app.py"),)
    assert not (destination / "src/vendor").exists()
    assert not (destination / "src/module").exists()


def test_stage_skips_fifos_without_opening_them(tmp_path: Path) -> None:
    """Treating every entry as a file would block forever when a FIFO is present."""
    repo, destination, manifest = project_fixture(tmp_path)
    write(repo / "src/app.py", "safe\n")
    fifo = repo / "src/events"
    os.mkfifo(fifo)

    staged = stage_input(repo, manifest, destination)

    assert staged.files == (PurePosixPath("src/app.py"),)
    assert not (destination / "src/events").exists()


def test_stage_rejects_a_source_that_changes_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting the post-read identity check could make a digest describe stale input."""
    repo, destination, manifest = project_fixture(tmp_path)
    source = repo / "src/app.py"
    write(source, "before\n")

    from project_knowledge import staging

    original_read = staging._read_regular_file

    def mutate_after_read(*args: object, **kwargs: object) -> bytes:
        payload = original_read(*args, **kwargs)
        source.write_text("after\n", encoding="utf-8")
        return payload

    monkeypatch.setattr(staging, "_read_regular_file", mutate_after_read)

    with pytest.raises(SourceChangedError, match="src/app.py"):
        stage_input(repo, manifest, destination)

    assert not destination.exists()


def test_stage_rejects_destination_inside_an_include_root(tmp_path: Path) -> None:
    """Allowing this would recursively include the newly-created staging tree."""
    repo, _, manifest = project_fixture(tmp_path)
    write(repo / "src/app.py", "safe\n")

    with pytest.raises(StagingError, match="include root"):
        stage_input(repo, manifest, repo / "src/.staged")

    assert not (repo / "src/.staged").exists()


def test_stage_orders_files_and_digest_deterministically(tmp_path: Path) -> None:
    """Unsorted traversal would make equivalent inputs produce unstable provenance."""
    repo_one, destination_one, manifest = project_fixture(tmp_path / "one")
    repo_two, destination_two, _ = project_fixture(tmp_path / "two")
    for repo in (repo_one, repo_two):
        write(repo / "src/z.py", "z\n")
        write(repo / "src/a.py", "a\n")
        write(repo / "src/nested/m.py", "m\n")

    first = stage_input(repo_one, manifest, destination_one)
    second = stage_input(repo_two, manifest, destination_two)

    expected = (
        PurePosixPath("src/a.py"),
        PurePosixPath("src/nested/m.py"),
        PurePosixPath("src/z.py"),
    )
    assert first.files == expected
    assert second.files == expected
    assert first.source_digest == second.source_digest


def test_stage_removes_partial_output_after_permission_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed read must not leave a partial projection available to later consumers."""
    repo, destination, manifest = project_fixture(tmp_path)
    write(repo / "src/a.py", "first\n")
    write(repo / "src/b.py", "second\n")

    from project_knowledge import staging

    original_read = staging._read_regular_file

    def deny_second(relative: PurePosixPath, *args: object, **kwargs: object) -> bytes:
        if relative == PurePosixPath("src/b.py"):
            raise PermissionError("denied")
        return original_read(relative, *args, **kwargs)

    monkeypatch.setattr(staging, "_read_regular_file", deny_second)

    with pytest.raises(PermissionError, match="denied"):
        stage_input(repo, manifest, destination)

    assert not destination.exists()


def test_stage_creates_owner_private_files_and_directories(tmp_path: Path) -> None:
    """Relaxed modes would expose the supposedly private projection to other users."""
    repo, destination, manifest = project_fixture(tmp_path)
    write(repo / "src/nested/app.py", "safe\n")

    stage_input(repo, manifest, destination)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "src").stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "src/nested/app.py").stat().st_mode) == 0o600


def test_stage_honors_gitignore_graphifyignore_and_nested_env_denies(tmp_path: Path) -> None:
    repo, destination, manifest = project_fixture(tmp_path)
    write(repo / ".gitignore", "src/ignored.py\n")
    write(repo / ".graphifyignore", "src/generated-report.md\n")
    write(repo / "src/app.py", "safe\n")
    write(repo / "src/ignored.py", "ignored\n")
    write(repo / "src/generated-report.md", "ignored\n")
    write(repo / "src/config/.env.local", "TOKEN=private\n")

    staged = stage_input(repo, manifest, destination)

    assert staged.files == (PurePosixPath("src/app.py"),)


def test_stage_rejects_high_confidence_secret_content_before_copy(tmp_path: Path) -> None:
    repo, destination, manifest = project_fixture(tmp_path)
    secret_shape = "123456789:" + ("a" * 35)
    write(repo / "src/app.py", f'BOT_TOKEN="{secret_shape}"\n')

    with pytest.raises(SecretShapeError, match="secret-shaped"):
        stage_input(repo, manifest, destination)

    assert not destination.exists()


@pytest.mark.parametrize(
    "secret_shape",
    [
        "AK" + "IA" + ("A" * 16),
        "gh" + "p_" + ("a" * 36),
        "xox" + "b-" + ("1" * 12) + "-" + ("a" * 24),
        "password = \"" + ("correct-horse-" * 3) + "\"",
        "authorization: Bearer " + ("a" * 32),
        "postgresql://app:" + ("p" * 20) + "@db.example/app",
    ],
)
def test_stage_rejects_common_high_confidence_credentials(
    tmp_path: Path, secret_shape: str
) -> None:
    repo, destination, manifest = project_fixture(tmp_path)
    write(repo / "src/settings.txt", secret_shape + "\n")

    with pytest.raises(SecretShapeError, match="secret-shaped"):
        stage_input(repo, manifest, destination)

    assert not destination.exists()


def test_stage_surfaces_cleanup_failure_for_manual_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, destination, manifest = project_fixture(tmp_path)
    write(repo / "src/app.py", "safe\n")
    from project_knowledge import staging

    monkeypatch.setattr(staging, "iter_safe_files", lambda *args: (_ for _ in ()).throw(PermissionError("denied")))
    monkeypatch.setattr(staging.shutil, "rmtree", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("busy")))

    with pytest.raises(StagingCleanupError, match="cleanup failed"):
        stage_input(repo, manifest, destination)

    assert destination.exists()
