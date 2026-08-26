# AtlasWeaver Artifact Distribution and Universal Fleet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Distribute exact privacy-safe AtlasWeaver graph generations as deterministic, provenance-verified bundles and operate arbitrary multi-repository fleets through one bounded, project-agnostic interface.

**Architecture:** Packing descriptor-captures a validated owned generation while holding the repository lifecycle lock, then writes a closed-schema `ZIP_STORED` archive from that immutable snapshot. Local install and GitHub Release pull share one strict archive parser and the existing candidate validator/promotion transaction; network resolution and attestation can authorize bytes but can never validate or promote them. Fleet code validates an external workspace manifest and delegates every project operation to the core lifecycle/registry APIs with bounded parallelism, stable ordering, and no cross-project rollback.

**Tech Stack:** Python 3.10+, stdlib `zipfile`/`struct`/`http.client`/`ssl`/`subprocess`/`concurrent.futures`/`importlib.resources`, PyYAML 6.x strict loading, pytest 8/9, Graphify `0.9.48`, GitHub REST API, GitHub CLI artifact attestation, GitHub Actions reusable workflows.

**Spec:** `docs/superpowers/specs/2026-08-26-atlasweaver-artifact-fleet-design.md`

## Global Constraints

- This plan executes third: the compatibility/impact plan and core-adoption plan are implementation prerequisites.
- No product-specific names, defaults, repository layout, or business logic may enter source, configuration, workflows, documentation, or fixtures.
- Bundle schema v1 contains only `artifact.json`, `graphify-out/graph.json`, `graphify-out/GRAPH_REPORT.md`, required `graphify-out/GRAPH_EVIDENCE.json`, and policy-permitted `graphify-out/graph.html`.
- Bundle v1 uses sorted entries, fixed timestamps/permissions, `ZIP_STORED`, canonical UTF-8 JSON, no links, no ZIP64, and no host-clock input during pack.
- Archive size is capped at 256 MiB total, 255 MiB aggregate entry payload, and 128 MiB per entry; no manifest or CLI input can raise these limits.
- Public `artifact install` accepts only `provider: none`; a `github-release` bundle is installable only by the same-process verified `pull` path.
- GitHub Release v1 supports only `github.com`, at most three HTTPS redirects, and only `api.github.com`, `github.com`, `objects.githubusercontent.com`, and `release-assets.githubusercontent.com`.
- Authorization is sent only to `api.github.com` and is stripped before every cross-host redirect; ambient proxies, netrc, cookies, and redirect-auth state are ignored.
- Publication is workflow-only: no local `publish` command is added.
- The rolling tag is `atlasweaver-graph-<project-uid>-<channel>` and the asset is `atlasweaver-graph-<project-uid>-<source-digest>-<projection-digest>-<bundle-sha256>.zip` with full lowercase SHA-256 digests. The archive component prevents bundles that differ only in Git/build/tool transport metadata from colliding; `artifact.json` independently binds the non-ownership generation digest.
- A reusable publish runs only for a protected branch, exact manifest `source_ref`, exact clean `HEAD == github.sha`, and configured immutable `github.repository_id`.
- The publish workflow retains the 20 most recent digest-addressed assets and deletes nothing until the new asset's remote size and digest have been verified.
- Initial agent platforms are exactly `codex` and `agents`; hooks remain separate and ordinary package installation never installs them.
- Fleet configuration is project-agnostic; `max_parallel` defaults to 2 and is confined to the inclusive range 1 through 8.
- Fleet workspace YAML and each descriptor-captured project manifest are capped at 256 KiB before parsing.
- The only registry key is `atlasweaver/<project_uid>`; `project_id` is display/selection data and never a registry identity.
- Fleet output order follows workspace configuration order, never task completion order; one project's failure never rolls back another completed repository.
- Read-only local commands never persist telemetry. AtlasWeaver has no automatic telemetry endpoint and never records source labels, query text, denied filenames, environment values, or secret fingerprints.
- Every behavior is implemented test-first; every task ends in focused green tests and an independent commit.
- All action references use immutable full commit SHAs. The initial reviewed pins are `actions/checkout@11d5960a326750d5838078e36cf38b85af677262`, `actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065`, `astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e`, `actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02`, `actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093`, and `actions/attest-build-provenance@e8998f949152b193b063cb0ec769d69d929409be`.

## Prerequisite interface ledger

Compatibility/impact and core land before this plan. Artifact/fleet imports the exact contracts below and does not introduce alternate manifest, projection, validation, health, query, registry, Graphify-rendering, or backend-credential authorities.

```python
# project_knowledge.models / manifest / staging
@dataclass(frozen=True)
class ArtifactIntent:
    provider: Literal["none", "github-release"] = "none"
    host: str | None = None
    repository: str | None = None
    repository_id: int | None = None
    channel: str | None = None
    source_ref: str | None = None
    signer_workflow: str | None = None
    signer_digest: str | None = None

ProjectManifest.project_id: str
ProjectManifest.project_uid: UUID | None
ProjectManifest.graphify_version: str
ProjectManifest.track_html: bool
ProjectManifest.artifacts: ArtifactIntent

def load_manifest(
    path: Path, repo_root: Path, *,
    repository_access: RepositoryAccess | None = None,
) -> ProjectManifest: ...
def load_manifest_payload(payload: bytes, repo_root: Path) -> ProjectManifest: ...

@dataclass(frozen=True)
class ProjectionSnapshot:
    source_digest: str
    projection_digest: str | None
    files: tuple[ProjectionFile, ...]
    decisions: tuple[PrivacyDecision, ...]
    reason_counts: tuple[tuple[str, int], ...]
    ignore_digests: tuple[str, ...]
    secret_exception_digest: str | None
    coverage_digest: str | None
    coverage_approvals: tuple[CoverageApproval, ...] = ()

@dataclass(frozen=True)
class StagedInput:
    root: Path
    source_digest: str
    files: tuple[PurePosixPath, ...]
    projection_digest: str | None = None
    reason_counts: tuple[tuple[str, int], ...] = ()
    coverage_approvals: tuple[CoverageApproval, ...] = ()
    projection_files: tuple[ProjectionFile, ...] = ()

def inspect_projection(
    repo_root: Path, manifest: ProjectManifest, *, repository_access: RepositoryAccess | None = None
) -> ProjectionSnapshot: ...
def stage_input(
    repo_root: Path, manifest: ProjectManifest, destination: Path,
    *, repository_access: RepositoryAccess | None = None,
) -> StagedInput: ...
```

`load_manifest_payload()` is an explicit amendment to Core Task 1 and must land in the same `manifest.py` commit: `load_manifest()` descriptor-captures/caps its file and delegates parsing to this byte-owned entry point. Fleet passes only bytes captured from the already opened repository descriptor, so no fleet code reopens a validated manifest pathname or reimplements strict YAML/schema validation.

```python
# project_knowledge.locking / lifecycle / health / doctor
RepositoryIdentity = tuple[int, int]

@dataclass
class RepositoryAccess(AbstractContextManager["RepositoryAccess"]):
    descriptor: int
    identity: RepositoryIdentity

def open_repository_access(
    repo_root: Path, *,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> RepositoryAccess: ...
def capture_lifecycle_repository(repo_root: Path) -> AbstractContextManager[RepositoryAccess]: ...
def require_current_manifest(
    repo_root: Path, supplied: ProjectManifest, *,
    repository_access: RepositoryAccess,
) -> ProjectManifest: ...
def assert_current_manifest_unchanged(
    repo_root: Path, manifest: ProjectManifest, *,
    repository_access: RepositoryAccess,
) -> None: ...
def inspect_init_journal(
    repo_root: Path, *,
    repository_access: RepositoryAccess | None = None,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> Literal["none", "recoverable", "corrupt"]: ...
def repository_lifecycle_lock(
    repo_root: Path, timeout: float = 5.0, *, create: bool = True,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> RepositoryLifecycleLock: ...

@dataclass(frozen=True)
class RefreshOptions:
    backend: str | None
    model: str | None
    deep: bool
    code_only: bool

def refresh_project(
    repo_root: Path,
    manifest: ProjectManifest,
    options: RefreshOptions,
    *,
    runner: CommandRunner | None = None,
    fs: RefreshFileSystem = REAL_REFRESH_FS,
    ambient: Mapping[str, str] | None = None,
    graphify_binary: Path | None = None,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> RefreshResult: ...
def inspect_project_state(
    repo_root: Path,
    manifest: ProjectManifest,
    *,
    atlas: FeatureHealth | None = None,
    registry: FeatureHealth | None = None,
    artifacts: FeatureHealth | None = None,
    repository_access: RepositoryAccess | None = None,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> KnowledgeState: ...
def assess_health(state: KnowledgeState) -> KnowledgeHealth: ...
def doctor_project(
    repo_root: Path,
    *,
    graphify_binary: Path | None = None,
    runner: CommandRunner | None = None,
    package_version: str | None = None,
    expected_repository_identity: RepositoryIdentity | None = None,
    expected_manifest: ProjectManifest | None = None,
) -> DoctorResult: ...
```

The `graphify_binary`, runner, filesystem, and package-version parameters above are library-test seams only. Artifact/fleet CLI and workflow inputs never expose executable or runner injection.

```python
# project_knowledge.artifacts / compatibility / evidence / graphify
from project_knowledge.evidence import GRAPH_EVIDENCE_MAX_BYTES
from project_knowledge.graphify import (
    CommandRunner as GraphifyCommandRunner,
    ResolvedGraphifyExecutable,
    probe_graphify,
    resolve_graphify_executable,
    run_graphify_operation,
)

def resolve_graphify_compatibility(version: str) -> GraphifyCompatibility: ...
def parse_graph_evidence(
    payload: bytes,
    contract: GraphifyCompatibility,
    *,
    expected_digest: str,
) -> GraphEvidence: ...
@dataclass(frozen=True)
class AgentInstallContract:
    platform: Literal["codex", "agents"]
    home_relative_skill: PurePosixPath

GraphifyCompatibility.agent_installs: tuple[AgentInstallContract, ...]
BackendContract.canonical_credential_environment: str

def render_graphify_agent_install(
    contract: GraphifyCompatibility,
    *,
    binary: Path,
    platform: Literal["codex", "agents"],
) -> RenderedCommand: ...
def bind_semantic_backend_credential(
    contract: GraphifyCompatibility,
    backend: str,
    credential: str | None,
) -> dict[str, str]: ...
def validate_public_model_identifier(model: object) -> str: ...
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
def promote_graph(
    candidate: ValidatedGraph,
    repo_root: Path,
    fs: FileSystem = REAL_FS,
    *,
    repository_access: RepositoryAccess | None = None,
) -> PromotionResult: ...
```

Task 1 owns private `CapturedGeneration`, `_capture_validated_generation()`, and `_capture_owned_generation()`. The first descriptor-copies an already current-process `ValidatedGraph`'s closed approved generation plus ownership into a mode-0700 private directory with pre/open/post identity and double-digest checks, validates that private copy again with the same ownership-bound digests, and returns only immutable bytes/metadata. While the lifecycle lock is held, `pack_bundle()` obtains the current `ProjectionSnapshot`; `_capture_owned_generation()` validates the live generation against that snapshot's two expected digests and delegates to the capture primitive. It does not parse ownership as an independent authority and never adds a second public ownership API.

```python
# project_knowledge.queries / registry
def open_query_snapshot(
    repo_root: Path, manifest: ProjectManifest, *,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> AbstractContextManager[QuerySnapshot]: ...
def query_nodes(snapshot: QuerySnapshot, term: str, *, limit: int = 20) -> QueryEnvelope: ...
def shortest_path(
    snapshot: QuerySnapshot, source: str, target: str, *, max_depth: int = 32
) -> QueryEnvelope: ...
def explain_node(snapshot: QuerySnapshot, node: str, *, depth: int = 1) -> QueryEnvelope: ...
def affected_nodes(
    snapshot: QuerySnapshot,
    node: str,
    *,
    depth: int = 2,
    relations: tuple[str, ...] = (),
) -> QueryEnvelope: ...
def registry_status(
    repo_root: Path, manifest: ProjectManifest, *, user_root: Path | None = None,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> RegistryStatus: ...
def registry_sync(
    repo_root: Path,
    manifest: ProjectManifest,
    *,
    user_root: Path | None = None,
    runner: CommandRunner | None = None,
    graphify_binary: Path | None = None,
    fs=REAL_REGISTRY_FS,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> RegistrySyncResult: ...
```

Core Tasks 9/10 own this exact fleet read-only amendment used in Task 10:

```python
@dataclass(frozen=True)
class RegistrySnapshotEntry:
    project_uid: UUID
    project_id: str
    registry_key: str
    graphify_version: str
    adapter_id: str
    graph_digest: str
    evidence_digest: str
    generation_digest: str
    snapshot_digest: str
    source_digest: str
    projection_digest: str
    graph_payload: bytes = field(repr=False)
    evidence_payload: bytes = field(repr=False)
    impact_trust: Literal["trusted", "navigation"]
    impact_limitations: tuple[str, ...]

@dataclass(frozen=True)
class RegistrySnapshot:
    generation: int
    registry_digest: str
    graphify_projection_digest: str
    entries: tuple[RegistrySnapshotEntry, ...]

@dataclass(frozen=True)
class RegistryQueryRequest:
    command: Literal["query", "path", "explain", "affected"]
    term: str | None = None
    source: str | None = None
    target: str | None = None
    node: str | None = None
    limit: int = 20
    max_depth: int = 32
    depth: int | None = None
    relations: tuple[str, ...] = ()

def capture_registry_snapshot(
    project_uids: tuple[UUID, ...],
    *,
    require_graphify_projection: bool,
    user_root: Path | None = None,
) -> RegistrySnapshot: ...
def query_registry(
    snapshot: RegistrySnapshot, request: RegistryQueryRequest
) -> QueryEnvelope: ...
```

`query_registry` normalizes `depth=None` by command: explain uses 1 and
affected uses 2, matching the single-repository APIs and CLI defaults. An
explicit explain depth must be 1..2; an explicit affected depth must be 0..8;
query/path reject any non-null depth before traversal.

The registry stores each complete capture as `snapshots/<uid>/<snapshot_digest>/owned/` with its canonical captured manifest at sibling `manifest.yaml`. `snapshot_digest` is the Core-owned domain-separated hash of generation digest plus exact ownership and manifest SHA-256 values; the internal registry binds all components and the Graphify projector receives only `owned/graph.json`. `capture_registry_snapshot()` takes the existing shared global lock without creating state, descriptor-captures every selected snapshot, recomputes the snapshot identity, parses the captured manifest, revalidates `owned/`, validates the internal manifest digest and Graphify compatibility projection, discards ownership/manifest bytes, and returns exact caller UID order. The aggregate graph/evidence cap is 256 MiB. Stable failures are `registry_snapshot_missing`, `registry_snapshot_stale`, `registry_snapshot_mismatch`, `registry_snapshot_too_large`, or `registry_snapshot_busy`; returned payloads have no live paths and are `repr=False`.

## File map

| File | Responsibility |
|---|---|
| `src/project_knowledge/bundles.py` | Bundle schema, canonical manifest, deterministic pack, strict ZIP parser, private candidate mapping, local install orchestration. |
| `src/project_knowledge/github_artifacts.py` | Stable system-tool resolver, GitHub identity/release resolution, bounded HTTPS download, immutable receipt, `gh attestation verify`, pull authorization/orchestration. |
| `src/project_knowledge/workflow_publish.py` | Internal workflow-only rebuild/repack, release upload, remote verification, rolling-tag update, retention; never registered as a public script. |
| `src/project_knowledge/fleet.py` | Strict universal workspace schema, path/Git-alias validation, selection, bounded coordination, stable aggregate results. |
| `src/project_knowledge/agent_install.py` | Graphify platform delegation plus owned atomic install/uninstall of packaged AtlasWeaver resources. |
| `src/project_knowledge/operation_state.py` | Content-free operation records, private atomic local state, CI summary JSON/Markdown rendering. |
| `src/project_knowledge/cli.py` | Nested artifact/fleet commands, pull, agent commands, safe JSON and stable exit mapping only. |
| `src/project_knowledge/resources/skills/using-project-knowledge-graphs/**` | Wheel-owned reviewed skill, OpenAI metadata, and workflow reference. |
| `pyproject.toml` | Include packaged resource files and keep one `project-knowledge` entry point. |
| `.github/workflows/atlasweaver-check.yml` | Reusable read-only manifest/privacy/freshness/coverage check. |
| `.github/workflows/atlasweaver-publish.yml` | Split unprivileged build and privileged revalidation/attest/release workflow. |
| `.github/workflows/ci.yml` | Pinned action references and artifact/fleet test matrix. |
| `tests/bundle_fixtures.py` | Shared owned-generation and independent deterministic-ZIP fixtures. |
| `tests/test_bundles_pack.py` | Canonical manifest, lifecycle snapshot, deterministic pack, pack race tests. |
| `tests/test_bundles_parse.py` | Byte-level ZIP adversarial parser/limit/path tests. |
| `tests/test_bundles_install.py` | Local install identity/freshness/transaction/race tests. |
| `tests/test_github_artifacts.py` | Fake HTTPS, release binding, redirect/auth, attestation, and pull tests. |
| `tests/test_workflow_publish.py` | Privileged publication sequencing, verification, idempotency, retention tests. |
| `tests/test_agent_install.py` | Resource packaging, ownership, rollback, delegation, uninstall tests. |
| `tests/test_fleet.py` | Workspace validation, bounded execution, stable output, registry/query tests. |
| `tests/test_operation_state.py` | Redaction, atomicity, read-only behavior, CI summary tests. |
| `tests/test_workflows.py` | Reusable workflow input, permission, pin, concurrency, and privilege-boundary contract tests. |
| `tests/test_artifact_fleet_cli.py` | Exact public parser and end-to-end CLI envelopes/exit codes. |
| `tests/test_artifact_fleet_e2e.py` | Pack/install, wheel-resource install, and multi-project fleet acceptance tests. |
| `skills/using-project-knowledge-graphs/**`, `README.md`, `CHANGELOG.md`, `SECURITY.md` | Reviewed usage, rollout, and provenance/security operator contract. |

---

### Task 1: Closed artifact schema and deterministic pack snapshot

**Files:**
- Create: `src/project_knowledge/bundles.py`
- Create: `tests/bundle_fixtures.py`
- Create: `tests/test_bundles_pack.py`
- Test: `tests/test_artifacts.py`

**Interfaces:**
- Consumes: exact `repository_lifecycle_lock`, `inspect_projection`, `resolve_graphify_compatibility`, `parse_graph_evidence`, and `validate_owned_graph` prerequisite signatures above.
- Produces: `BundleError(code: str, message: str, recovery_id: str | None = None)`; a recovery ID is constructor-validated lowercase hex and non-null only for private cleanup/output-recovery failures.
- Produces: `PayloadDescriptor(path: PurePosixPath, sha256: str, byte_length: int)`.
- Produces: `ArtifactManifest.from_bytes(payload: bytes) -> ArtifactManifest` and `ArtifactManifest.to_bytes() -> bytes`.
- Produces: `PackRequest(repo_root: Path, output: Path)` and `PackedBundle(path: Path, sha256: str, byte_length: int, artifact: ArtifactManifest)`.
- Produces: `pack_bundle(request: PackRequest, *, expected_repository_identity: RepositoryIdentity | None = None, expected_manifest: ProjectManifest | None = None) -> PackedBundle`; both expectations are library-only. Identity is checked on the lifecycle root descriptor, and a supplied manifest is compared in full through the retained access before capture or output creation.
- Produces private immutable `CapturedGeneration(manifest: ProjectManifest, validated: ValidatedGraph, payloads: tuple[CapturedPayload, ...])`, `_capture_validated_generation(validated: ValidatedGraph, manifest: ProjectManifest, destination: Path) -> CapturedGeneration`, and `_capture_owned_generation(repo_root: Path, manifest: ProjectManifest, projection: ProjectionSnapshot, destination: Path) -> CapturedGeneration`.
- Produces private mutable `_PackCommitState(output_committed=False)`, allocated
  by the outer pack/publication wrapper and never serialized, plus
  `_pack_captured_generation(generation: CapturedGeneration, output: Path, *, commit_state: _PackCommitState, version_provider: Callable[[], str] = installed_atlasweaver_version, precommit_check: Callable[[], None] = _noop) -> PackedBundle`, shared with the privileged workflow but never exported through CLI. The helper requires a fresh false state and flips it immediately after the exclusive output link is durable, before temporary-name cleanup, checkpoints, or return. The callback runs after all private bytes are finalized and immediately before exclusive output publication; failure leaves the output absent.
- Produces private `OperationTempCleanupError(operation: str, recovery_id: str)`, whose fields are closed and constructor-validated, plus injected `managed_operation_temp_root(operation: str, recovery_id: str, *, fs=REAL_OPERATION_TEMP_FS) -> ManagedOperationTempRoot(path, cleanup_failed)`. It creates one descriptor-bound mode-0700 system root and never raises a raw `TemporaryDirectory`/OS cleanup error. Nested `ExitStack` resources close before it. On normal body exit it catches/sanitizes cleanup `Exception` into `cleanup_failed`; callers inspect that flag only after leaving the context and apply their explicit pre/post-commit rule with the same opaque recovery ID. If an ordinary body `Exception` is already pending and cleanup also fails, the manager replaces it with `OperationTempCleanupError(operation, recovery_id) from None`, and the enclosing public operation maps that closed private error to its family-specific cleanup code. A pending non-ordinary `BaseException` is never suppressed or replaced even when cleanup fails. The CLI/workflow exposes no temp-root override.

- [ ] **Step 1: Write failing canonical-schema and deterministic-byte tests**

Create the shared fixture helper and tests with these exact assertions:

```python
# tests/test_bundles_pack.py
def test_artifact_manifest_is_closed_canonical_and_does_not_self_hash(owned_repo):
    first = pack_bundle(PackRequest(owned_repo.root, owned_repo.root / "first.zip"))
    artifact = read_artifact_json(first.path)
    assert list(artifact) == sorted(artifact)
    assert artifact["schema_version"] == 1
    assert artifact["project_id"] == "demo"
    assert artifact["project_uid"] == "4ed9af24-5aa2-4eac-8d0a-3f622cc74948"
    assert artifact["graphify_version"] == "0.9.48"
    assert artifact["adapter_id"] == "graphify-0.9.48"
    assert artifact["generation_digest"] == first.artifact.generation_digest
    assert artifact["build_epoch"] == 1_777_777_777
    assert "artifact.json" not in {item["path"] for item in artifact["payloads"]}
    assert all(set(item) == {"byte_length", "path", "sha256"} for item in artifact["payloads"])
    assert first.path.read_bytes() == expected_stored_zip_bytes(first.path)


def test_pack_is_identical_across_output_path_time_timezone_and_locale(owned_repo, monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    first = pack_bundle(PackRequest(owned_repo.root, owned_repo.root / "a.zip"))
    monkeypatch.setenv("TZ", "America/Adak")
    second = pack_bundle(PackRequest(owned_repo.root, owned_repo.root / "b.zip"))
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.sha256 == second.sha256


def test_pack_rejects_stale_unowned_or_incomplete_evidence(owned_repo):
    (owned_repo.root / "src/app.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(BundleError, match="bundle_stale"):
        pack_bundle(PackRequest(owned_repo.root, owned_repo.root / "stale.zip"))
```

In `tests/bundle_fixtures.py`, make `expected_stored_zip_bytes()` an independent `struct.pack` encoder of local headers, central records, and EOCD—not a call to production `zipfile`—so the test is a real golden-byte oracle on Python 3.10 and 3.13. The `owned_repo` fixture must create ownership through the prerequisite validator/promotion APIs, set an injected `build_epoch=1_777_777_777`, and never hand-write a trusted ownership file.

- [ ] **Step 2: Run the focused tests and verify the intended import failure**

Run: `uv run pytest -q tests/test_bundles_pack.py`

Expected: collection fails with `ModuleNotFoundError: No module named 'project_knowledge.bundles'`.

- [ ] **Step 3: Implement the exact artifact manifest and deterministic writer**

Use this closed top-level wire schema and fixed ZIP metadata:

```python
ARTIFACT_SCHEMA_VERSION = 1
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
ALLOWED_PAYLOADS = (
    PurePosixPath("graphify-out/GRAPH_EVIDENCE.json"),
    PurePosixPath("graphify-out/GRAPH_REPORT.md"),
    PurePosixPath("graphify-out/graph.html"),
    PurePosixPath("graphify-out/graph.json"),
)

@dataclass(frozen=True)
class ArtifactManifest:
    schema_version: int
    atlasweaver_version: str
    project_id: str
    project_uid: str
    graphify_version: str
    adapter_id: str
    source_digest: str
    projection_digest: str
    graph_digest: str
    generation_digest: str
    git: GitIdentity | None
    build_epoch: int
    transport: LocalTransport | GithubTransport
    payloads: tuple[PayloadDescriptor, ...]

    def to_bytes(self) -> bytes:
        return (json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ) + "\n").encode("utf-8")


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, ZIP_EPOCH)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.internal_attr = 0
    return info
```

The canonical manifest dictionary has exactly these keys: `adapter_id`, `atlasweaver_version`, `build_epoch`, `generation_digest`, `git`, `graph_digest`, `graphify_version`, `payloads`, `project_id`, `project_uid`, `projection_digest`, `schema_version`, `source_digest`, and `transport`. `generation_digest` must equal the prerequisite validator's domain-separated digest over the exact owned-root artifact descriptor set, so graph-identical report/evidence generations remain distinct. Bundle verification deterministically strips the single `graphify-out/` transport prefix before recomputing the same owned-root names; it does not define a second digest domain. `LocalTransport` serializes exactly `{"channel": null, "provider": "none"}`. `GithubTransport` serializes exactly `channel`, `host`, `provider`, `repository`, `repository_id`, and `source_ref`; signer settings remain trusted local configuration and are not copied into the bundle. Reject duplicate JSON keys, non-finite numbers, unknown keys, invalid UUID/digest/OID/version/channel/provider types, unordered or duplicate payload paths, and `graph.html` policy mismatches.

`ALLOWED_PAYLOADS` above is already ascending by raw ASCII/UTF-8 path bytes;
both writer and parser use that one tuple. ZIP entry order is exactly
`artifact.json` followed by the present tuple entries in that order, and the
manifest `payloads` array uses the identical order.

`pack_bundle()` must:

1. open and retain the output-parent descriptor without following symlinks and refuse an existing output; pathname checks alone never authorize publication;
2. take the lifecycle lock with the optional expected repository identity and capture one `RepositoryAccess` from its live lease;
3. require `inspect_init_journal(..., repository_access=repository) == "none"` inside that lock before manifest/projection/capture work, otherwise fail `init_recovery_required` without producing output; when an expected manifest is supplied, call `require_current_manifest` for full semantic equality, otherwise descriptor-load and pin the manifest, then use descriptor identity/relative containment to reject an output located beneath any safe include root, beneath reserved `.git`, `.project-knowledge`, or `graphify-out` directories, or at the exact repository control/config names `.graphify-project.yaml`, `.graphifyignore`, `.graphify-secret-exceptions.yaml`, and `.atlasweaver-coverage.yaml` (whether currently present or absent);
4. call `inspect_projection` through that same repository access, validate the live owned generation against both current digests, descriptor-capture its ownership plus the closed approved artifacts into a mode-0700 temporary snapshot, validate the captured generation again, and call `inspect_projection` again through the same access;
5. require before/owned/after source and projection digests to be identical;
6. recompute and require `CapturedGeneration.validated.generation_digest` over the immutable owned-root payload descriptors (before adding the transport prefix), then select evidence only from that descriptor, read its descriptor-captured bytes, validate them through `parse_graph_evidence(payload, resolve_graphify_compatibility(manifest.graphify_version), expected_digest=evidence_capture.sha256)`, and require schema-v2 evidence even when trust is navigation; `evidence_capture.sha256` comes from the no-follow, double-digest generation capture, never from hashing caller bytes at the parser call site;
7. release the lock only after the copied snapshot validates as one generation;
8. call `_pack_captured_generation` to build exclusively from the snapshot, writing `artifact.json` first and approved payloads in `ALLOWED_PAYLOADS` order;
9. fsync an exclusive mode-0600 sibling temporary, atomically `linkat` that inode through the retained output-parent descriptor to the absent destination (or use a capability-probed `renameat2(RENAME_NOREPLACE)` equivalent), fsync the parent, set the caller-owned `_PackCommitState.output_committed` bit, invoke the injected post-link checkpoint, unlink the temporary name, fsync the parent again, and return its SHA-256/length. The first parent fsync is the commit boundary; the second durably records cleanup of the private sibling name.

No pack step after lifecycle acquisition calls a repository pathname API.
Journal, manifest, projection, ownership, and artifact reads all derive from the
captured lease descriptor. If the root is renamed/replaced mid-capture, the
bundle is built wholly from the original inode (and descriptor-bound output
parent) or fails before publication; replacement bytes never enter the bundle.

The output publication must never call clobbering `os.rename`/`os.replace`. If a failure occurs after the no-replace link, unlink the destination only when its descriptor-bound device/inode/size/SHA-256 still match this transaction; otherwise return `bundle_output_recovery_required` without deleting another process's path. Derive `atlasweaver_version` from `importlib.metadata.version("atlasweaver")`; tests may inject a private metadata-version provider, but `PackRequest`, CLI, manifest, and environment cannot override it. Do not read `time`, `SOURCE_DATE_EPOCH`, locale, ZIP comments, extra fields, live graph paths, or the invocation environment when constructing bytes.

Generate one opaque recovery ID for pack and use the shared managed temp root;
never return from inside an ordinary `TemporaryDirectory` context. A cleanup
failure before the exclusive output link raises constant
`bundle_cleanup_failed` and leaves output absent. A cleanup failure after the
verified link/fsync raises `bundle_output_recovery_required`, preserves the
exact committed output, includes only the recovery ID, and never reports a raw
cleanup exception or deletes the committed inode. Operation state records this
as output-recovery-required, not a successful pack.

The implementation is an explicit state machine: initialize one
`commit_state = _PackCommitState()` and `packed = None`, enter
`managed_operation_temp_root("pack", recovery_id)`, construct the captured ZIP,
pass that exact state to `_pack_captured_generation`, assign `packed`, and leave
the context without returning. The helper—not its caller—sets the state at the
durable no-replace-link boundary. Catch
`OperationTempCleanupError` and every other ordinary `Exception` outside the
context: if `commit_state.output_committed` is true, raise
`BundleError("bundle_output_recovery_required", ..., recovery_id) from None`;
otherwise cleanup failure maps to
`BundleError("bundle_cleanup_failed", ..., recovery_id) from None` and the
original precommit operation exception propagates when cleanup succeeded. After a
normal body exit, `temporary.cleanup_failed and commit_state.output_committed` raises
`bundle_output_recovery_required`; the same flag while uncommitted raises
`bundle_cleanup_failed`. Only a non-null `packed` with no cleanup failure may be
returned. This ordering is covered for both cleanup checkpoints and makes a raw
cleanup exception impossible at the public boundary.

- [ ] **Step 4: Run deterministic pack and legacy artifact regression tests**

Run: `uv run pytest -q tests/test_bundles_pack.py tests/test_artifacts.py`

Expected: all tests pass, including both tracked-HTML modes and required incomplete evidence for Graphify 0.9.48.

- [ ] **Step 5: Add pack concurrency and source-drift failure injection**

Add tests with an injected capture checkpoint:

