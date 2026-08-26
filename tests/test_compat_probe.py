from __future__ import annotations

import json
import os
from pathlib import Path
from subprocess import CompletedProcess
import sys
from typing import Any

from packaging.tags import Tag
from packaging.version import Version
import pytest

from project_knowledge.compat_probe import (
    ProbeError,
    _install_and_probe,
    fetch_pypi_document,
    main,
    resolve_latest_stable_wheel,
    run_upstream_probe,
)
from project_knowledge.compatibility import CapabilityProbe, production_graphify_compatibility
from project_knowledge.graphify import GraphifyCommandError


SUPPORTED_TAGS = frozenset({Tag("py3", "none", "any")})


def wheel(
    filename: str,
    *,
    yanked: bool = False,
    packagetype: str = "bdist_wheel",
) -> dict[str, object]:
    return {
        "filename": filename,
        "packagetype": packagetype,
        "yanked": yanked,
    }


def sdist(filename: str) -> dict[str, object]:
    return wheel(filename, packagetype="sdist")


def pypi_document(releases: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    return {"releases": releases}


def test_resolver_selects_highest_stable_non_yanked_compatible_wheel() -> None:
    document = pypi_document(
        {
            "0.9.48": [wheel("graphifyy-0.9.48-py3-none-any.whl")],
            "0.9.49rc1": [wheel("graphifyy-0.9.49rc1-py3-none-any.whl")],
            "0.9.49": [wheel("graphifyy-0.9.49-py3-none-any.whl", yanked=True)],
            "0.9.50": [sdist("graphifyy-0.9.50.tar.gz")],
            "0.9.51": [wheel("graphifyy-0.9.51-py3-none-any.whl")],
        }
    )

    assert resolve_latest_stable_wheel(document, SUPPORTED_TAGS) == Version("0.9.51")


def test_resolver_ignores_invalid_unstable_local_and_incompatible_releases() -> None:
    document = pypi_document(
        {
            "not-a-version": [wheel("graphifyy-not-a-version-py3-none-any.whl")],
            "1.0.dev1": [wheel("graphifyy-1.0.dev1-py3-none-any.whl")],
            "1.0+private": [wheel("graphifyy-1.0+private-py3-none-any.whl")],
            "1.0": [wheel("other-1.0-py3-none-any.whl")],
            "1.1": [wheel("graphifyy-1.1-cp313-cp313-manylinux_2_40_x86_64.whl")],
            "1.2": [wheel("not a wheel")],
            "0.9.48": [wheel("graphifyy-0.9.48-py3-none-any.whl")],
        }
    )

    assert resolve_latest_stable_wheel(document, SUPPORTED_TAGS) == Version("0.9.48")


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"releases": []},
        {"releases": {"0.9.48": []}},
        {"releases": {"0.9.48": [{"filename": "x.whl", "yanked": "false"}]}},
    ],
)
def test_resolver_rejects_invalid_or_empty_release_documents(document: object) -> None:
    with pytest.raises(ProbeError, match="pypi_(document_invalid|no_compatible_release)"):
        resolve_latest_stable_wheel(document, SUPPORTED_TAGS)


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
        content_length: str | None = None,
    ) -> None:
        self.payload = payload
        self.status = status
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(payload)) if content_length is None else content_length,
        }
        self.offset = 0
        self.closed = False

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def read(self, amount: int) -> bytes:
        result = self.payload[self.offset : self.offset + amount]
        self.offset += len(result)
        return result

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
    ) -> None:
        self.requests.append((method, path, headers))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class RecordingFactory:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> FakeConnection:
        self.calls.append((args, kwargs))
        return self.connection


def test_pypi_fetch_uses_fixed_direct_request_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "NETRC",
        "COOKIE",
    ):
        monkeypatch.setenv(name, f"private-{name.lower()}")
    response = FakeResponse(json.dumps({"releases": {}}).encode())
    connection = FakeConnection(response)
    factory = RecordingFactory(connection)

    document = fetch_pypi_document(factory)

    assert document == {"releases": {}}
    assert factory.calls[0][0] == ("pypi.org", 443)
    assert set(factory.calls[0][1]) == {"timeout", "context"}
    assert connection.requests == [
        (
            "GET",
            "/pypi/graphifyy/json",
            {"Accept": "application/json", "Accept-Encoding": "identity"},
        )
    ]
    assert response.closed is True
    assert connection.closed is True
    serialized_calls = repr(factory.calls) + repr(connection.requests)
    assert "private-" not in serialized_calls


