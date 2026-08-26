from __future__ import annotations

import os
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from project_knowledge.graphify import (
    GraphifyCommandError,
    GraphifyContractError,
    resolve_graphify_executable,
    run_graphify_operation,
)


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