```python
def test_pack_blocks_concurrent_promotion_and_reads_one_generation(owned_repo, blocking_capture):
    future = blocking_capture.start_pack(owned_repo.root)
    assert blocking_capture.lifecycle_lock_is_held()
    assert blocking_capture.try_promote(timeout=0.05) == "repository_busy"
    blocking_capture.release()
    packed = future.result(timeout=2)
    assert validate_test_bundle(packed.path).artifact.graph_digest == owned_repo.graph_digest


def test_pack_source_drift_leaves_no_output(owned_repo, capture_fault):
    capture_fault.after_copy(lambda: rewrite_safe_source(owned_repo.root))
    with pytest.raises(BundleError, match="bundle_source_drift"):
        pack_bundle(PackRequest(owned_repo.root, owned_repo.root / "drift.zip"))
    assert not (owned_repo.root / "drift.zip").exists()


def test_pack_expected_manifest_change_precedes_capture_and_output(
    owned_repo, tmp_path,
) -> None:
    with open_repository_access(owned_repo.root) as repository:
        expected_identity = repository.identity
        expected_manifest = load_manifest(
            owned_repo.root / ".graphify-project.yaml", owned_repo.root,
            repository_access=repository,
        )
    rewrite_manifest_semantically(
        owned_repo.root,
        project_id=expected_manifest.project_id,
        project_uid=expected_manifest.project_uid,
        privacy=privacy_with_extra_include("private"),
        output="alternate-output",
    )
    output = tmp_path / "bundle.zip"
    with pytest.raises(ManifestError) as raised:
        pack_bundle(
            PackRequest(owned_repo.root, output),
            expected_repository_identity=expected_identity,
            expected_manifest=expected_manifest,
        )
    assert raised.value.kind == "changed"
    assert not output.exists()

@pytest.mark.parametrize("committed", [False, True])
def test_pack_cleanup_failure_has_stable_pre_or_post_output_semantics(
    owned_repo, operation_temp_fault, committed,
) -> None:
    output = owned_repo.root.parent / "bundle.zip"
    operation_temp_fault.fail_cleanup(
        "pack", after_output_publication=committed
    )
    with pytest.raises(BundleError) as raised:
        pack_bundle(PackRequest(owned_repo.root, output))
    assert raised.value.code == (
        "bundle_output_recovery_required" if committed else "bundle_cleanup_failed"
    )
    assert raised.value.recovery_id is not None
    assert output.exists() is committed
    if committed:
        assert inspect_bundle_manifest(output).project_uid == str(
            current_manifest(owned_repo.root).project_uid
        )


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_pack_ordinary_failure_after_output_commit_is_output_recovery(
    owned_repo, pack_fault, operation_temp_fault, cleanup_fails,
) -> None:
    output = owned_repo.root.parent / "bundle.zip"
    pack_fault.after_durable_output_link(
        lambda: (_ for _ in ()).throw(RuntimeError("private"))
    )
    if cleanup_fails:
        operation_temp_fault.fail_cleanup("pack")
    with pytest.raises(BundleError) as raised:
        pack_bundle(PackRequest(owned_repo.root, output))
    assert raised.value.code == "bundle_output_recovery_required"
    assert raised.value.recovery_id is not None
    assert inspect_bundle_manifest(output).project_uid == str(
        current_manifest(owned_repo.root).project_uid
    )

@pytest.mark.parametrize("relative_output", [
    "graphify-out/bundle.zip",
    ".project-knowledge/bundle.zip",
    ".git/objects/bundle.zip",
    ".graphify-secret-exceptions.yaml",
    ".atlasweaver-coverage.yaml",
])
def test_pack_rejects_reserved_repository_destinations_without_graph_damage(
    owned_repo, relative_output: str,
) -> None:
    before = tree_snapshot(owned_repo.root / "graphify-out")
    before_health = assess_health(inspect_project_state(
        owned_repo.root, current_manifest(owned_repo.root)
    ))
    with pytest.raises(BundleError, match="bundle_output_in_source"):
        pack_bundle(PackRequest(
            owned_repo.root, owned_repo.root / relative_output
        ))
    assert tree_snapshot(owned_repo.root / "graphify-out") == before
    assert assess_health(inspect_project_state(
        owned_repo.root, current_manifest(owned_repo.root)
    )) == before_health

def test_pack_root_replacement_after_lock_captures_only_original(
    owned_repo, capture_fault, tmp_path,
) -> None:
    original = tmp_path / "original"
    replacement = owned_repo.root
    capture_fault.after_lock(
        lambda: replace_repository_root(
            replacement, original, attacker_owned_repository(tmp_path)
        )
    )
    output = tmp_path / "packed.zip"
    packed = pack_bundle(PackRequest(replacement, output))
    assert validate_test_bundle(packed.path).artifact.graph_digest == owned_repo.graph_digest
    assert not (replacement / ".project-knowledge").exists()
    assert not (replacement / "graph.zip").exists()


@pytest.mark.parametrize("mutation", ["rewrite_same_inode", "truncate_same_inode"])
def test_pack_rejects_owned_generation_mutation_during_descriptor_copy(
    owned_repo, capture_fault, mutation
):
    capture_fault.mutate_owned_payload_during_second_pass(mutation)
    with pytest.raises(BundleError, match="bundle_generation_changed"):
        pack_bundle(PackRequest(owned_repo.root, owned_repo.root / "raced.zip"))
    assert not (owned_repo.root / "raced.zip").exists()


def test_pack_rejects_output_inside_safe_projection_without_creating_it(owned_repo):
    output = owned_repo.root / "src/graph.zip"
    with pytest.raises(BundleError, match="bundle_output_in_source"):
        pack_bundle(PackRequest(owned_repo.root, output))
    assert not output.exists()


def test_pack_no_replace_loses_destination_race_without_clobber(owned_repo, output_race):
    output = owned_repo.root / "graph.zip"
    output_race.create_before_link(output, b"other process\n")
    with pytest.raises(BundleError, match="bundle_output_exists"):
        pack_bundle(PackRequest(owned_repo.root, output))
    assert output.read_bytes() == b"other process\n"
```

Run: `uv run pytest -q tests/test_bundles_pack.py -k 'concurrent or drift'`

Expected: PASS; no archive or temporary survives either failure.

- [ ] **Step 6: Commit the deterministic pack boundary**

```bash
git add src/project_knowledge/bundles.py tests/bundle_fixtures.py tests/test_bundles_pack.py
git commit -m "feat: add deterministic graph bundle packing"
```

### Task 2: Byte-level strict ZIP parser and private candidate mapping

**Files:**
- Modify: `src/project_knowledge/bundles.py`
- Create: `tests/test_bundles_parse.py`
- Test: `tests/bundle_fixtures.py`

**Interfaces:**
- Consumes: `ArtifactManifest.from_bytes()` and `ALLOWED_PAYLOADS` from Task 1.
- Produces: `ArchiveLimits(total_bytes=268_435_456, payload_bytes=267_386_880, entry_bytes=134_217_728)`.
- Produces: `PayloadBinding(path: PurePosixPath, candidate_name: str, sha256: str, byte_length: int, device: int, inode: int)`.
- Produces: `ParsedBundle(AbstractContextManager["ParsedBundle"], root: Path, artifact: ArtifactManifest, payloads: tuple[PayloadBinding, ...], archive_sha256: str, archive_size: int)` owning a private bound destination-directory descriptor (excluded from repr), with idempotent `close()` and `read_payload(path: PurePosixPath, max_bytes: int) -> bytes` that opens through that retained descriptor and revalidates inode/digest/length. Reads after close fail with constant `bundle_invalid`; `__exit__` always closes the descriptor before any enclosing temporary directory is cleaned.
- Produces: `parse_bundle(bundle: Path, destination: Path) -> ParsedBundle`; every successful caller must use `with parse_bundle(...) as parsed:`.
- Produces: `inspect_bundle_manifest(bundle: Path) -> ArtifactManifest`, which validates archive structure and reads only bounded `artifact.json`.

For every later evidence parse, `expected_digest` is the matched `PayloadBinding.sha256`. `parse_bundle()` obtains that value from the closed `artifact.json` payload descriptor and verifies it against the captured archive bytes before returning; `read_payload()` revalidates the same binding. Consumers must not hash the bytes returned by `read_payload()` and use that self-derived value as an authority.

- [ ] **Step 1: Write a mutation matrix against local headers, central records, and EOCD**

```python
@pytest.mark.parametrize("mutation", [
    "path_traversal", "absolute_path", "backslash_path", "duplicate_normalized_name",
    "local_central_name_mismatch", "local_central_crc_mismatch", "encrypted",
    "compressed", "data_descriptor", "zip64", "multi_disk", "trailing_data",
    "archive_comment", "entry_extra", "symlink", "directory", "special_file",
    "entry_limit", "payload_limit", "archive_limit", "overlapping_entries",
])
def test_parser_rejects_malformed_archive_without_extracting(tmp_path, valid_bundle_bytes, mutation):
    bundle = tmp_path / "input.zip"
    bundle.write_bytes(mutate_zip(valid_bundle_bytes, mutation))
    destination = tmp_path / "private"
    with pytest.raises(BundleError, match="bundle_invalid|bundle_too_large"):
        parse_bundle(bundle, destination)
    assert not destination.exists()


def test_parser_maps_only_approved_graph_files_to_root_candidate(tmp_path, valid_bundle):
    with parse_bundle(valid_bundle, tmp_path / "private") as parsed:
        assert sorted(path.name for path in parsed.root.iterdir()) == [
            "GRAPH_EVIDENCE.json", "GRAPH_REPORT.md", "graph.json"
        ]
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in parsed.root.iterdir())
    with pytest.raises(BundleError, match="bundle_invalid"):
        parsed.read_payload(PurePosixPath("graphify-out/graph.json"), 1024)


@pytest.mark.parametrize("mutation", ["rewrite_same_inode", "truncate_same_inode"])
def test_parser_detects_archive_mutation_during_capture(tmp_path, valid_bundle_bytes, mutation, archive_race):
    bundle = tmp_path / "input.zip"
    bundle.write_bytes(valid_bundle_bytes)
    archive_race.mutate_during_second_pass(bundle, mutation)
    with pytest.raises(BundleError, match="bundle_changed_during_capture"):
        parse_bundle(bundle, tmp_path / "private")
    assert not (tmp_path / "private").exists()
```

The fixture mutator must edit raw ZIP structures using `struct`, including a valid central directory paired with an intentionally inconsistent local header; do not rely on `zipfile` to generate malformed cases.

- [ ] **Step 2: Run the parser tests and verify they fail at the missing API**

Run: `uv run pytest -q tests/test_bundles_parse.py`

Expected: collection/import fails because `parse_bundle` and `inspect_bundle_manifest` do not exist.

- [ ] **Step 3: Implement descriptor-bound parsing before any output creation**

Define exact v1 constants and open the caller path with `os.open(bundle, os.O_RDONLY | os.O_NOFOLLOW)` before any parsing:

```python
LOCAL = struct.Struct("<4s5H3L2H")
CENTRAL = struct.Struct("<4s6H3L5H2L")
EOCD = struct.Struct("<4s4H2LH")
LOCAL_MAGIC = b"PK\x03\x04"
CENTRAL_MAGIC = b"PK\x01\x02"
EOCD_MAGIC = b"PK\x05\x06"
UTF8_FLAG = 0x0800
FORBIDDEN_FLAGS = 0x0001 | 0x0008
V1_LIMITS = ArchiveLimits(268_435_456, 267_386_880, 134_217_728)
```

Before inspecting ZIP structures, descriptor-copy the bounded source into a fresh mode-0600 file under a mode-0700 private directory. Bind source `(device, inode, size, mtime_ns, ctime_ns)`, SHA-256 the bytes while copying, rewind the still-open source descriptor, hash a second complete pass, and require both digests plus pre/mid/post bindings to match. Parse and extract only from the private copy descriptor; never reopen the caller path. This makes replacement, same-inode rewrite, and truncation races explicit failures.

Require EOCD to end exactly at file length, disk numbers zero, identical per-disk/total entry counts, no comment, no ZIP64 sentinels/signatures, central directory bounds exact, local offsets unique and non-overlapping, first local header at offset zero, and the last stored payload immediately followed by the central directory. For every entry require compression method zero in both headers, forbidden flags clear, identical flags/name/CRC/sizes, empty extra/comment fields, exact ASCII/UTF-8 approved name, and Unix regular mode `0o100600`; reject creator/attributes that describe directories, links, devices, sockets, or unknown types.

Validate declared sizes against all three hard limits before reading a payload. Stream each payload in 1 MiB chunks from the already opened archive descriptor, recompute CRC-32/SHA-256/length, and compare against both headers and `artifact.json`. Only after every structural and manifest check passes, create a mode-0700 destination and map each `graphify-out/<allowed-name>` to a root-level mode-0600 file with `openat(O_CREAT|O_EXCL|O_NOFOLLOW)`. Fsync files/destination; on any exception remove only the freshly created bound directory.

`inspect_bundle_manifest()` shares the same two-pass private archive capture and central/local validator but reads at most 64 KiB for `artifact.json`. It does not parse graph/report/evidence bytes and is explicitly insufficient to install. Later consumers use `ParsedBundle.read_payload()`; they never call `Path.read_bytes()` on a supposedly validated candidate entry.

- [ ] **Step 4: Run all parser and pack tests**

Run: `uv run pytest -q tests/test_bundles_parse.py tests/test_bundles_pack.py`

Expected: PASS. The test for a forged central size must fail before its declared payload is allocated or read.

- [ ] **Step 5: Commit the strict parser**

```bash
git add src/project_knowledge/bundles.py tests/bundle_fixtures.py tests/test_bundles_parse.py
git commit -m "feat: validate graph bundles byte by byte"
```

### Task 3: Atomic local install and same-process transport authorization

**Files:**
- Modify: `src/project_knowledge/bundles.py`
- Create: `tests/test_bundles_install.py`
- Test: `tests/test_artifacts.py`
- Test: `tests/test_health.py`

**Interfaces:**
- Consumes: `parse_bundle`, Core projection/validator/promotion APIs, compatibility evidence validation, and the sole evidence cap `project_knowledge.evidence.GRAPH_EVIDENCE_MAX_BYTES`.
- Produces: `InstallResult(project_id: str, project_uid: str, graph_digest: str | None, generation_digest: str | None, build_epoch: int | None, changed: bool | None, status: Literal["installed", "already_current", "promoted_but_stale"], recovery_id: str | None = None)`; installed/already-current require complete descriptor-revalidated identity and null recovery ID, while committed-but-unverifiable uses `None` for all identity fields and `changed`. A non-null opaque recovery ID is allowed only for committed cleanup failure.
- Produces privately: `_InstallExecution(result: InstallResult, promotion_committed: bool)`. Cleanup and pull orchestration branch only on this explicit commit marker, never infer commit state from a public status or nullable identity field.
- Produces privately mutable `_InstallCommitState(promotion_committed=False, project_id=None, project_uid=None, installed=None)`, owned by the wrapper and never serialized. `_postcommit_install_execution(state, recovery_id=None)` constructs stale output from only descriptor-validated `state.installed`, otherwise an all-null identity.
- Produces privately: `_install_bundle_scoped(..., recovery_id: str) -> _InstallExecution`; `_install_bundle` is the sole wrapper that generates/retains the ID and normalizes `OperationTempCleanupError`.
- Produces: `install_local_bundle(repo_root: Path, bundle: Path, *, expected_repository_identity: RepositoryIdentity | None = None, expected_manifest: ProjectManifest | None = None) -> InstallResult`.
- Produces privately for Task 6: `_install_verified_pull_execution(repo_root: Path, bundle: Path, authorization: _PullAuthorization, *, expected_repository_identity: RepositoryIdentity | None = None, expected_manifest: ProjectManifest | None = None, recovery_id: str | None = None) -> _InstallExecution`; the test-facing compatibility wrapper `_install_verified_pull(...) -> InstallResult` returns only `.result`. `_PullAuthorization`, the execution marker, and the shared operation recovery ID cannot be accepted by any public CLI/parser API.

- [ ] **Step 1: Write failing end-to-end local install identity tests**

```python
def test_local_bundle_installs_through_candidate_validator_and_promotion(source_repo, clean_clone, tmp_path):
    bundle = pack_bundle(PackRequest(source_repo, tmp_path / "graph.zip"))
    result = install_local_bundle(clean_clone, bundle.path)
    assert result.status == "installed"
    assert result.project_uid == "4ed9af24-5aa2-4eac-8d0a-3f622cc74948"
    assert result.generation_digest == bundle.artifact.generation_digest
    assert result.build_epoch == bundle.artifact.build_epoch
    ownership = load_owned_generation(clean_clone / "graphify-out")
    assert ownership.build_epoch == 1_777_777_777
    assert ownership.source_digest == bundle.artifact.source_digest
    assert ownership.projection_digest == bundle.artifact.projection_digest


@pytest.mark.parametrize("field", [
    "project_id", "project_uid", "graphify_version", "adapter_id",
    "source_digest", "projection_digest", "graph_digest", "generation_digest", "build_epoch",
])
def test_install_rejects_every_identity_mismatch_without_touching_live_graph(clean_clone, forged_bundle, field):
    before = tree_snapshot(clean_clone / "graphify-out")
    with pytest.raises(BundleError, match="bundle_identity_mismatch|bundle_stale"):
        install_local_bundle(clean_clone, forged_bundle(field))
    assert tree_snapshot(clean_clone / "graphify-out") == before


def test_public_install_rejects_github_bundle_even_with_copied_receipt(clean_clone, github_bundle):
    with pytest.raises(BundleError, match="bundle_pull_required"):
        install_local_bundle(clean_clone, github_bundle)


def test_post_promotion_manifest_rewrite_returns_stale_verified_identity(
    clean_clone, valid_bundle, install_fault,
) -> None:
    install_fault.after_promotion(
        lambda: rewrite_and_restore_manifest_same_inode(clean_clone)
    )
    result = install_local_bundle(clean_clone, valid_bundle)
    owned = validate_owned_graph(clean_clone / "graphify-out", current_manifest(clean_clone))
    assert result.status == "promoted_but_stale"
    assert (result.graph_digest, result.generation_digest, result.build_epoch) == (
        owned.graph_digest, owned.generation_digest, owned.build_epoch,
    )


def test_install_cleanup_failure_after_changed_promotion_returns_recovery_id(
    clean_clone, valid_bundle, operation_temp_fault,
) -> None:
    operation_temp_fault.fail_cleanup("install")
    result = install_local_bundle(clean_clone, valid_bundle)
    assert result.status == "promoted_but_stale"
    assert result.recovery_id is not None
    assert validate_owned_graph(
        clean_clone / "graphify-out", current_manifest(clean_clone)
    ).generation_digest == result.generation_digest


def test_install_cleanup_failure_without_new_promotion_is_closed_error(
    clean_clone, valid_bundle, operation_temp_fault,
) -> None:
    installed = install_local_bundle(clean_clone, valid_bundle)
    operation_temp_fault.fail_cleanup("install")
    with pytest.raises(BundleError) as raised:
        install_local_bundle(clean_clone, valid_bundle)
    assert raised.value.code == "bundle_cleanup_failed"
    assert raised.value.recovery_id is not None
    assert validate_owned_graph(
        clean_clone / "graphify-out", current_manifest(clean_clone)
    ).generation_digest == installed.generation_digest


def test_install_ordinary_revalidation_failure_after_commit_is_stale_not_error(
    clean_clone, valid_bundle, install_fault,
) -> None:
    install_fault.raise_during_postcommit_revalidation(RuntimeError("private"))
    result = install_local_bundle(clean_clone, valid_bundle)
    assert result.status == "promoted_but_stale"
    assert (
        result.graph_digest, result.generation_digest,
        result.build_epoch, result.changed,
    ) == (None, None, None, None)


def test_install_postcommit_exception_plus_cleanup_failure_keeps_commit_recovery(
    clean_clone, valid_bundle, install_fault, operation_temp_fault,
) -> None:
    install_fault.raise_during_postcommit_revalidation(RuntimeError("private"))
    operation_temp_fault.fail_cleanup("install")
    result = install_local_bundle(clean_clone, valid_bundle)
    assert result.status == "promoted_but_stale"
    assert result.recovery_id is not None
    assert (
        result.graph_digest, result.generation_digest,
        result.build_epoch, result.changed,
    ) == (None, None, None, None)
```

- [ ] **Step 2: Run and observe the missing install API**

Run: `uv run pytest -q tests/test_bundles_install.py`

Expected: import/collection fails for `install_local_bundle`.

- [ ] **Step 3: Implement validation and promotion under one lifecycle boundary**

```python
def install_local_bundle(
    repo_root: Path,
    bundle: Path,
    *,
    expected_repository_identity: RepositoryIdentity | None = None,
    expected_manifest: ProjectManifest | None = None,
) -> InstallResult:
    return _install_bundle(
        repo_root, bundle, authorization=None,
        expected_repository_identity=expected_repository_identity,
        expected_manifest=expected_manifest,
    ).result


def _install_bundle(
    repo_root: Path,
    bundle: Path,
    authorization: _PullAuthorization | None,
    *,
    expected_repository_identity: RepositoryIdentity | None = None,
    expected_manifest: ProjectManifest | None = None,
    recovery_id: str | None = None,
) -> _InstallExecution:
    recovery_id = recovery_id or secrets.token_hex(16)
    commit_state = _InstallCommitState()
    try:
        return _install_bundle_scoped(
            repo_root, bundle, authorization,
            expected_repository_identity=expected_repository_identity,
            expected_manifest=expected_manifest,
            recovery_id=recovery_id,
            commit_state=commit_state,
        )
    except OperationTempCleanupError:
        if commit_state.promotion_committed:
            return _postcommit_install_execution(
                commit_state, recovery_id=recovery_id
            )
        raise BundleError(
            "bundle_cleanup_failed", "private install cleanup failed",
            recovery_id,
        ) from None
    except Exception:
        if commit_state.promotion_committed:
            return _postcommit_install_execution(commit_state)
        raise


def _install_bundle_scoped(
    repo_root: Path,
    bundle: Path,
    authorization: _PullAuthorization | None,
    *,
    expected_repository_identity: RepositoryIdentity | None,
    expected_manifest: ProjectManifest | None,
    recovery_id: str,
    commit_state: _InstallCommitState,
) -> _InstallExecution:
    with repository_lifecycle_lock(
        repo_root,
        expected_repository_identity=expected_repository_identity,
    ), capture_lifecycle_repository(repo_root) as repository:
        if inspect_init_journal(
            repo_root, repository_access=repository
        ) != "none":
            raise BundleError(
                "init_recovery_required", "configuration recovery is required"
            )
        if expected_manifest is None:
            manifest = load_manifest(
                repo_root / ".graphify-project.yaml", repo_root,
                repository_access=repository,
            )
            manifest = require_current_manifest(
                repo_root, manifest, repository_access=repository
            )
        else:
            manifest = require_current_manifest(
                repo_root, expected_manifest,
                repository_access=repository,
            )
        with managed_operation_temp_root(
            "install", recovery_id
        ) as temporary, ExitStack() as parsed_scope:
            private = temporary.path
            before = stage_input(
                repo_root, manifest, private / "source-before",
                repository_access=repository,
            )
            parsed = parsed_scope.enter_context(
                parse_bundle(bundle, private / "candidate")
            )
            _require_transport_authority(parsed, manifest, authorization)
            _require_project_snapshot_identity(parsed.artifact, manifest, before)
            compatibility = resolve_graphify_compatibility(manifest.graphify_version)
            evidence_path = PurePosixPath("graphify-out/GRAPH_EVIDENCE.json")
            evidence_binding = next(
                item for item in parsed.payloads if item.path == evidence_path
            )
            parse_graph_evidence(
                parsed.read_payload(
                    evidence_path,
                    GRAPH_EVIDENCE_MAX_BYTES,
                ),
                compatibility,
                expected_digest=evidence_binding.sha256,
            )
            validated = validate_candidate(
                parsed.root, before, manifest,
                expected_projection_digest=before.projection_digest,
                expected_evidence_digest=evidence_binding.sha256,
                build_epoch=parsed.artifact.build_epoch,
                git_identity=parsed.artifact.git,
            )
            immediately_before = stage_input(
                repo_root, manifest, private / "source-immediately-before",
                repository_access=repository,
            )
            _require_same_validation_projection(before, immediately_before)
            assert_current_manifest_unchanged(
                repo_root, manifest, repository_access=repository
            )
            promoted = promote_graph(
                validated, repo_root, repository_access=repository
            )
            promotion_committed = promoted.changed is True
            if promotion_committed:
                commit_state.promotion_committed = True
                commit_state.project_id = manifest.project_id
                commit_state.project_uid = str(manifest.project_uid)
            installed = _revalidate_installed_generation(
                repo_root, manifest,
                repository_access=repository,
            )
            if promotion_committed and installed is not None:
                commit_state.installed = installed
            installed_identity_matches = (
                installed is not None
                and promoted.digest == installed.graph_digest
                and promoted.generation_digest == installed.generation_digest
                and promoted.build_epoch == installed.build_epoch
                and installed.graph_digest == validated.graph_digest
                and installed.generation_digest == validated.generation_digest
                and (
                    not promoted.changed
                    or installed.build_epoch == validated.build_epoch
                )
            )
            try:
                after = stage_input(
                    repo_root, manifest, private / "source-after",
                    repository_access=repository,
                )
                assert_current_manifest_unchanged(
                    repo_root, manifest, repository_access=repository
                )
                current = (
                    installed_identity_matches
                    and _same_validation_projection(before, after)
                )
            except (StagingError, ManifestError):
                current = False
            if not current:
                result = InstallResult(
                    project_id=manifest.project_id,
                    project_uid=str(manifest.project_uid),
                    graph_digest=(None if installed is None else installed.graph_digest),
                    generation_digest=(
                        None if installed is None else installed.generation_digest
                    ),
                    build_epoch=(None if installed is None else installed.build_epoch),
                    changed=(promoted.changed if installed_identity_matches else None),
                    status="promoted_but_stale",
                )
            else:
                assert installed is not None
                result = InstallResult(
                    project_id=manifest.project_id,
                    project_uid=str(manifest.project_uid),
                    graph_digest=installed.graph_digest,
                    generation_digest=installed.generation_digest,
                    build_epoch=installed.build_epoch,
                    changed=promoted.changed,
                    status="installed" if promoted.changed else "already_current",
                )
        if temporary.cleanup_failed:
            if promotion_committed:
                return _postcommit_install_execution(
                    commit_state, recovery_id=recovery_id
                )
            raise BundleError(
                "bundle_cleanup_failed", "private install cleanup failed",
                recovery_id,
            )
        return _InstallExecution(result, promotion_committed)


def _postcommit_install_execution(
    state: _InstallCommitState, *, recovery_id: str | None = None,
) -> _InstallExecution:
    assert state.promotion_committed
    assert state.project_id is not None and state.project_uid is not None
    installed = state.installed
    return _InstallExecution(
        InstallResult(
            project_id=state.project_id,
            project_uid=state.project_uid,
            graph_digest=None if installed is None else installed.graph_digest,
            generation_digest=(
                None if installed is None else installed.generation_digest
            ),
            build_epoch=None if installed is None else installed.build_epoch,
            changed=None if installed is None else True,
            status="promoted_but_stale",
            recovery_id=recovery_id,
        ),
        promotion_committed=True,
    )
```

`_same_validation_projection()` compares the complete immutable validator input contract: `source_digest`, `projection_digest`, sorted safe paths, `projection_files`, `reason_counts`, and `coverage_approvals`. The before/immediately-before/after stages therefore use the same canonical privacy/coverage/scanner walk as `validate_candidate`; a lightweight `ProjectionSnapshot` is never passed where a `StagedInput` is required. The installer rejects a null schema-v2 projection digest, any bundled ownership file before mapping, and any mismatch among bundle, validated candidate, and descriptor-validated `PromotionResult` graph/generation/epoch identity; it regenerates ownership only through `validate_candidate`, preserves the immutable bundle `build_epoch` for a changed-generation promotion, validates current evidence/trust without promoting trust, and leaves the prior graph untouched on every pre-promotion failure. Exact-generation no-op ignores a different proposed bundle epoch, returns the descriptor-validated installed ownership epoch, and does not rewrite ownership.

After every committed promotion, `_revalidate_installed_generation` validates
the live owned tree through the same lease `RepositoryAccess` and returns graph
digest, generation digest, and positive epoch only from that validator. The
promotion summary is comparison-only. A summary mismatch with valid live
ownership returns `promoted_but_stale` using the revalidated identity; if live
ownership is unverifiable, graph digest, generation digest, epoch, and changed
are all `None`. It never mixes verified and unverified identity fields.
`promoted_but_stale` uses global exit code 3, demotes downstream trust, and does
not claim rollback. From lifecycle entry through post-promotion verification,
journal, manifest, staging, current ownership, promotion, and health are all
rooted in the one captured lease descriptor. A root replacement can neither
supply validation bytes nor receive the promoted graph.

The whole managed-root block is enclosed by an outer
`except OperationTempCleanupError` that raises the same closed
`BundleError("bundle_cleanup_failed", ..., recovery_id) from None` before
commit. The wrapper-owned `_InstallCommitState` is set immediately after a
changed `promote_graph` returns, so either `OperationTempCleanupError` or any
other ordinary postcommit `Exception` instead becomes
`_postcommit_install_execution`: it uses a descriptor-validated installed
identity captured before the failure when available, otherwise all identity
fields and `changed` are null. A coincident cleanup failure adds the recovery
ID; an ordinary postcommit verification failure alone does not. A non-ordinary
`BaseException` still wins. An exact-generation no-op never sets the marker and
therefore raises the closed cleanup error rather than fabricating a commit.

Same-process GitHub authorization uses a module-private identity registry:

```python
@dataclass(frozen=True)
class _PullAuthorization:
    nonce: str

_AUTHORIZED_PULLS: dict[int, tuple[_PullAuthorization, str, int, int]] = {}
_AUTHORIZED_PULLS_LOCK = threading.Lock()

@contextmanager
def registered_pull_authorization(
    bundle: Path,
    receipt: DownloadReceipt,
    verified: VerifiedAttestation,
    manifest: ProjectManifest,
) -> Iterator[_PullAuthorization]:
    authorization = _new_bound_pull_authorization(
        bundle, receipt, verified, manifest
    )
    entry = (
        authorization, receipt.archive_sha256,
        receipt.archive_size, receipt.identity.asset_id,
    )
    # The cleanup handler is active before the entry can become reachable.
    try:
        with _AUTHORIZED_PULLS_LOCK:
            if id(authorization) in _AUTHORIZED_PULLS:
                raise BundleError(
                    "bundle_unattested", "bundle is not authorized for install"
                )
            _AUTHORIZED_PULLS[id(authorization)] = entry
        _after_pull_authorization_insert_checkpoint()
        yield authorization
    finally:
        _discard_pull_authorization(authorization)
```

The stored tuple binds the exact authorization object identity, archive SHA-256, archive byte length, and immutable GitHub asset ID. Registration and consumption hold `_AUTHORIZED_PULLS_LOCK`; consumption atomically pops the entry before validation/promotion, so exactly one of two concurrent consumers can proceed. `_install_verified_pull_execution` rejects a copied/reconstructed object or mutated bundle and leaves no registry entry whether install succeeds or fails; the result-only wrapper preserves that behavior.

`_new_bound_pull_authorization` repeats the bounded file device/inode/size/hash
check against the verified receipt before constructing the nonce; it does not
insert anything. `registered_pull_authorization` is the only insertion API.
Its `try/finally` is established before the locked dictionary assignment, and
the injected checkpoint runs immediately after insertion but before the
context yields. Therefore assignment-adjacent failure, cancellation, or
`KeyboardInterrupt` cannot strand an entry. `_discard_pull_authorization`
holds the same lock and removes only an entry whose stored object `is` the
supplied authorization.

`_install_verified_pull_execution` wraps `_install_bundle` in `try/finally` and calls
`_discard_pull_authorization(authorization)` in the `finally`. Discard holds the
registry lock and removes an entry only when the registered authorization is
the exact same object; a copied object cannot revoke another capability. Normal
transport admission still atomically pops before validation. The final discard
is therefore a no-op after consumption, but it purges authorization when an
expected-identity/lifecycle failure happens before `_require_transport_authority`
can consume it.

- [ ] **Step 4: Add pre-promotion and post-promotion drift/race tests**

```python
def test_install_drift_before_promotion_preserves_old_graph(clean_clone, local_bundle, install_fault):
    old = tree_snapshot(clean_clone / "graphify-out")
    install_fault.before_promote(lambda: rewrite_safe_source(clean_clone))
    with pytest.raises(BundleError, match="bundle_source_drift"):
        install_local_bundle(clean_clone, local_bundle)
    assert tree_snapshot(clean_clone / "graphify-out") == old


def test_install_drift_after_promotion_is_explicit(clean_clone, local_bundle, install_fault):
    install_fault.after_promote(lambda: rewrite_safe_source(clean_clone))
    result = install_local_bundle(clean_clone, local_bundle)
    assert result.status == "promoted_but_stale"
    assert result.generation_digest == local_bundle.artifact.generation_digest
    manifest = load_manifest(clean_clone / ".graphify-project.yaml", clean_clone)
    assert assess_health(inspect_project_state(clean_clone, manifest)).core_status == "stale"

@pytest.mark.parametrize("field", ["digest", "generation_digest", "build_epoch"])
def test_install_malformed_promotion_summary_uses_only_live_owned_identity(
    clean_clone, local_bundle, install_fault, field,
) -> None:
    install_fault.corrupt_promotion_summary(
        field, "f" * 64 if field != "build_epoch" else None
    )
    result = install_local_bundle(clean_clone, local_bundle)
    manifest = load_manifest(clean_clone / ".graphify-project.yaml", clean_clone)
    owned = validate_owned_graph(clean_clone / "graphify-out", manifest)
    assert result.status == "promoted_but_stale"
    assert (result.graph_digest, result.generation_digest, result.build_epoch) == (
        owned.graph_digest, owned.generation_digest, owned.build_epoch
    )
    assert result.changed is None

def test_install_unverifiable_committed_tree_reports_null_complete_identity(
    clean_clone, local_bundle, install_fault,
) -> None:
    install_fault.corrupt_owned_tree_after_promotion()
    result = install_local_bundle(clean_clone, local_bundle)
    assert result.status == "promoted_but_stale"
    assert (
        result.graph_digest, result.generation_digest,
        result.build_epoch, result.changed,
    ) == (None, None, None, None)

def test_install_root_replacement_after_lock_promotes_only_original_descriptor(
    clean_clone, local_bundle, install_fault, tmp_path,
) -> None:
    original = tmp_path / "original"
    replacement = clean_clone
    install_fault.after_lock(
        lambda: replace_repository_root(
            replacement, original, attacker_repository_with_copied_identity(tmp_path)
        )
    )
    result = install_local_bundle(replacement, local_bundle)
    assert result.status in {"installed", "already_current"}
    assert (original / "graphify-out/graph.json").is_file()
    assert not (replacement / "graphify-out").exists()
    assert not (replacement / ".project-knowledge").exists()

def test_exact_generation_reinstall_reports_installed_identity_without_rewrite(
    clean_clone, local_bundle
) -> None:
    first = install_local_bundle(clean_clone, local_bundle)
    ownership_before = inode_and_bytes(clean_clone / "graphify-out" / OWNERSHIP_MANIFEST)
    second = install_local_bundle(clean_clone, local_bundle)
    assert second.status == "already_current"
    assert (second.graph_digest, second.generation_digest, second.build_epoch) == (
        first.graph_digest, first.generation_digest, first.build_epoch,
    )
    assert inode_and_bytes(clean_clone / "graphify-out" / OWNERSHIP_MANIFEST) == ownership_before

def test_exact_generation_noop_ignores_different_proposed_bundle_epoch(
    clean_clone, local_bundle
) -> None:
    installed = install_local_bundle(clean_clone, local_bundle)
    assert installed.build_epoch is not None
    proposed = repack_same_generation_with_epoch(
        local_bundle, installed.build_epoch + 100
    )
    result = install_local_bundle(clean_clone, proposed)
    assert result.status == "already_current"
    assert result.generation_digest == installed.generation_digest
    assert result.build_epoch == installed.build_epoch


def test_verified_pull_authorization_is_atomic_single_use(clean_clone, verified_download):
    results = run_two_threads(
        lambda: _install_verified_pull(
            clean_clone, verified_download.path, verified_download.authorization
        )
    )
    assert sum(isinstance(value, InstallResult) for value in results) == 1
    assert sum(isinstance(value, BundleError) and value.code == "bundle_unattested"
               for value in results) == 1

def test_verified_pull_identity_failure_purges_unconsumed_authorization(
    clean_clone, verified_download, tmp_path,
) -> None:
    with open_repository_access(clean_clone) as loaded:
        expected = loaded.identity
    clean_clone.rename(tmp_path / "original")
    replacement = repository_with_copied_id_uid(clean_clone)
    before = tree_snapshot(replacement)
    with pytest.raises(TransactionLockError) as raised:
        _install_verified_pull(
            replacement, verified_download.path,
            verified_download.authorization,
            expected_repository_identity=expected,
        )
    assert raised.value.kind == "authority"
    assert authorization_registry_size() == 0
    assert tree_snapshot(replacement) == before

@pytest.mark.parametrize("operation", ["pack", "install"])
@pytest.mark.parametrize("journal_state", ["recoverable", "corrupt"])
def test_artifact_library_mutations_recheck_init_journal_under_lock(
    operation, journal_state, artifact_fixture
) -> None:
    repo = artifact_fixture.repo
    seed_init_journal(repo, journal_state)
    before = tree_snapshot(repo)
    with pytest.raises(BundleError) as raised:
        invoke_artifact_library_operation(operation, artifact_fixture)
    assert raised.value.code == "init_recovery_required"
    assert tree_snapshot(repo) == before
```

