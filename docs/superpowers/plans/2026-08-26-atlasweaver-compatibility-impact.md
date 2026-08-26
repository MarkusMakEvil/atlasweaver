# AtlasWeaver Compatibility and Impact Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a sole-source Graphify compatibility registry, versioned 0.9.48 adapter, immutable evidence chain, deterministic structural validation, and compatibility CI while keeping impact claims honestly navigation-only until complete lineage exists.

**Architecture:** A frozen registry describes every admitted Graphify command, backend environment, graph invariant, native schema, and evidence capability. The 0.9.48 adapter captures the official post-dedup artifact, sanitizes official diagnostics, quarantines unresolved endpoints before clustering, assigns deterministic final-edge IDs, and hands version-neutral evidence to the existing candidate validator. Core lifecycle and query code consume these primitives in the subsequent core plan; this plan does not create `lifecycle.py`, `coverage.py`, `registry.py`, or `queries.py`.

**Tech Stack:** Python 3.10+, frozen dataclasses and protocols, strict JSON, SHA-256 canonical digests, PyYAML 6, pytest 8/9, Graphify `graphifyy==0.9.48`, `packaging==25.0`, uv `0.8.14`, GitHub Actions pinned by commit SHA.

**Spec:** `docs/superpowers/specs/2026-08-26-atlasweaver-compatibility-impact-design.md`

## Global Constraints

- Graphify support is keyed by an exact PEP 440 version; ranges, aliases, prereleases, and undeclared versions are rejected.
- The compatibility registry is the only accepted-version/capability allowlist used by manifest loading, extraction, adaptation, validation, health, query admission, CI, and tests.
- Runtime code may invoke only official Graphify CLI commands; internal imports and monkeypatch capture hooks are forbidden.
- Graphify 0.9.48 uses `extract --no-cluster`, `diagnose multigraph --undirected --json`, then `cluster-only --no-label`; `--no-viz` is present exactly when HTML is disabled.
- Never forward `--force`, `--global`, `--as`, `--postgres`, `--google-workspace`, `--no-gitignore`, `--cargo`, `--project`, `--strict`, or arbitrary user-supplied native flags. The only `--as` use is the registry-owned fixed `global add` renderer with a validated namespaced key; the only `--platform` use is the registry-owned agent-install renderer for the exact `codex` and `agents` targets.
- Semantic extraction admits only `gemini`, `kimi`, `claude`, `openai`, `deepseek`, and `ollama`; code-only extraction must be explicit.
- Graphify receives only the base process environment names `HOME`, `LANG`, `LC_ALL`, and `PATH` plus the selected backend's registry-declared names; secret values never enter a digest, error, report, fixture, or bundle.
- Reusable workflows/fleet accept one opaque semantic credential, bind it only to the selected backend's registry-declared canonical credential environment name, and never put it in argv, canonical argv, evidence, logs, or persisted environment-name lists. Local ambient-name admission remains a separate contract.
- Persisted argv uses logical role tokens such as `<staged-root>` and `<native-graph>`, never private absolute paths.
- Graphify resolution returns one immutable canonical regular-path/device/inode/launcher-digest identity. The shared process boundary reopens and no-follow revalidates that identity immediately before every Graphify subprocess; any mismatch fails closed as `graphify_executable_changed` before the runner is called.
- `launcher_sha256` hashes the resolved regular Graphify entrypoint bytes after no-follow identity checks; it does not authenticate the installed package by itself. Invocation also binds the operational capability-smoke digest.
- No public/high-level command accepts a Graphify binary override. Explicit paths are admitted only as injected library-test seams or the maintainer-only compatibility-fixture capture input.
- `source_digest` always means canonical safe-source SHA-256, `projection_digest` always means the policy/projection SHA-256, and `git_commit_oid` is never compared with either.
- Official 0.9.48 `extract --no-cluster` output is observed post-dedup evidence. It can never prove pre-dedup multiplicity or complete transform lineage, even when all observed counters are zero.
- Unknown evidence is `null` plus a stable limitation, never the integer zero.
- Endpoint repair is allowed only by an adapter-declared deterministic one-to-one rule. Missing, ambiguous, or unresolved endpoints are quarantined before clustering and counted by stable reason code.
- Clustering may not introduce a missing/dangling endpoint, invalid self-loop, exact duplicate, or forbidden parallel relation; final validation fails instead of rewriting it.
- Evidence permits only IDs, relation, direction, confidence, confined safe source paths, and line/column coordinates. Context strings, source fragments, arbitrary symbols, diagnostic examples, producer source snippets, and absolute paths are excluded.
- `GRAPH_EVIDENCE.json` is mandatory for artifact schema 2, bound into graph metadata and ownership, and capped at 64 MiB. Native graph capture is capped at 256 MiB and diagnostic capture at 4 MiB.
- Ownership schema 2 lists SHA-256 and byte length for every approved artifact, never a digest of the ownership file itself, and carries an immutable integer `build_epoch >= 1`.
- Impact is `trusted` only when every predicate in the approved spec is true. A manifest cannot override or promote trust.
- The 0.9.48 adapter always returns `navigation` with `pre_dedup_edge_projection_unavailable`, even when navigation graph integrity is clean.
- No result uses “unaffected” or “no impact” language in navigation mode; an empty traversal means only “no path found in this graph.”
- Scheduled upstream probes run read-only, never declare support, never open a pull request, and encode network/install failure as `inconclusive`.
- GitHub Actions use `actions/checkout@11d5960a326750d5838078e36cf38b85af677262`, `actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065`, and `actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02`.
- Every task follows red-green-refactor, ends with its focused tests plus the relevant regression slice passing, and creates a reviewable commit.

## Execution Order and Cross-Plan Boundary

Execution is deliberately phased to avoid a dependency cycle:

1. execute this plan's Tasks 1–4 (registry, integrity, adapter, evidence primitives);
2. execute Core Adoption Tasks 1–5 (manifest v2/project UID, privacy/projection receipt, coverage model, and their stable data types);
3. return to this plan for Tasks 5–8 (candidate/ownership integration, trust handoff, probe, and CI);
4. execute Core Adoption Tasks 6–13 (lifecycle, health, queries/affected, registry, and CLI integration);
5. execute the Artifact/Fleet plan.

This plan owns `compatibility.py`, `adapters/`, `evidence.py`, `integrity.py`, compatibility fixtures, the compatibility probe, and evidence-aware extensions to the existing `adapter.py` and `artifacts.py`. Core owns lifecycle orchestration, coverage approval, health schema v2, snapshot-gated `query/path/explain/affected`, and registry synchronization. Artifact/fleet owns pack/pull/publish/fleet transport. All phases call the stable APIs here and must not duplicate version allowlists, graph invariants, evidence parsing, or impact-trust decisions.

## File Map

- Create `src/project_knowledge/compatibility.py`: immutable registry, exact command renderer, backend environment admission, and capability consistency checks.
- Create `src/project_knowledge/compatibility_fixtures/__init__.py` and `runtime_probe.py`: packaged deterministic code-only smoke source and later captured fixture resources.
- Create `src/project_knowledge/adapters/__init__.py`: adapter resolver and public re-exports.
- Create `src/project_knowledge/adapters/base.py`: captured-artifact, native-graph, normalization-result, and adapter protocol contracts.
- Create `src/project_knowledge/adapters/graphify_0_9_48.py`: 0.9.48 parsing, endpoint normalization/quarantine, final graph adaptation, and deterministic edge IDs.
- Create `src/project_knowledge/compatibility_fixtures/graphify_0_9_48.json`: packaged, sanitized real 0.9.48 post-dedup/diagnostic fixture.
- Create `src/project_knowledge/evidence.py`: invocation digest, strict evidence builder/parser, edge-evidence index, and trust decision.
- Modify `src/project_knowledge/integrity.py`: adapter-driven structural invariants and canonical final-edge identity.
- Modify `src/project_knowledge/adapter.py`: select the versioned adapter, write evidence-bound schema-2 candidates, and append the AtlasWeaver report integrity section.
- Modify `src/project_knowledge/artifacts.py`: registry-based version admission, schema-2 evidence validation, non-circular ownership, and owned-live-graph validation.
- Modify `src/project_knowledge/manifest.py`: resolve exact versions only through the compatibility API.
- Modify `src/project_knowledge/graphify.py`: probe the registry-declared command surface instead of a module-level command allowlist.
- Delete `config/graphify.lock.yaml`: remove the second version/capability allowlist.
- Create `src/project_knowledge/compat_probe.py`: bounded PyPI resolver and read-only upstream compatibility report CLI.
- Create `scripts/capture-graphify-compatibility-fixture`: maintainer-only deterministic fixture capture through official CLI commands.
- Create `.github/workflows/graphify-compatibility.yml`: supported-fixture CI and scheduled newest-release probe.
- Modify `.github/workflows/ci.yml`: resolve the production version from the registry and pin tools/actions exactly.
- Create `tests/test_compatibility.py`, `tests/test_graphify_0_9_48_adapter.py`, `tests/test_evidence.py`, and `tests/test_compat_probe.py`.
- Modify `tests/test_integrity.py`, `tests/test_adapter.py`, `tests/test_artifacts.py`, `tests/test_graphify_adapter.py`, `tests/test_manifest.py`, `tests/test_real_graphify_pipeline.py`, and `tests/test_public_release.py`.
- Add fixture sources under `tests/fixtures/graphify/0.9.48/source/` and capture expectations under `tests/fixtures/graphify/0.9.48/`.

## Stable Interface Ledger

The implementation must export these exact names; later plans import them rather than recreating equivalent contracts.

```python
# project_knowledge.compatibility
class CompatibilityError(ValueError): ...

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

def resolve_graphify_compatibility(version: str) -> GraphifyCompatibility: ...
def production_graphify_compatibility() -> GraphifyCompatibility: ...
def supported_graphify_versions() -> tuple[str, ...]: ...
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
) -> RenderedCommand: ...
def admitted_graphify_environment(
    contract: GraphifyCompatibility,
    backend: str | None,
    ambient: Mapping[str, str],
) -> dict[str, str]: ...
def render_graphify_global_add(
    contract: GraphifyCompatibility,
    *,
    binary: Path,
    graph: Path,
    registry_key: str,
) -> RenderedCommand: ...
def render_graphify_agent_install(
    contract: GraphifyCompatibility,
    *,
    binary: Path,
    platform: Literal["codex", "agents"],
) -> RenderedCommand: ...
def validate_semantic_backend(
    contract: GraphifyCompatibility,
    backend: str,
    ambient: Mapping[str, str],
) -> BackendContract: ...
def bind_semantic_backend_credential(
    contract: GraphifyCompatibility,
    backend: str,
    credential: str | None,
) -> dict[str, str]: ...
def assert_capability_surface(
    contract: GraphifyCompatibility,
    probe: CapabilityProbe,
) -> None: ...

# project_knowledge.graphify (extended existing result)
@dataclass(frozen=True)
class ResolvedGraphifyExecutable:
    path: Path
    st_dev: int
    st_ino: int
    launcher_sha256: str

@dataclass(frozen=True)
class GraphifyCapabilities:
    version: str
    commands: frozenset[str]
    supports_obsidian_export: bool
    supports_global_registry: bool
    executable: ResolvedGraphifyExecutable
    capability_probe: CapabilityProbe

def resolve_graphify_executable(
    test_override: Path | None = None,
) -> ResolvedGraphifyExecutable: ...
def revalidate_graphify_executable(
    executable: ResolvedGraphifyExecutable,
) -> None: ...
def run_checked(
    runner: CommandRunner,
    executable: ResolvedGraphifyExecutable,
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = COMMAND_TIMEOUT,
    before_exec: Callable[[], None] = _noop,
) -> CompletedProcess[str]: ...
def probe_graphify(
    executable: ResolvedGraphifyExecutable,
    contract: GraphifyCompatibility,
    runner: CommandRunner,
    *,
    before_exec: Callable[[], None] = _noop,
) -> GraphifyCapabilities: ...
```

```python
# project_knowledge.adapters.base / project_knowledge.adapters
class AdapterContractError(ValueError): ...

@dataclass(frozen=True)
class CapturedArtifact:
    logical_path: PurePosixPath
    payload: bytes
    sha256: str
    byte_length: int

@dataclass(frozen=True)
class NativeGraph:
    document: Mapping[str, Any]
    nodes: tuple[Mapping[str, Any], ...]
    edges: tuple[Mapping[str, Any], ...]

@dataclass(frozen=True)
class ReasonCount:
    code: str
    count: int

@dataclass(frozen=True)
class NormalizationResult:
    cluster_input: CapturedArtifact
    observed_integrity: GraphIntegrity
    repairs: tuple[ReasonCount, ...]
    quarantines: tuple[ReasonCount, ...]

class GraphifyAdapter(Protocol):
    contract: GraphifyCompatibility
    def parse_post_dedup(self, artifact: CapturedArtifact) -> NativeGraph: ...
    def normalize_for_cluster(self, graph: NativeGraph) -> NormalizationResult: ...
    def adapt_clustered_graph(
        self,
        artifact: CapturedArtifact,
        *,
        staged_files: frozenset[PurePosixPath],
    ) -> bytes: ...

def capture_native_artifact(
    path: Path,
    logical_path: PurePosixPath,
    *,
    max_bytes: int,
) -> CapturedArtifact: ...
def adapter_for(contract: GraphifyCompatibility) -> GraphifyAdapter: ...
```

