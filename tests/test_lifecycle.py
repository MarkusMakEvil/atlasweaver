from __future__ import annotations

import os
import json
from dataclasses import replace
from pathlib import Path
from pathlib import PurePosixPath
from subprocess import CompletedProcess

import pytest

from project_knowledge.graphify import (
    GraphifyCommandError,
    GraphifyContractError,
    resolve_graphify_executable,
    run_graphify_operation,
)
from project_knowledge.lifecycle import (
    RefreshError,
    RefreshFileSystem,
    RefreshOptions,
    refresh_project,
)
from project_knowledge.artifacts import ArtifactValidationError, validate_owned_graph
from project_knowledge.locking import TransactionLockError, open_repository_access
from tests.support import manifest_v2, write_manifest_v2
from tests.test_graphify_adapter import ProbeRunner, _REQUIRED_HELP


class RecordingRunner:
    def __init__(self, *, returncode: int = 0, stdout: str = "{}\n", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[tuple[tuple[str, ...], dict[str, str], float]] = []

    def run(self, argv, *, text, capture_output, check, timeout, env):
        assert text and capture_output and not check
        self.calls.append((tuple(argv), dict(env), timeout))
        return CompletedProcess(tuple(argv), self.returncode, self.stdout, self.stderr)


def _resolved(tmp_path: Path):
    launcher = tmp_path / "graphify"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o700)
    return resolve_graphify_executable(test_override=launcher)


def test_operation_uses_exact_environment_timeout_and_binding_callback(tmp_path: Path) -> None:
    executable = _resolved(tmp_path)
    runner = RecordingRunner()
    checks: list[str] = []
    result = run_graphify_operation(
        runner,
        executable,
        (str(executable.path), "extract", "source"),
        env={"PATH": os.defpath, "TOKEN": "secret-value"},
        timeout=123.0,
        operation="extract",
        before_exec=lambda: checks.append("checked"),
    )
    assert result.stdout == "{}\n"
    assert checks == ["checked"]
    assert runner.calls == [
        ((str(executable.path), "extract", "source"), {"PATH": os.defpath, "TOKEN": "secret-value"}, 123.0)
    ]


@pytest.mark.parametrize("returncode", [0, 1])
def test_operation_redacts_short_and_long_values_on_both_streams(
    tmp_path: Path, returncode: int,
) -> None:
    runner = RecordingRunner(
        returncode=returncode,
        stdout="short=x long=opaque-credential-value\n",
        stderr="long=opaque-credential-value short=x\n",
    )
    executable = _resolved(tmp_path)
    try:
        result = run_graphify_operation(
            runner, executable, (str(executable.path), "--version"),
            env={"PATH": os.defpath, "SHORT_TOKEN": "x", "LONG_TOKEN": "opaque-credential-value"},
            timeout=1.0, operation="version",
        )
        rendered = result.stdout + result.stderr
    except GraphifyCommandError as error:
        rendered = repr(error) + str(error)
    assert "opaque-credential-value" not in rendered
    assert "short=x" not in rendered
    assert "[REDACTED]" in rendered


def test_operation_revalidates_launcher_after_callback_without_spawning(tmp_path: Path) -> None:
    executable = _resolved(tmp_path)
    runner = RecordingRunner()

    def mutate() -> None:
        executable.path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    with pytest.raises(GraphifyContractError, match="graphify_executable_changed"):
        run_graphify_operation(
            runner, executable, (str(executable.path), "--version"),
            env={"PATH": os.defpath}, timeout=1.0, operation="version",
            before_exec=mutate,
        )
    assert runner.calls == []


def test_operation_refuses_mismatched_argv_zero(tmp_path: Path) -> None:
    executable = _resolved(tmp_path)
    runner = RecordingRunner()
    with pytest.raises(GraphifyContractError, match="graphify_executable_changed"):
        run_graphify_operation(
            runner, executable, ("graphify", "--version"),
            env={"PATH": os.defpath}, timeout=1.0, operation="version",
        )
    assert runner.calls == []


