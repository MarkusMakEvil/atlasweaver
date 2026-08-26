"""Behavioral contract for the sole Graphify compatibility registry."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
from importlib import resources
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import stat
from subprocess import CompletedProcess
import subprocess
import sys

import pytest

from project_knowledge.compatibility import (
    AgentInstallContract,
    CapabilityProbe,
    CompatibilityError,
    EvidenceCapabilities,
    admitted_graphify_environment,
    assert_capability_surface,
    bind_semantic_backend_credential,
    production_graphify_compatibility,
    render_graphify_agent_install,
    render_graphify_argv,
    render_graphify_global_add,
    resolve_graphify_compatibility,
    supported_graphify_versions,
    validate_public_model_identifier,
    validate_semantic_backend,
)
from project_knowledge.graphify import (
    GraphifyContractError,
    resolve_graphify_executable,
    revalidate_graphify_executable,
    run_checked,
)


_CAPTURE_TOOL = runpy.run_path(
    str(Path("scripts/capture-graphify-compatibility-fixture").resolve()),
    run_name="atlasweaver_capture_fixture_tests",
)
_capture_load_json = _CAPTURE_TOOL["_load_json"]
_capture_reject_absolute_paths = _CAPTURE_TOOL["_reject_absolute_paths"]
_capture_write_output = _CAPTURE_TOOL["_write_output"]

_CAPTURE_SOURCE_MANIFEST = ".atlasweaver-fixture-source.json"


def _write_capture_source_manifest(
    source: Path, entries: list[object] | None = None
) -> None:
    document = {
        "files": ["fixture.py"] if entries is None else entries,
        "schema_version": 1,
    }
    (source / _CAPTURE_SOURCE_MANIFEST).write_bytes(
        (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )


def _run_capture_tool(
    launcher: Path, source: Path, output: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(Path("scripts/capture-graphify-compatibility-fixture").resolve()),
            "--binary", str(launcher),
            "--source", str(source),
            "--output", str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


_SOURCE_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".cjs", ".mjs", ".ts", ".tsx",
        ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".c", ".h", ".cc",
        ".cpp", ".cxx", ".hpp", ".cu", ".cuh", ".rb", ".swift", ".php",
        ".scala", ".lua", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1",
        ".ex", ".exs", ".m", ".mm", ".ml", ".mli", ".jl", ".r", ".dart",
        ".zig", ".v", ".vhd", ".vhdl", ".sql", ".tf", ".hcl", ".f", ".f90",
        ".f95", ".pas", ".apex", ".lisp", ".clj", ".cljs",
    }
)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, **options):  # type: ignore[no-untyped-def]
        call = tuple(argv)
        self.calls.append(call)
        return CompletedProcess(call, 0, "", "")


def _launcher(path: Path, payload: bytes = b"#!/bin/sh\nexit 0\n") -> Path:
    path.write_bytes(payload)
    path.chmod(0o755)
    return path


def _capture_graphify_launcher(tmp_path: Path) -> tuple[Path, Path]:
    calls = tmp_path / "capture-calls.jsonl"
    launcher = tmp_path / "capture-graphify"
    launcher.write_text(
        f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

with Path({str(calls)!r}).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
arguments = sys.argv[1:]
if arguments == ["--version"]:
    print("graphify 0.9.48")
elif arguments and arguments[0] == "extract":
    output = Path(arguments[arguments.index("--out") + 1]) / "graphify-out"
    output.mkdir(parents=True)
    (output / "graph.json").write_text(json.dumps({{
        "nodes": [{{"id": "fixture"}}], "edges": [], "hyperedges": [],
        "input_tokens": 0, "output_tokens": 0
    }}), encoding="utf-8")
elif arguments[:2] == ["diagnose", "multigraph"]:
    print(json.dumps({{
        "schema_version": 1,
        "summary": {{
            "node_count": 1, "raw_edge_count": 0, "missing_endpoint_edges": 0,
            "dangling_endpoint_edges": 0, "self_loop_edges": 0,
            "exact_duplicate_edges": 0, "undirected_unique_endpoint_pairs": 0,
            "undirected_same_endpoint_collapsed_edges": 0,
            "same_endpoint_group_count": 0, "relation_variant_groups": 0,
            "source_file_variant_groups": 0, "source_location_variant_groups": 0,
            "context_variant_groups": 0, "post_build_graph_type": "Graph",
            "post_build_node_count": 1, "post_build_edge_count": 0,
            "effective_directed": False
        }}
    }}))
else:
    raise SystemExit(9)
""",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher, calls