```python
# project_knowledge.evidence
class EvidenceError(ValueError): ...

GRAPH_EVIDENCE_MAX_BYTES = 67_108_864

@dataclass(frozen=True)
class ArtifactBinding:
    path: PurePosixPath
    sha256: str
    byte_length: int

@dataclass(frozen=True)
class CommandEnvironmentBinding:
    operation: Literal["extract", "diagnose", "cluster"]
    names: tuple[str, ...]

@dataclass(frozen=True)
class ExtractionInvocation:
    adapter_id: str
    graphify_version: str
    executable_sha256: str
    capability_smoke_digest: str
    commands: tuple[tuple[str, ...], ...]
    backend: str | None
    model: str | None
    configuration_sha256: str
    source_digest: str
    projection_digest: str
    environments: tuple[CommandEnvironmentBinding, ...]
    artifacts: tuple[ArtifactBinding, ...]
    payload: bytes
    digest: str

@dataclass(frozen=True)
class FinalEdgeEvidence:
    final_edge_id: str
    source: str
    target: str
    relation: str
    confidence: str | None
    source_file: PurePosixPath | None
    source_line: int | None
    source_column: int | None

@dataclass(frozen=True)
class PreDedupEdgeEvidence:
    occurrence_id: str
    source: str
    target: str
    relation: str
    confidence: str | None
    source_file: PurePosixPath | None
    source_line: int | None
    source_column: int | None
    final_edge_id: str | None
    disposition: Literal["kept", "rewritten", "dropped"]
    reason: str | None

@dataclass(frozen=True)
class GraphEvidence:
    graphify_version: str
    adapter_id: str
    source_digest: str
    projection_digest: str
    extraction_invocation_digest: str
    invocation: ExtractionInvocation
    observed_post_dedup: GraphIntegrity
    pre_dedup: tuple[PreDedupEdgeEvidence, ...] | None
    normalization_repairs: tuple[ReasonCount, ...]
    normalization_quarantines: tuple[ReasonCount, ...]
    final_integrity: GraphIntegrity
    final_edges: tuple[FinalEdgeEvidence, ...]
    evidence_complete: bool
    limitations: tuple[str, ...]
    payload: bytes
    digest: str

@dataclass(frozen=True)
class ImpactTrust:
    level: Literal["trusted", "navigation"]
    limitations: tuple[str, ...]

def build_extraction_invocation(
    contract: GraphifyCompatibility,
    *,
    executable_sha256: str,
    capability_smoke_digest: str,
    commands: tuple[RenderedCommand, ...],
    backend: str | None,
    model: str | None,
    configuration_sha256: str,
    source_digest: str,
    projection_digest: str,
    environments: tuple[CommandEnvironmentBinding, ...],
    artifacts: tuple[CapturedArtifact, ...],
) -> ExtractionInvocation: ...
def build_graph_evidence(
    contract: GraphifyCompatibility,
    *,
    source_digest: str,
    projection_digest: str,
    invocation: ExtractionInvocation,
    native_graph: CapturedArtifact,
    diagnosis: CapturedArtifact,
    normalization: NormalizationResult,
    clustered_graph: CapturedArtifact,
    final_graph: CapturedArtifact,
    staged_files: frozenset[PurePosixPath],
) -> GraphEvidence: ...
def parse_graph_evidence(
    payload: bytes,
    contract: GraphifyCompatibility,
    *,
    expected_digest: str,
) -> GraphEvidence: ...
def index_final_edge_evidence(
    evidence: GraphEvidence,
) -> Mapping[str, FinalEdgeEvidence]: ...
def decide_impact_trust(
    evidence: GraphEvidence,
    final_integrity: GraphIntegrity,
    *,
    source_current: bool | None,
    projection_current: bool | None,
    coverage_complete: bool,
    artifacts_bound: bool,
) -> ImpactTrust: ...
```

```python
# project_knowledge.integrity
class IntegrityError(ValueError): ...

@dataclass(frozen=True)
class GraphIntegrity:
    node_count: int
    edge_count: int
    missing_endpoint_edges: int
    dangling_endpoint_edges: int
    invalid_self_loop_edges: int
    exact_duplicate_edges: int
    conflicting_relation_edges: int
    collapsed_edges: int | None
    structurally_valid: bool
    schema_version: int = 2

def canonical_final_edge_id(
    edge: Mapping[str, Any],
    semantics: GraphSemantics,
) -> str: ...
def analyze_graph(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    semantics: GraphSemantics,
    collapsed_edges: int | None = None,
) -> GraphIntegrity: ...
def validate_final_graph(
    document: Mapping[str, Any],
    semantics: GraphSemantics,
) -> GraphIntegrity: ...
```

The existing candidate APIs become:

```python
@dataclass(frozen=True)
class GitIdentity:
    commit_oid: str
    algorithm: Literal["sha1", "sha256"]

@dataclass(frozen=True)
class AdaptedCandidate:
    root: Path
    artifact_schema_version: int
    source_digest: str
    projection_digest: str | None
    evidence_digest: str | None
    extraction_invocation_digest: str | None
    generation_digest: str
    node_count: int
    edge_count: int
    skipped_count: int

@dataclass(frozen=True)
class ValidatedGraph:
    root: Path
    artifact_schema_version: int
    project_uid: UUID | None
    adapter_id: str | None
    source_digest: str
    projection_digest: str | None
    graph_digest: str
    evidence_digest: str | None
    extraction_invocation_digest: str | None
    generation_digest: str
    node_count: int
    edge_count: int
    skipped_count: int
    unapproved_skips: int
    impact_trust: Literal["trusted", "navigation"]
    impact_limitations: tuple[str, ...]
    build_epoch: int | None
    git_identity: GitIdentity | None

@dataclass(frozen=True)
class PromotionResult:
    target: Path
    backup: Path
    digest: str
    generation_digest: str
    build_epoch: int | None
    changed: bool = True

def adapt_candidate(
    raw_candidate: Path,
    destination: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    *,
    evidence: GraphEvidence | None = None,
    post_write_check: Callable[[], None] | None = None,
) -> AdaptedCandidate: ...

def validate_candidate(
    candidate_dir: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    *,
    expected_projection_digest: str | None = None,
    expected_evidence_digest: str | None = None,
    build_epoch: int | None = None,
    git_identity: GitIdentity | None = None,
) -> ValidatedGraph: ...

def validate_owned_graph(
    root: Path,
    manifest: ProjectManifest,
    *,
    expected_source_digest: str | None = None,
    expected_projection_digest: str | None = None,
    repository_access: RepositoryAccess | None = None,
) -> ValidatedGraph: ...
```

`adapt_candidate()` keeps generated graph bytes independent from clocks and Git state. It also computes `generation_digest` as a domain-separated canonical hash of the exact approved non-ownership artifact path/SHA-256/byte-length set; this binds report/evidence/optional HTML changes that `graph_digest` alone cannot see. Evidence absent means legacy artifact schema 1; evidence present means artifact schema 2. `validate_candidate()` recomputes that generation digest, requires `build_epoch` (a non-boolean integer at least 1) and an externally anchored `expected_evidence_digest` for schema 2, accepts optional validated Git identity for ownership, and requires all three to be absent for schema 1. The anchor comes from the in-process `AdaptedCandidate.evidence_digest` or a descriptor-captured bundle binding, never by hashing the untrusted candidate at the validation call site. `promote_graph()` returns the descriptor-validated installed graph/generation digests and installed epoch; exact no-op therefore reports the pre-existing epoch, never the proposed candidate epoch. Core manifest-v2 refresh passes evidence into adaptation, compares the generation digest to the currently validated live generation, then passes the adapted evidence digest, staged projection digest, and the installed epoch for an exact no-op or the next epoch for a changed generation. Legacy schema-1 output remains readable but is always navigation-only.

---

### Task 1: Establish the sole-source compatibility registry

**Files:**
- Create: `src/project_knowledge/compatibility.py`
- Create: `src/project_knowledge/compatibility_fixtures/__init__.py`
- Create: `src/project_knowledge/compatibility_fixtures/runtime_probe.py`
- Create: `src/project_knowledge/compatibility_fixtures/graphify_0_9_48.json`
- Create: `tests/fixtures/graphify/0.9.48/source/fixture.py`
- Create: `scripts/capture-graphify-compatibility-fixture`
- Modify: `src/project_knowledge/manifest.py`
- Modify: `src/project_knowledge/graphify.py`
- Modify: `src/project_knowledge/artifacts.py`
- Delete: `config/graphify.lock.yaml`
- Create: `tests/test_compatibility.py`
- Modify: `tests/test_manifest.py`
- Modify: `tests/test_graphify_adapter.py`
- Modify: `tests/test_artifacts.py`

**Interfaces:**
- Consumes: existing `ProjectManifest.graphify_version` and `GraphifyCapabilities.version/commands`.
- Produces: every symbol in the `project_knowledge.compatibility` ledger above plus `ResolvedGraphifyExecutable`, `resolve_graphify_executable()`, `revalidate_graphify_executable()`, identity-revalidating `run_checked()`, and `probe_graphify()` exactly as declared in the `project_knowledge.graphify` ledger.

- [ ] **Step 1: Write failing registry and sole-source tests**

Create tests that pin the immutable 0.9.48 contract, reject ranges/unknown versions, prove tuple/frozenset immutability, and scan production touchpoints for duplicate version allowlists:

```python
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
        AgentInstallContract(
            "codex", PurePosixPath(".codex/skills/graphify/SKILL.md")
        ),
        AgentInstallContract(
            "agents", PurePosixPath(".agents/skills/graphify/SKILL.md")
        ),
    )
    assert contract.coverage_reason_codes == frozenset({
        "not_represented_by_graphify"
    })
    assert contract.normalization_reason_codes == frozenset({
        "unique_exact_node_alias", "missing_endpoint",
        "ambiguous_endpoint_alias", "dangling_endpoint",
    })
    assert contract.lineage_reason_codes == frozenset()
    assert supported_graphify_versions() == ("0.9.48",)
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
```

Also assert the exact 0.9.48 command contracts, graph semantics, backend names/environment lists, source suffixes, schema fingerprint format, and fixture digest format. The sensitive-source extension assertion must include `.py`, `.pyi`, `.js`, `.jsx`, `.cjs`, `.mjs`, `.ts`, `.tsx`, `.go`, `.rs`, `.java`, `.kt`, `.kts`, `.cs`, `.c`, `.h`, `.cc`, `.cpp`, `.cxx`, `.hpp`, `.cu`, `.cuh`, `.rb`, `.swift`, `.php`, `.scala`, `.lua`, `.sh`, `.bash`, `.zsh`, `.fish`, `.ps1`, `.psm1`, `.ex`, `.exs`, `.m`, `.mm`, `.ml`, `.mli`, `.jl`, `.r`, `.dart`, `.zig`, `.v`, `.vhd`, `.vhdl`, `.sql`, `.tf`, `.hcl`, `.f`, `.f90`, `.f95`, `.pas`, `.apex`, `.lisp`, `.clj`, and `.cljs`, while excluding `.json`, `.yaml`, `.yml`, `.toml`, `.ini`, and `.tfvars`.

Add executable-identity tests that create a mode-0755 launcher, resolve it once, and assert the frozen canonical path/device/inode/SHA-256. Parameterize replacement by `os.replace`, in-place byte mutation retaining the inode, a symlink swap, execute-bit removal, truncation/oversize, and disappearance. For every mutation, `revalidate_graphify_executable()` and the next `run_checked()` must raise exactly `GraphifyContractError("graphify_executable_changed")`; the fake runner call list remains empty. A two-command fake runner mutates the launcher after its first return and proves the second Graphify subprocess is rejected. Test that argv zero differing from `str(executable.path)` is rejected before revalidation/spawn and that `resolve_graphify_executable()` accepts an explicit path only through its named library-test seam—there is no environment or public CLI override.

The same test module must bind the resource to the reviewed literal before Task 1 can pass:

```python
payload = resources.files("project_knowledge.compatibility_fixtures").joinpath(
    contract.fixture_resource
).read_bytes()
assert hashlib.sha256(payload).hexdigest() == contract.compatibility_fixture_digest
```

- [ ] **Step 2: Run the registry tests and confirm the missing module failure**

Run: `uv run pytest -q tests/test_compatibility.py tests/test_manifest.py tests/test_graphify_adapter.py tests/test_artifacts.py`

Expected: collection fails with `ModuleNotFoundError: No module named 'project_knowledge.compatibility'`.

- [ ] **Step 3: Implement the immutable registry types and single 0.9.48 entry**

