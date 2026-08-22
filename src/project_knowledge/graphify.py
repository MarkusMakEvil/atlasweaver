"""Safe, deterministic process boundary for the official Graphify CLI.

The adapter deliberately exposes only the read-only query and the explicitly
requested local global-registry operation.  Full Graphify pipelines belong to
the official assistant skill, whose capabilities are probed from its installed
skill text rather than guessed from CLI syntax.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import subprocess
from subprocess import CompletedProcess
from threading import Thread
from typing import Literal, Protocol, TextIO


COMMAND_TIMEOUT = 30.0
MAX_CAPTURED_OUTPUT_CHARS = 16_384
OUTPUT_TRUNCATION_MARKER = "\n[output truncated]"
PROCESS_CLEANUP_TIMEOUT = 0.25
REQUIRED_CLI_COMMANDS = frozenset({"query", "explain", "path", "global", "export"})


class CommandRunner(Protocol):
    """The injectable, argument-vector-only subprocess boundary."""

    def run(
        self,
        argv: Sequence[str],
        *,
        text: bool,
        capture_output: bool,
        check: bool,
        timeout: float,
        env: Mapping[str, str],
    ) -> CompletedProcess[str]: ...


class GraphifyError(RuntimeError):
    """Base class for safe Graphify diagnostics."""


class GraphifyContractError(GraphifyError):
    """Raised when the installed Graphify capability contract is not met."""


class GraphifyCommandError(GraphifyError):
    """Raised for a failed Graphify subprocess without leaking raw diagnostics."""

    def __init__(self, operation: str, detail: str) -> None:
        self.operation = operation
        self.detail = detail
        super().__init__(f"graphify {operation} failed: {detail}")


@dataclass(frozen=True)
class GraphifyCapabilities:
    """Shell-CLI capabilities plus the separately verified skill surface."""

    version: str
    commands: frozenset[str]
    supports_obsidian_export: bool
    supports_global_registry: bool


@dataclass(frozen=True)
class AssistantSkillCapabilities:
    """Capabilities that are deliberately not inferred from CLI help output."""

    supports_obsidian_export: bool


@dataclass(frozen=True)
class RegistryResult:
    """The registry is supplementary; its failure leaves the project graph valid."""

    status: Literal["registered", "partial"]
    project_id: str
    detail: str | None = None


class _CappedText:
    """Collect process output without retaining more than the configured cap."""

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._size = 0
        self._truncated = False

    def append(self, value: str) -> None:
        remaining = MAX_CAPTURED_OUTPUT_CHARS - self._size
        if remaining <= 0:
            self._truncated = True
            return
        self._chunks.append(value[:remaining])
        self._size += min(len(value), remaining)
        if len(value) > remaining:
            self._truncated = True

    def value(self) -> str:
        output = "".join(self._chunks)
        return _bounded_output(output, truncated=self._truncated)


class SubprocessCommandRunner:
    """Production runner that drains both pipes while retaining capped output."""

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
        if not text or not capture_output or check:
            raise ValueError("Graphify commands require text capture with check disabled")
        popen_options: dict[str, object] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "env": dict(env),
            "shell": False,
        }
        if os.name == "posix":
            popen_options["start_new_session"] = True
        process = subprocess.Popen(tuple(argv), **popen_options)
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = _CappedText()
        stderr = _CappedText()
        stdout_thread = Thread(target=_drain, args=(process.stdout, stdout), daemon=True)
        stderr_thread = Thread(target=_drain, args=(process.stderr, stderr), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        timeout_error: subprocess.TimeoutExpired | None = None
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            timeout_error = error
            _terminate_process_tree(process)
            _wait_after_termination(process)
            returncode = process.returncode
        _join_drainers(stdout_thread, stderr_thread)
        if timeout_error is not None:
            raise timeout_error
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            _terminate_process_tree(process)
            _wait_after_termination(process)
            _join_drainers(stdout_thread, stderr_thread)
            raise subprocess.TimeoutExpired(tuple(argv), timeout)
        return CompletedProcess(tuple(argv), returncode, stdout.value(), stderr.value())


def minimal_environment() -> dict[str, str]:
    """Return only variables Graphify needs to find itself and its local registry."""
    return {
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", os.defpath),
    }


def run_checked(runner: CommandRunner, argv: Sequence[str]) -> CompletedProcess[str]:
    """Run one Graphify command with an argv boundary and safe diagnostics."""
    command = tuple(str(part) for part in argv)
    operation = _operation_name(command)
    try:
        result = runner.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=COMMAND_TIMEOUT,
            env=minimal_environment(),
        )
    except subprocess.TimeoutExpired as error:
        raise GraphifyCommandError(operation, "command timed out") from error
    except OSError as error:
        raise GraphifyCommandError(operation, "command could not be executed") from error
    result = _capped_result(result)
    if result.returncode:
        raise GraphifyCommandError(operation, sanitize_stderr(result.stderr))
    return result


def probe_graphify(
    binary: Path,
    expected_version: str,
    runner: CommandRunner,
    *,
    assistant_skill: Path | None = None,
) -> GraphifyCapabilities:
    """Verify the exact CLI release and parse its shell command surface.

    ``assistant_skill`` is optional because a machine may have the pinned CLI
    before the official skill is installed.  Its separate probe prevents
    treating slash-style full-pipeline options as shell commands.
    """
    executable = str(binary)
    version_result = run_checked(runner, (executable, "--version"))
    installed_version = _parse_version(version_result.stdout)
    if installed_version != expected_version:
        raise GraphifyContractError(
            f"expected {expected_version}, got {installed_version}"
        )

    help_result = run_checked(runner, (executable, "--help"))
    commands = _parse_cli_commands(help_result.stdout)
    missing = REQUIRED_CLI_COMMANDS - commands
    if missing:
        raise GraphifyContractError(
            "missing required CLI commands: " + ", ".join(sorted(missing))
        )

    skill_capabilities = (
        probe_assistant_skill(assistant_skill)
        if assistant_skill is not None
        else AssistantSkillCapabilities(supports_obsidian_export=False)
    )
    return GraphifyCapabilities(
        version=installed_version,
        commands=frozenset(commands),
        supports_obsidian_export=skill_capabilities.supports_obsidian_export,
        supports_global_registry="global" in commands,
    )


def probe_assistant_skill(skill_path: Path) -> AssistantSkillCapabilities:
    """Probe an explicit official skill file, independently from CLI help."""
    try:
        skill_text = skill_path.read_text(encoding="utf-8")
    except OSError as error:
        raise GraphifyContractError("official Graphify assistant skill is unavailable") from error
    skill_text = _cap_output(skill_text)
    return AssistantSkillCapabilities(
        supports_obsidian_export="--obsidian" in skill_text
        and "# /graphify" in skill_text,
    )


def query_graph(
    binary: Path, graph: Path, question: str, runner: CommandRunner
) -> CompletedProcess[str]:
    """Query a validated graph, keeping the complete question as one argv item."""
    return run_checked(
        runner,
        (str(binary), "query", question, "--graph", str(graph)),
    )


def sync_global_registry(
    binary: Path, graph: Path, project_id: str, runner: CommandRunner
) -> RegistryResult:
    """Register a validated graph, degrading only registry health on failure."""
    if not graph.is_file():
        return RegistryResult(
            status="partial",
            project_id=project_id,
            detail="project graph is unavailable for registration",
        )
    try:
        run_checked(
            runner,
            (str(binary), "global", "add", str(graph), "--as", project_id),
        )
    except GraphifyCommandError as error:
        return RegistryResult(status="partial", project_id=project_id, detail=error.detail)
    return RegistryResult(status="registered", project_id=project_id)


def sanitize_stderr(stderr: str | None) -> str:
    """Return a capped diagnostic without credentials, atlas paths, or env values."""
    value = _cap_output(stderr or "").strip()
    value = re.sub(
        r"(?i)\b(api[_-]?key|token|password|secret|authorization)\s*[=:]\s*"
        r"(?:bearer\s+)?[^\s,;]+",
        r"\1=[REDACTED]",
        value,
    )
    value = re.sub(r"\b(?:ghp|github_pat|sk|xox[baprs])[-_A-Za-z0-9]+\b", "[REDACTED]", value)
    value = re.sub(
        r"(?:/Users/[^/\s]+|/home/[^/\s]+)[^\n]*?Obsidian(?:[ /][^\n]*)?",
        "<atlas-path>",
        value,
        flags=re.IGNORECASE,
    )
    for environment_value in _sensitive_environment_values():
        value = value.replace(environment_value, "[REDACTED]")
    return _cap_output(value) or "Graphify returned no diagnostic"


def _drain(stream: TextIO, output: _CappedText) -> None:
    while chunk := stream.read(4_096):
        output.append(chunk)


def _capped_result(result: CompletedProcess[str]) -> CompletedProcess[str]:
    return CompletedProcess(
        result.args,
        result.returncode,
        _cap_output(result.stdout or ""),
        _cap_output(result.stderr or ""),
    )


def _cap_output(value: str) -> str:
    return _bounded_output(value, truncated=len(value) > MAX_CAPTURED_OUTPUT_CHARS)


def _bounded_output(value: str, *, truncated: bool) -> str:
    if not truncated:
        return value
    payload_limit = MAX_CAPTURED_OUTPUT_CHARS - len(OUTPUT_TRUNCATION_MARKER)
    return value[:payload_limit] + OUTPUT_TRUNCATION_MARKER


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate the command's owned process group where the platform supports it."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


def _wait_after_termination(process: subprocess.Popen[str]) -> None:
    try:
        process.wait(timeout=PROCESS_CLEANUP_TIMEOUT)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)


def _join_drainers(*threads: Thread) -> None:
    for thread in threads:
        thread.join(PROCESS_CLEANUP_TIMEOUT)


def _operation_name(argv: Sequence[str]) -> str:
    if len(argv) < 2:
        return "command"
    return argv[1].lstrip("-") or "command"


def _parse_version(stdout: str) -> str:
    match = re.fullmatch(r"graphify\s+(\S+)\s*", stdout)
    if match is None:
        return "unrecognized version output"
    return match.group(1)


def _parse_cli_commands(help_text: str) -> frozenset[str]:
    commands: set[str] = set()
    in_commands = False
    for line in help_text.splitlines():
        if line.strip() == "Commands:":
            in_commands = True
            continue
        if not in_commands:
            continue
        match = re.match(r"^  ([a-z][a-z-]*)\b", line)
        if match is not None:
            commands.add(match.group(1))
    return frozenset(commands)


def _sensitive_environment_values() -> tuple[str, ...]:
    values: list[str] = []
    for name, value in os.environ.items():
        if name in {"HOME", "LANG", "LC_ALL", "PATH", "PWD", "SHLVL", "_"}:
            continue
        if len(value) >= 5:
            values.append(value)
    return tuple(values)
