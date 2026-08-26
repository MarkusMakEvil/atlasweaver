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
- The rolling tag is `atlasweaver-graph-<project-uid>-<channel>` and the asset is `atlasweaver-graph-<project-uid>-<source-digest>-<projection-digest>.zip` with full lowercase SHA-256 digests.
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

def load_manifest(path: Path, repo_root: Path) -> ProjectManifest: ...
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

def inspect_projection(repo_root: Path, manifest: ProjectManifest) -> ProjectionSnapshot: ...
def stage_input(repo_root: Path, manifest: ProjectManifest, destination: Path) -> StagedInput: ...
```

`load_manifest_payload()` is an explicit amendment to Core Task 1 and must land in the same `manifest.py` commit: `load_manifest()` descriptor-captures/caps its file and delegates parsing to this byte-owned entry point. Fleet passes only bytes captured from the already opened repository descriptor, so no fleet code reopens a validated manifest pathname or reimplements strict YAML/schema validation.

```python
# project_knowledge.locking / lifecycle / health / doctor
def repository_lifecycle_lock(
    repo_root: Path, timeout: float = 5.0
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
) -> RefreshResult: ...
def inspect_project_state(
    repo_root: Path,
    manifest: ProjectManifest,
    *,
    atlas: FeatureHealth | None = None,
    registry: FeatureHealth | None = None,
    artifacts: FeatureHealth | None = None,
) -> KnowledgeState: ...
def assess_health(state: KnowledgeState) -> KnowledgeHealth: ...
def doctor_project(
    repo_root: Path,
    *,
    graphify_binary: Path = Path("graphify"),
    runner: CommandRunner | None = None,
    package_version: str | None = None,
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
def validate_candidate(
    candidate_dir: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    *,
    expected_projection_digest: str | None = None,
    build_epoch: int | None = None,
    git_identity: GitIdentity | None = None,
) -> ValidatedGraph: ...
def validate_owned_graph(
    root: Path,
    manifest: ProjectManifest,
    *,
    expected_source_digest: str | None = None,
    expected_projection_digest: str | None = None,
) -> ValidatedGraph: ...
def promote_graph(
    candidate: ValidatedGraph,
    repo_root: Path,
    fs: FileSystem = REAL_FS,
) -> PromotionResult: ...
```

Task 1 owns private `CapturedGeneration`, `_capture_validated_generation()`, and `_capture_owned_generation()`. The first descriptor-copies an already current-process `ValidatedGraph`'s closed approved generation plus ownership into a mode-0700 private directory with pre/open/post identity and double-digest checks, validates that private copy again with the same ownership-bound digests, and returns only immutable bytes/metadata. While the lifecycle lock is held, `pack_bundle()` obtains the current `ProjectionSnapshot`; `_capture_owned_generation()` validates the live generation against that snapshot's two expected digests and delegates to the capture primitive. It does not parse ownership as an independent authority and never adds a second public ownership API.

```python
# project_knowledge.queries / registry
def open_query_snapshot(
    repo_root: Path, manifest: ProjectManifest
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
    repo_root: Path, manifest: ProjectManifest, *, user_root: Path | None = None
) -> RegistryStatus: ...
def registry_sync(
    repo_root: Path,
    manifest: ProjectManifest,
    *,
    user_root: Path | None = None,
    runner: CommandRunner | None = None,
    graphify_binary: Path | None = None,
    fs=REAL_REGISTRY_FS,
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
    depth: int = 1
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

The registry stores each complete generation as `snapshots/<uid>/<graph_digest>/owned/` with its canonical captured manifest at sibling `manifest.yaml`; the internal registry binds that manifest SHA-256 and the Graphify projector receives only `owned/graph.json`. `capture_registry_snapshot()` takes the existing shared global lock without creating state, descriptor-captures every selected snapshot, parses the captured manifest, revalidates `owned/`, validates the internal manifest digest and Graphify compatibility projection, discards ownership/manifest bytes, and returns exact caller UID order. The aggregate graph/evidence cap is 256 MiB. Stable failures are `registry_snapshot_missing`, `registry_snapshot_stale`, `registry_snapshot_mismatch`, `registry_snapshot_too_large`, or `registry_snapshot_busy`; returned payloads have no live paths and are `repr=False`.

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
- Produces: `BundleError(code: str, message: str)`.
- Produces: `PayloadDescriptor(path: PurePosixPath, sha256: str, byte_length: int)`.
- Produces: `ArtifactManifest.from_bytes(payload: bytes) -> ArtifactManifest` and `ArtifactManifest.to_bytes() -> bytes`.
- Produces: `PackRequest(repo_root: Path, output: Path)` and `PackedBundle(path: Path, sha256: str, byte_length: int, artifact: ArtifactManifest)`.
- Produces: `pack_bundle(request: PackRequest) -> PackedBundle`.
- Produces private immutable `CapturedGeneration(manifest: ProjectManifest, validated: ValidatedGraph, payloads: tuple[CapturedPayload, ...])`, `_capture_validated_generation(validated: ValidatedGraph, manifest: ProjectManifest, destination: Path) -> CapturedGeneration`, and `_capture_owned_generation(repo_root: Path, manifest: ProjectManifest, projection: ProjectionSnapshot, destination: Path) -> CapturedGeneration`.
- Produces private `_pack_captured_generation(generation: CapturedGeneration, output: Path, *, version_provider: Callable[[], str] = installed_atlasweaver_version) -> PackedBundle`, shared with the privileged workflow but never exported through CLI.

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
    PurePosixPath("graphify-out/graph.json"),
    PurePosixPath("graphify-out/GRAPH_REPORT.md"),
    PurePosixPath("graphify-out/GRAPH_EVIDENCE.json"),
    PurePosixPath("graphify-out/graph.html"),
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

The canonical manifest dictionary has exactly these keys: `adapter_id`, `atlasweaver_version`, `build_epoch`, `git`, `graph_digest`, `graphify_version`, `payloads`, `project_id`, `project_uid`, `projection_digest`, `schema_version`, `source_digest`, and `transport`. `LocalTransport` serializes exactly `{"channel": null, "provider": "none"}`. `GithubTransport` serializes exactly `channel`, `host`, `provider`, `repository`, `repository_id`, and `source_ref`; signer settings remain trusted local configuration and are not copied into the bundle. Reject duplicate JSON keys, non-finite numbers, unknown keys, invalid UUID/digest/OID/version/channel/provider types, unordered or duplicate payload paths, and `graph.html` policy mismatches.

`pack_bundle()` must:

1. resolve the real repository/output parent without following symlinks, reject an output located beneath any safe include root, and refuse an existing output;
2. take the lifecycle lock;
3. call `inspect_projection`, validate the live owned generation against both current digests, descriptor-capture its ownership plus the closed approved artifacts into a mode-0700 temporary snapshot, validate the captured generation again, and call `inspect_projection` again;
4. require before/owned/after source and projection digests to be identical;
5. select evidence only from the immutable `CapturedGeneration.payloads` descriptor, read its descriptor-captured bytes, validate them through `parse_graph_evidence(payload, resolve_graphify_compatibility(manifest.graphify_version), expected_digest=evidence_capture.sha256)`, and require schema-v2 evidence even when trust is navigation; `evidence_capture.sha256` comes from the no-follow, double-digest generation capture, never from hashing caller bytes at the parser call site;
6. release the lock only after the copied snapshot validates as one generation;
7. call `_pack_captured_generation` to build exclusively from the snapshot, writing `artifact.json` first and approved payloads in `ALLOWED_PAYLOADS` order;
8. fsync an exclusive mode-0600 sibling temporary, atomically `linkat` that inode to the absent destination (or use a capability-probed `renameat2(RENAME_NOREPLACE)` equivalent), fsync the parent, unlink the temporary name, fsync again, and return its SHA-256/length.

The output publication must never call clobbering `os.rename`/`os.replace`. If a failure occurs after the no-replace link, unlink the destination only when its descriptor-bound device/inode/size/SHA-256 still match this transaction; otherwise return `bundle_output_recovery_required` without deleting another process's path. Derive `atlasweaver_version` from `importlib.metadata.version("atlasweaver")`; tests may inject a private metadata-version provider, but `PackRequest`, CLI, manifest, and environment cannot override it. Do not read `time`, `SOURCE_DATE_EPOCH`, locale, ZIP comments, extra fields, live graph paths, or the invocation environment when constructing bytes.

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
- Produces: `ParsedBundle(root: Path, artifact: ArtifactManifest, payloads: tuple[PayloadBinding, ...], archive_sha256: str, archive_size: int)` with `read_payload(path: PurePosixPath, max_bytes: int) -> bytes` that reopens through the bound directory descriptor and revalidates inode/digest/length.
- Produces: `parse_bundle(bundle: Path, destination: Path) -> ParsedBundle`.
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
    parsed = parse_bundle(valid_bundle, tmp_path / "private")
    assert sorted(path.name for path in parsed.root.iterdir()) == [
        "GRAPH_EVIDENCE.json", "GRAPH_REPORT.md", "graph.json"
    ]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in parsed.root.iterdir())


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
- Produces: `InstallResult(project_id: str, project_uid: str, graph_digest: str, changed: bool, status: Literal["installed", "already_current", "promoted_but_stale"])`.
- Produces: `install_local_bundle(repo_root: Path, bundle: Path) -> InstallResult`.
- Produces privately for Task 6: `_install_verified_pull(repo_root: Path, bundle: Path, authorization: _PullAuthorization) -> InstallResult`; `_PullAuthorization` cannot be accepted by any public CLI/parser API.

- [ ] **Step 1: Write failing end-to-end local install identity tests**

```python
def test_local_bundle_installs_through_candidate_validator_and_promotion(source_repo, clean_clone, tmp_path):
    bundle = pack_bundle(PackRequest(source_repo, tmp_path / "graph.zip"))
    result = install_local_bundle(clean_clone, bundle.path)
    assert result.status == "installed"
    assert result.project_uid == "4ed9af24-5aa2-4eac-8d0a-3f622cc74948"
    ownership = load_owned_generation(clean_clone / "graphify-out")
    assert ownership.build_epoch == 1_777_777_777
    assert ownership.source_digest == bundle.artifact.source_digest
    assert ownership.projection_digest == bundle.artifact.projection_digest


@pytest.mark.parametrize("field", [
    "project_id", "project_uid", "graphify_version", "adapter_id",
    "source_digest", "projection_digest", "graph_digest", "build_epoch",
])
def test_install_rejects_every_identity_mismatch_without_touching_live_graph(clean_clone, forged_bundle, field):
    before = tree_snapshot(clean_clone / "graphify-out")
    with pytest.raises(BundleError, match="bundle_identity_mismatch|bundle_stale"):
        install_local_bundle(clean_clone, forged_bundle(field))
    assert tree_snapshot(clean_clone / "graphify-out") == before


def test_public_install_rejects_github_bundle_even_with_copied_receipt(clean_clone, github_bundle):
    with pytest.raises(BundleError, match="bundle_pull_required"):
        install_local_bundle(clean_clone, github_bundle)
```

- [ ] **Step 2: Run and observe the missing install API**

Run: `uv run pytest -q tests/test_bundles_install.py`

Expected: import/collection fails for `install_local_bundle`.

- [ ] **Step 3: Implement validation and promotion under one lifecycle boundary**

```python
def install_local_bundle(repo_root: Path, bundle: Path) -> InstallResult:
    return _install_bundle(repo_root, bundle, authorization=None)


def _install_bundle(repo_root: Path, bundle: Path, authorization: _PullAuthorization | None) -> InstallResult:
    manifest = load_manifest(repo_root / ".graphify-project.yaml", repo_root)
    with repository_lifecycle_lock(repo_root):
        with TemporaryDirectory(prefix="atlasweaver-install-") as temporary:
            private = Path(temporary)
            before = stage_input(repo_root, manifest, private / "source-before")
            parsed = parse_bundle(bundle, private / "candidate")
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
                build_epoch=parsed.artifact.build_epoch,
                git_identity=parsed.artifact.git,
            )
            immediately_before = stage_input(
                repo_root, manifest, private / "source-immediately-before"
            )
            _require_same_validation_projection(before, immediately_before)
            promoted = promote_graph(validated, repo_root)
            try:
                after = stage_input(repo_root, manifest, private / "source-after")
                current = _same_validation_projection(before, after)
            except StagingError:
                current = False
            if not current:
                return InstallResult(
                    project_id=manifest.project_id,
                    project_uid=str(manifest.project_uid),
                    graph_digest=validated.graph_digest,
                    changed=promoted.changed,
                    status="promoted_but_stale",
                )
            return InstallResult(
                project_id=manifest.project_id,
                project_uid=str(manifest.project_uid),
                graph_digest=validated.graph_digest,
                changed=promoted.changed,
                status="installed" if promoted.changed else "already_current",
            )
```

`_same_validation_projection()` compares the complete immutable validator input contract: `source_digest`, `projection_digest`, sorted safe paths, `projection_files`, `reason_counts`, and `coverage_approvals`. The before/immediately-before/after stages therefore use the same canonical privacy/coverage/scanner walk as `validate_candidate`; a lightweight `ProjectionSnapshot` is never passed where a `StagedInput` is required. The installer rejects a null schema-v2 projection digest, any bundled ownership file before mapping, regenerates ownership only through `validate_candidate`, preserves the immutable bundle `build_epoch`, validates current evidence/trust without promoting trust, and leaves the prior graph untouched on every pre-promotion failure. `promoted_but_stale` is nonzero at the CLI layer and does not claim rollback.

Same-process GitHub authorization uses a module-private identity registry:

```python
@dataclass(frozen=True)
class _PullAuthorization:
    nonce: str

_AUTHORIZED_PULLS: dict[int, tuple[_PullAuthorization, str, int, int]] = {}
_AUTHORIZED_PULLS_LOCK = threading.Lock()
```

The stored tuple binds the exact authorization object identity, archive SHA-256, archive byte length, and immutable GitHub asset ID. Registration and consumption hold `_AUTHORIZED_PULLS_LOCK`; consumption atomically pops the entry before validation/promotion, so exactly one of two concurrent consumers can proceed. `_install_verified_pull` rejects a copied/reconstructed object or mutated bundle and leaves no registry entry whether install succeeds or fails.

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
    manifest = load_manifest(clean_clone / ".graphify-project.yaml", clean_clone)
    assert assess_health(inspect_project_state(clean_clone, manifest)).core_status == "stale"


def test_verified_pull_authorization_is_atomic_single_use(clean_clone, verified_download):
    results = run_two_threads(
        lambda: _install_verified_pull(
            clean_clone, verified_download.path, verified_download.authorization
        )
    )
    assert sum(isinstance(value, InstallResult) for value in results) == 1
    assert sum(isinstance(value, BundleError) and value.code == "bundle_unattested"
               for value in results) == 1
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
- Produces: `SuccessfulOperation(operation: str, duration_ms: int, safe_file_count: int, coverage: CoverageMetrics, source_digest: str, projection_digest: str, graph_digest: str | None, artifact_channel: str | None)`.
- Produces: `FailureRecord(operation: str, failure_code: str)`.
- Produces: `OperationState(schema_version: int, last_success: SuccessfulOperation | None, last_failure: FailureRecord | None)`.
- Produces: `record_success(repo_root: Path, operation: SuccessfulOperation) -> None`, `record_failure(repo_root: Path, failure: FailureRecord) -> None`, `load_operation_state(repo_root: Path) -> OperationState`, and `render_ci_summary(results: Sequence[dict[str, object]]) -> tuple[bytes, str]`.
- Produces internal `python -m project_knowledge.operation_state ci-summary --output-json PATH --preflight PATH --doctor PATH --scan PATH --health PATH` for workflow-only content-free rendering.

- [ ] **Step 1: Write failing state schema, redaction, and atomicity tests**

```python
def test_state_contains_only_content_free_closed_schema(repo):
    record_success(repo, SuccessfulOperation(
        operation="artifact_install", duration_ms=125, safe_file_count=7,
        coverage=CoverageMetrics(represented=6, approved_omissions=1, denied=3),
        source_digest="1" * 64, projection_digest="2" * 64,
        graph_digest="3" * 64, artifact_channel=None,
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

State JSON uses canonical sorted keys, schema version 1, integer non-negative counts/duration, lowercase 64-hex digests, operation names from an immutable allowlist, and stable failure codes from the CLI error registry. Every read/update takes `.project-knowledge/state.lock`; lock order is repository lifecycle lock then state lock. Open the real mode-0700 state directory once with `O_DIRECTORY|O_NOFOLLOW`, read existing state via `openat(O_NOFOLLOW)` with pre/open/post inode binding, write a mode-0600 sibling temp with `O_EXCL|O_NOFOLLOW`, fsync it, verify the destination still has the captured binding, `renameat` within the opened parent, then fsync the directory. Reject symlink/wrong-type/swap/duplicate-key/non-finite/unknown-field state and serialize concurrent pack/install updates so the last completed operation wins without a lost update.

`render_ci_summary()` returns canonical machine JSON and Markdown containing only project ID/UID, operation, status, counts, duration, digests, channel, trust, and stable limitations. It rejects values under keys matching `path`, `file`, `query`, `environment`, `fingerprint`, or `message` rather than trying to redact arbitrary content.

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
- Produces: `HttpsRequest(method: Literal["GET"], url: str, headers: tuple[tuple[str, str], ...], timeout_seconds: float)`.
- Produces protocol: `HttpsTransport.open(request: HttpsRequest) -> HttpsResponse`, where `HttpsResponse.status`, `headers`, `read(size)`, and `close()` are bounded/injectable.
- Produces: `ReleaseAssetIdentity(repository_id: int, release_id: int, tag: str, asset_id: int, asset_name: str, asset_size: int, asset_digest: str, source_commit_oid: str)`.
- Produces: `DownloadReceipt(identity: ReleaseAssetIdentity, archive_sha256: str, archive_size: int, artifact_git_commit_oid: str)`.
- Produces: `GithubCredentials(token: str = field(repr=False))`.
- Produces: `resolve_and_download(config: ArtifactIntent, project_uid: UUID, projection: ProjectionSnapshot, destination: Path, credentials: GithubCredentials, transport: HttpsTransport = REAL_HTTPS) -> DownloadReceipt`.

- [ ] **Step 1: Write fake-transport tests for exact API construction and immutable identity**

```python
def test_resolver_binds_repository_release_asset_and_commit(github_config, fake_https, tmp_path):
    fake_https.queue_json("https://api.github.com/repos/acme/widgets", {
        "id": 123456789, "full_name": "acme/widgets"
    })
    fake_https.queue_json(
        "https://api.github.com/repos/acme/widgets/releases/tags/atlasweaver-graph-4ed9af24-5aa2-4eac-8d0a-3f622cc74948-main",
        release_response(release_id=44, asset_id=55, digest="sha256:" + "a" * 64),
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
        github_bundle_bytes(git_oid="b" * 40),
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
                    + "1" * 64 + "-" + "2" * 64 + ".zip"),
        asset_size=len(github_bundle_bytes(git_oid="b" * 40)),
        asset_digest="a" * 64, source_commit_oid="b" * 40,
    )
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