Run: `uv run pytest -q tests/test_bundles_install.py tests/test_artifacts.py tests/test_health.py`

Expected: PASS with the old graph byte-identical for every failure before promotion.

- [ ] **Step 5: Commit local install**

```bash
git add src/project_knowledge/bundles.py tests/test_bundles_install.py
git commit -m "feat: install local graph bundles atomically"
```

### Task 4: Content-free local operation state and CI rendering

**Files:**
- Create: `src/project_knowledge/operation_state.py`
- Create: `tests/test_operation_state.py`
- Modify: `src/project_knowledge/bundles.py`
- Test: `tests/test_bundles_install.py`

**Interfaces:**
- Produces: `CoverageMetrics(represented: int, approved_omissions: int, denied: int)`.
- Produces: `SuccessfulOperation(operation: str, duration_ms: int, safe_file_count: int, coverage: CoverageMetrics, source_digest: str, projection_digest: str, graph_digest: str | None, generation_digest: str | None, build_epoch: int | None, artifact_channel: str | None)`.
- Produces: `FailureRecord(operation: str, failure_code: str)`.
- Produces: `OperationState(schema_version: int, last_success: SuccessfulOperation | None, last_failure: FailureRecord | None)`.
- Produces: `record_success(repo_root: Path, operation: SuccessfulOperation, *, repository_access: RepositoryAccess | None = None, expected_repository_identity: RepositoryIdentity | None = None) -> None`, the analogous `record_failure`, `load_operation_state(repo_root: Path, *, expected_repository_identity=None) -> OperationState`, and `render_ci_summary(results: Sequence[dict[str, object]]) -> tuple[bytes, str]`. Writer authority keywords are mutually exclusive; an access requires a live lifecycle lease.
- Produces internal `python -m project_knowledge.operation_state ci-summary --output-json PATH --preflight PATH --doctor PATH --scan PATH --health PATH` for workflow-only content-free rendering.

- [ ] **Step 1: Write failing state schema, redaction, and atomicity tests**

```python
def test_state_contains_only_content_free_closed_schema(repo):
    record_success(repo, SuccessfulOperation(
        operation="artifact_install", duration_ms=125, safe_file_count=7,
        coverage=CoverageMetrics(represented=6, approved_omissions=1, denied=3),
        source_digest="1" * 64, projection_digest="2" * 64,
        graph_digest="3" * 64, generation_digest="4" * 64,
        build_epoch=7, artifact_channel=None,
    ))
    payload = (repo / ".project-knowledge/state.json").read_text(encoding="utf-8")
    assert set(json.loads(payload)) == {"last_failure", "last_success", "schema_version"}
    assert str(repo) not in payload
    assert all(term not in payload for term in ("src/app.py", "query", "fingerprint", "API_KEY"))


def test_read_only_render_and_load_do_not_create_state(repo):
    before = tree_snapshot(repo)
    assert load_operation_state(repo).last_success is None
    render_ci_summary([{"project_id": "demo", "status": "healthy"}])
    assert tree_snapshot(repo) == before


def test_interrupted_state_replace_preserves_previous_record(repo, faulting_state_fs):
    write_first_valid_state(repo)
    before = (repo / ".project-knowledge/state.json").read_bytes()
    faulting_state_fs.fail_at("replace")
    with pytest.raises(OperationStateError, match="state_write_failed"):
        record_success(repo, second_operation(), fs=faulting_state_fs)
    assert (repo / ".project-knowledge/state.json").read_bytes() == before


def test_concurrent_pack_and_install_state_updates_do_not_clobber(repo, state_barrier):
    state_barrier.finish_install_then_pack()
    run_concurrent_pack_and_install(repo, state_barrier)
    state = load_operation_state(repo)
    assert state.last_success.operation == "artifact_pack"
    assert state_barrier.completed_updates == ["artifact_install", "artifact_pack"]
```

- [ ] **Step 2: Run and observe the missing module**

Run: `uv run pytest -q tests/test_operation_state.py`

Expected: collection fails for missing `project_knowledge.operation_state`.

- [ ] **Step 3: Implement strict private state and deterministic CI outputs**

State JSON uses canonical sorted keys, schema version 1, integer non-negative
counts/duration, lowercase 64-hex digests, operation names from an immutable
allowlist, and stable failure codes from the CLI error registry. Every update
takes the repository lifecycle lock then `.project-knowledge/state.lock`.
Install records through its already captured lease access before releasing the
transaction. Pack retains the captured repository identity and, after archive
publication, reacquires the lifecycle boundary with that expected identity
before recording; replacement state is never touched. A direct writer without
an access likewise acquires/validates the optional identity itself. Read-only
load uses `open_repository_access` and never creates state. Open the real
mode-0700 state directory relative to that repository access, read existing
state via `openat(O_NOFOLLOW)` with pre/open/post inode binding, write a
mode-0600 sibling temp with `O_EXCL|O_NOFOLLOW`, fsync it, verify the destination
still has the captured binding, `renameat` within the opened parent, then fsync
the directory. Reject symlink/wrong-type/swap/duplicate-key/non-finite/unknown-
field state and serialize concurrent pack/install updates so the last completed
operation wins without a lost update.

`render_ci_summary()` returns canonical machine JSON and Markdown containing only project ID/UID, operation, status, counts, duration, source/projection/graph/generation digests, installed build epoch, channel, trust, and stable limitations. It rejects values under keys matching `path`, `file`, `query`, `environment`, `fingerprint`, or `message` rather than trying to redact arbitrary content. Artifact pack/install success records require non-null graph/generation digests and a positive exact epoch. A committed-but-unverifiable install has all installed-identity fields null in its result, records the last verified successful identity plus a stable failure, and never promotes partial/unverified identity into success state.

The module's strict `argparse` `__main__` accepts only the `ci-summary` verb and exact file arguments above. It reads each bounded duplicate-key-free JSON envelope, projects only those allowlisted fields, writes canonical JSON to the exclusive requested output, and prints Markdown to stdout. The preflight projection admits only backend/model/deep mode and `credential_bound: bool`, never an environment name or credential. It never writes repository state.

Mutation orchestration records success only after its correctness result is final. It records a stable failure code when private state is already safely writable, but failure to write state never changes a pre-promotion failure into a success. Read-only doctor/health/query/fleet-health never call either writer.

- [ ] **Step 4: Wire pack/install success and failure records**

Add tests asserting pack records `artifact_pack`, install records `artifact_install`, `promoted_but_stale` records a failure code plus the last successful promotion, and neither bundle bytes nor CLI output includes the local state document.

Run: `uv run pytest -q tests/test_operation_state.py tests/test_bundles_pack.py tests/test_bundles_install.py`

Expected: PASS.

- [ ] **Step 5: Commit operation state**

```bash
git add src/project_knowledge/operation_state.py src/project_knowledge/bundles.py tests/test_operation_state.py tests/test_bundles_pack.py tests/test_bundles_install.py
git commit -m "feat: record content-free graph operations"
```

### Task 5: Bounded GitHub Release resolution and download

**Files:**
- Create: `src/project_knowledge/github_artifacts.py`
- Create: `tests/test_github_artifacts.py`
- Test: `tests/test_operation_state.py`

**Interfaces:**
- Consumes: `ArtifactManifest`, core `ArtifactIntent`, `inspect_bundle_manifest`, `V1_LIMITS`, and the current `ProjectionSnapshot` from `inspect_projection()`.
- Produces: `GithubArtifactError(code: str, message: str, recovery_id: str | None = None)`; a non-null constructor-validated recovery ID is allowed only for `github_cleanup_failed`.
- Produces: `HttpsRequest(method: Literal["GET"], url: str, headers: tuple[tuple[str, str], ...], timeout_seconds: float)`.
- Produces protocol: `HttpsTransport.open(request: HttpsRequest) -> HttpsResponse`, where `HttpsResponse.status`, `headers`, `read(size)`, and `close()` are bounded/injectable.
- Produces: `ReleaseAssetIdentity(repository_id: int, release_id: int, tag: str, asset_id: int, asset_name: str, asset_size: int, asset_digest: str, generation_digest: str, source_commit_oid: str)`.
- Produces: `DownloadReceipt(identity: ReleaseAssetIdentity, archive_sha256: str, archive_size: int, artifact_git_commit_oid: str)`.
- Produces: `GithubCredentials(token: str = field(repr=False))`; `__post_init__` requires an exact `str`, UTF-8 length 1..4096 bytes, and no C0/C1 controls, DEL, CR/LF, or surrounding whitespace. Every invalid value raises only `GithubArtifactError("github_token_required", "GitHub credential is required")` before request/header construction.
- Produces: `resolve_and_download(config: ArtifactIntent, project_uid: UUID, projection: ProjectionSnapshot, destination: Path, credentials: GithubCredentials, transport: HttpsTransport = REAL_HTTPS, *, before_request: Callable[[], None] = _noop) -> DownloadReceipt`. The private callback is a library-test/high-level binding hook, never public configuration, and is invoked immediately before every HTTP open including every redirect hop; an exception propagates before request headers, credentials, or bytes are sent.

- [ ] **Step 1: Write fake-transport tests for exact API construction and immutable identity**

```python
def test_resolver_binds_repository_release_asset_and_commit(github_config, fake_https, tmp_path):
    bundle_bytes = github_bundle_bytes(git_oid="b" * 40)
    archive_sha = hashlib.sha256(bundle_bytes).hexdigest()
    fake_https.queue_json("https://api.github.com/repos/acme/widgets", {
        "id": 123456789, "full_name": "acme/widgets"
    })
    fake_https.queue_json(
        "https://api.github.com/repos/acme/widgets/releases/tags/atlasweaver-graph-4ed9af24-5aa2-4eac-8d0a-3f622cc74948-main",
        release_response(release_id=44, asset_id=55, digest="sha256:" + archive_sha),
    )
    fake_https.queue_json("https://api.github.com/repos/acme/widgets/commits/" + "b" * 40, {
        "sha": "b" * 40,
    })
    fake_https.queue_redirect(
        "https://api.github.com/repos/acme/widgets/releases/assets/55",
        "https://release-assets.githubusercontent.com/github-production-release-asset/file.zip",
    )
    fake_https.queue_bytes(
        "https://release-assets.githubusercontent.com/github-production-release-asset/file.zip",
        bundle_bytes,
    )
    receipt = resolve_and_download(
        github_config, PROJECT_UID, projection("1" * 64, "2" * 64),
        tmp_path / "download.zip", GithubCredentials("token-value"), fake_https,
    )
    assert receipt.identity == ReleaseAssetIdentity(
        repository_id=123456789, release_id=44,
        tag="atlasweaver-graph-4ed9af24-5aa2-4eac-8d0a-3f622cc74948-main",
        asset_id=55,
        asset_name=("atlasweaver-graph-4ed9af24-5aa2-4eac-8d0a-3f622cc74948-"
                    + "1" * 64 + "-" + "2" * 64 + "-" + archive_sha + ".zip"),
        asset_size=len(bundle_bytes),
        asset_digest=archive_sha, generation_digest="3" * 64,
        source_commit_oid="b" * 40,
    )

def test_before_request_runs_for_every_api_and_redirect_hop(
    github_config, fake_https, current_projection, tmp_path,
) -> None:
    seed_successful_resolution(fake_https, git_oid="b" * 40)
    checkpoints: list[int] = []
    resolve_and_download(
        github_config, PROJECT_UID, current_projection,
        tmp_path / "download.zip", GithubCredentials("token-value"), fake_https,
        before_request=lambda: checkpoints.append(len(fake_https.requests)),
    )
    assert len(checkpoints) == len(fake_https.requests)

def test_before_request_failure_precedes_next_https_open_and_credentials(
    github_config, fake_https, current_projection, tmp_path,
) -> None:
    seed_successful_resolution(fake_https, git_oid="b" * 40)
    calls = 0
    def stop_before_second_request() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ManifestError("project manifest changed", kind="changed")
    with pytest.raises(ManifestError) as raised:
        resolve_and_download(
            github_config, PROJECT_UID, current_projection,
            tmp_path / "download.zip", GithubCredentials("must-remain-unused"),
            fake_https, before_request=stop_before_second_request,
        )
    assert raised.value.kind == "changed"
    assert len(fake_https.requests) == 1


@pytest.mark.parametrize("token", [
    "", " token", "token ", "line\nbreak", "header\rvalue", "nul\x00value",
    "x" * 4097, b"not-text", True,
])
def test_github_credentials_reject_invalid_header_values_before_request(
    github_config, fake_https, current_projection, tmp_path, token,
) -> None:
    with pytest.raises(GithubArtifactError) as raised:
        credentials = GithubCredentials(token)
        resolve_and_download(
            github_config, PROJECT_UID, current_projection,
            tmp_path / "download.zip", credentials, fake_https,
        )
    assert raised.value.code == "github_token_required"
    assert fake_https.requests == []
```

The fake records every request and asserts `Accept: application/vnd.github+json` for JSON API calls, `Accept: application/octet-stream` for the asset endpoint, `X-GitHub-Api-Version: 2022-11-28`, and `Authorization: Bearer token-value` only on `api.github.com`.

- [ ] **Step 2: Write redirect, limit, and ambient-state rejection tests**

```python
@pytest.mark.parametrize("location", [
    "http://release-assets.githubusercontent.com/file.zip",
    "https://evil.example/file.zip",
    "https://user:password@github.com/file.zip",
    "//evil.example/file.zip",
])
def test_download_rejects_unsafe_redirect_and_removes_partial_file(
    github_config, fake_https, current_projection, tmp_path, location
):
    destination = tmp_path / "download.zip"
    seed_successful_resolution(fake_https, git_oid="b" * 40)
    fake_https.queue_redirect(asset_api_url(), location)
    with pytest.raises(GithubArtifactError, match="github_redirect_invalid"):
        resolve_and_download(
            github_config, PROJECT_UID, current_projection,
            destination, GithubCredentials("token-value"), fake_https,
        )
    assert not destination.exists()


def test_cross_host_redirect_strips_authorization(
    github_config, fake_https, current_projection, tmp_path
):
    seed_successful_resolution(fake_https, git_oid="b" * 40)
    resolve_and_download(
        github_config, PROJECT_UID, current_projection,
        tmp_path / "download.zip", GithubCredentials("token-value"), fake_https,
    )
    redirected = fake_https.requests[-1]
    assert dict(redirected.headers).get("Authorization") is None


def test_retained_asset_commit_is_bound_to_artifact_not_advanced_rolling_target(
    github_config, fake_https, tmp_path
):
    fake_https.seed_release(target_commitish="main", current_main_oid="c" * 40)
    fake_https.seed_digest_asset(git_oid="b" * 40)
    receipt = resolve_and_download(
        github_config, PROJECT_UID, projection("1" * 64, "2" * 64),
        tmp_path / "old.zip", GithubCredentials("token-value"), fake_https,
    )
    assert receipt.identity.source_commit_oid == "b" * 40
    assert fake_https.requested("/repos/acme/widgets/commits/" + "b" * 40)
    assert not fake_https.requested("/repos/acme/widgets/commits/main")


@pytest.mark.parametrize("failure", [
    "content_length_missing", "content_length_mismatch", "stream_truncated",
    "stream_exceeds_limit", "fourth_redirect", "timeout", "tls_failure",
    "repository_id_mismatch", "release_tag_mismatch", "duplicate_asset_name",
    "asset_size_mismatch", "asset_digest_mismatch", "source_commit_mismatch",
])
def test_download_fails_closed_with_stable_code(
    github_config, fake_https, current_projection, tmp_path, failure
):
    destination = tmp_path / "download.zip"
    configure_failure(fake_https, failure)
    with pytest.raises(GithubArtifactError) as caught:
        resolve_and_download(
            github_config, PROJECT_UID, current_projection,
            destination, GithubCredentials("token-value"), fake_https,
        )
    assert caught.value.code.startswith("github_")
    assert not destination.exists()
```

- [ ] **Step 3: Run the tests and verify the module is absent**

Run: `uv run pytest -q tests/test_github_artifacts.py -k 'resolver or redirect or download'`

Expected: collection fails with missing `project_knowledge.github_artifacts`.

- [ ] **Step 4: Implement a no-ambient-state HTTPS boundary**

`REAL_HTTPS` uses `http.client.HTTPSConnection(host, port=443, context=ssl.create_default_context())`; it does not use `urllib.request`, environment proxy lookup, netrc, cookies, or a global opener. Parse URLs with `urllib.parse.urlsplit` and require: scheme exactly `https`, no username/password/fragment, default port 443, normalized hostname in `ALLOWED_GITHUB_HOSTS`, and a request path beginning with `/`. Revalidate every redirect and close the prior response before following it. Timeouts are fixed to connect/read 15 seconds; callers cannot override them from CLI/workspace/workflow input.

Use exact constructors:

```python
ALLOWED_GITHUB_HOSTS = frozenset({
    "api.github.com", "github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})
MAX_REDIRECTS = 3
CONNECT_TIMEOUT_SECONDS = 15.0
READ_CHUNK_BYTES = 1024 * 1024

def _headers(host: str, token: str, *, asset: bool) -> tuple[tuple[str, str], ...]:
    headers = [
        ("Accept", "application/octet-stream" if asset else "application/vnd.github+json"),
        ("User-Agent", "atlasweaver-artifact-client"),
        ("X-GitHub-Api-Version", "2022-11-28"),
    ]
    if host == "api.github.com":
        headers.append(("Authorization", f"Bearer {token}"))
    return tuple(headers)
```

Read each JSON response with a 1 MiB cap, strict duplicate-key/non-finite JSON, and a closed projection of required fields. Resolve repository identity and the rolling-tag release. Select only assets whose exact name matches the UID/current-source/current-projection prefix plus one lowercase full bundle SHA-256; sort matching assets by validated `(created_at, numeric_id)` newest-first and choose exactly the newest, rejecting duplicate exact names/IDs or malformed/ambiguous ordering. The clean clone need not know generation identity before download. Stream the selected asset while hashing it and require the computed archive SHA-256, GitHub's `sha256:<hex>` digest, and the exact final name component to agree before bounded structural parsing of `artifact.json`. Then resolve `/repos/<owner>/<repo>/commits/<claimed_git_commit_oid>` and require the exact returned SHA/repository; do not derive the asset's source commit from the rolling release `target_commitish`, because retained digest-addressed assets remain valid after the rolling branch advances. Require numeric IDs to be positive non-boolean integers and every returned name/digest/size plus full manifest source/projection/generation identity to match configuration/current projection and the parsed bundle. Generation digest and bundle SHA-256 are distinct domains and are never compared.

Construct and validate `GithubCredentials` before `_headers`, URL parsing,
transport selection, or any `HttpsTransport.open`. `_headers` accepts only that
validated object/token and cannot encounter a raw header-value `ValueError`;
defense-in-depth translates any unexpected header/HTTP library `Exception` to
the operation's closed GitHub error family from `None`.

Stream the asset to an exclusive mode-0600 file in a mode-0700 parent, enforce header length and the bundle hard maximum while reading, fsync, then call `inspect_bundle_manifest()` without parsing graph content. Require claimed Git OID to equal the API-resolved source commit. Finally require the streamed SHA-256 to match GitHub's `sha256:<hex>` asset digest and return the private receipt. Any failure descriptor-unlinks the file and returns a redacted stable code.

- [ ] **Step 5: Run the complete GitHub transport suite**

Run: `uv run pytest -q tests/test_github_artifacts.py`

Expected: the resolution/download tests pass; attestation/pull tests added in Task 6 remain deselected by `-k` until their APIs exist.

- [ ] **Step 6: Commit GitHub resolution and download**

```bash
git add src/project_knowledge/github_artifacts.py tests/test_github_artifacts.py
git commit -m "feat: download graph bundles from verified GitHub releases"
```

### Task 6: Artifact attestation verification and atomic pull

**Files:**
- Modify: `src/project_knowledge/github_artifacts.py`
- Modify: `src/project_knowledge/bundles.py`
- Modify: `tests/test_github_artifacts.py`
- Test: `tests/test_bundles_install.py`
- Test: `tests/test_operation_state.py`

**Interfaces:**
- Consumes: `resolve_and_download`, `DownloadReceipt`, private `_PullAuthorization`, result-only `_install_verified_pull`, and commit-aware `_install_verified_pull_execution`.
- Produces protocol: `GhCommandRunner.run(argv: tuple[str, ...], env: Mapping[str, str], timeout_seconds: float, output_limit: int) -> CompletedCommand`; this transport runner is distinct from Core's Graphify `CommandRunner`.
- Produces: `VerifiedAttestation(subject_sha256: str, signer_workflow: str, signer_digest: str, source_ref: str, source_digest: str, predicate_type: str)`.
- Produces: `ResolvedGhExecutable(path: Path, device: int, inode: int, sha256: str, version: str)` and narrow protocol `GhToolResolver.resolve(*, before_exec: Callable[[], None] = _noop) -> ResolvedGhExecutable`; the callback runs immediately before each bounded capability child.
- Produces private `SYSTEM_GH_RESOLVER` and `resolve_gh_executable(resolver: GhToolResolver = SYSTEM_GH_RESOLVER) -> ResolvedGhExecutable`; neither can resolve Graphify and neither is exposed through CLI/workflow input.
- Produces: `AttestationPolicy(repository: str, signer_workflow: str, signer_digest: str, source_ref: str, source_digest: str, predicate_type: str, deny_self_hosted_runners: bool = True)` and `verify_attestation_policy(bundle: Path, policy: AttestationPolicy, credentials: GithubCredentials, gh: ResolvedGhExecutable, runner: GhCommandRunner = SUBPROCESS_GH_RUNNER, *, before_exec: Callable[[], None] = _noop) -> VerifiedAttestation`. This primitive authenticates a subject/policy and knows nothing about releases, assets, or pull authorization; the callback runs immediately before the verifier child.
- Produces: `verify_attestation(bundle: Path, receipt: DownloadReceipt, config: ArtifactIntent, credentials: GithubCredentials, gh: ResolvedGhExecutable, runner: GhCommandRunner = SUBPROCESS_GH_RUNNER, *, before_exec: Callable[[], None] = _noop) -> VerifiedAttestation`.
- Produces: `PullDependencies(transport: HttpsTransport, runner: GhCommandRunner, gh_resolver: GhToolResolver)` for test injection only.
- Produces: `PullResult(download: DownloadReceipt, install: InstallResult)` and `pull_bundle(repo_root: Path, credentials: GithubCredentials, *, dependencies: PullDependencies = REAL_PULL_DEPENDENCIES, expected_repository_identity: RepositoryIdentity | None = None, expected_manifest: ProjectManifest | None = None) -> PullResult`. The expected manifest is a library-only CLI/fleet admission binding, never a CLI flag.

- [ ] **Step 1: Write failing exact-argv/minimal-environment attestation tests**

```python
def test_attestation_verifier_uses_exact_policy_and_minimal_environment(tmp_path, receipt, github_config, fake_runner):
    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"verified bytes")
    fake_runner.result = gh_result(subject=sha256(bundle.read_bytes()), source="b" * 40)
    gh = ResolvedGhExecutable(Path("/usr/bin/gh"), 1, 2, "d" * 64, "2.80.0")
    verified = verify_attestation(
        bundle, receipt, github_config, GithubCredentials("secret"), gh, fake_runner
    )
    assert fake_runner.argv == (
        "/usr/bin/gh", "attestation", "verify", str(bundle),
        "--hostname", "github.com", "--repo", "acme/widgets",
        "--signer-workflow", "acme/atlasweaver/.github/workflows/atlasweaver-publish.yml",
        "--signer-digest", "c" * 40,
        "--source-ref", "refs/heads/main", "--source-digest", "b" * 40,
        "--predicate-type", "https://slsa.dev/provenance/v1",
        "--deny-self-hosted-runners", "--format", "json",
    )
    assert set(fake_runner.env) == {"GH_CONFIG_DIR", "GH_TOKEN", "LANG", "LC_ALL"}
    assert fake_runner.env["GH_TOKEN"] == "secret"
    assert verified.subject_sha256 == sha256(bundle.read_bytes())


def test_policy_verifier_requires_no_release_receipt_and_cannot_authorize_pull(
    tmp_path, workflow_policy, fake_runner, resolved_gh,
) -> None:
    bundle = tmp_path / "prepared.zip"
    bundle.write_bytes(b"prepared publication bytes")
    fake_runner.result = gh_result(
        subject=sha256(bundle.read_bytes()),
        source=workflow_policy.source_digest,
    )
    verified = verify_attestation_policy(
        bundle, workflow_policy, GithubCredentials("token"),
        resolved_gh, fake_runner,
    )
    assert verified.subject_sha256 == sha256(bundle.read_bytes())
    assert active_pull_authorization_count() == 0
```

The test runner must also assert a 60-second timeout, a 256 KiB combined stdout/stderr cap, a mode-0700 empty `GH_CONFIG_DIR`, and deletion of that directory after success/failure.

- [ ] **Step 2: Add malformed/ambiguous provenance and pull race tests**

```python
@pytest.mark.parametrize("case", [
    "missing_result", "conflicting_results", "wrong_subject", "wrong_repository",
    "wrong_workflow", "wrong_signer_digest", "wrong_source_ref", "wrong_source_commit",
    "wrong_predicate", "self_hosted", "duplicate_json_key", "nonfinite_json",
    "oversized_output", "timeout", "nonzero_exit", "one_char_token",
])
def test_attestation_failure_is_redacted_and_non_authorizing(
    tmp_path, receipt, github_config, fake_runner, resolved_gh, case
):
    bundle = tmp_path / "bundle.zip"
    original = b"untrusted bundle bytes"
    bundle.write_bytes(original)
    token = "x" if case == "one_char_token" else "token-value"
    raw_stdout = f"runner stdout {token} {bundle}"
    raw_stderr = f"runner stderr {token} {bundle}"
    fake_runner.result = malformed_gh_result(
        case, stdout=raw_stdout, stderr=raw_stderr
    )
    with pytest.raises(GithubArtifactError, match="attestation_") as raised:
        verify_attestation(
            bundle, receipt, github_config, GithubCredentials(token),
            resolved_gh, fake_runner
        )
    rendered = f"{raised.value!s}\n{raised.value!r}"
    assert token not in rendered
    assert raw_stdout not in rendered
    assert raw_stderr not in rendered
    assert str(bundle) not in rendered
    assert bundle.read_bytes() == original
    assert active_pull_authorization_count() == 0


def test_repeated_identical_policy_attestations_are_idempotent(
    bundle, receipt, github_config, fake_runner, resolved_gh,
) -> None:
    fake_runner.result = two_valid_results_with_same_authority_projection(
        bundle, receipt, github_config
    )
    verified = verify_attestation(
        bundle, receipt, github_config, GithubCredentials("token"),
        resolved_gh, fake_runner,
    )
    assert verified.subject_sha256 == sha256(bundle.read_bytes())


def test_pull_rechecks_projection_under_lock_before_promotion(repo, fake_https, fake_runner, pull_fault):
    pull_fault.after_attestation(lambda: rewrite_safe_source(repo))
    before = tree_snapshot(repo / "graphify-out")
    with pytest.raises(GithubArtifactError, match="bundle_source_drift"):
        pull_bundle(
            repo, GithubCredentials("token-value"),
            dependencies=PullDependencies(
                transport=fake_https, runner=fake_runner,
                gh_resolver=fake_gh_resolver,
            ),
        )
    assert tree_snapshot(repo / "graphify-out") == before

def test_pull_expected_identity_mismatch_precedes_https_gh_and_token_use(
    repo, fake_https, fake_runner, tmp_path,
) -> None:
    with open_repository_access(repo) as loaded:
        expected = loaded.identity
    repo.rename(tmp_path / "original")
    replacement = github_repository_with_copied_id_uid(
        repo, repository="attacker/redirect"
    )
    with pytest.raises(TransactionLockError) as raised:
        pull_bundle(
            replacement, GithubCredentials("must-remain-unused"),
            dependencies=PullDependencies(
                transport=fake_https, runner=fake_runner,
                gh_resolver=fake_gh_resolver,
            ),
            expected_repository_identity=expected,
        )
    assert raised.value.kind == "authority"
    assert fake_https.calls == []
    assert fake_runner.calls == []
    assert not (replacement / ".project-knowledge").exists()


def test_pull_expected_manifest_mismatch_precedes_network_and_token_use(
    repo, fake_https, fake_runner,
) -> None:
    admitted = load_manifest(repo / ".graphify-project.yaml", repo)
    rewrite_manifest_semantically(
        repo,
        project_id=admitted.project_id,
        project_uid=admitted.project_uid,
        artifacts=github_artifacts(repository="attacker/redirect"),
    )
    with pytest.raises(ManifestError) as raised:
        pull_bundle(
            repo, GithubCredentials("must-remain-unused"),
            dependencies=PullDependencies(
                transport=fake_https, runner=fake_runner,
                gh_resolver=fake_gh_resolver,
            ),
            expected_manifest=admitted,
        )
    assert raised.value.kind == "changed"
    assert fake_https.calls == []
    assert fake_runner.calls == []


@pytest.mark.parametrize(
    "checkpoint,https_may_run,gh_may_run",
    [
        ("before_first_https", False, False),
        ("after_download", True, False),
        ("during_gh_capability", True, True),
        ("before_attestation", True, True),
        ("before_authorization", True, True),
        ("before_install_handoff", True, True),
    ],
)
def test_pull_rewrite_restore_is_detected_at_every_privileged_boundary(
    repo, fake_https, fake_runner, pull_fault,
    checkpoint: str, https_may_run: bool, gh_may_run: bool,
) -> None:
    pull_fault.rewrite_and_restore_manifest_at(checkpoint, repo)
    with pytest.raises(ManifestError) as raised:
        pull_bundle(
            repo, GithubCredentials("token"),
            dependencies=pull_fault.dependencies(fake_https, fake_runner),
        )
    assert raised.value.kind == "changed"
    assert bool(fake_https.calls) is https_may_run
    assert bool(fake_runner.calls) is gh_may_run
    assert pull_fault.install_calls == []
    assert active_pull_authorization_count() == 0


def test_pull_manifest_drift_after_committed_install_returns_stale_result(
    repo, fake_https, fake_runner, pull_fault,
) -> None:
    pull_fault.rewrite_and_restore_manifest_at("after_install", repo)
    result = pull_bundle(
        repo, GithubCredentials("token"),
        dependencies=pull_fault.dependencies(fake_https, fake_runner),
    )
    assert result.install.status == "promoted_but_stale"
    assert result.install.graph_digest == pull_fault.installed_identity.graph_digest
    assert result.install.generation_digest == pull_fault.installed_identity.generation_digest
    assert result.install.build_epoch == pull_fault.installed_identity.build_epoch
    assert active_pull_authorization_count() == 0


def test_verified_authorization_is_single_use(repo, verified_download):
    first = _install_verified_pull(repo, verified_download.path, verified_download.authorization)
    assert first.status in {"installed", "already_current"}
    with pytest.raises(BundleError, match="bundle_unattested"):
        _install_verified_pull(repo, verified_download.path, verified_download.authorization)


def test_pull_handoff_failure_discards_registered_authorization(
    repo, fake_https, fake_runner, pull_fault,
) -> None:
    pull_fault.after_authorization_registered(
        lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        pull_bundle(
            repo, GithubCredentials("token"),
            dependencies=PullDependencies(
                transport=fake_https, runner=fake_runner,
                gh_resolver=fake_gh_resolver,
            ),
        )
    assert active_pull_authorization_count() == 0


def test_pull_cleanup_failure_after_exact_noop_is_closed_github_error(
    repo, verified_download, operation_temp_fault,
) -> None:
    install_local_bundle(repo, verified_download.path_for_local_install)
    operation_temp_fault.fail_cleanup("pull")
    with pytest.raises(GithubArtifactError) as raised:
        verified_download.pull_into(repo)
    assert raised.value.code == "github_cleanup_failed"
    assert raised.value.recovery_id is not None


def test_pull_cleanup_failure_after_committed_install_returns_stale_recovery(
    repo, verified_download, operation_temp_fault,
) -> None:
    operation_temp_fault.fail_cleanup("pull")
    result = verified_download.pull_into(repo)
    assert result.install.status == "promoted_but_stale"
    assert result.install.recovery_id is not None


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_pull_postcommit_install_exception_preserves_commit_outcome(
    repo, verified_download, install_fault, operation_temp_fault,
    cleanup_fails: bool,
) -> None:
    install_fault.raise_during_postcommit_revalidation(RuntimeError("private"))
    if cleanup_fails:
        operation_temp_fault.fail_cleanup("install")
    result = verified_download.pull_into(repo)
    assert result.install.status == "promoted_but_stale"
    assert (
        result.install.graph_digest, result.install.generation_digest,
        result.install.build_epoch, result.install.changed,
    ) == (None, None, None, None)
    assert (result.install.recovery_id is not None) is cleanup_fails
    assert active_pull_authorization_count() == 0


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_pull_outer_postinstall_exception_preserves_commit_outcome(
    repo, verified_download, pull_fault, operation_temp_fault,
    cleanup_fails: bool,
) -> None:
    pull_fault.after_install(
        lambda: (_ for _ in ()).throw(RuntimeError("private"))
    )
    if cleanup_fails:
        operation_temp_fault.fail_cleanup("pull")
    result = verified_download.pull_into(repo)
    assert result.install.status == "promoted_but_stale"
    assert (
        result.install.graph_digest, result.install.generation_digest,
        result.install.build_epoch,
    ) == (
        pull_fault.installed_identity.graph_digest,
        pull_fault.installed_identity.generation_digest,
        pull_fault.installed_identity.build_epoch,
    )
    assert (result.install.recovery_id is not None) is cleanup_fails
    assert active_pull_authorization_count() == 0


def test_authorization_registration_baseexception_cannot_leak_entry(
    verified_download, authorization_fault,
) -> None:
    authorization_fault.after_registry_insert(
        lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        with registered_pull_authorization(
            verified_download.path,
            verified_download.receipt,
            verified_download.attestation,
            verified_download.manifest,
        ):
            pytest.fail("registration checkpoint should have interrupted")
    assert active_pull_authorization_count() == 0
```