Use frozen dataclasses containing only immutable values. Build the registry as a `MappingProxyType`, validate it once at import, require exactly one `production=True` entry, derive schema fingerprints from canonical embedded schema descriptors, and bind the captured fixture with the separately reviewed literal created in Step 5:

The runtime smoke resource contains exactly `def probe(value):\n    return value\n`; it has no import, docstring, network access, repository read, or semantic corpus.

```python
_BASE_ENV = ("HOME", "LANG", "LC_ALL", "PATH")
_FORBIDDEN = frozenset({
    "--force", "--global", "--as", "--postgres", "--google-workspace",
    "--no-gitignore", "--cargo", "--project", "--strict", "--platform",
})

_GRAPHIFY_0_9_48 = GraphifyCompatibility(
    version="0.9.48",
    adapter_id="graphify-0.9.48",
    artifact_schema_versions=(1, 2),
    required_cli_commands=frozenset({
        "extract", "diagnose", "cluster-only", "query", "explain", "path",
        "global", "export", "install",
    }),
    commands=(
        CommandContract(
            "extract", ("extract", "--out", "--no-cluster"), _FORBIDDEN,
            "native-json",
        ),
        CommandContract(
            "diagnose",
            ("diagnose", "multigraph", "--graph", "--undirected", "--json"),
            _FORBIDDEN,
            "diagnostic-json",
        ),
        CommandContract(
            "cluster",
            ("cluster-only", "--graph", "--no-label"),
            _FORBIDDEN,
            "clustered-artifacts",
        ),
        CommandContract(
            "global-add",
            ("global", "add", "--as"),
            _FORBIDDEN - {"--as"},
            "none",
        ),
        CommandContract(
            "agent-install",
            ("install", "--platform"),
            _FORBIDDEN - {"--platform"},
            "none",
        ),
    ),
    backends=(
        BackendContract(
            name="claude",
            admitted_environment=("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"),
            credential_environment=("ANTHROPIC_API_KEY",),
            canonical_credential_environment="ANTHROPIC_API_KEY",
        ),
        BackendContract(
            name="deepseek",
            admitted_environment=("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
            credential_environment=("DEEPSEEK_API_KEY",),
            canonical_credential_environment="DEEPSEEK_API_KEY",
        ),
        BackendContract(
            name="gemini",
            admitted_environment=("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_BASE_URL"),
            credential_environment=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            canonical_credential_environment="GEMINI_API_KEY",
        ),
        BackendContract(
            name="kimi",
            admitted_environment=("MOONSHOT_API_KEY", "KIMI_BASE_URL"),
            credential_environment=("MOONSHOT_API_KEY",),
            canonical_credential_environment="MOONSHOT_API_KEY",
        ),
        BackendContract(
            name="ollama",
            admitted_environment=("OLLAMA_API_KEY", "OLLAMA_BASE_URL", "OLLAMA_HOST"),
            credential_environment=("OLLAMA_API_KEY",),
            canonical_credential_environment="OLLAMA_API_KEY",
            allows_credentialless=True,
        ),
        BackendContract(
            name="openai",
            admitted_environment=("OPENAI_API_KEY", "OPENAI_BASE_URL"),
            credential_environment=("OPENAI_API_KEY",),
            canonical_credential_environment="OPENAI_API_KEY",
        ),
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
        edge_identity_fields=(
            "source", "target", "relation", "source_file", "source_location", "context"
        ),
        parallel_key_fields=("source", "target"),
        parallel_policy="forbid",
        allowed_self_loop_relations=frozenset(),
        accepted_confidence=frozenset({"EXTRACTED", "INFERRED", "AMBIGUOUS"}),
    ),
    sensitive_source_suffixes=_GRAPHIFY_0_9_48_SOURCE_SUFFIXES,
    coverage_reason_codes=frozenset({"not_represented_by_graphify"}),
    normalization_reason_codes=frozenset({
        "unique_exact_node_alias", "missing_endpoint",
        "ambiguous_endpoint_alias", "dangling_endpoint",
    }),
    lineage_reason_codes=frozenset(),
    structured_query_commands=frozenset(),
    production=True,
)
```

`_GRAPHIFY_0_9_48_NATIVE_SCHEMA` must describe the observed `extract --no-cluster` keys (`nodes`, `edges`, `hyperedges`, `input_tokens`, `output_tokens`), required node `id`, and edge `source/target/relation`; it is a schema descriptor, not a copy of mutable native metadata. The diagnostic descriptor covers the allowlisted summary and `effective_directed=False`; the clustered descriptor covers `directed=False`, `multigraph=False`, `nodes`, and `links`. `_GRAPHIFY_0_9_48_FIXTURE_SHA256` is a reviewed literal 64-character lowercase hex digest of the complete packaged fixture bytes. It is never computed from the installed resource at runtime: fixture capture and support declaration must produce a visible fixture-file diff and a separate visible registry-literal diff, and the consistency test rejects either side changing alone.

- [ ] **Step 4: Implement exact argv rendering and backend environment admission**

Render actual and canonical argv together, validate every option before rendering, and keep a fixed option order:

```python
if operation == "extract":
    if code_only == (backend is not None):
        raise CompatibilityError("choose exactly one extraction mode")
    if deep and code_only:
        raise CompatibilityError("deep mode requires a semantic backend")
    actual = [str(binary), "extract", str(source), "--out", str(output), "--no-cluster"]
    canonical = ["<graphify>", "extract", "<staged-root>", "--out", "<raw-output-root>", "--no-cluster"]
    if code_only:
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
    actual = [str(binary), "diagnose", "multigraph", "--graph", str(graph), "--undirected", "--json"]
    canonical = ["<graphify>", "diagnose", "multigraph", "--graph", "<native-graph>", "--undirected", "--json"]
else:
    if graph is None:
        raise CompatibilityError("cluster requires a normalized graph")
    actual = [str(binary), "cluster-only", str(source), "--graph", str(graph), "--no-label"]
    canonical = ["<graphify>", "cluster-only", "<cluster-workspace>", "--graph", "<cluster-input>", "--no-label"]
    if not track_html:
        actual.append("--no-viz")
        canonical.append("--no-viz")
return RenderedCommand(operation, tuple(actual), tuple(canonical))
```

The separate registry mutation renderer is fixed and admits no option passthrough:

```python
def render_graphify_global_add(
    contract: GraphifyCompatibility,
    *,
    binary: Path,
    graph: Path,
    registry_key: str,
) -> RenderedCommand:
    _command(contract, "global-add")
    pattern = r"atlasweaver/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    if re.fullmatch(pattern, registry_key) is None:
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
```

Test exact actual/canonical tuples, malformed/uppercase/non-v4 UUIDs, any namespace other than the single literal `atlasweaver/`, extra slashes, whitespace, empty/overlong values, and prove no caller can add a seventh argv item. `--as` remains in every pipeline contract's forbidden set and is admitted only as the fixed fifth token of this renderer.

The separate agent integration renderer also admits no passthrough or project-scoped mutation:

```python
def render_graphify_agent_install(
    contract: GraphifyCompatibility,
    *,
    binary: Path,
    platform: Literal["codex", "agents"],
) -> RenderedCommand:
    _command(contract, "agent-install")
    target = next(
        (item for item in contract.agent_installs if item.platform == platform),
        None,
    )
    if target is None:
        raise CompatibilityError("agent install platform is unsupported")
    return RenderedCommand(
        "agent-install",
        (str(binary), "install", "--platform", platform),
        ("<graphify>", "install", "--platform", platform),
    )
```

Test exact tuples for `codex` and `agents`, reject every other/uppercase/empty platform and non-string runtime value, and prove `--project`, `--strict`, an alternate output root, and extra argv cannot be expressed through this API. `AgentInstallContract.home_relative_skill` must be normalized, non-absolute, contain no `..`, and remain under the corresponding single top-level directory (`.codex/` or `.agents/`).

`validate_semantic_backend()` resolves only the registry backend and, unless `allows_credentialless` is true, requires at least one non-empty declared `credential_environment` value or raises `CompatibilityError("semantic_backend_required")`. Ollama is explicitly credentialless-capable because 0.9.48 supports its local default and `OLLAMA_BASE_URL`/`OLLAMA_HOST`; its optional API key is still admitted. `admitted_graphify_environment()` copies non-empty values only for `_BASE_ENV` plus the selected `BackendContract.admitted_environment`; it rejects an unknown backend and never mutates `ambient`.

`bind_semantic_backend_credential()` is the separate reusable-workflow/fleet boundary. Resolve the backend through the same registry; require `canonical_credential_environment` to be a member of both admitted and credential environment tuples; return `{canonical_name: credential}` for a non-empty opaque string; return `{}` only when the credential is absent and `allows_credentialless` is true; otherwise raise `CompatibilityError("semantic_backend_required")`. Never strip, interpolate, log, digest, or persist the credential, and never merge ambient variables in this function. Test every backend mapping, missing cloud credentials, credentialless Ollama, optional Ollama credential injection, unknown backend, empty string, and a sentinel credential containing whitespace/metacharacters to prove the value is returned byte-for-byte and never appears in exception text or canonical argv.

- [ ] **Step 5: Capture the reviewed compatibility fixture and literal digest**

Before routing consumers, create the exact deterministic fixture source shown below, implement the bounded official-CLI capture tool, capture the packaged JSON, and set `_GRAPHIFY_0_9_48_FIXTURE_SHA256` to the separately printed/reviewed literal. Task 1 is not complete and must not be committed while the resource and literal disagree.

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Request:
    name: str


def normalize(request: Request) -> str:
    return request.name.strip().casefold()


def dispatch(request: Request) -> str:
    return normalize(request)