Read each JSON response with a 1 MiB cap, strict duplicate-key/non-finite JSON, and a closed projection of required fields. Resolve repository identity, rolling-tag release, and exactly one exact-name asset. After bounded structural parsing of downloaded `artifact.json`, resolve `/repos/<owner>/<repo>/commits/<claimed_git_commit_oid>` and require the exact returned SHA/repository; do not derive the asset's source commit from the rolling release `target_commitish`, because retained digest-addressed assets remain valid after the rolling branch advances. Require numeric IDs to be positive non-boolean integers and every returned name/digest/size to match configuration/current projection.

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
- Consumes: `resolve_and_download`, `DownloadReceipt`, private `_PullAuthorization`, and `_install_verified_pull`.
- Produces protocol: `GhCommandRunner.run(argv: tuple[str, ...], env: Mapping[str, str], timeout_seconds: float, output_limit: int) -> CompletedCommand`; this transport runner is distinct from Core's Graphify `CommandRunner`.
- Produces: `VerifiedAttestation(subject_sha256: str, signer_workflow: str, signer_digest: str, source_ref: str, source_digest: str, predicate_type: str)`.
- Produces: `ResolvedGhExecutable(path: Path, device: int, inode: int, sha256: str, version: str)` and narrow protocol `GhToolResolver.resolve() -> ResolvedGhExecutable`.
- Produces private `SYSTEM_GH_RESOLVER` and `resolve_gh_executable(resolver: GhToolResolver = SYSTEM_GH_RESOLVER) -> ResolvedGhExecutable`; neither can resolve Graphify and neither is exposed through CLI/workflow input.
- Produces: `verify_attestation(bundle: Path, receipt: DownloadReceipt, config: ArtifactIntent, credentials: GithubCredentials, gh: ResolvedGhExecutable, runner: GhCommandRunner = SUBPROCESS_GH_RUNNER) -> VerifiedAttestation`.
- Produces: `PullDependencies(transport: HttpsTransport, runner: GhCommandRunner, gh_resolver: GhToolResolver)` for test injection only.
- Produces: `PullResult(download: DownloadReceipt, install: InstallResult)` and `pull_bundle(repo_root: Path, credentials: GithubCredentials, *, dependencies: PullDependencies = REAL_PULL_DEPENDENCIES) -> PullResult`.

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
```

The test runner must also assert a 60-second timeout, a 256 KiB combined stdout/stderr cap, a mode-0700 empty `GH_CONFIG_DIR`, and deletion of that directory after success/failure.

- [ ] **Step 2: Add malformed/ambiguous provenance and pull race tests**

```python
@pytest.mark.parametrize("case", [
    "missing_result", "two_matching_results", "wrong_subject", "wrong_repository",
    "wrong_workflow", "wrong_signer_digest", "wrong_source_ref", "wrong_source_commit",
    "wrong_predicate", "self_hosted", "duplicate_json_key", "nonfinite_json",
    "oversized_output", "timeout", "nonzero_exit",
])
def test_attestation_failure_is_redacted_and_non_authorizing(
    tmp_path, receipt, github_config, fake_runner, resolved_gh, case
):
    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"untrusted bundle bytes")
    fake_runner.result = malformed_gh_result(case)
    with pytest.raises(GithubArtifactError, match="attestation_"):
        verify_attestation(
            bundle, receipt, github_config, GithubCredentials("token-value"),
            resolved_gh, fake_runner
        )
    assert bundle.read_bytes() == original


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