def test_registry_resolves_only_exact_declared_versions() -> None:
    contract = resolve_graphify_compatibility("0.9.48")
    assert contract.adapter_id == "graphify-0.9.48"
    assert contract.production is True
    assert contract.evidence == EvidenceCapabilities(True, False, False)
    assert contract.structured_query_commands == frozenset()
    assert contract.required_cli_commands == frozenset({
        "extract", "diagnose", "cluster-only", "query", "explain", "path",
        "global", "export", "install",
    })
    assert contract.agent_installs == (
        AgentInstallContract("codex", PurePosixPath(".codex/skills/graphify/SKILL.md")),
        AgentInstallContract("agents", PurePosixPath(".agents/skills/graphify/SKILL.md")),
    )
    assert contract.coverage_reason_codes == frozenset({"not_represented_by_graphify"})
    assert contract.normalization_reason_codes == frozenset({
        "unique_exact_node_alias", "missing_endpoint", "ambiguous_endpoint_alias",
        "dangling_endpoint",
    })
    assert contract.lineage_reason_codes == frozenset()
    assert supported_graphify_versions() == ("0.9.48",)
    assert production_graphify_compatibility() is contract
    for invalid in ("0.9.49", ">=0.9.48", "0.9.48rc1", "latest", ""):
        with pytest.raises(CompatibilityError, match="unsupported Graphify version"):
            resolve_graphify_compatibility(invalid)


def test_registry_is_the_only_production_version_allowlist() -> None:
    forbidden = re.compile(
        r"(?:PINNED_GRAPHIFY_VERSION|_GRAPHIFY_VERSION|REQUIRED_CLI_COMMANDS)\s*="
    )
    offenders = []
    for path in Path("src/project_knowledge").rglob("*.py"):
        if path.name == "compatibility.py":
            continue
        if forbidden.search(path.read_text(encoding="utf-8")):
            offenders.append(path.as_posix())
    assert offenders == []
    assert not Path("config/graphify.lock.yaml").exists()


def test_registry_contract_values_are_immutable_and_complete() -> None:
    contract = resolve_graphify_compatibility("0.9.48")
    assert contract.artifact_schema_versions == (1, 2)
    assert tuple(item.operation for item in contract.commands) == (
        "extract", "diagnose", "cluster", "global-add", "agent-install"
    )
    assert tuple((item.operation, item.required_tokens, item.output_format) for item in contract.commands) == (
        ("extract", ("extract", "--out", "--no-cluster"), "native-json"),
        ("diagnose", ("diagnose", "multigraph", "--graph", "--undirected", "--json"), "diagnostic-json"),
        ("cluster", ("cluster-only", "--graph", "--no-label"), "clustered-artifacts"),
        ("global-add", ("global", "add", "--as"), "none"),
        ("agent-install", ("install", "--platform"), "none"),
    )
    forbidden = frozenset({
        "--force", "--global", "--as", "--postgres", "--google-workspace",
        "--no-gitignore", "--cargo", "--project", "--strict", "--platform",
    })
    assert contract.commands[0].forbidden_flags == forbidden
    assert contract.commands[3].forbidden_flags == forbidden - {"--as"}
    assert contract.commands[4].forbidden_flags == forbidden - {"--platform"}
    assert contract.sensitive_source_suffixes == _SOURCE_SUFFIXES
    assert contract.sensitive_source_suffixes.isdisjoint({
        ".json", ".yaml", ".yml", ".toml", ".ini", ".tfvars"
    })
    assert re.fullmatch(r"[0-9a-f]{64}", contract.native_schema_fingerprint)
    assert re.fullmatch(r"[0-9a-f]{64}", contract.diagnostic_schema_fingerprint)
    assert re.fullmatch(r"[0-9a-f]{64}", contract.clustered_schema_fingerprint)
    assert re.fullmatch(r"[0-9a-f]{64}", contract.compatibility_fixture_digest)
    with pytest.raises(FrozenInstanceError):
        contract.version = "0.9.49"  # type: ignore[misc]
    assert isinstance(contract.commands, tuple)
    assert isinstance(contract.required_cli_commands, frozenset)


