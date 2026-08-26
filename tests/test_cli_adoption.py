from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
import importlib.metadata
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from project_knowledge.lifecycle import RefreshResult
from project_knowledge.queries import QueryError
from tests.support import write_manifest_v2


def configured_v2_repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    write_manifest_v2(repo)
    return repo


def invoke_cli(repo: Path, capsys: pytest.CaptureFixture[str], *arguments: str) -> CompletedProcess[str]:
    from project_knowledge.cli import main

    argv = [arguments[0], "--repo", str(repo), *arguments[1:]]
    exit_code = main(argv)
    captured = capsys.readouterr()
    return CompletedProcess(argv, exit_code, captured.out, captured.err)


def payload(result: CompletedProcess[str]) -> dict[str, Any]:
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    return document


def command_choices() -> set[str]:
    from project_knowledge.cli import build_parser

    parser = build_parser()
    return set(
        next(
            action.choices
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
    )


def test_parser_exposes_high_level_and_legacy_commands() -> None:
    assert command_choices() == {
        "init",
        "manifest-migrate",
        "doctor",
        "refresh",
        "query",
        "path",
        "explain",
        "affected",
        "coverage",
        "registry-status",
        "registry-sync",
        "artifact",
        "pull",
        "install-agent",
        "uninstall-agent",
        "fleet",
        "detect",
        "preflight",
        "stage",
        "adapt",
        "scan-secrets",
        "validate",
        "promote",
        "atlas-prepare",
        "atlas-promote",
        "health",
    }


def test_high_level_refresh_has_no_binary_or_arbitrary_flag() -> None:
    from project_knowledge.cli import build_parser

    refresh = build_parser()._subparsers._group_actions[0].choices["refresh"]
    options = {value for action in refresh._actions for value in action.option_strings}
    assert "--graphify-binary" not in options
    assert "--graphify-flag" not in options
    assert {"--backend", "--model", "--deep", "--code-only", "--repo", "--json"} <= options


def test_trusted_preflight_has_semantic_shape_but_no_token_argv() -> None:
    from project_knowledge.cli import build_parser

    preflight = build_parser()._subparsers._group_actions[0].choices["preflight"]
    options = {value for action in preflight._actions for value in action.option_strings}
    assert {"--backend", "--model", "--deep"} <= options
    assert not {
        "--token",
        "--credential",
        "--api-key",
        "--graphify-binary",
        "--binary",
        "--executable",
    } & options


def test_root_version_comes_from_installed_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from project_knowledge.cli import build_parser

    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.8.7-test")
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == "project-knowledge 9.8.7-test"


def test_init_preview_then_apply_is_explicit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)

    preview = invoke_cli(
        repo,
        capsys,
        "init",
        "--project-id",
        "demo",
        "--include-root",
        "src",
        "--json",
    )

    assert preview.returncode == 0
    document = payload(preview)
    assert document["status"] == "preview"
    assert document["project_uid"] == "<generated-on-apply>"
    assert "schema_version: 2" in document["manifest"]
    assert not (repo / ".graphify-project.yaml").exists()

    applied = invoke_cli(
        repo,
        capsys,
        "init",
        "--project-id",
        "demo",
        "--include-root",
        "src",
        "--apply",
        "--json",
    )
    assert applied.returncode == 0
    assert payload(applied)["status"] == "initialized"
    assert (repo / ".graphify-project.yaml").is_file()


class _DoctorFixture:
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "command": "doctor",
            "status": "ready",
            "package": {},
            "manifest": {},
            "projection": {},
            "graphify": {},
            "skill": {},
            "health": {},
            "diagnostics": [],
        }


def _refresh_fixture(*args: object, **kwargs: object) -> RefreshResult:
    return RefreshResult(
        status="refreshed",
        source_digest="a" * 64,
        projection_digest="b" * 64,
        graph_digest="c" * 64,
        generation_digest="d" * 64,
        build_epoch=1,
        core_status="healthy",
        trust="navigation",
        limitations=("pre_dedup_edge_projection_unavailable",),
    )


def test_doctor_and_refresh_use_high_level_stable_envelopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = configured_v2_repository(tmp_path)
    monkeypatch.setattr("project_knowledge.cli.doctor_project", lambda repo: _DoctorFixture())
    monkeypatch.setattr("project_knowledge.cli.refresh_project", _refresh_fixture)

    doctor = invoke_cli(repo, capsys, "doctor", "--json")
    assert payload(doctor)["command"] == "doctor"

    refresh = invoke_cli(repo, capsys, "refresh", "--code-only", "--json")
    assert payload(refresh) == {
        "schema_version": 1,
        "command": "refresh",
        "status": "refreshed",
        "project_id": "demo",
        "source_digest": "a" * 64,
        "projection_digest": "b" * 64,
        "graph_digest": "c" * 64,
        "generation_digest": "d" * 64,
        "build_epoch": 1,
        "core_status": "healthy",
        "trust": "navigation",
        "limitations": ["pre_dedup_edge_projection_unavailable"],
    }