- [ ] **Step 3: Run focused tests and confirm attestation APIs are missing**

Run: `uv run pytest -q tests/test_github_artifacts.py -k 'attestation or pull or authorization'`

Expected: FAIL for missing `verify_attestation`/`pull_bundle`.

- [ ] **Step 4: Implement capability-checked verification and exact result projection**

Resolve `gh` exactly once through Task 6's private, gh-only `SYSTEM_GH_RESOLVER`; no public CLI/request/workspace/workflow input can name an alternate executable. It returns an absolute no-follow regular path, captures device/inode/SHA-256, and capability-checks `--version` plus `attestation verify --help` through the bounded process-group runner. Revalidate the gh executable binding immediately before exec and invoke only its absolute path under the minimal environment. This boundary has no `graphify` name, branch, type, or return value; Graphify authority remains exclusively in Impact/Core. `verify_attestation_policy` owns the exact attestation flags listed in the spec. Use no shell. Construct only the exact argv shown in Step 1. Parse stdout with duplicate-key rejection and require at least one result. Every returned result must have the same authority-relevant authenticated projection and must contain the ZIP subject SHA-256, policy workflow identity and digest, policy source ref/digest, SLSA provenance predicate type, and hosted-runner policy. Identical repeated valid attestations are deduplicated to one `VerifiedAttestation`, making workflow retries safe; any conflicting or malformed result fails closed as `attestation_ambiguous`. Ignore free-form statement predicate fields when making authority decisions.

`verify_attestation` is the pull-only wrapper: derive an
`AttestationPolicy` from the strict local config plus receipt source commit,
call `verify_attestation_policy`, then require all four pull identity sources to
agree in their corresponding domains: the TLS GitHub API receipt authenticates
immutable numeric repository ID plus release/asset identity; the verifier
authenticates repository slug, subject, workflow, and source; local manifest
config and untrusted bounded `artifact.json` claims must agree with both. A caller-fabricated/missing receipt can
never enter this wrapper. Neither verifier creates `_PullAuthorization`; only
`pull_bundle`, after all comparisons succeed, registers one authorization bound
to the exact file/receipt. The policy primitive cannot be passed to install and
is insufficient to authorize a downloaded release asset.

`pull_bundle()` sequence is exact:

```python
def pull_bundle(
    repo_root: Path,
    credentials: GithubCredentials,
    *,
    dependencies: PullDependencies = REAL_PULL_DEPENDENCIES,
    expected_repository_identity: RepositoryIdentity | None = None,
    expected_manifest: ProjectManifest | None = None,
) -> PullResult:
    with open_repository_access(
        repo_root,
        expected_repository_identity=expected_repository_identity,
    ) as repository:
        if inspect_init_journal(
            repo_root, repository_access=repository
        ) != "none":
            raise GithubArtifactError(
                "init_recovery_required", "configuration recovery is required"
            )
        if expected_manifest is None:
            manifest = load_manifest(
                repo_root / ".graphify-project.yaml", repo_root,
                repository_access=repository,
            )
            manifest = require_current_manifest(
                repo_root, manifest, repository_access=repository
            )
        else:
            manifest = require_current_manifest(
                repo_root, expected_manifest,
                repository_access=repository,
            )
        config = require_github_release_provider(manifest.artifacts)
        projection = inspect_projection(
            repo_root, manifest, repository_access=repository
        )
        admitted_repository_identity = repository.identity
        if manifest.project_uid is None or projection.projection_digest is None:
            raise GithubArtifactError(
                "manifest_migration_required", "portable identity is required"
            )
        recovery_id = secrets.token_hex(16)
        result: PullResult | None = None
        receipt: DownloadReceipt | None = None
        execution: _InstallExecution | None = None
        try:
            with managed_operation_temp_root(
                "pull", recovery_id
            ) as temporary:
                bundle = temporary.path / "bundle.zip"
                assert_current_manifest_unchanged(
                    repo_root, manifest, repository_access=repository
                )
                receipt = resolve_and_download(
                    config, manifest.project_uid, projection, bundle,
                    credentials, dependencies.transport,
                    before_request=lambda: assert_current_manifest_unchanged(
                        repo_root, manifest, repository_access=repository
                    ),
                )
                assert_current_manifest_unchanged(
                    repo_root, manifest, repository_access=repository
                )
                gh = dependencies.gh_resolver.resolve(
                    before_exec=lambda: assert_current_manifest_unchanged(
                        repo_root, manifest, repository_access=repository
                    )
                )
                assert_current_manifest_unchanged(
                    repo_root, manifest, repository_access=repository
                )
                verified = verify_attestation(
                    bundle, receipt, config, credentials, gh,
                    dependencies.runner,
                    before_exec=lambda: assert_current_manifest_unchanged(
                        repo_root, manifest, repository_access=repository
                    ),
                )
                assert_current_manifest_unchanged(
                    repo_root, manifest, repository_access=repository
                )
                with registered_pull_authorization(
                    bundle, receipt, verified, manifest
                ) as authorization:
                    _pull_handoff_checkpoint()
                    assert_current_manifest_unchanged(
                        repo_root, manifest, repository_access=repository
                    )
                    execution = _install_verified_pull_execution(
                        repo_root, bundle, authorization,
                        expected_repository_identity=admitted_repository_identity,
                        expected_manifest=manifest,
                        recovery_id=recovery_id,
                    )
                    # Retain commit state before any subsequent outer work.
                    _after_pull_install_checkpoint()
                    installed = execution.result
                    try:
                        assert_current_manifest_unchanged(
                            repo_root, manifest, repository_access=repository
                        )
                    except ManifestError:
                        installed = replace(
                            installed, status="promoted_but_stale"
                        )
                    result = PullResult(receipt, installed)
        except OperationTempCleanupError:
            if (
                receipt is not None
                and execution is not None
                and execution.promotion_committed
            ):
                return _postcommit_pull_result(
                    receipt, execution, recovery_id=recovery_id
                )
            raise GithubArtifactError(
                "github_cleanup_failed", "private GitHub cleanup failed",
                recovery_id,
            ) from None
        except Exception:
            if (
                receipt is not None
                and execution is not None
                and execution.promotion_committed
            ):
                return _postcommit_pull_result(receipt, execution)
            raise
        assert result is not None
        assert execution is not None
        if temporary.cleanup_failed:
            if not execution.promotion_committed:
                raise GithubArtifactError(
                    "github_cleanup_failed", "private GitHub cleanup failed",
                    recovery_id,
                )
            result = PullResult(
                result.download,
                replace(
                    result.install,
                    status="promoted_but_stale",
                    recovery_id=recovery_id,
                ),
            )
        return result
```

`_postcommit_pull_result(receipt, execution, recovery_id=None)` preserves only
the inner install result's descriptor-validated identity and changes its status
to `promoted_but_stale`. With no supplied recovery ID it preserves any recovery
ID already produced by the inner installer; with a supplied ID it replaces it
with the outer pull operation's ID. The private post-install checkpoint runs
only after the complete `_InstallExecution` has been retained. Consequently an
ordinary outer exception, alone or combined with pull-root cleanup failure,
cannot erase a committed install or leak private exception text.

The first repository open atomically checks the optional fleet identity before
journal, manifest, projection, transport, `gh`, or credential-consuming work.
It also compares every semantic field of the optional admitted manifest to the
descriptor-owned current manifest before provider resolution or transport;
preserving ID/UID while changing repository, signer, source ref, channel,
compatibility version, privacy, or feature intent still fails closed.
The journal gate is read-only and runs through that access before manifest,
projection, network, or credential use; `_install_verified_pull_execution` reopens the
path only through the lifecycle boundary and requires the exact identity just
captured, then rechecks the same journal gate and full admitted manifest under
lock through `_install_bundle`.
`resolve_and_download` invokes its retained-manifest callback immediately before
every HTTPS open/redirect; the gh resolver invokes it before every capability
child, and attestation invokes it immediately before the verifier child. The
outer access asserts again before authorization registration and the install
handoff. If the final outer assertion detects drift after nested install has
committed, pull preserves only that install result's descriptor-verified
identity, changes its status to `promoted_but_stale`, returns exit semantics 3,
and never converts a committed mutation into an ordinary exception.
`inspect_projection` before the network is advisory for exact asset resolution;
`_install_verified_pull_execution` creates the authoritative private `StagedInput` inside
the lifecycle lock. A root swap after the first open therefore uses only the
original descriptor's trusted provider/projection and then either reacquires
that same identity or fails before install; a replacement manifest can never
redirect authenticated GitHub traffic. `GithubCredentials` and every secret-
bearing fleet/refresh request field use `field(repr=False)`. Never put the token
in argv, repr, output, operation state, exception text, or a Graphify
environment. Record `artifact_pull` content-free state only after the install
outcome is known.

The managed pull root is commit-aware. A coincident ordinary body failure and
cleanup failure before install commit becomes the closed `github_cleanup_failed`
error with the same recovery ID. After install commit, either an ordinary outer
failure or its combination with cleanup failure returns the closed stale
result above. After a normal body exit it branches only on the retained private
`_InstallExecution.promotion_committed` bit, never on status: false is the
closed cleanup error; true preserves the descriptor-verified identity, returns
`promoted_but_stale`, and attaches the recovery ID. Thus final manifest drift
cannot make an exact-generation no-op look committed, and post-promotion stale
validation cannot hide a real commit. A pending non-ordinary `BaseException`
remains unsuppressed.

The registration context's internal `finally` is active before insertion and
closes the registration-to-wrapper handoff gap, including
`BaseException`/cancellation at the injected post-insert checkpoint. The
installer wrapper retains its own idempotent `finally` for failures after
entry. Both discard only the exact authorization object under
`_AUTHORIZED_PULLS_LOCK`; normal atomic consumption makes either later discard
a no-op.

- [ ] **Step 5: Run transport, provenance, install, and state tests together**

Run: `uv run pytest -q tests/test_github_artifacts.py tests/test_bundles_install.py tests/test_operation_state.py`

Expected: PASS, including subject/repository/workflow/source mismatch mutation cases and single-use authorization.

- [ ] **Step 6: Commit verified pull**

```bash
git add src/project_knowledge/github_artifacts.py src/project_knowledge/bundles.py tests/test_github_artifacts.py tests/test_bundles_install.py tests/test_operation_state.py
git commit -m "feat: verify provenance before pulling graph bundles"
```

### Task 7: Workflow-only publication, rolling release, and retention

**Files:**
- Create: `src/project_knowledge/workflow_publish.py`
- Create: `tests/test_workflow_publish.py`
- Modify: `src/project_knowledge/bundles.py`
- Test: `tests/test_bundles_pack.py`

**Interfaces:**
- Consumes: strict bundle parsing, candidate validation, deterministic packing, core `ArtifactIntent`, and the GitHub HTTPS/JSON boundary from Task 5.
- Produces: `WorkflowPublishError(code: str, message: str, recovery_id: str | None = None)`; a non-null constructor-validated recovery ID is allowed only for `workflow_cleanup_failed` and `workflow_output_recovery_required`.
- Produces: `WorkflowContext(repository: str, repository_id: int, ref: str, ref_type: str, ref_protected: bool, sha: str, run_id: int)` containing caller/source identity only; it never represents called-workflow signer identity.
- Produces privately: `WorkflowGitScope(checkout_descriptor, project_descriptor, project_segments, checkout_identity, project_identity, revalidate)` created only from Task 12's retained `WorkflowRepository`, or an equivalent root scope with empty segments for direct root-level library tests. It proves that the selected project descriptor is the exact descendant reached through the retained checkout chain.
- Produces privately: `GitCheckoutSnapshot(object_format: Literal["sha1", "sha256"], commit_oid: str, commit_tree_oid: str, index_tree_oid: str, safe_projection_digest: str)` and `capture_descriptor_git_checkout(scope: WorkflowGitScope, staged: StagedInput, expected_oid: str, runner: DescriptorGitRunner, *, before_exec: Callable[[], None] = _noop) -> GitCheckoutSnapshot`. This is the only publication Git authority and has no public executable/config/argv input; it opens `.git` from the retained checkout descriptor, scopes index/worktree/tree comparisons to the literal selected-root segment tuple, strips that prefix before comparison with `staged`, and invokes both scope revalidation and the callback immediately before every Git child.
- Produces privately: `PublicationDependencies(git_runner: DescriptorGitRunner)` with `REAL_PUBLICATION_DEPENDENCIES`; injection exists only for tests.
- Produces: `prepare_publication(repo_root: Path, build_bundle: Path, output: Path, context: WorkflowContext, *, dependencies: PublicationDependencies = REAL_PUBLICATION_DEPENDENCIES, expected_repository_identity: RepositoryIdentity | None = None, expected_manifest: ProjectManifest | None = None, workflow_git_scope: WorkflowGitScope | None = None) -> PackedBundle`. The three expected/authority values are library-only workflow-boundary seams and have no internal-verb override; identity is checked atomically at lifecycle acquisition, the supplied Git scope must bind that same project identity, and the manifest is compared in full before Git/bundle/output. Absence of a scope creates only a root-level empty-prefix scope from the already retained repository descriptor for direct tests; it never searches upward by pathname.
- Produces: `publish_release_asset(bundle: PackedBundle, context: WorkflowContext, credentials: GithubCredentials, client: GithubMutationClient = REAL_GITHUB_MUTATIONS) -> PublishedAsset`.
- The module is importable only as a library/internal `python -m project_knowledge.workflow_publish`; it is not added to `[project.scripts]` and no `publish` subcommand is added.

- [ ] **Step 1: Write failing privileged-boundary and repack tests**

```python
def test_prepare_publication_revalidates_and_repackages_build_output(repo, build_bundle, workflow_context, tmp_path):
    assert inspect_bundle_manifest(build_bundle).git is None
    prepared = prepare_publication(repo, build_bundle, tmp_path / "release.zip", workflow_context)
    repeated = prepare_publication(
        repo, build_bundle, tmp_path / "release-again.zip", workflow_context
    )
    assert prepared.artifact.git.commit_oid == workflow_context.sha
    assert prepared.artifact.transport.repository_id == workflow_context.repository_id
    assert prepared.path.read_bytes() == repeated.path.read_bytes()

def test_prepare_publication_root_swap_after_lock_uses_only_original_checkout(
    repo, build_bundle, workflow_context, publication_fault, tmp_path,
) -> None:
    original = tmp_path / "original-checkout"
    publication_fault.after_lock(
        lambda: replace_repository_root(
            repo, original, attacker_repository_with_copied_id_uid(tmp_path)
        )
    )
    prepared = prepare_publication(
        repo, build_bundle, tmp_path / "release.zip", workflow_context
    )
    assert prepared.artifact.project_uid == manifest_uid(original)
    assert not (repo / ".project-knowledge").exists()


def test_prepare_publication_root_swap_before_git_check_never_reads_replacement(
    repo, build_bundle, workflow_context, publication_fault, tmp_path,
) -> None:
    original = tmp_path / "original-checkout"
    replacement = attacker_repository_with_copied_id_uid(tmp_path)
    publication_fault.before_git_check(
        lambda: replace_repository_root(repo, original, replacement)
    )
    prepared = prepare_publication(
        repo, build_bundle, tmp_path / "release.zip", workflow_context
    )
    assert prepared.artifact.project_uid == manifest_uid(original)
    assert publication_fault.git_worktree_identity == repository_identity(original)
    assert publication_fault.git_worktree_identity != repository_identity(repo)


def test_workflow_admission_swap_before_prepare_fails_before_git_or_output(
    repo, build_bundle, workflow_context, fake_descriptor_git_runner, tmp_path,
) -> None:
    with open_repository_access(repo) as admitted_repository:
        expected_identity = admitted_repository.identity
        expected_manifest = load_manifest(
            repo / ".graphify-project.yaml", repo,
            repository_access=admitted_repository,
        )
    repo.rename(tmp_path / "original")
    replacement = attacker_repository_with_copied_id_uid(tmp_path, at=repo)
    output = tmp_path / "release.zip"
    with pytest.raises(TransactionLockError) as raised:
        prepare_publication(
            repo, build_bundle, output, workflow_context,
            dependencies=PublicationDependencies(fake_descriptor_git_runner),
            expected_repository_identity=expected_identity,
            expected_manifest=expected_manifest,
        )
    assert raised.value.kind == "authority"
    assert fake_descriptor_git_runner.calls == []
    assert not output.exists()
    assert not (replacement / ".project-knowledge").exists()


@pytest.mark.parametrize("change", ["manifest_rewrite_restore", "safe_source", "coverage_control"])
def test_prepare_publication_drift_before_output_leaves_output_absent(
    repo, build_bundle, workflow_context, publication_fault, tmp_path, change,
) -> None:
    output = tmp_path / "release.zip"
    publication_fault.before_output_commit(
        lambda: apply_publication_precommit_change(repo, change)
    )
    with pytest.raises((ManifestError, WorkflowPublishError)):
        prepare_publication(repo, build_bundle, output, workflow_context)
    assert not output.exists()


def test_descriptor_git_runner_checks_manifest_before_every_child(
    repo, staged, fake_descriptor_git_runner, root_workflow_git_scope,
) -> None:
    checks: list[int] = []
    capture_descriptor_git_checkout(
        root_workflow_git_scope(repo), staged, head_oid(repo),
        fake_descriptor_git_runner,
        before_exec=lambda: checks.append(len(fake_descriptor_git_runner.calls)),
    )
    assert len(checks) == len(fake_descriptor_git_runner.calls)


@pytest.mark.skipif(sys.platform != "linux", reason="real publisher is Linux-only")
def test_real_descriptor_git_runner_uses_traversable_proc_fds(
    repo, staged, root_workflow_git_scope,
) -> None:
    snapshot = capture_descriptor_git_checkout(
        root_workflow_git_scope(repo), staged, head_oid(repo),
        SYSTEM_DESCRIPTOR_GIT_RUNNER,
    )
    assert snapshot.commit_oid == head_oid(repo)


def test_non_linux_real_publication_fails_closed_before_git_or_output(
    repo, build_bundle, workflow_context, tmp_path,
) -> None:
    if sys.platform == "linux":
        pytest.skip("unsupported-platform branch")
    output = tmp_path / "release.zip"
    with pytest.raises(WorkflowPublishError) as raised:
        prepare_publication(repo, build_bundle, output, workflow_context)
    assert raised.value.code == "workflow_platform_unsupported"
    assert not output.exists()


def test_prepare_publication_rejects_mismatching_non_null_build_git(
    repo, build_bundle, workflow_context, tmp_path
):
    forged = rewrite_bundle_manifest(
        build_bundle,
        git={"algorithm": "sha1", "commit_oid": "a" * 40},
    )
    with pytest.raises(WorkflowPublishError, match="workflow_source_mismatch"):
        prepare_publication(
            repo, forged, tmp_path / "release.zip", workflow_context
        )
    assert not (tmp_path / "release.zip").exists()


@pytest.mark.parametrize("change", [
    "unprotected_ref", "tag_ref", "wrong_source_ref", "wrong_repository_id",
    "head_mismatch", "dirty_tracked_file", "untracked_safe_file", "signer_path_invalid",
    "build_bundle_project_mismatch", "build_bundle_projection_mismatch",
])
def test_prepare_publication_fails_closed_before_github_write(
    repo, build_bundle, workflow_context, tmp_path, mutation_client, change
):
    changed_repo, changed_bundle, changed_context = apply_publication_change(
        repo, build_bundle, workflow_context, change
    )
    with pytest.raises(WorkflowPublishError):
        prepare_publication(
            changed_repo, changed_bundle, tmp_path / "release.zip", changed_context
        )
    assert mutation_client.calls == []


@pytest.mark.parametrize("journal_state", ["recoverable", "corrupt"])
def test_prepare_publication_gates_recovery_before_git_parse_or_output(
    repo, build_bundle, workflow_context, publication_spies, tmp_path,
    journal_state,
) -> None:
    seed_init_journal(repo, journal_state)
    before = tree_snapshot(repo)
    output = tmp_path / "release.zip"
    with pytest.raises(WorkflowPublishError) as raised:
        prepare_publication(repo, build_bundle, output, workflow_context)
    assert raised.value.code == "init_recovery_required"
    assert publication_spies.git_calls == []
    assert publication_spies.bundle_parse_calls == []
    assert publication_spies.github_calls == []
    assert not output.exists()
    assert tree_snapshot(repo) == before


def test_prepare_publication_cleanup_after_output_preserves_zip_and_blocks_upload(
    repo, build_bundle, workflow_context, operation_temp_fault,
    mutation_client, tmp_path,
) -> None:
    output = tmp_path / "release.zip"
    operation_temp_fault.fail_cleanup("publish")
    with pytest.raises(WorkflowPublishError) as raised:
        prepare_verify_and_publish(
            repo, build_bundle, output, workflow_context, mutation_client
        )
    assert raised.value.code == "workflow_output_recovery_required"
    assert raised.value.recovery_id is not None
    assert inspect_bundle_manifest(output).git.commit_oid == workflow_context.sha
    assert mutation_client.calls == []


def test_prepare_publication_supports_nested_project_and_ignores_sibling_delta(
    monorepo_checkout, nested_build_bundle, workflow_context,
    trusted_tool_checkout, tmp_path,
) -> None:
    # This tracked sibling is outside the selected project prefix and must not
    # enter either its cleanliness decision or safe commit projection.
    (monorepo_checkout / "services/sibling/app.py").write_text(
        "locally changed sibling\n", encoding="utf-8"
    )
    with open_workflow_repository(
        monorepo_checkout, "services/api",
        forbidden_checkout=trusted_tool_checkout,
    ) as workflow_repository:
        prepared = prepare_publication(
            monorepo_checkout / "services/api", nested_build_bundle,
            tmp_path / "nested-release.zip", workflow_context,
            expected_repository_identity=workflow_repository.identity,
            expected_manifest=workflow_repository.manifest,
            workflow_git_scope=workflow_repository.git_scope,
        )
    assert prepared.artifact.git.commit_oid == workflow_context.sha


def test_nested_publication_rejects_uncommitted_safe_file_inside_prefix(
    monorepo_checkout, nested_build_bundle, workflow_context,
    trusted_tool_checkout, tmp_path,
) -> None:
    (monorepo_checkout / "services/api/src/untracked.py").write_text(
        "print('new')\n", encoding="utf-8"
    )
    with open_workflow_repository(
        monorepo_checkout, "services/api",
        forbidden_checkout=trusted_tool_checkout,
    ) as workflow_repository:
        with pytest.raises(WorkflowPublishError, match="workflow_source_mismatch"):
            prepare_publication(
                monorepo_checkout / "services/api", nested_build_bundle,
                tmp_path / "nested-release.zip", workflow_context,
                expected_repository_identity=workflow_repository.identity,
                expected_manifest=workflow_repository.manifest,
                workflow_git_scope=workflow_repository.git_scope,
            )


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_prepare_publication_pack_helper_postlink_failure_preserves_zip(
    repo, build_bundle, workflow_context, pack_fault, operation_temp_fault,
    mutation_client, tmp_path, cleanup_fails,
) -> None:
    output = tmp_path / "release.zip"
    pack_fault.after_durable_output_link(
        lambda: (_ for _ in ()).throw(RuntimeError("private"))
    )
    if cleanup_fails:
        operation_temp_fault.fail_cleanup("publish")
    with pytest.raises(WorkflowPublishError) as raised:
        prepare_verify_and_publish(
            repo, build_bundle, output, workflow_context, mutation_client
        )
    assert raised.value.code == "workflow_output_recovery_required"
    assert raised.value.recovery_id is not None
    assert "private" not in repr(raised.value)
    assert inspect_bundle_manifest(output).git.commit_oid == workflow_context.sha
    assert mutation_client.calls == []
```

The tracked-clean check must compare Git index/HEAD plus the safe projection: ignored private state and `graphify-out` do not make the checkout dirty, while every safe corpus byte must be represented by `context.sha`. The Git checks in both root-swap tests must either complete wholly against the original opened repository inode or fail with constant `workflow_git_unavailable`; they may never inspect the replacement.

- [ ] **Step 2: Write upload-before-tag-move and verify-before-delete tests**

```python
def test_publish_orders_new_upload_verification_tag_move_then_retention(bundle, context, fake_mutations):
    fake_mutations.seed_release(old_assets=21, old_tag_commit="a" * 40)
    result = publish_release_asset(
        bundle, context, GithubCredentials("token"), fake_mutations
    )
    assert [call.name for call in fake_mutations.calls] == [
        "get_repository", "get_or_create_release", "upload_asset",
        "get_asset", "move_tag", "update_release", "list_assets",
        "delete_asset", "delete_asset",
    ]
    assert result.sha256 == bundle.sha256
    assert len(fake_mutations.remaining_digest_assets()) == 20


def test_first_publish_creates_release_tag_before_upload_but_never_deletes_before_verify(
    bundle, context, fake_mutations
):
    fake_mutations.seed_no_release()
    publish_release_asset(
        bundle, context, GithubCredentials("token"), fake_mutations
    )
    assert fake_mutations.call_names[:4] == [
        "get_repository", "create_release_and_tag", "upload_asset", "get_asset"
    ]
    assert fake_mutations.call_names.index("get_asset") < fake_mutations.call_names.index("list_assets")
    assert "delete_asset" not in fake_mutations.call_names


def test_remote_digest_mismatch_never_moves_tag_or_deletes_old_assets(bundle, context, fake_mutations):
    fake_mutations.return_wrong_uploaded_digest()
    with pytest.raises(WorkflowPublishError, match="remote_asset_mismatch"):
        publish_release_asset(
            bundle, context, GithubCredentials("token"), fake_mutations
        )
    assert "move_tag" not in fake_mutations.call_names
    assert "delete_asset" not in fake_mutations.call_names


def test_publication_retry_accepts_repeated_identical_attestations_and_reuses_asset(
    bundle, context, fake_mutations, fake_attestation_runner,
) -> None:
    fake_attestation_runner.return_two_identical_valid_results(bundle, context)
    first = verify_then_publish(bundle, context, fake_attestation_runner, fake_mutations)
    second = verify_then_publish(bundle, context, fake_attestation_runner, fake_mutations)
    assert second.asset_id == first.asset_id
    assert fake_mutations.upload_count == 1
    assert "delete_asset" not in fake_mutations.call_names
```

Add idempotency cases: an exact-name asset with matching size/digest is reused; an exact name with mismatched bytes fails; a retry after upload but before tag move completes safely; retention sorts digest-addressed assets by `(created_at, id)` newest-first and never deletes unrelated assets. Also repack one generation with different authenticated Git ownership and one with a different injected AtlasWeaver package version: both retain `generation_digest` but produce different ZIP SHA-256 values and therefore different valid asset names rather than a collision.

- [ ] **Step 3: Run and observe the missing workflow module**

Run: `uv run pytest -q tests/test_workflow_publish.py`

Expected: collection fails for missing `project_knowledge.workflow_publish`.

- [ ] **Step 4: Implement internal publication preparation**

The real publication runner is intentionally Linux-only because the privileged
reusable workflow is fixed to a GitHub-hosted Ubuntu job. At module entry it
requires `sys.platform == "linux"` and capability-checks traversal through
`/proc/self/fd/<retained-directory-fd>` before any Git command or output. A
missing/non-traversable procfs boundary is stable
`workflow_platform_unsupported`. The public package remains cross-platform:
no public command invokes this workflow-only module. Unit tests on every
platform inject a closed fake `DescriptorGitRunner`; the real-runner
integration is Linux-marked, and non-Linux tests require fail-closed behavior.
No environment/CLI/workflow input can select a runner.

`capture_descriptor_git_checkout` descriptor-opens `.git` from the retained
consumer-checkout descriptor in `WorkflowGitScope`, never from the selected
nested project directory. A real directory is opened no-follow; a worktree marker is
bounded to one strict `gitdir:` line and every target component is opened
no-follow into retained gitdir/common-dir descriptors. It resolves and binds
the system Git executable privately, then a single-purpose Linux child runner
duplicates those descriptors, performs `fchdir()` to the retained worktree,
and invokes only exact read-only plumbing with the worktree/gitdir exposed as
inherited `/proc/self/fd/<n>` descriptor paths. It never runs `git -C <repo_root>`,
uses a pathname `cwd`, rediscovers `.git` from the mutable root, or invokes a
shell, hook, filter, fsmonitor, pager, editor, credential helper, network
transport, submodule, or LFS process.

The runner uses a fixed minimal environment including
`GIT_LITERAL_PATHSPECS=1`, disables system/global config,
fsmonitor, untracked-cache, optional locks, prompts, replacements, and
submodule recursion, validates the executable binding before every exec, and
caps/redacts both streams. Exact commands establish the object format,
`HEAD^{commit}`, `HEAD^{tree}`, project-prefix index state, and project-prefix
tracked worktree cleanliness. The resolved HEAD commit must equal `context.sha`.
For an empty prefix the index tree must equal the resolved
`context.sha^{tree}` OID; for a nested prefix, fixed plumbing compares only the
literal selected subtree in index/worktree against that commit, so a tracked
sibling delta outside the prefix is irrelevant while any selected-root delta
fails. A tree OID is never compared to a commit OID. The helper also enumerates
only the selected prefix of the commit tree through bounded
`ls-tree`/`cat-file` plumbing, strips the exact prefix, applies the identical
privacy projection, and requires its relative path-to-byte-digest map and
projection digest to equal `staged`; therefore an untracked safe file,
deleted/modified tracked safe file, or committed safe-byte mismatch inside the
project fails even when Git ignore rules would hide it. Ignored private state,
`graphify-out`, and checkout siblings are outside that comparison. All Git output is parsed from closed schemas and
bounded before allocation. A path swap at any checkpoint can only leave the
retained descriptors on the original checkout or produce the stable path-free
`workflow_git_unavailable` error.

`prepare_publication()` must parse caller/source `WorkflowContext` only from a strict allowlist of GitHub environment names, require all fields, validate full SHA/repository ID/ref types, validate the manifest signer path grammar, check the exact Git object format/OID and tracked safe projection, and reject any consumer-supplied shell/config override. It must not compare signer digest to normal `github.workflow_sha`; called-workflow identity becomes authoritative only through the post-attestation verification step. Its lifecycle-locked core is exact:

```python
with repository_lifecycle_lock(
    repo_root,
    expected_repository_identity=expected_repository_identity,
), capture_lifecycle_repository(
    repo_root
) as repository:
    if inspect_init_journal(
        repo_root, repository_access=repository
    ) != "none":
        raise WorkflowPublishError(
            "init_recovery_required", "configuration recovery is required"
        )
    if expected_manifest is None:
        manifest = load_manifest(
            repo_root / ".graphify-project.yaml", repo_root,
            repository_access=repository,
        )
        manifest = require_current_manifest(
            repo_root, manifest, repository_access=repository
        )
    else:
        manifest = require_current_manifest(
            repo_root, expected_manifest, repository_access=repository
        )
    recovery_id = secrets.token_hex(16)
    prepared: PackedBundle | None = None
    commit_state = _PackCommitState()
    try:
      with managed_operation_temp_root(
          "publish", recovery_id
      ) as temporary, ExitStack() as parsed_scope:
        private = temporary.path
        staged = stage_input(
            repo_root, manifest, private / "source",
            repository_access=repository,
        )
        git_scope = (
            workflow_git_scope
            if workflow_git_scope is not None
            else capture_root_workflow_git_scope(repository)
        )
        git_scope.require_project_identity(repository.identity)
        def publication_git_checkpoint() -> None:
            git_scope.revalidate()
            assert_current_manifest_unchanged(
                repo_root, manifest, repository_access=repository
            )
        git = capture_descriptor_git_checkout(
            git_scope, staged, context.sha, dependencies.git_runner,
            before_exec=publication_git_checkpoint,
        )
        parsed = parsed_scope.enter_context(
            parse_bundle(build_bundle, private / "candidate")
        )
        _require_publication_identity(
            parsed.artifact, manifest, staged, git, context
        )
        evidence_path = PurePosixPath("graphify-out/GRAPH_EVIDENCE.json")
        evidence_binding = next(
            item for item in parsed.payloads if item.path == evidence_path
        )
        parse_graph_evidence(
            parsed.read_payload(
                evidence_path,
                GRAPH_EVIDENCE_MAX_BYTES,
            ),
            resolve_graphify_compatibility(manifest.graphify_version),
            expected_digest=evidence_binding.sha256,
        )
        validated = validate_candidate(
            parsed.root,
            staged,
            manifest,
            expected_projection_digest=staged.projection_digest,
            expected_evidence_digest=evidence_binding.sha256,
            build_epoch=parsed.artifact.build_epoch,
            git_identity=git_identity_for_oid(context.sha),
        )
        immediately_before = stage_input(
            repo_root, manifest, private / "source-check",
            repository_access=repository,
        )
        _require_same_validation_projection(staged, immediately_before)
        captured = _capture_validated_generation(
            validated, manifest, private / "captured-generation"
        )
        after = stage_input(
            repo_root, manifest, private / "source-after",
            repository_access=repository,
        )
        _require_same_validation_projection(staged, after)
        assert_current_manifest_unchanged(
            repo_root, manifest, repository_access=repository
        )
        def publication_precommit_check() -> None:
            assert_current_manifest_unchanged(
                repo_root, manifest, repository_access=repository
            )
            precommit_source = stage_input(
                repo_root, manifest, private / "source-precommit",
                repository_access=repository,
            )
            _require_same_validation_projection(staged, precommit_source)
            assert_current_manifest_unchanged(
                repo_root, manifest, repository_access=repository
            )
        prepared = _pack_captured_generation(
            captured, output,
            commit_state=commit_state,
            precommit_check=publication_precommit_check,
        )
        _after_publication_output_commit_checkpoint()
    except OperationTempCleanupError:
        if commit_state.output_committed:
            raise WorkflowPublishError(
                "workflow_output_recovery_required",
                "publication output recovery is required", recovery_id,
            ) from None
        raise WorkflowPublishError(
            "workflow_cleanup_failed", "private publication cleanup failed",
            recovery_id,
        ) from None
    except Exception:
        if commit_state.output_committed:
            raise WorkflowPublishError(
                "workflow_output_recovery_required",
                "publication output recovery is required", recovery_id,
            ) from None
        raise
    if temporary.cleanup_failed:
        # A normal body exit implies _pack_captured_generation completed its
        # no-replace link and fsync contract; preserve that committed output.
        raise WorkflowPublishError(
            "workflow_output_recovery_required",
            "publication output recovery is required", recovery_id,
        )
    assert prepared is not None
    return prepared
```

Require the descriptor-rooted init journal to be `none` before manifest or
bundle parsing; recoverable/corrupt state is stable `init_recovery_required`
and produces no output or GitHub action. Require schema v2/non-null projection
and exact equality between the parsed manifest, revalidated candidate,
recaptured generation, and final prepared bundle generation digest. Every
checkout-side journal/manifest/projection/stage read uses the one lease-captured
repository descriptor; a pathname replacement after lock acquisition cannot
enter privileged output. The packer's final precommit callback reasserts the
pinned manifest and re-stages/recompares the complete source/projection/control
contract after ZIP bytes are finalized but immediately before exclusive output
publication; failure leaves output absent. Derive the asset-name final component only from
`PackedBundle.sha256` after the final privileged ZIP bytes exist, and require
that name component, local archive hash, uploaded GitHub digest, and attested
subject digest to agree. The unprivileged build bundle may carry `git: null`,
because normal Core refresh creates schema-2 ownership with
`git_identity=None`; if it carries a non-null Git identity, require it to equal
the authenticated workflow context SHA/object format or fail
`workflow_source_mismatch`. Authority comes only from the privileged checkout/
context: pass `git_identity_for_oid(context.sha)` to `validate_candidate`,
require the returned `ValidatedGraph.git_identity` to equal it, and require the
final prepared bundle's non-null Git identity to equal it before attestation.
This validates/re-owns a private candidate and packs directly from its
descriptor-captured ownership generation; it does not require or promote a
live `graphify-out` in the privileged checkout. Task 6 still requires the
attestation source digest, download receipt source commit, final bundle Git
identity, generation digest, archive digest, and manifest source ref to agree
in their corresponding domains. It must never trust the build job's health
JSON, booleans, path, backend output, archive digest, or consumer
`pyproject.toml`.

When the workflow boundary supplies expectations, lifecycle acquisition first
checks its retained root identity atomically, then
`require_current_manifest(..., expected_manifest)` compares the entire admitted
semantic contract before Git, bundle parsing, staging, or output. The internal
prepare driver always supplies both values from `WorkflowRepository`; neither
is accepted from workflow input or a public command. A swap between workflow
root admission and this lock therefore fails before privileged preparation
rather than adopting a copied-ID/UID replacement.

Publication never returns from inside a temporary-root context. It passes a
fresh wrapper-owned `_PackCommitState` into `_pack_captured_generation`, so the
durable output commit remains visible even when that helper never returns.
Cleanup failure before an output commit is mapped to closed
`workflow_cleanup_failed`; cleanup failure after the helper's durable link
preserves the verified ZIP, returns `workflow_output_recovery_required` with
only the opaque recovery ID, records no publication success, and prevents
attestation/upload. A pending
non-ordinary `BaseException` remains authoritative. The internal verb driver
does not continue from any `WorkflowPublishError` to `verify-attestation` or
`upload`.

The internal module accepts only fixed `prepare`, `verify-attestation`, and `upload` verbs when invoked with `python -m`; each requires `GITHUB_ACTIONS=true`, a complete `WorkflowContext`, and exact file paths created by the workflow under `$RUNNER_TEMP`. `verify-attestation` reads signer workflow/digest only from the strict project manifest, constructs Task 6's `AttestationPolicy` from that manifest plus the authenticated caller repository/ref/SHA context, and calls `verify_attestation_policy` after the attestation action. It does not fabricate or accept a `DownloadReceipt`, release ID, or asset ID and cannot create `_PullAuthorization`; those are pull-only authorities that do not exist before upload. It has no signer override flag. This is defense in depth for the just-created subject, not a release-asset authorization claim; the absence of a public entry point/subcommand is the product boundary.

- [ ] **Step 5: Implement GitHub mutation sequencing**

Use authenticated `api.github.com` REST calls with the Task 5 no-ambient client. Bind numeric repository ID on every response. For an existing rolling release, upload first, verify the new remote asset, then move the tag/update the release and enforce retention. For the first publication only, GitHub must create the release/tag before it exposes an upload endpoint; create it at `context.sha`, upload immediately, and perform no retention deletion until that asset verifies. Upload with exact `Content-Type: application/zip`, `Content-Length`, and exact asset name. After upload, GET the asset and require positive immutable asset ID, exact name/size, and `digest == sha256:<local>`. Then list all assets, keep the 20 newest matching the exact digest-addressed naming grammar for this UID/channel, and delete older matching assets by numeric ID. Never delete unrelated, malformed, current, or not-yet-verified assets.

- [ ] **Step 6: Run all publication and bundle tests**

Run: `uv run pytest -q tests/test_workflow_publish.py tests/test_bundles_pack.py tests/test_bundles_parse.py`

Expected: PASS with exact call-order assertions for fresh publish, idempotent retry, interrupted retry, and retention.

- [ ] **Step 7: Commit the workflow-only publisher**

```bash
git add src/project_knowledge/workflow_publish.py src/project_knowledge/bundles.py tests/test_workflow_publish.py tests/test_bundles_pack.py
git commit -m "feat: publish attested graph release assets safely"
```

### Task 8: Packaged agent resources and ownership-safe installer

**Files:**
- Create: `src/project_knowledge/agent_install.py`
- Create: `src/project_knowledge/resources/skills/using-project-knowledge-graphs/SKILL.md`
- Create: `src/project_knowledge/resources/skills/using-project-knowledge-graphs/agents/openai.yaml`
- Create: `src/project_knowledge/resources/skills/using-project-knowledge-graphs/references/workflow.md`
- Create: `tests/test_agent_install.py`
- Modify: `pyproject.toml`
- Modify: `scripts/install-project-knowledge-skill`
- Test: `tests/test_installer.py`
- Test: `tests/test_skill_contract.py`
- Test: `tests/test_public_release.py`

**Interfaces:**
- Consumes: `resolve_graphify_compatibility()` and compatibility-owned `AgentInstallContract`/`render_graphify_agent_install()`; `agent_install.py` owns no Graphify platform argv or target table.
- Produces: `AgentPlatform = Literal["codex", "agents"]`.
- Produces: `AgentInstallRequest(platform: AgentPlatform, user_home: Path)`; public `--home` always means the user's home, never `.codex`/`.agents` itself.
- Produces: `AgentInstallDependencies(runner: GraphifyCommandRunner, resolve_graphify: Callable[[], ResolvedGraphifyExecutable], resource_root: Traversable)` for tests/internal compatibility wrappers only. The production callable is exactly Core's `resolve_graphify_executable`; it is an injection seam, not another resolver implementation or public override.
- Produces: `AgentInstallResult(platform: AgentPlatform, status: Literal["installed", "already_current", "uninstalled"], resource_digest: str)`.
- Produces: `install_agent(request: AgentInstallRequest, *, dependencies: AgentInstallDependencies = REAL_AGENT_DEPENDENCIES) -> AgentInstallResult` and `uninstall_agent(platform: AgentPlatform, user_home: Path) -> AgentInstallResult`.

- [ ] **Step 1: Write failing wheel-resource and source-mirror tests**

```python
def test_packaged_skill_exactly_matches_reviewed_source_tree():
    packaged = resources.files("project_knowledge").joinpath(
        "resources/skills/using-project-knowledge-graphs"
    )
    assert resource_tree_digest(packaged) == filesystem_tree_digest(
        ROOT / "skills/using-project-knowledge-graphs"
    )


def test_built_wheel_contains_managed_skill_and_reference(tmp_path):
    wheel = build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert {
        "project_knowledge/resources/skills/using-project-knowledge-graphs/SKILL.md",
        "project_knowledge/resources/skills/using-project-knowledge-graphs/agents/openai.yaml",
        "project_knowledge/resources/skills/using-project-knowledge-graphs/references/workflow.md",
    } <= names
```

Copy the reviewed top-level files byte-for-byte into package resources in this task. Do not create a second divergent wording variant; `test_skill_contract.py` must run its semantic assertions against both trees.

- [ ] **Step 2: Write failing delegation, ownership, rollback, and uninstall tests**

```python
@pytest.mark.parametrize(("platform", "relative"), [
    ("codex", "skills/using-project-knowledge-graphs"),
    ("agents", "skills/using-project-knowledge-graphs"),
])
def test_install_delegates_graphify_then_atomically_installs_owned_skill(tmp_path, fake_runner, platform, relative):
    user_home = tmp_path / "user"
    platform_root = user_home / (".codex" if platform == "codex" else ".agents")
    contract = resolve_graphify_compatibility("0.9.48")
    resolved = resolved_graphify_executable(tmp_path)
    rendered = render_graphify_agent_install(
        contract, binary=resolved.path, platform=platform
    )
    result = install_agent(
        AgentInstallRequest(platform, user_home),
        dependencies=agent_dependencies(fake_runner, resolved),
    )
    assert fake_runner.operations[-1] == "agent-install"
    assert fake_runner.calls[-1].argv == rendered.argv
    assert fake_runner.calls[-1].environment == minimal_agent_env(user_home)
    assert all(call.argv[0] == str(resolved.path) for call in fake_runner.calls)
    assert all(
        call.environment.get("HOME") != str(user_home)
        for call in fake_runner.calls[:-1]
    )
    assert rendered.canonical_argv == (
        "<graphify>", "install", "--platform", platform
    )
    destination = platform_root / relative
    marker = json.loads((destination / ".atlasweaver-managed.json").read_text())
    assert marker == {
        "manager": "atlasweaver-agent-installer",
        "platform": platform,
        "resource_digest": result.resource_digest,
        "schema_version": 1,
    }


def test_install_refuses_unmanaged_or_modified_destination_without_running_graphify(
    tmp_path, fake_runner, destination_state
):
    request = AgentInstallRequest("codex", tmp_path / "user")
    seed_unmanaged_or_modified_destination(
        request.user_home / ".codex", destination_state
    )
    with pytest.raises(AgentInstallError, match="agent_destination_unmanaged|agent_destination_modified"):
        install_agent(
            request, dependencies=agent_dependencies(fake_runner, resolved_graphify_executable(tmp_path))
        )
    assert fake_runner.calls == []


def test_uninstall_removes_only_exact_owned_tree_and_never_uninstalls_graphify(
    tmp_path, fake_runner
):
    user_home = tmp_path / "user"
    home = user_home / ".codex"
    request = AgentInstallRequest("codex", user_home)
    install_agent(
        request, dependencies=agent_dependencies(fake_runner, resolved_graphify_executable(tmp_path))
    )
    managed_destination = home / "skills/using-project-knowledge-graphs"
    sibling = home / "skills/human/SKILL.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("human\n", encoding="utf-8")
    result = uninstall_agent("codex", user_home)
    assert result.status == "uninstalled"
    assert not managed_destination.exists()
    assert sibling.read_text(encoding="utf-8") == "human\n"
    assert fake_runner.calls_after_install == []
```

Also cover symlink home/skills/destination/entry rejection, non-regular package resources, partial-copy cleanup, fsync/replace failure rollback, idempotent reinstall, marker duplicate/unknown keys, marker digest tampering, unsupported platform, Graphify version/capability mismatch, and Graphify install failure leaving the Atlas destination unchanged.

- [ ] **Step 3: Run and verify the resource/installer APIs are absent**

Run: `uv run pytest -q tests/test_agent_install.py tests/test_skill_contract.py`

Expected: collection fails for `project_knowledge.agent_install`, and the packaged-resource path is absent.

- [ ] **Step 4: Implement platform adapters and atomic managed-tree replacement**

```python
SKILL_RELATIVE = PurePosixPath("skills/using-project-knowledge-graphs")
MANAGED_MARKER = ".atlasweaver-managed.json"
```

Resolve an explicit `--home` as the real user-home directory or create its absent final directory with mode 0700; the CLI default is `Path.home()`. Resolve the compatibility contract and select its one `AgentInstallContract` for the requested platform. The contract's `home_relative_skill` must be `.codex/skills/graphify/SKILL.md` for `codex` or `.agents/skills/graphify/SKILL.md` for `agents`; derive AtlasWeaver's separate managed destination as the corresponding platform root plus `skills/using-project-knowledge-graphs`. Before calling Graphify, validate any existing Atlas destination and require a valid marker plus exact current tree digest. Call the injected production alias of Core's `resolve_graphify_executable()` once, then run compatibility-owned `probe_graphify(resolved, contract, runner)`; the probe's version/help/seven-operation smoke uses its own private temporary `HOME` and cannot touch the requested user home. No artifact-layer Graphify resolver, identity type, or PATH lookup exists. Render only `render_graphify_agent_install(contract, binary=resolved.path, platform=request.platform).argv` and invoke it through Core's `run_graphify_operation(runner, resolved, rendered.argv, ...)`, which no-follow revalidates the same device/inode/launcher digest immediately before spawn. The final install receives exactly `HOME=<user-home>`, `LANG=C.UTF-8`, `LC_ALL=C.UTF-8`, and `PATH=os.defpath`; this supports a pinned `/usr/bin/env python` launcher without inheriting ambient PATH. No public argv can replace the executable and no local argv template, passthrough, `--project`, or `--strict` flag exists. Then copy package resources through `importlib.resources.as_file()` into a sibling mode-0700 stage, reject symlinks/special files, write the canonical mode-0600 marker, fsync, back up an existing owned destination, replace, fsync parent, and delete backup. Restore the backup on every caught replace failure.

`uninstall_agent()` validates the marker and current digest, renames the exact owned directory to a private tombstone, fsyncs, then deletes it; it refuses modified/unmanaged trees and does not run any Graphify uninstall command. The reviewed package skill is the managed instruction fragment; repository hooks and project `AGENTS.md` edits remain outside this command.

Configure Hatch to include `src/project_knowledge/resources/**` in wheels/sdists. Keep the legacy script's current `--codex-home <platform-root>` meaning for one deprecation window by calling the shared atomic resource installer with an internal explicit destination override; it still runs Graphify with `HOME=<platform-root.parent>` and requires that Graphify's resulting Codex root equals the supplied path. The new public CLI never exposes that override.

- [ ] **Step 5: Run installer, package, and skill contracts**

Run: `uv run pytest -q tests/test_agent_install.py tests/test_installer.py tests/test_skill_contract.py tests/test_public_release.py`

Expected: PASS. Then run `uv build` and `python -m zipfile -l dist/*.whl`; expected resource paths are present once and no `.project-knowledge`, `graphify-out`, query memory, or Obsidian content is present.

- [ ] **Step 6: Commit packaged agent integration**

```bash
git add src/project_knowledge/agent_install.py src/project_knowledge/resources pyproject.toml scripts/install-project-knowledge-skill tests/test_agent_install.py tests/test_installer.py tests/test_skill_contract.py tests/test_public_release.py
git commit -m "feat: package and install AtlasWeaver agent guidance"
```

### Task 9: Strict universal fleet schema and repository identity validation

**Files:**
- Create: `src/project_knowledge/fleet.py`
- Create: `tests/test_fleet.py`
- Test: `tests/test_manifest.py`

**Interfaces:**
- Consumes: strict project `load_manifest_payload(payload, repo_root)`, read-only descriptor-rooted `inspect_init_journal(..., repository_access=repository)`, and v2 `ProjectManifest.project_uid`.
- Produces: `FleetProject(id: str, repository: PurePosixPath, root: Path, repository_identity: RepositoryIdentity, project_uid: UUID)`; identity is library-only and never serialized.
- Produces: `FleetWorkspace(schema_version: int, root: Path, projects: tuple[FleetProject, ...], max_parallel: int)`.
- Produces privately: `FleetAliasKey(kind: Literal["git", "repository"], identity: RepositoryIdentity)` and `_git_alias_key(repository: RepositoryAccess) -> FleetAliasKey`; the key is inode authority, never a canonical pathname.
- Produces: `load_fleet_workspace(path: Path) -> FleetWorkspace` and `select_fleet_projects(workspace: FleetWorkspace, project_ids: tuple[str, ...]) -> tuple[FleetProject, ...]`.

- [ ] **Step 1: Write the valid minimal/default/explicit schema tests**

```python
def test_fleet_workspace_is_project_agnostic_and_preserves_order(tmp_path):
    workspace_path = write_workspace(tmp_path, """schema_version: 1
projects:
  - id: api
    repository: repos/api
  - id: documentation
    repository: repos/docs
defaults:
  max_parallel: 2
""")
    workspace = load_fleet_workspace(workspace_path)
    assert workspace.max_parallel == 2
    assert [project.id for project in workspace.projects] == ["api", "documentation"]
    assert all(project.root.is_relative_to(tmp_path.resolve()) for project in workspace.projects)
    assert all(
        project.repository_identity == opened_identity(project.root)
        for project in workspace.projects
    )


def test_max_parallel_defaults_to_two(tmp_path):
    workspace = load_fleet_workspace(write_workspace_without_defaults(tmp_path))
    assert workspace.max_parallel == 2
```

Use correct persisted RFC 9562 UUIDv4 fixtures (`4ed9af24-5aa2-4eac-8d0a-3f622cc74948`, `abf38b85-953d-4548-911d-8c1f90b12fc5`) in the project manifests.

- [ ] **Step 2: Write the complete closed-schema/path/alias rejection matrix**

```python
@pytest.mark.parametrize("case", [
    "duplicate_yaml_key", "yaml_alias", "yaml_merge", "second_document",
    "unknown_top_key", "unknown_project_key", "unknown_defaults_key",
    "boolean_schema", "boolean_parallel", "parallel_zero", "parallel_nine",
    "empty_projects", "duplicate_id", "duplicate_uid", "duplicate_lexical_path",
    "duplicate_canonical_path", "absolute_path", "parent_escape", "symlink_repo",
    "non_directory_repo", "nested_repo", "worktree_alias", "manifest_missing",
    "manifest_id_mismatch", "manifest_v1_uid_missing", "workspace_symlink",
])
def test_fleet_workspace_rejects_invalid_or_aliased_projects(tmp_path, case):
    path = invalid_workspace_fixture(tmp_path, case)
    with pytest.raises(FleetConfigError) as caught:
        load_fleet_workspace(path)
    assert caught.value.code.startswith("fleet_")
    assert str(tmp_path) not in caught.value.public_message


def test_fleet_manifest_path_swap_cannot_change_validated_project(tmp_path, manifest_race):
    workspace_path = valid_workspace_fixture(tmp_path)
    manifest_race.replace_path_after_descriptor_open(
        tmp_path / "repos/api/.graphify-project.yaml",
        manifest_with(project_id="attacker"),
    )
    workspace = load_fleet_workspace(workspace_path)
    assert workspace.projects[0].id == "api"

def test_fleet_identity_comes_from_same_descriptor_as_manifest_capture(
    tmp_path, repository_capture_spy,
) -> None:
    workspace = load_fleet_workspace(valid_workspace_fixture(tmp_path))
    assert [project.repository_identity for project in workspace.projects] == [
        repository_capture_spy.manifest_descriptor_identity(project.id)
        for project in workspace.projects
    ]


def test_fleet_git_alias_capture_never_follows_post_open_path_swap(
    tmp_path, fleet_fault,
) -> None:
    workspace_path, original_git_identity, replacement_git_identity = (
        worktree_workspace_with_distinct_git_targets(tmp_path)
    )
    fleet_fault.after_git_alias_open_before_finalize(
        replace_dot_git_with_replacement
    )
    try:
        workspace = load_fleet_workspace(workspace_path)
    except FleetConfigError as error:
        assert error.code == "fleet_git_changed"
    else:
        assert fleet_fault.captured_alias_identity(workspace.projects[0]) == (
            original_git_identity
        )
        assert fleet_fault.captured_alias_identity(workspace.projects[0]) != (
            replacement_git_identity
        )


def test_real_linked_worktrees_resolve_dotdot_commondir_and_alias(
    tmp_path,
) -> None:
    main, feature = create_real_git_linked_worktrees(tmp_path)
    assert (feature / ".git").read_text(encoding="utf-8").startswith("gitdir: ")
    with pytest.raises(FleetConfigError) as raised:
        load_fleet_workspace(workspace_for_roots(tmp_path, (main, feature)))
    assert raised.value.code == "fleet_worktree_alias"


def test_fleet_rejects_workspace_or_manifest_over_256_kib(tmp_path):
    with pytest.raises(FleetConfigError, match="fleet_document_too_large"):
        load_fleet_workspace(write_oversized_workspace(tmp_path, 262_145))
    with pytest.raises(FleetConfigError, match="fleet_manifest_too_large"):
        load_fleet_workspace(workspace_with_oversized_manifest(tmp_path, 262_145))

@pytest.mark.parametrize("journal_state", ["recoverable", "corrupt"])
def test_fleet_loader_refuses_init_journal_before_manifest_parse(
    tmp_path, journal_state, monkeypatch
):
    workspace = workspace_with_init_journal(tmp_path, journal_state)
    monkeypatch.setattr(
        "project_knowledge.fleet.load_manifest_payload",
        lambda *args, **kwargs: pytest.fail("manifest parsed before recovery gate"),
    )
    with pytest.raises(FleetConfigError) as raised:
        load_fleet_workspace(workspace)
    assert raised.value.code == "init_recovery_required"
```

For the worktree case, fixture `.git` files point at `/workspace/repository/.git/worktrees/feature-a` and `/workspace/repository/.git/worktrees/feature-b`, sharing `/workspace/repository/.git` as one canonical common Git directory. For non-Git repositories, use `(st_dev, st_ino)` of the opened real repository root as the alias identity. Reject a parent/child repository pair even if both contain valid manifests.

- [ ] **Step 3: Run and observe the missing fleet module**

Run: `uv run pytest -q tests/test_fleet.py -k 'workspace or rejects or parallel'`

Expected: collection fails for missing `project_knowledge.fleet`.

- [ ] **Step 4: Implement the strict loader and alias key**

Use an event-checked PyYAML loader that forbids aliases, merge keys, duplicate keys, non-string mapping keys, and multiple documents before constructing values. The accepted YAML mapping is exactly:

```python
{
    "schema_version": 1,
    "projects": [
        {"id": "api", "repository": "repos/api"},
        {"id": "documentation", "repository": "repos/docs"},
    ],
    "defaults": {"max_parallel": int},  # optional; defaults to 2
}
```

Open the workspace parent/file and each repository path component with no-follow descriptors. Cap the workspace at 262,144 bytes, descriptor-read it with pre/open/post binding plus a second digest pass, and parse only those captured bytes. Require repository paths unique both lexically and by resolved path, beneath the real fleet root, non-symlink directories, and not nested. Before opening a project manifest, call Core's descriptor-safe read-only `inspect_init_journal(..., repository_access=repository)` and keep using the already opened repository identity; any recoverable/corrupt state is stable `init_recovery_required`, with no manifest parse or state mutation. From that same repository descriptor, open `.graphify-project.yaml` with `openat(O_NOFOLLOW)`, cap it at 262,144 bytes, descriptor-capture it with the same binding/double-digest checks, and call the core `load_manifest_payload(captured_bytes, repo_root)`; never reopen the manifest pathname. Persist `(st_dev, st_ino)` from that descriptor as `FleetProject.repository_identity`, not from a later `stat()`. Require `manifest.project_id == entry.id`, schema v2, and non-null UUIDv4 `project_uid`, then reject duplicate UIDs.

Define `_git_alias_key(repository: RepositoryAccess)` without invoking Git and
without reopening `repository.root`. Descriptor-open `.git` with
`openat(repository.descriptor, ..., O_NOFOLLOW)`. For a real directory, retain
that descriptor. For a regular marker, cap it at 4,096 bytes, require exactly
one UTF-8 `gitdir: <path>` line, and traverse the target component-by-component
with no-follow directory opens (from an opened filesystem-root descriptor for
an absolute target or the retained worktree descriptor for a relative target).
Immediately after the `.git` directory/marker and all target descriptors are
opened but before the alias key is finalized, invoke the private test-only
`after_git_alias_open_before_finalize` checkpoint. Then descriptor-open and
strictly parse an optional bounded `commondir` file. An absent file means the
gitdir itself is common. A present file must be exactly one newline-terminated
relative path composed of 1..8 `..` segments and nothing else (the ordinary
linked-worktree value is `../..`). Resolve each ascent with retained
`openat(current_fd, "..", O_DIRECTORY|O_NOFOLLOW)`, retain/fstat every level,
and require each child plus its descriptor-opened parent to keep stable
pre/open/post bindings through final key capture; then descriptor-check the
final directory's bounded Git common-dir structure. Absolute paths, names mixed with
ascents, `.`/empty/control/backslash segments, or an ascent beyond the captured
bound are `fleet_git_changed`. This narrowly permits real Git worktree ancestry
without treating general parent traversal as repository path authority.
Reject symlinks, special files, malformed relative paths, changed
pre/open/post inode bindings, inaccessible targets, and digest drift as stable
`fleet_git_changed`. Never call `Path.resolve()`, `realpath()`, `stat()` then
reopen, or use a canonical path string as identity. Return a `git` alias key
from `fstat()` of the retained common-git-directory descriptor; a non-Git
repository returns a `repository` key from the already-open root identity.
Two equal keys are `fleet_worktree_alias`. A `.git`/root swap after the alias
descriptor is opened must yield the originally opened key or
`fleet_git_changed`, never a replacement alias. The permanent race test injects
only at that implementable descriptor-bound checkpoint; it does not claim an
original `.git` authority before any `.git` descriptor exists.

The `--project` option itself is repeatable, but each selected ID value must be
unique. Selection rejects unknown or duplicate values and returns the original
workspace order—not selector order.

- [ ] **Step 5: Run strict fleet configuration tests**

Run: `uv run pytest -q tests/test_fleet.py -k 'workspace or rejects or parallel or select' tests/test_manifest.py`

Expected: PASS without creating `.project-knowledge` or invoking Git.

- [ ] **Step 6: Commit universal fleet configuration**

```bash
git add src/project_knowledge/fleet.py tests/test_fleet.py
git commit -m "feat: validate universal repository fleets"
```

### Task 10: Bounded fleet operations and atomic registry/query view

**Files:**
- Modify: `src/project_knowledge/fleet.py`
- Modify: `tests/test_fleet.py`
- Test: `tests/test_operation_state.py`
- Test: `tests/test_github_artifacts.py`

**Interfaces:**
- Consumes: core `inspect_init_journal`, `doctor_project`, `inspect_project_state`/`assess_health`, `RefreshOptions`/`refresh_project`, `registry_status`/`registry_sync`, `capture_registry_snapshot`/`query_registry`; compatibility `resolve_graphify_compatibility`/`bind_semantic_backend_credential`; artifact `pull_bundle`; and Task 9 workspace selection.
- Produces: `FleetProjectAdmission(project: FleetProject, manifest: ProjectManifest)` and private `_load_fleet_project_manifest(project, *, expected_manifest: ProjectManifest | None = None) -> FleetProjectAdmission`; it opens the persisted identity, gates the journal, descriptor-loads the current manifest, revalidates schema/project ID/UID, and when expected is supplied requires exact full semantic equality before returning.
- Produces: `require_fleet_projects_ready(selected: tuple[FleetProject, ...]) -> tuple[FleetProjectAdmission, ...]`, a read-only all-selected identity/journal/current-manifest gate used before any credential binding or dispatch; mismatch is stable path-free `fleet_repository_changed`.
- Produces: `FleetOperation = Literal["doctor", "health", "pull", "refresh", "registry-sync", "query"]`.
- Produces: `FleetProjectResult(id: str, uid: str, status: str, result: dict[str, object] | None, error_code: str | None, duration_ms: int)`.
- Produces: `FleetResult(operation: FleetOperation, status: Literal["ok", "partial_failure", "failed"], projects: tuple[FleetProjectResult, ...], result: dict[str, object] | None)`; `result` is used only for aggregate fleet query output.
- Produces: `FleetProjectOutcome(status: str, result: dict[str, object] | None)` and protocol `FleetProjectRunner.__call__(admission: FleetProjectAdmission, operation: FleetOperation, request: FleetProjectWorkRequest) -> FleetProjectOutcome`. The coordinator, not the runner, owns ID/UID, timing, exception mapping, ordering, and final `FleetProjectResult` construction. A worker request is freshly derived for exactly one admission and can never reference the aggregate request or another project's environment.
- Produces frozen `FleetBaseRequest()`, `PullFleetRequest(credentials: GithubCredentials = field(repr=False))`, `ProjectBackendEnvironment(project_uid: UUID, credential_name: str | None = field(repr=False), entries: tuple[tuple[str, str], ...] = field(repr=False))` with defensive contract-aware `capture(...)` and fresh-copy `as_mapping()`, `RefreshFleetRequest(backend: str | None, model: str | None, deep: bool, code_only: bool, project_environments: tuple[ProjectBackendEnvironment, ...] = field(repr=False))`, and `FleetQueryRequest(query: RegistryQueryRequest)`.
- Produces closed `FleetOperationRequest: TypeAlias = FleetBaseRequest | PullFleetRequest | RefreshFleetRequest | FleetQueryRequest`; no request contains an arbitrary mapping, argv, executable, output path, provider override, or untyped payload.
- Produces private frozen `BaseProjectWorkRequest()`, `PullProjectWorkRequest(credentials=field(repr=False))`, and `RefreshProjectWorkRequest(options: RefreshOptions, environment: ProjectBackendEnvironment | None = field(repr=False))`, plus closed `FleetProjectWorkRequest`. These are the only values submitted to futures. A semantic refresh work request contains exactly the matching admission UID's one environment; code-only contains `None`.
- Produces: `run_fleet_operation(workspace: FleetWorkspace, operation: FleetOperation, selected: tuple[FleetProject, ...], request: FleetOperationRequest, runner: FleetProjectRunner = DEFAULT_FLEET_RUNNER, *, expected_admissions: tuple[FleetProjectAdmission, ...] | None = None) -> FleetResult`. `expected_admissions` is a library-only pre-secret binding used by the CLI/workflow coordinator and has no parser/workspace/request representation.

The exact request/runner contract is:

```python
@dataclass(frozen=True)
class FleetBaseRequest:
    pass

@dataclass(frozen=True)
class PullFleetRequest:
    credentials: GithubCredentials = field(repr=False)

@dataclass(frozen=True)
class ProjectBackendEnvironment:
    project_uid: UUID
    credential_name: str | None = field(repr=False)
    entries: tuple[tuple[str, str], ...] = field(repr=False)

    @classmethod
    def capture(
        cls, project_uid: UUID, values: Mapping[str, str], *,
        canonical_credential_name: str | None,
    ) -> "ProjectBackendEnvironment":
        return cls(
            project_uid,
            canonical_credential_name,
            _validate_and_freeze_backend_environment(
                values, canonical_credential_name=canonical_credential_name
            ),
        )

    def as_mapping(self) -> dict[str, str]:
        return dict(self.entries)

@dataclass(frozen=True)
class RefreshFleetRequest:
    backend: str | None
    model: str | None
    deep: bool
    code_only: bool
    project_environments: tuple[ProjectBackendEnvironment, ...] = field(
        repr=False
    )

@dataclass(frozen=True)
class FleetQueryRequest:
    query: RegistryQueryRequest

FleetOperationRequest: TypeAlias = (
    FleetBaseRequest | PullFleetRequest | RefreshFleetRequest | FleetQueryRequest
)

@dataclass(frozen=True)
class BaseProjectWorkRequest:
    pass

@dataclass(frozen=True)
class PullProjectWorkRequest:
    credentials: GithubCredentials = field(repr=False)

@dataclass(frozen=True)
class RefreshProjectWorkRequest:
    options: RefreshOptions
    environment: ProjectBackendEnvironment | None = field(repr=False)

FleetProjectWorkRequest: TypeAlias = (
    BaseProjectWorkRequest | PullProjectWorkRequest | RefreshProjectWorkRequest
)

@dataclass(frozen=True)
class FleetProjectOutcome:
    status: str
    result: dict[str, object] | None

class FleetProjectRunner(Protocol):
    def __call__(
        self,
        admission: FleetProjectAdmission,
        operation: FleetOperation,
        request: FleetProjectWorkRequest,
    ) -> FleetProjectOutcome: ...
```