```

Run the capture tool twice so each invocation creates a different independently allocated temporary source root and writes a different explicit output; require byte equality across those two captures. Recursively assert the packaged JSON has no absolute POSIX/Windows path before inserting its exact `shasum -a 256` result as the registry literal. The committed capture script accepts `--expected-version` during this bootstrap call but stores no default/allowlist; after the registry resource exists, its normal mode resolves `production_graphify_compatibility()` and Task 3 tests that mode. This proves temp-root normalization, avoids a circular digest, and avoids a second production allowlist.

The executable Python capture tool has this exact contract:

1. require explicit Graphify binary, source directory, and output path;
2. verify `graphify --version` equals the supplied bootstrap version or, in normal mode, the production registry version;
3. copy only the committed fixture source into a mode-0700 temporary root;
4. execute registry-rendered `extract --code-only --no-cluster`;
5. capture native graph bytes before any normalization;
6. execute registry-rendered `diagnose ... --undirected --json` and parse stdout;
7. retain only `schema_version` and summary fields `node_count`, `raw_edge_count`, `missing_endpoint_edges`, `dangling_endpoint_edges`, `self_loop_edges`, `exact_duplicate_edges`, `undirected_unique_endpoint_pairs`, `undirected_same_endpoint_collapsed_edges`, `same_endpoint_group_count`, `relation_variant_groups`, `source_file_variant_groups`, `source_location_variant_groups`, `context_variant_groups`, `post_build_graph_type`, `post_build_node_count`, `post_build_edge_count`, and `effective_directed`;
8. normalize persisted source paths to logical root `fixture/` and reject every remaining absolute POSIX/Windows path recursively;
9. exclusive-create canonical JSON containing schema/version/source digest/native graph/sanitized diagnosis unless `--replace` is explicit;
10. print the complete fixture SHA-256.

Resolve the explicitly supplied maintainer binary once with `resolve_graphify_executable(test_override=binary)` and pass that immutable object to every bounded `SubprocessCommandRunner`/`run_checked` call; never use raw `subprocess.run`. Pass only fixed code-only environment names. Tests seed proxy/token/path ambient variables and prove none reach the runner/output. Run the tool twice, compare bytes with `cmp`, hash the committed resource with `shasum -a 256`, then apply that exact value to `_GRAPHIFY_0_9_48_FIXTURE_SHA256`. The consistency test must fail if either file or literal changes alone.

- [ ] **Step 6: Route manifest, Graphify probing, and artifact version checks through the registry**

Replace `_GRAPHIFY_VERSION`, `REQUIRED_CLI_COMMANDS`, and `PINNED_GRAPHIFY_VERSION` checks with `resolve_graphify_compatibility(manifest.graphify_version)` and `assert_capability_surface()`. Keep the manifest's exact pin as project data, not a global accepted-version list. Delete `config/graphify.lock.yaml`; update its former contract tests to assert the registry instead. Every shell command required by current or later code must be present in `contract.required_cli_commands`; no supplementary command set is allowed in `graphify.py`, CLI code, tests, or workflows.

Help parsing remains a cheap missing-command check, but is not sufficient admission. Extend `probe_graphify()` to run a deterministic official smoke sequence in a private temporary directory using only registry-rendered argv: copy a packaged source containing only `def probe(value): return value`; run code-only extract/no-cluster; run a second extract over the same code-only corpus with `backend="ollama"`, `model="atlasweaver-capability-probe"`, and `deep=True` into a separate output root; run undirected JSON diagnosis of the first native graph; then cluster/no-label/no-viz. The second run has no document, paper, or image input, receives no backend credential/endpoint environment, and therefore exercises exact `--backend`, `--model`, and `--mode deep` parser/runtime admission without a semantic request; any attempted semantic connection makes preflight fail closed.

Run registry-rendered `global add` against that private clustered graph with the fixed valid probe key `atlasweaver/00000000-0000-4000-8000-000000000000`, a separate mode-0700 HOME, and an otherwise stripped environment. Require no-follow regular `${HOME}/.graphify/global-graph.json` and `global-manifest.json`, strict-parse both under the existing caps, require the manifest's sole repo key to equal the probe key and the global graph to be non-empty, and set `global_add_verified=True` only after those checks. This is an operational exact-flag smoke, not syntax/help resemblance; it cannot touch the user's registry, and its absolute private source path is never persisted outside the disposable HOME or capability digest. Finally, run both registry-rendered agent installs with separate private mode-0700 HOME directories and an otherwise stripped environment; require the exact registry-declared non-empty `SKILL.md` target to exist as a no-follow regular file, reject any output escaping that HOME, and delete all private homes.

Read outputs with a private no-follow, identity-checked, size-capped helper in `graphify.py`; require both extracts to match the native schema fingerprint; compute the diagnostic/clustered fingerprints; and pass the constructed `CapabilityProbe` to `assert_capability_surface()`. Set `canonical_commands` to the exact seven registry-rendered canonical tuples in `(code-only extract, semantic-no-content extract, diagnose, cluster, global-add, codex install, agents install)` order and compare them to freshly rendered expected tuples in `assert_capability_surface()`. Require `global_add_verified is True`; set `agent_install_platforms` only after the corresponding confined target check and require it to equal the registry platform set. Compute `CapabilityProbe.digest` as SHA-256 of `b"atlasweaver-graphify-capability-v1\0"` plus canonical JSON containing installed version, sorted command names, all seven canonical command tuples, all three fingerprints, `global_add_verified`, sorted installed agent platforms, packaged smoke-source SHA-256, and the registry's reviewed compatibility-fixture digest. Return `GraphifyCapabilities(..., executable=executable, capability_probe=capability_probe)` only after admission. Delete the temporary root on success/failure; a cleanup failure is a sanitized contract failure. Unit tests inject a prepared smoke root and fake runner that writes the expected graph/global/install artifacts, then mutate one token/order/output fingerprint/global manifest key/global graph/install target at a time and assert rejection. This operational probe verifies the exact accepted extraction, global-add, and agent-install flags plus observed output shapes without a semantic API call, user-home mutation, or Graphify internal import. Task 3 replaces no call sites with a weaker reader; its reusable descriptor adds immutable payload/digest metadata on top of the same checks.

`resolve_graphify_executable(None)` finds only the literal `graphify` name through `shutil.which`; the optional explicit argument is a named library-test/maintainer seam and is never read from CLI or ambient configuration. Canonicalize with `Path.resolve(strict=True)`, `lstat` the resolved path, require a regular file with an execute bit, open with `O_RDONLY | O_NOFOLLOW | O_CLOEXEC`, compare path stat/open `fstat`/final `fstat` device and inode, cap reads at 16 MiB, and return `ResolvedGraphifyExecutable(path, st_dev, st_ino, launcher_sha256)`. The digest identifies the Python launcher file only; capability-smoke digest plus exact package-reported version provide the operational binding and no code describes the launcher hash as package authentication.

`revalidate_graphify_executable()` repeats the same no-follow open, regular/execute/cap/stable-stat checks against the frozen canonical path and recomputes the complete launcher digest. Any missing/unreadable path, symlink, device/inode change, in-place byte change, execute-bit change, oversize, or unstable stat is normalized to exactly `GraphifyContractError("graphify_executable_changed")` with no path/cause detail. `run_checked()` first requires non-empty argv with `argv[0] == str(executable.path)`, invokes its private `before_exec` binding callback, then calls executable revalidation as its final action before `runner.run`; it never retries or re-resolves. Every `probe_graphify()` subprocess—version, help, two extracts, diagnosis, clustering, global-add, and both installs—passes the probe callback through that function, so a retained manifest/root binding can be checked before every child and a launcher mutation between any pair fails before the next spawn. Callback failure propagates before executable revalidation/spawn and no later smoke child runs. Core Task 6 may factor execution/error redaction into `run_graphify_operation()` but must preserve this callback/revalidation ordering and immutable argument.

Add a callback-count regression requiring one callback immediately before every
recorded probe child. Parameterize failure after each completed child; the next
callback raises `ManifestError(kind="changed")`, runner call count does not
advance, no later smoke operation occurs, and all private roots are cleaned.

- [ ] **Step 7: Run focused and full regressions**

Run: `uv run pytest -q tests/test_compatibility.py tests/test_manifest.py tests/test_graphify_adapter.py tests/test_artifacts.py`

Expected: PASS.

Run: `uv run pytest -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit the registry and reviewed fixture**

```bash
git add src/project_knowledge/compatibility.py src/project_knowledge/compatibility_fixtures scripts/capture-graphify-compatibility-fixture tests/fixtures/graphify/0.9.48/source/fixture.py src/project_knowledge/manifest.py src/project_knowledge/graphify.py src/project_knowledge/artifacts.py tests/test_compatibility.py tests/test_manifest.py tests/test_graphify_adapter.py tests/test_artifacts.py config/graphify.lock.yaml
git commit -m "feat: centralize Graphify compatibility contracts"
```

### Task 2: Replace blanket integrity heuristics with adapter invariants

**Files:**
- Modify: `src/project_knowledge/integrity.py`
- Modify: `tests/test_integrity.py`
- Modify: `src/project_knowledge/adapter.py`
- Modify: `src/project_knowledge/artifacts.py`
- Modify: `tests/test_adapter.py`
- Modify: `tests/test_artifacts.py`

**Interfaces:**
- Consumes: `GraphSemantics` from Task 1.
- Produces: `GraphIntegrity`, `canonical_final_edge_id()`, `analyze_graph()`, and `validate_final_graph()` from the interface ledger.

- [ ] **Step 1: Write failing directedness, identity, parallel-policy, and edge-ID tests**

Replace tests that compare entire edge JSON blobs with invariant-driven cases:

```python
def test_undirected_identity_is_endpoint_order_independent() -> None:
    semantics = resolve_graphify_compatibility("0.9.48").semantics
    forward = edge("a", "b", relation="calls")
    reverse = edge("b", "a", relation="calls")
    assert canonical_final_edge_id(forward, semantics) == canonical_final_edge_id(reverse, semantics)


def test_parallel_relation_conflict_uses_adapter_policy() -> None:
    semantics = resolve_graphify_compatibility("0.9.48").semantics
    result = analyze_graph(
        nodes(),
        [edge("a", "b", "calls"), edge("a", "b", "imports")],
        semantics=semantics,
    )
    assert result.exact_duplicate_edges == 0
    assert result.conflicting_relation_edges == 1
    assert result.structurally_valid is False


def test_unknown_collapse_evidence_is_not_rewritten_as_zero() -> None:
    result = analyze_graph(nodes(), [edge()], semantics=semantics())
    assert result.collapsed_edges is None
    assert result.to_dict()["collapsed_edges"] is None
```

Add cases for duplicate node IDs, missing endpoint fields, dangling IDs, allowed versus invalid self-loops, exact duplicates under the declared identity tuple, directed endpoint order, booleans rejected as counters, non-string identity fields, and pre-forged `atlasweaver_edge_id` rejection.

- [ ] **Step 2: Run integrity tests and verify signature/schema failures**

Run: `uv run pytest -q tests/test_integrity.py`

Expected: FAIL because `analyze_graph()` does not accept `semantics`, `canonical_final_edge_id()` is absent, and schema v1 fields differ.

- [ ] **Step 3: Implement canonical identities and GraphIntegrity schema 2**

Canonicalize endpoints according to directedness, include exactly the registry-declared identity fields, and domain-separate the ID:

```python
def canonical_final_edge_id(edge: Mapping[str, Any], semantics: GraphSemantics) -> str:
    if "atlasweaver_edge_id" in edge:
        raise IntegrityError("edge contains reserved AtlasWeaver identity")
    values = {field: _identity_value(edge.get(field), field) for field in semantics.edge_identity_fields}
    source = values[semantics.source_field]
    target = values[semantics.target_field]
    if not semantics.directed and target < source:
        values[semantics.source_field], values[semantics.target_field] = target, source
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "edge-" + hashlib.sha256(b"atlasweaver-final-edge-v1\0" + payload).hexdigest()
```

`analyze_graph()` must:

1. validate unique non-empty node IDs;
2. count missing and dangling endpoints separately;
3. canonicalize endpoint-pair grouping with registry directedness;
4. count exact duplicate declared identities;
5. apply `parallel_policy` to groups with more than one distinct identity;
6. count only non-allowed self-loops;
7. set `structurally_valid` only when all invalid counters are zero;
8. preserve `collapsed_edges=None` without deriving impact trust.

`validate_final_graph()` accepts exactly one of `links` or `edges`, rejects a non-list/object member, calls `analyze_graph()`, verifies every `atlasweaver_edge_id` equals the recomputed ID after temporarily excluding that reserved field, and rejects duplicate final IDs.

- [ ] **Step 4: Update adapter/artifact callers to pass registry semantics**

At every production call site resolve the manifest version once and pass `contract.semantics`. Map the old graph-health document to schema 2 and keep the old legacy field aliases only where tests explicitly require backward reading; do not compute trust in `integrity.py`.

- [ ] **Step 5: Run focused and full regressions**

Run: `uv run pytest -q tests/test_integrity.py tests/test_adapter.py tests/test_artifacts.py`

Expected: PASS.

Run: `uv run pytest -q`

Expected: all tests PASS.

- [ ] **Step 6: Commit semantic integrity**

```bash
git add src/project_knowledge/integrity.py src/project_knowledge/adapter.py src/project_knowledge/artifacts.py tests/test_integrity.py tests/test_adapter.py tests/test_artifacts.py
git commit -m "feat: validate graphs with adapter invariants"
```

### Task 3: Implement the versioned 0.9.48 adapter against the reviewed fixture

**Files:**
- Create: `src/project_knowledge/adapters/__init__.py`
- Create: `src/project_knowledge/adapters/base.py`
- Create: `src/project_knowledge/adapters/graphify_0_9_48.py`
- Test: `src/project_knowledge/compatibility_fixtures/graphify_0_9_48.json`
- Test: `scripts/capture-graphify-compatibility-fixture`
- Test: `tests/fixtures/graphify/0.9.48/source/fixture.py`
- Create: `tests/test_graphify_0_9_48_adapter.py`

**Interfaces:**
- Consumes: `GraphifyCompatibility`, `GraphSemantics`, `GraphIntegrity`, `analyze_graph()`, and `canonical_final_edge_id()`.
- Produces: every adapter symbol in the stable interface ledger and `adapter_for()` resolution for `graphify-0.9.48`.

- [ ] **Step 1: Verify the deterministic fixture source and write failing adapter tests**

Keep the Task 1 code-only corpus byte-for-byte; it produces internal calls plus external-import dangling endpoints without credentials or generated prose:

```python
# tests/fixtures/graphify/0.9.48/source/fixture.py
from dataclasses import dataclass


@dataclass(frozen=True)
class Request:
    name: str


def normalize(request: Request) -> str:
    return request.name.strip().casefold()


def dispatch(request: Request) -> str:
    return normalize(request)
```

Test the packaged fixture is canonical JSON, contains an actual Graphify `edges` extraction artifact, contains only the allowlisted diagnostic summary, has no absolute path/source sample/context leak, and hashes to the registry entry's fixture digest:

```python
def test_packaged_fixture_is_safe_real_0948_output() -> None:
    payload = resources.files("project_knowledge.compatibility_fixtures").joinpath(
        "graphify_0_9_48.json"
    ).read_bytes()
    document = json.loads(payload)
    assert document["schema_version"] == 1
    assert document["graphify_version"] == "0.9.48"
    assert document["native_graph"]["edges"]
    assert "producer_suppression" not in document["diagnosis"]
    assert "examples" not in document["diagnosis"]
    assert "input_path" not in json.dumps(document["diagnosis"])
    assert b"/Users/" not in payload and b"/home/" not in payload and b"/tmp/" not in payload
    assert_no_absolute_posix_or_windows_strings(document)
    contract = resolve_graphify_compatibility("0.9.48")
    assert hashlib.sha256(payload).hexdigest() == contract.compatibility_fixture_digest
```

Also assert `adapter_for()` resolves every registry `adapter_id` exactly once and that its internal adapter-factory keys equal `{resolve_graphify_compatibility(v).adapter_id for v in supported_graphify_versions()}`—no orphan adapter and no second version-keyed allowlist.

- [ ] **Step 2: Run the adapter test and verify the missing package failure**

Run: `uv run pytest -q tests/test_graphify_0_9_48_adapter.py`

Expected: collection fails with `ModuleNotFoundError: No module named 'project_knowledge.adapters'`.

- [ ] **Step 3: Implement immutable descriptor capture and strict native parsing**

