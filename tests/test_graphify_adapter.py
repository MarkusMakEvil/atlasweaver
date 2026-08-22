"""Boundary tests for the official Graphify command adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import subprocess
from subprocess import CompletedProcess
import sys
import time

import pytest

from project_knowledge.graphify import (
    COMMAND_TIMEOUT,
    GraphifyCommandError,
    GraphifyContractError,
    MAX_CAPTURED_OUTPUT_CHARS,
    SubprocessCommandRunner,
    probe_assistant_skill,
    probe_graphify,
    query_graph,
    sanitize_stderr,
    sync_global_registry,
)


class FakeRunner:
    """Specific subprocess boundary double; production behavior remains real."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.options: list[dict[str, object]] = []
        self._answers: dict[tuple[str, ...], CompletedProcess[str]] = {}
        self._prefix_answers: list[tuple[tuple[str, ...], CompletedProcess[str]]] = []

    def answer(
        self,
        argv: tuple[str, ...],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self._answers[argv] = CompletedProcess(argv, returncode, stdout, stderr)

    def answer_prefix(
        self,
        prefix: tuple[str, ...],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self._prefix_answers.append(
            (prefix, CompletedProcess(prefix, returncode, stdout, stderr))
        )

    def run(
        self,
        argv: Sequence[str],
        *,
        text: bool,
        capture_output: bool,
        check: bool,
        timeout: float,
        env: Mapping[str, str],
    ) -> CompletedProcess[str]:
        call = tuple(argv)
        self.calls.append(call)
        self.options.append(
            {
                "text": text,
                "capture_output": capture_output,
                "check": check,
                "timeout": timeout,
                "env": dict(env),
            }
        )
        if call in self._answers:
            return self._answers[call]
        for prefix, answer in self._prefix_answers:
            if call[: len(prefix)] == prefix:
                return CompletedProcess(call, answer.returncode, answer.stdout, answer.stderr)
        return CompletedProcess(call, 0, "", "")


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def graph(tmp_path: Path) -> Path:
    path = tmp_path / "graph.json"
    path.write_text("{}", encoding="utf-8")
    return path


def test_probe_rejects_wrong_version(fake_runner: FakeRunner) -> None:
    """Accepting a different CLI release could select an incompatible contract."""
    fake_runner.answer(("graphify", "--version"), stdout="graphify 9.9.9\n")

    with pytest.raises(GraphifyContractError, match="expected 0.9.48, got 9.9.9"):
        probe_graphify(Path("graphify"), "0.9.48", fake_runner)


def test_probe_reports_cli_commands_but_not_skill_only_export(
    fake_runner: FakeRunner,
) -> None:
    """Treating assistant-skill syntax as a CLI command would break bootstrap."""
    fake_runner.answer(("graphify", "--version"), stdout="graphify 0.9.48\n")
    fake_runner.answer(
        ("graphify", "--help"),
        stdout="""Usage: graphify <command>\n\nCommands:\n  query \"<question>\"\n  explain \"node\"\n  path \"A\" \"B\"\n  global add <graph.json>\n  export callflow-html\n""",
    )

    capabilities = probe_graphify(Path("graphify"), "0.9.48", fake_runner)

    assert capabilities.commands == frozenset(
        {"query", "explain", "path", "global", "export"}
    )
    assert capabilities.supports_global_registry is True
    assert capabilities.supports_obsidian_export is False


def test_probe_keeps_late_required_commands_inside_captured_help(
    fake_runner: FakeRunner,
) -> None:
    """Truncating real help before `global` would reject the pinned CLI contract."""
    fake_runner.answer(("graphify", "--version"), stdout="graphify 0.9.48\n")
    fake_runner.answer(
        ("graphify", "--help"),
        stdout=(
            "Usage: graphify <command>\n\nCommands:\n"
            + "x" * 9_000
            + "\n  query \"<question>\"\n  explain \"node\"\n  path \"A\" \"B\"\n"
            "  global add <graph.json>\n  export callflow-html\n"
        ),
    )

    capabilities = probe_graphify(Path("graphify"), "0.9.48", fake_runner)

    assert {"query", "explain", "path", "global", "export"} <= capabilities.commands


def test_probe_does_not_treat_wrapped_help_descriptions_as_commands(
    fake_runner: FakeRunner,
) -> None:
    """A permissive indentation match would report help prose as CLI operations."""
    fake_runner.answer(("graphify", "--version"), stdout="graphify 0.9.48\n")
    fake_runner.answer(
        ("graphify", "--help"),
        stdout="""Usage: graphify <command>\n\nCommands:\n  query \"<question>\"\n                            and filters context\n  explain \"node\"\n  path \"A\" \"B\"\n  global add <graph.json>\n  export callflow-html\n""",
    )

    capabilities = probe_graphify(Path("graphify"), "0.9.48", fake_runner)

    assert "and" not in capabilities.commands


def test_assistant_skill_probe_detects_obsidian_pipeline_from_skill_text(
    tmp_path: Path,
) -> None:
    """Looking only at CLI help would hide the skill's full-pipeline export."""
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "# /graphify\n/graphify <path> --obsidian --obsidian-dir ~/vault\n",
        encoding="utf-8",
    )

    capabilities = probe_assistant_skill(skill)

    assert capabilities.supports_obsidian_export is True


def test_query_passes_untrusted_text_as_one_argument(
    fake_runner: FakeRunner, graph: Path
) -> None:
    """Splitting a question into shell syntax could execute untrusted input."""
    query_graph(Path("graphify"), graph, "auth; touch /tmp/pwned", fake_runner)

    assert fake_runner.calls[-1] == (
        "graphify",
        "query",
        "auth; touch /tmp/pwned",
        "--graph",
        str(graph),
    )
    assert fake_runner.options[-1] == {
        "text": True,
        "capture_output": True,
        "check": False,
        "timeout": COMMAND_TIMEOUT,
        "env": fake_runner.options[-1]["env"],
    }
    assert set(fake_runner.options[-1]["env"]) <= {"HOME", "LANG", "LC_ALL", "PATH"}


def test_command_failures_are_capped_and_sanitized(fake_runner: FakeRunner, graph: Path) -> None:
    """Raw Graphify diagnostics could disclose atlas paths or credentials."""
    fake_runner.answer(
        ("graphify", "query", "where", "--graph", str(graph)),
        returncode=1,
        stderr=(
            "token=top-secret /Users/example/Documents/Obsidian Vault/Private "
            + "x" * 20_000
        ),
    )

    with pytest.raises(GraphifyCommandError) as raised:
        query_graph(Path("graphify"), graph, "where", fake_runner)

    message = str(raised.value)
    assert "top-secret" not in message
    assert "Obsidian Vault" not in message
    assert len(message) < 10_000


def test_subprocess_runner_keeps_a_truncated_stream_within_its_output_limit() -> None:
    """Appending a marker after the payload limit would exceed the advertised cap."""
    result = SubprocessCommandRunner().run(
        (
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write('x' * {MAX_CAPTURED_OUTPUT_CHARS + 1})",
        ),
        text=True,
        capture_output=True,
        check=False,
        timeout=COMMAND_TIMEOUT,
        env=dict(os.environ),
    )

    assert len(result.stdout) == MAX_CAPTURED_OUTPUT_CHARS
    assert result.stdout.endswith("[output truncated]")


def test_sanitized_diagnostic_stays_within_output_limit() -> None:
    """Error redaction must preserve the same strict output boundary as capture."""
    diagnostic = sanitize_stderr("x" * (MAX_CAPTURED_OUTPUT_CHARS + 1))

    assert len(diagnostic) == MAX_CAPTURED_OUTPUT_CHARS
    assert diagnostic.endswith("[output truncated]")


def test_redaction_expansion_stays_within_output_limit_without_leaking_token() -> None:
    """A final cap is required because token redaction can expand an exact-limit input."""
    diagnostic = sanitize_stderr(
        "x" * (MAX_CAPTURED_OUTPUT_CHARS - len(" token=x")) + " token=x"
    )

    assert len(diagnostic) == MAX_CAPTURED_OUTPUT_CHARS
    assert diagnostic.endswith("[output truncated]")
    assert "token=x" not in diagnostic


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
def test_subprocess_timeout_kills_pipe_holding_child_without_blocking(tmp_path: Path) -> None:
    """Killing only a parent leaves its child holding pipes and blocks cleanup."""
    child_pid_file = tmp_path / "child.pid"
    child_code = "import time; time.sleep(1.5)"
    parent_code = (
        "from pathlib import Path; import subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
        "time.sleep(1.5)"
    )
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        SubprocessCommandRunner().run(
            (sys.executable, "-c", parent_code),
            text=True,
            capture_output=True,
            check=False,
            timeout=0.15,
            env=dict(os.environ),
        )

    assert time.monotonic() - started < 0.9
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("timeout left an owned child process alive")


def test_registry_failure_is_reported_without_invalidating_project_graph(
    fake_runner: FakeRunner, graph: Path
) -> None:
    """A local-registry outage must not make an already validated graph unusable."""
    fake_runner.answer_prefix(
        ("graphify", "global", "add"), returncode=1, stderr="registry unavailable"
    )

    result = sync_global_registry(Path("graphify"), graph, "demo", fake_runner)

    assert result.status == "partial"
    assert graph.exists()
