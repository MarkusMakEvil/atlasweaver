"""Bounded, read-only probing of the newest Graphify release."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import argparse
from dataclasses import dataclass, replace
import http.client
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
from typing import Any, Protocol, Sequence

from packaging.tags import Tag, sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from .compatibility import (
    CapabilityProbe,
    _expected_canonical_commands,
    production_graphify_compatibility,
    supported_graphify_versions,
)
from .graphify import (
    CommandRunner,
    GraphifyCommandError,
    GraphifyContractError,
    ResolvedGraphifyExecutable,
    SubprocessCommandRunner,
    _parse_cli_commands,
    _parse_version,
    _run_capability_smoke,
    resolve_graphify_executable,
    run_checked,
)


PYPI_HOST = "pypi.org"
PYPI_PORT = 443
PYPI_PATH = "/pypi/graphifyy/json"
PYPI_TIMEOUT = 15
PYPI_MAX_BYTES = 2_097_152


class ProbeError(ValueError):
    """A stable, sanitized compatibility-probe failure."""


class _Response(Protocol):
    status: int

    def getheader(self, name: str, default: str | None = None) -> str | None: ...

    def read(self, amount: int) -> bytes: ...

    def close(self) -> None: ...


class _Connection(Protocol):
    def request(
        self, method: str, path: str, *, headers: Mapping[str, str]
    ) -> None: ...

    def getresponse(self) -> _Response: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[..., _Connection]
ProbeInstaller = Callable[[Version, Path], CapabilityProbe]
ProbeObserver = Callable[
    [ResolvedGraphifyExecutable, object, CommandRunner], CapabilityProbe
]


@dataclass(frozen=True)
class _ToolIdentity:
    path: Path
    st_dev: int
    st_ino: int
    sha256: str


def fetch_pypi_document(
    connection_factory: ConnectionFactory | _Connection | _Response | None = None,
) -> Mapping[str, Any]:
    """Fetch and strictly parse the fixed, bounded official PyPI document."""
    connection: _Connection | None = None
    response: _Response | None = None
    try:
        if connection_factory is None:
            connection = http.client.HTTPSConnection(
                PYPI_HOST,
                PYPI_PORT,
                timeout=PYPI_TIMEOUT,
                context=ssl.create_default_context(),
            )
        elif callable(connection_factory):
            connection = connection_factory(
                PYPI_HOST,
                PYPI_PORT,
                timeout=PYPI_TIMEOUT,
                context=ssl.create_default_context(),
            )
        elif hasattr(connection_factory, "request") and hasattr(
            connection_factory, "getresponse"
        ):
            connection = connection_factory
        elif hasattr(connection_factory, "read") and hasattr(
            connection_factory, "status"
        ):
            response = connection_factory
        else:
            raise ProbeError("pypi_request_failed")

        if connection is not None:
            connection.request(
                "GET",
                PYPI_PATH,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
        assert response is not None
        status = response.status
        if type(status) is not int:
            raise ProbeError("pypi_request_failed")
        if 300 <= status < 400:
            raise ProbeError("pypi_redirect_forbidden")
        if status != 200:
            raise ProbeError("pypi_request_failed")

        content_type = response.getheader("Content-Type")
        if (
            type(content_type) is not str
            or content_type.split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise ProbeError("pypi_content_type_invalid")
        expected_length = _content_length(response.getheader("Content-Length"))
        if expected_length is not None and expected_length > PYPI_MAX_BYTES:
            raise ProbeError("pypi_response_too_large")

        payload = _read_bounded_response(response)
        if expected_length is not None and len(payload) < expected_length:
            raise ProbeError("pypi_response_short_read")
        if expected_length is not None and len(payload) != expected_length:
            raise ProbeError("pypi_response_length_mismatch")
        return _strict_json_mapping(payload)
    except ProbeError:
        raise
    except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError):
        raise ProbeError("pypi_request_failed") from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def resolve_latest_stable_wheel(
    document: object,
    supported_tags: frozenset[Tag] | Iterable[Tag],
) -> Version:
    """Select the highest stable, non-yanked Graphify wheel for this runtime."""
    if not isinstance(document, Mapping):
        raise ProbeError("pypi_document_invalid")
    releases = document.get("releases")
    if not isinstance(releases, Mapping):
        raise ProbeError("pypi_document_invalid")
    try:
        admitted_tags = frozenset(supported_tags)
    except TypeError:
        raise ProbeError("pypi_document_invalid") from None
    if not admitted_tags or any(type(tag) is not Tag for tag in admitted_tags):
        raise ProbeError("pypi_document_invalid")

    candidates: set[Version] = set()
    for raw_version, files in releases.items():
        if type(raw_version) is not str or not isinstance(files, list):
            continue
        try:
            version = Version(raw_version)
        except InvalidVersion:
            continue
        if version.is_prerelease or version.is_devrelease or version.local is not None:
            continue
        if any(
            _is_compatible_graphify_wheel(item, version, admitted_tags)
            for item in files
        ):
            candidates.add(version)
    if not candidates:
        raise ProbeError("pypi_no_compatible_release")
    return max(candidates)


def run_upstream_probe(
    *,
    candidate_version: Version,
    output: Path,
    installer: ProbeInstaller | None = None,
    uv: Path | None = None,
) -> dict[str, object]:
    """Probe one exact release and atomically persist a closed safe report."""
    if type(candidate_version) is not Version or not _is_stable(candidate_version):
        raise ProbeError("candidate_version_invalid")
    if installer is None:
        if uv is None:
            raise ProbeError("uv_executable_invalid")
        installer = _production_installer(uv)

    observation: CapabilityProbe | None = None
    outcome = "inconclusive"
    limitations: tuple[str, ...] = ("probe_install_failed",)
    try:
        with tempfile.TemporaryDirectory(prefix="atlasweaver-upstream-probe-") as name:
            root = Path(name)
            root.chmod(0o700)
            observation = installer(candidate_version, root)
        limitations = _surface_limitations(candidate_version, observation)
        outcome = "compatible" if not limitations else "incompatible"
    except GraphifyContractError as error:
        if str(error) == "graphify_executable_changed":
            outcome = "incompatible"
            limitations = ("graphify_executable_changed",)
        else:
            outcome = "incompatible"
            limitations = ("capability_smoke_failed",)
    except GraphifyCommandError:
        outcome = "incompatible"
        limitations = ("graphify_command_failed",)
    except ProbeError as error:
        code = _safe_limitation(str(error), fallback="probe_install_failed")
        if code == "graphify_executable_changed":
            outcome = "incompatible"
        limitations = (code,)
    except (OSError, TimeoutError, ValueError):
        limitations = ("probe_install_failed",)

    report = _report(
        candidate_version=str(candidate_version),
        executable_version=(
            _safe_executable_version(observation.installed_version)
            if observation is not None
            else None
        ),
        outcome=outcome,
        limitations=limitations,
    )
    _write_report_atomic(output, report)
    return report


def main(
    argv: Sequence[str] | None = None,
    *,
    fetcher: Callable[[], object] = fetch_pypi_document,
    installer: ProbeInstaller | None = None,
) -> int:
    """Run the scheduled probe; expected probe failures still emit a report."""
    parser = argparse.ArgumentParser(prog="python -m project_knowledge.compat_probe")
    parser.add_argument("--uv")
    parser.add_argument("--output", required=True)
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code or 2)
    output = Path(arguments.output)
    try:
        _validate_output_path(output)
    except ProbeError:
        return 2

    try:
        candidate = resolve_latest_stable_wheel(
            fetcher(), frozenset(sys_tags())
        )
    except ProbeError as error:
        report = _report(
            candidate_version=None,
            executable_version=None,
            outcome="inconclusive",
            limitations=(_safe_limitation(str(error), fallback="pypi_request_failed"),),
        )
        try:
            _write_report_atomic(output, report)
        except ProbeError:
            return 2
        return 0
    except (OSError, TimeoutError, ValueError):
        report = _report(
            candidate_version=None,
            executable_version=None,
            outcome="inconclusive",
            limitations=("pypi_request_failed",),
        )
        try:
            _write_report_atomic(output, report)
        except ProbeError:
            return 2
        return 0

    uv_path = Path(arguments.uv) if arguments.uv is not None else None
    if installer is None and uv_path is None:
        located = shutil.which("uv")
        if located is None:
            report = _report(
                candidate_version=str(candidate),
                executable_version=None,
                outcome="inconclusive",
                limitations=("uv_executable_unavailable",),
            )
            try:
                _write_report_atomic(output, report)
            except ProbeError:
                return 2
            return 0
        uv_path = Path(located)
    try:
        run_upstream_probe(
            candidate_version=candidate,
            output=output,
            installer=installer,
            uv=uv_path,
        )
    except ProbeError:
        return 2
    return 0


def _surface_limitations(
    candidate_version: Version, observation: object
) -> tuple[str, ...]:
    if type(observation) is not CapabilityProbe:
        return ("capability_probe_invalid",)
    contract = production_graphify_compatibility()
    limitations: list[str] = []
    if observation.installed_version != str(candidate_version):
        limitations.append("candidate_version_mismatch")
    if not contract.required_cli_commands <= observation.commands:
        limitations.append("required_command_missing")
    if observation.canonical_commands != _expected_canonical_commands(contract):
        limitations.append("canonical_command_mismatch")
    for field, code in (
        ("native_schema_fingerprint", "native_schema_mismatch"),
        ("diagnostic_schema_fingerprint", "diagnostic_schema_mismatch"),
        ("clustered_schema_fingerprint", "clustered_schema_mismatch"),
    ):
        if getattr(observation, field) != getattr(contract, field):
            limitations.append(code)
    if observation.global_add_verified is not True:
        limitations.append("global_add_mismatch")
    if observation.agent_install_platforms != frozenset(
        item.platform for item in contract.agent_installs
    ):
        limitations.append("agent_install_mismatch")
    if (
        type(observation.digest) is not str
        or len(observation.digest) != 64
        or any(character not in "0123456789abcdef" for character in observation.digest)
    ):
        limitations.append("capability_digest_invalid")
    return tuple(sorted(set(limitations)))


def _report(
    *,
    candidate_version: str | None,
    executable_version: str | None,
    outcome: str,
    limitations: Iterable[str],
) -> dict[str, object]:
    if outcome not in {"compatible", "incompatible", "inconclusive"}:
        raise ProbeError("probe_report_invalid")
    stable_limitations = sorted(
        {_safe_limitation(item, fallback="probe_failed") for item in limitations}
    )
    support_declared = (
        candidate_version is not None
        and candidate_version in supported_graphify_versions()
    )
    return {
        "schema_version": 1,
        "candidate_version": candidate_version,
        "executable_version": executable_version,
        "production_version": production_graphify_compatibility().version,
        "outcome": outcome,
        "support_declared": support_declared,
        "limitations": stable_limitations,
    }


def _write_report_atomic(output: Path, report: Mapping[str, object]) -> None:
    parent = _validate_output_path(output)
    payload = (
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=parent
        )
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        _validate_output_path(output)
        os.replace(temporary, output)
        temporary = None
        parent_descriptor = os.open(
            parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except (OSError, TypeError, ValueError):
        raise ProbeError("report_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_output_path(output: Path) -> Path:
    if not isinstance(output, Path) or output.name in {"", ".", ".."}:
        raise ProbeError("output_path_invalid")
    parent = output.absolute().parent
    try:
        metadata = parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise OSError("unsafe parent")
        output_metadata = output.lstat()
    except FileNotFoundError:
        return parent
    except OSError:
        raise ProbeError("output_path_invalid") from None
    if not stat.S_ISREG(output_metadata.st_mode):
        raise ProbeError("output_path_invalid")
    return parent


def _safe_limitation(value: object, *, fallback: str) -> str:
    if (
        type(value) is str
        and 1 <= len(value) <= 96
        and value[0].islower()
        and all(character.islower() or character.isdigit() or character == "_" for character in value)
    ):
        return value
    return fallback


def _is_stable(version: Version) -> bool:
    return not (
        version.is_prerelease or version.is_devrelease or version.local is not None
    )


def _safe_executable_version(value: object) -> str | None:
    if type(value) is not str:
        return None
    try:
        parsed = Version(value)
    except InvalidVersion:
        return None
    if str(parsed) != value or not _is_stable(parsed):
        return None
    return value


def _production_installer(uv: Path) -> ProbeInstaller:
    def install(version: Version, root: Path) -> CapabilityProbe:
        return _install_and_probe(
            version,
            root,
            uv=uv,
            runner=SubprocessCommandRunner(),
        )

    return install


def _install_and_probe(
    candidate_version: Version,
    root: Path,
    *,
    uv: Path,
    runner: CommandRunner,
    observer: ProbeObserver | None = None,
) -> CapabilityProbe:
    """Install one exact wheel into a private venv and smoke its explicit binary."""
    if not isinstance(root, Path) or not root.is_dir():
        raise ProbeError("probe_install_failed")
    try:
        root.chmod(0o700)
        uv_identity = _resolve_tool(uv)
        home = root / "home"
        home.mkdir(mode=0o700)
        venv = root / "venv"
        environment = {
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.defpath,
            "UV_INDEX_URL": "https://pypi.org/simple",
            "UV_NO_CONFIG": "1",
        }
        _run_uv(
            runner,
            uv_identity,
            (
                str(uv_identity.path),
                "venv",
                str(venv),
                "--python",
                sys.executable,
            ),
            environment,
        )
        venv_python = _venv_binary(venv, "python")
        _run_uv(
            runner,
            uv_identity,
            (
                str(uv_identity.path),
                "pip",
                "install",
                "--python",
                str(venv_python),
                f"graphifyy=={candidate_version}",
            ),
            environment,
        )
        graphify = resolve_graphify_executable(
            test_override=_venv_binary(venv, "graphify")
        )
    except ProbeError:
        raise
    except (OSError, ValueError, GraphifyContractError):
        raise ProbeError("probe_install_failed") from None

    candidate_contract = replace(
        production_graphify_compatibility(),
        version=str(candidate_version),
        production=False,
    )
    if observer is not None:
        return observer(graphify, candidate_contract, runner)
    return _observe_candidate_surface(
        graphify,
        candidate_contract,
        _SanitizedProbeRunner(runner),
        home=root / "runtime-home",
    )


class _SanitizedProbeRunner:
    """Keep Graphify smoke subprocesses on a fixed base-only environment."""

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def run(
        self,
        argv: Sequence[str],
        *,
        text: bool,
        capture_output: bool,
        check: bool,
        timeout: float,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        home = env.get("HOME")
        if type(home) is not str or not home:
            raise OSError("missing probe home")
        return self._runner.run(
            argv,
            text=text,
            capture_output=capture_output,
            check=check,
            timeout=timeout,
            env={
                "HOME": home,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.defpath,
            },
        )


def _observe_candidate_surface(
    executable: ResolvedGraphifyExecutable,
    contract: object,
    runner: CommandRunner,
    *,
    home: Path,
) -> CapabilityProbe:
    from .compatibility import GraphifyCompatibility

    if type(contract) is not GraphifyCompatibility:
        raise GraphifyContractError("Graphify capability smoke failed")
    home.mkdir(mode=0o700)
    environment = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
    }
    version_result = run_checked(
        runner,
        executable,
        (str(executable.path), "--version"),
        env=environment,
    )
    installed_version = _parse_version(version_result.stdout)
    help_result = run_checked(
        runner,
        executable,
        (str(executable.path), "--help"),
        env=environment,
    )
    commands = _parse_cli_commands(help_result.stdout)
    if installed_version != contract.version or not contract.required_cli_commands <= commands:
        return CapabilityProbe(
            installed_version=installed_version,
            commands=commands,
            canonical_commands=(),
            native_schema_fingerprint="0" * 64,
            diagnostic_schema_fingerprint="0" * 64,
            clustered_schema_fingerprint="0" * 64,
            global_add_verified=False,
            agent_install_platforms=frozenset(),
            digest="0" * 64,
        )
    return _run_capability_smoke(executable, contract, runner, commands)


def _venv_binary(venv: Path, name: str) -> Path:
    if os.name == "nt":
        suffix = ".exe"
        return venv / "Scripts" / f"{name}{suffix}"
    return venv / "bin" / name


def _run_uv(
    runner: CommandRunner,
    executable: _ToolIdentity,
    argv: tuple[str, ...],
    environment: Mapping[str, str],
) -> None:
    if not argv or argv[0] != str(executable.path):
        raise ProbeError("probe_install_failed")
    _revalidate_tool(executable)
    try:
        result = runner.run(
            argv,
            text=True,
            capture_output=True,
            check=False,
            timeout=120.0,
            env=dict(environment),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ProbeError("probe_install_failed") from None
    if result.returncode != 0:
        raise ProbeError("probe_install_failed")


def _resolve_tool(path: Path) -> _ToolIdentity:
    try:
        canonical = path.resolve(strict=True)
        metadata, digest = _read_tool(canonical)
    except (OSError, ValueError):
        raise ProbeError("probe_install_failed") from None
    return _ToolIdentity(canonical, metadata.st_dev, metadata.st_ino, digest)


def _revalidate_tool(executable: _ToolIdentity) -> None:
    try:
        metadata, digest = _read_tool(executable.path)
    except OSError:
        raise ProbeError("probe_install_failed") from None
    if (
        metadata.st_dev != executable.st_dev
        or metadata.st_ino != executable.st_ino
        or digest != executable.sha256
    ):
        raise ProbeError("probe_install_failed")


def _read_tool(path: Path) -> tuple[os.stat_result, str]:
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_mode & 0o111 == 0
        or before.st_size <= 0
        or before.st_size > 134_217_728
    ):
        raise OSError("tool identity invalid")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
        ):
            raise OSError("tool identity unstable")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 65_536):
            total += len(chunk)
            if total > 134_217_728:
                raise OSError("tool identity invalid")
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        after.st_dev != opened.st_dev
        or after.st_ino != opened.st_ino
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
        or total != opened.st_size
    ):
        raise OSError("tool identity unstable")
    return after, digest.hexdigest()


if __name__ == "__main__":  # pragma: no cover - exercised via the module CLI
    raise SystemExit(main())


def _is_compatible_graphify_wheel(
    item: object, version: Version, supported_tags: frozenset[Tag]
) -> bool:
    if not isinstance(item, Mapping):
        return False
    yanked = item.get("yanked", False)
    if type(yanked) is not bool or yanked:
        return False
    filename = item.get("filename")
    packagetype = item.get("packagetype", "bdist_wheel")
    if (
        type(filename) is not str
        or not filename.endswith(".whl")
        or packagetype != "bdist_wheel"
    ):
        return False
    try:
        distribution, wheel_version, _, wheel_tags = parse_wheel_filename(filename)
    except (InvalidVersion, ValueError):
        return False
    return (
        canonicalize_name(str(distribution)) == canonicalize_name("graphifyy")
        and wheel_version == version
        and bool(wheel_tags & supported_tags)
    )


def _content_length(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not str or not value.isascii() or not value.isdigit():
        raise ProbeError("pypi_content_length_invalid")
    length = int(value)
    if length < 0:
        raise ProbeError("pypi_content_length_invalid")
    return length


def _read_bounded_response(response: _Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(65_536, PYPI_MAX_BYTES + 1 - total))
        if type(chunk) is not bytes:
            raise ProbeError("pypi_response_invalid")
        if not chunk:
            break
        total += len(chunk)
        if total > PYPI_MAX_BYTES:
            raise ProbeError("pypi_response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _strict_json_mapping(payload: bytes) -> Mapping[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ProbeError("pypi_response_invalid")
            result[key] = value
        return result

    def finite(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ProbeError("pypi_response_invalid")
        return parsed

    try:
        document = json.loads(
            payload,
            object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ProbeError("pypi_response_invalid")
            ),
            parse_float=finite,
        )
    except ProbeError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ProbeError("pypi_response_invalid") from None
    if not isinstance(document, Mapping):
        raise ProbeError("pypi_response_invalid")
    return document