`capture_native_artifact()` must open a regular file with `O_NOFOLLOW`, compare pre-open and `fstat()` identity, reject size above the supplied cap before/while reading, read to EOF in bounded chunks, compare a final `fstat()`, and return bytes plus a confined logical path/digest. Provide a pure constructor for adapter-produced bytes:

```python
@classmethod
def from_payload(cls, logical_path: PurePosixPath, payload: bytes) -> "CapturedArtifact":
    _require_logical_path(logical_path)
    return cls(
        logical_path=logical_path,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_length=len(payload),
    )
```

Parse native JSON with duplicate-key and non-finite-number rejection. For 0.9.48 post-dedup input require exactly one `edges` array, non-empty `nodes`, object entries, and the registry's native schema fingerprint. Reject wrapper metadata and `links` at this phase.

- [ ] **Step 4: Write failing normalization/quarantine regression tests**

Cover the real fixture and focused mutations:

```python
def test_normalization_quarantines_external_import_endpoints() -> None:
    adapter = adapter_for(resolve_graphify_compatibility("0.9.48"))
    graph = adapter.parse_post_dedup(native_fixture())
    result = adapter.normalize_for_cluster(graph)
    normalized = json.loads(result.cluster_input.payload)
    node_ids = {node["id"] for node in normalized["nodes"]}
    assert all(
        edge["source"] in node_ids and edge["target"] in node_ids
        for edge in normalized["edges"]
    )
    assert dict((item.code, item.count) for item in result.quarantines)[
        "dangling_endpoint"
    ] >= 1


def test_normalization_repairs_only_a_unique_exact_alias() -> None:
    artifact = native_artifact(
        nodes=[
            {"id": "module:a", "label": "a"},
            {"id": "function:run", "label": "run"},
        ],
        edges=[{"source": "a", "target": "function:run", "relation": "calls"}],
    )
    result = adapter().normalize_for_cluster(adapter().parse_post_dedup(artifact))
    edge = json.loads(result.cluster_input.payload)["edges"][0]
    assert edge["source"] == "module:a"
    assert result.repairs == (ReasonCount("unique_exact_node_alias", 1),)


def test_ambiguous_alias_is_quarantined_not_guessed() -> None:
    artifact = native_artifact(
        nodes=[{"id": "a:run", "label": "run"}, {"id": "b:run", "label": "run"}],
        edges=[{"source": "run", "target": "a:run", "relation": "calls"}],
    )
    result = adapter().normalize_for_cluster(adapter().parse_post_dedup(artifact))
    assert json.loads(result.cluster_input.payload)["edges"] == []
    assert result.quarantines == (ReasonCount("ambiguous_endpoint_alias", 1),)
```

Also test missing/non-string endpoints, exact IDs taking priority over aliases, alias cycles being impossible, empty source-path sentinels removed, and source aliases canonicalized only against `staged_files` in the final graph.

- [ ] **Step 5: Run normalization tests and confirm unresolved endpoints remain**

Run: `uv run pytest -q tests/test_graphify_0_9_48_adapter.py -k 'normalization or alias or endpoint'`

Expected: FAIL because the adapter methods are not implemented.

- [ ] **Step 6: Implement deterministic normalization and final adaptation**

Build the alias index only from non-empty exact `label` and `norm_label` strings. For each endpoint apply this ordered rule set:

1. exact node ID: keep;
2. missing/non-string/empty: quarantine `missing_endpoint`;
3. exactly one alias target: rewrite and count `unique_exact_node_alias`;
4. more than one alias target: quarantine `ambiguous_endpoint_alias`;
5. otherwise: quarantine `dangling_endpoint`.

Never add a synthetic node. Sort reason counts by code, preserve kept-edge input order, canonicalize output JSON, and compute observed integrity from the unmodified post-dedup graph.

`adapt_clustered_graph()` accepts `links` (not `edges`) from `cluster-only`, removes only empty path sentinels, canonicalizes the known 0.9.48 node path alias, assigns `atlasweaver_edge_id = canonical_final_edge_id(edge, semantics)` to every final edge, calls `validate_final_graph()`, and fails on any invalid structure. It must not carry native `context` into evidence; the public graph may retain Graphify navigation fields only if the existing artifact path/literal validator accepts them.

- [ ] **Step 7: Re-run fixture consistency and adapter regressions**

Run the committed capture tool in normal registry mode to a validated `mktemp` output, compare it byte-for-byte with `src/project_knowledge/compatibility_fixtures/graphify_0_9_48.json`, and delete only that explicit temporary file. Assert its SHA-256 equals the reviewed registry literal. This task tests the Task 1 owner; it does not recreate or rewrite the fixture/script.

Run: `uv run pytest -q tests/test_graphify_0_9_48_adapter.py tests/test_compatibility.py tests/test_integrity.py`

Expected: PASS.

Run: `uv run pytest -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit the versioned adapter**

```bash
git add src/project_knowledge/adapters tests/test_graphify_0_9_48_adapter.py
git commit -m "feat: add Graphify 0.9.48 adapter evidence boundary"
```

### Task 4: Bind the complete extraction invocation and build strict graph evidence

**Files:**
- Create: `src/project_knowledge/evidence.py`
- Create: `tests/test_evidence.py`
- Modify: `src/project_knowledge/secrets_scan.py`
- Test: `tests/test_secrets_scan.py`
- Modify: `src/project_knowledge/adapters/graphify_0_9_48.py`
- Test: `tests/test_graphify_0_9_48_adapter.py`

**Interfaces:**
- Consumes: `RenderedCommand`, `GraphifyCompatibility`, `CapturedArtifact`, `NormalizationResult`, `GraphIntegrity`, and `validate_final_graph()`.
- Produces: every symbol in the `project_knowledge.evidence` ledger, including the sole-source exact 64 MiB `GRAPH_EVIDENCE_MAX_BYTES = 67_108_864` cap imported by artifact validation/bundling.

- [ ] **Step 1: Write failing extraction-invocation binding tests**

Prove every orchestrator input changes the digest while private path spellings and secret values cannot enter the payload:

```python
def test_invocation_digest_binds_contract_without_private_paths() -> None:
    invocation = build_extraction_invocation(
        contract(),
        executable_sha256="a" * 64,
        capability_smoke_digest="9" * 64,
        commands=rendered_commands(),
        backend="openai",
        model="gpt-4.1-mini",
        configuration_sha256="b" * 64,
        source_digest="c" * 64,
        projection_digest="d" * 64,
        environments=(
            CommandEnvironmentBinding(
                "extract", ("HOME", "LANG", "LC_ALL", "OPENAI_API_KEY", "PATH")
            ),
            CommandEnvironmentBinding(
                "diagnose", ("HOME", "LANG", "LC_ALL", "PATH")
            ),
            CommandEnvironmentBinding(
                "cluster", ("HOME", "LANG", "LC_ALL", "PATH")
            ),
        ),
        artifacts=(
            native_graph(), diagnosis(), cluster_input(),
            clustered_graph(), clustered_report(),
        ),
    )
    assert len(invocation.digest) == 64
    serialized = invocation.payload
    assert b"<staged-root>" in serialized
    assert b"/Users/" not in serialized
    assert b"sk-live-secret" not in serialized


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("executable_sha256", "e" * 64),
        ("capability_smoke_digest", "8" * 64),
        ("model", "other-model"),
        ("configuration_sha256", "f" * 64),
        ("source_digest", "1" * 64),
        ("projection_digest", "2" * 64),
        ("environments", (
            CommandEnvironmentBinding("extract", ("HOME", "PATH")),
            CommandEnvironmentBinding("diagnose", ("HOME", "PATH")),
            CommandEnvironmentBinding("cluster", ("HOME", "PATH")),
        )),
    ],
)
def test_invocation_mutation_changes_digest(field: str, replacement: object) -> None:
    original = invocation_factory()
    changed = invocation_factory(**{field: replacement})
    assert changed.digest != original.digest
```

Define `invocation_factory(**overrides)` in the test module as a complete call to `build_extraction_invocation()` with the exact base arguments shown in the first test, replacing only named keyword values; it must not use `dataclasses.replace()` because the digest must be recomputed.

Also test duplicate logical artifact paths, missing/duplicate/reordered command-environment bindings, unsorted environment names, a selected-backend name admitted to diagnose/cluster, names from another backend, non-registry environment names, duplicate commands, wrong command order, a non-canonical argv path, digest/length mismatch, booleans as lengths, and a command rendered for another adapter. Positive regressions cover OpenAI's endpoint name, Ollama's host/base-url names, and either/both Gemini credential aliases on extract; each admitted name changes the invocation digest while no value is serialized.

- [ ] **Step 2: Run invocation tests and verify the missing module failure**

Run: `uv run pytest -q tests/test_evidence.py -k invocation`

Expected: collection fails with `ModuleNotFoundError: No module named 'project_knowledge.evidence'`.

- [ ] **Step 3: Implement canonical invocation construction**

Use domain-separated canonical UTF-8 JSON:

```python
_INVOCATION_DOMAIN = b"atlasweaver-graphify-pipeline-v1\0"

payload = {
    "schema_version": 1,
    "adapter_id": contract.adapter_id,
    "graphify_version": contract.version,
    "executable": {"sha256": executable_sha256, "version": contract.version},
    "capability_smoke_digest": capability_smoke_digest,
    "argv": [
        {"operation": command.operation, "items": list(command.canonical_argv)}
        for command in commands
    ],
    "backend": backend,
    "model": model,
    "configuration_sha256": configuration_sha256,
    "source_digest": source_digest,
    "projection_digest": projection_digest,
    "environments": [
        {"operation": item.operation, "names": list(item.names)}
        for item in environments
    ],
    "artifacts": [
        {"path": item.logical_path.as_posix(), "sha256": item.sha256, "byte_length": item.byte_length}
        for item in artifacts
    ],
}
digest = hashlib.sha256(_INVOCATION_DOMAIN + _canonical_json(payload)).hexdigest()
```

Require commands and environment bindings in exact `(extract, diagnose,
cluster)` order and compare each canonical argv with a fresh registry render
using logical placeholders. Each name tuple is sorted/unique. Extract contains
the fixed base names plus exactly the non-empty subset of the selected
`BackendContract.admitted_environment` that the orchestrator passed (including
registry-declared endpoint/alias names); code-only contains only the base
names. A semantic backend that is not credentialless must include at least one
name from its `credential_environment`; credentialless backends may omit it.
Diagnose and no-label cluster must have exactly the base names. Artifact
bindings are sorted by logical path and must include exactly `raw/graph.json`,
`raw/diagnose.json`, `cluster-input/graph.json`, `clustered/graph.json`,
`clustered/GRAPH_REPORT.md`, plus `clustered/graph.html` only when requested.
Never serialize actual argv or environment values.

- [ ] **Step 4: Write failing evidence capture, strict-parser, and sanitization tests**

Build evidence from the real fixture and assert the observed-versus-unknown split:

```python
def test_0948_evidence_is_observed_post_dedup_and_never_complete() -> None:
    evidence = build_fixture_evidence()
    assert evidence.observed_post_dedup.node_count > 0
    assert evidence.pre_dedup is None
    assert evidence.evidence_complete is False
    expected = {"pre_dedup_edge_projection_unavailable"}
    if evidence.normalization_quarantines:
        expected.add("raw_endpoint_unresolved")
    if any(
        edge.source_file is None or edge.source_line is None
        for edge in evidence.final_edges
    ):
        expected.add("impact_edge_provenance_incomplete")
    assert set(evidence.limitations) == expected
    document = json.loads(evidence.payload)
    assert document["pre_dedup"] is None
    assert document["observed_post_dedup"]["dangling_endpoint_edges"] >= 1
    assert document["normalization"]["quarantines"]