@pytest.mark.parametrize(
    ("response", "limitation"),
    [
        (FakeResponse(b"x" * 2_097_153), "pypi_response_too_large"),
        (FakeResponse(b"{}", status=302), "pypi_redirect_forbidden"),
        (FakeResponse(b"{}", status=399), "pypi_redirect_forbidden"),
        (FakeResponse(b"{}", content_type="text/html"), "pypi_content_type_invalid"),
        (FakeResponse(b"{}", content_length="3"), "pypi_response_short_read"),
    ],
)
def test_pypi_fetch_rejects_bounded_protocol_failures(
    response: FakeResponse,
    limitation: str,
) -> None:
    connection = FakeConnection(response)

    with pytest.raises(ProbeError, match=limitation):
        fetch_pypi_document(RecordingFactory(connection))

    assert response.closed is True
    assert connection.closed is True


@pytest.mark.parametrize(
    "payload",
    [
        b'{"releases":{},"releases":{}}',
        b'{"releases":{},"value":NaN}',
        b'{"releases":{},"value":1e999}',
    ],
)
def test_pypi_fetch_rejects_non_strict_json(payload: bytes) -> None:
    with pytest.raises(ProbeError, match="pypi_response_invalid"):
        fetch_pypi_document(RecordingFactory(FakeConnection(FakeResponse(payload))))


CANONICAL_COMMANDS = (
    (
        "<graphify>",
        "extract",
        "<staged-root>",
        "--out",
        "<raw-output-root>",
        "--no-cluster",
        "--code-only",
    ),
    (
        "<graphify>",
        "extract",
        "<staged-root>",
        "--out",
        "<raw-output-root>",
        "--no-cluster",
        "--backend",
        "ollama",
        "--model",
        "atlasweaver-capability-probe",
        "--mode",
        "deep",
    ),
    (
        "<graphify>",
        "diagnose",
        "multigraph",
        "--graph",
        "<native-graph>",
        "--undirected",
        "--json",
    ),
    (
        "<graphify>",
        "cluster-only",
        "<cluster-workspace>",
        "--graph",
        "<cluster-input>",
        "--no-label",
        "--no-viz",
    ),
    ("<graphify>", "global", "add", "<registry-graph>", "--as", "<registry-key>"),
    ("<graphify>", "install", "--platform", "codex"),
    ("<graphify>", "install", "--platform", "agents"),
)


def capability_probe(version: Version, **changes: object) -> CapabilityProbe:
    contract = production_graphify_compatibility()
    values: dict[str, object] = {
        "installed_version": str(version),
        "commands": contract.required_cli_commands,
        "canonical_commands": CANONICAL_COMMANDS,
        "native_schema_fingerprint": contract.native_schema_fingerprint,
        "diagnostic_schema_fingerprint": contract.diagnostic_schema_fingerprint,
        "clustered_schema_fingerprint": contract.clustered_schema_fingerprint,
        "global_add_verified": True,
        "agent_install_platforms": frozenset({"codex", "agents"}),
        "digest": "a" * 64,
    }
    values.update(changes)
    return CapabilityProbe(**values)  # type: ignore[arg-type]


class FakeInstaller:
    def __init__(self, probe: CapabilityProbe | BaseException) -> None:
        self.probe = probe
        self.calls: list[tuple[Version, Path]] = []

    def __call__(self, version: Version, root: Path) -> CapabilityProbe:
        assert root.is_dir()
        assert root.stat().st_mode & 0o077 == 0
        self.calls.append((version, root))
        if isinstance(self.probe, BaseException):
            raise self.probe
        return self.probe