def test_verified_authorization_is_single_use(repo, verified_download):
    first = _install_verified_pull(repo, verified_download.path, verified_download.authorization)
    assert first.status in {"installed", "already_current"}
    with pytest.raises(BundleError, match="bundle_unattested"):
        _install_verified_pull(repo, verified_download.path, verified_download.authorization)
```

- [ ] **Step 3: Run focused tests and confirm attestation APIs are missing**

Run: `uv run pytest -q tests/test_github_artifacts.py -k 'attestation or pull or authorization'`

Expected: FAIL for missing `verify_attestation`/`pull_bundle`.

- [ ] **Step 4: Implement capability-checked verification and exact result projection**

Resolve `gh` exactly once through Task 6's private, gh-only `SYSTEM_GH_RESOLVER`; no public CLI/request/workspace/workflow input can name an alternate executable. It returns an absolute no-follow regular path, captures device/inode/SHA-256, and capability-checks `--version` plus `attestation verify --help` through the bounded process-group runner. Revalidate the gh executable binding immediately before exec and invoke only its absolute path under the minimal environment. This boundary has no `graphify` name, branch, type, or return value; Graphify authority remains exclusively in Impact/Core. Require the exact attestation flags listed in the spec. Use no shell. Construct only the exact argv shown in Step 1. Parse stdout with duplicate-key rejection and accept exactly one result whose authenticated certificate/verification projection contains the ZIP subject SHA-256, configured workflow identity and digest, configured source ref, receipt/bundle source commit, SLSA provenance predicate type, and hosted-runner policy. Ignore free-form statement predicate fields when making authority decisions.

Require all four identity sources to agree: immutable repository ID/name from the TLS GitHub API receipt, local manifest config, untrusted bounded `artifact.json` claims, and `gh` fields explicitly enforced by flags. `verify_attestation` never creates `_PullAuthorization`; only `pull_bundle`, after all comparisons succeed, registers one authorization bound to the exact file/receipt.

`pull_bundle()` sequence is exact:

```python
def pull_bundle(repo_root: Path, credentials: GithubCredentials, *,
                dependencies: PullDependencies = REAL_PULL_DEPENDENCIES) -> PullResult:
    manifest = load_manifest(repo_root / ".graphify-project.yaml", repo_root)
    config = require_github_release_provider(manifest.artifacts)
    projection = inspect_projection(repo_root, manifest)
    if manifest.project_uid is None or projection.projection_digest is None:
        raise GithubArtifactError("manifest_migration_required", "portable identity is required")
    with TemporaryDirectory(prefix="atlasweaver-pull-") as temporary:
        bundle = Path(temporary) / "bundle.zip"
        receipt = resolve_and_download(
            config, manifest.project_uid, projection, bundle,
            credentials, dependencies.transport,
        )
        gh = dependencies.gh_resolver.resolve()
        verified = verify_attestation(
            bundle, receipt, config, credentials, gh, dependencies.runner
        )
        authorization = _register_pull_authorization(bundle, receipt, verified, manifest)
        installed = _install_verified_pull(repo_root, bundle, authorization)
        return PullResult(receipt, installed)