def test_promoted_but_stale_is_nonzero_and_keeps_verified_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = configured_v2_repository(tmp_path)
    result = RefreshResult(
        status="promoted_but_stale",
        source_digest="a" * 64,
        projection_digest="b" * 64,
        graph_digest="c" * 64,
        generation_digest="d" * 64,
        build_epoch=2,
        core_status="stale",
        trust="navigation",
        limitations=("post_promotion_verification_failed",),
    )
    monkeypatch.setattr("project_knowledge.cli.refresh_project", lambda *args, **kwargs: result)

    completed = invoke_cli(repo, capsys, "refresh", "--code-only", "--json")

    assert completed.returncode == 3
    document = payload(completed)
    assert document["status"] == "promoted_but_stale"
    assert document["generation_digest"] == "d" * 64
    assert document["build_epoch"] == 2
    assert "recovery_id" not in document


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (("--backend", "openai"), "semantic_model_required"),
        (("--backend", "openai", "--model=--api-key"), "semantic_model_required"),
        (("--backend", "openai", "--model", "bad\nmodel"), "semantic_model_required"),
        (("--backend", "openai", "--model", "ghp_" + "a" * 32), "semantic_model_required"),
        (("--model", "gpt-5"), "semantic_backend_required"),
        (("--deep",), "semantic_backend_required"),
    ],
)
def test_refresh_rejects_semantic_shape_before_secret_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    code: str,
) -> None:
    repo = configured_v2_repository(tmp_path)
    monkeypatch.setattr(
        "project_knowledge.cli._read_secret_environment",
        lambda name: pytest.fail(f"secret environment read before admission: {name}"),
        raising=False,
    )
    monkeypatch.setattr(
        "project_knowledge.cli.refresh_project",
        lambda *args, **kwargs: pytest.fail("refresh called before option admission"),
    )

    result = invoke_cli(repo, capsys, "refresh", *arguments, "--json")

    assert payload(result)["error"]["code"] == code


def test_refresh_recovery_gate_precedes_secret_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = configured_v2_repository(tmp_path)
    state = repo / ".project-knowledge"
    state.mkdir(mode=0o700)
    (state / "init-transaction.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        "project_knowledge.cli._read_secret_environment",
        lambda name: pytest.fail(f"secret environment read before recovery gate: {name}"),
        raising=False,
    )

    result = invoke_cli(
        repo,
        capsys,
        "refresh",
        "--backend",
        "openai",
        "--model",
        "gpt-5",
        "--json",
    )

    assert payload(result)["error"]["code"] == "init_recovery_required"


def test_refresh_maps_trusted_generic_token_to_only_canonical_ambient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = configured_v2_repository(tmp_path)
    captured: list[dict[str, str] | None] = []

    def recording_refresh(*args: object, ambient=None, **kwargs: object) -> RefreshResult:
        captured.append(None if ambient is None else dict(ambient))
        return _refresh_fixture(*args, **kwargs)

    monkeypatch.setattr("project_knowledge.cli.refresh_project", recording_refresh)
    monkeypatch.setenv("ATLASWEAVER_BACKEND_TOKEN", "trusted-generic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-local-secret")

    result = invoke_cli(
        repo,
        capsys,
        "refresh",
        "--backend",
        "openai",
        "--model",
        "gpt-5",
        "--json",
    )

    assert result.returncode == 0
    assert captured[-1] is not None
    assert set(captured[-1]) == {"HOME", "LANG", "LC_ALL", "PATH", "OPENAI_API_KEY"}
    assert captured[-1]["OPENAI_API_KEY"] == "trusted-generic-secret"
    assert "ATLASWEAVER_BACKEND_TOKEN" not in captured[-1]
    assert "trusted-generic-secret" not in result.stdout + result.stderr


class _RaisingSnapshot(AbstractContextManager[object]):
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def __enter__(self) -> object:
        raise self.error

    def __exit__(self, *args: object) -> None:
        return None


def test_query_error_message_is_sanitized_and_documented_code_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = configured_v2_repository(tmp_path)
    error = QueryError("node_not_found", f"private path {repo}")
    monkeypatch.setattr(
        "project_knowledge.cli.open_query_snapshot",
        lambda *args, **kwargs: _RaisingSnapshot(error),
        raising=False,
    )

    result = invoke_cli(repo, capsys, "query", "dispatch", "--json")

    assert payload(result)["error"] == {
        "code": "node_not_found",
        "message": "graph query failed",
    }
    assert str(repo) not in result.stdout + result.stderr


def test_coverage_approval_is_preview_only_without_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = configured_v2_repository(tmp_path)

    result = invoke_cli(
        repo,
        capsys,
        "coverage",
        "approve",
        "--file",
        "src/app.py",
        "--reason",
        "not_represented_by_graphify",
        "--rationale",
        "reviewed",
        "--json",
    )

    assert result.returncode == 0
    document = payload(result)
    assert document["status"] == "preview"
    assert document["file"] == "src/app.py"
    assert "schema_version: 1" in document["content"]
    assert not (repo / ".atlasweaver-coverage.yaml").exists()


def test_low_level_schema2_validate_requires_external_evidence_anchor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = configured_v2_repository(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "graph.json").write_text(
        json.dumps({"artifact_schema_version": 2}), encoding="utf-8"
    )

    result = invoke_cli(
        repo,
        capsys,
        "validate",
        "--candidate",
        str(candidate),
        "--json",
    )

    assert result.returncode == 1
    assert payload(result)["error"] == {
        "code": "evidence_anchor_required",
        "message": "schema-2 validation requires a trusted evidence anchor",
    }


def test_text_output_serializes_nested_query_data_as_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from project_knowledge.cli import CliResponse

    repo = configured_v2_repository(tmp_path)
    monkeypatch.setattr(
        "project_knowledge.cli._query",
        lambda *args, **kwargs: CliResponse(
            {
                    "schema_version": 1,
                    "command": "query",
                    "trust": "navigation",
                    "result": {"nodes": [{"id": "dispatch"}]},
                    "limitations": [],
            }
        ),
        raising=False,
    )

    result = invoke_cli(repo, capsys, "query", "dispatch")

    assert result.returncode == 0
    assert "result: {\"nodes\":[{\"id\":\"dispatch\"}]}" in result.stdout
    assert "{'nodes':" not in result.stdout