```

Parameterize strict parsing failures for duplicate JSON keys at every nesting depth, `NaN`, `Infinity`, `1e400`, unknown keys, wrong schema/adapter/version/digest, inconsistent counters, duplicate edge IDs, an absolute/escaping/backslash path, invalid line/column, unapproved confidence, a context/source-fragment field, payload one byte above `GRAPH_EVIDENCE_MAX_BYTES`, and diagnosis greater than `4_194_304` bytes. Unit-test the bounded-reader size guard directly at exactly the cap (accepted) and one byte above it (rejected before JSON allocation). Assert `GRAPH_EVIDENCE_MAX_BYTES == 67_108_864` and scan `src/project_knowledge` for any second production assignment.

Add three independent limitation derivation mutations: clean complete provenance produces only the registry capability limitation; adding one quarantine adds only `raw_endpoint_unresolved`; removing one final source line adds only `impact_edge_provenance_incomplete`. Assert sorted unique codes in every case.

- [ ] **Step 5: Run evidence tests and confirm missing builder/parser failures**

Run: `uv run pytest -q tests/test_evidence.py -k 'evidence or duplicate or path or size'`

Expected: FAIL because `build_graph_evidence()` and `parse_graph_evidence()` are absent.

- [ ] **Step 6: Implement diagnosis comparison and evidence generation**

Parse the official diagnostic capture strictly, accept only the allowlisted summary fields from Task 3, and compare its node/edge/missing/dangling/self-loop/exact-duplicate counters and `effective_directed is False` with adapter recomputation. A mismatch raises `EvidenceError("Graphify diagnosis does not match captured native graph")`.

Generate the schema with these exact top-level keys:

```python
document = {
    "schema_version": 1,
    "graphify_version": contract.version,
    "adapter_id": contract.adapter_id,
    "source_digest": source_digest,
    "projection_digest": projection_digest,
    "extraction_invocation_digest": invocation.digest,
    "extraction_invocation": json.loads(invocation.payload),
    "observed_post_dedup": observed.to_dict(),
    "pre_dedup": None,
    "normalization": {
        "repairs": [_reason_document(item) for item in normalization.repairs],
        "quarantines": [_reason_document(item) for item in normalization.quarantines],
    },
    "final_integrity": final_integrity.to_dict(),
    "final_edges": [_safe_final_edge(edge, contract.semantics) for edge in final_edges],
    "evidence_complete": False,
    "limitations": sorted(_derive_evidence_limitations(contract, normalization, final_edges)),
}
```

`_safe_final_edge()` permits only final ID, directed endpoints, relation, normalized confidence, confined `source_file`, and parsed `source_line/source_column`. Accept `L12`, `L12:4`, and `12:4`; normalize to integer fields. Missing provenance stays `null` and adds the stable limitation `impact_edge_provenance_incomplete`. Sort final evidence by `final_edge_id`; reject duplicate IDs.

- [ ] **Step 7: Implement the closed evidence parser and round-trip identity**

The parser must enforce `GRAPH_EVIDENCE_MAX_BYTES` while reading, then exact key sets recursively, plain integers but not booleans, finite numeric confidence only if a future adapter declares it, sorted unique limitations/reason counts/edge IDs, 64-character lowercase hex digests, confined POSIX paths, and registry capability consistency. A non-null `pre_dedup` list maps exactly to `PreDedupEdgeEvidence`: occurrence IDs are unique non-empty UTF-8 strings capped at 256 bytes; disposition is one of `kept/rewritten/dropped`; kept/rewritten require a final ID and no drop reason; dropped requires a reason from `contract.lineage_reason_codes` and no final ID. Re-serialize the parsed value canonically and require byte equality so alternate encodings cannot have the same semantic acceptance.

`ExtractionInvocation.payload` is canonical JSON of the invocation body without a self-digest; `digest` is domain-separated over those bytes. The evidence parser reconstructs it from the embedded closed object and requires the recomputed digest to equal the top-level `extraction_invocation_digest`. This lets later candidate/owned validators verify the orchestration binding rather than trusting an orphan 64-hex string. `GraphEvidence.payload` is the canonical bytes and `GraphEvidence.digest` is ordinary SHA-256 of those bytes. Do not include the evidence digest inside its own payload.

`parse_graph_evidence(payload, contract, *, expected_digest)` requires an external lowercase SHA-256 anchor. Enforce the byte cap, validate `expected_digest`, and compare it with SHA-256 of the exact payload before JSON parsing or semantic admission. Tests pass `built.digest` for a builder-produced descriptor; semantic-malformation tests independently hash their controlled fixture bytes only to reach post-anchor validation; and a canonical relation/source-path mutation must fail when paired with the original trusted digest. Production callers must pass the SHA-256 already bound by their captured payload descriptor or bundle artifact metadata, never hash an untrusted payload and present that result as trust.

`build_graph_evidence(..., staged_files=frozenset(...))` requires the exact staged projection path set. At this prerequisite phase, schema-v1 callers retain the unchanged legacy `GLOBAL_DENY_PATTERNS`/`is_denied` admission boundary. That rule is not the final schema-v2 boundary: Core Task 4 updates both evidence admission and `Graphify0948Adapter.adapt_clustered_graph()` to call the shared structured `classify_path(..., sensitive_source_suffixes=contract.sensitive_source_suffixes)` policy, reject only `deny`, and admit `scan` only for bytes already scanned upstream. Add the cross-phase regression that `src/credentials.py` survives evidence and final adaptation while `src/credentials/app.py`, `src/database-creds.json`, and every legacy private/runtime/data rule fail closed. The final descriptor is recomputed at `adapted/graph.json` from invocation-bound clustered bytes plus the policy-version-correct admitted staged set; v2 never reapplies the legacy sensitive leaf-name deny list.

- [ ] **Step 8: Run evidence and full regressions**

Run: `uv run pytest -q tests/test_evidence.py tests/test_graphify_0_9_48_adapter.py tests/test_integrity.py`

Expected: PASS.

Run: `uv run pytest -q`

Expected: all tests PASS.

- [ ] **Step 9: Commit invocation and evidence contracts**

```bash
git add src/project_knowledge/evidence.py tests/test_evidence.py
git commit -m "feat: bind Graphify extraction evidence"
```

### Task 5: Integrate evidence into candidate adaptation and non-circular ownership

**Files:**
- Modify: `src/project_knowledge/evidence.py`
- Modify: `src/project_knowledge/adapter.py`
- Modify: `src/project_knowledge/artifacts.py`
- Modify: `tests/test_evidence.py`
- Modify: `tests/test_adapter.py`
- Modify: `tests/test_artifacts.py`
- Modify: `tests/test_health.py`

**Interfaces:**
- Consumes: Tasks 1–4 plus Core Tasks 1–5 exports: `ProjectManifest.project_uid: UUID | None`; `ProjectionFile`; `ProjectionSnapshot`; `StagedInput.projection_digest: str | None`, `reason_counts`, `projection_files`, and `coverage_approvals`; and `CoverageApproval`. Artifact schema 2 requires non-null project UID/projection digest. The later Core Task 7 computes previous valid ownership epoch + 1 (or 1) and passes it to this plan's validator; this task does not own an epoch allocator.
- Produces: evidence-aware `adapt_candidate()`, `GitIdentity`, evidence-aware `validate_candidate()`, and `validate_owned_graph()` exactly as declared in the stable interface ledger.
- Amends the already landed Task 4 invocation schema before Core Task 7: replace the former pipeline-wide environment-name tuple with the exact three `CommandEnvironmentBinding` entries declared above. Extract contains the four fixed base names plus only the selected backend's non-empty `admitted_environment` subset (credential aliases and endpoint names included); diagnose and cluster contain exactly the four base names. The parser rejects the legacy shape, duplicate/reordered operations, any selected-backend name on either local operation, a name from another backend, or any unknown name. No environment value is serialized.

- [ ] **Step 0: Migrate the landed invocation schema to per-command environments**

Update `src/project_knowledge/evidence.py` and `tests/test_evidence.py` first. Add a regression that the same semantic extraction credential reaches only extract, that diagnose/cluster remain base-only, and that changing any one command binding changes the digest. Run: `uv run pytest -q tests/test_evidence.py -k 'invocation or environment'`. Expected: PASS before adaptation work starts.

- [ ] **Step 1: Write failing schema-2 adaptation tests**

Add a fixture that supplies a parsed 0.9.48 evidence document to `adapt_candidate()` and assert deterministic candidate contents:

```python
def test_evidenced_adapter_writes_schema2_graph_evidence_and_report(tmp_path: Path) -> None:
    result = adapt_candidate(
        clustered_candidate(tmp_path / "raw"),
        tmp_path / "adapted",
        staged(tmp_path),
        manifest(),
        evidence=graph_evidence(),
    )
    assert result.artifact_schema_version == 2
    assert result.projection_digest == graph_evidence().projection_digest
    assert result.evidence_digest == graph_evidence().digest
    assert result.extraction_invocation_digest == graph_evidence().extraction_invocation_digest
    assert len(result.generation_digest) == 64
    assert (result.root / "GRAPH_EVIDENCE.json").read_bytes() == graph_evidence().payload
    document = json.loads((result.root / "graph.json").read_text())
    assert document["artifact_schema_version"] == 2
    assert document["projection_digest"] == graph_evidence().projection_digest
    assert document["evidence_digest"] == graph_evidence().digest
    assert document["extraction_invocation_digest"] == graph_evidence().extraction_invocation_digest
    assert "impact_trust" not in document
    assert "impact_limitations" not in document
    assert "AtlasWeaver Integrity" in (result.root / "GRAPH_REPORT.md").read_text()
```

Test that evidence/output source digest, projection digest, adapter/version, final node/edge counters, final edge IDs, and invocation digest must match. Confirm raw Graphify bytes remain unchanged, the report appendix includes only counts/digests/limitations, and repeated adaptation with the same inputs is byte-identical. Confirm supplying `build_epoch` is impossible because it is not an adaptation parameter.

- [ ] **Step 2: Run adaptation tests and verify missing evidence support**

Run: `uv run pytest -q tests/test_adapter.py -k evidenced`

Expected: FAIL because `adapt_candidate()` does not accept `evidence` and does not emit `GRAPH_EVIDENCE.json`.

- [ ] **Step 3: Route final graph bytes through the versioned adapter**

Resolve the contract and adapter from `manifest.graphify_version`, capture clustered `graph.json`, call `adapter.adapt_clustered_graph(..., staged_files=frozenset(staged.files))`, and validate its final integrity. Continue copying only `GRAPH_REPORT.md` and policy-permitted `graph.html` from the raw candidate.

When evidence is supplied, require all bindings before writing, add these graph fields, and write exact evidence bytes as a new approved artifact:

```python
document.update({
    "artifact_schema_version": 2,
    "project_id": manifest.project_id,
    "graphify_version": contract.version,
    "source_digest": staged.source_digest,
    "projection_digest": evidence.projection_digest,
    "evidence_digest": evidence.digest,
    "extraction_invocation_digest": evidence.extraction_invocation_digest,
    "graph_health": evidence.final_integrity.to_dict(),
    "extraction_coverage": existing_coverage_document,
})
```

Append a deterministic Markdown section with native observed counts, normalization repair/quarantine reason counts, final counts, evidence digest, and the exact navigation limitation. Never copy diagnostic examples or private paths. With no evidence, retain artifact schema 1 compatibility and navigation trust.

- [ ] **Step 4: Write failing schema-2 candidate and ownership tests**

Cover non-circular ownership, epoch injection, optional Git identity, evidence tampering, and live validation:

```python
def test_validation_injects_epoch_only_into_non_circular_ownership(
    evidenced_candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
) -> None:
    before_graph = (evidenced_candidate / "graph.json").read_bytes()
    validated = validate_candidate(
        evidenced_candidate,
        staged,
        manifest,
        expected_projection_digest="2" * 64,
        expected_evidence_digest=graph_evidence().digest,
        build_epoch=7,
        git_identity=GitIdentity(commit_oid="a" * 40, algorithm="sha1"),
    )
    ownership = json.loads((evidenced_candidate / OWNERSHIP_MANIFEST).read_text())
    assert (evidenced_candidate / "graph.json").read_bytes() == before_graph
    assert ownership["schema_version"] == 2
    assert ownership["build_epoch"] == 7
    assert ownership["project_uid"] == str(manifest.project_uid)
    assert ownership["adapter_id"] == "graphify-0.9.48"
    assert ownership["graph_digest"] == validated.graph_digest
    assert ownership["generation_digest"] == validated.generation_digest
    assert ownership["git_commit_oid"] == "a" * 40
    assert ownership["git_commit_algorithm"] == "sha1"
    assert OWNERSHIP_MANIFEST not in ownership["artifacts"]
    assert ownership["artifacts"]["GRAPH_EVIDENCE.json"] == {
        "sha256": validated.evidence_digest,
        "byte_length": (evidenced_candidate / "GRAPH_EVIDENCE.json").stat().st_size,
    }


def test_owned_graph_validation_recomputes_every_digest(promoted_graph: Path) -> None:
    validated = validate_owned_graph(
        promoted_graph,
        manifest(),
        expected_source_digest="1" * 64,
        expected_projection_digest="2" * 64,
    )
    assert validated.build_epoch == 7
    (promoted_graph / "GRAPH_EVIDENCE.json").write_bytes(b"{}\n")
    with pytest.raises(ArtifactValidationError, match="artifact digest mismatch"):
        validate_owned_graph(promoted_graph, manifest())