```

`inspect_projection` before the network is advisory for exact asset resolution; `_install_verified_pull` creates the authoritative private `StagedInput` inside the lifecycle lock. `GithubCredentials` and every secret-bearing fleet/refresh request field use `field(repr=False)`. Never put the token in argv, repr, output, operation state, exception text, or a Graphify environment. Record `artifact_pull` content-free state only after the install outcome is known.

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
- Produces: `WorkflowContext(repository: str, repository_id: int, ref: str, ref_type: str, ref_protected: bool, sha: str, run_id: int)` containing caller/source identity only; it never represents called-workflow signer identity.
- Produces: `prepare_publication(repo_root: Path, build_bundle: Path, output: Path, context: WorkflowContext) -> PackedBundle`.
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
```

The tracked-clean check must compare Git index/HEAD plus the safe projection: ignored private state and `graphify-out` do not make the checkout dirty, while every safe corpus byte must be represented by `context.sha`.

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
```

Add idempotency cases: an exact-name asset with matching size/digest is reused; an exact name with mismatched bytes fails; a retry after upload but before tag move completes safely; retention sorts digest-addressed assets by `(created_at, id)` newest-first and never deletes unrelated assets.

- [ ] **Step 3: Run and observe the missing workflow module**

Run: `uv run pytest -q tests/test_workflow_publish.py`

Expected: collection fails for missing `project_knowledge.workflow_publish`.

- [ ] **Step 4: Implement internal publication preparation**

`prepare_publication()` must parse caller/source `WorkflowContext` only from a strict allowlist of GitHub environment names, require all fields, validate full SHA/repository ID/ref types, validate the manifest signer path grammar, check the exact Git object format/OID and tracked safe projection, and reject any consumer-supplied shell/config override. It must not compare signer digest to normal `github.workflow_sha`; called-workflow identity becomes authoritative only through the post-attestation verification step. Its lifecycle-locked core is exact:

```python
with repository_lifecycle_lock(repo_root):
    with TemporaryDirectory(prefix="atlasweaver-publish-") as temporary:
        private = Path(temporary)
        staged = stage_input(repo_root, manifest, private / "source")
        parsed = parse_bundle(build_bundle, private / "candidate")
        _require_publication_identity(parsed.artifact, manifest, staged, context)
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
            build_epoch=parsed.artifact.build_epoch,
            git_identity=git_identity_for_oid(context.sha),
        )
        immediately_before = stage_input(repo_root, manifest, private / "source-check")
        _require_same_validation_projection(staged, immediately_before)
        captured = _capture_validated_generation(
            validated, manifest, private / "captured-generation"
        )
        prepared = _pack_captured_generation(captured, output)
        after = stage_input(repo_root, manifest, private / "source-after")
        _require_same_validation_projection(staged, after)
        return prepared