class Official0948FixtureRunner(ProbeRunner):
    """Materialize the official fixture for probe and refresh commands."""

    def __init__(
        self,
        executable: Path,
        *,
        raw_diagnosis: bool = False,
        version: str = "0.9.48",
    ) -> None:
        super().__init__()
        self.raw_diagnosis = raw_diagnosis
        self.version = version
        self.answer((str(executable), "--version"), stdout=f"graphify {version}\n")
        self.answer((str(executable), "--help"), stdout=_REQUIRED_HELP)
        self.refresh_operations: list[str] = []
        self.refresh_environments: list[dict[str, str]] = []

    def run(self, argv, **options):
        call = tuple(argv)
        operation = call[1] if len(call) > 1 else ""
        refresh = "atlasweaver-graphify-probe-" not in " ".join(call)
        normalized_before_cluster = None
        if refresh and operation == "cluster-only":
            normalized_before_cluster = json.loads(
                Path(call[call.index("--graph") + 1]).read_text(encoding="utf-8")
            )
        result = super().run(argv, **options)
        if refresh and operation in {"extract", "diagnose", "cluster-only"}:
            self.refresh_operations.append(
                "cluster" if operation == "cluster-only" else operation
            )
            self.refresh_environments.append(dict(options["env"]))
            fixture = json.loads(
                Path(
                    "src/project_knowledge/compatibility_fixtures/"
                    f"graphify_{self.version.replace('.', '_')}.json"
                )
                .read_text(encoding="utf-8")
            )
            if operation == "extract":
                output = Path(call[call.index("--out") + 1]) / "graphify-out"
                output.mkdir(parents=True, exist_ok=True)
                (output / "graph.json").write_text(
                    json.dumps(fixture["native_graph"]), encoding="utf-8"
                )
            elif operation == "diagnose":
                diagnosis = fixture["diagnosis"]
                if self.raw_diagnosis:
                    diagnosis = {
                        **diagnosis,
                        "examples": [{"source": "private"}],
                        "producer_suppression": {"path": "/private/source.py"},
                        "notes": ["untrusted diagnostic prose"],
                    }
                    diagnosis["summary"] = {
                        **diagnosis["summary"],
                        "input_path": "/private/graph.json",
                        "post_build_error": "",
                    }
                return CompletedProcess(call, 0, json.dumps(diagnosis), "")
            else:
                graph_path = Path(call[call.index("--graph") + 1])
                assert normalized_before_cluster is not None
                normalized = normalized_before_cluster
                graph_path.write_text(
                    json.dumps(
                        {
                            "directed": False,
                            "multigraph": False,
                            "graph": {},
                            "nodes": normalized["nodes"],
                            "links": normalized["edges"],
                        }
                    ),
                    encoding="utf-8",
                )
                (graph_path.parent / "GRAPH_REPORT.md").write_text(
                    "# Graph report\n", encoding="utf-8"
                )
        return result


def _source_repository(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "fixture.py").write_text(
        "def fixture():\n    return 1\n", encoding="utf-8"
    )
    selected = manifest_v2(include_roots=(PurePosixPath("fixture.py"),))
    write_manifest_v2(repo, include_roots=selected.include_roots)
    return repo, selected


def _fixture_graphify(tmp_path: Path) -> Path:
    launcher = tmp_path / "fixture-graphify"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o700)
    return launcher


class RecordingRefreshFileSystem(RefreshFileSystem):
    def __init__(self) -> None:
        super().__init__()
        self.checkpoints: list[str] = []

    def checkpoint(self, operation: str) -> None:
        self.checkpoints.append(operation)