- [ ] **Step 1: Write bounded parallelism, isolation, and ordering tests**

```python
def test_parallel_fleet_operation_is_bounded_and_returns_configuration_order(workspace, blocking_runner):
    result = run_fleet_operation(
        workspace, "health", workspace.projects,
        FleetBaseRequest(), blocking_runner,
    )
    assert blocking_runner.max_active == workspace.max_parallel == 2
    assert [project.id for project in result.projects] == ["api", "documentation", "worker"]


def test_partial_failure_does_not_cancel_or_rollback_other_repositories(workspace, recording_runner):
    recording_runner.fail("documentation", code="graph_missing")
    request = RefreshFleetRequest(
        backend="openai", model="gpt-5-mini", deep=True,
        code_only=False,
        project_environments=(
            ProjectBackendEnvironment.capture(API_UID, {
                **minimal_environment(), "OPENAI_API_KEY": "fixture-secret",
            }, canonical_credential_name="OPENAI_API_KEY"),
            ProjectBackendEnvironment.capture(DOCS_UID, {
                **minimal_environment(), "OPENAI_API_KEY": "fixture-secret",
            }, canonical_credential_name="OPENAI_API_KEY"),
            ProjectBackendEnvironment.capture(WORKER_UID, {
                **minimal_environment(), "OPENAI_API_KEY": "fixture-secret",
            }, canonical_credential_name="OPENAI_API_KEY"),
        ),
    )
    result = run_fleet_operation(
        workspace, "refresh", workspace.projects, request, recording_runner
    )
    assert result.status == "partial_failure"
    assert [item.status for item in result.projects] == ["refreshed", "failed", "refreshed"]
    assert recording_runner.completed == {"api", "worker"}
    assert recording_runner.rollback_calls == []
    assert all(
        set(environment) == {"HOME", "LANG", "LC_ALL", "PATH", "OPENAI_API_KEY"}
        for environment in recording_runner.refresh_environments
    )
    assert "fixture-secret" not in repr(request)

def test_refresh_environment_is_immutable_after_capture_and_while_queued(
    workspace, queued_runner,
) -> None:
    source = {**minimal_environment(), "OPENAI_API_KEY": "original-secret"}
    captured = ProjectBackendEnvironment.capture(
        workspace.projects[0].project_uid, source,
        canonical_credential_name="OPENAI_API_KEY",
    )
    request = RefreshFleetRequest(
        "openai", "gpt-5", False, False, (captured,)
    )
    queued_runner.after_gate(lambda: source.__setitem__(
        "OPENAI_API_KEY", "replacement-secret"
    ))
    run_fleet_operation(
        workspace, "refresh", (workspace.projects[0],), request, queued_runner
    )
    assert queued_runner.environments == [{
        **minimal_environment(), "OPENAI_API_KEY": "original-secret",
    }]
    assert captured.as_mapping() is not captured.as_mapping()


def test_worker_request_cannot_observe_another_project_environment(
    workspace, malicious_recording_runner,
) -> None:
    environments = tuple(
        ProjectBackendEnvironment.capture(
            project.project_uid,
            {**minimal_environment(), "OPENAI_API_KEY": f"secret-{project.id}"},
            canonical_credential_name="OPENAI_API_KEY",
        )
        for project in workspace.projects
    )
    aggregate = RefreshFleetRequest(
        "openai", "gpt-5", False, False, environments
    )
    run_fleet_operation(
        workspace, "refresh", workspace.projects, aggregate,
        malicious_recording_runner,
    )
    for admission, work_request in malicious_recording_runner.calls:
        assert type(work_request) is RefreshProjectWorkRequest
        assert not hasattr(work_request, "project_environments")
        assert work_request.environment is not None
        assert work_request.environment.project_uid == admission.project.project_uid
        visible = work_request.environment.as_mapping()
        assert visible["OPENAI_API_KEY"] == f"secret-{admission.project.id}"
        assert all(
            f"secret-{other.id}" not in repr(work_request)
            and f"secret-{other.id}" not in json.dumps(visible)
            for other in workspace.projects if other.id != admission.project.id
        )

def test_code_only_refresh_has_no_semantic_fields_or_captured_environments(
    workspace, recording_runner,
) -> None:
    request = RefreshFleetRequest(
        backend=None, model=None, deep=False, code_only=True,
        project_environments=(),
    )
    run_fleet_operation(
        workspace, "refresh", workspace.projects, request, recording_runner
    )
    assert recording_runner.refresh_options == [
        RefreshOptions(None, None, False, True)
    ] * len(workspace.projects)
    assert recording_runner.refresh_ambient == [{}] * len(workspace.projects)

@pytest.mark.parametrize("request", [
    RefreshFleetRequest("openai", None, False, False, ()),
    RefreshFleetRequest(None, "gpt-5", False, False, ()),
    RefreshFleetRequest(None, None, True, True, ()),
    RefreshFleetRequest("openai", "gpt-5", False, True, ()),
])
def test_invalid_code_only_or_semantic_pair_fails_before_runner_or_secret(
    workspace, recording_runner, request,
) -> None:
    with pytest.raises(FleetConfigError, match="fleet_invalid"):
        run_fleet_operation(
            workspace, "refresh", workspace.projects, request, recording_runner
        )
    assert recording_runner.calls == []

@pytest.mark.parametrize("journal_state", ["recoverable", "corrupt"])
def test_fleet_recovery_gate_runs_before_runner_and_mutation(
    workspace, recording_runner, journal_state
) -> None:
    seed_init_journal(workspace.projects[0], journal_state)
    before = tuple(tree_snapshot(project.root) for project in workspace.projects)
    with pytest.raises(FleetConfigError) as raised:
        run_fleet_operation(
            workspace, "refresh", workspace.projects,
            RefreshFleetRequest("openai", "gpt-5", True, False, ()),
            recording_runner,
        )
    assert raised.value.code == "init_recovery_required"
    assert recording_runner.calls == []
    assert tuple(tree_snapshot(project.root) for project in workspace.projects) == before

def test_repository_replaced_after_workspace_load_fails_before_runner(
    workspace, recording_runner, tmp_path,
) -> None:
    project = workspace.projects[0]
    original = tmp_path / "original-api"
    replace_repository_root_with_copied_id_uid(project.root, original)
    replacement_before = tree_snapshot(project.root)
    with pytest.raises(FleetConfigError) as raised:
        run_fleet_operation(
            workspace, "health", workspace.projects,
            FleetBaseRequest(), recording_runner,
        )
    assert raised.value.code == "fleet_repository_changed"
    assert recording_runner.calls == []
    assert tree_snapshot(project.root) == replacement_before
    assert not (project.root / ".project-knowledge").exists()

def test_fleet_admission_reloads_current_manifest_before_credential_binding(
    workspace,
) -> None:
    project = workspace.projects[0]
    rewrite_manifest_semantically(
        project.root, project_id=project.id, project_uid=project.project_uid,
        track_html=True,
    )
    admissions = require_fleet_projects_ready((project,))
    assert admissions[0].manifest.track_html is True


def test_pre_secret_admission_change_is_rejected_before_request_credential_use(
    workspace, recording_runner,
) -> None:
    selected = (workspace.projects[0],)
    admitted = require_fleet_projects_ready(selected)
    rewrite_manifest_semantically(
        selected[0].root,
        artifacts=github_artifacts(repository="attacker/redirect"),
    )
    request = PullFleetRequest(GithubCredentials("must-remain-unused"))
    with pytest.raises(FleetConfigError) as raised:
        run_fleet_operation(
            workspace, "pull", selected, request, recording_runner,
            expected_admissions=admitted,
        )
    assert raised.value.code == "fleet_manifest_changed"
    assert recording_runner.calls == []
    assert recording_runner.credential_reads == []


def test_worker_reload_requires_full_aggregate_manifest_semantics(
    workspace, production_runner_fixture,
) -> None:
    project = workspace.projects[0]
    production_runner_fixture.after_aggregate_gate(
        lambda: rewrite_manifest_semantically(
            project.root,
            project_id=project.id,
            project_uid=project.project_uid,
            privacy=privacy_with_extra_include("private"),
            artifacts=github_artifacts(repository="attacker/redirect"),
        )
    )
    result = run_fleet_operation(
        workspace, "pull", (project,),
        PullFleetRequest(GithubCredentials("must-remain-unused")),
        production_runner_fixture.runner,
    )
    assert result.projects[0].error_code == "fleet_manifest_changed"
    assert production_runner_fixture.https_calls == []
    assert production_runner_fixture.gh_calls == []

def test_fleet_admission_rejects_changed_project_identity_before_runner(
    workspace, recording_runner,
) -> None:
    project = workspace.projects[0]
    rewrite_manifest_semantically(project.root, project_uid=uuid4())
    with pytest.raises(FleetConfigError) as raised:
        run_fleet_operation(
            workspace, "health", (project,), FleetBaseRequest(),
            recording_runner,
        )
    assert raised.value.code == "fleet_manifest_changed"
    assert recording_runner.calls == []

@pytest.mark.parametrize(
    "operation", ["doctor", "health", "pull", "refresh", "registry-sync", "query"]
)
def test_root_swap_after_aggregate_gate_is_caught_by_operation_boundary(
    workspace, production_runner_fixture, operation,
) -> None:
    project = workspace.projects[0]
    replacement = production_runner_fixture.swap_root_after_aggregate_gate(project)
    result = run_fleet_operation(
        workspace, operation, (project,),
        production_runner_fixture.request_for(operation),
        production_runner_fixture.runner,
    )
    assert result.status == "failed"
    assert result.projects[0].error_code == "fleet_repository_changed"
    assert production_runner_fixture.graphify_calls == []
    assert production_runner_fixture.https_calls == []
    assert production_runner_fixture.gh_calls == []
    assert production_runner_fixture.registry_capture_calls == []
    assert tree_snapshot(project.root) == replacement

@pytest.mark.parametrize("mutation", ["valid_semantic_change", "malformed_yaml"])
@pytest.mark.parametrize(
    "operation", ["doctor", "health", "pull", "refresh", "registry-sync", "query"]
)
def test_manifest_swap_after_worker_reload_fails_before_graphify(
    workspace, production_runner_fixture, mutation, operation,
) -> None:
    project = workspace.projects[0]
    production_runner_fixture.swap_manifest_after_worker_admission(
        project, mutation, operation=operation
    )
    result = run_fleet_operation(
        workspace, operation, (project,),
        production_runner_fixture.request_for(operation),
        production_runner_fixture.runner,
    )
    assert result.status == "failed"
    assert result.projects[0].error_code == "fleet_manifest_changed"
    assert production_runner_fixture.graphify_calls == []
    assert production_runner_fixture.https_calls == []
    assert production_runner_fixture.gh_calls == []
    assert production_runner_fixture.registry_capture_calls == []
```

Only semantic CLI mode constructs one `ProjectBackendEnvironment` per selected project. It resolves that project's current compatibility and `BackendContract`, passes the exact `canonical_credential_environment` into `capture`, then takes the credential from `ATLASWEAVER_BACKEND_TOKEN` when that trusted-wrapper bridge is present or otherwise from that one canonical name. It starts from Core's `minimal_environment()` (exactly `HOME`, `LANG`, `LC_ALL`, and `PATH`), calls `bind_semantic_backend_credential(contract, backend, credential)`, and merges only that returned one-key mapping. For `openai` the only additional name is `OPENAI_API_KEY`; the other exact registry mappings are `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, `MOONSHOT_API_KEY`, and `OLLAMA_API_KEY`. A missing credential is accepted only when the selected compatibility backend declares credentialless operation, in which case only the four base names remain. Code-only constructs no project environment, reads no secret, and uses `RefreshFleetRequest(None, None, False, True, ())`. The request contains no generic token field, executable override, arbitrary argv, output path, provider override, or allow-dirty field.

- [ ] **Step 2: Write serialized registry and atomic fleet-query tests**

```python
def test_registry_sync_is_globally_serialized_in_workspace_order(workspace, recording_runner):
    result = run_fleet_operation(workspace, "registry-sync", workspace.projects, FleetBaseRequest(), recording_runner)
    assert recording_runner.registry_max_active == 1
    assert recording_runner.registry_order == ["api", "documentation", "worker"]
    assert [item.id for item in result.projects] == recording_runner.registry_order


def test_fleet_query_uses_one_verified_atomic_registry_snapshot(workspace, registry_fixture):
    request = FleetQueryRequest(query=RegistryQueryRequest(
        command="path", source="api:start", target="worker:end"
    ))
    result = run_fleet_operation(workspace, "query", workspace.projects, request)
    assert registry_fixture.capture_calls == [(
        tuple(project.project_uid for project in workspace.projects), True,
    )]
    assert result.status == "ok"
    assert result.result is not None
    assert result.result["trust"] == "navigation"
    assert all(project.result is None for project in result.projects)

@pytest.mark.parametrize("command,expected_depth", [("explain", 1), ("affected", 2)])
def test_registry_query_default_depth_matches_single_repository_api(
    registry_fixture, command: str, expected_depth: int,
) -> None:
    request = RegistryQueryRequest(command=command, node="api:start")
    query_registry(registry_fixture.current_snapshot(), request)
    assert registry_fixture.traversal_depth == expected_depth


@pytest.mark.parametrize("state", ["missing", "stale", "mismatch", "projection_mismatch"])
def test_fleet_query_refuses_noncurrent_registry_without_implicit_sync(workspace, registry_fixture, state):
    registry_fixture.set_state(state)
    before = registry_fixture.snapshot_bytes()
    result = run_fleet_operation(workspace, "query", workspace.projects, fleet_query_request())
    assert result.status == "failed"
    assert registry_fixture.sync_calls == []
    assert registry_fixture.snapshot_bytes() == before
```

- [ ] **Step 3: Run and observe missing coordinator APIs**

Run: `uv run pytest -q tests/test_fleet.py -k 'bounded or partial or registry or query'`

Expected: FAIL for missing `run_fleet_operation`/request/result types.

- [ ] **Step 4: Implement operation delegation without lifecycle duplication**

`_load_fleet_project_manifest` opens a selected repository through
`open_repository_access(...,
expected_repository_identity=project.repository_identity)`, calls
`inspect_init_journal(..., repository_access=repository)`, descriptor-loads the
current manifest, and re-requires schema 2 plus exact configured project ID/UID.
When `expected_manifest` is supplied it also requires exact full semantic
equality through Core's manifest contract: Graphify version, privacy/include/
exclude/coverage controls, output, feature intent, artifact host/repository/
repository ID/channel/source ref/signer, and every remaining schema field—not
merely ID/UID.
It returns `FleetProjectAdmission`; it never trusts or stores a manifest loaded
only at workspace-parse time. Identity mismatch is constant/path-free
`fleet_repository_changed`; changed schema/ID/UID or a manifest race is
`fleet_manifest_changed`; recoverable/corrupt state is
`init_recovery_required`. `require_fleet_projects_ready` runs this helper for
every selected repository in configuration order and returns the admissions;
it creates nothing. A `stat()` followed by a pathname reopen is forbidden as
authority. The CLI uses these returned current manifests to resolve each
compatibility/backend contract before secret lookup, retains the exact tuple,
constructs the closed credential/environment request, and passes the tuple as
`expected_admissions`. A provider/privacy/signer rewrite after token lookup can
therefore never receive that token or become the worker's current contract.

`run_fleet_operation` first validates an optional `expected_admissions` tuple
against the exact selected IDs/UIDs/order, then invokes the aggregate gate
before allocating workers or touching any request credential mapping. Every
fresh aggregate manifest must be fully semantically equal to the corresponding
expected pre-secret manifest or the operation fails `fleet_manifest_changed`
without reading a request credential field. It retains that aggregate admission
map, and each per-project worker reloads once more with
`expected_manifest=aggregate_admission.manifest` immediately before its
operation and passes both that equal current manifest and the persisted expected root identity
into the exact consuming Core/artifact API. Every Core API accepting a manifest
performs its own descriptor-rooted `require_current_manifest` check, closing the
last gap between worker reload and child/graph access. A root, journal, or
manifest swap after any earlier gate therefore yields a failed project with the
same stable code and no Graphify child, GitHub/gh call, registry capture, or
replacement mutation. Query reloads all project manifests immediately before
registry status/capture.

Before admission or field access, require the exact closed operation/request
pair: doctor, health, and registry-sync accept only `FleetBaseRequest`; pull
accepts only `PullFleetRequest`; refresh only `RefreshFleetRequest`; query only
`FleetQueryRequest`. Subclasses, booleans in integer fields, duplicate project
environment UUIDs, missing/extra selected UUIDs, and every mismatched pair fail
as path-free `fleet_invalid` before a secret is read or a runner/future is
created. All request dataclasses are frozen; credential-bearing fields use
`repr=False`, are omitted from result serialization, and are scoped to the one
matching selected operation.

For refresh, enforce the exact mode contract before environment field access:
code-only requires `backend is None`, `model is None`, `deep is False`, and an
empty `project_environments` tuple. Semantic mode requires non-empty validated
`backend` and public `model`, permits `deep`, and requires exactly one captured
environment for every selected project. No mixed pair is representable past
admission.

`ProjectBackendEnvironment.capture` validates exact `str` keys/values and
copies caller data immediately into one canonical immutable tuple: `HOME`,
`LANG`, `LC_ALL`, `PATH` in that order, followed by zero or one compatibility-
declared credential name. It rejects duplicates, extra/missing base names,
wrong credential name, subclasses, controls, and a mapping that changes during
the defensive copy. The caller supplies the canonical credential name resolved
from that project's admitted `BackendContract`; capture stores it privately and
accepts only the four base names plus that optional exact key. For semantic
dispatch, `run_fleet_operation` independently resolves the current admission's
backend contract and requires the stored name, optional credential presence,
and selected UUID coverage to match it before creating futures. The coordinator
then constructs one new `RefreshProjectWorkRequest` per admission containing
only that admission's `RefreshOptions` and matching environment object; it does
not close over or submit the aggregate `RefreshFleetRequest`. A worker receives
only a new dict from `as_mapping()` for its own child call and discards it
afterward; no mutable caller mapping, aggregate environment tuple, or another
project's credential crosses the future boundary. The same split produces
closed base/pull work requests for their matching operations.

The production runner is the sole constructor of project outcomes and converts
only the declared domain result `.to_dict()`/fixed serializer for the matching
operation. The coordinator rejects subclasses, non-string status, non-dict
result, non-finite numbers, non-string mapping keys, secret-shaped strings,
unknown operation-specific status, or canonical JSON above 1 MiB as stable
`fleet_result_invalid`; no runner may return a prebuilt `FleetProjectResult` or
choose another project's ID/UID/duration. Test runners implement the same
callable protocol.

Use `ThreadPoolExecutor(max_workers=workspace.max_parallel)` only for doctor,
health, pull, and refresh. Submit in configuration order, retain a future-to-
index map, catch only the public operation exception families, map root identity
authority failure to `fleet_repository_changed`, and map any `ManifestError`
from a consuming boundary that received `admission.manifest`—including strict
malformed/unsupported reload and doctor's expected-manifest mismatch—to
`fleet_manifest_changed`. These authority mappings take precedence over the
ordinary direct-command `invalid_manifest`/doctor diagnostic shape. Convert
failures to stable path-
free `FleetProjectResult`, and assemble results by index. Do not stop already
submitted independent work after one failure. Never share manifest, lifecycle
lock, credential environment, temporary directory, or mutation state between
projects. Each `executor.submit` receives only `(admission, operation,
project_work_request)` as positional state; its callable/closure captures no
aggregate request. `DEFAULT_FLEET_RUNNER` receives a `FleetProjectAdmission` and delegates
exactly: doctor to `doctor_project(project.root,
expected_repository_identity=project.repository_identity,
expected_manifest=admission.manifest)`;
health to `assess_health(inspect_project_state(project.root,
admission.manifest,
expected_repository_identity=project.repository_identity))`; refresh to
`refresh_project(project.root, admission.manifest, request.options,
ambient=(request.environment.as_mapping() if request.environment is not None else {}),
expected_repository_identity=project.repository_identity)`; pull to
`pull_bundle(project.root, request.credentials,
expected_repository_identity=project.repository_identity,
expected_manifest=admission.manifest)`;
registry sync/status to `registry_sync(project.root, admission.manifest, ...)`
or `registry_status(project.root, admission.manifest, ...)` with the same
expected identity; aggregate query reloads all admissions then calls the exact
snapshot/query APIs once. It passes neither test-only
executable nor runner overrides.

Fleet refresh projects serialize the complete Core `RefreshResult`, including
descriptor-validated `graph_digest`, `generation_digest`, and installed
`build_epoch`; they never reconstruct identity from graph bytes. The closed
result schema permits `recovery_id` only as `null`/omitted on normal results or
as the bounded opaque Core-generated ID when status is
`promoted_but_stale` and limitations contain `cleanup_failed`; it is never a
path and is included in the 1 MiB outcome cap. Content-free
operation state/CI handoff uses those same fields for verified success, while
`promoted_but_stale` follows the last-verified-success plus failure rule.

`registry-sync` runs one repository at a time in configuration order because each project operation acquires its repository lifecycle lock then the user-global registry lock. It delegates to the core registry API and maps bounded `registry_busy` independently.

`query` is one read-only aggregate operation: first require `registry_status(...).status == "current"` for every selected project, then capture exactly one Core-owned AtlasWeaver registry snapshot for selected UUIDs with `require_graphify_projection=True` and call `query_registry` once. Store `QueryEnvelope.to_dict()` only in `FleetResult.result`; per-project rows carry admission status/error only and never receive an aggregate path or payload. Namespace every result project as `{"id": <display-id>, "uid": <uuid>}`. If any entry is missing/stale/mismatched, set aggregate `result=None`, return no partial query data, and never sync implicitly.

Fleet doctor/health/pull/local refresh require no Git. GitHub credentials and the per-project admitted backend environments are passed only to the specific selected operation boundary that consumes them, are absent from result dataclasses, and are never submitted to an unselected or different project. Add a three-project regression proving one selected OpenAI refresh sees only the four Core base names plus `OPENAI_API_KEY`, doctor/health see no credential environment, and serialized results/state/summary contain neither the credential environment name nor secret value.

- [ ] **Step 5: Run coordinator and operation state suites**

Run: `uv run pytest -q tests/test_fleet.py tests/test_operation_state.py tests/test_github_artifacts.py`

Expected: PASS with deterministic ordering across 100 randomized completion schedules and `max_active <= max_parallel` in every run.

- [ ] **Step 6: Commit fleet coordination**

```bash
git add src/project_knowledge/fleet.py tests/test_fleet.py
git commit -m "feat: coordinate bounded universal fleet operations"
```

### Task 11: Public artifact, pull, agent, and fleet CLI contracts

**Files:**
- Modify: `src/project_knowledge/cli.py`
- Create: `tests/test_artifact_fleet_cli.py`
- Modify: `tests/test_cli.py`
- Test: `tests/test_bundles_pack.py`
- Test: `tests/test_bundles_install.py`
- Test: `tests/test_github_artifacts.py`
- Test: `tests/test_agent_install.py`
- Test: `tests/test_fleet.py`

**Interfaces:**
- Consumes: all public library APIs from Tasks 1–10.
- Produces public commands: `artifact pack`, `artifact install`, `pull`, `install-agent`, `uninstall-agent`, and `fleet doctor|health|pull|refresh|registry-sync|query`.
- Keeps `main(argv: Sequence[str] | None = None) -> int` and one stable JSON envelope/error mapper.

- [ ] **Step 1: Write the exact parser surface test before adding commands**

```python
def test_parser_exposes_exact_artifact_agent_and_fleet_surface():
    surface = parser_surface(build_parser())
    assert surface["artifact"] == {"pack", "install"}
    assert {"pull", "install-agent", "uninstall-agent", "fleet"} <= surface["root"]
    assert surface["fleet"] == {
        "doctor", "health", "pull", "refresh", "registry-sync", "query"
    }
    assert "publish" not in surface["root"]
    assert "publish" not in surface["artifact"]
```

Exact flags:

```text
project-knowledge artifact pack --repo REPO --output ZIP [--json]
project-knowledge artifact install --repo REPO --bundle ZIP [--json]
project-knowledge pull --repo REPO [--json]
project-knowledge install-agent --platform {codex,agents} [--home USER_HOME] [--json]
project-knowledge uninstall-agent --platform {codex,agents} [--home USER_HOME] [--json]
project-knowledge fleet <operation> --workspace YAML [--project ID] [--json]
```

`fleet refresh` adds only the same validated core refresh flags (`--backend`, `--model`, `--deep`, `--code-only`); semantic mode requires backend and public model together, while either alone fails before project dispatch or secret lookup. Code-only rejects backend/model/deep, serializes both optional fields as `None`, captures no per-project environment, and performs no secret lookup. It does not accept executable overrides, arbitrary native flags, output paths, provider identity, or `--allow-dirty`. `fleet query` has one required nested operation with the core caps: `query TERM [--limit 1..100]`, `path SOURCE TARGET [--max-depth 1..32]`, `explain NODE [--depth 1..2]`, or `affected NODE [--depth 0..8] [--relation RELATION]` with at most 16 relations. It builds `RegistryQueryRequest`, never an opaque shell string. `GITHUB_TOKEN` and `ATLASWEAVER_BACKEND_TOKEN` are environment-only and never accepted as argv values; no command exposes `--gh-binary` or `--graphify-binary`.

- [ ] **Step 2: Write success/error JSON and read-only/mutation tests**

```python
def test_artifact_pack_json_is_path_free(repo, tmp_path):
    result = run_cli("artifact", "pack", "--repo", repo, "--output", tmp_path / "graph.zip", "--json")
    assert result.returncode == 0
    document = json.loads(result.stdout)
    assert document == {
        "artifact_sha256": document["artifact_sha256"],
        "byte_length": document["byte_length"],
        "command": "artifact pack", "project_id": "demo",
        "schema_version": 1, "status": "packed",
    }
    assert str(repo) not in result.stdout
    assert str(tmp_path) not in result.stdout


def test_fleet_partial_failure_is_nonzero_with_stable_order(workspace):
    result = run_cli("fleet", "health", "--workspace", workspace, "--json")
    assert result.returncode == 1
    document = json.loads(result.stdout)
    assert document["status"] == "partial_failure"
    assert [item["id"] for item in document["projects"]] == ["api", "documentation"]
    assert all(set(item) <= {"duration_ms", "error_code", "id", "result", "status", "uid"} for item in document["projects"])


