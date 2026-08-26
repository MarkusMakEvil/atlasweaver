from __future__ import annotations

import os
from pathlib import Path

import pytest

import project_knowledge.workflow_boundary as workflow_module
from project_knowledge.workflow_boundary import (
    WorkflowBoundaryError,
    _main,
    admit_workflow_extraction_mode,
    open_workflow_repository,
)


def checkouts(tmp_path: Path) -> tuple[Path, Path, Path]:
    consumer = tmp_path / "consumer"
    project = consumer / "nested/repo"
    project.mkdir(parents=True)
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    return consumer, project, trusted


def test_workflow_repository_retains_exact_descendant_and_git_scope(
    tmp_path: Path,
) -> None:
    consumer, project, trusted = checkouts(tmp_path)

    with open_workflow_repository(
        consumer, "nested/repo", forbidden_checkout=trusted
    ) as repository:
        assert repository.repository_identity == (
            project.stat().st_dev,
            project.stat().st_ino,
        )
        assert repository.segments == ("nested", "repo")
        assert repository.git_scope.project_segments == repository.segments
        repository.revalidate()


@pytest.mark.parametrize(
    "root",
    ["/etc", "../outside", "nested/../../outside", "nested//repo", "nested\\repo", ""],
)
def test_workflow_repository_rejects_invalid_roots(tmp_path: Path, root: str) -> None:
    consumer, _, trusted = checkouts(tmp_path)

    with pytest.raises(WorkflowBoundaryError) as raised:
        open_workflow_repository(consumer, root, forbidden_checkout=trusted)

    assert raised.value.code == "workflow_root_invalid"
    assert str(tmp_path) not in raised.value.message


def test_workflow_repository_rejects_symlink_and_trusted_alias(tmp_path: Path) -> None:
    consumer, project, trusted = checkouts(tmp_path)
    (consumer / "alias").symlink_to(project, target_is_directory=True)
    with pytest.raises(WorkflowBoundaryError) as raised:
        open_workflow_repository(consumer, "alias", forbidden_checkout=trusted)
    assert raised.value.code == "workflow_root_invalid"

    with pytest.raises(WorkflowBoundaryError) as raised:
        open_workflow_repository(trusted, ".", forbidden_checkout=trusted)
    assert raised.value.code == "workflow_root_forbidden"


def test_workflow_repository_detects_root_replacement(tmp_path: Path) -> None:
    consumer, project, trusted = checkouts(tmp_path)
    original = tmp_path / "original"
    repository = open_workflow_repository(
        consumer, "nested/repo", forbidden_checkout=trusted
    )
    try:
        project.rename(original)
        project.mkdir()
        (project / "attacker").write_text("replacement\n", encoding="utf-8")
        with pytest.raises(WorkflowBoundaryError) as raised:
            repository.revalidate()
        assert raised.value.code == "workflow_root_changed"
    finally:
        repository.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("backend", "model", "deep", "expected"),
    [
        ("", "", False, "code_only"),
        ("ollama", "llama3.2", False, "semantic"),
        ("ollama", "org/model:tag", True, "semantic"),
    ],
)
def test_extraction_mode_is_closed(
    backend: str, model: str, deep: bool, expected: str
) -> None:
    assert admit_workflow_extraction_mode(backend, model, deep) == expected


@pytest.mark.parametrize(
    ("backend", "model", "deep"),
    [("", "", True), ("ollama", "", False), ("", "model", False), ("unknown", "model", False)],
)
def test_extraction_mode_rejects_half_configured_inputs(
    backend: str, model: str, deep: bool
) -> None:
    with pytest.raises(WorkflowBoundaryError) as raised:
        admit_workflow_extraction_mode(backend, model, deep)
    assert raised.value.code == "workflow_extraction_invalid"


def test_internal_extraction_admission_deletes_bounded_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATLASWEAVER_BACKEND", "")
    monkeypatch.setenv("ATLASWEAVER_MODEL", "")
    monkeypatch.setenv("ATLASWEAVER_DEEP", "false")

    assert _main(["validate-extraction"]) == 0
    assert not {
        "ATLASWEAVER_BACKEND",
        "ATLASWEAVER_MODEL",
        "ATLASWEAVER_DEEP",
    } & os.environ.keys()


def test_internal_check_has_one_closed_fixed_path_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumer, _, trusted = checkouts(tmp_path)
    output = tmp_path / "output"
    calls: list[tuple[Path, str, Path, Path]] = []

    monkeypatch.setattr(
        workflow_module,
        "run_workflow_check",
        lambda checkout, root, forbidden, destination: calls.append(
            (checkout, root, forbidden, destination)
        ),
        raising=False,
    )

    assert workflow_module._main([
        "check",
        "--consumer-checkout", str(consumer),
        "--repo-root", "nested/repo",
        "--trusted-tool-checkout", str(trusted),
        "--output-directory", str(output),
    ]) == 0
    assert calls == [(consumer, "nested/repo", trusted, output)]


def test_internal_inspect_has_closed_root_and_output_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumer, _, trusted = checkouts(tmp_path)
    output = tmp_path / "inspect.json"
    calls: list[tuple[Path, str, Path, Path]] = []
    monkeypatch.setattr(
        workflow_module,
        "run_workflow_inspect",
        lambda checkout, root, forbidden, destination: calls.append(
            (checkout, root, forbidden, destination)
        ),
        raising=False,
    )

    assert workflow_module._main([
        "inspect",
        "--consumer-checkout", str(consumer),
        "--repo-root", "nested/repo",
        "--trusted-tool-checkout", str(trusted),
        "--output", str(output),
    ]) == 0
    assert calls == [(consumer, "nested/repo", trusted, output)]