class FailingRefreshFileSystem(RecordingRefreshFileSystem):
    def __init__(self, boundary: str) -> None:
        super().__init__()
        self.boundary = boundary

    def checkpoint(self, operation: str) -> None:
        super().checkpoint(operation)
        if operation == self.boundary:
            raise RefreshError("injected_failure", "injected refresh failure")


class MutatingRefreshFileSystem(RecordingRefreshFileSystem):
    def __init__(self, boundary: str, mutation) -> None:
        super().__init__()
        self.boundary = boundary
        self.mutation = mutation

    def checkpoint(self, operation: str) -> None:
        super().checkpoint(operation)
        if operation == self.boundary:
            self.mutation()


class CleanupFailingRefreshFileSystem(RecordingRefreshFileSystem):
    def __init__(self, *, signal_boundary: str | None = None) -> None:
        super().__init__()
        self.signal_boundary = signal_boundary
        self.cleanup_attempted = False

    def checkpoint(self, operation: str) -> None:
        super().checkpoint(operation)
        if operation == self.signal_boundary:
            raise KeyboardInterrupt()

    def remove_run_root(self, run: Path) -> None:
        self.cleanup_attempted = True
        raise OSError("injected private cleanup failure")


class FailingBeforeCommitAndCleanup(CleanupFailingRefreshFileSystem):
    def checkpoint(self, operation: str) -> None:
        super().checkpoint(operation)
        if operation == "validate":
            raise RefreshError("injected_failure", "injected refresh failure")


class FailOnAmbientRead(dict[str, str]):
    def __iter__(self):
        raise AssertionError("ambient was read before admission")

    def keys(self):
        raise AssertionError("ambient was read before admission")


def _tree_snapshot(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def _replace_repository_root(repo: Path, original: Path) -> None:
    repo.rename(original)
    repo.mkdir()
    (repo / "fixture.py").write_text("REPLACEMENT = True\n", encoding="utf-8")
    write_manifest_v2(repo, include_roots=(PurePosixPath("fixture.py"),))


def test_refresh_runs_official_pipeline_and_promotes_owned_v2_graph(
    tmp_path: Path,
) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    runner = Official0948FixtureRunner(executable.resolve())
    fs = RefreshFileSystem()

    result = refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=runner,
        fs=fs,
        ambient={"CUSTOMER_SECRET": "never-forward"},
        graphify_binary=executable,
    )

    ownership = json.loads(
        (repo / "graphify-out/.project-knowledge-ownership.json").read_text()
    )
    assert result.status == "refreshed"
    assert result.trust == "navigation"
    assert result.build_epoch == 1
    assert result.generation_digest == ownership["generation_digest"]
    assert ownership["projection_digest"] == result.projection_digest
    assert "GRAPH_EVIDENCE.json" in ownership["artifacts"]
    assert runner.refresh_operations == ["extract", "diagnose", "cluster"]
    assert "CUSTOMER_SECRET" not in runner.refresh_environments[0]
    assert fs.live_run_roots == ()


def test_refresh_sanitizes_real_diagnostic_superset_before_evidence(
    tmp_path: Path,
) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)

    result = refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(
            executable.resolve(), raw_diagnosis=True
        ),
        ambient={},
        graphify_binary=executable,
    )

    assert result.status == "refreshed"
    assert "/private/" not in (
        repo / "graphify-out/GRAPH_EVIDENCE.json"
    ).read_text(encoding="utf-8")


def test_exact_generation_refresh_is_noop_and_reuses_installed_epoch(
    tmp_path: Path,
) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    first = refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        ambient={},
        graphify_binary=executable,
    )
    ownership = repo / "graphify-out/.project-knowledge-ownership.json"
    before = ownership.read_bytes()
    before_inode = ownership.stat().st_ino

    second = refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        ambient={},
        graphify_binary=executable,
    )

    assert second.status == "unchanged"
    assert second.generation_digest == first.generation_digest
    assert second.build_epoch == first.build_epoch == 1
    assert ownership.read_bytes() == before
    assert ownership.stat().st_ino == before_inode