def test_registry_backend_and_graph_semantics_are_exact() -> None:
    contract = resolve_graphify_compatibility("0.9.48")
    assert tuple(
        (
            backend.name,
            backend.admitted_environment,
            backend.credential_environment,
            backend.canonical_credential_environment,
            backend.allows_credentialless,
        )
        for backend in contract.backends
    ) == (
        ("claude", ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"), ("ANTHROPIC_API_KEY",), "ANTHROPIC_API_KEY", False),
        ("deepseek", ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"), ("DEEPSEEK_API_KEY",), "DEEPSEEK_API_KEY", False),
        ("gemini", ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_BASE_URL"), ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "GEMINI_API_KEY", False),
        ("kimi", ("MOONSHOT_API_KEY", "KIMI_BASE_URL"), ("MOONSHOT_API_KEY",), "MOONSHOT_API_KEY", False),
        ("ollama", ("OLLAMA_API_KEY", "OLLAMA_BASE_URL", "OLLAMA_HOST"), ("OLLAMA_API_KEY",), "OLLAMA_API_KEY", True),
        ("openai", ("OPENAI_API_KEY", "OPENAI_BASE_URL"), ("OPENAI_API_KEY",), "OPENAI_API_KEY", False),
    )
    semantics = contract.semantics
    assert semantics.directed is False
    assert (semantics.node_id_field, semantics.source_field, semantics.target_field) == (
        "id", "source", "target"
    )
    assert semantics.edge_identity_fields == (
        "source", "target", "relation", "source_file", "source_location", "context"
    )
    assert semantics.parallel_key_fields == ("source", "target")
    assert semantics.parallel_policy == "forbid"
    assert semantics.allowed_self_loop_relations == frozenset()
    assert semantics.accepted_confidence == frozenset({"EXTRACTED", "INFERRED", "AMBIGUOUS"})


def test_compatibility_fixture_is_bound_to_reviewed_literal() -> None:
    contract = resolve_graphify_compatibility("0.9.48")
    payload = resources.files("project_knowledge.compatibility_fixtures").joinpath(
        contract.fixture_resource
    ).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == contract.compatibility_fixture_digest
    document = json.loads(payload)
    assert document["graphify_version"] == "0.9.48"


def test_capture_reader_rejects_oversize_symlink_nonregular_and_malformed_inputs(
    tmp_path: Path,
) -> None:
    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(target)
    directory = tmp_path / "directory.json"
    directory.mkdir()
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")

    for path in (oversized, symlink, directory, malformed):
        with pytest.raises(
            GraphifyContractError, match="fixture artifact is invalid"
        ) as raised:
            _capture_load_json(path)
        assert raised.value.__cause__ is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_capture_reader_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "graph.json"
    os.mkfifo(fifo)
    code = f"""
import runpy
from pathlib import Path
scope = runpy.run_path({str(Path('scripts/capture-graphify-compatibility-fixture').resolve())!r}, run_name='fifo_test')
try:
    scope['_load_json'](Path({str(fifo)!r}))
except scope['GraphifyContractError']:
    print('rejected')
else:
    raise SystemExit(2)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=0.75)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        pytest.fail("capture reader blocked while opening a FIFO")

    assert process.returncode == 0, stderr
    assert stdout.strip() == "rejected"


@pytest.mark.parametrize("absolute", ["/private/tmp/source.py", r"C:\\temp\\source.py"])
def test_capture_rejects_absolute_paths_recursively(absolute: str) -> None:
    with pytest.raises(GraphifyContractError, match="absolute path"):
        _capture_reject_absolute_paths({"outer": [{"source": absolute}]})


def test_capture_output_is_exclusive_unless_replace_is_explicit(tmp_path: Path) -> None:
    output = tmp_path / "fixture.json"
    _capture_write_output(output, b"first", replace=False)

    with pytest.raises(FileExistsError):
        _capture_write_output(output, b"second", replace=False)
    assert output.read_bytes() == b"first"

    _capture_write_output(output, b"second", replace=True)
    assert output.read_bytes() == b"second"


def test_capture_tool_strips_ambient_secrets_and_writes_only_sanitized_output(
    tmp_path: Path,
) -> None:
    launcher, calls = _capture_graphify_launcher(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "fixture.py").write_text("def fixture():\n    return 1\n", encoding="utf-8")
    _write_capture_source_manifest(source)
    output = tmp_path / "fixture.json"
    sentinel = "ambient-capture-secret;$()"
    environment = {
        **os.environ,
        "OPENAI_API_KEY": sentinel,
        "HTTPS_PROXY": sentinel,
        "UNRELATED_TOKEN": sentinel,
    }

    result = subprocess.run(
        [
            str(Path("scripts/capture-graphify-compatibility-fixture").resolve()),
            "--binary", str(launcher),
            "--source", str(source),
            "--output", str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    captured_calls = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert [item["argv"][0] for item in captured_calls] == ["--version", "extract", "diagnose"]
    assert all(
        set(item["env"]) - {"__CF_USER_TEXT_ENCODING"}
        <= {"HOME", "LANG", "LC_ALL", "PATH"}
        for item in captured_calls
    )
    assert all(sentinel not in item["env"].values() for item in captured_calls)
    assert sentinel not in output.read_text(encoding="utf-8")
    assert sentinel not in result.stdout


def test_capture_tool_manifest_excludes_ignored_and_unlisted_files(
    tmp_path: Path,
) -> None:
    """Walking the directory instead would let local junk alter reviewed bytes."""
    launcher, _ = _capture_graphify_launcher(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "fixture.py").write_text("def fixture():\n    return 1\n", encoding="utf-8")
    _write_capture_source_manifest(source)
    baseline = tmp_path / "baseline.json"
    first = _run_capture_tool(launcher, source, baseline)
    assert first.returncode == 0, first.stderr

    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "fixture.pyc").write_bytes(b"ignored bytecode")
    (source / "unreviewed.txt").write_text("unlisted\n", encoding="utf-8")
    contaminated = tmp_path / "contaminated.json"
    second = _run_capture_tool(launcher, source, contaminated)

    assert second.returncode == 0, second.stderr
    assert contaminated.read_bytes() == baseline.read_bytes()
    assert hashlib.sha256(contaminated.read_bytes()).digest() == hashlib.sha256(
        baseline.read_bytes()
    ).digest()


@pytest.mark.parametrize(
    ("case", "entries"),
    [
        ("empty", []),
        ("duplicate", ["fixture.py", "fixture.py"]),
        ("unsorted", ["z.py", "fixture.py"]),
        ("absolute", ["/fixture.py"]),
        ("backslash", [r"nested\fixture.py"]),
        ("nul", ["fixture\x00.py"]),
        ("empty-entry", [""]),
        ("non-string", [7]),
        ("dot", ["."]),
        ("dot-component", ["./fixture.py"]),
        ("dotdot", ["nested/../fixture.py"]),
        ("escaping", ["../fixture.py"]),
    ],
)
def test_capture_tool_rejects_invalid_manifest_entries_without_path_leaks(
    tmp_path: Path, case: str, entries: list[object]
) -> None:
    """Permissive manifest parsing would admit unreviewed or escaping inputs."""
    launcher, _ = _capture_graphify_launcher(tmp_path)
    source = tmp_path / case
    source.mkdir()
    (source / "fixture.py").write_text("safe\n", encoding="utf-8")
    (source / "z.py").write_text("safe\n", encoding="utf-8")
    _write_capture_source_manifest(source, entries)

    result = _run_capture_tool(launcher, source, tmp_path / f"{case}.json")

    assert result.returncode != 0
    assert "GraphifyContractError: Graphify fixture source manifest is invalid" in result.stderr
    assert str(source) not in result.stderr
    assert "The above exception was the direct cause" not in result.stderr


@pytest.mark.parametrize(
    "case", ["missing-manifest", "malformed", "missing-file", "symlink", "non-regular"]
)
def test_capture_tool_fails_closed_for_invalid_manifest_inventory(
    tmp_path: Path, case: str
) -> None:
    """Missing or non-regular reviewed inputs must fail before Graphify runs."""
    launcher, _ = _capture_graphify_launcher(tmp_path)
    source = tmp_path / case
    source.mkdir()
    if case != "missing-manifest":
        if case == "malformed":
            (source / _CAPTURE_SOURCE_MANIFEST).write_text("{", encoding="utf-8")
        elif case == "missing-file":
            _write_capture_source_manifest(source)
        elif case == "symlink":
            target = source / "target.py"
            target.write_text("safe\n", encoding="utf-8")
            (source / "fixture.py").symlink_to(target)
            _write_capture_source_manifest(source)
        else:
            (source / "fixture.py").mkdir()
            _write_capture_source_manifest(source)

    result = _run_capture_tool(launcher, source, tmp_path / f"{case}.json")

    assert result.returncode != 0
    assert "GraphifyContractError: Graphify fixture source manifest is invalid" in result.stderr
    assert str(source) not in result.stderr
    assert "The above exception was the direct cause" not in result.stderr


def test_render_pipeline_argv_is_exact_and_canonical(tmp_path: Path) -> None:
    contract = resolve_graphify_compatibility("0.9.48")
    binary = Path("/bin/graphify")
    source, output, graph = tmp_path / "source", tmp_path / "out", tmp_path / "graph.json"
    code = render_graphify_argv(contract, "extract", binary=binary, source=source, output=output, code_only=True)
    semantic = render_graphify_argv(contract, "extract", binary=binary, source=source, output=output, backend="openai", model="gpt", deep=True)
    diagnose = render_graphify_argv(contract, "diagnose", binary=binary, source=source, output=output, graph=graph)
    cluster = render_graphify_argv(contract, "cluster", binary=binary, source=source, output=output, graph=graph, track_html=False)
    assert code.argv == (str(binary), "extract", str(source), "--out", str(output), "--no-cluster", "--code-only")
    assert code.canonical_argv == ("<graphify>", "extract", "<staged-root>", "--out", "<raw-output-root>", "--no-cluster", "--code-only")
    assert semantic.argv == (str(binary), "extract", str(source), "--out", str(output), "--no-cluster", "--backend", "openai", "--model", "gpt", "--mode", "deep")
    assert diagnose.argv == (str(binary), "diagnose", "multigraph", "--graph", str(graph), "--undirected", "--json")
    assert cluster.argv == (str(binary), "cluster-only", str(source), "--graph", str(graph), "--no-label", "--no-viz")


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({}, "choose exactly one extraction mode"),
        ({"code_only": True, "backend": "openai", "model": "gpt"}, "choose exactly one extraction mode"),
        ({"code_only": True, "deep": True}, "deep mode requires a semantic backend"),
        ({"backend": "openai", "model": ""}, "semantic_model_required"),
        ({"backend": "unknown", "model": "m"}, "semantic backend is unsupported"),
    ],
)
def test_render_extract_rejects_unexpressible_modes(tmp_path: Path, kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(CompatibilityError, match=message):
        render_graphify_argv(
            resolve_graphify_compatibility("0.9.48"), "extract",
            binary=Path("/bin/graphify"), source=tmp_path / "src", output=tmp_path / "out",
            **kwargs,  # type: ignore[arg-type]
        )


def test_global_add_and_agent_install_are_closed_renderers() -> None:
    contract = resolve_graphify_compatibility("0.9.48")
    binary, graph = Path("/bin/graphify"), Path("/tmp/graph.json")
    key = "atlasweaver/00000000-0000-4000-8000-000000000000"
    command = render_graphify_global_add(contract, binary=binary, graph=graph, registry_key=key)
    assert command.argv == (str(binary), "global", "add", str(graph), "--as", key)
    assert command.canonical_argv == ("<graphify>", "global", "add", "<registry-graph>", "--as", "<registry-key>")
    assert len(command.argv) == 6
    for platform in ("codex", "agents"):
        install = render_graphify_agent_install(contract, binary=binary, platform=platform)  # type: ignore[arg-type]
        assert install.argv == (str(binary), "install", "--platform", platform)
        assert install.canonical_argv == ("<graphify>", "install", "--platform", platform)


@pytest.mark.parametrize(
    "key",
    ["", "demo", "atlasweaver//00000000-0000-4000-8000-000000000000", "atlasweaver/00000000-0000-1000-8000-000000000000", "atlasweaver/00000000-0000-4000-8000-000000000000 ", "ATLASWEAVER/00000000-0000-4000-8000-000000000000", "atlasweaver/AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"],
)
def test_global_add_rejects_every_noncanonical_registry_key(key: str) -> None:
    with pytest.raises(CompatibilityError, match="registry key is invalid"):
        render_graphify_global_add(resolve_graphify_compatibility("0.9.48"), binary=Path("g"), graph=Path("x"), registry_key=key)


@pytest.mark.parametrize("platform", ["", "Codex", "other", None, 1])
def test_agent_install_rejects_unsupported_runtime_platform(platform: object) -> None:
    with pytest.raises(CompatibilityError, match="agent install platform is unsupported"):
        render_graphify_agent_install(resolve_graphify_compatibility("0.9.48"), binary=Path("g"), platform=platform)  # type: ignore[arg-type]


def test_backend_environment_is_allowlisted_without_mutating_ambient() -> None:
    contract = resolve_graphify_compatibility("0.9.48")
    ambient = {
        "HOME": "/private/home", "LANG": "C", "LC_ALL": "C", "PATH": "/bin",
        "OPENAI_API_KEY": "secret", "OPENAI_BASE_URL": "https://example.invalid",
        "HTTPS_PROXY": "leak", "EMPTY": "",
    }
    original = dict(ambient)
    assert admitted_graphify_environment(contract, "openai", ambient) == {
        "HOME": "/private/home", "LANG": "C", "LC_ALL": "C", "PATH": "/bin",
        "OPENAI_API_KEY": "secret", "OPENAI_BASE_URL": "https://example.invalid",
    }
    assert ambient == original
    assert validate_semantic_backend(contract, "openai", ambient).name == "openai"
    with pytest.raises(CompatibilityError, match="semantic_backend_required"):
        validate_semantic_backend(contract, "openai", {"OPENAI_API_KEY": ""})
    assert validate_semantic_backend(contract, "ollama", {}).allows_credentialless is True


def test_backend_credential_binding_is_canonical_and_opaque() -> None:
    contract = resolve_graphify_compatibility("0.9.48")
    names = {
        "claude": "ANTHROPIC_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
        "gemini": "GEMINI_API_KEY", "kimi": "MOONSHOT_API_KEY",
        "ollama": "OLLAMA_API_KEY", "openai": "OPENAI_API_KEY",
    }
    sentinel = "  value;$() *\n"
    for backend, name in names.items():
        assert bind_semantic_backend_credential(contract, backend, sentinel) == {name: sentinel}
    assert bind_semantic_backend_credential(contract, "ollama", None) == {}
    for backend in names.keys() - {"ollama"}:
        with pytest.raises(CompatibilityError, match="semantic_backend_required") as raised:
            bind_semantic_backend_credential(contract, backend, None)
        assert sentinel not in str(raised.value)
    for empty in ("", None):
        with pytest.raises(CompatibilityError, match="semantic_backend_required"):
            bind_semantic_backend_credential(contract, "openai", empty)
    with pytest.raises(CompatibilityError, match="semantic backend is unsupported"):
        bind_semantic_backend_credential(contract, "unknown", "secret")


def test_resolve_graphify_executable_freezes_canonical_identity(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path / "graphify")
    executable = resolve_graphify_executable(test_override=launcher)
    metadata = launcher.stat()
    assert executable.path == launcher.resolve()
    assert executable.st_dev == metadata.st_dev
    assert executable.st_ino == metadata.st_ino
    assert executable.launcher_sha256 == hashlib.sha256(launcher.read_bytes()).hexdigest()


@pytest.mark.parametrize("mutation", ["replace", "bytes", "symlink", "mode", "truncate", "oversize", "missing"])
def test_executable_mutation_fails_closed_before_spawn(tmp_path: Path, mutation: str) -> None:
    launcher = _launcher(tmp_path / "graphify")
    executable = resolve_graphify_executable(test_override=launcher)
    if mutation == "replace":
        replacement = _launcher(tmp_path / "replacement", b"#!/bin/sh\nexit 1\n")
        os.replace(replacement, launcher)
    elif mutation == "bytes":
        launcher.write_bytes(b"#!/bin/sh\nexit 2\n")
    elif mutation == "symlink":
        target = _launcher(tmp_path / "target")
        launcher.unlink()
        launcher.symlink_to(target)
    elif mutation == "mode":
        launcher.chmod(stat.S_IRUSR | stat.S_IWUSR)
    elif mutation == "truncate":
        launcher.write_bytes(b"")
    elif mutation == "oversize":
        with launcher.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
    else:
        launcher.unlink()
    runner = RecordingRunner()
    for operation in (
        lambda: revalidate_graphify_executable(executable),
        lambda: run_checked(runner, executable, (str(executable.path), "--version")),
    ):
        with pytest.raises(GraphifyContractError) as raised:
            operation()
        assert str(raised.value) == "graphify_executable_changed"
    assert runner.calls == []


def test_run_checked_revalidates_between_commands(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path / "graphify")
    executable = resolve_graphify_executable(test_override=launcher)

    class MutatingRunner(RecordingRunner):
        def run(self, argv, **options):  # type: ignore[no-untyped-def]
            result = super().run(argv, **options)
            launcher.write_bytes(b"#!/bin/sh\nexit 3\n")
            return result

    runner = MutatingRunner()
    run_checked(runner, executable, (str(executable.path), "--version"))
    with pytest.raises(GraphifyContractError) as raised:
        run_checked(runner, executable, (str(executable.path), "--help"))
    assert str(raised.value) == "graphify_executable_changed"
    assert len(runner.calls) == 1


def test_run_checked_rejects_argv_zero_before_revalidation_or_spawn(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path / "graphify")
    executable = resolve_graphify_executable(test_override=launcher)
    launcher.unlink()
    runner = RecordingRunner()
    with pytest.raises(GraphifyContractError) as raised:
        run_checked(runner, executable, ("graphify", "--version"))
    assert str(raised.value) == "graphify_executable_changed"
    assert runner.calls == []


def test_capability_surface_rejects_any_contract_drift() -> None:
    contract = resolve_graphify_compatibility("0.9.48")
    commands = (
        ("<graphify>", "extract", "<staged-root>", "--out", "<raw-output-root>", "--no-cluster", "--code-only"),
        ("<graphify>", "extract", "<staged-root>", "--out", "<raw-output-root>", "--no-cluster", "--backend", "ollama", "--model", "atlasweaver-capability-probe", "--mode", "deep"),
        ("<graphify>", "diagnose", "multigraph", "--graph", "<native-graph>", "--undirected", "--json"),
        ("<graphify>", "cluster-only", "<cluster-workspace>", "--graph", "<cluster-input>", "--no-label", "--no-viz"),
        ("<graphify>", "global", "add", "<registry-graph>", "--as", "<registry-key>"),
        ("<graphify>", "install", "--platform", "codex"),
        ("<graphify>", "install", "--platform", "agents"),
    )
    probe = CapabilityProbe(
        installed_version="0.9.48",
        commands=contract.required_cli_commands,
        canonical_commands=commands,
        native_schema_fingerprint=contract.native_schema_fingerprint,
        diagnostic_schema_fingerprint=contract.diagnostic_schema_fingerprint,
        clustered_schema_fingerprint=contract.clustered_schema_fingerprint,
        global_add_verified=True,
        agent_install_platforms=frozenset({"codex", "agents"}),
        digest="0" * 64,
    )
    assert_capability_surface(contract, probe)
    for changed in (
        CapabilityProbe(**{**probe.__dict__, "installed_version": "0.9.49"}),
        CapabilityProbe(**{**probe.__dict__, "commands": frozenset()}),
        CapabilityProbe(**{**probe.__dict__, "canonical_commands": commands[::-1]}),
        CapabilityProbe(**{**probe.__dict__, "native_schema_fingerprint": "1" * 64}),
        CapabilityProbe(**{**probe.__dict__, "global_add_verified": False}),
        CapabilityProbe(**{**probe.__dict__, "agent_install_platforms": frozenset({"codex"})}),
    ):
        with pytest.raises(CompatibilityError, match="Graphify capability surface mismatch"):
            assert_capability_surface(contract, changed)


@pytest.mark.parametrize("model", ["gpt-5", "vendor/model-1", "family:model_2"])
def test_public_model_identifier_round_trips(model: str) -> None:
    assert validate_public_model_identifier(model) == model


@pytest.mark.parametrize(
    "model",
    [None, "", "-flag", "bad model", "bad\nmodel", "é", "x" * 129,
     "ghp_" + "a" * 32, "../../model"],
)
def test_public_model_identifier_fails_closed_without_echo(model: object) -> None:
    with pytest.raises(CompatibilityError) as raised:
        validate_public_model_identifier(model)
    assert str(raised.value) == "semantic_model_required"
    if isinstance(model, str) and model:
        assert model not in repr(raised.value)