```

Require schema v2/non-null projection. The unprivileged build bundle may carry `git: null`, because normal Core refresh creates schema-2 ownership with `git_identity=None`; if it carries a non-null Git identity, require it to equal the authenticated workflow context SHA/object format or fail `workflow_source_mismatch`. Authority comes only from the privileged checkout/context: pass `git_identity_for_oid(context.sha)` to `validate_candidate`, require the returned `ValidatedGraph.git_identity` to equal it, and require the final prepared bundle's non-null Git identity to equal it before attestation. This validates/re-owns a private candidate and packs directly from its descriptor-captured ownership generation; it does not require or promote a live `graphify-out` in the privileged checkout. Task 6 still requires the attestation source digest, download receipt source commit, final bundle Git identity, and manifest source ref to agree. It must never trust the build job's health JSON, booleans, path, backend output, archive digest, or consumer `pyproject.toml`.

The internal module accepts only fixed `prepare`, `verify-attestation`, and `upload` verbs when invoked with `python -m`; each requires `GITHUB_ACTIONS=true`, a complete `WorkflowContext`, and exact file paths created by the workflow under `$RUNNER_TEMP`. `verify-attestation` reads signer workflow/digest only from the strict project manifest and applies Task 6's exact verifier after the attestation action; it has no signer override flag. This is defense in depth, not a public authorization claim; the absence of a public entry point/subcommand is the product boundary.

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

Resolve an explicit `--home` as the real user-home directory or create its absent final directory with mode 0700; the CLI default is `Path.home()`. Resolve the compatibility contract and select its one `AgentInstallContract` for the requested platform. The contract's `home_relative_skill` must be `.codex/skills/graphify/SKILL.md` for `codex` or `.agents/skills/graphify/SKILL.md` for `agents`; derive AtlasWeaver's separate managed destination as the corresponding platform root plus `skills/using-project-knowledge-graphs`. Before calling Graphify, validate any existing Atlas destination and require a valid marker plus exact current tree digest. Call the injected production alias of Core's `resolve_graphify_executable()` once, then run compatibility-owned `probe_graphify(resolved, contract, runner)`; the probe's version/help/seven-operation smoke uses its own private temporary `HOME` and cannot touch the requested user home. No artifact-layer Graphify resolver, identity type, or PATH lookup exists. Render only `render_graphify_agent_install(contract, binary=resolved.path, platform=request.platform).argv` and invoke it through Core's `run_graphify_operation(runner, resolved, rendered.argv, ...)`, which no-follow revalidates the same device/inode/launcher digest immediately before spawn. The final install receives only `HOME=<user-home>`, `LANG=C.UTF-8`, and `LC_ALL=C.UTF-8`; no public argv can replace the executable and no local argv template, passthrough, `--project`, or `--strict` flag exists. Then copy package resources through `importlib.resources.as_file()` into a sibling mode-0700 stage, reject symlinks/special files, write the canonical mode-0600 marker, fsync, back up an existing owned destination, replace, fsync parent, and delete backup. Restore the backup on every caught replace failure.

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
- Consumes: strict project `load_manifest_payload(payload, repo_root)` and v2 `ProjectManifest.project_uid`.
- Produces: `FleetProject(id: str, repository: PurePosixPath, root: Path, project_uid: UUID)`.
- Produces: `FleetWorkspace(schema_version: int, root: Path, projects: tuple[FleetProject, ...], max_parallel: int)`.
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


def test_fleet_rejects_workspace_or_manifest_over_256_kib(tmp_path):
    with pytest.raises(FleetConfigError, match="fleet_document_too_large"):
        load_fleet_workspace(write_oversized_workspace(tmp_path, 262_145))
    with pytest.raises(FleetConfigError, match="fleet_manifest_too_large"):
        load_fleet_workspace(workspace_with_oversized_manifest(tmp_path, 262_145))
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

Open the workspace parent/file and each repository path component with no-follow descriptors. Cap the workspace at 262,144 bytes, descriptor-read it with pre/open/post binding plus a second digest pass, and parse only those captured bytes. Require repository paths unique both lexically and by resolved path, beneath the real fleet root, non-symlink directories, and not nested. From the already opened repository descriptor, open `.graphify-project.yaml` with `openat(O_NOFOLLOW)`, cap it at 262,144 bytes, descriptor-capture it with the same binding/double-digest checks, and call the core `load_manifest_payload(captured_bytes, repo_root)`; never reopen the manifest pathname. Require `manifest.project_id == entry.id`, schema v2, and non-null UUIDv4 `project_uid`, then reject duplicate UIDs.

Define `_git_alias_key(repo)` without invoking Git: a real `.git` directory resolves to its canonical path; a regular `.git` file must contain one bounded UTF-8 `gitdir: <path>` line. If that target matches `<common>/.git/worktrees/<name>`, return the canonical `<common>/.git`; otherwise return the canonical gitdir. Reject symlink/malformed/inaccessible markers. Two equal non-null keys are `fleet_worktree_alias`.

Selection accepts repeated IDs, rejects unknown or duplicate selectors, and returns the original workspace order—not selector order.

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
- Consumes: core `doctor_project`, `inspect_project_state`/`assess_health`, `RefreshOptions`/`refresh_project`, `registry_status`/`registry_sync`, `capture_registry_snapshot`/`query_registry`; compatibility `resolve_graphify_compatibility`/`bind_semantic_backend_credential`; artifact `pull_bundle`; and Task 9 workspace selection.
- Produces: `FleetOperation = Literal["doctor", "health", "pull", "refresh", "registry-sync", "query"]`.
- Produces: `FleetProjectResult(id: str, uid: str, status: str, result: dict[str, object] | None, error_code: str | None, duration_ms: int)`.
- Produces: `FleetResult(operation: FleetOperation, status: Literal["ok", "partial_failure", "failed"], projects: tuple[FleetProjectResult, ...], result: dict[str, object] | None)`; `result` is used only for aggregate fleet query output.
- Produces: `ProjectBackendEnvironment(project_uid: UUID, values: Mapping[str, str] = field(repr=False))` and `RefreshFleetRequest(backend, model, deep, code_only, project_environments: tuple[ProjectBackendEnvironment, ...] = field(repr=False))`.
- Produces: `run_fleet_operation(workspace: FleetWorkspace, operation: FleetOperation, selected: tuple[FleetProject, ...], request: FleetOperationRequest, runner: FleetProjectRunner = DEFAULT_FLEET_RUNNER) -> FleetResult`.

- [ ] **Step 1: Write bounded parallelism, isolation, and ordering tests**

```python
def test_parallel_fleet_operation_is_bounded_and_returns_configuration_order(workspace, blocking_runner):
    result = run_fleet_operation(
        workspace, "health", workspace.projects,
        FleetOperationRequest(), blocking_runner,
    )
    assert blocking_runner.max_active == workspace.max_parallel == 2
    assert [project.id for project in result.projects] == ["api", "documentation", "worker"]