def test_changed_projection_promotes_next_owned_epoch(tmp_path: Path) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    first = refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        ambient={},
        graphify_binary=executable,
    )
    (repo / "fixture.py").write_text(
        "def fixture():\n    return 2\n", encoding="utf-8"
    )

    second = refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        ambient={},
        graphify_binary=executable,
    )

    owned = validate_owned_graph(repo / "graphify-out", selected)
    assert first.build_epoch == 1
    assert second.status == "refreshed"
    assert second.build_epoch == owned.build_epoch == 2
    assert second.generation_digest == owned.generation_digest
    assert second.generation_digest != first.generation_digest


def test_refresh_migrates_an_owned_graph_to_a_new_supported_contract(
    tmp_path: Path,
) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    first = refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        ambient={},
        graphify_binary=executable,
    )
    upgraded = replace(selected, graphify_version="0.9.51")
    with pytest.raises(ArtifactValidationError, match="ownership identity mismatch"):
        validate_owned_graph(repo / "graphify-out", upgraded)
    write_manifest_v2(
        repo,
        include_roots=upgraded.include_roots,
        graphify_version=upgraded.graphify_version,
    )

    second = refresh_project(
        repo,
        upgraded,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(
            executable.resolve(), version=upgraded.graphify_version
        ),
        ambient={},
        graphify_binary=executable,
    )

    ownership = json.loads(
        (repo / "graphify-out/.project-knowledge-ownership.json").read_text()
    )
    assert first.build_epoch == 1
    assert second.status == "refreshed"
    assert second.build_epoch == 2
    assert ownership["graphify_version"] == "0.9.51"
    assert ownership["adapter_id"] == "graphify-0.9.51"


def test_refresh_records_every_state_machine_boundary(tmp_path: Path) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    fs = RecordingRefreshFileSystem()

    refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        fs=fs,
        ambient={},
        graphify_binary=executable,
    )

    assert fs.checkpoints == [
        "preflight",
        "projection",
        "extract",
        "capture-native",
        "diagnose",
        "normalize",
        "cluster",
        "capture-final",
        "evidence",
        "adapt",
        "validate",
        "pre-promote-projection",
        "promote",
        "post-promote-projection",
        "health",
        "cleanup",
    ]


@pytest.mark.parametrize(
    "boundary",
    [
        "preflight",
        "projection",
        "extract",
        "capture-native",
        "diagnose",
        "normalize",
        "cluster",
        "capture-final",
        "evidence",
        "adapt",
        "validate",
        "pre-promote-projection",
        "promote",
    ],
)
def test_precommit_failure_preserves_previous_graph_and_cleans_run(
    tmp_path: Path, boundary: str
) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        ambient={},
        graphify_binary=executable,
    )
    previous = _tree_snapshot(repo / "graphify-out")
    (repo / "fixture.py").write_text("def fixture():\n    return 3\n")
    fs = FailingRefreshFileSystem(boundary)

    with pytest.raises(RefreshError, match="injected refresh failure"):
        refresh_project(
            repo,
            selected,
            RefreshOptions(None, None, False, True),
            runner=Official0948FixtureRunner(executable.resolve()),
            fs=fs,
            ambient={},
            graphify_binary=executable,
        )

    assert _tree_snapshot(repo / "graphify-out") == previous
    assert fs.live_run_roots == ()


def test_semantic_options_fail_before_ambient_or_state_creation(tmp_path: Path) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)

    with pytest.raises(RefreshError) as raised:
        refresh_project(
            repo,
            selected,
            RefreshOptions("openai", "--private", False, False),
            runner=Official0948FixtureRunner(executable.resolve()),
            ambient=FailOnAmbientRead(),
            graphify_binary=executable,
        )

    assert raised.value.code == "semantic_model_required"
    assert not (repo / ".project-knowledge").exists()