```

Parameterize build epochs `None`, `0`, `-1`, `True`, and `1.5`; invalid SHA-1/SHA-256 length/hex; ownership self-hash; duplicate/non-finite ownership JSON; unknown ownership/artifact keys; missing evidence; evidence larger than cap; graph/evidence/invocation/projection mismatch; forged trust; missing artifact length; length mismatch; symlinked artifact; and mutation between capture and final snapshot.

Add a positive disjoint-digest test where a valid SHA-1 `git_commit_oid` deliberately differs from both 64-hex source/projection digests and validation succeeds; add negative tests only for Git algorithm/length/hex, never equality with safe-source digests.

- [ ] **Step 5: Run candidate tests and confirm ownership schema failures**

Run: `uv run pytest -q tests/test_artifacts.py -k 'evidence or ownership or epoch or owned_graph'`

Expected: FAIL because ownership is schema 1 and `validate_owned_graph()`/`GitIdentity` are absent.

- [ ] **Step 6: Implement evidence-aware candidate validation**

Detect artifact schema from graph metadata. For schema 2 require `GRAPH_EVIDENCE.json` and a non-null lowercase `expected_evidence_digest`, parse it through the registry adapter using exactly that external anchor, validate exact graph/evidence/source/projection/invocation bindings, recompute final integrity, compare the safe final-edge evidence index against graph IDs, and call:

```python
trust = decide_impact_trust(
    evidence,
    integrity,
    source_current=True,
    projection_current=True,
    coverage_complete=(unapproved_skips == 0 and skipped_count == 0),
    artifacts_bound=True,
)
```

Reject schema-2 graph documents containing `impact_trust`, `impact_limitations`, or legacy `impact_analysis_trusted`: candidate graph bytes are evidence inputs, never the trust authority. Require `build_epoch >= 1`, non-null `manifest.project_uid`, and optional valid Git identity only after all candidate artifacts pass. Keep schema-1 validation compatible, require `build_epoch is None`/`git_identity is None`, and force its validated result to navigation. A future capable adapter can therefore become trusted through `decide_impact_trust()` and ownership creation without rewriting graph bytes.

- [ ] **Step 7: Write ownership schema 2 without a self-hash**

Compute `generation_digest` from canonical UTF-8 JSON of the sorted approved
owned-root artifact documents `{path, sha256, byte_length}` (`graph.json`,
`GRAPH_REPORT.md`, `GRAPH_EVIDENCE.json`, optional `graph.html`), prefixed by
`b"atlasweaver-generation-v1\0"`; ownership is excluded. Use this wire shape,
omitting Git fields together when unavailable:

```python
ownership = {
    "schema_version": 2,
    "artifact_schema_version": 2,
    "project_id": manifest.project_id,
    "project_uid": str(manifest.project_uid),
    "graphify_version": contract.version,
    "adapter_id": contract.adapter_id,
    "source_digest": staged.source_digest,
    "projection_digest": evidence.projection_digest,
    "extraction_invocation_digest": evidence.extraction_invocation_digest,
    "evidence_digest": evidence.digest,
    "graph_digest": graph_digest,
    "generation_digest": generation_digest,
    "build_epoch": build_epoch,
    "impact_trust": trust.level,
    "impact_limitations": list(trust.limitations),
    "artifacts": {
        relative.as_posix(): {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_length": len(payload),
        }
        for relative, _, payload in artifact_files
    },
}
if git_identity is not None:
    ownership["git_commit_oid"] = git_identity.commit_oid
    ownership["git_commit_algorithm"] = git_identity.algorithm
```

Do not include `generated_at`, ownership filename in `artifacts`, or an ownership digest inside this payload. The existing process-local snapshot still binds the newly written ownership bytes so promotion cannot accept a forged object.

- [ ] **Step 8: Implement strict owned-live-graph validation**

`validate_owned_graph()` opens/captures the complete approved tree without following links, parses ownership first only to obtain the closed expected artifact set, verifies every digest and length, then performs the same graph/evidence/integrity/trust checks as candidate validation without writing anything. First recompute generation-time trust with the ownership-bound source/projection values and compare it to the stored ownership claim. Then compute returned current trust: an omitted expected source or projection digest is unverified and returns navigation with `source_digest_unverified` or `projection_digest_unverified`; an unequal value is stale; only supplied equal values can preserve trusted status. Absence therefore permits structural/ownership validation but never an affirmative current-impact claim. Reuse private validation helpers so candidate and live paths cannot drift.

Recompute `generation_digest` from the strict ownership-listed artifact set and
require equality with schema-2 ownership. Change schema-2 promotion no-op
identity from `graph_digest` to `generation_digest`; schema-1 compatibility may
derive the same value in memory from its closed artifact list. Add a regression
where graph bytes are unchanged but report/evidence bytes differ and promotion
must replace the generation, plus an exact-generation no-op regression that
leaves the installed ownership bytes and epoch untouched. On both paths,
construct `PromotionResult` from the descriptor-validated installed ownership,
not merely from the proposed candidate: `digest`, `generation_digest`, and
`build_epoch` describe what is durably installed when the function returns.
For schema 2, `build_epoch` is an exact non-boolean positive integer; schema 1
keeps it `None`. Assert these result fields in changed and no-op tests, including
a no-op candidate whose proposed epoch differs from the installed epoch.

Update health's existing owned-output inspection to call this validator for integrity/evidence facts while retaining health's own status classification. Legacy ownership stays accepted as navigation-only during the compatibility window.

- [ ] **Step 9: Run focused, promotion, health, and full regressions**

Run: `uv run pytest -q tests/test_adapter.py tests/test_artifacts.py tests/test_health.py`

Expected: PASS.

Run: `uv run pytest -q tests/test_cli.py tests/test_real_graphify_pipeline.py`

Expected: PASS; existing low-level schema-1 flow remains operational.

Run: `uv run pytest -q`

Expected: all tests PASS.

- [ ] **Step 10: Commit evidence-bound candidates**

```bash
git add src/project_knowledge/adapter.py src/project_knowledge/artifacts.py tests/test_adapter.py tests/test_artifacts.py tests/test_health.py
git commit -m "feat: validate evidence-bound graph ownership"
```

### Task 6: Finalize impact trust and the Atlas-native affected handoff

**Files:**
- Modify: `src/project_knowledge/evidence.py`
- Modify: `src/project_knowledge/adapters/graphify_0_9_48.py`
- Modify: `tests/test_evidence.py`
- Modify: `tests/test_graphify_0_9_48_adapter.py`
- Modify: `tests/test_artifacts.py`

**Interfaces:**
- Consumes: validated final graph IDs, `GraphEvidence`, and `GraphIntegrity`.
- Produces: `index_final_edge_evidence()` and `decide_impact_trust()` as the only evidence join/trust APIs consumed by the later core `affected` implementation.

- [ ] **Step 1: Write a complete trust-predicate mutation matrix**

Start from a synthetic future adapter contract with `pre_dedup_occurrences=True` and `total_transform_lineage=True`, complete one-to-one lineage, clean final graph, and complete provenance. Mutate every predicate independently:

```python
@pytest.mark.parametrize(
    "mutation,expected_limitation",
    [
        (lambda x: replace(x, evidence_complete=False), "impact_evidence_incomplete"),
        (lambda x: x, "source_digest_stale"),
        (lambda x: x, "projection_digest_stale"),
        (lambda x: x, "source_digest_unverified"),
        (lambda x: x, "projection_digest_unverified"),
        (lambda x: x, "scope_coverage_incomplete"),
        (lambda x: x, "artifact_binding_invalid"),
        (lambda x: replace(x, pre_dedup=None), "pre_dedup_edge_projection_unavailable"),
        (lambda x: with_unresolved_lineage(x), "raw_endpoint_unresolved"),
        (lambda x: with_unexplained_drop(x), "transform_lineage_incomplete"),
        (lambda x: with_missing_provenance(x), "impact_edge_provenance_incomplete"),
    ],
)
def test_each_predicate_independently_demotes_trust(mutation, expected_limitation) -> None:
    evidence, integrity, flags = trusted_fixture()
    evidence = mutation(evidence)
    flags = mutate_flag_for(expected_limitation, flags)
    result = decide_impact_trust(evidence, integrity, **flags)
    assert result.level == "navigation"
    assert expected_limitation in result.limitations
```

Add final-integrity mutations for missing/dangling endpoint, invalid self-loop, exact duplicate, conflicting relation, duplicate final-edge evidence IDs, missing graph edge ID, and evidence edge not present in the graph. Assert limitations are sorted, unique, stable codes.

- [ ] **Step 2: Run trust tests and verify optimistic current behavior fails**

Run: `uv run pytest -q tests/test_evidence.py -k trust`

Expected: FAIL until all trust predicates and stable limitations are implemented.

- [ ] **Step 3: Implement the single trust decision**

`decide_impact_trust()` begins with the evidence's own limitations, appends one stable code for every failed external predicate and structural invariant, and returns `trusted` only when the limitation set is empty. For source/projection state, `True` means verified current, `False` means verified stale, and `None` means unverified; stale and unverified get distinct stable codes. It never reads a manifest override. Before returning trusted, require:

- adapter capability has both complete pre-dedup occurrences and total lineage;
- every pre-dedup occurrence ID is unique;
- every occurrence disposition is `kept`, `rewritten`, or `dropped` with a declared reason;
- kept/rewritten entries point to an existing final edge ID;
- dropped entries have no final ID;
- every final edge has at least one lineage predecessor;
- every final edge evidence record has accepted confidence plus confined source path and source line;
- final structural counters are all zero and collapsed evidence is a known valid integer;
- source/projection/coverage/artifact-binding flags are true.

For adapter `graphify-0.9.48`, prepend `pre_dedup_edge_projection_unavailable` unconditionally from registry capability. Zero diagnosis counters cannot remove it.

- [ ] **Step 4: Implement the strict final-edge evidence index**

`index_final_edge_evidence()` returns `MappingProxyType` keyed by `final_edge_id`, rejects duplicates, and exposes only the safe dataclass fields. Add a helper test that models the later core reverse traversal without calling native Graphify:

```python
def test_affected_handoff_joins_graph_edges_by_stable_id() -> None:
    graph = final_graph_document()
    evidence = parsed_evidence()
    index = index_final_edge_evidence(evidence)
    traversed = [edge for edge in graph["links"] if edge["target"] == "service"]
    joined = [index[edge["atlasweaver_edge_id"]] for edge in traversed]
    assert all(item.relation and item.source and item.target for item in joined)
```

This is the compatibility half of Atlas-native `affected`: adapters guarantee final IDs and evidence joinability. The core plan owns bounded reverse traversal, source-verification wording, stable query envelope, and the rule that an empty navigation traversal is “no path found in this graph.” It must not call or parse Graphify 0.9.48 `affected` output.

- [ ] **Step 5: Prove 0.9.48 stays navigation-only end to end**

Construct a zero-observed-defect 0.9.48 evidence fixture, validate it into ownership, and assert `ValidatedGraph`, evidence decision, and ownership all say `navigation` with `pre_dedup_edge_projection_unavailable`. Assert the graph contains no authoritative trust field and no serialized artifact contains `"unaffected"`, `"no impact"`, or `"impact_analysis_trusted": true`.

- [ ] **Step 6: Run trust, candidate, and full regressions**

Run: `uv run pytest -q tests/test_evidence.py tests/test_graphify_0_9_48_adapter.py tests/test_artifacts.py`

Expected: PASS.

Run: `uv run pytest -q`

Expected: all tests PASS.

- [ ] **Step 7: Commit trustworthy impact handoff**

```bash
git add src/project_knowledge/evidence.py src/project_knowledge/adapters/graphify_0_9_48.py tests/test_evidence.py tests/test_graphify_0_9_48_adapter.py tests/test_artifacts.py
git commit -m "feat: make impact trust evidence-driven"
```

### Task 7: Build the bounded newest-release compatibility probe

**Files:**
- Create: `src/project_knowledge/compat_probe.py`
- Create: `tests/test_compat_probe.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: production registry contract, runtime smoke-probe mechanics, and packaged fixture source.
- Produces: `resolve_latest_stable_wheel()`, `run_upstream_probe()`, and `python -m project_knowledge.compat_probe --output <path>` for scheduled CI only.

- [ ] **Step 1: Write failing bounded PyPI resolver tests**

Use local JSON fixtures/fake HTTP responses; tests never depend on live PyPI:

```python
def test_resolver_selects_highest_stable_non_yanked_compatible_wheel() -> None:
    document = pypi_document({
        "0.9.48": [wheel("graphifyy-0.9.48-py3-none-any.whl")],
        "0.9.49rc1": [wheel("graphifyy-0.9.49rc1-py3-none-any.whl")],
        "0.9.49": [wheel("graphifyy-0.9.49-py3-none-any.whl", yanked=True)],
        "0.9.50": [sdist("graphifyy-0.9.50.tar.gz")],
        "0.9.51": [wheel("graphifyy-0.9.51-py3-none-any.whl")],
    })
    assert str(resolve_latest_stable_wheel(document, frozenset(sys_tags()))) == "0.9.51"


def test_pypi_fetch_rejects_oversize_redirect_and_wrong_content_type() -> None:
    with pytest.raises(ProbeError, match="pypi_response_too_large"):
        fetch_pypi_document(fake_response(b"x" * 2_097_153))
    with pytest.raises(ProbeError, match="pypi_redirect_forbidden"):
        fetch_pypi_document(fake_response(b"{}", status=302, location="https://example.com/x"))
    with pytest.raises(ProbeError, match="pypi_content_type_invalid"):
        fetch_pypi_document(fake_response(b"{}", content_type="text/html"))
```

