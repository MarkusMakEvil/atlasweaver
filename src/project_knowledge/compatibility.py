"""Immutable, sole-source Graphify compatibility contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Literal
from uuid import UUID

class CompatibilityError(ValueError):
    """Raised when a Graphify version or capability is outside the registry."""


@dataclass(frozen=True)
class RenderedCommand:
    operation: Literal["extract", "diagnose", "cluster", "global-add", "agent-install"]
    argv: tuple[str, ...]
    canonical_argv: tuple[str, ...]


@dataclass(frozen=True)
class CommandContract:
    operation: Literal["extract", "diagnose", "cluster", "global-add", "agent-install"]
    required_tokens: tuple[str, ...]
    forbidden_flags: frozenset[str]
    output_format: Literal["native-json", "diagnostic-json", "clustered-artifacts", "none"]


@dataclass(frozen=True)
class BackendContract:
    name: str
    admitted_environment: tuple[str, ...]
    credential_environment: tuple[str, ...]
    canonical_credential_environment: str
    allows_credentialless: bool = False


@dataclass(frozen=True)
class AgentInstallContract:
    platform: Literal["codex", "agents"]
    home_relative_skill: PurePosixPath


@dataclass(frozen=True)
class GraphSemantics:
    directed: bool
    node_id_field: str
    source_field: str
    target_field: str
    edge_identity_fields: tuple[str, ...]
    parallel_key_fields: tuple[str, ...]
    parallel_policy: Literal["forbid", "distinct-relation", "allow"]
    allowed_self_loop_relations: frozenset[str]
    accepted_confidence: frozenset[str]


@dataclass(frozen=True)
class EvidenceCapabilities:
    observed_post_dedup: bool
    pre_dedup_occurrences: bool
    total_transform_lineage: bool


@dataclass(frozen=True)
class CapabilityProbe:
    installed_version: str
    commands: frozenset[str]
    canonical_commands: tuple[tuple[str, ...], ...]
    native_schema_fingerprint: str
    diagnostic_schema_fingerprint: str
    clustered_schema_fingerprint: str
    global_add_verified: bool
    agent_install_platforms: frozenset[str]
    digest: str


@dataclass(frozen=True)
class GraphifyCompatibility:
    version: str
    adapter_id: str
    artifact_schema_versions: tuple[int, ...]
    required_cli_commands: frozenset[str]
    commands: tuple[CommandContract, ...]
    backends: tuple[BackendContract, ...]
    agent_installs: tuple[AgentInstallContract, ...]
    native_schema_fingerprint: str
    diagnostic_schema_fingerprint: str
    clustered_schema_fingerprint: str
    fixture_resource: str
    compatibility_fixture_digest: str
    evidence: EvidenceCapabilities
    semantics: GraphSemantics
    sensitive_source_suffixes: frozenset[str]
    coverage_reason_codes: frozenset[str]
    normalization_reason_codes: frozenset[str]
    lineage_reason_codes: frozenset[str]
    structured_query_commands: frozenset[str]
    production: bool


_BASE_ENV = ("HOME", "LANG", "LC_ALL", "PATH")
_FORBIDDEN = frozenset({
    "--force", "--global", "--as", "--postgres", "--google-workspace",
    "--no-gitignore", "--cargo", "--project", "--strict", "--platform",
})
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PUBLIC_MODEL_IDENTIFIER = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?"
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]*)?\Z"
)

_GRAPHIFY_0_9_48_NATIVE_SCHEMA = {
    "artifact": "extract --no-cluster",
    "keys": ("edges", "hyperedges", "input_tokens", "nodes", "output_tokens"),
    "node": {"required": ("id",)},
    "edge": {"required": ("relation", "source", "target")},
}
_DIAGNOSTIC_SUMMARY_FIELDS = (
    "node_count", "raw_edge_count", "missing_endpoint_edges",
    "dangling_endpoint_edges", "self_loop_edges", "exact_duplicate_edges",
    "undirected_unique_endpoint_pairs", "undirected_same_endpoint_collapsed_edges",
    "same_endpoint_group_count", "relation_variant_groups", "source_file_variant_groups",
    "source_location_variant_groups", "context_variant_groups", "post_build_graph_type",
    "post_build_node_count", "post_build_edge_count", "effective_directed",
)
_GRAPHIFY_0_9_48_DIAGNOSTIC_SCHEMA = {
    "artifact": "diagnose multigraph --undirected --json",
    "keys": ("schema_version", "summary"),
    "summary": _DIAGNOSTIC_SUMMARY_FIELDS,
    "effective_directed": False,
}
_GRAPHIFY_0_9_48_CLUSTERED_SCHEMA = {
    "artifact": "cluster-only --no-label",
    "keys": ("directed", "links", "multigraph", "nodes"),
    "directed": False,
    "multigraph": False,
}


def _sha256_canonical(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


_GRAPHIFY_0_9_48_SOURCE_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".cjs", ".mjs", ".ts", ".tsx",
    ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".c", ".h", ".cc",
    ".cpp", ".cxx", ".hpp", ".cu", ".cuh", ".rb", ".swift", ".php",
    ".scala", ".lua", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1",
    ".ex", ".exs", ".m", ".mm", ".ml", ".mli", ".jl", ".r", ".dart",
    ".zig", ".v", ".vhd", ".vhdl", ".sql", ".tf", ".hcl", ".f", ".f90",
    ".f95", ".pas", ".apex", ".lisp", ".clj", ".cljs",
})

# Reviewed separately from the packaged fixture. Updated only after deterministic
# fixture capture and an explicit byte-for-byte comparison of two independent runs.
_GRAPHIFY_0_9_48_FIXTURE_SHA256 = "8b02c146e5c352adbbcefbda8496e0c5effa85d6353d073073a5fb56f7bf0c28"
_GRAPHIFY_0_9_51_FIXTURE_SHA256 = "31769f6a5b467720112de5d20e8b251923acd12f1fd537544ac22dc2c4f8036d"

_GRAPHIFY_0_9_48 = GraphifyCompatibility(
    version="0.9.48",
    adapter_id="graphify-0.9.48",
    artifact_schema_versions=(1, 2),
    required_cli_commands=frozenset({
        "extract", "diagnose", "cluster-only", "query", "explain", "path",
        "global", "export", "install",
    }),
    commands=(
        CommandContract("extract", ("extract", "--out", "--no-cluster"), _FORBIDDEN, "native-json"),
        CommandContract("diagnose", ("diagnose", "multigraph", "--graph", "--undirected", "--json"), _FORBIDDEN, "diagnostic-json"),
        CommandContract("cluster", ("cluster-only", "--graph", "--no-label"), _FORBIDDEN, "clustered-artifacts"),
        CommandContract("global-add", ("global", "add", "--as"), _FORBIDDEN - {"--as"}, "none"),
        CommandContract("agent-install", ("install", "--platform"), _FORBIDDEN - {"--platform"}, "none"),
    ),
    backends=(
        BackendContract("claude", ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"), ("ANTHROPIC_API_KEY",), "ANTHROPIC_API_KEY"),
        BackendContract("deepseek", ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"), ("DEEPSEEK_API_KEY",), "DEEPSEEK_API_KEY"),
        BackendContract("gemini", ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_BASE_URL"), ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "GEMINI_API_KEY"),
        BackendContract("kimi", ("MOONSHOT_API_KEY", "KIMI_BASE_URL"), ("MOONSHOT_API_KEY",), "MOONSHOT_API_KEY"),
        BackendContract("ollama", ("OLLAMA_API_KEY", "OLLAMA_BASE_URL", "OLLAMA_HOST"), ("OLLAMA_API_KEY",), "OLLAMA_API_KEY", True),
        BackendContract("openai", ("OPENAI_API_KEY", "OPENAI_BASE_URL"), ("OPENAI_API_KEY",), "OPENAI_API_KEY"),
    ),
    agent_installs=(
        AgentInstallContract("codex", PurePosixPath(".codex/skills/graphify/SKILL.md")),
        AgentInstallContract("agents", PurePosixPath(".agents/skills/graphify/SKILL.md")),
    ),
    native_schema_fingerprint=_sha256_canonical(_GRAPHIFY_0_9_48_NATIVE_SCHEMA),
    diagnostic_schema_fingerprint=_sha256_canonical(_GRAPHIFY_0_9_48_DIAGNOSTIC_SCHEMA),
    clustered_schema_fingerprint=_sha256_canonical(_GRAPHIFY_0_9_48_CLUSTERED_SCHEMA),
    fixture_resource="graphify_0_9_48.json",
    compatibility_fixture_digest=_GRAPHIFY_0_9_48_FIXTURE_SHA256,
    evidence=EvidenceCapabilities(True, False, False),
    semantics=GraphSemantics(
        directed=False,
        node_id_field="id",
        source_field="source",
        target_field="target",
        edge_identity_fields=("source", "target", "relation", "source_file", "source_location", "context"),
        parallel_key_fields=("source", "target"),
        parallel_policy="forbid",
        allowed_self_loop_relations=frozenset(),
        accepted_confidence=frozenset({"EXTRACTED", "INFERRED", "AMBIGUOUS"}),
    ),
    sensitive_source_suffixes=_GRAPHIFY_0_9_48_SOURCE_SUFFIXES,
    coverage_reason_codes=frozenset({"not_represented_by_graphify"}),
    normalization_reason_codes=frozenset({
        "unique_exact_node_alias", "missing_endpoint", "ambiguous_endpoint_alias",
        "dangling_endpoint",
    }),
    lineage_reason_codes=frozenset(),
    structured_query_commands=frozenset(),
    production=False,
)

_GRAPHIFY_0_9_51 = replace(
    _GRAPHIFY_0_9_48,
    version="0.9.51",
    adapter_id="graphify-0.9.51",
    fixture_resource="graphify_0_9_51.json",
    compatibility_fixture_digest=_GRAPHIFY_0_9_51_FIXTURE_SHA256,
    production=True,
)

_REGISTRY: Mapping[str, GraphifyCompatibility] = MappingProxyType(
    {"0.9.48": _GRAPHIFY_0_9_48, "0.9.51": _GRAPHIFY_0_9_51}
)


def _validate_registry() -> None:
    production = tuple(item for item in _REGISTRY.values() if item.production)
    if len(production) != 1:
        raise RuntimeError("compatibility registry must have exactly one production entry")
    for version, contract in _REGISTRY.items():
        if version != contract.version or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise RuntimeError("compatibility registry requires exact version keys")
        if len({item.operation for item in contract.commands}) != len(contract.commands):
            raise RuntimeError("compatibility registry contains duplicate operations")
        if len({item.name for item in contract.backends}) != len(contract.backends):
            raise RuntimeError("compatibility registry contains duplicate backends")
        for digest in (
            contract.native_schema_fingerprint, contract.diagnostic_schema_fingerprint,
            contract.clustered_schema_fingerprint, contract.compatibility_fixture_digest,
        ):
            if _HEX_DIGEST.fullmatch(digest) is None:
                raise RuntimeError("compatibility registry digest is invalid")
        for target in contract.agent_installs:
            path = target.home_relative_skill
            expected_root = f".{target.platform}"
            if path.is_absolute() or ".." in path.parts or path.parts[0] != expected_root:
                raise RuntimeError("agent install target is not confined")
        for backend in contract.backends:
            canonical = backend.canonical_credential_environment
            if canonical not in backend.admitted_environment or canonical not in backend.credential_environment:
                raise RuntimeError("canonical backend credential is not admitted")


_validate_registry()


def resolve_graphify_compatibility(version: str) -> GraphifyCompatibility:
    try:
        return _REGISTRY[version]
    except (KeyError, TypeError) as error:
        raise CompatibilityError(f"unsupported Graphify version: {version}") from error


def production_graphify_compatibility() -> GraphifyCompatibility:
    return next(item for item in _REGISTRY.values() if item.production)


def supported_graphify_versions() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def _command(contract: GraphifyCompatibility, operation: str) -> CommandContract:
    item = next((candidate for candidate in contract.commands if candidate.operation == operation), None)
    if item is None:
        raise CompatibilityError("Graphify operation is unsupported")
    return item


def _backend(contract: GraphifyCompatibility, name: object) -> BackendContract:
    if not isinstance(name, str):
        raise CompatibilityError("semantic backend is unsupported")
    item = next((candidate for candidate in contract.backends if candidate.name == name), None)
    if item is None:
        raise CompatibilityError("semantic backend is unsupported")
    return item


def _require_backend_and_model(
    contract: GraphifyCompatibility, backend: str | None, model: str | None
) -> BackendContract:
    item = _backend(contract, backend)
    validate_public_model_identifier(model)
    return item


def validate_public_model_identifier(model: object) -> str:
    """Validate the public, non-secret model identifier used in argv/evidence."""
    from .secrets_scan import has_secret_shape

    if type(model) is not str or not 1 <= len(model) <= 128:
        raise CompatibilityError("semantic_model_required")
    try:
        payload = model.encode("ascii")
    except UnicodeEncodeError:
        raise CompatibilityError("semantic_model_required") from None
    if (
        _PUBLIC_MODEL_IDENTIFIER.fullmatch(model) is None
        or has_secret_shape(payload)
    ):
        raise CompatibilityError("semantic_model_required")
    return model


def render_graphify_argv(
    contract: GraphifyCompatibility,
    operation: Literal["extract", "diagnose", "cluster"],
    *,
    binary: Path,
    source: Path,
    output: Path,
    graph: Path | None = None,
    backend: str | None = None,
    model: str | None = None,
    code_only: bool = False,
    deep: bool = False,
    track_html: bool = False,
) -> RenderedCommand:
    _command(contract, operation)
    if operation == "extract":
        if code_only == (backend is not None):
            raise CompatibilityError("choose exactly one extraction mode")
        if deep and code_only:
            raise CompatibilityError("deep mode requires a semantic backend")
        if graph is not None or track_html:
            raise CompatibilityError("extract received an unsupported option")
        actual = [str(binary), "extract", str(source), "--out", str(output), "--no-cluster"]
        canonical = ["<graphify>", "extract", "<staged-root>", "--out", "<raw-output-root>", "--no-cluster"]
        if code_only:
            if model is not None:
                raise CompatibilityError("extract received an unsupported option")
            actual.append("--code-only")
            canonical.append("--code-only")
        else:
            _require_backend_and_model(contract, backend, model)
            actual.extend(("--backend", backend, "--model", model))
            canonical.extend(("--backend", backend, "--model", model))
            if deep:
                actual.extend(("--mode", "deep"))
                canonical.extend(("--mode", "deep"))
    elif operation == "diagnose":
        if graph is None:
            raise CompatibilityError("diagnose requires a native graph")
        if backend is not None or model is not None or code_only or deep or track_html:
            raise CompatibilityError("diagnose received an unsupported option")
        actual = [str(binary), "diagnose", "multigraph", "--graph", str(graph), "--undirected", "--json"]
        canonical = ["<graphify>", "diagnose", "multigraph", "--graph", "<native-graph>", "--undirected", "--json"]
    elif operation == "cluster":
        if graph is None:
            raise CompatibilityError("cluster requires a normalized graph")
        if backend is not None or model is not None or code_only or deep:
            raise CompatibilityError("cluster received an unsupported option")
        actual = [str(binary), "cluster-only", str(source), "--graph", str(graph), "--no-label"]
        canonical = ["<graphify>", "cluster-only", "<cluster-workspace>", "--graph", "<cluster-input>", "--no-label"]
        if not track_html:
            actual.append("--no-viz")
            canonical.append("--no-viz")
    else:
        raise CompatibilityError("Graphify operation is unsupported")
    return RenderedCommand(operation, tuple(actual), tuple(canonical))


def render_graphify_global_add(
    contract: GraphifyCompatibility, *, binary: Path, graph: Path, registry_key: str
) -> RenderedCommand:
    _command(contract, "global-add")
    pattern = r"atlasweaver/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    if not isinstance(registry_key, str) or re.fullmatch(pattern, registry_key) is None:
        raise CompatibilityError("registry key is invalid")
    suffix = registry_key.removeprefix("atlasweaver/")
    parsed = UUID(suffix)
    if parsed.version != 4 or str(parsed) != suffix:
        raise CompatibilityError("registry key is invalid")
    return RenderedCommand(
        "global-add",
        (str(binary), "global", "add", str(graph), "--as", registry_key),
        ("<graphify>", "global", "add", "<registry-graph>", "--as", "<registry-key>"),
    )


def render_graphify_agent_install(
    contract: GraphifyCompatibility, *, binary: Path, platform: Literal["codex", "agents"]
) -> RenderedCommand:
    _command(contract, "agent-install")
    target = next((item for item in contract.agent_installs if item.platform == platform), None)
    if target is None:
        raise CompatibilityError("agent install platform is unsupported")
    return RenderedCommand(
        "agent-install",
        (str(binary), "install", "--platform", platform),
        ("<graphify>", "install", "--platform", platform),
    )


def validate_semantic_backend(
    contract: GraphifyCompatibility, backend: str, ambient: Mapping[str, str]
) -> BackendContract:
    item = _backend(contract, backend)
    if not item.allows_credentialless and not any(
        isinstance(ambient.get(name), str) and bool(ambient[name])
        for name in item.credential_environment
    ):
        raise CompatibilityError("semantic_backend_required")
    return item


def admitted_graphify_environment(
    contract: GraphifyCompatibility,
    backend: str | None,
    ambient: Mapping[str, str],
) -> dict[str, str]:
    names = list(_BASE_ENV)
    if backend is not None:
        names.extend(_backend(contract, backend).admitted_environment)
    return {
        name: value
        for name in names
        if isinstance((value := ambient.get(name)), str) and value
    }


def bind_semantic_backend_credential(
    contract: GraphifyCompatibility, backend: str, credential: str | None
) -> dict[str, str]:
    item = _backend(contract, backend)
    canonical = item.canonical_credential_environment
    if canonical not in item.admitted_environment or canonical not in item.credential_environment:
        raise CompatibilityError("semantic backend registry is invalid")
    if isinstance(credential, str) and credential:
        return {canonical: credential}
    if credential is None and item.allows_credentialless:
        return {}
    raise CompatibilityError("semantic_backend_required")


def _expected_canonical_commands(contract: GraphifyCompatibility) -> tuple[tuple[str, ...], ...]:
    binary = Path("/graphify")
    source, output, graph = Path("/source"), Path("/output"), Path("/graph.json")
    key = "atlasweaver/00000000-0000-4000-8000-000000000000"
    return (
        render_graphify_argv(contract, "extract", binary=binary, source=source, output=output, code_only=True).canonical_argv,
        render_graphify_argv(contract, "extract", binary=binary, source=source, output=output, backend="ollama", model="atlasweaver-capability-probe", deep=True).canonical_argv,
        render_graphify_argv(contract, "diagnose", binary=binary, source=source, output=output, graph=graph).canonical_argv,
        render_graphify_argv(contract, "cluster", binary=binary, source=source, output=output, graph=graph).canonical_argv,
        render_graphify_global_add(contract, binary=binary, graph=graph, registry_key=key).canonical_argv,
        render_graphify_agent_install(contract, binary=binary, platform="codex").canonical_argv,
        render_graphify_agent_install(contract, binary=binary, platform="agents").canonical_argv,
    )


def assert_capability_surface(contract: GraphifyCompatibility, probe: CapabilityProbe) -> None:
    expected_platforms = frozenset(item.platform for item in contract.agent_installs)
    if (
        probe.installed_version != contract.version
        or not contract.required_cli_commands <= probe.commands
        or probe.canonical_commands != _expected_canonical_commands(contract)
        or probe.native_schema_fingerprint != contract.native_schema_fingerprint
        or probe.diagnostic_schema_fingerprint != contract.diagnostic_schema_fingerprint
        or probe.clustered_schema_fingerprint != contract.clustered_schema_fingerprint
        or probe.global_add_verified is not True
        or probe.agent_install_platforms != expected_platforms
        or _HEX_DIGEST.fullmatch(probe.digest) is None
    ):
        raise CompatibilityError("Graphify capability surface mismatch")


def _validate_native_document(document: object) -> str:
    if not isinstance(document, Mapping) or set(document) != {"nodes", "edges", "hyperedges", "input_tokens", "output_tokens"}:
        raise CompatibilityError("Graphify native schema mismatch")
    nodes, edges = document["nodes"], document["edges"]
    if not isinstance(nodes, list) or not isinstance(edges, list) or not isinstance(document["hyperedges"], list):
        raise CompatibilityError("Graphify native schema mismatch")
    if any(
        not isinstance(node, Mapping)
        or not isinstance(node.get("id"), str)
        or not node["id"]
        for node in nodes
    ):
        raise CompatibilityError("Graphify native schema mismatch")
    if any(
        not isinstance(edge, Mapping)
        or any(not isinstance(edge.get(name), str) or not edge[name] for name in ("source", "target", "relation"))
        for edge in edges
    ):
        raise CompatibilityError("Graphify native schema mismatch")
    if isinstance(document["input_tokens"], bool) or not isinstance(document["input_tokens"], int):
        raise CompatibilityError("Graphify native schema mismatch")
    if isinstance(document["output_tokens"], bool) or not isinstance(document["output_tokens"], int):
        raise CompatibilityError("Graphify native schema mismatch")
    return _sha256_canonical(_GRAPHIFY_0_9_48_NATIVE_SCHEMA)


def _validate_diagnostic_document(document: object) -> str:
    if not isinstance(document, Mapping) or set(document) != {"schema_version", "summary"}:
        raise CompatibilityError("Graphify diagnostic schema mismatch")
    summary = document["summary"]
    if not isinstance(summary, Mapping) or set(summary) != set(_DIAGNOSTIC_SUMMARY_FIELDS):
        raise CompatibilityError("Graphify diagnostic schema mismatch")
    if summary.get("effective_directed") is not False:
        raise CompatibilityError("Graphify diagnostic schema mismatch")
    count_fields = set(_DIAGNOSTIC_SUMMARY_FIELDS) - {
        "post_build_graph_type", "effective_directed"
    }
    if (
        document["schema_version"] != 1
        or not isinstance(summary.get("post_build_graph_type"), str)
        or not summary["post_build_graph_type"]
        or any(
            isinstance(summary[name], bool)
            or not isinstance(summary[name], int)
            or summary[name] < 0
            for name in count_fields
        )
    ):
        raise CompatibilityError("Graphify diagnostic schema mismatch")
    return _sha256_canonical(_GRAPHIFY_0_9_48_DIAGNOSTIC_SCHEMA)


def _validate_clustered_document(document: object) -> str:
    required = {"directed", "multigraph", "nodes", "links"}
    allowed = required | {"graph", "hyperedges"}
    if (
        not isinstance(document, Mapping)
        or not required <= set(document)
        or not set(document) <= allowed
    ):
        raise CompatibilityError("Graphify clustered schema mismatch")
    if document["directed"] is not False or document["multigraph"] is not False:
        raise CompatibilityError("Graphify clustered schema mismatch")
    if not isinstance(document["nodes"], list) or not isinstance(document["links"], list):
        raise CompatibilityError("Graphify clustered schema mismatch")
    return _sha256_canonical(_GRAPHIFY_0_9_48_CLUSTERED_SCHEMA)