def test_missing_semantic_credential_fails_before_state_creation(tmp_path: Path) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)

    with pytest.raises(RefreshError) as raised:
        refresh_project(
            repo,
            selected,
            RefreshOptions("openai", "gpt-5", False, False),
            runner=Official0948FixtureRunner(executable.resolve()),
            ambient={},
            graphify_binary=executable,
        )

    assert raised.value.code == "semantic_backend_required"
    assert not (repo / ".project-knowledge").exists()


def test_selected_credential_reaches_extract_only_and_only_name_is_evidence(
    tmp_path: Path,
) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    runner = Official0948FixtureRunner(executable.resolve())
    secret = "opaque-semantic-secret"

    refresh_project(
        repo,
        selected,
        RefreshOptions("openai", "gpt-5", False, False),
        runner=runner,
        ambient={"OPENAI_API_KEY": secret, "CUSTOMER_SECRET": "excluded"},
        graphify_binary=executable,
    )

    assert runner.refresh_environments[0]["OPENAI_API_KEY"] == secret
    assert all(
        "OPENAI_API_KEY" not in value for value in runner.refresh_environments[1:]
    )
    assert [set(value) for value in runner.refresh_environments] == [
        {"HOME", "LANG", "LC_ALL", "PATH", "OPENAI_API_KEY"},
        {"HOME", "LANG", "LC_ALL", "PATH"},
        {"HOME", "LANG", "LC_ALL", "PATH"},
    ]
    evidence = (repo / "graphify-out/GRAPH_EVIDENCE.json").read_text()
    assert secret not in evidence
    assert "OPENAI_API_KEY" in evidence


def test_expected_repository_identity_mismatch_precedes_ambient_and_state(
    tmp_path: Path,
) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    with open_repository_access(repo) as repository:
        expected = repository.identity
    repo.rename(tmp_path / "original")
    replacement_parent = tmp_path / "replacement-parent"
    replacement_parent.mkdir()
    replacement, _ = _source_repository(replacement_parent)
    replacement.rename(repo)

    with pytest.raises(TransactionLockError) as raised:
        refresh_project(
            repo,
            selected,
            RefreshOptions("openai", "gpt-5", False, False),
            runner=Official0948FixtureRunner(executable.resolve()),
            ambient=FailOnAmbientRead(),
            graphify_binary=executable,
            expected_repository_identity=expected,
        )

    assert raised.value.kind == "authority"
    assert not (repo / ".project-knowledge").exists()


def test_post_commit_projection_drift_reports_promoted_but_stale(
    tmp_path: Path,
) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    fs = MutatingRefreshFileSystem(
        "post-promote-projection",
        lambda: (repo / "fixture.py").write_text(
            "def fixture():\n    return 9\n", encoding="utf-8"
        ),
    )

    result = refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        fs=fs,
        ambient={},
        graphify_binary=executable,
    )

    assert result.status == "promoted_but_stale"
    assert result.core_status == "stale"
    assert "source_changed_after_promotion" in result.limitations
    assert result.graph_digest is not None
    assert (repo / "graphify-out/graph.json").is_file()
    assert "cleanup" in fs.checkpoints


def test_root_swap_after_lock_promotes_only_through_original_descriptor(
    tmp_path: Path,
) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    original = tmp_path / "original"
    fs = MutatingRefreshFileSystem(
        "preflight", lambda: _replace_repository_root(repo, original)
    )

    result = refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        fs=fs,
        ambient={},
        graphify_binary=executable,
    )

    assert result.status in {"refreshed", "unchanged"}
    assert (original / "graphify-out/graph.json").is_file()
    assert not (repo / "graphify-out").exists()
    assert not (repo / ".project-knowledge").exists()


