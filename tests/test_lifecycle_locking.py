from concurrent.futures import ThreadPoolExecutor
import contextvars
import os
from pathlib import Path

import pytest

from project_knowledge.locking import (
    ExclusiveDescriptorLock,
    TransactionLockError,
    assert_lifecycle_lock_held,
    capture_lifecycle_descriptors,
    capture_lifecycle_repository,
    open_repository_access,
    open_state_root,
    repository_lifecycle_lock,
)
from project_knowledge.manifest import (
    ManifestError,
    assert_current_manifest_unchanged,
    load_manifest,
    require_current_manifest,
)
from tests.support import manifest_v2, write_manifest_v2


def test_state_root_is_private_real_and_no_follow(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with open_state_root(repo, create=True) as state:
        assert state is not None
        assert state.path == repo / ".project-knowledge"
        assert state.descriptor >= 0

    assert (repo / ".project-knowledge").stat().st_mode & 0o777 == 0o700


def test_state_root_rejects_symlink_and_unsafe_permissions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, repo / ".project-knowledge", target_is_directory=True)

    with pytest.raises(TransactionLockError, match="private state"):
        open_state_root(repo, create=True)


def test_read_only_repository_access_rejects_replacement_without_state(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with open_repository_access(repo) as loaded:
        expected = loaded.identity
    repo.rename(tmp_path / "original")
    repo.mkdir()

    with pytest.raises(TransactionLockError) as raised:
        with open_repository_access(repo, expected_repository_identity=expected):
            pass

    assert raised.value.kind == "authority"
    assert not (repo / ".project-knowledge").exists()


def test_expected_identity_precedes_lifecycle_state_creation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with open_repository_access(repo) as loaded:
        expected = loaded.identity
    repo.rename(tmp_path / "original")
    repo.mkdir()

    with pytest.raises(TransactionLockError) as raised:
        with repository_lifecycle_lock(
            repo, expected_repository_identity=expected
        ):
            pass

    assert raised.value.kind == "authority"
    assert not (repo / ".project-knowledge").exists()


def test_lifecycle_lock_serializes_and_exposes_order(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with repository_lifecycle_lock(repo):
        assert_lifecycle_lock_held(repo)
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocked = pool.submit(
                lambda: repository_lifecycle_lock(repo, timeout=0.01).__enter__()
            )
            with pytest.raises(TransactionLockError, match="timed out"):
                blocked.result()

    with pytest.raises(TransactionLockError, match="lifecycle lock is required"):
        assert_lifecycle_lock_held(repo)


def test_copied_context_cannot_export_lifecycle_authority(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    captured: contextvars.Context | None = None

    with repository_lifecycle_lock(repo):
        captured = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(TransactionLockError):
                pool.submit(captured.run, assert_lifecycle_lock_held, repo).result()

    assert captured is not None
    with pytest.raises(TransactionLockError):
        captured.run(assert_lifecycle_lock_held, repo)


def test_noncreating_absence_has_no_side_effects(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(TransactionLockError) as absent:
        with repository_lifecycle_lock(repo, create=False):
            pass

    assert absent.value.kind == "unavailable"
    assert str(repo) not in str(absent.value)
    assert not (repo / ".project-knowledge").exists()


@pytest.mark.parametrize(
    "name", ["", ".", "..", "../outside", "sub/lock", "bad\\lock", "bad\nlock"]
)
def test_descriptor_lock_rejects_noncanonical_basename(
    tmp_path: Path, name: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with open_state_root(repo, create=True) as state:
        assert state is not None
        before = tuple(repo.rglob("*"))
        with pytest.raises(TransactionLockError):
            ExclusiveDescriptorLock(state.descriptor, name)
        assert tuple(repo.rglob("*")) == before


def test_lifecycle_descriptor_capture_stays_on_original_inode_after_swap(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "marker").write_text("original", encoding="utf-8")

    with repository_lifecycle_lock(repo):
        repo.rename(tmp_path / "original")
        repo.mkdir()
        (repo / "marker").write_text("replacement", encoding="utf-8")
        with capture_lifecycle_descriptors(repo) as descriptors:
            fd = os.open("marker", os.O_RDONLY, dir_fd=descriptors.repository_descriptor)
            try:
                assert os.read(fd, 32) == b"original"
            finally:
                os.close(fd)


def test_descriptor_loaded_manifest_is_pinned_for_later_assertion(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = write_manifest_v2(repo)
    original = path.read_bytes()

    with open_repository_access(repo) as repository:
        current = load_manifest(path, repo, repository_access=repository)
        path.write_bytes(original.replace(b"track_html: false", b"track_html: true "))
        path.write_bytes(original)
        with pytest.raises(ManifestError) as raised:
            assert_current_manifest_unchanged(
                repo, current, repository_access=repository
            )

    assert raised.value.kind == "changed"


def test_require_current_manifest_rejects_semantic_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    write_manifest_v2(repo)

    with open_repository_access(repo) as repository:
        with pytest.raises(ManifestError) as raised:
            require_current_manifest(
                repo,
                manifest_v2(track_html=True),
                repository_access=repository,
            )

    assert raised.value.kind == "changed"


def test_capture_lifecycle_repository_owns_a_duplicate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with repository_lifecycle_lock(repo):
        with capture_lifecycle_repository(repo) as repository:
            os.fstat(repository.descriptor)
            descriptor = repository.descriptor
        with pytest.raises(OSError):
            os.fstat(descriptor)