Also cover duplicate JSON keys, non-finite numbers, no releases, invalid PEP 440 versions, prereleases/dev/local versions, yanked files, sdist-only releases, malformed wheel filenames, incompatible interpreter/ABI/platform tags, a boolean `yanked`, timeout, TLS/HTTP error, short read, every 3xx status, and the exact fixed host/path `pypi.org:443` plus `/pypi/graphifyy/json`. Set `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, `NO_PROXY`, `NETRC`, and cookie-related ambient values in a test and prove the injected direct connection receives none of them.

- [ ] **Step 2: Run resolver tests and verify the missing module failure**

Run: `uv run pytest -q tests/test_compat_probe.py -k resolver`

Expected: collection fails with `ModuleNotFoundError: No module named 'project_knowledge.compat_probe'`.

- [ ] **Step 3: Add the pinned PEP 440/wheel parser and bounded fetcher**

Add `packaging==25.0` to the dev dependency group and run `uv lock`. Use `packaging.version.Version`, `packaging.utils.parse_wheel_filename`, and `packaging.tags.Tag`; do not implement a partial version parser.

The fetcher uses an injected direct-connection factory in tests and, in production, `http.client.HTTPSConnection("pypi.org", 443, timeout=15, context=ssl.create_default_context())`. It sends a fixed `GET /pypi/graphifyy/json` request with `Accept: application/json` and `Accept-Encoding: identity`; it does not use `urllib`, ambient proxy variables, netrc, cookies, auth handlers, or redirects. Require status 200 (all 3xx are `pypi_redirect_forbidden` without following `Location`), `application/json`, `Content-Length <= 2_097_152` when present, and stop after reading `2_097_153` bytes. Always close response/connection. Parse strict JSON and return a mapping. Resolver iterates `releases`, requires a stable `Version`, and accepts a release only when at least one non-yanked `.whl` has a tag intersecting `packaging.tags.sys_tags()`.

- [ ] **Step 4: Write failing probe report outcome tests**

Model success, changed surface, installation/network failure, and already-supported newest release:

```python
def test_upstream_candidate_never_declares_support(tmp_path: Path) -> None:
    report = run_upstream_probe(
        candidate_version=Version("0.9.99"),
        output=tmp_path / "report.json",
        installer=successful_fake_installer(changed_cluster_schema=True),
    )
    assert report["candidate_version"] == "0.9.99"
    assert report["support_declared"] is False
    assert report["outcome"] == "incompatible"
    assert report["limitations"] == ["clustered_schema_mismatch"]


def test_network_failure_is_inconclusive_and_reported(tmp_path: Path) -> None:
    result = main(["--output", str(tmp_path / "report.json")], fetcher=timeout_fetcher)
    assert result == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["outcome"] == "inconclusive"
    assert report["limitations"] == ["pypi_request_failed"]
```

Assert every report uses a closed schema, canonical JSON, stable sorted limitations, no path/environment/raw stderr, and `support_declared` is true only when the selected version already exists in `supported_graphify_versions()`—never because a probe matched.

- [ ] **Step 5: Implement isolated installation and official smoke probing**

`run_upstream_probe()` creates one private temporary root, runs the pinned uv executable with argv only:

```python
(uv, "venv", str(venv), "--python", sys.executable)
(uv, "pip", "install", "--python", str(venv_python), f"graphifyy=={candidate_version}")
```

It locates the venv Graphify executable without PATH search, freezes it with `resolve_graphify_executable(test_override=venv_graphify)`, and executes the same official seven-operation capability sequence as runtime admission with mandatory pre-spawn identity revalidation. It compares observed required command names, operational mutations, and three fingerprints against the production contract. It records candidate executable/package version, not a support alias. A resolver/install/process/network failure is `inconclusive`; `graphify_executable_changed` is an executed `incompatible` result; any other executed incompatible surface is `incompatible`; a matching surface is `compatible` but still `support_declared=false` unless already in the registry. All subprocess errors pass through capped redaction.

The uv install boundary uses the existing process-group/capped-output runner with a fixed 120-second install timeout and 30-second Graphify-command timeout—never raw unbounded `subprocess.run`. Its environment contains only a temporary empty `HOME`, fixed locale/PATH, `UV_NO_CONFIG=1`, and `UV_INDEX_URL=https://pypi.org/simple`; strip all proxy, credential, netrc, pip, cloud, and Graphify backend variables. No repository directory is mounted/copied into the probe except the packaged smoke source.

The CLI writes its report atomically to the explicit output path and exits 0 for `compatible`, `incompatible`, or expected `inconclusive` outcomes so the workflow can always upload it. Invalid arguments, unsafe output path, or inability to write the report exit nonzero.

- [ ] **Step 6: Run probe and full regressions**

Run: `uv run pytest -q tests/test_compat_probe.py tests/test_compatibility.py tests/test_graphify_adapter.py`

Expected: PASS.

Run: `uv run pytest -q`

Expected: all tests PASS.

- [ ] **Step 7: Commit the compatibility probe**

```bash
git add pyproject.toml uv.lock src/project_knowledge/compat_probe.py tests/test_compat_probe.py
git commit -m "feat: probe upstream Graphify compatibility"
```

### Task 8: Enforce compatibility in CI and prove the real evidence pipeline

**Files:**
- Create: `.github/workflows/graphify-compatibility.yml`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_public_release.py`
- Modify: `tests/test_real_graphify_pipeline.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: production registry version, packaged fixtures, official Graphify pipeline, evidence-aware adaptation/validation.
- Produces: push/PR supported-version verification, scheduled read-only upstream report, and a complete real 0.9.48 navigation-evidence regression.

- [ ] **Step 1: Write failing workflow contract tests**

Parse both workflow files and assert exact security/immutability properties:

```python
def test_compatibility_workflow_is_read_only_and_digest_pinned() -> None:
    text = Path(".github/workflows/graphify-compatibility.yml").read_text()
    assert "contents: read" in text
    assert "pull-requests: write" not in text
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in text
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in text
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in text
    assert "uv==0.8.14" in text
    assert "project_knowledge.compat_probe" in text
    assert "pull_request_target" not in text
    assert "gh pr" not in text and "git push" not in text
```

Add tests that no workflow contains `actions/*@vN`, production Graphify installation calls `production_graphify_compatibility().version` rather than a YAML literal, scheduled upload uses `if: always()`, report retention is exactly 14 days, and workflow/package permissions contain no write scope.

- [ ] **Step 2: Run workflow tests and verify missing/unpinned workflow failures**

Run: `uv run pytest -q tests/test_public_release.py -k workflow`

Expected: FAIL because the compatibility workflow is absent and current actions/uv/Graphify are unpinned or duplicated.

- [ ] **Step 3: Pin the primary CI workflow and resolve production Graphify from code**

Change checkout/setup-python uses to the exact SHAs in Global Constraints. Install `uv==0.8.14`. Resolve Graphify without a second allowlist:

```bash
uv sync --frozen --dev
GRAPHIFY_VERSION="$(uv run python -c 'from project_knowledge.compatibility import production_graphify_compatibility as p; print(p().version)')"
uv tool install "graphifyy==$GRAPHIFY_VERSION"
graphify --version
```

Run `uv run pytest -q tests/test_compatibility.py tests/test_graphify_0_9_48_adapter.py tests/test_evidence.py` before the full suite so registry/fixture drift fails quickly.

- [ ] **Step 4: Add the supported/scheduled compatibility workflow**

Use `push`, `pull_request`, and cron `23 3 * * 2`. The ordinary job installs pinned uv, syncs the lock, and runs every declared adapter against its packaged fixture through `pytest`; tests enumerate `supported_graphify_versions()` so the YAML has no version matrix literal.

The scheduled-only job uses Python 3.13, `permissions: contents: read`, runs:

```bash
uv run python -m project_knowledge.compat_probe \
  --uv "$(command -v uv)" \
  --output graphify-compatibility-report.json
```

Then upload exactly that JSON as artifact name `graphify-upstream-compatibility`, retention 14 days, with the pinned upload-artifact SHA and `if: always()`. It must not checkout with credentials for a write, create/edit a branch, publish a package, or invoke GitHub APIs.

- [ ] **Step 5: Write the failing real evidence-pipeline test**

Extend the real test to run the exact sequence independent of the later lifecycle CLI:

1. stage the safe source;
2. render/run code-only extract/no-cluster;
3. descriptor-capture raw graph and diagnose JSON;
4. normalize and write private cluster input;
5. render/run cluster/no-label/no-viz;
6. capture final graph/report and build invocation/evidence;
7. adapt with evidence;
8. validate with `expected_projection_digest=staged.projection_digest`, `expected_evidence_digest=evidence.digest`, `build_epoch=1`;
9. acquire `repository_lifecycle_lock(repo)`, capture its
   `RepositoryAccess` with `capture_lifecycle_repository(repo)`, promote through
   `promote_graph(..., repository_access=repository)`, and validate the owned
   live graph through that same descriptor authority.

The assertions are exact:

```python
assert validated.artifact_schema_version == 2
assert validated.impact_trust == "navigation"
assert "pre_dedup_edge_projection_unavailable" in validated.impact_limitations
if evidence.normalization_quarantines:
    assert "raw_endpoint_unresolved" in validated.impact_limitations
assert validated.build_epoch == 1
assert (candidate / "GRAPH_EVIDENCE.json").is_file()
with repository_lifecycle_lock(repo), capture_lifecycle_repository(
    repo
) as repository:
    promote_graph(validated, repo, repository_access=repository)
    owned = validate_owned_graph(
        repo / "graphify-out",
        manifest,
        expected_source_digest=staged.source_digest,
        expected_projection_digest=staged.projection_digest,
        repository_access=repository,
    )
assert owned.evidence_digest == validated.evidence_digest
```

Also assert the raw captured bytes did not change after cluster input creation, every final edge has a valid deterministic ID, all unresolved native edges are represented in reason-coded quarantine counts, and the report never equates native counts with final counts.

- [ ] **Step 6: Run the real pipeline test and fix only integration defects**

Run: `uv run pytest -q tests/test_real_graphify_pipeline.py`

Expected before wiring: FAIL because the existing test skips diagnosis/evidence. After the exact evidence sequence is wired: PASS with installed Graphify 0.9.48, or SKIP only when that exact executable is unavailable.

- [ ] **Step 7: Document the trust boundary and adapter contribution workflow**

Update README with:

- registry as the sole support authority;
- exact 0.9.48 official pipeline;
- why 0.9.48 is useful but navigation-only;
- `GRAPH_EVIDENCE.json`/ownership bindings;
- how a new adapter requires official fixtures, complete lineage capability, fixture digest, compatibility tests, and reviewed code;
- scheduled probe outcomes do not declare support.

Add an unreleased changelog section naming the registry, evidence artifact, endpoint quarantine, deterministic edge IDs, and CI probe. Do not claim trusted impact for 0.9.48.

- [ ] **Step 8: Run all verification gates**

Run: `uv run pytest -q`

Expected: all tests PASS.

Run: `uv run python -m compileall -q src tests`

Expected: exit 0.

Run: `uv build`

Expected: source distribution and wheel build successfully, and the wheel contains `project_knowledge/compatibility_fixtures/runtime_probe.py` plus `graphify_0_9_48.json`.

Run: `git diff --check`

Expected: exit 0 with no whitespace errors.

- [ ] **Step 9: Commit compatibility CI and documentation**

```bash
git add .github/workflows/ci.yml .github/workflows/graphify-compatibility.yml tests/test_public_release.py tests/test_real_graphify_pipeline.py README.md CHANGELOG.md
git commit -m "ci: enforce Graphify compatibility evidence"
```

## Final Cross-Plan Acceptance Contract

Before handing off to the core and artifact/fleet plans, verify all of these are true:

- `resolve_graphify_compatibility()` is the only accepted-version resolver and all required CLI commands/backend credentials/coverage reasons live in its entry.
- Runtime admission resolves one immutable launcher identity, revalidates it immediately before every official version/help/extract/diagnose/cluster/global-add/agent-install subprocess, and matches three schema fingerprints; help text alone cannot admit a binary and launcher drift always fails as `graphify_executable_changed` before spawn.
- Registry rendering emits only the canonical role tokens documented in the interface ledger and rejects every forbidden native flag.
- The captured 0.9.48 graph is explicitly post-dedup; evidence `pre_dedup` is null and its limitation cannot be removed by zero counters.
- Normalization preserves an immutable raw descriptor, repairs only a unique exact alias, and quarantines every unresolved endpoint before cluster input.
- Final graphs contain recomputable deterministic edge IDs and pass adapter-declared structural invariants.
- Evidence parser/builder round-trip canonical bytes and reject malformed, oversized, unsafe, contradictory, or unbound content.
- Schema-2 adaptation is deterministic and contains no epoch/Git clock state; validation injects epoch/Git identity only into non-circular ownership.
- `validate_owned_graph()` is read-only and recomputes every artifact/evidence/ownership binding for later query and artifact consumers.
- `index_final_edge_evidence()` is the only affected edge-join contract and `decide_impact_trust()` is the only trust decision.
- Real Graphify 0.9.48 candidate, ownership, and affected handoff all remain navigation-only.
- CI uses no duplicate Graphify version literal, no floating action ref, no unpinned uv/packaging resolver, and no scheduled write permission.