def test_post_commit_cleanup_failure_reports_installed_identity_and_recovery(
    tmp_path: Path,
) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    fs = CleanupFailingRefreshFileSystem()

    result = refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        fs=fs,
        ambient={},
        graphify_binary=executable,
    )

    owned = validate_owned_graph(repo / "graphify-out", selected)
    assert result.status == "promoted_but_stale"
    assert result.recovery_id is not None
    assert "cleanup_failed" in result.limitations
    assert result.generation_digest == owned.generation_digest
    assert result.build_epoch == owned.build_epoch
    assert fs.live_run_roots


def test_exact_noop_cleanup_failure_is_closed_noncommit_error(tmp_path: Path) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    first = refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        ambient={},
        graphify_binary=executable,
    )

    with pytest.raises(RefreshError) as raised:
        refresh_project(
            repo,
            selected,
            RefreshOptions(None, None, False, True),
            runner=Official0948FixtureRunner(executable.resolve()),
            fs=CleanupFailingRefreshFileSystem(),
            ambient={},
            graphify_binary=executable,
        )

    assert raised.value.code == "cleanup_failed"
    assert raised.value.recovery_id is not None
    assert validate_owned_graph(
        repo / "graphify-out", selected
    ).generation_digest == first.generation_digest


def test_precommit_cleanup_failure_replaces_internal_error_with_closed_recovery(
    tmp_path: Path,
) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)

    with pytest.raises(RefreshError) as raised:
        refresh_project(
            repo,
            selected,
            RefreshOptions(None, None, False, True),
            runner=Official0948FixtureRunner(executable.resolve()),
            fs=FailingBeforeCommitAndCleanup(),
            ambient={},
            graphify_binary=executable,
        )

    assert raised.value.code == "cleanup_failed"
    assert raised.value.recovery_id is not None
    assert not (repo / "graphify-out").exists()


def test_cleanup_failure_never_suppresses_keyboard_interrupt(tmp_path: Path) -> None:
    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    fs = CleanupFailingRefreshFileSystem(signal_boundary="validate")

    with pytest.raises(KeyboardInterrupt):
        refresh_project(
            repo,
            selected,
            RefreshOptions(None, None, False, True),
            runner=Official0948FixtureRunner(executable.resolve()),
            fs=fs,
            ambient={},
            graphify_binary=executable,
        )

    assert fs.cleanup_attempted


@pytest.mark.parametrize("field", ["digest", "generation_digest", "build_epoch"])
def test_malformed_promotion_summary_reports_revalidated_live_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    import project_knowledge.lifecycle as lifecycle_module

    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    real_promote = lifecycle_module.promote_graph

    def malformed(*args, **kwargs):
        result = real_promote(*args, **kwargs)
        return replace(
            result,
            **{field: None if field == "build_epoch" else "f" * 64},
        )

    monkeypatch.setattr(lifecycle_module, "promote_graph", malformed)
    result = refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        ambient={},
        graphify_binary=executable,
    )

    owned = validate_owned_graph(repo / "graphify-out", selected)
    assert result.status == "promoted_but_stale"
    assert result.graph_digest == owned.graph_digest
    assert result.generation_digest == owned.generation_digest
    assert result.build_epoch == owned.build_epoch
    assert "f" * 64 not in {result.graph_digest, result.generation_digest}


def test_unverifiable_committed_generation_reports_null_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import project_knowledge.lifecycle as lifecycle_module

    repo, selected = _source_repository(tmp_path)
    executable = _fixture_graphify(tmp_path)
    real_promote = lifecycle_module.promote_graph

    def corrupt_after_commit(*args, **kwargs):
        result = real_promote(*args, **kwargs)
        (repo / "graphify-out/GRAPH_EVIDENCE.json").write_text("{}\n")
        return result

    monkeypatch.setattr(lifecycle_module, "promote_graph", corrupt_after_commit)
    result = refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        ambient={},
        graphify_binary=executable,
    )

    assert result.status == "promoted_but_stale"
    assert (result.graph_digest, result.generation_digest, result.build_epoch) == (
        None,
        None,
        None,
    )
