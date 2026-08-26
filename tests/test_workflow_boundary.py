from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import project_knowledge.workflow_boundary as workflow_module
from project_knowledge.workflow_boundary import (
    WorkflowBoundaryError,
    _main,
    admit_workflow_extraction_mode,
    open_workflow_repository,
)
from tests.support import write_manifest_v2


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


def test_internal_build_has_closed_root_and_output_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumer, _, trusted = checkouts(tmp_path)
    bundle = tmp_path / "bundle.zip"
    summary = tmp_path / "summary.json"
    calls: list[tuple[Path, str, Path, Path, Path]] = []
    monkeypatch.setattr(
        workflow_module,
        "run_workflow_build",
        lambda checkout, root, forbidden, output, report: calls.append(
            (checkout, root, forbidden, output, report)
        ),
        raising=False,
    )

    assert workflow_module._main([
        "build",
        "--consumer-checkout", str(consumer),
        "--repo-root", "nested/repo",
        "--trusted-tool-checkout", str(trusted),
        "--output-bundle", str(bundle),
        "--output-summary", str(summary),
    ]) == 0
    assert calls == [(consumer, "nested/repo", trusted, bundle, summary)]


def _workflow_project(tmp_path: Path) -> tuple[Path, Path]:
    consumer = tmp_path / "consumer"
    (consumer / "src").mkdir(parents=True)
    (consumer / "src/app.py").write_text("safe = True\n", encoding="utf-8")
    write_manifest_v2(consumer)
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    return consumer, trusted


def _workflow_health(impact: str) -> SimpleNamespace:
    return SimpleNamespace(
        core_status="healthy",
        trust=SimpleNamespace(impact=impact),
        to_dict=lambda: {
            "schema_version": 2,
            "core_status": "healthy",
            "trust": {"impact": impact, "limitations": []},
        },
    )


@pytest.mark.parametrize(
    ("backend", "model", "deep", "impact"),
    [
        ("", "", "false", "trusted"),
        ("", "", "false", "navigation"),
        ("ollama", "llama3.2", "true", "trusted"),
        ("ollama", "llama3.2", "true", "navigation"),
    ],
)
def test_check_enforces_required_trusted_impact_after_final_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    model: str,
    deep: str,
    impact: str,
) -> None:
    consumer, trusted = _workflow_project(tmp_path)
    output = tmp_path / "check"
    monkeypatch.setenv("ATLASWEAVER_BACKEND", backend)
    monkeypatch.setenv("ATLASWEAVER_MODEL", model)
    monkeypatch.setenv("ATLASWEAVER_DEEP", deep)
    monkeypatch.setenv("ATLASWEAVER_REQUIRE_IMPACT_TRUST", "true")
    monkeypatch.setattr(
        workflow_module,
        "inspect_projection",
        lambda *args, **kwargs: SimpleNamespace(files=("src/app.py",), reason_counts={}),
    )
    monkeypatch.setattr(workflow_module, "resolve_graphify_executable", lambda: Path("graphify"))
    monkeypatch.setattr(
        workflow_module,
        "probe_graphify",
        lambda *args, **kwargs: SimpleNamespace(version="0.9.48"),
    )
    monkeypatch.setattr(
        workflow_module,
        "doctor_project",
        lambda *args, **kwargs: SimpleNamespace(
            to_dict=lambda: {"schema_version": 1, "status": "ready"}
        ),
    )
    monkeypatch.setattr(workflow_module, "inspect_project_state", lambda *args, **kwargs: object())
    monkeypatch.setattr(workflow_module, "assess_health", lambda state: _workflow_health(impact))

    if impact == "navigation":
        with pytest.raises(WorkflowBoundaryError) as raised:
            workflow_module.run_workflow_check(consumer, ".", trusted, output)
        assert raised.value.code == "workflow_root_invalid"
        assert not output.exists()
        return

    workflow_module.run_workflow_check(consumer, ".", trusted, output)
    assert (output / "health.json").is_file()


@pytest.mark.parametrize(
    ("backend", "model", "deep"),
    [("", "", "false"), ("ollama", "llama3.2", "true")],
)
def test_build_rejects_required_navigation_impact_after_refresh_before_pack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    model: str,
    deep: str,
) -> None:
    consumer, trusted = _workflow_project(tmp_path)
    monkeypatch.setenv("ATLASWEAVER_BACKEND", backend)
    monkeypatch.setenv("ATLASWEAVER_MODEL", model)
    monkeypatch.setenv("ATLASWEAVER_DEEP", deep)
    monkeypatch.setenv("ATLASWEAVER_REQUIRE_IMPACT_TRUST", "true")
    monkeypatch.setattr(
        workflow_module,
        "doctor_project",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    refreshed: list[bool] = []
    monkeypatch.setattr(
        workflow_module,
        "refresh_project",
        lambda *args, **kwargs: refreshed.append(True) or SimpleNamespace(status="refreshed"),
    )
    monkeypatch.setattr(workflow_module, "inspect_project_state", lambda *args, **kwargs: object())
    monkeypatch.setattr(workflow_module, "assess_health", lambda state: _workflow_health("navigation"))
    monkeypatch.setattr(
        workflow_module,
        "pack_bundle",
        lambda *args, **kwargs: pytest.fail("bundle packed before impact trust gate"),
    )

    with pytest.raises(WorkflowBoundaryError) as raised:
        workflow_module.run_workflow_build(
            consumer,
            ".",
            trusted,
            tmp_path / "build/bundle.zip",
            tmp_path / "build/summary.json",
        )

    assert refreshed == [True]
    assert raised.value.code == "workflow_root_invalid"
