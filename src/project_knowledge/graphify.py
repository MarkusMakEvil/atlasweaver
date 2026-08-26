"""Safe, identity-revalidating process boundary for the official Graphify CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
from importlib import resources
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
from subprocess import CompletedProcess
import tempfile
from threading import Thread
from typing import Literal, Protocol, TextIO

from .compatibility import (
    CapabilityProbe,
    CompatibilityError,
    GraphifyCompatibility,
    _DIAGNOSTIC_SUMMARY_FIELDS,
    _validate_clustered_document,
    _validate_diagnostic_document,
    _validate_native_document,
    admitted_graphify_environment,
    assert_capability_surface,
    render_graphify_agent_install,
    render_graphify_argv,
    render_graphify_global_add,
)


COMMAND_TIMEOUT = 30.0
MAX_CAPTURED_OUTPUT_CHARS = 16_384
OUTPUT_TRUNCATION_MARKER = "\n[output truncated]"
PROCESS_CLEANUP_TIMEOUT = 0.25
MAX_EXECUTABLE_BYTES = 16 * 1024 * 1024
MAX_PROBE_ARTIFACT_BYTES = 16 * 1024 * 1024


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
    executable: ResolvedGraphifyExecutable
    capability_probe: CapabilityProbe


@dataclass(frozen=True)
class ResolvedGraphifyExecutable:
    """Canonical launcher identity revalidated immediately before every spawn."""

    path: Path
    st_dev: int
    st_ino: int
    launcher_sha256: str


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


def resolve_graphify_executable(
    test_override: Path | None = None,
) -> ResolvedGraphifyExecutable:
    """Resolve the literal Graphify command or a named test/maintainer seam."""
    candidate: Path
    if test_override is None:
        located = shutil.which("graphify")
        if located is None:
            raise GraphifyContractError("graphify executable is unavailable")
        candidate = Path(located)
    elif isinstance(test_override, Path):
        candidate = test_override
    else:
        raise GraphifyContractError("graphify executable is unavailable")
    try:
        canonical = candidate.resolve(strict=True)
        metadata, digest = _read_executable(canonical)
    except (OSError, ValueError, GraphifyContractError) as error:
        raise GraphifyContractError("graphify executable is unavailable") from error
    return ResolvedGraphifyExecutable(
        canonical, metadata.st_dev, metadata.st_ino, digest
    )


def revalidate_graphify_executable(executable: ResolvedGraphifyExecutable) -> None:
    """Fail closed if a resolved launcher changed in any observable way."""
    try:
        metadata, digest = _read_executable(executable.path)
        if (
            metadata.st_dev != executable.st_dev
            or metadata.st_ino != executable.st_ino
            or digest != executable.launcher_sha256
        ):
            raise OSError("identity mismatch")
    except (OSError, ValueError, GraphifyContractError) as error:
        raise GraphifyContractError("graphify_executable_changed") from error


def _read_executable(path: Path) -> tuple[os.stat_result, str]:
    path_metadata = path.lstat()
    if not stat.S_ISREG(path_metadata.st_mode):
        raise GraphifyContractError("launcher is not regular")
    if path_metadata.st_mode & 0o111 == 0:
        raise GraphifyContractError("launcher is not executable")
    if path_metadata.st_size <= 0 or path_metadata.st_size > MAX_EXECUTABLE_BYTES:
        raise GraphifyContractError("launcher size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != path_metadata.st_dev
            or opened.st_ino != path_metadata.st_ino
            or opened.st_mode & 0o111 == 0
            or opened.st_size <= 0
            or opened.st_size > MAX_EXECUTABLE_BYTES
        ):
            raise GraphifyContractError("launcher identity is unstable")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, min(65_536, MAX_EXECUTABLE_BYTES + 1 - total)):
            total += len(chunk)
            if total > MAX_EXECUTABLE_BYTES:
                raise GraphifyContractError("launcher is oversized")
            digest.update(chunk)
        final = os.fstat(descriptor)
        if (
            final.st_dev != opened.st_dev
            or final.st_ino != opened.st_ino
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or total != opened.st_size
        ):
            raise GraphifyContractError("launcher identity is unstable")
        return final, digest.hexdigest()
    finally:
        os.close(descriptor)


def run_checked(
    runner: CommandRunner,
    executable: ResolvedGraphifyExecutable,
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = COMMAND_TIMEOUT,
) -> CompletedProcess[str]:
    """Revalidate and run one immutable Graphify command with safe diagnostics."""
    command = tuple(str(part) for part in argv)
    if not command or command[0] != str(executable.path):
        raise GraphifyContractError("graphify_executable_changed")
    operation = _operation_name(command)
    revalidate_graphify_executable(executable)
    try:
        result = runner.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=dict(minimal_environment() if env is None else env),
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
    executable: ResolvedGraphifyExecutable,
    contract: GraphifyCompatibility,
    runner: CommandRunner,
) -> GraphifyCapabilities:
    """Operationally verify the complete registry-declared CLI surface."""
    binary = executable.path
    version_result = run_checked(runner, executable, (str(binary), "--version"))
    installed_version = _parse_version(version_result.stdout)
    if installed_version != contract.version:
        raise GraphifyContractError(
            f"expected {contract.version}, got {installed_version}"
        )

    help_result = run_checked(runner, executable, (str(binary), "--help"))
    commands = _parse_cli_commands(help_result.stdout)
    missing = contract.required_cli_commands - commands
    if missing:
        raise GraphifyContractError(
            "missing required CLI commands: " + ", ".join(sorted(missing))
        )
    try:
        capability_probe = _run_capability_smoke(executable, contract, runner, commands)
        assert_capability_surface(contract, capability_probe)
    except CompatibilityError as error:
        raise GraphifyContractError(str(error)) from error
    return GraphifyCapabilities(
        version=installed_version,
        commands=frozenset(commands),
        supports_obsidian_export=False,
        supports_global_registry="global" in commands,
        executable=executable,
        capability_probe=capability_probe,
    )


def _run_capability_smoke(
    executable: ResolvedGraphifyExecutable,
    contract: GraphifyCompatibility,
    runner: CommandRunner,
    commands: frozenset[str],
) -> CapabilityProbe:
    smoke_payload = resources.files("project_knowledge.compatibility_fixtures").joinpath(
        "runtime_probe.py"
    ).read_bytes()
    try:
        with tempfile.TemporaryDirectory(prefix="atlasweaver-graphify-probe-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            source = root / "source"
            source.mkdir(mode=0o700)
            (source / "runtime_probe.py").write_bytes(smoke_payload)
            code_output = root / "code"
            semantic_output = root / "semantic"
            code_command = render_graphify_argv(
                contract, "extract", binary=executable.path, source=source,
                output=code_output, code_only=True,
            )
            semantic_command = render_graphify_argv(
                contract, "extract", binary=executable.path, source=source,
                output=semantic_output, backend="ollama",
                model="atlasweaver-capability-probe", deep=True,
            )
            base_environment = admitted_graphify_environment(
                contract, None,
                {**minimal_environment(), "HOME": str(root / "extract-home")},
            )
            Path(base_environment["HOME"]).mkdir(mode=0o700)
            run_checked(runner, executable, code_command.argv, env=base_environment)
            run_checked(runner, executable, semantic_command.argv, env=base_environment)

            native_path = code_output / "graphify-out" / "graph.json"
            semantic_path = semantic_output / "graphify-out" / "graph.json"
            native = _read_probe_json(native_path)
            semantic = _read_probe_json(semantic_path)
            native_fingerprint = _validate_native_document(native)
            if _validate_native_document(semantic) != native_fingerprint:
                raise GraphifyContractError("Graphify native schema mismatch")

            diagnose_command = render_graphify_argv(
                contract, "diagnose", binary=executable.path, source=source,
                output=code_output, graph=native_path,
            )
            diagnosis_result = run_checked(
                runner, executable, diagnose_command.argv, env=base_environment
            )
            diagnosis_full = _load_bounded_json_text(diagnosis_result.stdout)
            diagnosis = _sanitize_diagnosis(diagnosis_full)
            diagnostic_fingerprint = _validate_diagnostic_document(diagnosis)

            cluster_command = render_graphify_argv(
                contract, "cluster", binary=executable.path, source=code_output,
                output=code_output, graph=native_path, track_html=False,
            )
            run_checked(runner, executable, cluster_command.argv, env=base_environment)
            clustered_fingerprint = _validate_clustered_document(
                _read_probe_json(native_path)
            )

            registry_home = root / "registry-home"
            registry_home.mkdir(mode=0o700)
            probe_key = "atlasweaver/00000000-0000-4000-8000-000000000000"
            global_command = render_graphify_global_add(
                contract, binary=executable.path, graph=native_path,
                registry_key=probe_key,
            )
            registry_environment = admitted_graphify_environment(
                contract, None,
                {**minimal_environment(), "HOME": str(registry_home)},
            )
            run_checked(runner, executable, global_command.argv, env=registry_environment)
            registry_root = registry_home / ".graphify"
            global_graph = _read_probe_json(registry_root / "global-graph.json")
            global_manifest = _read_probe_json(registry_root / "global-manifest.json")
            _validate_global_probe(global_graph, global_manifest, probe_key)

            install_commands = []
            installed_platforms: set[str] = set()
            for target in contract.agent_installs:
                install_home = root / f"install-{target.platform}"
                install_home.mkdir(mode=0o700)
                install_command = render_graphify_agent_install(
                    contract, binary=executable.path, platform=target.platform
                )
                install_environment = admitted_graphify_environment(
                    contract, None,
                    {**minimal_environment(), "HOME": str(install_home)},
                )
                run_checked(
                    runner, executable, install_command.argv, env=install_environment
                )
                _validate_install_home(install_home, target.home_relative_skill)
                installed_platforms.add(target.platform)
                install_commands.append(install_command)

            canonical_commands = (
                code_command.canonical_argv,
                semantic_command.canonical_argv,
                diagnose_command.canonical_argv,
                cluster_command.canonical_argv,
                global_command.canonical_argv,
                *(item.canonical_argv for item in install_commands),
            )
            digest_payload = {
                "installed_version": contract.version,
                "commands": sorted(commands),
                "canonical_commands": canonical_commands,
                "native_schema_fingerprint": native_fingerprint,
                "diagnostic_schema_fingerprint": diagnostic_fingerprint,
                "clustered_schema_fingerprint": clustered_fingerprint,
                "global_add_verified": True,
                "agent_install_platforms": sorted(installed_platforms),
                "smoke_source_sha256": hashlib.sha256(smoke_payload).hexdigest(),
                "compatibility_fixture_digest": contract.compatibility_fixture_digest,
            }
            digest = hashlib.sha256(
                b"atlasweaver-graphify-capability-v1\0"
                + json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            return CapabilityProbe(
                installed_version=contract.version,
                commands=commands,
                canonical_commands=canonical_commands,
                native_schema_fingerprint=native_fingerprint,
                diagnostic_schema_fingerprint=diagnostic_fingerprint,
                clustered_schema_fingerprint=clustered_fingerprint,
                global_add_verified=True,
                agent_install_platforms=frozenset(installed_platforms),
                digest=digest,
            )
    except GraphifyError:
        raise
    except (OSError, ValueError, json.JSONDecodeError, CompatibilityError) as error:
        raise GraphifyContractError("Graphify capability smoke failed") from error


def _read_probe_json(path: Path) -> object:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_PROBE_ARTIFACT_BYTES:
            raise OSError("invalid probe artifact")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
                raise OSError("unstable probe artifact")
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(descriptor, min(65_536, MAX_PROBE_ARTIFACT_BYTES + 1 - total)):
                total += len(chunk)
                if total > MAX_PROBE_ARTIFACT_BYTES:
                    raise OSError("oversized probe artifact")
                chunks.append(chunk)
            final = os.fstat(descriptor)
            if (
                final.st_dev != opened.st_dev
                or final.st_ino != opened.st_ino
                or final.st_size != opened.st_size
                or final.st_mtime_ns != opened.st_mtime_ns
                or total != opened.st_size
            ):
                raise OSError("unstable probe artifact")
        finally:
            os.close(descriptor)
        return _load_bounded_json_text(b"".join(chunks).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise GraphifyContractError("Graphify capability artifact is invalid") from error


def _load_bounded_json_text(payload: str) -> object:
    if len(payload.encode("utf-8")) > MAX_PROBE_ARTIFACT_BYTES:
        raise GraphifyContractError("Graphify capability artifact is invalid")
    return json.loads(payload, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def _sanitize_diagnosis(document: object) -> dict[str, object]:
    if not isinstance(document, Mapping) or set(document) < {"schema_version", "summary"}:
        raise GraphifyContractError("Graphify diagnostic schema mismatch")
    summary = document["summary"]
    if not isinstance(summary, Mapping):
        raise GraphifyContractError("Graphify diagnostic schema mismatch")
    if any(name not in summary for name in _DIAGNOSTIC_SUMMARY_FIELDS):
        raise GraphifyContractError("Graphify diagnostic schema mismatch")
    return {
        "schema_version": document["schema_version"],
        "summary": {name: summary[name] for name in _DIAGNOSTIC_SUMMARY_FIELDS},
    }


def _validate_global_probe(global_graph: object, manifest: object, key: str) -> None:
    if not isinstance(global_graph, Mapping):
        raise GraphifyContractError("Graphify global add verification failed")
    nodes = global_graph.get("nodes")
    links = global_graph.get("links", global_graph.get("edges"))
    if not isinstance(nodes, list) or not nodes or not isinstance(links, list):
        raise GraphifyContractError("Graphify global add verification failed")
    if not isinstance(manifest, Mapping):
        raise GraphifyContractError("Graphify global add verification failed")
    repositories = manifest.get("repos", manifest.get("repositories"))
    if not isinstance(repositories, Mapping) or set(repositories) != {key}:
        raise GraphifyContractError("Graphify global add verification failed")


def _validate_install_home(home: Path, target: PurePosixPath) -> None:
    expected = home.joinpath(*target.parts)
    document = _read_regular_bytes(expected)
    if not document:
        raise GraphifyContractError("Graphify agent install verification failed")
    canonical_home = home.resolve()
    for path in home.rglob("*"):
        if path.is_symlink():
            raise GraphifyContractError("Graphify agent install verification failed")
        try:
            path.resolve().relative_to(canonical_home)
        except ValueError as error:
            raise GraphifyContractError("Graphify agent install verification failed") from error


def _read_regular_bytes(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_PROBE_ARTIFACT_BYTES:
        raise GraphifyContractError("Graphify capability artifact is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
            raise GraphifyContractError("Graphify capability artifact is invalid")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(
            descriptor, min(65_536, MAX_PROBE_ARTIFACT_BYTES + 1 - total)
        ):
            total += len(chunk)
            if total > MAX_PROBE_ARTIFACT_BYTES:
                raise GraphifyContractError("Graphify capability artifact is invalid")
            chunks.append(chunk)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        final.st_dev != opened.st_dev
        or final.st_ino != opened.st_ino
        or final.st_size != opened.st_size
        or final.st_mtime_ns != opened.st_mtime_ns
        or total != opened.st_size
    ):
        raise GraphifyContractError("Graphify capability artifact is invalid")
    return b"".join(chunks)


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
    executable: ResolvedGraphifyExecutable,
    graph: Path,
    question: str,
    runner: CommandRunner,
) -> CompletedProcess[str]:
    """Query a validated graph, keeping the complete question as one argv item."""
    return run_checked(
        runner,
        executable,
        (str(executable.path), "query", question, "--graph", str(graph)),
    )


def sync_global_registry(
    executable: ResolvedGraphifyExecutable,
    graph: Path,
    registry_key: str,
    runner: CommandRunner,
) -> RegistryResult:
    """Register a validated graph, degrading only registry health on failure."""
    if not graph.is_file():
        return RegistryResult(
            status="partial",
            project_id=registry_key,
            detail="project graph is unavailable for registration",
        )
    try:
        from .compatibility import production_graphify_compatibility

        command = render_graphify_global_add(
            production_graphify_compatibility(),
            binary=executable.path,
            graph=graph,
            registry_key=registry_key,
        )
        run_checked(
            runner,
            executable,
            command.argv,
        )
    except (GraphifyCommandError, CompatibilityError) as error:
        detail = error.detail if isinstance(error, GraphifyCommandError) else str(error)
        return RegistryResult(status="partial", project_id=registry_key, detail=detail)
    return RegistryResult(status="registered", project_id=registry_key)


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