def test_pull_requires_environment_token_without_echoing_it(repo, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = run_cli("pull", "--repo", repo, "--json")
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "github_token_required"
    assert result.stderr == ""

@pytest.mark.parametrize(("arguments", "expected"), [
    (("--backend", "openai"), "semantic_model_required"),
    (("--model", "gpt-5"), "semantic_backend_required"),
    (("--backend", "openai", "--model=--api-key"), "semantic_model_required"),
    (("--backend", "openai", "--model", "bad\nmodel"), "semantic_model_required"),
    (("--backend", "openai", "--model", "ghp_" + "a" * 32), "semantic_model_required"),
])
def test_fleet_refresh_validates_public_model_before_secret_or_dispatch(
    workspace, arguments, expected, monkeypatch
) -> None:
    reads: list[str] = []
    monkeypatch.setattr(
        "project_knowledge.cli._read_secret_environment",
        lambda name: reads.append(name) or pytest.fail("secret read"),
    )
    runner = RecordingFleetRunner()
    result = invoke_cli_with_fleet_runner(
        runner, "fleet", "refresh", "--workspace", workspace,
        *arguments, "--json",
    )
    assert json.loads(result.stdout)["error"]["code"] == expected
    assert reads == []
    assert runner.calls == []

def test_fleet_root_replacement_after_load_precedes_secret_and_dispatch(
    workspace, monkeypatch,
) -> None:
    real_load = load_fleet_workspace
    def load_then_replace(path: Path) -> FleetWorkspace:
        loaded = real_load(path)
        replace_repository_root_with_copied_id_uid(
            loaded.projects[0].root, path.parent / "original-api"
        )
        return loaded
    monkeypatch.setattr(
        "project_knowledge.cli.load_fleet_workspace", load_then_replace
    )
    monkeypatch.setattr(
        "project_knowledge.cli._read_secret_environment",
        lambda name: pytest.fail(f"secret read after repository replacement: {name}"),
    )
    runner = RecordingFleetRunner()
    result = invoke_cli_with_fleet_runner(
        runner, "fleet", "refresh", "--workspace", workspace,
        "--backend", "openai", "--model", "gpt-5", "--json",
    )
    assert json.loads(result.stdout)["error"]["code"] == "fleet_repository_changed"
    assert runner.calls == []


def test_fleet_manifest_change_after_token_read_cannot_redirect_credential(
    workspace, monkeypatch, fake_https, fake_runner,
) -> None:
    project = first_project(workspace)
    monkeypatch.setenv("GITHUB_TOKEN", "must-remain-unused")
    install_secret_read_checkpoint(
        "GITHUB_TOKEN",
        lambda: rewrite_manifest_semantically(
            project.root,
            artifacts=github_artifacts(repository="attacker/redirect"),
        ),
    )
    result = run_cli(
        "fleet", "pull", "--workspace", workspace, "--json",
        dependencies=fleet_pull_dependencies(fake_https, fake_runner),
    )
    document = json.loads(result.stdout)
    assert document["projects"][0]["error_code"] == "fleet_manifest_changed"
    assert fake_https.calls == []
    assert fake_runner.calls == []


def test_pull_cli_rejects_control_bearing_token_before_https_or_gh(
    repo, monkeypatch, fake_https, fake_runner,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token\r\ninjected: value")
    result = run_cli(
        "pull", "--repo", repo, "--json",
        dependencies=pull_dependencies(fake_https, fake_runner),
    )
    assert json.loads(result.stdout)["error"]["code"] == "github_token_required"
    assert fake_https.calls == []
    assert fake_runner.calls == []

@pytest.mark.parametrize("journal_state", ["recoverable", "corrupt"])
@pytest.mark.parametrize("direct_command", ["artifact-pack", "artifact-install", "pull"])
def test_repo_and_fleet_commands_gate_init_recovery_before_secret_or_mutation(
    repo, workspace_for_repo, journal_state, direct_command, monkeypatch
) -> None:
    seed_init_journal(repo, journal_state)
    before = tree_snapshot(repo)
    monkeypatch.setattr(
        "project_knowledge.cli._read_secret_environment",
        lambda name: pytest.fail("secret read before recovery gate"),
    )
    direct = invoke_recovery_gated_direct_command(direct_command, repo)
    fleet = run_cli(
        "fleet", "refresh", "--workspace", workspace_for_repo,
        "--backend", "openai", "--model=--api-key", "--json",
    )
    assert json.loads(direct.stdout)["error"]["code"] == "init_recovery_required"
    assert json.loads(fleet.stdout)["error"]["code"] == "init_recovery_required"
    assert tree_snapshot(repo) == before

@pytest.mark.parametrize("command", ["artifact-install", "pull", "fleet-refresh"])
def test_all_promoted_but_stale_commands_use_global_result_exit_three(
    command, stale_result_fixture
) -> None:
    result = invoke_stale_command(command, stale_result_fixture)
    document = json.loads(result.stdout)
    assert result.returncode == 3
    assert document["status"] == "promoted_but_stale"
    assert "code" not in document

@pytest.mark.parametrize("family,fallback_code,fallback_message", [
    (BundleError, "bundle_failed", "bundle operation failed"),
    (GithubArtifactError, "github_artifact_failed", "GitHub artifact operation failed"),
    (AgentInstallError, "agent_install_failed", "agent resource operation failed"),
    (FleetConfigError, "fleet_invalid", "fleet request is invalid"),
    (WorkflowPublishError, "workflow_publish_failed", "workflow publication failed"),
])
def test_forged_family_code_and_message_never_escape_public_envelope(
    family, fallback_code, fallback_message, monkeypatch,
) -> None:
    forged = "private_path_" + str(Path.home())
    monkeypatch.setattr(
        "project_knowledge.cli._dispatch_typed",
        raising(family(forged, "secret message " + forged)),
    )
    result = run_cli("artifact", "pack", "--repo", fixture_repo(), "--json")
    assert json.loads(result.stdout)["error"] == {
        "code": fallback_code, "message": fallback_message,
    }
    assert forged not in result.stdout + result.stderr

def test_every_documented_new_error_has_constant_message_and_exit() -> None:
    for family, table in documented_artifact_error_tables():
        for code, (exit_code, message) in table.items():
            rendered = map_public_error(family(code, "attacker-controlled message"))
            assert rendered == (exit_code, code, message)


@pytest.mark.parametrize("error", [
    BundleError("bundle_cleanup_failed", "ignored", "a" * 32),
    BundleError("bundle_output_recovery_required", "ignored", "b" * 32),
    GithubArtifactError("github_cleanup_failed", "ignored", "c" * 32),
    WorkflowPublishError("workflow_cleanup_failed", "ignored", "d" * 32),
    WorkflowPublishError(
        "workflow_output_recovery_required", "ignored", "e" * 32
    ),
])
def test_only_closed_cleanup_errors_expose_validated_recovery_id(error) -> None:
    rendered = render_public_error_document(error)
    assert rendered["error"]["recovery_id"] == error.recovery_id
    forged = type(error)("private_code", "ignored", "f" * 32)
    assert "recovery_id" not in render_public_error_document(forged)["error"]

def test_direct_and_fleet_manifest_races_use_distinct_closed_codes(
    repo, workspace_for_repo, manifest_race,
) -> None:
    manifest_race.rewrite_after_admission(repo)
    direct = run_cli("pull", "--repo", repo, "--json")
    fleet = run_cli(
        "fleet", "pull", "--workspace", workspace_for_repo, "--json"
    )
    assert json.loads(direct.stdout)["error"]["code"] == "manifest_changed"
    assert json.loads(fleet.stdout)["projects"][0]["error_code"] == (
        "fleet_manifest_changed"
    )
```

Also assert doctor/health/fleet-health/query before/after tree equality; pack creates only the explicit output plus mutation state; install/pull/refresh mutate only their selected repository; agent commands mutate only the selected platform home; unknown selection prevents all fleet work.

- [ ] **Step 3: Run parser/CLI tests and confirm missing command failures**

Run: `uv run pytest -q tests/test_artifact_fleet_cli.py tests/test_cli.py`

Expected: parser surface and command invocations fail because the new commands are not registered.

- [ ] **Step 4: Refactor parser construction into nested explicit handlers**

Register nested `argparse` subparsers and dispatch to typed handler functions; do not inspect raw argv after parsing. Normalize UUIDs to canonical strings only at JSON serialization. Add stable error mappings:

```python
from types import MappingProxyType

_ADMISSION_PUBLIC_ERRORS = {
    "init_recovery_required": (1, "configuration recovery is required"),
    "manifest_migration_required": (1, "manifest migration is required"),
}

BUNDLE_PUBLIC_ERRORS = MappingProxyType({
    **_ADMISSION_PUBLIC_ERRORS,
    "bundle_invalid": (1, "bundle validation failed"),
    "bundle_too_large": (1, "bundle exceeds its size limit"),
    "bundle_changed_during_capture": (1, "bundle changed during capture"),
    "bundle_output_exists": (1, "bundle output already exists"),
    "bundle_output_in_source": (1, "bundle output overlaps its source"),
    "bundle_cleanup_failed": (1, "private bundle cleanup failed"),
    "bundle_output_recovery_required": (1, "bundle output recovery is required"),
    "bundle_source_drift": (1, "project source changed"),
    "bundle_generation_changed": (1, "graph generation changed"),
    "bundle_identity_mismatch": (1, "bundle identity does not match"),
    "bundle_stale": (1, "bundle is stale"),
    "bundle_pull_required": (1, "remote bundle requires verified pull"),
    "bundle_unattested": (1, "bundle is not authorized for install"),
})

GITHUB_PUBLIC_ERRORS = MappingProxyType({
    **_ADMISSION_PUBLIC_ERRORS,
    "github_token_required": (1, "GitHub credential is required"),
    "github_resolution_failed": (1, "GitHub artifact resolution failed"),
    "github_redirect_invalid": (1, "GitHub artifact redirect is invalid"),
    "github_cleanup_failed": (1, "private GitHub cleanup failed"),
    "attestation_failed": (1, "artifact attestation verification failed"),
    "attestation_ambiguous": (1, "artifact attestation is ambiguous"),
})

AGENT_PUBLIC_ERRORS = MappingProxyType({
    "agent_destination_unmanaged": (1, "agent destination is unmanaged"),
    "agent_destination_modified": (1, "managed agent destination was modified"),
    "agent_install_failed": (1, "agent resource operation failed"),
})

FLEET_PUBLIC_ERRORS = MappingProxyType({
    **_ADMISSION_PUBLIC_ERRORS,
    "fleet_invalid": (1, "fleet request is invalid"),
    "fleet_document_too_large": (1, "fleet document exceeds its size limit"),
    "fleet_manifest_too_large": (1, "project manifest exceeds its size limit"),
    "fleet_repository_changed": (1, "fleet repository identity changed"),
    "fleet_manifest_changed": (1, "fleet project contract changed"),
    "fleet_git_changed": (1, "fleet Git identity changed"),
    "fleet_worktree_alias": (1, "fleet repositories alias one Git worktree"),
    "fleet_result_invalid": (1, "fleet project result is invalid"),
    "fleet_partial_failure": (1, "one or more fleet operations failed"),
})

REGISTRY_SNAPSHOT_PUBLIC_ERRORS = MappingProxyType({
    "registry_snapshot_missing": (1, "registry snapshot is missing"),
    "registry_snapshot_stale": (1, "registry snapshot is stale"),
    "registry_snapshot_mismatch": (1, "registry snapshot identity does not match"),
    "registry_snapshot_too_large": (1, "registry snapshot exceeds its size limit"),
    "registry_snapshot_busy": (1, "registry snapshot is busy"),
})

WORKFLOW_BOUNDARY_PUBLIC_ERRORS = MappingProxyType({
    "workflow_root_invalid": (1, "workflow repository root is invalid"),
    "workflow_root_forbidden": (1, "workflow repository root is forbidden"),
    "workflow_root_changed": (1, "workflow repository root changed"),
})

WORKFLOW_PUBLIC_ERRORS = MappingProxyType({
    **_ADMISSION_PUBLIC_ERRORS,
    "workflow_root_invalid": (1, "workflow repository root is invalid"),
    "workflow_root_forbidden": (1, "workflow repository root is forbidden"),
    "workflow_root_changed": (1, "workflow repository root changed"),
    "workflow_platform_unsupported": (1, "publication platform is unsupported"),
    "workflow_git_unavailable": (1, "publication Git authority is unavailable"),
    "workflow_source_mismatch": (1, "publication source identity does not match"),
    "workflow_cleanup_failed": (1, "private publication cleanup failed"),
    "workflow_output_recovery_required": (
        1, "publication output recovery is required"
    ),
    "workflow_publish_failed": (1, "workflow publication failed"),
})

FAMILY_FALLBACKS = MappingProxyType({
    BundleError: (1, "bundle_failed", "bundle operation failed"),
    GithubArtifactError: (1, "github_artifact_failed", "GitHub artifact operation failed"),
    AgentInstallError: (1, "agent_install_failed", "agent resource operation failed"),
    FleetConfigError: (1, "fleet_invalid", "fleet request is invalid"),
    WorkflowBoundaryError: (
        1, "workflow_root_invalid", "workflow repository root is invalid"
    ),
    WorkflowPublishError: (1, "workflow_publish_failed", "workflow publication failed"),
})
```

The mapper selects a table by exact exception family, looks up only the exact
`.code`, and substitutes the constant code/message/exit from that table. It
never serializes the exception's code or message directly. Unknown/forged
codes, subclasses, or a wrong-family documented code use that family's fixed
fallback. Extend Core's immutable `RegistryError` allowlist with exactly
`REGISTRY_SNAPSHOT_PUBLIC_ERRORS`; operation-state write failures collapse to
Core's constant `state_write_failed` and never replace the primary operation
result. `ManifestError(kind="changed")` continues through Core's closed mapper
as direct `manifest_changed`; the fleet coordinator translates it to
`fleet_manifest_changed` before constructing a project row.
`WorkflowBoundaryError` uses only `WORKFLOW_BOUNDARY_PUBLIC_ERRORS` in the
check/build internal driver. The publication driver catches that exact family
and raises `WorkflowPublishError(code, WORKFLOW_PUBLIC_ERRORS[code][1]) from
None`; it never forwards a boundary exception, arbitrary message, or subclass.

The canonical error serializer includes an optional sibling
`error.recovery_id` only when the exact exception family/code pair is
`BundleError` with `bundle_cleanup_failed` or
`bundle_output_recovery_required`, `GithubArtifactError` with
`github_cleanup_failed`, or `WorkflowPublishError` with
`workflow_cleanup_failed`/`workflow_output_recovery_required`. It validates the
lowercase-hex grammar again, omits the field when null, and collapses every
forged code/ID combination to the family fallback without an ID. Successful
`promoted_but_stale` result envelopes may include the same validated
`recovery_id` only for a committed cleanup failure.

`promoted_but_stale` is intentionally absent from all error maps: refresh,
artifact install, pull, and fleet refresh serialize it as a successful result
shape with the descriptor-revalidated installed identity (or an all-null
unverifiable graph/generation/epoch identity) and return
the one global exit code 3 inherited from Core.

Preserve Core Task 11's centralized routing while adding repo-less commands.
Route `fleet`, `install-agent`, and `uninstall-agent` before
`_real_repo`/generic manifest loading. Fleet loads its workspace, validates
selection, and calls `require_fleet_projects_ready`—which atomically validates
every persisted repository identity—before any secret access. It retains that
exact admission tuple across request construction and passes it to
`run_fleet_operation(expected_admissions=...)`; the coordinator's first fresh
gate must match every full manifest semantic before it may inspect the closed
request's credential fields. Every direct
repository command—including `artifact pack`, `artifact install`, and `pull`—
derives its explicit `--repo`, opens one noncreating `RepositoryAccess`, applies
`inspect_init_journal(..., repository_access=repository) == "none"` before
manifest/library dispatch or secret lookup, captures that access identity, and
passes it as `expected_repository_identity` to the consuming pack/install/pull
library boundary. Install and pull also receive the descriptor-loaded admission
manifest as `expected_manifest` and compare its complete semantic contract
inside their lifecycle/network boundary; pack also receives the descriptor-
loaded admission manifest and requires it under its lifecycle lock. Workflow
build passes the same pair from `WorkflowRepository`. Thus a root or in-place manifest swap after the CLI
gate cannot redirect a mutation or authenticated network call. Existing
init/migrate/doctor self-loading behavior
and the journal gate for all legacy manifest commands remain unchanged; the
refactor may not bypass or duplicate them.

All errors preserve Core Task 11's one canonical envelope:
`{"schema_version":1,"command":"artifact install","status":"error","error":{"code":"bundle_invalid","message":"bundle validation failed"}}`, with the actual command/code and a constant path-free message substituted from fixed enums. No command adds a flat top-level `code`, raw exception message, traceback, or alternate error shape. `_read_secret_environment(name)` is the sole private environment lookup for `GITHUB_TOKEN`, `ATLASWEAVER_BACKEND_TOKEN`, and registry-declared canonical backend names, which makes ordering testable. `pull` copies `GITHUB_TOKEN` into a local variable only after the recovery gate, immediately removes it from any child/base environment map, and passes it explicitly only to resolver/verifier. For fleet refresh, after workspace/selection/recovery validation, validate backend/model pairing and call Core's `validate_public_model_identifier(model)` before `_read_secret_environment` for any name. Missing backend is `semantic_backend_required`; missing/rejected model is `semantic_model_required`; code-only rejects all semantic flags, constructs the exact empty-environment request, and returns to dispatch without any lookup. Semantic mode then prefers the trusted-wrapper `ATLASWEAVER_BACKEND_TOKEN` when set, otherwise reads only each selected backend's registry-declared canonical credential name, calls `minimal_environment()` and `bind_semantic_backend_credential()` separately for every selected project's compatibility contract, merges exactly the four base entries plus at most that one canonical credential, captures it with that contract's canonical name, constructs the complete `ProjectBackendEnvironment` tuple, then discards all source credential references before dispatch. An unselected project never receives a binding and non-refresh commands never read these variables. Agent default homes are platform-adapter values. Fleet selection and recovery are validated before any credential is read or any runner is invoked.

- [ ] **Step 5: Run all CLI and subsystem suites**

Run: `uv run pytest -q tests/test_artifact_fleet_cli.py tests/test_cli.py tests/test_bundles_pack.py tests/test_bundles_install.py tests/test_github_artifacts.py tests/test_agent_install.py tests/test_fleet.py`

Expected: PASS; `project-knowledge --help` contains no product name and no publish/flag/provider override.

- [ ] **Step 6: Commit the public command surface**

```bash
git add src/project_knowledge/cli.py tests/test_artifact_fleet_cli.py tests/test_cli.py
git commit -m "feat: expose artifact and universal fleet commands"
```

### Task 12: Reusable read-only check workflow and pinned base CI

**Files:**
- Create: `.github/workflows/atlasweaver-check.yml`
- Create: `src/project_knowledge/workflow_boundary.py`
- Create: `tests/test_workflow_boundary.py`
- Create: `tests/test_workflows.py`
- Modify: `.github/workflows/ci.yml`
- Test: `tests/test_public_release.py`

**Interfaces:**
- Consumes: trusted Core `preflight` with exact `--backend`/`--model`/`--deep` flags, public `doctor`, `scan-secrets`, and `health` JSON, plus `render_ci_summary`.
- Produces reusable workflow inputs `repo-root: string`, `python-version: string`, `backend: string`, `model: string`, `deep-mode: boolean`, and `require-impact-trust: boolean`.
- Produces one optional named secret `semantic_backend_token`.
- Produces internal renderer invocation `python -m project_knowledge.operation_state ci-summary --output-json PATH --preflight PATH --doctor PATH --scan PATH --health PATH`; it writes only under `$RUNNER_TEMP` and `$GITHUB_STEP_SUMMARY`.
- Produces private `open_workflow_repository(consumer_checkout: Path, repo_root: str, *, forbidden_checkout: Path) -> AbstractContextManager[WorkflowRepository]`. `WorkflowRepository` retains the no-follow consumer/root directory descriptors and exact validated relative segment tuple, exposes the verified `RepositoryIdentity` plus a library-only descriptor-rooted `RepositoryAccess` and `WorkflowGitScope`, and can revalidate every traversed directory entry. The same helper is mandatory for check/build/publish internal drivers and is not a public CLI option.
- Produces private exact `WorkflowBoundaryError(code: Literal["workflow_root_invalid", "workflow_root_forbidden", "workflow_root_changed", "workflow_extraction_invalid"], message: str)`. The check/build driver renders it only through `WORKFLOW_BOUNDARY_PUBLIC_ERRORS`; the publication driver catches it and constructs an exact `WorkflowPublishError` with the same allowlisted code/constant message. Raw helper text and subclasses never cross either internal entrypoint.
- Produces private `admit_workflow_extraction_mode(backend: str, model: str, deep: bool) -> Literal["code_only", "semantic"]`: code-only requires empty backend/model and false deep; semantic requires non-empty validated backend/model and permits deep. Its exact internal `workflow_boundary validate-extraction` verb reads only bounded `ATLASWEAVER_BACKEND`, `ATLASWEAVER_MODEL`, and `ATLASWEAVER_DEEP`, deletes them, emits no content, and runs before Graphify install, secret exposure, repository output, or the mode-specific driver.
- Produces exact internal entrypoint `python -m project_knowledge.workflow_boundary check --consumer-checkout PATH --repo-root RELATIVE --trusted-tool-checkout PATH --output-directory PATH`. In one `WorkflowRepository` scope it emits exactly `preflight.json`, `doctor.json`, `scan.json`, and `health.json`; its strict parser exposes no command/provider/executable/path override beyond those fixed workflow paths and the relative root. Backend/model/deep are read from the fixed workflow environment contract only after repository admission.

- [ ] **Step 1: Write workflow contract tests before creating either workflow**

```python
PINNED_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")

def test_check_workflow_has_closed_typed_inputs_and_read_only_permissions():
    workflow = load_workflow("atlasweaver-check.yml")
    call = workflow["on"]["workflow_call"]
    assert set(call["inputs"]) == {
        "backend", "deep-mode", "model", "python-version",
        "repo-root", "require-impact-trust",
    }
    assert set(call["secrets"]) == {"semantic_backend_token"}
    assert workflow["permissions"] == {"contents": "read"}
    assert all(job.get("permissions", {"contents": "read"}) == {"contents": "read"}
               for job in workflow["jobs"].values())


def test_every_action_reference_is_a_full_commit_sha():
    for path in (WORKFLOWS / "ci.yml", WORKFLOWS / "atlasweaver-check.yml"):
        workflow = load_workflow(path.name)
        for uses in action_uses(workflow):
            assert PINNED_ACTION.fullmatch(uses), (path, uses)


def test_ci_fetches_history_required_by_immutable_pin_contracts():
    workflow = load_workflow("ci.yml")
    for checkout in steps_using(workflow, "actions/checkout"):
        assert checkout["with"]["fetch-depth"] == 0


def test_check_never_executes_consumer_commands_or_uploads_graph_content():
    text = (WORKFLOWS / "atlasweaver-check.yml").read_text(encoding="utf-8")
    assert "test-command" not in text
    assert "shell-command" not in text
    assert "graphify-out" not in upload_artifact_paths(load_workflow("atlasweaver-check.yml"))
    assert "python -m project_knowledge.workflow_boundary check" in text
    assert all(name in text for name in (
        "preflight.json", "doctor.json", "scan.json", "health.json",
        "GITHUB_STEP_SUMMARY",
    ))
    assert all(term not in text for term in (
        "project-knowledge preflight", "project-knowledge doctor",
        "project-knowledge scan-secrets", "project-knowledge health",
    ))


def test_check_uses_backend_inputs_only_in_read_only_preflight_and_scopes_secret():
    workflow = load_workflow("atlasweaver-check.yml")
    preflight = step_named(workflow, "Semantic preflight")
    code_only = step_named(workflow, "Code-only preflight")
    assert preflight["if"] == "${{ inputs.backend != '' && inputs.model != '' }}"
    assert code_only["if"] == (
        "${{ inputs.backend == '' && inputs.model == '' && !inputs.deep-mode }}"
    )
    assert "ATLASWEAVER_BACKEND_TOKEN" not in json.dumps(code_only, sort_keys=True)
    assert set(preflight["env"]) == {
        "ATLASWEAVER_BACKEND", "ATLASWEAVER_BACKEND_TOKEN",
        "ATLASWEAVER_DEEP", "ATLASWEAVER_MODEL", "ATLASWEAVER_REPO_ROOT",
    }
    assert preflight["env"]["ATLASWEAVER_BACKEND_TOKEN"] == (
        "${{ secrets.semantic_backend_token }}"
    )
    assert "python -m project_knowledge.workflow_boundary check" in preflight["run"]
    assert all(flag not in preflight["run"] for flag in (
        "--backend", "--model", "--deep", "--token", "--credential",
    ))
    for step in all_steps_except(workflow, "Semantic preflight"):
        assert "ATLASWEAVER_BACKEND_TOKEN" not in json.dumps(step, sort_keys=True)
    assert "semantic_backend_token" not in json.dumps(
        summary_step(workflow), sort_keys=True
    )


def test_deep_only_workflow_input_fails_before_secret_graphify_or_output(
    workflow_spies,
) -> None:
    with pytest.raises(WorkflowBoundaryError) as raised:
        run_internal_extraction_admission(
            backend="", model="", deep=True, spies=workflow_spies
        )
    assert raised.value.code == "workflow_extraction_invalid"
    assert workflow_spies.secret_reads == []
    assert workflow_spies.graphify_calls == []
    assert workflow_spies.output_writes == []


def test_check_workflow_runs_no_secret_admission_before_mode_driver():
    workflow = load_workflow("atlasweaver-check.yml")
    steps = workflow["jobs"]["check"]["steps"]
    admission = step_named(workflow, "Validate extraction inputs")
    semantic = step_named(workflow, "Semantic preflight")
    code_only = step_named(workflow, "Code-only preflight")
    assert steps.index(admission) < steps.index(semantic)
    assert steps.index(admission) < steps.index(code_only)
    assert set(admission["env"]) == {
        "ATLASWEAVER_BACKEND", "ATLASWEAVER_DEEP", "ATLASWEAVER_MODEL",
    }
    assert "ATLASWEAVER_BACKEND_TOKEN" not in json.dumps(admission)
    assert "workflow_boundary validate-extraction" in admission["run"]
    assert semantic["if"] == "${{ inputs.backend != '' && inputs.model != '' }}"
    assert code_only["if"] == (
        "${{ inputs.backend == '' && inputs.model == '' && !inputs.deep-mode }}"
    )
    assert "ATLASWEAVER_DEEP" not in json.dumps(code_only)


def test_reusable_workflow_uses_hard_pinned_trusted_tool_not_caller_workflow_sha():
    workflow = load_workflow("atlasweaver-check.yml")
    tool_sha = workflow["env"]["ATLASWEAVER_TOOL_SHA"]
    assert re.fullmatch(r"[0-9a-f]{40}", tool_sha)
    assert git_object_exists(tool_sha)
    assert checkout_ref_for_path(workflow, "atlasweaver-tool") == "${{ env.ATLASWEAVER_TOOL_SHA }}"
    text = json.dumps(workflow, sort_keys=True)
    assert "github.workflow_sha" not in text
    assert '--project "$GITHUB_WORKSPACE/atlasweaver-tool"' in text
    assert '--project "$GITHUB_WORKSPACE/consumer"' not in text


@pytest.mark.parametrize("repo_root", [
    "/etc", "../outside", "nested/../../outside", "nested//repo",
    "nested\\repo", "nested/\x00repo",
])
def test_workflow_repository_root_rejects_escape_before_secret_or_graphify(
    consumer_checkout, trusted_tool_checkout, repo_root, workflow_spies,
) -> None:
    with pytest.raises(WorkflowBoundaryError) as raised:
        run_internal_workflow_check(
            consumer_checkout, repo_root, trusted_tool_checkout,
            semantic_token="must-remain-unread",
            spies=workflow_spies,
        )
    assert raised.value.code == "workflow_root_invalid"
    assert workflow_spies.secret_reads == []
    assert workflow_spies.graphify_calls == []
    assert workflow_spies.publication_calls == []


def test_workflow_repository_root_rejects_symlink_component_before_secret(
    consumer_checkout, trusted_tool_checkout, workflow_spies,
) -> None:
    (consumer_checkout / "alias").symlink_to(
        consumer_checkout / "actual", target_is_directory=True
    )
    with pytest.raises(WorkflowBoundaryError) as raised:
        run_internal_workflow_check(
            consumer_checkout, "alias/repo", trusted_tool_checkout,
            semantic_token="must-remain-unread", spies=workflow_spies,
        )
    assert raised.value.code == "workflow_root_invalid"
    assert workflow_spies.secret_reads == []
    assert workflow_spies.graphify_calls == []


def test_workflow_repository_cannot_alias_trusted_tool_checkout(
    trusted_tool_checkout, workflow_spies,
) -> None:
    with pytest.raises(WorkflowBoundaryError) as raised:
        run_internal_workflow_check(
            trusted_tool_checkout, ".", trusted_tool_checkout,
            semantic_token="must-remain-unread", spies=workflow_spies,
        )
    assert raised.value.code == "workflow_root_forbidden"
    assert workflow_spies.secret_reads == []
    assert workflow_spies.graphify_calls == []


def test_workflow_repository_root_swap_uses_retained_original_or_fails_closed(
    consumer_checkout, trusted_tool_checkout, workflow_fault, workflow_spies,
    tmp_path,
) -> None:
    workflow_fault.after_root_open(
        lambda: replace_repository_root(
            consumer_checkout / "repo", tmp_path / "original",
            attacker_repository_with_copied_id_uid(tmp_path),
        )
    )
    outcome = run_internal_workflow_check(
        consumer_checkout, "repo", trusted_tool_checkout,
        semantic_token="token", spies=workflow_spies,
    )
    assert outcome.code == "workflow_root_changed"
    assert workflow_spies.secret_reads == []
    assert workflow_spies.graphify_calls == []
    assert workflow_spies.replacement_reads == []
```

`load_workflow()` uses a YAML 1.2-compatible loader or a PyYAML loader with the YAML 1.1 boolean resolver removed so the key `on` remains a string. `action_uses()` recursively collects every scalar under a `uses` key; local/reusable workflow references are tested separately.

- [ ] **Step 2: Run and verify the new workflow is missing and old CI pins fail**

Run: `uv run pytest -q tests/test_workflows.py tests/test_public_release.py`

Expected: FAIL because `atlasweaver-check.yml` is absent and `ci.yml` still uses mutable `@v4`/`@v5` refs.

- [ ] **Step 3: Implement the reusable check with fixed trusted-tool checkout**

Immediately before creating the workflow, run `git rev-parse --verify HEAD`, require a lowercase 40-hex commit containing all Tasks 1–11 tool code, and insert that exact value as workflow-level `ATLASWEAVER_TOOL_SHA` using `apply_patch`. This is the provisional implementation pin used to make Task 12 executable; Task 14 replaces both reusable-workflow pins with final release tool commit A after all tool/resources/docs code is frozen. Never derive either pin from `github.workflow_sha`, because the normal reusable-workflow `github` context identifies the caller. Create this job shape, retaining the exact action pins from Global Constraints:

```yaml
name: AtlasWeaver check
on:
  workflow_call:
    inputs:
      repo-root: {type: string, required: false, default: "."}
      python-version: {type: string, required: false, default: "3.13"}
      backend: {type: string, required: false, default: ""}
      model: {type: string, required: false, default: ""}
      deep-mode: {type: boolean, required: false, default: false}
      require-impact-trust: {type: boolean, required: false, default: false}
    secrets:
      semantic_backend_token: {required: false}
permissions:
  contents: read
jobs:
  check:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - name: Checkout consumer snapshot
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262
        with: {path: consumer, persist-credentials: false}
      - name: Checkout trusted AtlasWeaver workflow source
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262
        with:
          repository: MarkusMakEvil/atlasweaver
          ref: ${{ env.ATLASWEAVER_TOOL_SHA }}
          path: atlasweaver-tool
          persist-credentials: false
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065
        with: {python-version: "${{ inputs.python-version }}"}
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e
        with: {version: "0.8.14", enable-cache: false}
```

Follow those fixed setup steps with `uv sync --project "$GITHUB_WORKSPACE/atlasweaver-tool" --frozen`, then derive the sole production Graphify version from pinned trusted tool commit A and install it without a YAML literal:

```bash
GRAPHIFY_VERSION="$(uv run --project "$GITHUB_WORKSPACE/atlasweaver-tool" python -c 'from project_knowledge.compatibility import production_graphify_compatibility as p; print(p().version)')"
case "$GRAPHIFY_VERSION" in
  ''|*[!0-9A-Za-z._-]*) exit 1 ;;
esac
uv tool install "graphifyy==$GRAPHIFY_VERSION"
```

Run consumer inspection only through a trusted-tool internal driver that calls
`open_workflow_repository()` before reading the semantic secret, resolving
Graphify, parsing a manifest, or touching output. The raw `repo-root` input is
either exactly `.` or a bounded UTF-8 POSIX-relative string whose raw segments
are non-empty and exclude `.`, `..`, backslash, NUL/control characters, and
platform separators; absolute paths are rejected. The helper opens the
consumer checkout no-follow, then descriptor-walks every root segment with
`openat(O_DIRECTORY|O_NOFOLLOW)`, retaining each parent/child binding. Every
component must be a real directory owned beneath that checkout. It separately
opens the trusted-tool checkout and rejects an equal device/inode at any
resolved consumer root. It revalidates the retained chain before every
Graphify/Git/network child and before each final result. Root-entry replacement
therefore either continues through the original retained descriptor or raises
path-free `workflow_root_changed`; it never adopts replacement bytes.

The YAML never forms
`$GITHUB_WORKSPACE/consumer/$ATLASWEAVER_REPO_ROOT`, calls `realpath`, or changes
directory into consumer-controlled content. It passes the fixed consumer
checkout path and untrusted relative root as distinct quoted arguments to the
pinned tool's internal driver. That process retains `WorkflowRepository` for
preflight, doctor, scan, and health and calls library APIs with its
descriptor-rooted `RepositoryAccess`; no operation reopens the raw path. Every
Python invocation still uses
`uv run --project "$GITHUB_WORKSPACE/atlasweaver-tool"`; code is never
built/installed/run from the consumer checkout or its `pyproject.toml`.

Run the trusted no-secret `validate-extraction` verb before installing Graphify
or creating any repository output. It requires backend/model both empty and
deep false for code-only, or backend/model both non-empty for semantic mode;
deep-only and every half-configured combination fail
`workflow_extraction_invalid` before secret lookup. Two mutually exclusive YAML
steps invoke the same exact `workflow_boundary check` entrypoint; whichever
runs creates all four bounded envelopes in one retained scope. Code-only
receives no secret/backend/model/deep environment, while semantic mode alone
receives `semantic_backend_token` as `ATLASWEAVER_BACKEND_TOKEN`. The driver
reads and deletes that generic environment entry only after root admission,
calls `bind_semantic_backend_credential()`, validates the registry-rendered
semantic contract, and emits only `credential_bound: bool`. Doctor, scan,
health, renderer, summaries, and
every unselected project receive neither the generic secret nor the canonical
backend environment. Pass repo-root, backend, model, and deep mode through
environment variables/quoted exact arguments—never interpolate
`${{ inputs.* }}` directly into shell source.

Fail when secret scan has unaccepted findings, core health is not admitted, or `require-impact-trust` is true and trust is not `trusted`. Always render a content-free summary in a final `if: always()` step, append Markdown to `$GITHUB_STEP_SUMMARY`, and upload only `atlasweaver-check-summary.json` with retention 7 days through the pinned upload action. Never upload the graph, report, evidence, stage, receipt, or command logs.

- [ ] **Step 4: Pin the existing base CI and extend its artifact/fleet gates**

Replace mutable checkout/setup pins in `.github/workflows/ci.yml` with the exact SHAs above, set `fetch-depth: 0` on every CI checkout so permanent commit-object/ancestor pin tests have the required history, use pinned setup-uv `0.8.14`, preserve Python `3.10` and `3.13`, preserve the compatibility plan's code-derived `production_graphify_compatibility().version` install step with no Graphify version literal in YAML, and add focused invocations for `tests/test_bundles_pack.py`, `tests/test_bundles_parse.py`, `tests/test_agent_install.py`, `tests/test_fleet.py`, and `tests/test_workflows.py` before the full suite/build. No CI job gains write permission.

- [ ] **Step 5: Exercise the Task 4 content-free CI renderer**

Invoke the already implemented strict `operation_state ci-summary` module through the pinned trusted-tool checkout. Add workflow tests proving it reads only the four bounded JSON envelopes, writes only the requested summary JSON, and appends only its content-free Markdown to `$GITHUB_STEP_SUMMARY`.

- [ ] **Step 6: Run workflow contracts and YAML action audit**

Run: `uv run pytest -q tests/test_workflows.py tests/test_operation_state.py tests/test_public_release.py`

Expected: PASS. Then run:

```bash
rg -n 'uses:\s+[^#]+@(v[0-9]+|main|master|[A-Za-z][A-Za-z0-9._/-]*)\s*$' .github/workflows
```

Expected: no output.

- [ ] **Step 7: Commit reusable check and pinned CI**

```bash
git add .github/workflows/atlasweaver-check.yml .github/workflows/ci.yml src/project_knowledge/workflow_boundary.py tests/test_workflow_boundary.py tests/test_workflows.py tests/test_operation_state.py tests/test_public_release.py
git commit -m "ci: add reusable AtlasWeaver validation workflow"
```

### Task 13: Reusable split-privilege publish workflow

**Files:**
- Create: `.github/workflows/atlasweaver-publish.yml`
- Modify: `src/project_knowledge/workflow_boundary.py`
- Modify: `src/project_knowledge/workflow_publish.py`
- Modify: `tests/test_workflow_boundary.py`
- Modify: `tests/test_workflows.py`
- Modify: `tests/test_workflow_publish.py`
- Test: `tests/test_public_release.py`

**Interfaces:**
- Consumes: Task 12's mandatory descriptor-walk `open_workflow_repository`; public-library `doctor`, `scan-secrets`, `refresh`, `health`, `artifact pack`; internal `workflow_publish prepare|verify-attestation|upload`; Task 6 `verify_attestation_policy` (not its pull receipt wrapper); pinned attestation action.
- Produces the same closed inputs/secret as Task 12.
- Produces inspect outputs `project_uid` and `channel`, used only for concurrency naming and revalidated in the privileged job.
- Extends the strict internal boundary parser with exact verbs: `inspect --consumer-checkout PATH --repo-root RELATIVE --trusted-tool-checkout PATH --output PATH` and `build --consumer-checkout PATH --repo-root RELATIVE --trusted-tool-checkout PATH --output-bundle PATH --output-summary PATH`. Both retain one `WorkflowRepository`; build admits/binds the secret only after root+manifest admission and passes expected identity/full manifest to refresh and pack. The publication prepare driver additionally passes that retained repository's `WorkflowGitScope`, preserving the checkout-root Git descriptor and selected-root prefix; it never asks `prepare_publication` to rediscover Git from the nested project path.
- Produces closed `WorkflowPublicationHandoff(schema_version=1, repository_identity, manifest_sha256, context, bundle_sha256, bundle_size, bundle_identity)` in a bounded canonical mode-0600 `$RUNNER_TEMP` file. Repository identity is local authority data and is never uploaded or emitted in summaries.
- Extends internal `workflow_publish` exact verbs so each takes the same `--consumer-checkout`, `--repo-root`, and `--trusted-tool-checkout` arguments. `prepare` additionally requires `--input`, `--output`, `--handoff`; `verify-attestation` and `upload` each require `--bundle` and `--handoff`. Every verb independently opens the descriptor boundary and validates the handoff/root/full manifest/context/bundle before reading a token, invoking `gh`, or mutating GitHub.

- [ ] **Step 1: Write the split-permission, concurrency, and no-untrusted-execution tests**

```python
def test_publish_workflow_has_split_least_privilege_jobs():
    workflow = load_workflow("atlasweaver-publish.yml")
    jobs = workflow["jobs"]
    assert set(jobs) == {"inspect", "build", "publish"}
    assert jobs["inspect"]["permissions"] == {"contents": "read"}
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["publish"]["permissions"] == {
        "attestations": "write", "contents": "write", "id-token": "write"
    }
    assert jobs["publish"]["runs-on"] == "ubuntu-24.04"
    assert jobs["publish"]["concurrency"] == {
        "cancel-in-progress": False,
        "group": "atlasweaver-publish-${{ github.repository_id }}-${{ needs.inspect.outputs.project_uid }}-${{ needs.inspect.outputs.channel }}",
    }


def test_privileged_publish_job_never_runs_graphify_tests_backend_or_consumer_commands():
    job = load_workflow("atlasweaver-publish.yml")["jobs"]["publish"]
    text = json.dumps(job, sort_keys=True)
    assert "graphify extract" not in text
    assert "project-knowledge refresh" not in text
    assert "pytest" not in text
    assert "semantic_backend_token" not in text
    assert "project_knowledge.workflow_publish prepare" in text
    assert "project_knowledge.workflow_publish upload" in text


def test_publish_secret_is_scoped_only_to_unprivileged_refresh():
    workflow = load_workflow("atlasweaver-publish.yml")
    refresh = step_named(workflow, "Refresh private graph")
    code_only = step_named(workflow, "Refresh private graph (code-only)")
    assert refresh["env"]["ATLASWEAVER_BACKEND_TOKEN"] == (
        "${{ secrets.semantic_backend_token }}"
    )
    assert refresh["if"] == "${{ inputs.backend != '' && inputs.model != '' }}"
    assert code_only["if"] == (
        "${{ inputs.backend == '' && inputs.model == '' && !inputs.deep-mode }}"
    )
    assert "ATLASWEAVER_BACKEND_TOKEN" not in json.dumps(code_only, sort_keys=True)
    assert "ATLASWEAVER_DEEP" not in json.dumps(code_only, sort_keys=True)
    for job_name, job in workflow["jobs"].items():
        for step in job["steps"]:
            if step is not refresh:
                assert "ATLASWEAVER_BACKEND_TOKEN" not in json.dumps(step, sort_keys=True)
    assert "semantic_backend_token" not in json.dumps(
        workflow["jobs"]["publish"], sort_keys=True
    )


def test_publish_attests_before_release_upload_and_pins_every_action():
    steps = load_workflow("atlasweaver-publish.yml")["jobs"]["publish"]["steps"]
    names = [step.get("name", step.get("uses", "")) for step in steps]
    assert names.index("Attest release bundle") < names.index("Verify reusable-workflow signer")
    assert names.index("Verify reusable-workflow signer") < names.index("Upload verified rolling release asset")
    assert all(PINNED_ACTION.fullmatch(value) for value in action_uses(steps))
    token_steps = {
        step["name"] for step in steps
        if "GITHUB_TOKEN" in step.get("env", {})
    }
    assert token_steps == {
        "Verify reusable-workflow signer",
        "Upload verified rolling release asset",
    }


def test_publish_uses_caller_context_for_source_and_hard_pin_for_trusted_tool():
    workflow = load_workflow("atlasweaver-publish.yml")
    assert re.fullmatch(r"[0-9a-f]{40}", workflow["env"]["ATLASWEAVER_TOOL_SHA"])
    assert checkout_ref_for_path(workflow, "atlasweaver-tool") == "${{ env.ATLASWEAVER_TOOL_SHA }}"
    text = json.dumps(workflow, sort_keys=True)
    assert "github.workflow_sha" not in text
    assert '--project "$GITHUB_WORKSPACE/atlasweaver-tool"' in text
    assert "project_knowledge.workflow_publish verify-attestation" in text


def test_publish_never_concatenates_untrusted_repo_root_into_a_path():
    text = (WORKFLOWS / "atlasweaver-publish.yml").read_text(encoding="utf-8")
    assert "consumer/$ATLASWEAVER_REPO_ROOT" not in text
    assert "consumer/${{ inputs.repo-root }}" not in text
    for job_name in ("inspect", "build", "publish"):
        assert internal_workflow_driver_mode(
            load_workflow("atlasweaver-publish.yml"), job_name
        ) == job_name


def test_internal_workflow_parsers_are_closed_and_phase_specific():
    boundary = workflow_boundary_parser_surface()
    assert boundary == {
        "inspect": {
            "--consumer-checkout", "--repo-root",
            "--trusted-tool-checkout", "--output",
        },
        "build": {
            "--consumer-checkout", "--repo-root",
            "--trusted-tool-checkout", "--output-bundle",
            "--output-summary",
        },
        "check": {
            "--consumer-checkout", "--repo-root",
            "--trusted-tool-checkout", "--output-directory",
        },
    }
    publisher = workflow_publish_parser_surface()
    common = {
        "--consumer-checkout", "--repo-root", "--trusted-tool-checkout",
    }
    assert publisher == {
        "prepare": common | {"--input", "--output", "--handoff"},
        "verify-attestation": common | {"--bundle", "--handoff"},
        "upload": common | {"--bundle", "--handoff"},
    }


@pytest.mark.parametrize("mode", ["inspect", "build", "publish"])
@pytest.mark.parametrize("attack", [
    "absolute", "parent_escape", "symlink_component", "trusted_tool_alias",
    "root_swap",
])
def test_every_publish_phase_reuses_closed_workflow_root_boundary(
    mode, attack, workflow_phase_fixture,
) -> None:
    result = workflow_phase_fixture.run(mode, attack=attack)
    assert result.code in {
        "workflow_root_invalid", "workflow_root_forbidden",
        "workflow_root_changed",
    }
    assert workflow_phase_fixture.secret_reads == []
    assert workflow_phase_fixture.graphify_calls == []
    assert workflow_phase_fixture.prepare_calls == []
    assert workflow_phase_fixture.github_mutations == []
    assert workflow_phase_fixture.replacement_reads == []


@pytest.mark.parametrize("verb", ["verify-attestation", "upload"])
def test_post_prepare_verbs_reopen_root_and_validate_immutable_handoff_first(
    publication_handoff_fixture, verb,
) -> None:
    handoff = publication_handoff_fixture.prepare()
    publication_handoff_fixture.replace_consumer_root_with_copied_id_uid()
    result = publication_handoff_fixture.invoke(
        verb, handoff, token="must-remain-unread"
    )
    assert result.code == "workflow_root_changed"
    assert publication_handoff_fixture.token_reads == []
    assert publication_handoff_fixture.gh_calls == []
    assert publication_handoff_fixture.github_mutations == []


@pytest.mark.parametrize("mutation", [
    "handoff_unknown_key", "handoff_manifest_digest", "bundle_replace",
    "context_change",
])
@pytest.mark.parametrize("verb", ["verify-attestation", "upload"])
def test_post_prepare_verbs_reject_tampered_handoff_before_authority(
    publication_handoff_fixture, mutation, verb,
) -> None:
    handoff = publication_handoff_fixture.prepare()
    publication_handoff_fixture.mutate(mutation, handoff)
    result = publication_handoff_fixture.invoke(
        verb, handoff, token="must-remain-unread"
    )
    assert result.code == "workflow_source_mismatch"
    assert publication_handoff_fixture.token_reads == []
    assert publication_handoff_fixture.gh_calls == []
    assert publication_handoff_fixture.github_mutations == []
```

Also assert: workflow is `workflow_call` only; no pull-request publication; inputs contain no shell/test/native flags/output/provider/project/signer/source-ref/channel fields; checkout credentials are not persisted; build artifact has fixed name/path/retention; publish downloads only that artifact; caller boolean outputs are never used as authorization.

- [ ] **Step 2: Run and observe the missing workflow**

Run: `uv run pytest -q tests/test_workflows.py -k publish`

Expected: FAIL because `atlasweaver-publish.yml` does not exist.

- [ ] **Step 3: Implement inspect and unprivileged build jobs**

`inspect` checks out consumer and trusted AtlasWeaver source exactly as Task 12, runs the trusted strict manifest/context inspector inside one `open_workflow_repository` scope, and writes only canonical UUID/channel outputs through `$GITHUB_OUTPUT`. It validates but does not authorize publication. Root admission and its final binding recheck precede all output.

The inspect step invokes only
`python -m project_knowledge.workflow_boundary inspect` with the three common
arguments plus `--output "$RUNNER_TEMP/inspect.json"`; a fixed trusted follow-up
projects only validated `project_uid`/`channel` into `$GITHUB_OUTPUT`. The build
step invokes only the `build` verb with the same common arguments plus
`--output-bundle "$RUNNER_TEMP/build/bundle.zip"` and
`--output-summary "$RUNNER_TEMP/build/summary.json"`. Neither parser accepts a
backend/model/token flag; the fixed environment contract is validated and
deleted/bound inside the retained process after repository admission.

`build` depends on inspect, has `contents: read`, checks out exact caller `${{ github.sha }}` with credentials disabled, and checks out trusted AtlasWeaver at the same provisional implementation pin used by Task 12; Task 14 atomically replaces both workflow constants with final release tool commit A. The normal `github` context deliberately supplies caller repository/ref/SHA; it is never used as trusted-tool identity. Every trusted command runs with `uv run --project "$GITHUB_WORKSPACE/atlasweaver-tool"`. Before Graphify installation or output, build runs the same trusted no-secret extraction-admission verb; backend empty requires model empty and deep false, while semantic mode requires both backend and model. Two mutually exclusive build steps then call the same strict driver, and only the semantic step receives backend/model/deep plus the token; the code-only step receives none of them. One trusted internal build driver accepts the fixed consumer-checkout path and the raw relative repo-root separately, enters `open_workflow_repository`, and retains that binding across doctor, secret scan, refresh, pack, and final health; it passes descriptor access/expected identity into every mutating boundary and never reopens the raw input. The job derives and installs Graphify from that trusted checkout's `production_graphify_compatibility().version` using the exact Task 12 shell and contains no duplicated version literal. Only the semantic build driver process receives `semantic_backend_token` as `ATLASWEAVER_BACKEND_TOKEN`, and it reads that value only after root/manifest admission immediately before refresh; the trusted Core refresh handler binds it to the compatibility-declared canonical environment and removes the generic name before Graphify execution. Doctor, scan, pack, health, summary, and publish receive no credential. Credential presence is decided only by the selected compatibility contract through `bind_semantic_backend_credential`: an admitted credentialless backend may proceed without a token, while a credential-required backend fails closed. It never silently adds `--code-only` or drops deep mode. It uploads one workflow artifact named `atlasweaver-build-${{ github.run_id }}` containing only `bundle.zip`, with retention 1 day. Pull-request callers still cannot publish because the privileged job's context gate requires a protected branch.

- [ ] **Step 4: Implement privileged revalidation, attestation, and upload job**

`publish` depends on inspect/build, is fixed to `ubuntu-24.04`, capability-checks
Linux `/proc/self/fd` traversal before preparation, and declares the exact
concurrency mapping tested in Step 1. It checks out consumer `github.sha` and
trusted workflow source, downloads the one build artifact into
`$RUNNER_TEMP/build`, and runs:

```text
uv run --project "$GITHUB_WORKSPACE/atlasweaver-tool" \
  python -m project_knowledge.workflow_publish prepare \
  --consumer-checkout "$GITHUB_WORKSPACE/consumer" \
  --repo-root "$ATLASWEAVER_REPO_ROOT" \
  --trusted-tool-checkout "$GITHUB_WORKSPACE/atlasweaver-tool" \
  --input "$RUNNER_TEMP/build/bundle.zip" \
  --output "$RUNNER_TEMP/release/bundle.zip" \
  --handoff "$RUNNER_TEMP/release/publication-handoff.json"
```

The actual YAML uses a block scalar and environment variables for all paths; it does not interpolate input text into shell or concatenate checkout/root strings. The internal command first enters the same Task 12 `open_workflow_repository` boundary, then rechecks `github.ref_type == branch`, `github.ref_protected == true`, exact manifest `source_ref`, exact numeric repository ID, `HEAD == github.sha`, clean tracked/safe projection, expected reusable signer workflow path, build bundle identity, and final health through the retained descriptor/expected identity. The job runs no Graphify, tests, consumer script, consumer package build, backend, or arbitrary command.

After the prepared ZIP and its cleanup contract succeed, `prepare` writes the
closed canonical handoff exclusively under the descriptor-validated
`RUNNER_TEMP` parent and fsyncs it. The handoff binds the retained repository
device/inode, canonical full-manifest SHA-256, closed authenticated
`WorkflowContext`, and prepared bundle device/inode/size/SHA-256. It contains no
path, token, source name, graph bytes, or query. Unknown keys, duplicate JSON,
non-finite/invalid values, symlink/special files, mutable-file binding, or a file
above 64 KiB fail `workflow_source_mismatch`.

Attest `$RUNNER_TEMP/release/bundle.zip` with `actions/attest-build-provenance@e8998f949152b193b063cb0ec769d69d929409be`, `subject-path` set to that exact file, and `push-to-registry: false`. Then call internal `workflow_publish verify-attestation` with the three common root arguments plus exact `--bundle`/`--handoff` files. It independently reopens the workflow root, requires root identity/full manifest/context/bundle equality with the handoff, and only then reads its step-scoped token and invokes Task 6's receipt-free `verify_attestation_policy` against the newly created attestation. It requires manifest `signer_workflow` to name `owner/atlasweaver/.github/workflows/atlasweaver-publish.yml`, manifest `signer_digest` to equal the immutable called reusable-workflow commit B, and policy repository/source ref/source digest to equal the authenticated `WorkflowContext`. No release asset exists yet, so this command accepts no `DownloadReceipt`/release/asset argument and cannot create pull authorization. This authenticated post-attestation check is the subject/signer digest authority; normal `github.workflow_sha` describes the caller and is never accepted. Scope `${{ github.token }}` as `GITHUB_TOKEN` separately to exactly the signer-verification and upload steps; neither token reaches preparation, Graphify, summary, nor artifacts. Only after verification succeeds call internal `workflow_publish upload` with the same common root arguments and exact bundle/handoff. Upload repeats all handoff/root/full-manifest/context/bundle checks before reading its separate token or making a request. The uploader then creates/resolves the release asset and performs Task 7's remote verify/tag/retention ordering; subsequent consumers add the distinct receipt-bound pull verification. Finish with an always-run content-free JSON/job summary artifact; never upload the release bundle as an ordinary workflow artifact from this job.

- [ ] **Step 5: Run workflow and publisher contract suites**

Run: `uv run pytest -q tests/test_workflows.py tests/test_workflow_publish.py tests/test_public_release.py`

Expected: PASS, including a text audit proving the privileged job lacks consumer-configurable command execution and every action uses a 40-hex SHA.

- [ ] **Step 6: Commit reusable publication**

```bash
git add .github/workflows/atlasweaver-publish.yml tests/test_workflows.py tests/test_workflow_publish.py tests/test_public_release.py
git commit -m "ci: publish attested graph bundles with split privilege"
```

### Task 14: Documentation, end-to-end acceptance, staged rollout, and release evidence

**Files:**
- Create: `tests/test_artifact_fleet_e2e.py`
- Create: `.github/workflows/atlasweaver-self-publish.yml`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `SECURITY.md`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.graphify-project.yaml`
- Modify: `.github/workflows/atlasweaver-check.yml`
- Modify: `.github/workflows/atlasweaver-publish.yml`
- Modify: `skills/using-project-knowledge-graphs/SKILL.md`
- Modify: `skills/using-project-knowledge-graphs/references/workflow.md`
- Modify: `src/project_knowledge/resources/skills/using-project-knowledge-graphs/SKILL.md`
- Modify: `src/project_knowledge/resources/skills/using-project-knowledge-graphs/references/workflow.md`
- Modify: `tests/test_skill_contract.py`
- Modify: `tests/test_public_release.py`
- Modify: `tests/test_workflows.py`
- Verify: all files changed by Tasks 1–13.

**Interfaces:**
- Consumes: complete public artifact/agent/fleet CLI, reusable workflows, operation summaries, core health/trust semantics.
- Produces: reproducible acceptance evidence and an opt-in rollout sequence; it does not enroll any external repository implicitly.

- [ ] **Step 1: Write failing black-box acceptance tests**

```python
def test_pack_twice_install_clean_clone_and_query_navigation(tmp_path, built_wheel):
    source, clone = create_matching_v2_repositories(tmp_path)
    refresh_with_fixture_graph(source, build_epoch=1_777_777_777)
    first = run_installed_cli(built_wheel, "artifact", "pack", "--repo", source,
                              "--output", tmp_path / "first.zip", "--json")
    second = run_installed_cli(built_wheel, "artifact", "pack", "--repo", source,
                               "--output", tmp_path / "second.zip", "--json")
    assert (tmp_path / "first.zip").read_bytes() == (tmp_path / "second.zip").read_bytes()
    installed = run_installed_cli(built_wheel, "artifact", "install", "--repo", clone,
                                  "--bundle", tmp_path / "first.zip", "--json")
    assert installed.returncode == 0
    health = installed_cli_json(built_wheel, "health", "--repo", clone, "--json")
    assert health["core_status"] == "healthy"
    assert health["trust"]["impact"] == "navigation"


def test_built_wheel_installs_both_agent_platforms_from_any_cwd(tmp_path, built_wheel):
    graphify = require_real_graphify("0.9.48")
    for platform in ("codex", "agents"):
        user_home = tmp_path / platform
        platform_root = user_home / (".codex" if platform == "codex" else ".agents")
        result = run_installed_cli(
            built_wheel, "install-agent", "--platform", platform,
            "--home", user_home, cwd=tmp_path / "unrelated", json_output=True,
            env=isolated_user_environment(user_home, graphify=graphify),
        )
        assert result.returncode == 0
        assert (platform_root / "skills/using-project-knowledge-graphs/SKILL.md").is_file()
        assert (platform_root / "skills/graphify/SKILL.md").is_file()


def test_three_repository_fleet_health_pull_refresh_and_registry_are_universal(tmp_path, fake_github):
    workspace = create_fleet(tmp_path, ids=("api", "documentation", "worker"), max_parallel=2)
    assert fleet_ids(run_cli_json("fleet", "health", "--workspace", workspace)) == [
        "api", "documentation", "worker"
    ]
    assert fleet_ids(run_cli_json("fleet", "pull", "--workspace", workspace,
                                  env=fake_github.env)) == ["api", "documentation", "worker"]
    assert fleet_ids(run_cli_json("fleet", "refresh", "--workspace", workspace,
                                  "--code-only")) == ["api", "documentation", "worker"]
    assert fleet_ids(run_cli_json("fleet", "registry-sync", "--workspace", workspace)) == [
        "api", "documentation", "worker"
    ]
    queried = run_cli_json(
        "fleet", "query", "--workspace", workspace,
        "query", "ProjectManifest", "--limit", "20",
    )
    assert queried["status"] == "ok"
    assert queried["result"]["trust"] == "navigation"
```

The end-to-end fixtures use only generic repository labels, correct UUIDv4 values, local fake HTTPS/attestation boundaries, and temporary platform homes. They never touch the developer's real global registry, home, GitHub account, or Obsidian vault.

- [ ] **Step 2: Run and verify acceptance tests fail before docs claim availability**

Run: `uv run pytest -q tests/test_artifact_fleet_e2e.py`

Expected: failures identify any missing CLI/resource/workflow integration; do not update availability claims until all black-box cases pass.

- [ ] **Step 3: Update reviewed skill and package mirror with the safe high-level workflow**

Replace the obsolete manual stage/adapt choreography with exact read-only `doctor`/`health`, explicit `refresh`, query trust, local pack/install, verified pull, and fleet command sections. State all mutation authority boundaries: remote pull is explicit/networked; registry sync is separate; artifact install requires an exact matching safe projection; GitHub bundles cannot be installed offline; fleet never rolls back another completed repository; Graphify 0.9.48 remains navigation-only. Keep hook/Obsidian/publication/commit/push as separately authorized mutations.

After editing the top-level skill, copy its three reviewed files byte-for-byte into package resources and require digest equality in tests; never patch the two copies independently.

- [ ] **Step 4: Update operator/security/release documentation without project-specific defaults**

README must include:

```text
project-knowledge artifact pack --repo <repo> --output graph.zip --json
project-knowledge artifact install --repo <matching-clone> --bundle graph.zip --json
project-knowledge pull --repo <repo> --json
project-knowledge install-agent --platform codex --json
project-knowledge fleet health --workspace fleet.yaml --json
```

Document the exact generic fleet schema, UUID registry key, provider manifest, release tag/asset naming, source/projection identity, navigation trust, no local publish, workflow opt-in, 256/255/128 MiB limits, GitHub host/redirect/attestation policy, and 20-asset retention. SECURITY.md must describe malicious ZIPs, release substitution, redirect credential leakage, workflow privilege separation, managed-resource ownership, and fleet path/worktree alias threats. CHANGELOG adds the integrated `0.3.0` release entry.

- [ ] **Step 5: Bump the one integrated release version and lock it**

Change `[project].version` in `pyproject.toml` from `0.2.2` to `0.3.0`, update the root package record in `uv.lock` with `uv lock`, and change public-release expectations/README install examples/CHANGELOG heading to exactly `0.3.0`/`v0.3.0`. There is no second source-tree version constant; runtime `--version` remains `importlib.metadata.version("atlasweaver")`.

Run:

```bash
uv lock --check
uv run pytest -q tests/test_public_release.py tests/test_artifact_fleet_e2e.py
```

Expected: PASS and every public version assertion reports `0.3.0`.

- [ ] **Step 6: Rebase before pinning and create immutable release tool commit A**

Fetch `origin` and verify whether the implementation branch still descends from `origin/main`. If it does not, rebase the implementation branch before creating any final workflow pin, resolve only reviewed conflicts, and rerun the full suite. No rebase is allowed after commits B/C below unless the entire A/B/C pin sequence is regenerated.

Commit all final tool code, packaged resources, version, docs, and acceptance tests:

```bash
git add src tests skills scripts examples README.md CHANGELOG.md SECURITY.md pyproject.toml uv.lock
git commit -m "release: prepare AtlasWeaver 0.3.0 tool"
release_tool_commit=$(git rev-parse --verify HEAD)
printf '%s\n' "$release_tool_commit" | rg -x '[0-9a-f]{40}'
git show "${release_tool_commit}:pyproject.toml" | rg '^version = "0\.3\.0"$'
```

Record `release_tool_commit` as A and confirm all workflow-invoked Python modules exist at that commit. The anchored regex rejects non-lowercase-hex or any length other than 40.

- [ ] **Step 7: Pin reusable workflows to A and create workflow commit B**

Use `apply_patch` to set `ATLASWEAVER_TOOL_SHA` in both reusable workflows to the exact commit A printed in Step 6. The workflow checkout must remain `repository: MarkusMakEvil/atlasweaver`, `ref: ${{ env.ATLASWEAVER_TOOL_SHA }}`, credentials disabled. Run workflow/public tests, then commit only workflow pin/test changes:

```bash
uv run pytest -q tests/test_workflows.py tests/test_workflow_publish.py tests/test_public_release.py
git add .github/workflows/atlasweaver-check.yml .github/workflows/atlasweaver-publish.yml tests/test_workflows.py tests/test_public_release.py
git commit -m "ci: pin AtlasWeaver 0.3.0 reusable workflows"
reusable_workflow_commit=$(git rev-parse --verify HEAD)
printf '%s\n' "$reusable_workflow_commit" | rg -x '[0-9a-f]{40}'
```

Record the printed lowercase 40-hex value as reusable workflow commit B. Contract tests must prove both workflow tool pins equal A, A is an ancestor containing version `0.3.0`, neither workflow uses `github.workflow_sha` as a tool pin, and the attesting workflow path is `.github/workflows/atlasweaver-publish.yml`.

- [ ] **Step 8: Add the immutable self-caller and signer config in commit C**

Migrate AtlasWeaver's own manifest to v2 with exact portable/product-independent fields plus this repository identity:

```yaml
schema_version: 2
project_id: atlasweaver
project_uid: 1ed43f8e-e849-4f05-96aa-3d965723f4f9
features:
  atlas: disabled
  registry: disabled
artifacts:
  provider: github-release
  host: github.com
  repository: MarkusMakEvil/atlasweaver
  repository_id: 1343065113
  channel: main
  source_ref: refs/heads/main
  signer_workflow: MarkusMakEvil/atlasweaver/.github/workflows/atlasweaver-publish.yml
```

Retain the immutable UUIDv4 `1ed43f8e-e849-4f05-96aa-3d965723f4f9` established by Core Task 12 and the approved include roots/output/Obsidian/Graphify/HTML fields; the snippet above lists the identity/feature/artifact section rather than replacing those fields. Before writing the provider block, run `actual_repository_id=$(gh api repos/MarkusMakEvil/atlasweaver --jq '.id')` and `test "$actual_repository_id" = "1343065113"`; fail rather than writing configuration if the authenticated GitHub API differs. Use `apply_patch` to add `signer_digest` with the exact lowercase 40-hex output of `git rev-parse --verify HEAD` from Step 7, never a tag/branch/runtime caller SHA.

Create `.github/workflows/atlasweaver-self-publish.yml` with `workflow_dispatch` only and one calling job. Use `apply_patch` to put the same exact Step 7 commit after the `@` in the reusable workflow reference. The completed workflow must satisfy this contract test:

```python
def test_self_caller_and_manifest_bind_reusable_workflow_commit():
    caller = load_workflow("atlasweaver-self-publish.yml")
    assert caller["on"] == {"workflow_dispatch": None}
    assert caller["permissions"] == {
        "attestations": "write", "contents": "write", "id-token": "write"
    }
    job = caller["jobs"]["publish"]
    prefix = (
        "MarkusMakEvil/atlasweaver/.github/workflows/"
        "atlasweaver-publish.yml@"
    )
    assert job["uses"].startswith(prefix)
    caller_commit = job["uses"][len(prefix):]
    signer_commit = str(
        load_manifest(
            ROOT / ".graphify-project.yaml", ROOT
        ).artifacts.signer_digest
    )
    assert re.fullmatch(r"[0-9a-f]{40}", caller_commit)
    assert signer_commit == caller_commit
    assert git_object_exists(f"{caller_commit}^{{commit}}")
    assert git_is_ancestor(caller_commit, "HEAD")
    assert git_path_exists_at_commit(
        caller_commit, ".github/workflows/atlasweaver-publish.yml"
    )
    assert job["permissions"] == caller["permissions"]
    assert job["with"] == {
        "backend": "openai", "deep-mode": True, "model": "gpt-5-mini",
        "python-version": "3.13", "repo-root": ".",
        "require-impact-trust": False,
    }
    assert job["secrets"] == {
        "semantic_backend_token": "${{ secrets.ATLASWEAVER_SEMANTIC_BACKEND_TOKEN }}"
    }
```

Also assert both caller and reusable grant attestation/id-token permissions; `--signer-workflow` names the reusable workflow (official GitHub attestation identity), while `--repo` identifies the caller/release repository. Commit:

```bash
git add .graphify-project.yaml .github/workflows/atlasweaver-self-publish.yml tests/test_workflows.py tests/test_manifest.py tests/test_public_release.py
git commit -m "ci: enable immutable AtlasWeaver self publication"
release_source_commit=$(git rev-parse --verify HEAD)
printf '%s\n' "$release_source_commit" | rg -x '[0-9a-f]{40}'
```

Record the printed value as release/source commit C. A/B/C must be a strict ancestor chain and C is the only release/tag/main target.

- [ ] **Step 9: Run the full verification matrix from commit C**

```bash
uv sync --frozen --dev
uv run --python 3.10 pytest -q
uv run --python 3.13 pytest -q
uv run python -m compileall -q src tests
uv build
git diff --check
```

Expected: all tests pass on Python 3.10 and 3.13; compile/build exit zero; both wheel and sdist contain the reviewed packaged skill; no ZIP golden differs between runtimes; no diff whitespace errors; workflow pins form A <- B <- C exactly.

Then install the wheel into a fresh temporary uv tool environment and run:

```bash
project-knowledge --version
project-knowledge artifact --help
project-knowledge fleet --help
project-knowledge install-agent --help
```

Expected: installed package version is `0.3.0`; all new commands exist; `artifact publish` does not exist.

- [ ] **Step 10: Audit privacy, workflow pins, branch diff, and spec coverage**

Run:

```bash
rg -n 'uses:\s+[^#]+@(v[0-9]+|main|master)\b' .github/workflows
rg -n '(GITHUB_TOKEN|GH_TOKEN|ATLASWEAVER_BACKEND_TOKEN).*?(print|echo|json|state)' src tests .github/workflows
rg -n '(BrandMap|server repo|web repo|product-specific)' src tests skills README.md .github/workflows
git status --short
git diff --stat origin/main...HEAD
```

Expected: first three scans return no unsafe matches (fixture names that assert rejection must be narrowly exempted in the test itself); the working tree is clean at C; diff against `origin/main` matches the file map. Review every artifact/fleet spec paragraph against Tasks 1–14 and record no uncovered requirement before claiming completion.

- [ ] **Step 11: Fast-forward main, tag/release, reinstall globally, and dogfood publication**

From the main checkout, verify `origin/main` has not advanced since A/B/C were created. If it has, return to Step 6 and regenerate all three commits/pins. Otherwise fast-forward only:

```bash
git fetch origin
git merge --ff-only atlasweaver-adoption
git push origin main
git tag -a v0.3.0 -m "AtlasWeaver 0.3.0"
git push origin v0.3.0
gh release create v0.3.0 --repo MarkusMakEvil/atlasweaver --verify-tag --title "AtlasWeaver 0.3.0" --generate-notes
```

Force-reinstall from the reviewed main checkout with `scripts/install-project-knowledge-tool`. From a fresh `mktemp -d` outside every checkout, require `project-knowledge --version` to print `0.3.0` and require `artifact`, `pull`, `install-agent`, and `fleet` help to expose the new surface without `publish`/binary override flags.

Dispatch the immutable self-caller and wait for success:

```bash
release_source_commit=$(git rev-parse --verify main)
dispatch_started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
baseline_run_ids=$(gh run list \
  --repo MarkusMakEvil/atlasweaver \
  --workflow atlasweaver-self-publish.yml \
  --event workflow_dispatch \
  --branch main \
  --limit 100 \
  --json databaseId \
  --jq '[.[].databaseId]')
gh workflow run atlasweaver-self-publish.yml --repo MarkusMakEvil/atlasweaver --ref main
release_run_id=""
for attempt in $(seq 1 30); do
  candidate_runs=$(gh run list \
    --repo MarkusMakEvil/atlasweaver \
    --workflow atlasweaver-self-publish.yml \
    --event workflow_dispatch \
    --branch main \
    --created ">=$dispatch_started" \
    --limit 20 \
    --json databaseId,createdAt,headSha)
  matching_runs=$(printf '%s\n' "$candidate_runs" | jq -c \
    --arg source "$release_source_commit" \
    --argjson baseline "$baseline_run_ids" \
    '[.[] as $run | select(
      $run.headSha == $source and
      ($baseline | index($run.databaseId) | not)
    )] | sort_by(.createdAt)')
  matching_count=$(printf '%s\n' "$matching_runs" | jq 'length')
  if test "$matching_count" -gt 1; then
    echo "ambiguous self-publish workflow dispatch" >&2
    exit 1
  fi
  if test "$matching_count" -eq 1; then
    release_run_id=$(printf '%s\n' "$matching_runs" | jq -r '.[0].databaseId')
  fi
  test -n "$release_run_id" && break
  sleep 2
done
test -n "$release_run_id"
gh run watch "$release_run_id" --repo MarkusMakEvil/atlasweaver --exit-status
```

Set `atlasweaver_main_checkout=$(pwd -P)` in the verified clean `main` checkout and verify the rolling release asset/attestation through the installed `project-knowledge pull --repo "$atlasweaver_main_checkout" --json`; then run health. This pull bootstraps AtlasWeaver's managed v2 graph through the new pipeline without a local semantic downgrade. In a clean temporary clone at `v0.3.0`, install `0.3.0`, run pull/health, and require source/projection identity plus `navigation` impact trust. Only after that clean-clone pull passes, run health, pull, refresh, registry-sync, and query against the generic temporary three-repository fleet from the acceptance suite; do not enroll any external consumer repository implicitly. Record release ID, asset ID, attestation result, C, and content-free health output; never include tokens or graph/source content.

If protected-main, OIDC, attestation, release permissions, or a credential required by the selected compatibility backend are unavailable, stop at this gate with the stable failure. A registry-admitted credentialless backend needs no synthetic secret. Do not weaken permissions, signer digest, attestation flags, branch protection, compatibility credential policy, or semantic extraction.

- [ ] **Step 12: Remove every non-main worktree and local/remote branch**

Before deletion, run `git worktree list --porcelain`, `git branch --format='%(refname:short)'`, and `git for-each-ref --format='%(refname:short)' refs/remotes/origin`. For every exact non-main branch discovered, run `git cherry main` with that resolved branch name as the final argument. If any `+` commit contains work not present in main, incorporate and re-run the A/B/C release sequence before cleanup; do not discard unreviewed work.

After main contains all required work, remove every non-main worktree by its exact absolute path recorded by `git worktree list --porcelain`, after verifying its `branch` record is the corresponding non-main ref; invoke `git worktree remove -- "$resolved_worktree_path"` once per validated path from the main checkout. Delete the merged local branches `atlasweaver-adoption`, `codex/atlasweaver-creds-defense`, `codex/atlasweaver-empty-source-path`, and `codex/atlasweaver-hardening` with `git branch -d`; when `git cherry` reports only patch-equivalent `-` commits but ancestry prevents `-d`, the user's explicit one-main requirement authorizes `git branch -D` after that evidence is recorded. Delete each known remote independently so an absent branch cannot block later cleanup:

```bash
for branch_name in atlasweaver-adoption codex/atlasweaver-creds-defense codex/atlasweaver-empty-source-path codex/atlasweaver-hardening; do
  if git ls-remote --exit-code --heads origin "$branch_name" >/dev/null; then
    git push origin --delete "$branch_name"
  fi
done
```

If branch discovery found any additional non-main branch, validate it with the same `git cherry` check and delete that exact already-resolved name as explicitly requested by the user.

Finish with:

```bash
git branch --format='%(refname:short)'
git for-each-ref --format='%(refname:short)' refs/remotes/origin
git worktree list --porcelain
git status --short --branch
git ls-remote --heads origin
```

Expected: the only local branch is `main`; remote-tracking refs contain only `origin/main` plus optional symbolic `origin/HEAD`; `git ls-remote --heads origin` contains only `refs/heads/main`; the only remaining AtlasWeaver worktree is the clean main checkout recorded in Step 11; `main`, `origin/main`, and tag `v0.3.0` point at C. Tags/releases remain because the requirement is one branch, not one ref.