def test_upstream_candidate_never_declares_support(tmp_path: Path) -> None:
    candidate = Version("0.9.99")
    installer = FakeInstaller(
        capability_probe(candidate, clustered_schema_fingerprint="b" * 64)
    )
    output = tmp_path / "report.json"

    report = run_upstream_probe(
        candidate_version=candidate,
        output=output,
        installer=installer,
    )

    assert report["candidate_version"] == "0.9.99"
    assert report["executable_version"] == "0.9.99"
    assert report["support_declared"] is False
    assert report["outcome"] == "incompatible"
    assert report["limitations"] == ["clustered_schema_mismatch"]
    assert output.read_bytes() == (
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def test_matching_probe_is_compatible_but_only_registry_version_is_supported(
    tmp_path: Path,
) -> None:
    future = Version("0.9.99")
    future_report = run_upstream_probe(
        candidate_version=future,
        output=tmp_path / "future.json",
        installer=FakeInstaller(capability_probe(future)),
    )
    supported = Version(production_graphify_compatibility().version)
    supported_report = run_upstream_probe(
        candidate_version=supported,
        output=tmp_path / "supported.json",
        installer=FakeInstaller(capability_probe(supported)),
    )

    assert future_report["outcome"] == "compatible"
    assert future_report["support_declared"] is False
    assert supported_report["outcome"] == "compatible"
    assert supported_report["support_declared"] is True


def test_install_failure_is_inconclusive_and_sanitized(tmp_path: Path) -> None:
    report = run_upstream_probe(
        candidate_version=Version("0.9.99"),
        output=tmp_path / "report.json",
        installer=FakeInstaller(OSError("/Users/private token=secret")),
    )

    assert report["outcome"] == "inconclusive"
    assert report["limitations"] == ["probe_install_failed"]
    assert "/Users/" not in json.dumps(report)
    assert "secret" not in json.dumps(report)


def test_malformed_executable_version_cannot_enter_the_report(tmp_path: Path) -> None:
    report = run_upstream_probe(
        candidate_version=Version("0.9.99"),
        output=tmp_path / "report.json",
        installer=FakeInstaller(
            capability_probe(
                Version("0.9.99"), installed_version="/Users/private/secret"
            )
        ),
    )

    assert report["outcome"] == "incompatible"
    assert report["executable_version"] is None
    assert "/Users/" not in json.dumps(report)


def test_executed_graphify_process_failure_is_incompatible_and_sanitized(
    tmp_path: Path,
) -> None:
    report = run_upstream_probe(
        candidate_version=Version("0.9.99"),
        output=tmp_path / "report.json",
        installer=FakeInstaller(
            GraphifyCommandError("extract", "/Users/private token=secret")
        ),
    )

    assert report["outcome"] == "incompatible"
    assert report["limitations"] == ["graphify_command_failed"]
    assert "/Users/" not in json.dumps(report)
    assert "secret" not in json.dumps(report)


def test_network_failure_is_inconclusive_and_reported(tmp_path: Path) -> None:
    output = tmp_path / "report.json"

    def timeout_fetcher() -> object:
        raise ProbeError("pypi_request_failed")

    result = main(
        ["--uv", str(tmp_path / "unused-uv"), "--output", str(output)],
        fetcher=timeout_fetcher,
    )

    assert result == 0
    report = json.loads(output.read_text())
    assert report == {
        "candidate_version": None,
        "executable_version": None,
        "limitations": ["pypi_request_failed"],
        "outcome": "inconclusive",
        "production_version": production_graphify_compatibility().version,
        "schema_version": 1,
        "support_declared": False,
    }


def test_report_output_rejects_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "outside.json"
    output = tmp_path / "report.json"
    target.write_text("leave me alone")
    output.symlink_to(target)

    result = main(
        ["--uv", str(tmp_path / "unused-uv"), "--output", str(output)],
        fetcher=lambda: (_ for _ in ()).throw(ProbeError("pypi_request_failed")),
    )

    assert result != 0
    assert target.read_text() == "leave me alone"


def executable(path: Path) -> Path:
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


class InstallRunner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run(self, argv: object, **options: object) -> CompletedProcess[str]:
        command = tuple(argv)  # type: ignore[arg-type]
        self.calls.append((command, options))
        venv = self.root / "venv"
        binary = venv / ("Scripts" if os.name == "nt" else "bin")
        binary.mkdir(parents=True, exist_ok=True)
        if command[1] == "venv":
            executable(binary / ("python.exe" if os.name == "nt" else "python"))
        elif command[1:3] == ("pip", "install"):
            executable(binary / ("graphify.exe" if os.name == "nt" else "graphify"))
        return CompletedProcess(command, 0, "", "")


def test_install_boundary_uses_only_pinned_uv_and_a_sanitized_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "HTTPS_PROXY",
        "PIP_INDEX_URL",
        "OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.setenv(name, f"private-{name.lower()}")
    root = tmp_path / "private-probe"
    root.mkdir(mode=0o700)
    uv = executable(tmp_path / "uv")
    runner = InstallRunner(root)
    observed: list[Path] = []

    def observer(graphify, contract, selected_runner):  # type: ignore[no-untyped-def]
        observed.append(graphify.path)
        assert selected_runner is runner
        return capability_probe(Version(contract.version))

    result = _install_and_probe(
        Version("0.9.99"),
        root,
        uv=uv,
        runner=runner,
        observer=observer,
    )

    venv = root / "venv"
    venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    graphify = venv / ("Scripts/graphify.exe" if os.name == "nt" else "bin/graphify")
    assert [call for call, _ in runner.calls] == [
        (str(uv.resolve()), "venv", str(venv), "--python", sys.executable),
        (
            str(uv.resolve()),
            "pip",
            "install",
            "--python",
            str(venv_python),
            "graphifyy==0.9.99",
        ),
    ]
    for _, options in runner.calls:
        assert options["timeout"] == 120.0
        environment = options["env"]
        assert environment == {
            "HOME": str(root / "home"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.defpath,
            "UV_INDEX_URL": "https://pypi.org/simple",
            "UV_NO_CONFIG": "1",
        }
    assert observed == [graphify.resolve()]
    assert result.installed_version == "0.9.99"