def test_partial_failure_does_not_cancel_or_rollback_other_repositories(workspace, recording_runner):
    recording_runner.fail("documentation", code="graph_missing")
    request = RefreshFleetRequest(
        backend="openai", model="gpt-5-mini", deep=True,
        code_only=False,
        project_environments=(
            ProjectBackendEnvironment(API_UID, {
                **minimal_environment(), "OPENAI_API_KEY": "fixture-secret",
            }),
            ProjectBackendEnvironment(DOCS_UID, {
                **minimal_environment(), "OPENAI_API_KEY": "fixture-secret",
            }),
            ProjectBackendEnvironment(WORKER_UID, {
                **minimal_environment(), "OPENAI_API_KEY": "fixture-secret",
            }),
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
```

The CLI constructs one `ProjectBackendEnvironment` per selected project by resolving its compatibility contract and selected `BackendContract`, then taking the credential from `ATLASWEAVER_BACKEND_TOKEN` when that trusted-wrapper bridge is present or otherwise from that backend's one `canonical_credential_environment`. It starts from Core's `minimal_environment()` (exactly `HOME`, `LANG`, `LC_ALL`, and `PATH`), calls `bind_semantic_backend_credential(contract, backend, credential)`, and merges only that returned one-key mapping. For `openai` the only additional name is `OPENAI_API_KEY`; the other exact registry mappings are `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, `MOONSHOT_API_KEY`, and `OLLAMA_API_KEY`. A missing credential is accepted only when the selected compatibility backend declares credentialless operation, in which case only the four base names remain. `RefreshFleetRequest` contains no generic token field, executable override, arbitrary argv, output path, provider override, or allow-dirty field.

- [ ] **Step 2: Write serialized registry and atomic fleet-query tests**

```python
def test_registry_sync_is_globally_serialized_in_workspace_order(workspace, recording_runner):
    result = run_fleet_operation(workspace, "registry-sync", workspace.projects, FleetOperationRequest(), recording_runner)
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

Use `ThreadPoolExecutor(max_workers=workspace.max_parallel)` only for doctor, health, pull, and refresh. Submit in configuration order, retain a future-to-index map, catch only the public operation exception families, convert them to stable path-free `FleetProjectResult`, and assemble results by index. Do not stop already submitted independent work after one failure. Never share manifest, lifecycle lock, credential environment, temporary directory, or mutation state between projects. `DEFAULT_FLEET_RUNNER` delegates exactly: doctor to `doctor_project`; health to `assess_health(inspect_project_state(...))`; refresh to `refresh_project(..., RefreshOptions(...), ambient=project_environment.values)`; pull to `pull_bundle`; and registry sync/status to their core APIs. It passes neither test-only executable nor runner overrides.

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

`fleet refresh` adds only the same validated core refresh flags (`--backend`, `--model`, `--deep`, `--code-only`); it does not accept executable overrides, arbitrary native flags, output paths, provider identity, or `--allow-dirty`. `fleet query` has one required nested operation with the core caps: `query TERM [--limit 1..100]`, `path SOURCE TARGET [--max-depth 1..32]`, `explain NODE [--depth 0..8]`, or `affected NODE [--depth 0..8] [--relation RELATION]` with at most 16 relations. It builds `RegistryQueryRequest`, never an opaque shell string. `GITHUB_TOKEN` and `ATLASWEAVER_BACKEND_TOKEN` are environment-only and never accepted as argv values; no command exposes `--gh-binary` or `--graphify-binary`.

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
    assert json.loads(result.stdout)["code"] == "github_token_required"
    assert result.stderr == ""
```

Also assert doctor/health/fleet-health/query before/after tree equality; pack creates only the explicit output plus mutation state; install/pull/refresh mutate only their selected repository; agent commands mutate only the selected platform home; unknown selection prevents all fleet work.

- [ ] **Step 3: Run parser/CLI tests and confirm missing command failures**

Run: `uv run pytest -q tests/test_artifact_fleet_cli.py tests/test_cli.py`

Expected: parser surface and command invocations fail because the new commands are not registered.

- [ ] **Step 4: Refactor parser construction into nested explicit handlers**

Register nested `argparse` subparsers and dispatch to typed handler functions; do not inspect raw argv after parsing. Normalize UUIDs to canonical strings only at JSON serialization. Add stable error mappings:

```python
ERROR_EXIT_CODES = {
    "bundle_invalid": 1, "bundle_too_large": 1, "bundle_stale": 1,
    "bundle_identity_mismatch": 1, "bundle_pull_required": 1,
    "bundle_unattested": 1, "github_token_required": 1,
    "github_resolution_failed": 1, "attestation_failed": 1,
    "agent_destination_unmanaged": 1, "agent_destination_modified": 1,
    "fleet_invalid": 1, "fleet_partial_failure": 1,
    "promoted_but_stale": 2,
}
```

All errors emit `{"schema_version":1,"command":"artifact install","status":"error","code":"bundle_invalid"}` with the actual command/code substituted from fixed enums and no raw exception message. `pull` copies `GITHUB_TOKEN` into a local variable, immediately removes it from any child/base environment map, and passes it explicitly only to resolver/verifier. For fleet refresh, validate workspace and selection first, prefer the trusted-wrapper `ATLASWEAVER_BACKEND_TOKEN` when set, otherwise read only each selected backend's registry-declared canonical credential name, call `minimal_environment()` and `bind_semantic_backend_credential()` separately for every selected project's compatibility contract, merge exactly the four base entries plus at most that one canonical credential, construct the exact `ProjectBackendEnvironment` tuple, then discard all source credential references before dispatch. An unselected project never receives a binding and non-refresh commands never read these variables. Agent default homes are platform-adapter values. Fleet selection is validated before any credential is read or any runner is invoked.

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
- Create: `tests/test_workflows.py`
- Modify: `.github/workflows/ci.yml`
- Test: `tests/test_public_release.py`

**Interfaces:**
- Consumes: trusted Core `preflight` with exact `--backend`/`--model`/`--deep` flags, public `doctor`, `scan-secrets`, and `health` JSON, plus `render_ci_summary`.
- Produces reusable workflow inputs `repo-root: string`, `python-version: string`, `backend: string`, `model: string`, `deep-mode: boolean`, and `require-impact-trust: boolean`.
- Produces one optional named secret `semantic_backend_token`.
- Produces internal renderer invocation `python -m project_knowledge.operation_state ci-summary --output-json PATH --preflight PATH --doctor PATH --scan PATH --health PATH`; it writes only under `$RUNNER_TEMP` and `$GITHUB_STEP_SUMMARY`.

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


def test_check_never_executes_consumer_commands_or_uploads_graph_content():
    text = (WORKFLOWS / "atlasweaver-check.yml").read_text(encoding="utf-8")
    assert "test-command" not in text
    assert "shell-command" not in text
    assert "graphify-out" not in upload_artifact_paths(load_workflow("atlasweaver-check.yml"))
    assert all(term in text for term in (
        "project-knowledge preflight", "project-knowledge doctor", "project-knowledge scan-secrets",
        "project-knowledge health", "GITHUB_STEP_SUMMARY",
    ))


def test_check_uses_backend_inputs_only_in_read_only_preflight_and_scopes_secret():
    workflow = load_workflow("atlasweaver-check.yml")
    preflight = step_named(workflow, "Semantic preflight")
    code_only = step_named(workflow, "Code-only preflight")
    assert preflight["if"] == "${{ inputs.backend != '' }}"
    assert code_only["if"] == "${{ inputs.backend == '' }}"
    assert "ATLASWEAVER_BACKEND_TOKEN" not in json.dumps(code_only, sort_keys=True)
    assert set(preflight["env"]) == {
        "ATLASWEAVER_BACKEND", "ATLASWEAVER_BACKEND_TOKEN",
        "ATLASWEAVER_DEEP", "ATLASWEAVER_MODEL", "ATLASWEAVER_REPO_ROOT",
    }
    assert preflight["env"]["ATLASWEAVER_BACKEND_TOKEN"] == (
        "${{ secrets.semantic_backend_token }}"
    )
    assert "--backend" in preflight["run"]
    assert "--model" in preflight["run"]
    assert "--deep" in preflight["run"]
    for step in all_steps_except(workflow, "Semantic preflight"):
        assert "ATLASWEAVER_BACKEND_TOKEN" not in json.dumps(step, sort_keys=True)
    assert "semantic_backend_token" not in json.dumps(
        summary_step(workflow), sort_keys=True
    )


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

Follow those fixed setup steps with: `uv sync --project "$GITHUB_WORKSPACE/atlasweaver-tool" --frozen`; `uv tool install 'graphifyy==0.9.48'`; root confinement through the trusted AtlasWeaver checkout; then invoke every Python/CLI command with `uv run --project "$GITHUB_WORKSPACE/atlasweaver-tool"`. Never build/install/run from the consumer checkout or its `pyproject.toml`. Two mutually exclusive read-only steps write the same bounded preflight JSON path: `Code-only preflight` runs when `inputs.backend == ''` and receives no secret/backend/model/deep environment, while `Semantic preflight` runs when `inputs.backend != ''`. Then run doctor, scan, and health into the other three `$RUNNER_TEMP` JSON files. Pass `repo-root`, backend, model, and deep mode through environment variables and quote every expansion—never interpolate `${{ inputs.* }}` directly into shell source. The semantic-preflight step alone receives `semantic_backend_token` as `ATLASWEAVER_BACKEND_TOKEN`; Core reads it once, calls `bind_semantic_backend_credential()`, validates the registry-rendered semantic contract, removes the generic name, and emits only `credential_bound: bool`. Doctor, scan, health, renderer, summaries, and every unselected project receive neither the generic secret nor the canonical backend environment.

Fail when secret scan has unaccepted findings, core health is not admitted, or `require-impact-trust` is true and trust is not `trusted`. Always render a content-free summary in a final `if: always()` step, append Markdown to `$GITHUB_STEP_SUMMARY`, and upload only `atlasweaver-check-summary.json` with retention 7 days through the pinned upload action. Never upload the graph, report, evidence, stage, receipt, or command logs.

- [ ] **Step 4: Pin the existing base CI and extend its artifact/fleet gates**

Replace mutable checkout/setup pins in `.github/workflows/ci.yml` with the exact SHAs above, use pinned setup-uv `0.8.14`, preserve Python `3.10` and `3.13`, install exact Graphify `0.9.48`, and add focused invocations for `tests/test_bundles_pack.py`, `tests/test_bundles_parse.py`, `tests/test_agent_install.py`, `tests/test_fleet.py`, and `tests/test_workflows.py` before the full suite/build. No CI job gains write permission.

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
git add .github/workflows/atlasweaver-check.yml .github/workflows/ci.yml tests/test_workflows.py tests/test_operation_state.py tests/test_public_release.py
git commit -m "ci: add reusable AtlasWeaver validation workflow"
```

### Task 13: Reusable split-privilege publish workflow

**Files:**
- Create: `.github/workflows/atlasweaver-publish.yml`
- Modify: `tests/test_workflows.py`
- Modify: `tests/test_workflow_publish.py`
- Test: `tests/test_public_release.py`

**Interfaces:**
- Consumes: public `doctor`, `scan-secrets`, `refresh`, `health`, `artifact pack`; internal `workflow_publish prepare|verify-attestation|upload`; pinned attestation action.
- Produces the same closed inputs/secret as Task 12.
- Produces inspect outputs `project_uid` and `channel`, used only for concurrency naming and revalidated in the privileged job.

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
    assert refresh["env"]["ATLASWEAVER_BACKEND_TOKEN"] == (
        "${{ secrets.semantic_backend_token }}"
    )
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
```

Also assert: workflow is `workflow_call` only; no pull-request publication; inputs contain no shell/test/native flags/output/provider/project/signer/source-ref/channel fields; checkout credentials are not persisted; build artifact has fixed name/path/retention; publish downloads only that artifact; caller boolean outputs are never used as authorization.

- [ ] **Step 2: Run and observe the missing workflow**

Run: `uv run pytest -q tests/test_workflows.py -k publish`

Expected: FAIL because `atlasweaver-publish.yml` does not exist.

- [ ] **Step 3: Implement inspect and unprivileged build jobs**

`inspect` checks out consumer and trusted AtlasWeaver source exactly as Task 12, runs the trusted strict manifest/context inspector, and writes only canonical UUID/channel outputs through `$GITHUB_OUTPUT`. It validates but does not authorize publication.

`build` depends on inspect, has `contents: read`, checks out exact caller `${{ github.sha }}` with credentials disabled, and checks out trusted AtlasWeaver at the same provisional implementation pin used by Task 12; Task 14 atomically replaces both workflow constants with final release tool commit A. The normal `github` context deliberately supplies caller repository/ref/SHA; it is never used as trusted-tool identity. Every trusted command runs with `uv run --project "$GITHUB_WORKSPACE/atlasweaver-tool"`. The job installs pinned Graphify. Only its refresh step receives `semantic_backend_token` as `ATLASWEAVER_BACKEND_TOKEN`; the trusted Core refresh handler binds it to the compatibility-declared canonical environment and removes the generic name before Graphify execution. Doctor, scan, pack, health, summary, and publish receive no credential. It runs doctor, secret scan, the approved `refresh`, `artifact pack`, and a second health check. It fails if semantic content lacks a backend secret and never silently adds `--code-only`. It uploads one workflow artifact named `atlasweaver-build-${{ github.run_id }}` containing only `bundle.zip`, with retention 1 day. Pull-request callers still cannot publish because the privileged job's context gate requires a protected branch.

- [ ] **Step 4: Implement privileged revalidation, attestation, and upload job**

`publish` depends on inspect/build and declares the exact concurrency mapping tested in Step 1. It checks out consumer `github.sha` and trusted workflow source, downloads the one build artifact into `$RUNNER_TEMP/build`, and runs:

```text
uv run --project "$GITHUB_WORKSPACE/atlasweaver-tool" \
  python -m project_knowledge.workflow_publish prepare \
  --repo "$GITHUB_WORKSPACE/consumer/$ATLASWEAVER_REPO_ROOT" \
  --input "$RUNNER_TEMP/build/bundle.zip" \
  --output "$RUNNER_TEMP/release/bundle.zip"
```

The actual YAML uses a block scalar and environment variables for all paths; it does not interpolate input text into shell. The internal command rechecks `github.ref_type == branch`, `github.ref_protected == true`, exact manifest `source_ref`, exact numeric repository ID, `HEAD == github.sha`, clean tracked/safe projection, expected reusable signer workflow path, build bundle identity, and final health. The job runs no Graphify, tests, consumer script, consumer package build, backend, or arbitrary command.

Attest `$RUNNER_TEMP/release/bundle.zip` with `actions/attest-build-provenance@e8998f949152b193b063cb0ec769d69d929409be`, `subject-path` set to that exact file, and `push-to-registry: false`. Then call internal `workflow_publish verify-attestation`, which invokes the Task 6 verifier against the newly published attestation and requires manifest `signer_workflow` to name `owner/atlasweaver/.github/workflows/atlasweaver-publish.yml` and manifest `signer_digest` to equal the immutable called reusable-workflow commit B. This authenticated post-attestation check is the digest authority; normal `github.workflow_sha` describes the caller and is never accepted. Scope `${{ github.token }}` as `GITHUB_TOKEN` separately to exactly the signer-verification and upload steps; neither token reaches preparation, Graphify, summary, nor artifacts. Only after verification succeeds call internal `workflow_publish upload`. The uploader performs Task 7's remote verify/tag/retention ordering. Finish with an always-run content-free JSON/job summary artifact; never upload the release bundle as an ordinary workflow artifact from this job.

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
git add src tests skills scripts config examples README.md CHANGELOG.md SECURITY.md pyproject.toml uv.lock
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
    reusable_commit = git_rev_parse("HEAD^")
    caller = load_workflow("atlasweaver-self-publish.yml")
    assert caller["on"] == {"workflow_dispatch": None}
    assert caller["permissions"] == {
        "attestations": "write", "contents": "write", "id-token": "write"
    }
    job = caller["jobs"]["publish"]
    assert job["uses"] == (
        "MarkusMakEvil/atlasweaver/.github/workflows/"
        f"atlasweaver-publish.yml@{reusable_commit}"
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
    assert load_manifest(ROOT / ".graphify-project.yaml", ROOT).artifacts.signer_digest == reusable_commit
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
git diff --stat
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

If protected-main, OIDC, attestation, named backend secret, or release permissions are unavailable, stop at this gate with the stable failure. Do not weaken permissions, signer digest, attestation flags, branch protection, or semantic extraction.

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
