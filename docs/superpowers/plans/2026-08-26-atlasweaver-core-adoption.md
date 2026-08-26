# AtlasWeaver Core Adoption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make repository onboarding, diagnosis, privacy-safe refresh, health inspection, graph queries, coverage approval, and registry synchronization safe first-class `project-knowledge` workflows.

**Architecture:** Execute in two interlocked phases: compatibility/impact Tasks 1–4, this plan's Tasks 1–5, compatibility/impact Tasks 5–8, then this plan's Tasks 6–13. The immutable compatibility registry and version adapter own Graphify-specific argv, normalization, evidence, and impact trust; the core layer owns strict repository configuration, safe projection, lifecycle locking/orchestration, query snapshots, health, registry transactions, and CLI contracts. Every mutation enters one repository lifecycle lock, every Graphify invocation sees only a private staged tree and an operation-specific environment, and every read-only graph operation validates an immutable owned snapshot before traversal.

**Tech Stack:** Python 3.10+, frozen dataclasses, PyYAML `>=6.0,<7`, pathspec `>=0.12,<1`, descriptor-confined POSIX filesystem operations, `pytest>=8,<10`, official Graphify `0.9.48`, GitHub Actions on Python 3.10 and 3.13.

**Spec:** `docs/superpowers/specs/2026-08-26-atlasweaver-core-adoption-design.md`

## Global Constraints

- Enforce this phase gate exactly: compatibility/impact Tasks 1–4 → core Tasks 1–5 → compatibility/impact Tasks 5–8 → core Tasks 6–13 → artifact/fleet. Do not run either plan straight through in isolation.
- Graphify `0.9.48` remains unconditionally `navigation`; no core setting may promote it to trusted impact analysis.
- AtlasWeaver does not become a long-running daemon and performs no background enrollment, refresh, registry synchronization, publication, hook installation, or Obsidian write.
- Never run Graphify against the unsanitized repository root. Only a mode-0700 private staged projection may be an extraction source.
- Never forward `--allow-partial`, `--global`, `--no-gitignore`, database introspection, Google Workspace export, `--force`, or arbitrary native flags.
- `--code-only` is caller-explicit; a semantic corpus without an admitted backend fails with `semantic_backend_required` before extraction.
- Backend credentials come only from the compatibility entry's operation-specific environment-name allowlist; Graphify never inherits the complete parent environment.
- Backend credential values never enter evidence, graph metadata, health, query output, diagnostics, or digests; the sorted admitted environment-variable names are intentionally bound into extraction evidence.
- Resolve Graphify once into an immutable canonical path/device/inode/launcher-SHA identity and no-follow revalidate it immediately before every Graphify subprocess. Any replacement or mutation fails closed as `graphify_executable_changed` before spawn; no public command exposes a binary override.
- Repository lock order is lifecycle lock, then promotion lock; registry operations take the per-user registry lock only after the repository lifecycle lock.
- Configuration files are mode `0644`; `.project-knowledge`, private run directories, query sandboxes, and per-user registry state are mode `0700`; receipts and journals are mode `0600`.
- `source_digest` is the domain-separated SHA-256 of ordered safe `{path, sha256, byte_length}` entries. `projection_digest` additionally binds the policy version, extraction-relevant manifest fields, ignore-rule digests, private reason-coded decisions, coverage-control digest, and `source_digest`.
- Denied filenames and detector fingerprints stay private. Only aggregate action/reason counts and `projection_digest` may enter graph metadata, health output, telemetry, or future bundles.
- Queries are read-only and never persist query text, cache, or results. Fixed limits are 4,096 UTF-8 input bytes, 100,000 nodes, 500,000 edges, 32 path hops, 8 affected hops, 100 returned nodes, 100 neighbors, 16 relation filters, and 1,048,576 output bytes.
- Health always emits schema v2. The compatibility `status` alias equals `core_status`; optional disabled/unavailable features never lower core status.
- Schema v1 remains readable and all existing low-level commands remain callable. `refresh`, registry sync, and remote-artifact identity require explicit schema-v2 migration and a persisted UUIDv4 `project_uid`.
- Do not bump or publish the package version in this plan; the final artifact/fleet plan owns the single release version, build, installation smoke test, GitHub release, merge, and branch cleanup.
- Use TDD for every behavior. Each production commit must pass the focused tests named in its task before the next task begins.

## Inter-plan interface ledger

Compatibility/impact Tasks 1–4 provide the compatibility registry, argv, integrity, adapter protocol, and captured 0.9.48 fixtures used by core Tasks 1–5. Core Tasks 1–5 then provide manifest v2, structured projection, and coverage contracts to compatibility/impact Tasks 5–8. After that second impact phase, core Tasks 6–13 import, but never redefine, these exact evidence/ownership interfaces:

```python
from project_knowledge.compatibility import (
    CapabilityProbe,
    BackendContract,
    CompatibilityError,
    GraphifyCompatibility,
    RenderedCommand,
    admitted_graphify_environment,
    assert_capability_surface,
    bind_semantic_backend_credential,
    render_graphify_global_add,
    render_graphify_argv,
    resolve_graphify_compatibility,
    supported_graphify_versions,
    validate_semantic_backend,
)
from project_knowledge.adapters import adapter_for
from project_knowledge.adapters.base import CapturedArtifact, NormalizationResult
from project_knowledge.evidence import (
    GraphEvidence,
    build_extraction_invocation,
    build_graph_evidence,
)
from project_knowledge.artifacts import (
    GitIdentity,
    ValidatedGraph,
    promote_graph,
    validate_candidate,
    validate_owned_graph,
)
from project_knowledge.graphify import (
    CommandRunner,
    GraphifyCapabilities,
    ResolvedGraphifyExecutable,
    probe_graphify,
    revalidate_graphify_executable,
    resolve_graphify_executable,
    run_checked,
)
```

The implemented signatures relied on here are:

```python
def resolve_graphify_compatibility(version: str) -> GraphifyCompatibility: ...
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
def render_graphify_global_add(
    contract: GraphifyCompatibility,
    *,
    binary: Path,
    graph: Path,
    registry_key: str,
) -> RenderedCommand: ...
def admitted_graphify_environment(
    contract: GraphifyCompatibility,
    backend: str | None,
    ambient: Mapping[str, str],
) -> dict[str, str]: ...
def assert_capability_surface(
    contract: GraphifyCompatibility,
    probe: CapabilityProbe,
) -> None: ...
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
def resolve_graphify_executable(
    test_override: Path | None = None,
) -> ResolvedGraphifyExecutable: ...
def revalidate_graphify_executable(
    executable: ResolvedGraphifyExecutable,
) -> None: ...
def probe_graphify(
    executable: ResolvedGraphifyExecutable,
    contract: GraphifyCompatibility,
    runner: CommandRunner,
) -> GraphifyCapabilities: ...
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
```

`BackendContract.canonical_credential_environment` and `bind_semantic_backend_credential` are the sole generic-secret bridge for trusted reusable-workflow/fleet preflight. The locked mapping is Claude→`ANTHROPIC_API_KEY`, DeepSeek→`DEEPSEEK_API_KEY`, Gemini→`GEMINI_API_KEY`, Kimi→`MOONSHOT_API_KEY`, Ollama→`OLLAMA_API_KEY`, and OpenAI→`OPENAI_API_KEY`; a missing credential is accepted only for credentialless Ollama. The opaque value never enters argv, canonical argv, evidence, digests, logs, or response documents.

Core refresh calls the evidence-bound adaptation path only:

```python
adapt_candidate(
    raw_candidate,
    destination,
    staged,
    manifest,
    evidence=evidence,
    post_write_check=post_write_check,
)
```

Evidence is required for adaptation schema 2. Core then supplies `build_epoch >= 1` only to `validate_candidate`, where ownership is created without changing graph bytes; a core refresh rejects a returned candidate unless `artifact_schema_version == 2`, `projection_digest == staged.projection_digest`, and `impact_trust` came from the compatibility/impact layer.

For live trust recomputation, `decide_impact_trust` accepts `source_current: bool | None` and `projection_current: bool | None`: `None` means unverified and must demote trust. Core health, query, and registry pass expected source/projection digests to `validate_owned_graph`, so only exact verified equality supplies `True`; no core caller upgrades an impact decision on its own.

## File Map

| File | Responsibility |
|---|---|
| `src/project_knowledge/models.py` | Frozen v1/v2 manifest, feature, artifact, privacy-decision, projection, and coverage value objects. |
| `src/project_knowledge/manifest.py` | Strict one-document YAML loading, deterministic init/migration rendering, and recoverable no-clobber configuration transactions. |
| `src/project_knowledge/locking.py` | No-follow private state bootstrap, repository lifecycle lock, and explicit lock-order guards. |
| `src/project_knowledge/privacy.py` | Structured `deny`/`scan`/`allow` path policy and ignore-file digests. |
| `src/project_knowledge/staging.py` | Full manifest-scope decisions, v1/v2 safe snapshots, staging, `source_digest`, and `projection_digest`. |
| `src/project_knowledge/receipt.py` | Backward-readable receipt v1 plus projection-bound receipt v2. |
| `src/project_knowledge/secrets_scan.py` | Projection-candidate scanning and literal redaction used by query/CLI output. |
| `src/project_knowledge/coverage.py` | Strict `.atlasweaver-coverage.yaml` loading, preview, journaled apply, and omission admission. |
| `src/project_knowledge/graphify.py` | Capped process-group runner with injected timeout/environment plus exact compatibility probing. |
| `src/project_knowledge/lifecycle.py` | Refresh pipeline, private run cleanup, failure injection, build epoch, and post-promotion stale result. |
| `src/project_knowledge/health.py` | Schema-v2 core/feature/trust classification and live owned-graph inspection. |
| `src/project_knowledge/doctor.py` | Read-only package, manifest, projection, capability, skill, graph, and provider diagnostics. |
| `src/project_knowledge/queries.py` | Immutable query snapshots and bounded deterministic `query`/`path`/`explain`/`affected`. |
| `src/project_knowledge/registry.py` | Atomic AtlasWeaver per-user registry and journaled Graphify compatibility projection. |
| `src/project_knowledge/cli.py` | Public parser, preview/apply routing, lifecycle-lock routing, stable JSON envelopes, and error mapping. |
| `tests/support.py` | Canonical v1/v2 project/manifest and graph fixtures shared by new tests. |
| `tests/test_manifest_v2.py` | Strict schema, init, migration, transaction, and recovery tests. |
| `tests/test_lifecycle_locking.py` | State bootstrap, lock order, race, and signal-release tests. |
| `tests/test_privacy_v2.py` | Structured policy, projection digest, receipt, and redaction tests. |
| `tests/test_coverage.py` | Approval schema, binding, preview/apply, and trust-demotion tests. |
| `tests/test_lifecycle.py` | Refresh argv/environment, boundaries, cleanup, drift, and promotion tests. |
| `tests/test_doctor.py` | Read-only diagnostic and installed-package/resource-version tests. |
| `tests/test_queries.py` | Query admission, traversal, ambiguity, caps, redaction, and immutability tests. |
| `tests/test_registry.py` | Registry status/sync, journals, projection, lock, and recovery tests. |
| `tests/test_cli_adoption.py` | Complete high-level CLI JSON/error contract and low-level compatibility tests. |
| `tests/test_real_graphify_pipeline.py` | Real official 0.9.48 refresh and semantic-preflight integration. |
| `README.md`, `CHANGELOG.md`, `skills/using-project-knowledge-graphs/{SKILL.md,references/workflow.md}` | Exact operator and agent workflow. |
| `.graphify-project.yaml`, `examples/.graphify-project.yaml` | Dogfood and example schema-v2 manifests with optional features disabled. |
| `.graphifyignore`, `examples/.graphifyignore` | Reviewed projection boundaries used by dogfood and examples. |

---

### Task 1: Manifest schema v2 and shared test fixtures

**Files:**
- Create: `tests/support.py`
- Create: `tests/test_manifest_v2.py`
- Modify: `src/project_knowledge/models.py`
- Modify: `src/project_knowledge/manifest.py`
- Modify: `tests/test_manifest.py`

**Interfaces:**
- Consumes: `resolve_graphify_compatibility(version: str) -> GraphifyCompatibility`.
- Produces: `FeatureIntent(atlas: Literal["disabled", "enabled"], registry: Literal["disabled", "enabled"])`.
- Produces: `ArtifactIntent(provider, host, repository, repository_id, channel, source_ref, signer_workflow, signer_digest)`.
- Produces: backward-compatible `ProjectManifest` with `project_uid: UUID | None`, `features`, and `artifacts` defaults.
- Produces: `load_manifest(path: Path, repo_root: Path) -> ProjectManifest` for exact schemas 1 and 2.
- Produces: `load_manifest_payload(payload: bytes, repo_root: Path) -> ProjectManifest`; registry/fleet parses already captured bytes and never reopens a path.
- Produces: `render_manifest_v2(manifest: ProjectManifest) -> bytes`.

- [ ] **Step 1: Add canonical fixture builders and failing strict-v2 tests**

```python
# tests/support.py
from pathlib import Path, PurePosixPath
from uuid import UUID

from project_knowledge.models import ArtifactIntent, FeatureIntent, ProjectManifest

DEMO_UID = UUID("4ed9af24-5aa2-4eac-8d0a-3f622cc74948")

def manifest_v1() -> ProjectManifest:
    return ProjectManifest(
        schema_version=1,
        project_id="demo",
        display_name="Demo",
        include_roots=(PurePosixPath("src"),),
        output_dir=PurePosixPath("graphify-out"),
        obsidian_namespace=PurePosixPath("Projects/demo/Generated"),
        excludes=(),
        track_html=False,
        graphify_version="0.9.48",
    )

def manifest_v2(**changes: object) -> ProjectManifest:
    values: dict[str, object] = {
        **manifest_v1().__dict__,
        "schema_version": 2,
        "project_uid": DEMO_UID,
        "features": FeatureIntent(),
        "artifacts": ArtifactIntent(),
    }
    values.update(changes)
    return ProjectManifest(**values)

def write_manifest_v2(repo: Path, **changes: object) -> Path:
    manifest = manifest_v2(**changes)
    path = repo / ".graphify-project.yaml"
    path.write_bytes(__import__("project_knowledge.manifest", fromlist=["render_manifest_v2"]).render_manifest_v2(manifest))
    return path
```

```python
# tests/test_manifest_v2.py
from pathlib import Path
from uuid import UUID
import pytest

from project_knowledge.manifest import (
    ManifestError, load_manifest, load_manifest_payload, render_manifest_v2,
)
from tests.support import DEMO_UID, manifest_v2, write_manifest_v2

def test_v2_round_trip_is_canonical_and_closed(tmp_path: Path) -> None:
    path = write_manifest_v2(tmp_path)
    loaded = load_manifest(path, tmp_path)
    assert loaded == manifest_v2()
    assert loaded.project_uid == DEMO_UID
    assert loaded.features.registry == "disabled"
    assert loaded.artifacts.provider == "none"
    assert render_manifest_v2(loaded) == path.read_bytes()

def test_byte_owned_manifest_parser_matches_descriptor_loader(tmp_path: Path) -> None:
    payload = render_manifest_v2(manifest_v2())
    path = tmp_path / ".graphify-project.yaml"
    path.write_bytes(payload)
    assert load_manifest_payload(payload, tmp_path) == load_manifest(path, tmp_path)

@pytest.mark.parametrize("payload", [
    "schema_version: 2\nschema_version: 2\n",
    "schema_version: 2\nfeatures: &f {atlas: disabled, registry: disabled}\ncopy: *f\n",
    "schema_version: 2\nfeatures: {atlas: false, registry: disabled}\n",
    "schema_version: 2\nfeatures: {atlas: disabled, registry: disabled, extra: x}\n",
    "schema_version: 2\n---\nschema_version: 2\n",
])
def test_v2_rejects_duplicates_aliases_wrong_types_unknowns_and_documents(
    tmp_path: Path, payload: str
) -> None:
    path = tmp_path / ".graphify-project.yaml"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(path, tmp_path)

def test_github_provider_requires_exact_transport_identity(tmp_path: Path) -> None:
    path = write_manifest_v2(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        "artifacts:\n  provider: none\n",
        "artifacts:\n  provider: github-release\n  host: github.com\n",
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ManifestError, match="artifact transport"):
        load_manifest(path, tmp_path)

def test_v1_maps_optional_intent_without_portable_identity(tmp_path: Path) -> None:
    from tests.test_manifest import write_manifest
    loaded = load_manifest(write_manifest(tmp_path), tmp_path)
    assert loaded.project_uid is None
    assert loaded.features.atlas == loaded.features.registry == "disabled"
    assert loaded.artifacts.provider == "none"
```

- [ ] **Step 2: Run the tests and verify the missing contracts fail**

Run: `uv run pytest -q tests/test_manifest.py tests/test_manifest_v2.py`

Expected: collection fails with `ImportError: cannot import name 'ArtifactIntent' from 'project_knowledge.models'`.

- [ ] **Step 3: Add frozen models and the strict version-dispatched loader**

```python
# src/project_knowledge/models.py
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

FeatureSwitch = Literal["disabled", "enabled"]
ArtifactProvider = Literal["none", "github-release"]

@dataclass(frozen=True)
class FeatureIntent:
    atlas: FeatureSwitch = "disabled"
    registry: FeatureSwitch = "disabled"

@dataclass(frozen=True)
class ArtifactIntent:
    provider: ArtifactProvider = "none"
    host: str | None = None
    repository: str | None = None
    repository_id: int | None = None
    channel: str | None = None
    source_ref: str | None = None
    signer_workflow: str | None = None
    signer_digest: str | None = None

@dataclass(frozen=True)
class ProjectManifest:
    schema_version: int
    project_id: str
    display_name: str
    include_roots: tuple[PurePosixPath, ...]
    output_dir: PurePosixPath
    obsidian_namespace: PurePosixPath
    excludes: tuple[str, ...]
    track_html: bool
    graphify_version: str
    project_uid: UUID | None = None
    features: FeatureIntent = field(default_factory=FeatureIntent)
    artifacts: ArtifactIntent = field(default_factory=ArtifactIntent)
```

In `manifest.py`, replace `yaml.safe_load` and the single `_FIELDS` set with a loader that rejects aliases and duplicate keys before dispatching schemas:

```python
class _StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent: yaml.Node | None, index: int | None) -> yaml.Node:
        if self.check_event(yaml.AliasEvent):
            raise ManifestError("YAML aliases are forbidden")
        return super().compose_node(parent, index)

def _construct_unique_mapping(
    loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ManifestError("duplicate YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result

_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)

def _load_one_yaml(payload: bytes) -> dict[str, object]:
    try:
        value = yaml.load(payload.decode("utf-8"), Loader=_StrictLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ManifestError("manifest must be one valid UTF-8 YAML document") from error
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ManifestError("manifest must be a string-keyed mapping")
    return value
```

`load_manifest_payload` owns schema dispatch and must validate the existing nine fields for schema 1, fill the three backward-compatible defaults, and validate these exact schema-v2 nested sets. `load_manifest` descriptor-opens one no-follow regular file, caps it at 262,144 bytes, reads exact bytes, verifies the inode/size before and after, closes it, and delegates those owned bytes to `load_manifest_payload`; no parser helper reopens the path. Validate these exact schema-v2 nested sets:

```python
V2_FIELDS = frozenset({
    "schema_version", "project_id", "project_uid", "display_name",
    "include_roots", "output_dir", "obsidian_namespace", "excludes",
    "track_html", "graphify_version", "features", "artifacts",
})
FEATURE_FIELDS = frozenset({"atlas", "registry"})
GITHUB_FIELDS = frozenset({
    "provider", "host", "repository", "repository_id", "channel",
    "source_ref", "signer_workflow", "signer_digest",
})

def _graphify_version(value: object) -> str:
    version = _nonempty_string(value, "graphify_version")
    try:
        resolve_graphify_compatibility(version)
    except CompatibilityError as error:
        raise ManifestError("graphify_version is unsupported") from error
    return version
```

For `provider: none`, require the artifact mapping to be exactly `{"provider": "none"}`. For `github-release`, require every `GITHUB_FIELDS` field, `host == "github.com"`, a positive non-boolean numeric repository ID, `repository` matching `[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+`, a channel matching `[A-Za-z0-9][A-Za-z0-9._-]{0,62}`, a source ref beginning `refs/heads/`, a confined `<owner>/<repo>/.github/workflows/<name>.yml` signer workflow, and a lowercase 40-hex signer digest. Parse `project_uid` with `UUID(value)`, require `version == 4` and `variant == RFC_4122`, and preserve it as `UUID`.

Render with fixed key ordering and scalar quoting through a local `_yaml_scalar` that permits only already-validated strings; do not use a dumper that emits tags or anchors:

```python
def _yaml_scalar(value: str) -> str:
    if type(value) is not str or not value:
        raise ManifestError("manifest scalar is invalid")
    return json.dumps(value, ensure_ascii=False)

def render_manifest_v2(manifest: ProjectManifest) -> bytes:
    if manifest.schema_version != 2 or manifest.project_uid is None:
        raise ManifestError("only a complete schema-v2 manifest can be rendered")
    lines = [
        "schema_version: 2",
        f"project_id: {_yaml_scalar(manifest.project_id)}",
        f"project_uid: {manifest.project_uid}",
        f"display_name: {_yaml_scalar(manifest.display_name)}",
        "include_roots:",
        *(f"  - {_yaml_scalar(path.as_posix())}" for path in manifest.include_roots),
        "output_dir: graphify-out",
        f"obsidian_namespace: {_yaml_scalar(manifest.obsidian_namespace.as_posix())}",
        *(
            ["excludes: []"] if not manifest.excludes
            else ["excludes:", *(f"  - {_yaml_scalar(value)}" for value in manifest.excludes)]
        ),
        f"track_html: {'true' if manifest.track_html else 'false'}",
        f"graphify_version: {_yaml_scalar(manifest.graphify_version)}",
        "features:",
        f"  atlas: {manifest.features.atlas}",
        f"  registry: {manifest.features.registry}",
        "artifacts:",
        f"  provider: {manifest.artifacts.provider}",
    ]
    if manifest.artifacts.provider == "github-release":
        for key in ("host", "repository", "repository_id", "channel", "source_ref", "signer_workflow", "signer_digest"):
            value = getattr(manifest.artifacts, key)
            lines.append(f"  {key}: {value if type(value) is int else _yaml_scalar(str(value))}")
    return ("\n".join(lines) + "\n").encode("utf-8")
```

- [ ] **Step 4: Run focused and legacy tests**

Run: `uv run pytest -q tests/test_manifest.py tests/test_manifest_v2.py tests/test_privacy.py tests/test_staging.py tests/test_artifacts.py`

Expected: all selected tests pass; existing direct schema-v1 `ProjectManifest(...)` construction remains valid.

- [ ] **Step 5: Commit**

```bash
git add src/project_knowledge/models.py src/project_knowledge/manifest.py tests/support.py tests/test_manifest.py tests/test_manifest_v2.py
git commit -m "feat: add strict manifest v2 contract"
```

### Task 2: Private state bootstrap and repository lifecycle lock

**Files:**
- Create: `tests/test_lifecycle_locking.py`
- Modify: `src/project_knowledge/locking.py`
- Modify: `src/project_knowledge/artifacts.py`
- Modify: `tests/test_locking.py`
- Modify: `tests/test_artifacts.py`

**Interfaces:**
- Produces: `StateRoot(path: Path, descriptor: int)` context manager.
- Produces: `open_state_root(repo_root: Path, *, create: bool) -> StateRoot | None`.
- Produces: `ExclusiveDescriptorLock(parent_fd: int, name: str, timeout: float = 5.0)`; lock files are opened relative to the verified state descriptor.
- Produces: `repository_lifecycle_lock(repo_root: Path, timeout: float = 5.0) -> ExclusiveFileLock`.
- Produces: `assert_lifecycle_lock_held(repo_root: Path) -> None` for nested promotion-order enforcement.
- Preserves: `ExclusiveFileLock(path: Path, timeout: float = 5.0)` and `promote_graph(...)`'s narrower `promotion.lock`.

- [ ] **Step 1: Write failing mode, symlink, contention, and lock-order tests**

```python
# tests/test_lifecycle_locking.py
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import pytest

from project_knowledge.locking import (
    TransactionLockError,
    assert_lifecycle_lock_held,
    open_state_root,
    repository_lifecycle_lock,
)

def test_state_root_is_private_real_and_no_follow(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with open_state_root(repo, create=True) as state:
        assert state is not None
        assert state.path == repo / ".project-knowledge"
        assert state.descriptor >= 0
    assert (repo / ".project-knowledge").stat().st_mode & 0o777 == 0o700

def test_state_root_rejects_symlink_and_unsafe_permissions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, repo / ".project-knowledge", target_is_directory=True)
    with pytest.raises(TransactionLockError, match="private state"):
        open_state_root(repo, create=True)

def test_lifecycle_lock_serializes_and_exposes_lock_order(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with repository_lifecycle_lock(repo):
        assert_lifecycle_lock_held(repo)
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocked = pool.submit(lambda: repository_lifecycle_lock(repo, timeout=0.01).__enter__())
            with pytest.raises(TransactionLockError, match="timed out"):
                blocked.result()
    with pytest.raises(TransactionLockError, match="lifecycle lock is required"):
        assert_lifecycle_lock_held(repo)
```

- [ ] **Step 2: Run the tests and verify the new imports fail**

Run: `uv run pytest -q tests/test_locking.py tests/test_lifecycle_locking.py`

Expected: collection fails because `open_state_root` and `repository_lifecycle_lock` do not exist.

- [ ] **Step 3: Implement no-follow bootstrap and process-local order tracking**

```python
# additions to src/project_knowledge/locking.py
from contextlib import AbstractContextManager
from dataclasses import dataclass
import contextvars
import os
import stat

_LIFECYCLE_ROOTS: contextvars.ContextVar[tuple[Path, ...]] = contextvars.ContextVar(
    "atlasweaver_lifecycle_roots", default=()
)

@dataclass
class StateRoot(AbstractContextManager["StateRoot"]):
    path: Path
    descriptor: int
    def __exit__(self, *args: object) -> None:
        os.close(self.descriptor)

def open_state_root(repo_root: Path, *, create: bool) -> StateRoot | None:
    repo = repo_root.absolute()
    root_fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        created = False
        try:
            info = os.stat(".project-knowledge", dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                return None
            os.mkdir(".project-knowledge", 0o700, dir_fd=root_fd)
            os.fsync(root_fd)
            created = True
            info = os.stat(".project-knowledge", dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise TransactionLockError("private state must be a real directory")
        descriptor = os.open(
            ".project-knowledge",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        if created:
            os.fchmod(descriptor, 0o700)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            os.close(descriptor)
            raise TransactionLockError("private state changed while opening")
        if opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o700:
            os.close(descriptor)
            raise TransactionLockError("private state permissions are unsafe")
        return StateRoot(repo / ".project-knowledge", descriptor)
    finally:
        os.close(root_fd)

class ExclusiveDescriptorLock(AbstractContextManager["ExclusiveDescriptorLock"]):
    def __init__(self, parent_fd: int, name: str, timeout: float = 5.0) -> None:
        self.parent_fd = os.dup(parent_fd)
        self.name = name
        self.timeout = timeout
        self.fd: int | None = None
    def __enter__(self) -> "ExclusiveDescriptorLock":
        try:
            self.fd = os.open(
                self.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600, dir_fd=self.parent_fd,
            )
            opened = os.fstat(self.fd)
            named = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                raise TransactionLockError("transaction lock file is unsafe")
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TransactionLockError("transaction lock timed out")
                    time.sleep(0.01)
        except BaseException:
            self.__exit__(None, None, None)
            raise
    def __exit__(self, *args: object) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1

class RepositoryLifecycleLock(AbstractContextManager["RepositoryLifecycleLock"]):
    def __init__(self, repo_root: Path, timeout: float) -> None:
        self.repo_root = repo_root.absolute()
        self.timeout = timeout
        self.state: StateRoot | None = None
        self.lock: ExclusiveDescriptorLock | None = None
        self.token: contextvars.Token[tuple[Path, ...]] | None = None
    def __enter__(self) -> "RepositoryLifecycleLock":
        try:
            self.state = open_state_root(self.repo_root, create=True)
            assert self.state is not None
            self.lock = ExclusiveDescriptorLock(
                self.state.descriptor, "refresh.lock", self.timeout
            )
            self.lock.__enter__()
            self.token = _LIFECYCLE_ROOTS.set((*_LIFECYCLE_ROOTS.get(), self.repo_root))
            return self
        except BaseException:
            if self.lock is not None:
                self.lock.__exit__(None, None, None)
            if self.state is not None:
                self.state.__exit__(None, None, None)
            self.lock = None
            self.state = None
            raise
    def __exit__(self, *args: object) -> None:
        if self.token is not None:
            _LIFECYCLE_ROOTS.reset(self.token)
        if self.lock is not None:
            self.lock.__exit__(*args)
        if self.state is not None:
            self.state.__exit__(*args)

def repository_lifecycle_lock(
    repo_root: Path, timeout: float = 5.0
) -> RepositoryLifecycleLock:
    return RepositoryLifecycleLock(repo_root, timeout)

def assert_lifecycle_lock_held(repo_root: Path) -> None:
    if repo_root.absolute() not in _LIFECYCLE_ROOTS.get():
        raise TransactionLockError("repository lifecycle lock is required")
```

`ExclusiveDescriptorLock` is the only lock used for lifecycle and global-registry state. It duplicates the caller's verified directory descriptor, opens `name` with `dir_fd`, `O_RDWR | O_CREAT | O_NOFOLLOW`, requires a regular file owned by `os.geteuid()` with mode `0600`, verifies the opened inode against a no-follow `os.stat`, then applies the existing non-blocking `fcntl.flock` timeout loop. It closes both the file and duplicated parent descriptor on every failed `__enter__` path. Add a race test that renames `.project-knowledge` and replaces it with a symlink immediately after `open_state_root`; the lock must land in the descriptor-bound original directory and nothing may be created through the symlink.

Update `promote_graph` so it calls `assert_lifecycle_lock_held(root)` immediately before entering `promotion.lock`. Update all direct artifact tests to acquire `repository_lifecycle_lock(repo)` around the production call; add one explicit regression asserting an unlocked promotion fails. This makes the required order executable rather than documentary.

- [ ] **Step 4: Run lock and promotion tests**

Run: `uv run pytest -q tests/test_locking.py tests/test_lifecycle_locking.py tests/test_artifacts.py`

Expected: all selected tests pass, including existing transaction recovery tests under the outer lifecycle lock.

- [ ] **Step 5: Commit**

```bash
git add src/project_knowledge/locking.py src/project_knowledge/artifacts.py tests/test_locking.py tests/test_lifecycle_locking.py tests/test_artifacts.py
git commit -m "feat: enforce repository lifecycle lock order"
```

### Task 3: Recoverable init and explicit manifest migration

**Files:**
- Modify: `src/project_knowledge/manifest.py`
- Modify: `tests/test_manifest_v2.py`
- Test: `tests/test_lifecycle_locking.py`

**Interfaces:**
- Produces: `InitPreview(status, manifest_payload, ignore_payload, candidate_roots, project_uid)`.
- Produces: `preview_init(repo_root, project_id, *, project_uid=None, include_roots=()) -> InitPreview`.
- Produces: `apply_init(repo_root, preview, *, uuid_factory=uuid4) -> Literal["initialized", "already_configured", "init_conflict", "init_recovery_required"]`.
- Produces: `preview_manifest_migration(repo_root, *, project_uid=None) -> InitPreview`.
- Produces: `apply_manifest_migration(repo_root, preview, *, uuid_factory=uuid4) -> Literal["migrated", "already_current", "init_conflict", "init_recovery_required"]`.
- Produces: `inspect_init_journal(repo_root) -> Literal["none", "recoverable", "corrupt"]` without writing.

- [ ] **Step 1: Write failing deterministic-preview, no-clobber, rollback, and recovery tests**

```python
# append to tests/test_manifest_v2.py
from project_knowledge.manifest import (
    apply_init,
    apply_manifest_migration,
    inspect_init_journal,
    preview_init,
    preview_manifest_migration,
)

def test_init_preview_is_read_only_and_apply_generates_uuid_once(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    before = tuple(repo.iterdir())
    preview = preview_init(repo, "demo")
    assert preview.status == "preview"
    assert preview.project_uid is None
    assert b"<generated-on-apply>" in preview.manifest_payload
    assert tuple(repo.iterdir()) == before
    status = apply_init(repo, preview, uuid_factory=lambda: DEMO_UID)
    assert status == "initialized"
    assert load_manifest(repo / ".graphify-project.yaml", repo).project_uid == DEMO_UID
    assert inspect_init_journal(repo) == "none"

def test_init_requires_explicit_roots_when_discovery_is_ambiguous(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir()
    preview = preview_init(repo, "demo")
    assert preview.status == "ambiguous_roots"
    assert preview.candidate_roots == (PurePosixPath("docs"), PurePosixPath("src"))
    with pytest.raises(ManifestError, match="ambiguous_roots"):
        apply_init(repo, preview, uuid_factory=lambda: DEMO_UID)

def test_init_never_overwrites_and_rolls_back_only_its_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    preview = preview_init(repo, "demo", include_roots=(PurePosixPath("src"),))
    original_replace = os.link
    calls = 0
    def fail_second(source: str, target: str, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second create failure")
        original_replace(source, target, **kwargs)
    monkeypatch.setattr(os, "link", fail_second)
    with pytest.raises(ManifestError, match="configuration transaction failed"):
        apply_init(repo, preview, uuid_factory=lambda: DEMO_UID)
    assert not (repo / ".graphify-project.yaml").exists()
    assert not (repo / ".graphifyignore").exists()
    assert inspect_init_journal(repo) == "recoverable"

def test_v1_migration_changes_only_manifest(tmp_path: Path) -> None:
    from tests.test_manifest import write_manifest
    repo = tmp_path
    write_manifest(repo)
    ignore = repo / ".graphifyignore"
    ignore.write_bytes(b"caller-reviewed-ignore/**\n")
    graph = repo / "graphify-out/graph.json"
    graph.parent.mkdir()
    graph.write_text("caller bytes\n", encoding="utf-8")
    preview = preview_manifest_migration(repo, project_uid=DEMO_UID)
    assert apply_manifest_migration(repo, preview) == "migrated"
    assert load_manifest(repo / ".graphify-project.yaml", repo).schema_version == 2
    assert ignore.read_bytes() == b"caller-reviewed-ignore/**\n"
    assert graph.read_text(encoding="utf-8") == "caller bytes\n"
```

- [ ] **Step 2: Run the tests and verify the configuration APIs are absent**

Run: `uv run pytest -q tests/test_manifest_v2.py`

Expected: collection fails on the missing `preview_init` import.

- [ ] **Step 3: Implement canonical previews and the journaled two-file transaction**

Use these exact public value objects and discovery set:

```python
@dataclass(frozen=True)
class InitPreview:
    status: Literal["preview", "ambiguous_roots", "already_configured", "already_current"]
    manifest_payload: bytes
    ignore_payload: bytes
    candidate_roots: tuple[PurePosixPath, ...]
    project_uid: UUID | None

_DISCOVERABLE_ROOTS = (
    "app", "cmd", "docs", "lib", "packages", "scripts", "services", "src", "tests"
)
_DEFAULT_GRAPHIFYIGNORE = b".env*\n.git/\n.project-knowledge/\ngraphify-out/\n"
_INIT_JOURNAL = "init-transaction.json"
_INIT_SCHEMA = 1
```

`preview_init` must lstat the repository, reject existing configuration as `already_configured`, sort real non-symlink discovery roots, require explicit confined roots when discovery finds more than one, and render `<generated-on-apply>` only in preview. `apply_init` must acquire `repository_lifecycle_lock`, recover a valid prior journal first, allocate UUIDv4 exactly once, replace the placeholder in-memory, and execute this fixed state machine:

```python
def _apply_configuration_pair(
    repo_root: Path,
    *,
    manifest_payload: bytes,
    ignore_payload: bytes,
) -> None:
    state = open_state_root(repo_root, create=True)
    assert state is not None
    with state:
        transaction_id = secrets.token_hex(16)
        manifest_temp = f"init-{transaction_id}.manifest"
        ignore_temp = f"init-{transaction_id}.ignore"
        _write_new_at(state.descriptor, manifest_temp, manifest_payload, 0o644)
        _write_new_at(state.descriptor, ignore_temp, ignore_payload, 0o644)
        journal = {
            "schema_version": 1,
            "phase": "prepared",
            "files": [
                {"destination": ".graphify-project.yaml", "temporary": manifest_temp,
                 "sha256": hashlib.sha256(manifest_payload).hexdigest()},
                {"destination": ".graphifyignore", "temporary": ignore_temp,
                 "sha256": hashlib.sha256(ignore_payload).hexdigest()},
            ],
        }
        _write_new_at(state.descriptor, _INIT_JOURNAL, _canonical_json(journal), 0o600)
        _install_without_replacement(repo_root, state, journal)
        _mark_journal_committed(state, journal)
        _remove_transaction_temporaries(state, journal)
        _unlink_regular_at(state.descriptor, _INIT_JOURNAL)
```

Migration calls a distinct `_replace_v1_manifest(repo_root, expected_old_sha256, new_payload)` transaction whose strict journal contains exactly `destination`, `old_inode`, `old_sha256`, `backup`, `new_temporary`, `new_sha256`, and `phase`. There is no ignore-file field in that journal. The function aborts if the current manifest is not the captured v1 inode/digest, and recovery restores or completes only those exact recorded manifest names and hashes.

`_install_without_replacement` uses `os.link(..., follow_symlinks=False)` from each mode-0644 sibling temporary to an absent destination, verifies the destination inode and SHA-256, fsyncs the repository, then unlinks the temporary. Init alone uses the two-file transaction. Migration uses a separate one-file journal: it descriptor-captures and digest-binds only the existing v1 manifest, writes the new manifest to a unique sibling, renames the exact old inode to a journal-bound backup, renames the new sibling into place, and restores the backup on failure. It never creates, replaces, chmods, or removes `.graphifyignore`; an existing ignore file's bytes and inode remain caller-owned. Recovery may remove/restore only an inode and digest recorded by a valid strict journal; a missing/corrupt journal returns `init_recovery_required` and never authorizes deletion. Successful recovery fsyncs each affected directory.

- [ ] **Step 4: Run configuration and locking tests**

Run: `uv run pytest -q tests/test_manifest.py tests/test_manifest_v2.py tests/test_lifecycle_locking.py`

Expected: all selected tests pass, and previews leave no `.project-knowledge` directory.

- [ ] **Step 5: Commit**

```bash
git add src/project_knowledge/manifest.py tests/test_manifest_v2.py tests/test_lifecycle_locking.py
git commit -m "feat: add recoverable project initialization"
```

### Task 4: Privacy policy v2, projection digests, and receipt v2

**Files:**
- Create: `tests/test_privacy_v2.py`
- Modify: `src/project_knowledge/models.py`
- Modify: `src/project_knowledge/privacy.py`
- Modify: `src/project_knowledge/staging.py`
- Modify: `src/project_knowledge/receipt.py`
- Modify: `src/project_knowledge/secrets_scan.py`
- Modify: `src/project_knowledge/health.py`
- Modify: `tests/test_privacy.py`
- Modify: `tests/test_staging.py`
- Modify: `tests/test_receipt.py`
- Modify: `tests/test_secrets_scan.py`

**Interfaces:**
- Consumes: `resolve_graphify_compatibility(manifest.graphify_version).sensitive_source_suffixes`.
- Produces: `PrivacyDecision(path, action: Literal["deny", "scan", "allow"], rule_id)`.
- Produces: `ProjectionFile(path, sha256, byte_length)` and private `ProjectionSnapshot`.
- Extends: `StagedInput(root, source_digest, files, projection_digest=None, reason_counts=(), coverage_approvals=())` without breaking positional v1 callers.
- Produces: `inspect_projection(repo_root, manifest) -> ProjectionSnapshot`.
- Produces: `redact_literals(value: str) -> str`.
- Extends: receipt schema 2 with `projection_digest`, aggregate `reason_counts`, and exact v2 digest verification while continuing to load schema 1.

- [ ] **Step 1: Write failing policy-precedence and projection-binding tests**

```python
# tests/test_privacy_v2.py
from dataclasses import replace
from pathlib import Path, PurePosixPath
import json
import pytest

from project_knowledge.privacy import classify_path
from project_knowledge.receipt import load_staging_receipt
from project_knowledge.staging import inspect_projection, stage_input_with_receipt
from tests.support import manifest_v2

@pytest.mark.parametrize(("relative", "action", "rule_id"), [
    ("src/credentials.py", "scan", "sensitive_source_name"),
    ("src/SECRET.tsx", "scan", "sensitive_source_name"),
    ("src/apiToken.ts", "scan", "sensitive_source_name"),
    ("src/databaseCredentials.py", "scan", "sensitive_source_name"),
    ("src/database-creds.json", "deny", "sensitive_data_name"),
    ("src/auth-token.yaml", "deny", "sensitive_data_name"),
    ("src/.ENV.local", "deny", "environment_file"),
    ("src/signing.KEY", "deny", "private_key_material"),
    ("src/session/store.py", "deny", "session_store"),
    ("src/app.py", "allow", "ordinary_source"),
])
def test_policy_v2_precedence(relative: str, action: str, rule_id: str) -> None:
    decision = classify_path(
        PurePosixPath(relative),
        project_excludes=(),
        sensitive_source_suffixes=resolve_graphify_compatibility("0.9.48").sensitive_source_suffixes,
    )
    assert (decision.action, decision.rule_id) == (action, rule_id)

def test_sensitive_source_is_scanned_but_sensitive_data_never_staged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/credentials.py").write_text("TOKEN_NAME = getenv('TOKEN_NAME')\n", encoding="utf-8")
    (repo / "src/database-creds.json").write_text('{"url":"postgres://u:long-password@db"}\n', encoding="utf-8")
    staged = stage_input_with_receipt(repo, manifest_v2(), tmp_path / "stage", tmp_path / "receipt.json")
    assert staged.files == (PurePosixPath("src/credentials.py"),)
    assert staged.projection_digest is not None
    assert not (staged.root / "src/database-creds.json").exists()

@pytest.mark.parametrize(("relative", "rule_id"), [
    (".git/config", "git_metadata"),
    (".worktrees/x/source.py", "worktree_metadata"),
    ("workspace/source.py", "root_workspace"),
    ("src/runtime/job.json", "runtime_state"),
    ("src/drafts/post.md", "draft_content"),
    ("src/snapshots/state.json", "snapshot_state"),
    ("src/cookies/data.sqlite", "cookie_store"),
    ("src/sessions/data.sqlite", "session_store"),
    ("src/identity.yaml", "identity_control"),
    ("src/disclosure.yaml", "disclosure_control"),
    ("src/current-state.yaml", "runtime_control"),
    ("Projects/demo/Notes/manual.md", "human_notes"),
])
def test_policy_v2_preserves_every_legacy_non_bypassable_rule(
    relative: str, rule_id: str
) -> None:
    decision = classify_path(
        PurePosixPath(relative), project_excludes=(),
        sensitive_source_suffixes=resolve_graphify_compatibility("0.9.48").sensitive_source_suffixes,
    )
    assert (decision.action, decision.rule_id) == ("deny", rule_id)

def test_root_workspace_rule_is_not_broadened_to_nested_component() -> None:
    decision = classify_path(
        PurePosixPath("src/workspace/tool.py"), project_excludes=(),
        sensitive_source_suffixes=resolve_graphify_compatibility("0.9.48").sensitive_source_suffixes,
    )
    assert decision.action == "allow"

def test_projection_digest_binds_ignore_policy_without_hashing_denied_payload(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("safe\n", encoding="utf-8")
    (repo / "src/private-token.json").write_text("first denied bytes\n", encoding="utf-8")
    first = inspect_projection(repo, manifest_v2())
    (repo / "src/private-token.json").write_text("second denied bytes\n", encoding="utf-8")
    second = inspect_projection(repo, manifest_v2())
    assert first.source_digest == second.source_digest
    assert first.projection_digest == second.projection_digest
    (repo / ".graphifyignore").write_text("src/generated.py\n", encoding="utf-8")
    third = inspect_projection(repo, manifest_v2())
    assert third.source_digest == second.source_digest
    assert third.projection_digest != second.projection_digest

def test_receipt_v2_is_projection_bound_and_publicly_path_safe(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("safe\n", encoding="utf-8")
    receipt_path = tmp_path / "receipt.json"
    staged = stage_input_with_receipt(repo, manifest_v2(), tmp_path / "stage", receipt_path)
    receipt = load_staging_receipt(receipt_path, manifest_v2())
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 2
    assert receipt.projection_digest == staged.projection_digest
    assert "database-creds" not in receipt_path.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run the tests and verify structured policy is missing**

Run: `uv run pytest -q tests/test_privacy.py tests/test_privacy_v2.py tests/test_staging.py tests/test_receipt.py tests/test_secrets_scan.py`

Expected: collection fails on `cannot import name 'classify_path'`.

- [ ] **Step 3: Add structured policy models and exact precedence**

```python
# additions to src/project_knowledge/models.py
PrivacyAction = Literal["deny", "scan", "allow"]

@dataclass(frozen=True)
class PrivacyDecision:
    path: PurePosixPath
    action: PrivacyAction
    rule_id: str

@dataclass(frozen=True)
class ProjectionFile:
    path: PurePosixPath
    sha256: str
    byte_length: int

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
```

In `privacy.py`, retain `is_denied` for schema-v1 compatibility but route new staging through this exact ordered classifier:

```python
_SENSITIVE_FRAGMENTS = ("token", "credential", "creds", "secret")
_DATA_SUFFIXES = frozenset({
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".tfvars", ".sqlite", ".sqlite3", ".db", ".kdbx", ".p12", ".pfx",
})
_KEY_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".kdbx"})
GLOBAL_DENY_RULES: tuple[tuple[str, str], ...] = (
    ("environment_file", ".env"),
    ("environment_file", ".env.*"),
    ("environment_file", "**/.env"),
    ("environment_file", "**/.env.*"),
    ("git_metadata", ".git/**"),
    ("worktree_metadata", ".worktrees/**"),
    ("root_workspace", "workspace/**"),
    ("runtime_state", "**/runtime/**"),
    ("draft_content", "**/drafts/**"),
    ("snapshot_state", "**/snapshots/**"),
    ("cookie_store", "**/cookies/**"),
    ("session_store", "**/sessions/**"),
    ("virtual_environment", "**/.venv/**"),
    ("dependency_tree", "**/node_modules/**"),
    ("build_output", "**/dist/**"),
    ("bytecode_cache", "**/__pycache__/**"),
    ("private_content", "**/private/**"),
    ("identity_control", "**/identity.yaml"),
    ("disclosure_control", "**/disclosure.yaml"),
    ("runtime_control", "**/current-state.yaml"),
    ("voice_source", "**/voice-source/**"),
    ("private_key_material", "**/*.pem"),
    ("private_key_material", "**/*.key"),
    ("private_key_material", "**/*.p12"),
    ("private_key_material", "**/*.pfx"),
    ("credential_store", "**/*.kdbx"),
    ("human_notes", "Projects/*/Notes/**"),
)

def classify_path(
    path: PurePosixPath,
    *,
    project_excludes: Sequence[str],
    sensitive_source_suffixes: frozenset[str],
) -> PrivacyDecision:
    if not path.parts or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return PrivacyDecision(path, "deny", "path_escape")
    folded = tuple(part.casefold() for part in path.parts)
    name = folded[-1]
    suffix = PurePosixPath(name).suffix.casefold()
    for rule_id, pattern in GLOBAL_DENY_RULES:
        if _matches(folded, tuple(part.casefold() for part in pattern.split("/"))):
            return PrivacyDecision(path, "deny", rule_id)
    if any(
        _matches(folded, tuple(part.casefold() for part in pattern.split("/")))
        for pattern in project_excludes
    ):
        return PrivacyDecision(path, "deny", "project_exclude")
    if any(fragment in name for fragment in _SENSITIVE_FRAGMENTS):
        if suffix in sensitive_source_suffixes and suffix not in _DATA_SUFFIXES:
            return PrivacyDecision(path, "scan", "sensitive_source_name")
        return PrivacyDecision(path, "deny", "sensitive_data_name")
    return PrivacyDecision(path, "allow", "ordinary_source")
```

`RepositoryIgnores` must retain canonical SHA-256 values for every regular ignore file it reads, sorted by confined relative path, and expose `digests() -> tuple[str, ...]`. Digest entries are domain-separated hashes of `{relative ignore-file path, bytes}`; only the digest values enter a public projection.

- [ ] **Step 4: Implement v2 projection capture, canonical digests, and receipt dispatch**

Add these constants and canonical hash helper to `staging.py`:

```python
SOURCE_DIGEST_DOMAIN = b"atlasweaver-source-v2\0"
PROJECTION_DIGEST_DOMAIN = b"atlasweaver-projection-v2\0"
POLICY_VERSION = 2

def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def _source_digest(files: tuple[ProjectionFile, ...]) -> str:
    payload = [
        {"path": item.path.as_posix(), "sha256": item.sha256, "byte_length": item.byte_length}
        for item in files
    ]
    return hashlib.sha256(SOURCE_DIGEST_DOMAIN + _canonical_json(payload)).hexdigest()

def _projection_digest(
    manifest: ProjectManifest,
    *,
    source_digest: str,
    decisions: tuple[PrivacyDecision, ...],
    ignore_digests: tuple[str, ...],
    reason_counts: tuple[tuple[str, int], ...],
    secret_exception_digest: str | None,
    coverage_digest: str | None,
) -> str:
    payload = {
        "policy_version": POLICY_VERSION,
        "manifest": {
            "project_id": manifest.project_id,
            "project_uid": str(manifest.project_uid),
            "include_roots": [path.as_posix() for path in manifest.include_roots],
            "excludes": list(manifest.excludes),
            "graphify_version": manifest.graphify_version,
            "track_html": manifest.track_html,
        },
        "decisions": [
            {"path": item.path.as_posix(), "action": item.action, "rule_id": item.rule_id}
            for item in decisions
        ],
        "ignore_digests": list(ignore_digests),
        "secret_exception_digest": secret_exception_digest,
        "reason_counts": dict(reason_counts),
        "coverage_digest": coverage_digest,
        "source_digest": source_digest,
    }
    return hashlib.sha256(PROJECTION_DIGEST_DOMAIN + _canonical_json(payload)).hexdigest()
```

`inspect_projection` and `stage_input` must share one descriptor-based walker. For every manifest-scope filesystem entry, record a private reason-coded decision. A symlink becomes `deny:symlink`, a non-regular file becomes `deny:non_regular`, and a regular payload containing NUL or failing strict UTF-8 text classification becomes `deny:binary_payload`; these are counted decisions, not silent skips. Do not read payload bytes for path-classified `deny`; for `scan` and `allow`, scan the exact descriptor-read payload and require all unaccepted findings absent before building `ProjectionFile`. Scanner findings still fail the entire projection closed. The coverage and secret-exception controls are never corpus files. Bind the strict control-file SHA-256 values (or `None`) for both `.atlasweaver-coverage.yaml` and `.graphify-secret-exceptions.yaml` into `projection_digest`. For schema 1, preserve receipt schema 1 and the legacy source-digest algorithm; for schema 2, require non-`None` `projection_digest` and write receipt schema 2:

```json
{
  "schema_version": 2,
  "project_id": "demo",
  "project_uid": "4ed9af24-5aa2-4eac-8d0a-3f622cc74948",
  "graphify_version": "0.9.48",
  "source_digest": "<64 hex>",
  "projection_digest": "<64 hex>",
  "files": ["src/app.py"],
  "reason_counts": {"allow:ordinary_source": 1}
}
```

Update `scan_repository` to iterate `allow` and `scan` projection candidates, not `GLOBAL_DENY_PATTERNS`. Update secret-exception validation to call `classify_path` and reject only a current `deny` decision, so `credentials.py` may use an exact contextual exception but `database-creds.json` never can. Add `redact_literals` using the existing structured detector patterns plus `_ASSIGNMENT`, replacing matched values with `[REDACTED]` and then enforcing UTF-8 byte caps at the caller.

- [ ] **Step 5: Run privacy, scanner, staging, receipt, and health regressions**

Run: `uv run pytest -q tests/test_privacy.py tests/test_privacy_v2.py tests/test_secrets_scan.py tests/test_staging.py tests/test_receipt.py tests/test_health.py`

Expected: all selected tests pass. Schema-v1 receipt golden JSON stays unchanged; schema-v2 projection output contains counts/digests but no denied filename or detector fingerprint.

- [ ] **Step 6: Commit**

```bash
git add src/project_knowledge/models.py src/project_knowledge/privacy.py src/project_knowledge/staging.py src/project_knowledge/receipt.py src/project_knowledge/secrets_scan.py src/project_knowledge/health.py tests/test_privacy.py tests/test_privacy_v2.py tests/test_staging.py tests/test_receipt.py tests/test_secrets_scan.py
git commit -m "feat: bind staging to privacy projection v2"
```

### Task 5: Tracked coverage approvals

**Files:**
- Create: `src/project_knowledge/coverage.py`
- Create: `tests/test_coverage.py`
- Modify: `src/project_knowledge/models.py`
- Modify: `src/project_knowledge/staging.py`

**Interfaces:**
- Produces: `CoverageApproval(path, content_sha256, adapter_id, reason_code, rationale)`.
- Produces: `load_coverage_approvals(repo_root, projection, contract) -> tuple[CoverageApproval, ...]`.
- Produces: `preview_coverage_approval(repo_root, manifest, path, reason_code, rationale) -> CoveragePreview`.
- Produces: `apply_coverage_approval(repo_root, preview) -> Literal["approved", "unchanged", "coverage_conflict", "coverage_recovery_required"]`.
- Extends: `ProjectionSnapshot.coverage_approvals` and `StagedInput.coverage_approvals`; compatibility/impact Tasks 5–8 consume this tracked authority when they implement candidate validation.

- [ ] **Step 1: Write failing strict-control and admission tests**

```python
# tests/test_coverage.py
from pathlib import Path, PurePosixPath
import hashlib
import pytest

from project_knowledge.compatibility import resolve_graphify_compatibility
from project_knowledge.coverage import (
    CoverageError,
    apply_coverage_approval,
    load_coverage_approvals,
    preview_coverage_approval,
)
from project_knowledge.staging import inspect_projection
from tests.support import manifest_v2

def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/unsupported.py").write_text("safe current bytes\n", encoding="utf-8")
    return repo

def test_coverage_preview_is_read_only_and_apply_binds_current_bytes(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    preview = preview_coverage_approval(
        repo, manifest_v2(), PurePosixPath("src/unsupported.py"),
        "not_represented_by_graphify", "Reviewed: navigation remains useful.",
    )
    assert not (repo / ".atlasweaver-coverage.yaml").exists()
    assert apply_coverage_approval(repo, preview) == "approved"
    projection = inspect_projection(repo, manifest_v2())
    approvals = load_coverage_approvals(
        repo, projection, resolve_graphify_compatibility("0.9.48")
    )
    assert approvals[0].path == PurePosixPath("src/unsupported.py")
    assert approvals[0].content_sha256 == projection.files[0].sha256

@pytest.mark.parametrize(("field", "value"), [
    ("path", "src/*.py"),
    ("path", "src"),
    ("path", "src/database-creds.json"),
    ("reason_code", "made_up_reason"),
    ("rationale", ""),
])
def test_coverage_rejects_globs_directories_denies_unknown_reasons_and_empty_rationale(
    tmp_path: Path, field: str, value: str
) -> None:
    import yaml
    repo = repository(tmp_path)
    control = repo / ".atlasweaver-coverage.yaml"
    entry = {
        "path": "src/unsupported.py",
        "content_sha256": hashlib.sha256(b"safe current bytes\n").hexdigest(),
        "adapter_id": "graphify-0.9.48",
        "reason_code": "not_represented_by_graphify",
        "rationale": "reviewed",
    }
    entry[field] = value
    control.write_text(
        yaml.safe_dump({"schema_version": 1, "approvals": [entry]}, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(CoverageError):
        load_coverage_approvals(
            repo, inspect_projection(repo, manifest_v2()),
            resolve_graphify_compatibility("0.9.48"),
        )

def test_content_change_invalidates_approval(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    preview = preview_coverage_approval(
        repo, manifest_v2(), PurePosixPath("src/unsupported.py"),
        "not_represented_by_graphify", "reviewed",
    )
    apply_coverage_approval(repo, preview)
    (repo / "src/unsupported.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(CoverageError, match="current content"):
        inspect_projection(repo, manifest_v2())
```

- [ ] **Step 2: Run the tests and verify the coverage module is absent**

Run: `uv run pytest -q tests/test_coverage.py tests/test_staging.py`

Expected: collection fails with `ModuleNotFoundError: project_knowledge.coverage`.

- [ ] **Step 3: Implement the closed approval schema and canonical preview**

```python
# src/project_knowledge/models.py
@dataclass(frozen=True)
class CoverageApproval:
    path: PurePosixPath
    content_sha256: str
    adapter_id: str
    reason_code: str
    rationale: str

# src/project_knowledge/coverage.py
COVERAGE_FILE = PurePosixPath(".atlasweaver-coverage.yaml")
MAX_COVERAGE_BYTES = 262_144
_FIELDS = frozenset({"schema_version", "approvals"})
_ENTRY_FIELDS = frozenset({
    "path", "content_sha256", "adapter_id", "reason_code", "rationale"
})

@dataclass(frozen=True)
class CoveragePreview:
    payload: bytes
    previous_sha256: str | None
    approval: CoverageApproval

def _validate_approval(
    entry: Mapping[str, object],
    *,
    safe_files: Mapping[PurePosixPath, ProjectionFile],
    contract: GraphifyCompatibility,
) -> CoverageApproval:
    if set(entry) != _ENTRY_FIELDS:
        raise CoverageError("coverage approval schema is invalid")
    path = _exact_file_path(entry["path"])
    if any(character in path.as_posix() for character in "*?[]"):
        raise CoverageError("coverage approval globs are forbidden")
    current = safe_files.get(path)
    if current is None:
        raise CoverageError("coverage approval must name a current safe file")
    adapter_id = _nonempty(entry["adapter_id"])
    reason_code = _nonempty(entry["reason_code"])
    rationale = _nonempty(entry["rationale"])
    if adapter_id != contract.adapter_id or reason_code not in contract.coverage_reason_codes:
        raise CoverageError("coverage approval adapter or reason is invalid")
    if entry["content_sha256"] != current.sha256:
        raise CoverageError("coverage approval does not match current content")
    return CoverageApproval(path, current.sha256, adapter_id, reason_code, rationale)
```

Use the manifest module's strict loader rules: one document, no aliases/merges/duplicates/non-string keys. Order approvals by path, reject duplicate paths, cap the control at 262,144 bytes, scan its payload with `scan_payload`, and include its full SHA-256 (or `None`) in `projection_digest`. The control file is explicitly excluded from staged `files`.

`preview_coverage_approval` requires a current safe regular file, the selected compatibility entry's adapter ID/reason code, and non-empty rationale; it outputs the complete canonical replacement and the prior control digest without writing. `apply_coverage_approval` takes `repository_lifecycle_lock`, rereads and digest-compares the prior file, and uses a mode-0600 journal plus descriptor-bound backup/new sibling rename. It must never overwrite a changed inode and must recover only its own recorded inode/digest.

- [ ] **Step 4: Bind approvals into staging for the evidence/validator phase**

Add `coverage_approvals: tuple[CoverageApproval, ...] = ()` to `ProjectionSnapshot`, and add both that field plus `projection_files: tuple[ProjectionFile, ...] = ()` to `StagedInput`. `stage_input`/`inspect_projection` load approvals only after safe-file capture and then recompute `projection_digest` with the coverage file digest. Do not modify adapter/evidence/artifact code in this phase. Compatibility/impact Tasks 5–8 consume these immutable fields and must ignore candidate-controlled `approved` values, recompute exact path/content/adapter/reason matching, force approved omissions to `navigation`, and reject unapproved omissions as `extraction_coverage_unapproved`.

Avoid a projection/coverage recursion: implement a private `_inspect_projection_base(repo_root, manifest) -> ProjectionSnapshot` that descriptor-captures policy decisions and safe file hashes with `coverage_digest` bound but `coverage_approvals=()`. `inspect_projection` passes that base snapshot to `load_coverage_approvals`, then returns a frozen copy containing the validated approvals and the same already-bound projection digest. `stage_input` copies only from that finalized snapshot. `load_coverage_approvals` must never call `inspect_projection`.

The integration code must use this exact predicate:

```python
def omission_is_approved(
    path: PurePosixPath,
    reason_code: str,
    staged: StagedInput,
    adapter_id: str,
) -> bool:
    safe_hashes = {item.path: item.sha256 for item in staged.projection_files}
    return any(
        approval.path == path
        and approval.reason_code == reason_code
        and approval.adapter_id == adapter_id
        and approval.content_sha256 == safe_hashes.get(path)
        for approval in staged.coverage_approvals
    )
```

- [ ] **Step 5: Run focused integration tests**

Run: `uv run pytest -q tests/test_coverage.py tests/test_staging.py tests/test_privacy_v2.py tests/test_receipt.py`

Expected: all selected tests pass; the staged projection exposes only tracked, current approvals to the next implementation phase.

- [ ] **Step 6: Commit**

```bash
git add src/project_knowledge/coverage.py src/project_knowledge/models.py src/project_knowledge/staging.py tests/test_coverage.py tests/test_staging.py
git commit -m "feat: add tracked extraction coverage approvals"
```

**Mandatory phase gate:** stop core execution here. Execute compatibility/impact Tasks 5–8 and require their full focused suite to pass before starting Task 6. Those tasks consume `ProjectManifest.project_uid`, `ProjectionFile`, `StagedInput.projection_digest`, `StagedInput.projection_files`, and `StagedInput.coverage_approvals`, and produce the evidence-bound adapter/validator interfaces used below.

### Task 6: Capped Graphify process boundary and exact lifecycle argv

**Files:**
- Modify: `src/project_knowledge/graphify.py`
- Modify: `tests/test_graphify_adapter.py`
- Create: `tests/test_lifecycle.py`

**Interfaces:**
- Extends without weakening: prerequisite `run_checked(runner, executable, argv, *, env=None, timeout=30.0) -> CompletedProcess[str]`.
- Produces: `run_graphify_operation(runner, executable, argv, *, env, timeout, operation) -> CompletedProcess[str]`.
- Consumes unchanged: prerequisite `ResolvedGraphifyExecutable`, `resolve_graphify_executable(test_override=None)`, and `revalidate_graphify_executable()`; the override is a library-test seam and never a high-level CLI flag.
- Consumes unchanged: prerequisite `probe_graphify(executable, contract, runner) -> GraphifyCapabilities`, including its official private operational smoke pipeline and nested `capability_probe`.
- Consumes: compatibility `render_graphify_argv`, `admitted_graphify_environment`, and `assert_capability_surface`.
- Preserves: process-group termination, bounded stdout/stderr, literal/path redaction, and argument-vector-only execution.

- [ ] **Step 1: Write failing exact argv/environment/timeout tests**

```python
# initial tests in tests/test_lifecycle.py
from pathlib import Path
from subprocess import CompletedProcess

from project_knowledge.compatibility import (
    admitted_graphify_environment,
    render_graphify_argv,
    resolve_graphify_compatibility,
)
from project_knowledge.graphify import run_graphify_operation

class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str], float]] = []
    def run(self, argv, *, text, capture_output, check, timeout, env):
        assert text and capture_output and not check
        self.calls.append((tuple(argv), dict(env), timeout))
        return CompletedProcess(tuple(argv), 0, "{}\n", "")

def test_0948_refresh_argv_is_rendered_only_by_compatibility_contract(tmp_path: Path) -> None:
    contract = resolve_graphify_compatibility("0.9.48")
    stage = tmp_path / "stage"
    raw = tmp_path / "raw"
    graph = raw / "graphify-out/graph.json"
    extract = render_graphify_argv(
        contract, "extract", binary=Path("graphify"), source=stage,
        output=raw, code_only=True,
    )
    assert extract.canonical_argv == (
        "<graphify>", "extract", "<staged-root>", "--out",
        "<raw-output-root>", "--no-cluster", "--code-only",
    )
    diagnose = render_graphify_argv(
        contract, "diagnose", binary=Path("graphify"), source=stage,
        output=raw, graph=graph,
    )
    assert diagnose.canonical_argv == (
        "<graphify>", "diagnose", "multigraph", "--graph",
        "<native-graph>", "--undirected", "--json",
    )
    cluster = render_graphify_argv(
        contract, "cluster", binary=Path("graphify"), source=stage,
        output=tmp_path / "cluster", graph=tmp_path / "cluster/graphify-out/graph.json",
        track_html=False,
    )
    assert cluster.canonical_argv == (
        "<graphify>", "cluster-only", "<cluster-workspace>", "--graph",
        "<cluster-input>", "--no-label", "--no-viz",
    )

def test_runner_receives_only_admitted_environment_names(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CUSTOMER_SECRET", "must-not-cross")
    monkeypatch.setenv("OPENAI_API_KEY", "admitted-for-selected-backend")
    contract = resolve_graphify_compatibility("0.9.48")
    env = admitted_graphify_environment(contract, "openai", dict(os.environ))
    runner = RecordingRunner()
    executable = resolved_executable(tmp_path)
    run_graphify_operation(
        runner, executable, (str(executable.path), "extract", str(tmp_path)),
        env=env, timeout=123.0, operation="extract",
    )
    assert "CUSTOMER_SECRET" not in runner.calls[0][1]
    assert runner.calls[0][1]["OPENAI_API_KEY"] == "admitted-for-selected-backend"
    assert runner.calls[0][2] == 123.0
```

Retain and extend `tests/test_graphify_adapter.py` with a real child-process timeout test proving the child process group is killed and neither its delayed stdout nor a secret value appears in diagnostics.

Add a `resolved_executable(tmp_path)` fixture that writes an executable launcher and calls `resolve_graphify_executable(test_override=launcher)`. Test `run_graphify_operation()` refuses raw/mismatched argv zero. After resolution, independently test `os.replace`, same-inode byte rewrite, symlink replacement, chmod without execute bits, disappearance, and a runner that mutates the launcher after the first successful call. Each next call must raise exactly `GraphifyContractError("graphify_executable_changed")` and record no additional subprocess. This is required even when a capability probe already succeeded.

- [ ] **Step 2: Run the tests and verify the new runner API is absent**

Run: `uv run pytest -q tests/test_graphify_adapter.py tests/test_lifecycle.py`

Expected: collection fails on `cannot import name 'run_graphify_operation'`.

- [ ] **Step 3: Generalize the existing safe runner without weakening defaults**

```python
# src/project_knowledge/graphify.py
def run_checked(
    runner: CommandRunner,
    executable: ResolvedGraphifyExecutable,
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = COMMAND_TIMEOUT,
) -> CompletedProcess[str]:
    command = tuple(str(part) for part in argv)
    operation = _operation_name(command)
    return run_graphify_operation(
        runner,
        executable,
        command,
        env=minimal_environment() if env is None else env,
        timeout=timeout,
        operation=operation,
    )

def run_graphify_operation(
    runner: CommandRunner,
    executable: ResolvedGraphifyExecutable,
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    operation: str,
) -> CompletedProcess[str]:
    if not argv or not operation or timeout <= 0:
        raise GraphifyContractError("Graphify operation contract is invalid")
    command = tuple(str(part) for part in argv)
    if command[0] != str(executable.path):
        raise GraphifyContractError("graphify_executable_changed")
    child_environment = dict(env)
    # This must remain the final action before runner.run. Do not re-resolve.
    revalidate_graphify_executable(executable)
    try:
        result = runner.run(
            command, text=True, capture_output=True, check=False,
            timeout=timeout, env=child_environment,
        )
    except subprocess.TimeoutExpired as error:
        raise GraphifyCommandError(operation, "command timed out") from error
    except OSError as error:
        raise GraphifyCommandError(operation, "command could not be executed") from error
    result = _capped_result(result)
    if result.returncode:
        raise GraphifyCommandError(
            operation,
            sanitize_stderr(result.stderr, sensitive_values=tuple(env.values())),
        )
    return result
```

Extend `sanitize_stderr(stderr, *, sensitive_values: Sequence[str] = ())` to replace every distinct non-empty supplied value of at least five characters with `[REDACTED]` before applying the existing credential-literal, environment, home, and Atlas-path patterns. Keep `minimal_environment()` as the default for legacy probes. It may include only `HOME`, `LANG`, `LC_ALL`, and `PATH`. Refresh never uses the default: it always supplies `admitted_graphify_environment(...)`. Keep `shell=False`, POSIX `start_new_session=True`, concurrent pipe draining, 16,384-character capture, and hard process-group termination. Redact both captured streams before any exception or diagnostic field is created.

Mechanically update every existing Graphify subprocess helper and caller in `graphify.py` (version/help, legacy query/path/explain/export, registry operations, and assistant installation) to accept a `ResolvedGraphifyExecutable`, render argv zero from `.path`, and call identity-revalidating `run_checked()`/`run_graphify_operation()`. No production function may accept a raw executable `Path` after resolution or call `runner.run` directly. Add an AST/`rg` regression that the sole production `runner.run(` occurrence is inside `run_graphify_operation()` and every public high-level parser lacks `--graphify-binary`/equivalent.

Do not replace or weaken the prerequisite plan's `probe_graphify(executable, contract, runner) -> GraphifyCapabilities`: it already uses identity-revalidating `run_checked()` for every `--version`, `--help`, registry-rendered extract/diagnose/cluster/global-add/agent-install subprocess in private roots, then calls `assert_capability_surface(contract, capabilities.capability_probe)`. Add a regression in `tests/test_lifecycle.py` that refresh calls this probe before creating its source stage and aborts with no stage when any fingerprint differs. Add another that mutates the launcher after probe return and proves the first lifecycle extraction fails before runner invocation.

- [ ] **Step 4: Run focused runner and compatibility tests**

Run: `uv run pytest -q tests/test_graphify_adapter.py tests/test_compatibility.py tests/test_lifecycle.py`

Expected: all selected tests pass; recorded argv contains no shell string and recorded environments contain no unadmitted name.

- [ ] **Step 5: Commit**

```bash
git add src/project_knowledge/graphify.py tests/test_graphify_adapter.py tests/test_lifecycle.py
git commit -m "feat: add exact Graphify lifecycle process boundary"
```

### Task 7: Evidence-bound refresh orchestration

**Files:**
- Create: `src/project_knowledge/lifecycle.py`
- Modify: `tests/test_lifecycle.py`
- Test: `tests/test_evidence.py`
- Test: `tests/test_artifacts.py`

**Interfaces:**
- Produces: `RefreshOptions(backend, model, deep, code_only)`; the high-level CLI has no binary override.
- Produces: `RefreshResult(status, source_digest, projection_digest, graph_digest, build_epoch, core_status, trust, limitations)`.
- Produces: `RefreshError(code: str, message: str, recovery_id: str | None = None)`.
- Produces: `RefreshFileSystem.checkpoint(operation: str)`, `create_run_root(...)`, `write_private_artifact(...)`, and descriptor-safe cleanup for failure injection.
- Produces: `refresh_project(repo_root, manifest, options, *, runner=SubprocessCommandRunner(), fs=REAL_REFRESH_FS, ambient=None, graphify_binary=None) -> RefreshResult`; `graphify_binary` is an injected library-test seam only, is immediately frozen by `resolve_graphify_executable()`, and is absent from every CLI parser.
- Consumes: `adapter_for`, `capture_native_artifact`, `build_extraction_invocation`, `build_graph_evidence`, evidence-bound `adapt_candidate`, `validate_candidate(..., build_epoch=...)`, and `promote_graph`.

- [ ] **Step 1: Write failing happy-path sequence, environment, and schema-v2 ownership tests**

```python
# append to tests/test_lifecycle.py
from dataclasses import replace
import json
import os

from project_knowledge.lifecycle import RefreshOptions, refresh_project
from tests.support import manifest_v2

class FixtureRefreshFileSystem:
    def __init__(self) -> None:
        self.checkpoints: list[str] = []
    def checkpoint(self, operation: str) -> None:
        self.checkpoints.append(operation)

def test_refresh_runs_official_pipeline_and_promotes_owned_v2_graph(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    runner = Official0948FixtureRunner()
    fs = FixtureRefreshFileSystem()
    result = refresh_project(
        repo,
        manifest_v2(),
        RefreshOptions(backend=None, model=None, deep=False, code_only=True),
        runner=runner,
        fs=fs,
        ambient={"CUSTOMER_SECRET": "never-forward"},
        graphify_binary=fixture_graphify(tmp_path),
    )
    assert result.status == "refreshed"
    assert result.trust == "navigation"
    assert result.build_epoch == 1
    ownership = json.loads((repo / "graphify-out/.project-knowledge-ownership.json").read_text())
    assert ownership["schema_version"] == 2
    assert ownership["build_epoch"] == 1
    assert ownership["projection_digest"] == result.projection_digest
    assert "GRAPH_EVIDENCE.json" in ownership["artifacts"]
    assert [call.operation for call in runner.rendered_calls] == [
        "extract", "diagnose", "cluster"
    ]
    assert "CUSTOMER_SECRET" not in runner.environments[0]
    assert fs.checkpoints == [
        "preflight", "projection", "extract", "capture-native", "diagnose",
        "normalize", "cluster", "capture-final", "evidence", "adapt",
        "validate", "pre-promote-projection", "promote",
        "post-promote-projection", "health", "cleanup",
    ]
```

`Official0948FixtureRunner` is a test-only `CommandRunner` that recognizes the exact `RenderedCommand.argv` arrays from the compatibility registry and writes the captured 0.9.48 fixture artifacts at their requested private outputs. Any unexpected or forbidden flag raises `AssertionError`; the runner records each passed environment.

- [ ] **Step 2: Add failure-boundary, cleanup, semantic-preflight, and drift tests**

```python
@pytest.mark.parametrize("boundary", [
    "preflight", "projection", "extract", "capture-native", "diagnose",
    "normalize", "cluster", "capture-final", "evidence", "adapt", "validate",
    "pre-promote-projection", "promote",
])
def test_refresh_failure_before_commit_preserves_previous_graph(
    tmp_path: Path, boundary: str
) -> None:
    repo, previous = owned_v2_repository(tmp_path)
    fs = FailingRefreshFileSystem(boundary)
    with pytest.raises(RefreshError):
        refresh_project(
            repo, manifest_v2(), RefreshOptions(None, None, False, True),
            runner=Official0948FixtureRunner(), fs=fs, ambient={}, graphify_binary=fixture_graphify(tmp_path),
        )
    assert tree_snapshot(repo / "graphify-out") == previous
    assert not any((repo / ".project-knowledge/runs").iterdir())

def test_semantic_backend_is_required_before_stage_creation(tmp_path: Path) -> None:
    repo = source_repository(tmp_path)
    with pytest.raises(RefreshError) as raised:
        refresh_project(
            repo, manifest_v2(), RefreshOptions(None, None, False, False),
            runner=Official0948FixtureRunner(), ambient={}, graphify_binary=fixture_graphify(tmp_path),
        )
    assert raised.value.code == "semantic_backend_required"
    assert not (repo / ".project-knowledge/runs").exists()

def test_selected_backend_without_credential_fails_before_stage(tmp_path: Path) -> None:
    repo = source_repository(tmp_path)
    with pytest.raises(RefreshError) as raised:
        refresh_project(
            repo, manifest_v2(), RefreshOptions("openai", None, False, False),
            runner=Official0948FixtureRunner(), ambient={}, graphify_binary=fixture_graphify(tmp_path),
        )
    assert raised.value.code == "semantic_backend_required"
    assert not (repo / ".project-knowledge/runs").exists()

def test_selected_credential_value_reaches_only_subprocess_but_name_is_evidence(
    tmp_path: Path,
) -> None:
    repo = source_repository(tmp_path)
    runner = Official0948FixtureRunner()
    secret = "opaque-semantic-secret"
    refresh_project(
        repo, manifest_v2(), RefreshOptions("openai", "gpt-5", False, False),
        runner=runner, ambient={"OPENAI_API_KEY": secret},
        graphify_binary=fixture_graphify(tmp_path),
    )
    assert any(env.get("OPENAI_API_KEY") == secret for env in runner.environments)
    evidence_text = (repo / "graphify-out/GRAPH_EVIDENCE.json").read_text(encoding="utf-8")
    assert secret not in evidence_text
    assert "OPENAI_API_KEY" in evidence_text

def test_post_commit_drift_reports_promoted_but_stale_without_fake_rollback(
    tmp_path: Path, monkeypatch
) -> None:
    repo = source_repository(tmp_path)
    fs = MutatingRefreshFileSystem(
        "post-promote-projection",
        lambda: (repo / "src/app.py").write_text("changed after commit\n", encoding="utf-8"),
    )
    result = refresh_project(
        repo, manifest_v2(), RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(), fs=fs, ambient={}, graphify_binary=fixture_graphify(tmp_path),
    )
    assert result.status == "promoted_but_stale"
    assert result.core_status == "stale"
    assert (repo / "graphify-out/graph.json").is_file()

def test_cleanup_failure_retains_private_run_and_reports_only_recovery_id(
    tmp_path: Path
) -> None:
    repo = source_repository(tmp_path)
    fs = CleanupFailingRefreshFileSystem()
    with pytest.raises(RefreshError) as raised:
        refresh_project(
            repo, manifest_v2(), RefreshOptions(None, None, False, True),
            runner=Official0948FixtureRunner(), fs=fs, ambient={}, graphify_binary=fixture_graphify(tmp_path),
        )
    assert raised.value.code == "cleanup_failed"
    assert raised.value.recovery_id is not None
    assert str(repo) not in str(raised.value)
```

- [ ] **Step 3: Run tests and verify lifecycle orchestration is absent**

Run: `uv run pytest -q tests/test_lifecycle.py`

Expected: collection fails with `ModuleNotFoundError: project_knowledge.lifecycle`.

- [ ] **Step 4: Implement lifecycle value objects and the exact refresh state machine**

```python
# src/project_knowledge/lifecycle.py
EXTRACT_TIMEOUT_SECONDS = 7_200.0
DIAGNOSE_TIMEOUT_SECONDS = 60.0
CLUSTER_TIMEOUT_SECONDS = 1_800.0

@dataclass(frozen=True)
class RefreshOptions:
    backend: str | None
    model: str | None
    deep: bool
    code_only: bool

@dataclass(frozen=True)
class RefreshResult:
    status: Literal["refreshed", "unchanged", "promoted_but_stale"]
    source_digest: str
    projection_digest: str
    graph_digest: str
    build_epoch: int
    core_status: str
    trust: Literal["trusted", "navigation"]
    limitations: tuple[str, ...]

@dataclass(frozen=True)
class RefreshError(Exception):
    code: str
    message: str
    recovery_id: str | None = None

def refresh_project(
    repo_root: Path,
    manifest: ProjectManifest,
    options: RefreshOptions,
    *,
    runner: CommandRunner | None = None,
    fs: RefreshFileSystem = REAL_REFRESH_FS,
    ambient: Mapping[str, str] | None = None,
    graphify_binary: Path | None = None,
) -> RefreshResult:
    if manifest.schema_version != 2 or manifest.project_uid is None:
        raise RefreshError("manifest_migration_required", "refresh requires manifest schema 2")
    runner = runner or SubprocessCommandRunner()
    ambient_values = dict(os.environ if ambient is None else ambient)
    executable = resolve_graphify_executable(test_override=graphify_binary)
    contract = resolve_graphify_compatibility(manifest.graphify_version)
    if not options.code_only:
        if options.backend is None:
            raise RefreshError("semantic_backend_required", "semantic extraction requires a backend")
        validate_semantic_backend(contract, options.backend, ambient_values)
    elif options.backend is not None or options.model is not None or options.deep:
        raise RefreshError("invalid_refresh_options", "code-only cannot select semantic options")
    environment = admitted_graphify_environment(contract, options.backend, ambient_values)
    with repository_lifecycle_lock(repo_root):
        if inspect_init_journal(repo_root) != "none":
            raise RefreshError("init_recovery_required", "configuration recovery is required")
        fs.checkpoint("preflight")
        capabilities = probe_graphify(executable, contract, runner)
        recovery_id = secrets.token_hex(16)
        run = fs.create_run_root(repo_root, recovery_id)
        try:
            fs.checkpoint("projection")
            staged = stage_input_with_receipt(
                repo_root, manifest, run / "stage", run / "receipt.json"
            )
            assert staged.projection_digest is not None
            commands: list[RenderedCommand] = []

            extract = render_graphify_argv(
                contract, "extract", binary=executable.path,
                source=staged.root, output=run / "raw", backend=options.backend,
                model=options.model, code_only=options.code_only, deep=options.deep,
                track_html=manifest.track_html,
            )
            commands.append(extract)
            fs.checkpoint("extract")
            run_graphify_operation(
                runner, executable, extract.argv, env=environment,
                timeout=EXTRACT_TIMEOUT_SECONDS,
                operation="extract",
            )
            fs.checkpoint("capture-native")
            native = capture_native_artifact(
                run / "raw/graphify-out/graph.json", PurePosixPath("raw/graph.json"),
                max_bytes=134_217_728,
            )
            adapter = adapter_for(contract)
            native_graph = adapter.parse_post_dedup(native)

            diagnose = render_graphify_argv(
                contract, "diagnose", binary=executable.path,
                source=staged.root, output=run / "raw",
                graph=run / "raw/graphify-out/graph.json",
            )
            commands.append(diagnose)
            fs.checkpoint("diagnose")
            diagnosis_result = run_graphify_operation(
                runner, executable, diagnose.argv, env=environment,
                timeout=DIAGNOSE_TIMEOUT_SECONDS,
                operation="diagnose",
            )
            diagnosis = fs.write_private_artifact(
                run, PurePosixPath("raw/diagnose.json"),
                diagnosis_result.stdout.encode("utf-8"), max_bytes=1_048_576,
            )

            fs.checkpoint("normalize")
            normalization = adapter.normalize_for_cluster(native_graph)
            cluster_graph = fs.write_private_artifact(
                run, PurePosixPath("cluster-input/graph.json"),
                normalization.cluster_input.payload, max_bytes=134_217_728,
            )
            cluster = render_graphify_argv(
                contract, "cluster", binary=executable.path,
                source=run / "cluster", output=run / "cluster",
                graph=run / "cluster/graphify-out/graph.json",
                backend=options.backend, model=options.model,
                track_html=manifest.track_html,
            )
            commands.append(cluster)
            fs.checkpoint("cluster")
            run_graphify_operation(
                runner, executable, cluster.argv, env=environment,
                timeout=CLUSTER_TIMEOUT_SECONDS,
                operation="cluster",
            )

            fs.checkpoint("capture-final")
            clustered_graph = capture_native_artifact(
                run / "cluster/graphify-out/graph.json", PurePosixPath("clustered/graph.json"),
                max_bytes=134_217_728,
            )
            report = capture_native_artifact(
                run / "cluster/graphify-out/GRAPH_REPORT.md", PurePosixPath("clustered/GRAPH_REPORT.md"),
                max_bytes=134_217_728,
            )
            final_graph = CapturedArtifact.from_payload(
                PurePosixPath("adapted/graph.json"),
                adapter.adapt_clustered_graph(
                    clustered_graph, staged_files=frozenset(staged.files)
                ),
            )
            captured = [native, diagnosis, cluster_graph, clustered_graph, report]
            if manifest.track_html:
                captured.append(capture_native_artifact(
                    run / "cluster/graphify-out/graph.html", PurePosixPath("clustered/graph.html"),
                    max_bytes=134_217_728,
                ))
            invocation = build_extraction_invocation(
                contract,
                executable_sha256=capabilities.executable.launcher_sha256,
                capability_smoke_digest=capabilities.capability_probe.digest,
                commands=tuple(commands), backend=options.backend, model=options.model,
                configuration_sha256=_configuration_sha256(manifest, options),
                source_digest=staged.source_digest,
                projection_digest=staged.projection_digest,
                environment_names=tuple(sorted(environment)),
                artifacts=tuple(captured),
            )
            fs.checkpoint("evidence")
            evidence = build_graph_evidence(
                contract, source_digest=staged.source_digest,
                projection_digest=staged.projection_digest, invocation=invocation,
                native_graph=native, diagnosis=diagnosis,
                normalization=normalization, clustered_graph=clustered_graph,
                final_graph=final_graph,
            )

            fs.checkpoint("adapt")
            adapted = adapt_candidate(
                run / "cluster/graphify-out", run / "candidate", staged, manifest,
                evidence=evidence,
                post_write_check=lambda: _require_current_projection(repo_root, manifest, staged),
            )
            if adapted.artifact_schema_version != 2 or adapted.projection_digest != staged.projection_digest:
                raise RefreshError("adaptation_failed", "adapter did not preserve projection evidence")
            build_epoch = _next_build_epoch(repo_root, manifest)
            fs.checkpoint("validate")
            validated = validate_candidate(
                adapted.root, staged, manifest,
                expected_projection_digest=staged.projection_digest,
                build_epoch=build_epoch,
                git_identity=None,
            )
            fs.checkpoint("pre-promote-projection")
            _require_current_projection(repo_root, manifest, staged)
            fs.checkpoint("promote")
            promoted = promote_graph(validated, repo_root)
            fs.checkpoint("post-promote-projection")
            current = inspect_projection(repo_root, manifest)
            stale = (
                current.source_digest != staged.source_digest
                or current.projection_digest != staged.projection_digest
            )
            fs.checkpoint("health")
            health = assess_health(inspect_project_state(repo_root, manifest))
            return RefreshResult(
                status="promoted_but_stale" if stale else ("unchanged" if not promoted.changed else "refreshed"),
                source_digest=staged.source_digest,
                projection_digest=staged.projection_digest,
                graph_digest=validated.graph_digest,
                build_epoch=build_epoch,
                core_status="stale" if stale else health.core_status,
                trust=validated.impact_trust,
                limitations=validated.impact_limitations,
            )
        finally:
            fs.checkpoint("cleanup")
            try:
                fs.remove_run_root(run)
            except OSError as error:
                raise RefreshError(
                    "cleanup_failed", "private refresh cleanup failed", recovery_id
                ) from error
```

`RefreshFileSystem` must create `.project-knowledge/runs` and each random child descriptor-relatively, no-follow, mode 0700; `write_private_artifact` exclusively creates parent directories/files, enforces the passed byte cap, fsyncs, and returns `CapturedArtifact`. `resolve_graphify_executable(None)` in `graphify.py` resolves only the literal `graphify` executable through `shutil.which` into the prerequisite immutable path/device/inode/launcher-digest object; the optional explicit path exists only as a library-test seam and is not parsed by any CLI. Every lifecycle child receives that same object, renders argv with `executable.path`, and is revalidated in `run_graphify_operation()` immediately before spawn. Invocation evidence uses `capabilities.executable.launcher_sha256`; no later ad-hoc path hash exists. `_configuration_sha256` hashes canonical JSON of manifest schema/project UID/Graphify/track-html plus backend/model/deep/code-only. `_require_current_projection` compares source digest, projection digest, ordered projection files, and coverage approvals. `_next_build_epoch` returns the previous valid ownership-v2 epoch plus one, otherwise one; booleans and values above `2**63 - 1` fail closed. Persist `tuple(sorted(environment))` unchanged as the invocation's admitted environment-name evidence, including the selected credential variable name; only names are bound, never values. No graph bytes contain the epoch.

If cleanup fails while another error is active, `cleanup_failed` is the returned stable code and chains the original internally; neither path nor original exception text is serialized. Signal/timeout behavior stays in the process runner. `RefreshFileSystem.checkpoint` exists in production as a no-op and is invoked at every boundary listed by the test.

- [ ] **Step 5: Run refresh, evidence, adapter, artifact, and transaction tests**

Run: `uv run pytest -q tests/test_lifecycle.py tests/test_evidence.py tests/test_adapter.py tests/test_artifacts.py tests/test_staging.py tests/test_receipt.py`

Expected: all selected tests pass. Graphify 0.9.48 refresh produces required evidence but remains `navigation`; every pre-commit failure preserves the old graph.

- [ ] **Step 6: Commit**

```bash
git add src/project_knowledge/lifecycle.py tests/test_lifecycle.py tests/test_evidence.py tests/test_artifacts.py
git commit -m "feat: orchestrate evidence-bound graph refresh"
```

### Task 8: Health schema v2 and read-only doctor

**Files:**
- Create: `src/project_knowledge/doctor.py`
- Create: `tests/test_doctor.py`
- Modify: `src/project_knowledge/health.py`
- Modify: `tests/test_health.py`
- Test: `tests/test_public_release.py`

**Interfaces:**
- Produces: `FeatureHealth(status: Literal["disabled", "available", "unavailable", "misconfigured"], issues=())`.
- Produces: `TrustHealth(impact: Literal["trusted", "navigation"], limitations=())`.
- Produces: `KnowledgeHealth(schema_version=2, core_status, status, source_matches, projection_matches, features, trust, issues, warnings, legacy booleans)`.
- Produces: `inspect_project_state(repo_root, manifest, *, atlas=None, registry=None, artifacts=None) -> KnowledgeState`.
- Produces: `DoctorDiagnostic(code, severity)` and `DoctorResult(status, package, manifest, projection, graphify, skill, health, diagnostics)`.
- Produces: `doctor_project(repo_root, *, graphify_binary=None, runner=None, package_version=None) -> DoctorResult`; the binary keyword is an injected library-test seam only and is absent from CLI.

- [ ] **Step 1: Replace aggregate health expectations with failing schema-v2 tests**

```python
# additions/replacements in tests/test_health.py
from project_knowledge.artifacts import validate_owned_graph
from project_knowledge.health import FeatureHealth, KnowledgeState, assess_health
from project_knowledge.health import inspect_project_state
from project_knowledge.models import ProjectManifest
from tests.support import manifest_v2
from tests.test_lifecycle import owned_v2_repository

def healthy_state(**changes: object) -> KnowledgeState:
    values: dict[str, object] = {
        "project_id": "demo",
        "graph_exists": True,
        "graph_valid": True,
        "graph_version": "0.9.48",
        "current_source_digest": "a" * 64,
        "graph_source_digest": "a" * 64,
        "current_projection_digest": "b" * 64,
        "graph_projection_digest": "b" * 64,
        "features": {
            "atlas": FeatureHealth("disabled"),
            "registry": FeatureHealth("disabled"),
            "artifacts": FeatureHealth("disabled"),
        },
        "coverage_skips": 0,
        "unapproved_skips": 0,
        "impact_trust": "navigation",
        "impact_limitations": ("pre_dedup_edge_projection_unavailable",),
        "errors": (),
    }
    values.update(changes)
    return KnowledgeState(**values)

def test_disabled_optional_features_do_not_lower_core_health() -> None:
    health = assess_health(healthy_state())
    assert health.schema_version == 2
    assert health.core_status == health.status == "healthy"
    assert health.features["atlas"].status == "disabled"
    assert health.issues == ()
    assert "impact_evidence_incomplete" in health.warnings
    assert health.impact_analysis_trusted is False

def test_enabled_but_unavailable_feature_warns_without_lowering_core() -> None:
    health = assess_health(healthy_state(features={
        "atlas": FeatureHealth("unavailable", ("atlas_unavailable",)),
        "registry": FeatureHealth("disabled"),
        "artifacts": FeatureHealth("disabled"),
    }))
    assert health.core_status == "healthy"
    assert "atlas_unavailable" in health.warnings

@pytest.mark.parametrize(("changes", "status"), [
    ({"errors": ("source_inspection_failed",)}, "error"),
    ({"graph_exists": False, "graph_valid": False}, "missing"),
    ({"graph_source_digest": "c" * 64}, "stale"),
    ({"graph_projection_digest": "c" * 64}, "stale"),
    ({"coverage_skips": 1}, "partial"),
    ({}, "healthy"),
])
def test_core_precedence(changes: dict[str, object], status: str) -> None:
    assert assess_health(healthy_state(**changes)).core_status == status

def test_unapproved_coverage_is_error_not_admitted_partial() -> None:
    health = assess_health(healthy_state(coverage_skips=1, unapproved_skips=1))
    assert health.core_status == "error"
    assert "extraction_coverage_unapproved" in health.issues

def test_live_inspection_supplies_both_current_digests_to_owned_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = owned_v2_repository(tmp_path)
    manifest = manifest_v2()
    real_validate = validate_owned_graph
    supplied: dict[str, str | None] = {}
    def recording_validate(root: Path, value: ProjectManifest, **kwargs: object):
        supplied.update(kwargs)
        return real_validate(root, value, **kwargs)
    monkeypatch.setattr("project_knowledge.health.validate_owned_graph", recording_validate)
    state = inspect_project_state(repo, manifest)
    assert supplied == {
        "expected_source_digest": state.current_source_digest,
        "expected_projection_digest": state.current_projection_digest,
    }
    assert state.source_matches and state.projection_matches
```

- [ ] **Step 2: Write failing doctor read-only/version/resource diagnostics**

```python
# tests/test_doctor.py
from pathlib import Path
import json

from project_knowledge.doctor import doctor_project
from tests.support import write_manifest_v2
from tests.test_cli import tree_snapshot

def test_doctor_is_repository_read_only_path_free_and_versioned(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("safe\n", encoding="utf-8")
    write_manifest_v2(repo)
    before = tree_snapshot(repo)
    result = doctor_project(
        repo,
        graphify_binary=tmp_path / "fixture-graphify",
        runner=CapabilityFixtureRunner(),
        package_version="9.8.7-test",
    )
    document = result.to_dict()
    assert document["package"] == {
        "version": "9.8.7-test",
        "manifest_schema": 2,
        "ownership_schema": 2,
        "evidence_schema": 1,
        "query_schema": 1,
    }
    assert document["projection"]["safe_file_count"] == 1
    assert str(repo) not in json.dumps(document)
    assert tree_snapshot(repo) == before
    assert not (repo / ".project-knowledge").exists()

def test_doctor_reports_recovery_required_without_touching_corrupt_journal(tmp_path: Path) -> None:
    repo = configured_repository(tmp_path)
    state = repo / ".project-knowledge"
    state.mkdir(mode=0o700)
    journal = state / "init-transaction.json"
    journal.write_text("corrupt caller bytes\n", encoding="utf-8")
    result = doctor_project(repo, runner=CapabilityFixtureRunner(), package_version="test")
    assert [item.code for item in result.diagnostics] == ["init_recovery_required"]
    assert journal.read_text(encoding="utf-8") == "corrupt caller bytes\n"
```

- [ ] **Step 3: Run tests and verify health/doctor contracts fail**

Run: `uv run pytest -q tests/test_health.py tests/test_doctor.py`

Expected: health assertions fail because disabled optional features still produce `partial`, and doctor collection fails because `project_knowledge.doctor` is absent.

- [ ] **Step 4: Implement the schema-v2 classifier and backward fields**

```python
# central contracts in src/project_knowledge/health.py
CoreStatus = Literal["error", "missing", "stale", "partial", "healthy"]
FeatureStatus = Literal["disabled", "available", "unavailable", "misconfigured"]

@dataclass(frozen=True)
class FeatureHealth:
    status: FeatureStatus
    issues: tuple[str, ...] = ()

@dataclass(frozen=True)
class TrustHealth:
    impact: Literal["trusted", "navigation"]
    limitations: tuple[str, ...] = ()

@dataclass(frozen=True)
class KnowledgeState:
    project_id: str
    graph_exists: bool
    graph_valid: bool
    graph_version: str | None
    current_source_digest: str | None
    graph_source_digest: str | None
    current_projection_digest: str | None
    graph_projection_digest: str | None
    features: Mapping[str, FeatureHealth]
    coverage_skips: int = 0
    unapproved_skips: int = 0
    impact_trust: Literal["trusted", "navigation"] = "navigation"
    impact_limitations: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    @property
    def source_matches(self) -> bool:
        return (
            self.current_source_digest is not None
            and self.current_source_digest == self.graph_source_digest
        )
    @property
    def projection_matches(self) -> bool:
        return (
            self.current_projection_digest is not None
            and self.current_projection_digest == self.graph_projection_digest
        )

@dataclass(frozen=True)
class KnowledgeHealth:
    core_status: CoreStatus
    project_id: str
    graph_version: str | None
    source_matches: bool
    projection_matches: bool
    features: Mapping[str, FeatureHealth]
    trust: TrustHealth
    issues: tuple[str, ...]
    warnings: tuple[str, ...]
    schema_version: int = 2
    @property
    def status(self) -> CoreStatus:
        return self.core_status
    @property
    def atlas_available(self) -> bool:
        return self.features["atlas"].status == "available"
    @property
    def registry_matches(self) -> bool:
        return self.features["registry"].status == "available"
    @property
    def impact_analysis_trusted(self) -> bool:
        return self.trust.impact == "trusted"
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "core_status": self.core_status,
            "status": self.core_status,
            "project_id": self.project_id,
            "graph_version": self.graph_version,
            "source_matches": self.source_matches,
            "projection_matches": self.projection_matches,
            "features": {
                name: {"status": value.status, "issues": list(value.issues)}
                for name, value in sorted(self.features.items())
            },
            "trust": {"impact": self.trust.impact, "limitations": list(self.trust.limitations)},
            "issues": list(self.issues),
            "warnings": list(self.warnings),
            "atlas_available": self.atlas_available,
            "registry_matches": self.registry_matches,
            "impact_analysis_trusted": self.impact_analysis_trusted,
        }
```

`assess_health` implements exact precedence `errors -> graph missing/invalid -> source/projection stale -> unapproved coverage error -> approved coverage partial -> healthy`. Policy-denied scope and optional feature state are excluded from that decision. Append unavailable/misconfigured feature issue codes to `warnings`; append `impact_evidence_incomplete` and the validated impact limitations when trust is navigation. Deduplicate codes while preserving first occurrence.

`inspect_project_state` calls `inspect_projection`, then calls `validate_owned_graph(output, manifest, expected_source_digest=projection.source_digest, expected_projection_digest=projection.projection_digest)` and compares the returned ownership digests to the same current projection. An unequal digest remains structurally valid but makes the separately computed source/projection match false and core status stale; its returned impact trust is already demoted by the validator. For an absent output set graph existence false. For invalid ownership/artifacts set existence true and valid false without exposing the validator exception. Map manifest intent to disabled feature states unless an explicit already-validated feature state is supplied. For schema v1, graph projection digest may be absent; treat projection matching as true only for a valid schema-v1 owned graph and schema-v1 manifest, while impact remains navigation.

- [ ] **Step 5: Implement deterministic read-only doctor diagnostics**

```python
# src/project_knowledge/doctor.py
@dataclass(frozen=True)
class DoctorDiagnostic:
    code: str
    severity: Literal["error", "warning"]

@dataclass(frozen=True)
class DoctorResult:
    status: Literal["ready", "issues"]
    package: Mapping[str, object]
    manifest: Mapping[str, object]
    projection: Mapping[str, object]
    graphify: Mapping[str, object]
    skill: Mapping[str, object]
    health: Mapping[str, object]
    diagnostics: tuple[DoctorDiagnostic, ...]
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "command": "doctor",
            "status": self.status,
            "package": dict(self.package),
            "manifest": dict(self.manifest),
            "projection": dict(self.projection),
            "graphify": dict(self.graphify),
            "skill": dict(self.skill),
            "health": dict(self.health),
            "diagnostics": [item.__dict__ for item in self.diagnostics],
        }
```

`doctor_project` must lstat/open the repository without creating state, inspect `init-transaction.json` only when the real private state directory already exists, load the manifest, call `inspect_projection`, resolve compatibility, freeze the Graphify test/default path with `resolve_graphify_executable()`, run the prerequisite private capability probe with that object, inspect packaged/installed skill ownership if present, validate artifact provider fields locally, and call health. Every probe subprocess performs the mandatory identity revalidation. The installed version defaults to `importlib.metadata.version("atlasweaver")`; never import `pyproject.toml` or a source constant for it. Package schema constants are `manifest=2`, `ownership=2`, `evidence=1`, `query=1`.

Diagnostics are stable codes only: `init_recovery_required`, `manifest_invalid`, `projection_invalid`, `graphify_unavailable`, `graphify_contract_mismatch`, `agent_skill_unavailable`, `agent_skill_mismatch`, `graph_missing`, `graph_invalid`, `source_stale`, `projection_stale`, `artifact_provider_invalid`. Sort by the explicit subsystem order above, not alphabetically. Absolute paths, secret literals, Graphify stderr, and exception text never enter `DoctorResult`.

- [ ] **Step 6: Run health, doctor, public-release, and legacy CLI tests**

Run: `uv run pytest -q tests/test_health.py tests/test_doctor.py tests/test_public_release.py tests/test_cli.py`

Expected: all selected tests pass. Existing CLI health consumers receive the legacy booleans plus schema-v2 core/feature/trust fields.

- [ ] **Step 7: Commit**

```bash
git add src/project_knowledge/health.py src/project_knowledge/doctor.py tests/test_health.py tests/test_doctor.py tests/test_public_release.py
git commit -m "feat: separate core health from optional features"
```

### Task 9: Immutable deterministic query surface

**Files:**
- Create: `src/project_knowledge/queries.py`
- Create: `tests/test_queries.py`
- Modify: `src/project_knowledge/secrets_scan.py`
- Test: `tests/test_health.py`
- Test: `tests/test_evidence.py`

**Interfaces:**
- Produces: `QueryError(code, message, candidates=())`.
- Produces: `QueryEnvelope(command, trust, result, limitations, schema_version=1)`.
- Produces: `QuerySnapshot(root, validated, document, contract, evidence, health)` context manager.
- Produces: `open_query_snapshot(repo_root, manifest) -> AbstractContextManager[QuerySnapshot]`.
- Produces: `query_nodes(snapshot, term, *, limit=20) -> QueryEnvelope`.
- Produces: `shortest_path(snapshot, source, target, *, max_depth=32) -> QueryEnvelope`.
- Produces: `explain_node(snapshot, node, *, depth=1) -> QueryEnvelope`.
- Produces: `affected_nodes(snapshot, node, *, depth=2, relations=()) -> QueryEnvelope`.
- Produces: frozen `RegistryQueryRequest(command, term, source, target, node, limit, max_depth, depth, relations)` and private `CapturedQueryGraph` for the Task 10 registry adapter.
- Produces: `query_captured_graphs(graphs: tuple[CapturedQueryGraph, ...], request: RegistryQueryRequest) -> QueryEnvelope`; Task 10 re-exports the request and owns the public `query_registry` wrapper.

- [ ] **Step 1: Write failing immutable-snapshot and admission tests**

```python
# tests/test_queries.py
from pathlib import Path
import json
import pytest

from project_knowledge.queries import (
    QueryError,
    affected_nodes,
    explain_node,
    open_query_snapshot,
    query_nodes,
    shortest_path,
)
from tests.support import manifest_v2
from tests.test_cli import tree_snapshot

def test_query_snapshot_is_validated_immutable_and_leaves_no_cache(tmp_path: Path) -> None:
    repo = owned_query_repository(tmp_path, trust="navigation")
    before = tree_snapshot(repo)
    with open_query_snapshot(repo, manifest_v2()) as snapshot:
        assert snapshot.root != repo / "graphify-out"
        assert snapshot.health.core_status == "healthy"
        result = query_nodes(snapshot, "dispatch")
        (repo / "graphify-out/graph.json").write_text("changed live bytes\n", encoding="utf-8")
        assert result.to_dict()["result"]["nodes"][0]["id"] == "dispatch"
    assert not (repo / "graphify-out/memory").exists()
    assert not (repo / "graphify-out/reflections").exists()
    assert tree_snapshot(repo) != before  # only the deliberate test mutation changed live bytes

@pytest.mark.parametrize("status", ["error", "missing", "stale"])
def test_query_refuses_non_admitted_core_health(tmp_path: Path, status: str) -> None:
    repo = repository_with_health(tmp_path, status)
    with pytest.raises(QueryError) as raised:
        with open_query_snapshot(repo, manifest_v2()):
            pass
    assert raised.value.code == f"graph_{status}"

def test_partial_is_admitted_only_for_tracked_approved_coverage(tmp_path: Path) -> None:
    approved = owned_query_repository(tmp_path / "approved", approved_skips=1)
    with open_query_snapshot(approved, manifest_v2()) as snapshot:
        assert snapshot.health.core_status == "partial"
        assert "approved_coverage_omission" in snapshot.validated.impact_limitations
    unapproved = owned_query_repository(tmp_path / "unapproved", unapproved_skips=1)
    with pytest.raises(QueryError, match="unapproved"):
        with open_query_snapshot(unapproved, manifest_v2()):
            pass
```

- [ ] **Step 2: Write failing traversal, ambiguity, trust, and cap tests**

```python
def test_exact_id_wins_and_duplicate_labels_require_explicit_id(tmp_path: Path) -> None:
    repo = owned_query_repository(tmp_path, duplicate_label="Handler")
    with open_query_snapshot(repo, manifest_v2()) as snapshot:
        assert explain_node(snapshot, "handler:one").result["node"]["id"] == "handler:one"
        with pytest.raises(QueryError) as raised:
            explain_node(snapshot, "Handler")
        assert raised.value.code == "ambiguous_node"
        assert raised.value.candidates == ("handler:one", "handler:two")

def test_shortest_path_and_reverse_affected_are_deterministic(tmp_path: Path) -> None:
    repo = owned_query_repository(tmp_path)
    with open_query_snapshot(repo, manifest_v2()) as snapshot:
        path = shortest_path(snapshot, "source", "sink")
        assert [node["id"] for node in path.result["nodes"]] == ["source", "dispatch", "sink"]
        affected = affected_nodes(snapshot, "sink", depth=2, relations=("calls",))
        assert [node["id"] for node in affected.result["nodes"]] == ["dispatch", "source"]
        assert affected.trust == "navigation"
        assert "source_verification_required" in affected.limitations
        assert "unaffected" not in json.dumps(affected.to_dict()).casefold()

@pytest.mark.parametrize(("operation", "expected"), [
    (lambda snapshot: query_nodes(snapshot, "x" * 4097), "query_input_too_large"),
    (lambda snapshot: shortest_path(snapshot, "source", "sink", max_depth=33), "query_depth_exceeded"),
    (lambda snapshot: affected_nodes(snapshot, "sink", depth=9), "query_depth_exceeded"),
    (lambda snapshot: affected_nodes(snapshot, "sink", relations=tuple(str(i) for i in range(17))), "query_relation_cap_exceeded"),
])
def test_query_caps_fail_without_partial_result(tmp_path: Path, operation, expected: str) -> None:
    repo = owned_query_repository(tmp_path)
    with open_query_snapshot(repo, manifest_v2()) as snapshot:
        with pytest.raises(QueryError) as raised:
            operation(snapshot)
    assert raised.value.code == expected

def test_every_rendered_string_is_literal_redacted(tmp_path: Path) -> None:
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    repo = owned_query_repository(tmp_path, injected_label=secret)
    with open_query_snapshot(repo, manifest_v2()) as snapshot:
        document = explain_node(snapshot, "source").to_dict()
    assert secret not in json.dumps(document)
    assert "[REDACTED]" in json.dumps(document)
```

- [ ] **Step 3: Run tests and verify query module is absent**

Run: `uv run pytest -q tests/test_queries.py`

Expected: collection fails with `ModuleNotFoundError: project_knowledge.queries`.

- [ ] **Step 4: Implement descriptor-captured snapshot validation**

```python
# public contracts in src/project_knowledge/queries.py
MAX_QUERY_INPUT_BYTES = 4_096
MAX_QUERY_NODES = 100_000
MAX_QUERY_EDGES = 500_000
MAX_PATH_DEPTH = 32
MAX_AFFECTED_DEPTH = 8
MAX_RESULTS = 100
MAX_NEIGHBORS = 100
MAX_RELATION_FILTERS = 16
MAX_OUTPUT_BYTES = 1_048_576

@dataclass(frozen=True)
class QueryError(Exception):
    code: str
    message: str
    candidates: tuple[str, ...] = ()

@dataclass(frozen=True)
class NodeRecord:
    id: str
    label: str
    attributes: Mapping[str, object]

@dataclass(frozen=True)
class EdgeRecord:
    id: str
    source: str
    target: str
    relation: str
    attributes: Mapping[str, object]

@dataclass(frozen=True)
class QuerySnapshot:
    root: Path
    validated: ValidatedGraph
    document: Mapping[str, object]
    contract: GraphifyCompatibility
    evidence: GraphEvidence | None
    health: KnowledgeHealth
    nodes: Mapping[str, NodeRecord]
    edges: tuple[EdgeRecord, ...]
    outgoing: Mapping[str, tuple[EdgeRecord, ...]]
    incoming: Mapping[str, tuple[EdgeRecord, ...]]

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

@dataclass(frozen=True)
class CapturedQueryGraph:
    registry_key: str
    project_id: str
    graph_payload: bytes = field(repr=False)
    evidence_payload: bytes = field(repr=False)
    contract: GraphifyCompatibility
    trust: Literal["trusted", "navigation"]
    limitations: tuple[str, ...]

@dataclass(frozen=True)
class QueryEnvelope:
    command: Literal["query", "path", "explain", "affected"]
    trust: Literal["trusted", "navigation"]
    result: Mapping[str, object]
    limitations: tuple[str, ...]
    schema_version: int = 1
    def to_dict(self) -> dict[str, object]:
        document = {
            "schema_version": 1, "command": self.command, "trust": self.trust,
            "result": dict(self.result), "limitations": list(self.limitations),
        }
        redacted = _redact_tree(document)
        payload = json.dumps(redacted, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_OUTPUT_BYTES:
            raise QueryError("query_output_too_large", "query output exceeded its cap")
        return redacted
```

`open_query_snapshot` takes `repository_lifecycle_lock` only for capture. Inside it: inspect the current projection, descriptor-open live `graphify-out`, copy only ownership-listed regular artifacts into a fresh mode-0700 temporary root without following links, compare every source inode before/after copy, and call `validate_owned_graph(copy, manifest, expected_source_digest=projection.source_digest, expected_projection_digest=projection.projection_digest)`. Build health from that validated snapshot. Refuse `error`, `missing`, `stale`, and unapproved partial; admit approved partial with its mandatory limitation. Release the lifecycle lock only after validation; keep the private temporary alive until context exit, then remove it. Cleanup failure raises `query_cleanup_failed` with no path.

Strict-load `graph.json` with duplicate-key/non-finite rejection and exactly one edge array (`edges` or `links`). Reject more than 100,000 nodes or 500,000 edges before building indexes. Require unique non-empty string node IDs and adapter-semantic endpoint strings. Parse `GRAPH_EVIDENCE.json` through `parse_graph_evidence` when artifact schema is 2 and build the final-edge index through `index_final_edge_evidence`; never parse native Graphify human output.

Deep-copy every nested attribute document and index into immutable tuples and `MappingProxyType` instances before returning `QuerySnapshot`; a frozen dataclass wrapped around caller-owned dictionaries is not an immutable snapshot.

- [ ] **Step 5: Implement deterministic resolution and traversals**

```python
def _resolve_one(snapshot: QuerySnapshot, value: str) -> str:
    _bounded_input(value)
    if value in snapshot.nodes:
        return value
    folded = value.casefold()
    exact = tuple(sorted(
        node_id for node_id, node in snapshot.nodes.items()
        if node.label.casefold() == folded
    ))
    if len(exact) == 1:
        return exact[0]
    candidates = exact or tuple(sorted(
        node_id for node_id, node in snapshot.nodes.items()
        if folded in node_id.casefold() or folded in node.label.casefold()
    ))[:20]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise QueryError("ambiguous_node", "node selection is ambiguous", candidates)
    raise QueryError("node_not_found", "node was not found")

def query_nodes(snapshot: QuerySnapshot, term: str, *, limit: int = 20) -> QueryEnvelope:
    _bounded_input(term)
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise QueryError("query_result_cap_invalid", "result limit is invalid")
    folded = term.casefold()
    ranked = sorted(
        snapshot.nodes.values(),
        key=lambda node: (
            0 if node.id == term else 1 if node.label.casefold() == folded
            else 2 if node.label.casefold().startswith(folded) else 3,
            node.label.casefold(), node.id,
        ),
    )
    matches = [node for node in ranked if folded in node.id.casefold() or folded in node.label.casefold()]
    return _envelope(snapshot, "query", {"nodes": [_node_view(node) for node in matches[:limit]]})
```

Implement shortest path as deterministic BFS over adjacency lists sorted by `(relation, neighbor_id, final_edge_id)`, stopping at 32 hops and 100 returned nodes. `explain` performs breadth layers of depth one or two and caps each node's sorted neighbors at 100. `affected` follows incoming edges for directed contracts and both sides for undirected contracts, applies up to 16 exact relation filters, caps depth at eight and results at 100, and sorts each layer by node ID. When trust is `trusted`, join each returned edge to exact final-edge evidence and include relation/confidence/source file/line/column. When navigation, include `source_verification_required`; if traversal is empty add `no_path_found_not_proof_of_no_impact`. Never emit the word “unaffected.”

`_redact_tree` recursively accepts only JSON scalar/list/string-keyed mapping output, calls `redact_literals` on every key and string, rejects non-finite numbers, and enforces `MAX_OUTPUT_BYTES` after serialization. `_bounded_input` checks non-empty strict strings and UTF-8 byte length, not Python character count.

`query_captured_graphs` is the in-memory adapter used only after Task 10 has validated and captured registry entries. It validates the request as a closed command union: `query` requires only `term`; `path` requires only `source`/`target`; `explain` and `affected` require only `node`; non-applicable non-default fields are rejected. It strict-parses every captured graph/evidence payload through the same loaders, applies the node/edge caps to the aggregate before allocating indexes, and rewrites local IDs to `<registry-key>::<local-id>` before combining sorted projects. Exact namespaced IDs win; a label or local ID found in multiple projects raises `ambiguous_node` with sorted namespaced candidates. It delegates to the four existing traversals, returns one normal `QueryEnvelope`, and sets trust to navigation if any participating graph is navigation while unioning limitations in sorted order. It never reads registry/live files and never accepts ownership claims from the request.

- [ ] **Step 6: Run queries with health/evidence regressions**

Run: `uv run pytest -q tests/test_queries.py tests/test_health.py tests/test_evidence.py tests/test_artifacts.py`

Expected: all selected tests pass. Query execution does not create `memory`, `reflections`, caches, or any other live graph entry.

- [ ] **Step 7: Commit**

```bash
git add src/project_knowledge/queries.py src/project_knowledge/secrets_scan.py tests/test_queries.py tests/test_health.py tests/test_evidence.py
git commit -m "feat: add immutable bounded graph queries"
```

### Task 10: Atomic per-user registry and Graphify compatibility projection

**Files:**
- Create: `src/project_knowledge/registry.py`
- Create: `tests/test_registry.py`
- Modify: `src/project_knowledge/locking.py`
- Modify: `src/project_knowledge/graphify.py`
- Modify: `src/project_knowledge/health.py`
- Test: `tests/test_lifecycle_locking.py`

**Interfaces:**
- Produces: `RegistryError(code: str, message: str)` with stable path-free codes.
- Produces: `RegistryStatus(status: Literal["disabled", "missing", "stale", "mismatch", "current"], issues=())`.
- Produces: `RegistrySyncResult(status: Literal["synced", "unchanged"], key, generation, graph_digest)`.
- Produces: immutable `RegistrySnapshotEntry`, `RegistrySnapshot`, and re-exported `RegistryQueryRequest` contracts for fleet readers.
- Produces: `registry_status(repo_root, manifest, *, user_root=None) -> RegistryStatus` with no writes or lock-file creation.
- Produces: `registry_sync(repo_root, manifest, *, user_root=None, runner=None, graphify_binary=None, fs=REAL_REGISTRY_FS) -> RegistrySyncResult`; runner/binary/filesystem are injected library-test seams, the binary is immediately frozen as `ResolvedGraphifyExecutable`, and no public CLI override is added.
- Produces: `capture_registry_snapshot(project_uids, *, require_graphify_projection, user_root=None) -> RegistrySnapshot` as an atomic, bounded, read-only capture.
- Produces: `query_registry(snapshot, request) -> QueryEnvelope` over only the captured bytes.
- Produces: canonical registry schema 1 keyed only by `atlasweaver/<project_uid>`.
- Consumes: lifecycle lock, descriptor lock, `inspect_projection`, `validate_owned_graph`, compatibility-owned `render_graphify_global_add`, official Graphify `global add` in an isolated temporary HOME, and stable executable resolution.

- [ ] **Step 1: Write failing opt-in, status, namespace, and no-local-graph-mutation tests**

```python
# tests/test_registry.py
from dataclasses import replace
from pathlib import Path
import json

from project_knowledge.models import FeatureIntent
from project_knowledge.registry import registry_status, registry_sync
from tests.support import DEMO_UID, manifest_v2
from tests.test_cli import tree_snapshot

def enabled_manifest():
    return manifest_v2(features=FeatureIntent(registry="enabled"))

def test_disabled_registry_is_read_only_and_creates_no_user_state(tmp_path: Path) -> None:
    repo = owned_registry_repository(tmp_path)
    user_root = tmp_path / "user"
    status = registry_status(repo, manifest_v2(), user_root=user_root)
    assert status.status == "disabled"
    assert not user_root.exists()

def test_sync_writes_uid_namespace_and_status_becomes_current(tmp_path: Path) -> None:
    repo = owned_registry_repository(tmp_path)
    user_root = tmp_path / "user"
    before = tree_snapshot(repo / "graphify-out")
    result = registry_sync(
        repo, enabled_manifest(), user_root=user_root,
        runner=GlobalFixtureRunner(), graphify_binary=fixture_graphify(tmp_path),
    )
    assert result.status == "synced"
    assert result.key == f"atlasweaver/{DEMO_UID}"
    registry = json.loads((user_root / "atlasweaver/registry.json").read_text())
    assert list(registry["entries"]) == [result.key]
    assert registry["entries"][result.key]["project_id"] == "demo"
    assert registry_status(repo, enabled_manifest(), user_root=user_root).status == "current"
    assert tree_snapshot(repo / "graphify-out") == before

def test_source_change_makes_registry_stale_without_mutation(tmp_path: Path) -> None:
    repo = owned_registry_repository(tmp_path)
    user_root = tmp_path / "user"
    registry_sync(repo, enabled_manifest(), user_root=user_root,
                  runner=GlobalFixtureRunner(), graphify_binary=fixture_graphify(tmp_path))
    before = tree_snapshot(user_root)
    (repo / "src/app.py").write_text("changed\n", encoding="utf-8")
    assert registry_status(repo, enabled_manifest(), user_root=user_root).status == "stale"
    assert tree_snapshot(user_root) == before
```

- [ ] **Step 2: Write failing journal, unmanaged-state, lock, and recovery tests**

```python
@pytest.mark.parametrize("boundary", [
    "copy-project-snapshot", "validate-project-snapshot", "write-atlas-registry",
    "build-graphify-projection",
    "backup-graphify-graph", "backup-graphify-manifest", "promote-graphify-graph",
    "promote-graphify-manifest", "commit-generation",
])
def test_registry_transaction_recovers_every_interruption(
    tmp_path: Path, boundary: str
) -> None:
    repo = owned_registry_repository(tmp_path)
    user_root = tmp_path / "user"
    with pytest.raises(RegistryError):
        registry_sync(
            repo, enabled_manifest(), user_root=user_root,
            runner=GlobalFixtureRunner(), graphify_binary=fixture_graphify(tmp_path),
            fs=FailingRegistryFileSystem(boundary),
        )
    recovered = registry_sync(
        repo, enabled_manifest(), user_root=user_root,
        runner=GlobalFixtureRunner(), graphify_binary=fixture_graphify(tmp_path),
    )
    assert recovered.status in {"synced", "unchanged"}
    assert registry_status(repo, enabled_manifest(), user_root=user_root).status == "current"

def test_status_reports_journal_as_mismatch_but_remains_read_only(tmp_path: Path) -> None:
    repo = owned_registry_repository(tmp_path)
    user_root = prepared_interrupted_registry(tmp_path)
    before = tree_snapshot(user_root)
    status = registry_status(repo, enabled_manifest(), user_root=user_root)
    assert status.status == "mismatch"
    assert "registry_recovery_required" in status.issues
    assert tree_snapshot(user_root) == before

def test_first_sync_refuses_unmanaged_graphify_files(tmp_path: Path) -> None:
    repo = owned_registry_repository(tmp_path)
    user_root = tmp_path / "user"
    graphify = user_root / ".graphify"
    graphify.mkdir(parents=True)
    (graphify / "global-graph.json").write_text("caller owned\n", encoding="utf-8")
    with pytest.raises(RegistryError) as raised:
        registry_sync(repo, enabled_manifest(), user_root=user_root,
                      runner=GlobalFixtureRunner(), graphify_binary=fixture_graphify(tmp_path))
    assert raised.value.code == "registry_unmanaged_state"
    assert (graphify / "global-graph.json").read_text() == "caller owned\n"

def test_two_repository_syncs_serialize_on_one_descriptor_lock(tmp_path: Path) -> None:
    first, second = two_owned_registry_repositories(tmp_path)
    runner = ConcurrencyDetectingGlobalRunner()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda repo: registry_sync(repo, manifest_for(repo), user_root=tmp_path / "user",
                                       runner=runner, graphify_binary=fixture_graphify(tmp_path)),
            (first, second),
        ))
    assert all(item.status == "synced" for item in results)
    assert runner.maximum_concurrency == 1

def test_registry_projection_uses_registry_owned_global_add_contract(tmp_path: Path) -> None:
    repo = owned_registry_repository(tmp_path)
    runner = GlobalFixtureRunner()
    registry_sync(
        repo, enabled_manifest(), user_root=tmp_path / "user", runner=runner,
        graphify_binary=fixture_graphify(tmp_path),
    )
    assert [call.operation for call in runner.rendered_calls] == ["global-add"]
    assert runner.rendered_calls[0].canonical_argv == (
        "<graphify>", "global", "add", "<registry-graph>",
        "--as", "<registry-key>",
    )

def test_capture_and_query_registry_are_atomic_read_only_and_uid_ordered(
    tmp_path: Path,
) -> None:
    first, second = two_owned_registry_repositories(tmp_path)
    user_root = tmp_path / "user"
    for repo in (first, second):
        registry_sync(
            repo, manifest_for(repo), user_root=user_root,
            runner=GlobalFixtureRunner(), graphify_binary=fixture_graphify(tmp_path),
        )
    requested = (manifest_for(second).project_uid, manifest_for(first).project_uid)
    assert all(value is not None for value in requested)
    before = tree_snapshot(user_root)
    snapshot = capture_registry_snapshot(
        requested, require_graphify_projection=True, user_root=user_root,
    )
    assert tuple(item.project_uid for item in snapshot.entries) == requested
    result = query_registry(
        snapshot, RegistryQueryRequest(command="query", term="dispatch", limit=20),
    )
    assert all("::" in item["id"] for item in result.result["nodes"])
    assert tree_snapshot(user_root) == before

@pytest.mark.parametrize("failure", [
    "duplicate_uid", "missing_uid", "journal", "entry_digest", "owned_artifact",
    "evidence", "graphify_projection", "aggregate_cap", "busy",
])
def test_registry_capture_fails_closed_with_stable_code(
    tmp_path: Path, failure: str
) -> None:
    user_root, requested = prepared_registry_capture_failure(tmp_path, failure)
    with pytest.raises(RegistryError) as raised:
        capture_registry_snapshot(
            requested, require_graphify_projection=True, user_root=user_root,
        )
    assert raised.value.code == {
        "duplicate_uid": "registry_snapshot_mismatch",
        "missing_uid": "registry_snapshot_missing",
        "journal": "registry_snapshot_stale",
        "entry_digest": "registry_snapshot_mismatch",
        "owned_artifact": "registry_snapshot_mismatch",
        "evidence": "registry_snapshot_mismatch",
        "graphify_projection": "registry_snapshot_mismatch",
        "aggregate_cap": "registry_snapshot_too_large",
        "busy": "registry_snapshot_busy",
    }[failure]
```

- [ ] **Step 3: Run tests and verify registry module is absent**

Run: `uv run pytest -q tests/test_registry.py`

Expected: collection fails with `ModuleNotFoundError: project_knowledge.registry`.

- [ ] **Step 4: Implement strict canonical registry reads and status**

```python
# src/project_knowledge/registry.py
REGISTRY_SCHEMA_VERSION = 1
REGISTRY_NAME = "registry.json"
REGISTRY_LOCK = "registry.lock"
REGISTRY_JOURNAL = "registry-transaction.json"
MAX_REGISTRY_SNAPSHOT_BYTES = 268_435_456
GRAPHIFY_PROJECTION_DOMAIN = b"atlasweaver-graphify-projection-v1\0"

@dataclass(frozen=True)
class RegistryError(Exception):
    code: str
    message: str

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
class RegistryStatus:
    status: Literal["disabled", "missing", "stale", "mismatch", "current"]
    issues: tuple[str, ...] = ()

@dataclass(frozen=True)
class RegistrySyncResult:
    status: Literal["synced", "unchanged"]
    key: str
    generation: int
    graph_digest: str

def registry_key(manifest: ProjectManifest) -> str:
    if manifest.schema_version != 2 or manifest.project_uid is None:
        raise RegistryError("manifest_migration_required", "portable identity is required")
    return f"atlasweaver/{manifest.project_uid}"
```

The canonical AtlasWeaver document is exact, duplicate-key-free JSON:

```json
{
  "schema_version": 1,
  "generation": 3,
  "graphify_projection": {
    "global_graph_sha256": "<64 hex>",
    "global_manifest_sha256": "<64 hex>"
  },
  "entries": {
    "atlasweaver/4ed9af24-5aa2-4eac-8d0a-3f622cc74948": {
      "project_uid": "4ed9af24-5aa2-4eac-8d0a-3f622cc74948",
      "project_id": "demo",
      "graphify_version": "0.9.48",
      "adapter_id": "graphify-0.9.48",
      "source_digest": "<64 hex>",
      "projection_digest": "<64 hex>",
      "graph_digest": "<64 hex>",
      "build_epoch": 1,
      "manifest_sha256": "<64 hex>",
      "snapshot": "snapshots/4ed9af24-5aa2-4eac-8d0a-3f622cc74948/<graph digest>"
    }
  }
}
```

`registry_status` is strictly read-only: if registry is disabled return immediately; if state is absent return missing; if a journal exists return mismatch plus `registry_recovery_required`; strict-load the registry and descriptor-hash both Graphify files; inspect current projection and owned graph. Return stale only for current-source/projection drift. Return mismatch for schema, UID, alias, ownership, digest, snapshot, generation, or Graphify projection disagreement. Return current only when every binding matches. It never opens a creating lock.

- [ ] **Step 5: Implement sync, isolated official projection, and journal recovery**

`registry_sync` requires manifest schema 2, `features.registry == "enabled"`, core health exactly healthy, source/projection current, and a validated owned graph. It takes the repository lifecycle lock, then opens the mode-0700 per-user AtlasWeaver root and a descriptor-relative `ExclusiveDescriptorLock`. Under that lock it recovers a valid prior journal, descriptor-copies the complete ownership-listed generation plus its ownership manifest into `snapshots/<uid>/<graph_digest>/owned/`, validates that completed private `owned/` copy with `validate_owned_graph`, builds the prospective canonical registry generation, then rebuilds both Graphify files from every sorted registry entry in an isolated mode-0700 temporary HOME. The registry entry points to the generation directory; the Graphify projector passes only `<snapshot>/owned/graph.json` to `render_graphify_global_add`.

The projector resolves one immutable executable before projection and obtains every official command only from the compatibility registry while holding the global lock:

```python
rendered = render_graphify_global_add(
    contract,
    binary=executable.path,
    graph=private_snapshot,
    registry_key=registry_key,
)
run_graphify_operation(
    runner,
    executable,
    rendered.argv,
    env={"HOME": str(isolated_home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": os.defpath},
    timeout=60.0,
    operation=rendered.operation,
)
```

The same `executable` object is passed for every sorted registry entry; `run_graphify_operation()` no-follow revalidates it immediately before every `global add`. Add a multi-entry regression whose fake runner replaces the launcher after the first successful add: the second add must fail `graphify_executable_changed`, the journal recovery path preserves the prior committed registry generation, and no second runner call occurs.

`render_graphify_global_add` is also the sole validator for the key. It admits only canonical lowercase RFC 9562 UUIDv4 keys matching `atlasweaver/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}` and rechecks `UUID(suffix).version == 4` plus `str(UUID(suffix)) == suffix`. The core never constructs a second Graphify command template and never forwards `--as` from user input.

Capture the resulting isolated `.graphify/global-graph.json` and `global-manifest.json` descriptor-safely. Validate that the manifest has exactly all registry keys, each `source_path` is confined to the isolated private projection root, each 16-hex `source_hash` equals the corresponding full graph SHA-256 prefix, and the global graph contains only correctly prefixed `key::local-id` nodes plus explicitly merged external nodes allowed by 0.9.48. Do not import Graphify internals or use its live HOME as a build target.

The mode-0600 journal records transaction ID, phase, old/new SHA-256 and inode for the canonical registry, project snapshot directory and bound files, Graphify graph, and Graphify manifest, plus descriptor-confined backup/new names. Phase order is `prepared`, `snapshot_installed`, `atlas_registry_installed`, `graphify_graph_installed`, `graphify_manifest_installed`, `committed`. After every exclusive write/rename fsync the file and parent. Recovery rolls forward when all new hashes exist or restores all exact backups otherwise. An absent/corrupt journal never authorizes deletion. First sync refuses pre-existing Graphify global files unless the Atlas registry already binds their exact hashes; it never silently takes ownership of unmanaged state.

After commit, reread/rehash all three canonical files before returning `synced`; an identical entry/projection returns `unchanged`. Cleanup old transaction-owned backups/snapshots only after commit and exact ownership validation. Registry failure never modifies `repo/graphify-out`.

- [ ] **Step 6: Add atomic read-only fleet capture and bounded registry queries**

Each immutable generation directory also contains canonical `render_manifest_v2(manifest)` bytes as sibling `manifest.yaml`; add `manifest_sha256` to its registry entry and bind that file in the transaction journal. This lets a later registry reader reconstruct the exact strict `ProjectManifest` needed by `validate_owned_graph` without consulting a live repository, while keeping configuration outside the validator's closed `owned/` artifact set. The projector still receives only `owned/graph.json`.

Implement `capture_registry_snapshot(project_uids: tuple[UUID, ...], *, require_graphify_projection: bool, user_root: Path | None = None) -> RegistrySnapshot` and the following exact query wrapper in `registry.py`, re-exporting `RegistryQueryRequest` imported from `queries.py`:

```python
def query_registry(
    snapshot: RegistrySnapshot,
    request: RegistryQueryRequest,
) -> QueryEnvelope:
    graphs = tuple(
        CapturedQueryGraph(
            registry_key=entry.registry_key,
            project_id=entry.project_id,
            graph_payload=entry.graph_payload,
            evidence_payload=entry.evidence_payload,
            contract=resolve_graphify_compatibility(entry.graphify_version),
            trust=entry.impact_trust,
            limitations=entry.impact_limitations,
        )
        for entry in snapshot.entries
    )
    return query_captured_graphs(graphs, request)
```

Extend `ExclusiveDescriptorLock` with keyword-only `create: bool = True` and `shared: bool = False`. With `create=False`, open only an already-existing descriptor-relative regular file using `O_NOFOLLOW`, enforce the same owner/mode/inode checks, and never create or chmod it; with `shared=True`, use `LOCK_SH | LOCK_NB`. Capture opens the already-existing user root and registry lock with `create=False, shared=True`; absence is `registry_snapshot_missing`, timeout is `registry_snapshot_busy`, and it creates no path.

Require `project_uids` to be a tuple of canonical UUIDv4 objects, non-empty and duplicate-free. Under the shared global lock, strict-read the committed registry and reject a journal/non-current generation. For each requested UID in caller order, locate exactly one entry, descriptor-copy its generation into a private mode-0700 temporary directory under the aggregate cap, parse its bound captured `manifest.yaml` bytes with `load_manifest_payload`, and call `validate_owned_graph(private_generation / "owned", manifest, expected_source_digest=entry.source_digest, expected_projection_digest=entry.projection_digest)`. Require artifact schema 2 and exact project UID, adapter, graph/evidence/source/projection/trust/limitations equality with the registry entry. Return graph and evidence bytes only after this validation and remove the private directory before returning; no live path or ownership payload enters `RegistrySnapshotEntry`.

Before and after all captures, reread and hash the registry and, when `require_graphify_projection=True`, both Graphify projection files from verified descriptors. Any inode/hash/generation drift is `registry_snapshot_stale` or `registry_snapshot_mismatch`; the function never retries across generations. Enforce 268,435,456 aggregate bytes before retaining payloads and return `registry_snapshot_too_large` without a partial snapshot. `registry_digest` is SHA-256 of the exact canonical registry bytes. `graphify_projection_digest` is `sha256(GRAPHIFY_PROJECTION_DOMAIN + canonical_json({"global_graph_sha256": ..., "global_manifest_sha256": ...}))`; when projection verification is not required, compute it from the committed registry hashes without opening the Graphify files.

`query_registry` performs no filesystem access and delegates only to Task 9's immutable parser/traversal primitive. Map its failures to existing `query_*` codes. Add the capture/query tests from Steps 1–2 to the Task 10 focused run.

- [ ] **Step 7: Wire registry feature state into health**

When manifest registry is disabled, pass `FeatureHealth("disabled")`. When enabled, translate current to `FeatureHealth("available")`, missing/stale/mismatch to `FeatureHealth("unavailable", ("registry_<status>",))`. This warning never changes `core_status`. Do not make health call a mutating recovery path.

- [ ] **Step 8: Run registry, health, lock, and subprocess tests**

Run: `uv run pytest -q tests/test_registry.py tests/test_health.py tests/test_lifecycle_locking.py tests/test_graphify_adapter.py`

Expected: all selected tests pass; status is byte-for-byte read-only, sync is recoverable, and managed global calls never overlap.

- [ ] **Step 9: Commit**

```bash
git add src/project_knowledge/registry.py src/project_knowledge/locking.py src/project_knowledge/graphify.py src/project_knowledge/health.py tests/test_registry.py tests/test_lifecycle_locking.py tests/test_health.py
git commit -m "feat: add atomic project knowledge registry"
```

### Task 11: High-level CLI and lifecycle-lock routing

**Files:**
- Create: `tests/test_cli_adoption.py`
- Modify: `src/project_knowledge/cli.py`
- Modify: `tests/test_cli.py`
- Test: `tests/test_lifecycle.py`
- Test: `tests/test_queries.py`
- Test: `tests/test_registry.py`
- Test: `tests/test_coverage.py`

**Interfaces:**
- Produces root `--version` from installed package metadata.
- Produces top-level commands `init`, `manifest-migrate`, `doctor`, `refresh`, `query`, `path`, `explain`, `affected`, `coverage`, `registry-status`, and `registry-sync` while preserving the ten legacy commands.
- Produces: `CliResponse(document: dict[str, object], exit_code: int = 0)`.
- Produces stable error codes and path/literal-free JSON for every high-level command.
- Produces: `inspect_candidate_schema(candidate: Path) -> Literal[1, 2]` so retained low-level validate/promote can supply an ownership epoch only for evidence-bound candidates.
- Extends retained read-only `preflight` with `--backend`, `--model`, and `--deep`; it reads the trusted generic credential only from `ATLASWEAVER_BACKEND_TOKEN`, binds it through `bind_semantic_backend_credential`, runs no semantic extraction, and emits no secret/name.

- [ ] **Step 1: Write failing parser/version/no-binary-override tests**

```python
# tests/test_cli_adoption.py
from pathlib import Path
import argparse
import importlib.metadata
from subprocess import CompletedProcess

from project_knowledge.cli import build_parser, main

def invoke_cli(repo: Path, capsys, *arguments: str) -> CompletedProcess[str]:
    argv = [arguments[0], "--repo", str(repo), *arguments[1:]]
    exit_code = main(argv)
    captured = capsys.readouterr()
    return CompletedProcess(argv, exit_code, captured.out, captured.err)

def command_choices() -> set[str]:
    parser = build_parser()
    return set(next(
        action.choices for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ))

def test_parser_exposes_high_level_and_legacy_commands() -> None:
    assert command_choices() == {
        "init", "manifest-migrate", "doctor", "refresh", "query", "path",
        "explain", "affected", "coverage", "registry-status", "registry-sync",
        "detect", "preflight", "stage", "adapt", "scan-secrets", "validate",
        "promote", "atlas-prepare", "atlas-promote", "health",
    }

def test_high_level_refresh_has_no_graphify_binary_or_arbitrary_flag() -> None:
    help_text = build_parser().format_help()
    refresh = build_parser()._subparsers._group_actions[0].choices["refresh"]
    options = {value for action in refresh._actions for value in action.option_strings}
    assert "--graphify-binary" not in options
    assert "--graphify-flag" not in options
    assert {"--backend", "--model", "--deep", "--code-only", "--repo", "--json"} <= options

def test_trusted_preflight_has_semantic_shape_but_no_token_argv() -> None:
    preflight = build_parser()._subparsers._group_actions[0].choices["preflight"]
    options = {value for action in preflight._actions for value in action.option_strings}
    assert {"--backend", "--model", "--deep"} <= options
    assert not {
        "--token", "--credential", "--api-key", "--graphify-binary",
        "--binary", "--executable",
    } & options

def test_root_version_comes_from_installed_metadata(monkeypatch, capsys) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.8.7-test")
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == "project-knowledge 9.8.7-test"
```

- [ ] **Step 2: Write failing init/migrate/doctor/refresh JSON contracts**

```python
def test_init_preview_then_apply_is_explicit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    preview = run_cli(repo, "init", "--project-id", "demo", "--include-root", "src", "--json")
    assert preview.returncode == 0
    document = payload(preview)
    assert document["status"] == "preview"
    assert document["project_uid"] == "<generated-on-apply>"
    assert not (repo / ".graphify-project.yaml").exists()
    applied = run_cli(repo, "init", "--project-id", "demo", "--include-root", "src", "--apply", "--json")
    assert applied.returncode == 0
    assert payload(applied)["status"] == "initialized"

def test_doctor_and_refresh_use_high_level_stable_envelopes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = configured_v2_repository(tmp_path)
    monkeypatch.setattr("project_knowledge.cli.doctor_project", doctor_fixture)
    monkeypatch.setattr("project_knowledge.cli.refresh_project", refresh_fixture)
    doctor = invoke_cli(repo, capsys, "doctor", "--json")
    assert payload(doctor)["command"] == "doctor"
    refresh = invoke_cli(repo, capsys, "refresh", "--code-only", "--json")
    assert payload(refresh) == {
        "schema_version": 1, "command": "refresh", "status": "refreshed",
        "project_id": "demo", "source_digest": "a" * 64,
        "projection_digest": "b" * 64, "graph_digest": "c" * 64,
        "build_epoch": 1, "core_status": "healthy", "trust": "navigation",
        "limitations": ["pre_dedup_edge_projection_unavailable"],
    }

def test_promoted_but_stale_is_emitted_and_returns_nonzero(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = configured_v2_repository(tmp_path)
    monkeypatch.setattr("project_knowledge.cli.refresh_project", stale_refresh_fixture)
    result = invoke_cli(repo, capsys, "refresh", "--code-only", "--json")
    assert result.returncode == 3
    assert payload(result)["status"] == "promoted_but_stale"

def test_trusted_semantic_preflight_binds_generic_token_without_extraction_or_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = configured_v2_repository(tmp_path)
    token = "generic-workflow-secret-value"
    monkeypatch.setenv("ATLASWEAVER_BACKEND_TOKEN", token)
    runner = CapabilityFixtureRunner()
    monkeypatch.setattr("project_knowledge.cli.SubprocessCommandRunner", lambda: runner)
    result = invoke_cli(
        repo, capsys, "preflight", "--backend", "openai", "--model", "gpt-5",
        "--deep", "--json",
    )
    document = payload(result)
    assert document == {
        "schema_version": 1,
        "command": "preflight",
        "status": "ready",
        "graphify_version": "0.9.48",
        "backend": "openai",
        "model": "gpt-5",
        "deep": True,
        "credential_bound": True,
    }
    assert runner.semantic_extractions == 0
    assert all("ATLASWEAVER_BACKEND_TOKEN" not in env for env in runner.environments)
    assert token not in result.stdout + result.stderr

def test_preflight_without_backend_does_not_read_generic_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = configured_v2_repository(tmp_path)
    monkeypatch.setenv("ATLASWEAVER_BACKEND_TOKEN", "must-remain-unread")
    monkeypatch.setattr(
        "project_knowledge.cli.bind_semantic_backend_credential",
        lambda *args, **kwargs: pytest.fail("binder called without backend"),
    )
    monkeypatch.setattr(
        "project_knowledge.cli.SubprocessCommandRunner", lambda: CapabilityFixtureRunner()
    )
    result = invoke_cli(repo, capsys, "preflight", "--json")
    assert payload(result)["credential_bound"] is False

def test_semantic_preflight_requires_bound_credential_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = configured_v2_repository(tmp_path)
    monkeypatch.delenv("ATLASWEAVER_BACKEND_TOKEN", raising=False)
    runner = CapabilityFixtureRunner()
    monkeypatch.setattr("project_knowledge.cli.SubprocessCommandRunner", lambda: runner)
    result = invoke_cli(repo, capsys, "preflight", "--backend", "openai", "--json")
    assert payload(result)["error"]["code"] == "semantic_backend_required"
    assert runner.calls == []

def test_refresh_maps_trusted_generic_token_but_local_refresh_keeps_normal_ambient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = configured_v2_repository(tmp_path)
    captured: list[dict[str, str] | None] = []
    def recording_refresh(*args: object, ambient=None, **kwargs: object):
        captured.append(None if ambient is None else dict(ambient))
        return refresh_fixture(*args, **kwargs)
    monkeypatch.setattr("project_knowledge.cli.refresh_project", recording_refresh)

    monkeypatch.setenv("ATLASWEAVER_BACKEND_TOKEN", "trusted-generic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-local-secret")
    first = invoke_cli(repo, capsys, "refresh", "--backend", "openai", "--json")
    assert first.returncode == 0
    assert captured[-1] is not None
    assert set(captured[-1]) == {
        "HOME", "LANG", "LC_ALL", "PATH", "OPENAI_API_KEY",
    }
    assert captured[-1]["OPENAI_API_KEY"] == "trusted-generic-secret"
    assert "ATLASWEAVER_BACKEND_TOKEN" not in captured[-1]

    monkeypatch.delenv("ATLASWEAVER_BACKEND_TOKEN")
    second = invoke_cli(repo, capsys, "refresh", "--backend", "openai", "--json")
    assert second.returncode == 0
    assert captured[-1] is not None
    assert captured[-1]["OPENAI_API_KEY"] == "unrelated-local-secret"
    assert "ATLASWEAVER_BACKEND_TOKEN" not in captured[-1]
```

- [ ] **Step 3: Write failing query, coverage, registry, and lock-routing contracts**

```python
def test_query_commands_normalize_to_stable_envelope(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = configured_v2_repository(tmp_path)
    monkeypatch.setattr("project_knowledge.cli.open_query_snapshot", query_snapshot_fixture)
    cases = (
        (("query", "dispatch"), "query"),
        (("path", "source", "sink"), "path"),
        (("explain", "dispatch"), "explain"),
        (("affected", "sink", "--depth", "2", "--relation", "calls"), "affected"),
    )
    for arguments, command in cases:
        result = invoke_cli(repo, capsys, *arguments, "--json")
        assert result.returncode == 0
        document = payload(result)
        assert document["command"] == command
        assert document["trust"] == "navigation"

def test_coverage_and_registry_mutations_require_explicit_opt_in(tmp_path: Path) -> None:
    repo = configured_v2_repository(tmp_path)
    preview = run_cli(
        repo, "coverage", "approve", "--file", "src/app.py",
        "--reason", "not_represented_by_graphify", "--rationale", "reviewed", "--json",
    )
    assert payload(preview)["status"] == "preview"
    assert not (repo / ".atlasweaver-coverage.yaml").exists()
    disabled = run_cli(repo, "registry-sync", "--json")
    assert disabled.returncode == 1
    assert payload(disabled)["error"]["code"] == "registry_disabled"

@pytest.mark.parametrize("command", [
    "stage", "adapt", "validate", "promote", "atlas-prepare", "atlas-promote",
])
def test_legacy_graph_affecting_commands_enter_lifecycle_lock(
    tmp_path: Path, command: str, monkeypatch, capsys
) -> None:
    repo, arguments = configured_legacy_invocation(tmp_path, command)
    entered: list[Path] = []
    monkeypatch.setattr(
        "project_knowledge.cli.repository_lifecycle_lock",
        recording_lock(entered),
    )
    result = invoke_cli(repo, capsys, command, *arguments, "--json")
    assert result.returncode == 0
    assert entered == [repo.absolute()]
```

- [ ] **Step 4: Run tests and verify commands are absent**

Run: `uv run pytest -q tests/test_cli.py tests/test_cli_adoption.py`

Expected: parser-set assertions fail because only the ten legacy commands exist.

- [ ] **Step 5: Refactor main so configuration commands do not require a manifest**

```python
# central routing in src/project_knowledge/cli.py
CLI_SCHEMA_VERSION = 1

@dataclass(frozen=True)
class CliResponse:
    document: dict[str, object]
    exit_code: int = 0

CONFIGURATION_COMMANDS = frozenset({"init", "manifest-migrate"})
LOCKED_LEGACY_COMMANDS = frozenset({
    "stage", "adapt", "validate", "promote",
    "atlas-prepare", "atlas-promote",
})

def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        repo = _real_repo(arguments.repo)
        if arguments.command == "init":
            response = _init(arguments, repo)
        else:
            manifest = _manifest(repo)
            if arguments.command == "manifest-migrate":
                response = _manifest_migrate(arguments, repo, manifest)
            elif arguments.command in LOCKED_LEGACY_COMMANDS:
                with repository_lifecycle_lock(repo):
                    response = CliResponse(_dispatch(arguments, repo, manifest))
            else:
                response = _dispatch_response(arguments, repo, manifest)
    except CliFailure as error:
        return _emit_error(arguments, error)
    except DOMAIN_ERROR_TYPES as error:
        return _emit_error(arguments, _map_domain_error(error))
    _emit(response.document, as_json=arguments.as_json)
    return response.exit_code
```

`init` preview never loads a missing manifest or creates state; only its `--apply` path calls the transaction function, which owns the lifecycle lock. Migration, coverage apply, refresh, queries, and registry sync/status call library functions that own precisely scoped locks, so main must not nest a second repository lock around them. Read-only detect/preflight/scan-secrets/health/doctor remain unlocked; query snapshot briefly locks internally.

Add root `--version` using `importlib.metadata.version("atlasweaver")`. Remove the legacy public `preflight --graphify-binary` option and its dispatch plumbing; test injection uses only library keyword seams. Do not add a binary/executable override to refresh, preflight, doctor, registry, coverage, query, install, or fleet-facing commands.

Update the existing `tests/test_cli.py::payload` helper to `payload(result, *, schema_version: int = 1)` and assert the passed version. Existing command tests keep the default; health tests pass `schema_version=2`, because health is the one compatibility command whose document intentionally moves to schema v2.

- [ ] **Step 6: Add exact parsers and handlers**

```python
def add_refresh(sub) -> None:
    parser = _command(sub, "refresh")
    parser.add_argument("--backend")
    parser.add_argument("--model")
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--code-only", action="store_true")

def extend_preflight(parser) -> None:
    parser.add_argument("--backend")
    parser.add_argument("--model")
    parser.add_argument("--deep", action="store_true")

def add_query_commands(sub) -> None:
    query = _command(sub, "query")
    query.add_argument("term")
    query.add_argument("--limit", type=int, default=20)
    path = _command(sub, "path")
    path.add_argument("source")
    path.add_argument("target")
    path.add_argument("--max-depth", type=int, default=32)
    explain = _command(sub, "explain")
    explain.add_argument("node")
    explain.add_argument("--depth", type=int, default=1)
    affected = _command(sub, "affected")
    affected.add_argument("node")
    affected.add_argument("--depth", type=int, default=2)
    affected.add_argument("--relation", action="append", default=[])
```

`init` adds `--project-id`, optional `--project-uid`, repeatable `--include-root`, and `--apply`. `manifest-migrate` adds optional `--project-uid` and `--apply`. `coverage` has required nested subcommand `approve` with `--file`, `--reason`, `--rationale`, and `--apply`. Doctor, registry-status, and registry-sync add no authority-expanding override. Every preview serializes canonical content as UTF-8 strings, never a path.

Handlers call the exact library APIs from Tasks 3, 5, 7–10. Query handlers use `with open_query_snapshot(...)` and then call the matching traversal. `refresh` constructs `RefreshOptions` from only its four admitted flags. `registry-sync` accepts no aliases or provider input. `_emit` must recognize query/doctor nested result structures without converting lists to Python repr in text mode; JSON stays canonical compact JSON.

For trusted reusable-workflow/fleet preflight, reject `--model`/`--deep` without `--backend`. With a backend, read `ATLASWEAVER_BACKEND_TOKEN` exactly once, call `bind_semantic_backend_credential(contract, backend, token)`, remove the generic name, and validate semantic argv shape through `render_graphify_argv` without executing it. Then run only the official capability probe's private code-only smoke. Return backend/model/deep plus `credential_bound: bool`; never return the generic name, canonical credential name, value, rendered argv, path, or diagnostics. Without backend, do not access the generic variable or call the binder. Preflight creates no repository state or semantic stage.

The refresh CLI has a narrow trusted-boundary opt-in while preserving normal local behavior. With `--backend`, remove `ATLASWEAVER_BACKEND_TOKEN` from a private ambient copy. If its value is present, start from `minimal_environment()` (only `HOME`, `LANG`, `LC_ALL`, `PATH`), merge `bind_semantic_backend_credential(...)`'s one-key canonical mapping, and pass only that five-name mapping as `ambient` to `refresh_project`; no unrelated parent variable survives. If the generic value is absent, pass the ordinary ambient copy so local `OPENAI_API_KEY`/provider-specific use still follows `validate_semantic_backend` and `admitted_graphify_environment`. Without `--backend`, never look up the generic name and let `refresh_project` use its normal ambient path. Neither bridge changes argv or response schemas.

- [ ] **Step 7: Map domain failures and retain evidence-aware low-level commands**

Use this stable error mapping and never serialize `str(error)`:

```python
ERROR_CODES: tuple[tuple[type[BaseException], str, str], ...] = (
    (ManifestError, "invalid_manifest", "project manifest is invalid"),
    (CompatibilityError, "graphify_contract_failed", "Graphify contract failed"),
    (PrivacyError, "privacy_invalid", "privacy policy is invalid"),
    (SecretExceptionError, "secret_policy_invalid", "secret exception policy is invalid"),
    (CoverageError, "coverage_invalid", "coverage control is invalid"),
    (StagingError, "staging_failed", "safe input staging failed"),
    (AdapterError, "adaptation_failed", "Graphify adaptation failed"),
    (ArtifactValidationError, "artifact_invalid", "graph artifact is invalid"),
    (GraphifyError, "graphify_contract_failed", "Graphify contract failed"),
    (QueryError, "query_failed", "graph query failed"),
    (RegistryError, "registry_failed", "registry operation failed"),
)
```

For `RefreshError`, `QueryError`, and `RegistryError`, pass through only their documented stable `code`. For `CompatibilityError`, pass through only `semantic_backend_required`; use the table code for every other compatibility failure and every other mapped type. `promoted_but_stale` is a successful result envelope with exit code 3, not an error envelope. Argparse usage remains exit 2.

Add `inspect_candidate_schema` as a strict, capped, descriptor-safe read of candidate `graph.json` returning only schema 1 or 2. Retained low-level validate/promote passes `build_epoch=None` for schema 1 and `_next_build_epoch(repo, manifest)` for schema 2, plus `expected_projection_digest` for schema 2. It never copies a candidate-provided epoch. Adapt without evidence remains schema 1/navigation. Refresh is the only public command that assembles evidence schema 2.

- [ ] **Step 8: Run the complete CLI and domain suites**

Run: `uv run pytest -q tests/test_cli.py tests/test_cli_adoption.py tests/test_lifecycle.py tests/test_queries.py tests/test_registry.py tests/test_coverage.py tests/test_manifest_v2.py`

Expected: all selected tests pass. Legacy command JSON remains stable except intentional health schema-v2 semantics and the expanded parser set.

- [ ] **Step 9: Commit**

```bash
git add src/project_knowledge/cli.py tests/test_cli.py tests/test_cli_adoption.py tests/test_lifecycle.py tests/test_queries.py tests/test_registry.py tests/test_coverage.py
git commit -m "feat: expose safe project knowledge lifecycle"
```

### Task 12: Dogfood schema v2, real Graphify refresh, and operator/agent documentation

**Files:**
- Modify: `.graphify-project.yaml`
- Modify: `examples/.graphify-project.yaml`
- Modify: `.graphifyignore`
- Modify: `examples/.graphifyignore`
- Modify: `tests/test_real_graphify_pipeline.py`
- Modify: `tests/test_public_release.py`
- Modify: `tests/test_skill_contract.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `skills/using-project-knowledge-graphs/SKILL.md`
- Modify: `skills/using-project-knowledge-graphs/references/workflow.md`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: public `init`, `manifest-migrate`, `doctor`, `refresh`, health, query, coverage, and registry commands.
- Produces: AtlasWeaver's tracked manifest-v2 dogfood contract with all optional features disabled.
- Produces: a real official Graphify 0.9.48 evidence-bound lifecycle gate on Python 3.10 and 3.13.
- Produces: exact human and agent instructions that prefer high-level lifecycle commands while retaining a clearly labeled low-level recovery workflow.

- [ ] **Step 1: Write failing public-contract and skill-documentation assertions**

```python
# additions to tests/test_public_release.py
def test_dogfood_and_example_manifests_are_v2_and_optional_by_default() -> None:
    from project_knowledge.manifest import load_manifest
    for root in (ROOT, ROOT / "examples"):
        manifest = load_manifest(root / ".graphify-project.yaml", root)
        assert manifest.schema_version == 2
        assert manifest.project_uid is not None
        assert manifest.features.atlas == "disabled"
        assert manifest.features.registry == "disabled"
        assert manifest.artifacts.provider == "none"

# additions to tests/test_skill_contract.py
def test_skill_prefers_high_level_safe_lifecycle_and_honest_query_trust() -> None:
    combined = skill_text() + "\n" + WORKFLOW.read_text(encoding="utf-8")
    for command in (
        "project-knowledge doctor", "project-knowledge refresh",
        "project-knowledge health", "project-knowledge query",
        "project-knowledge affected", "project-knowledge registry-status",
    ):
        assert command in combined
    lowered = combined.casefold()
    assert "navigation" in lowered
    assert "source verification" in lowered
    assert "never point graphify at the repository root" in lowered
    assert "never commit or push implicitly" in lowered
```

- [ ] **Step 2: Replace the real integration test with high-level refresh**

```python
# replacement lifecycle test in tests/test_real_graphify_pipeline.py
def test_real_graphify_0948_high_level_refresh_health_and_query(tmp_path: Path) -> None:
    require_pinned_graphify()
    repo = tmp_path / "repo"
    write(repo / "src/app.py", "def run():\n    return 1\n")
    write_manifest_v2(
        repo,
        project_id="integration-demo",
        display_name="Integration Demo",
        obsidian_namespace=PurePosixPath("Projects/integration-demo/Generated"),
    )

    refreshed = run_project_knowledge(repo, "refresh", "--code-only")
    assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
    refresh_document = json.loads(refreshed.stdout)
    assert refresh_document["status"] == "refreshed"
    assert refresh_document["trust"] == "navigation"
    assert (repo / "graphify-out/GRAPH_EVIDENCE.json").is_file()

    health = run_project_knowledge(repo, "health")
    assert health.returncode == 0, health.stdout + health.stderr
    health_document = json.loads(health.stdout)
    assert health_document["core_status"] == "healthy"
    assert health_document["source_matches"] is True
    assert health_document["projection_matches"] is True
    assert health_document["trust"]["impact"] == "navigation"

    query = run_project_knowledge(repo, "query", "run")
    assert query.returncode == 0, query.stdout + query.stderr
    assert json.loads(query.stdout)["trust"] == "navigation"

    write(repo / "src/app.py", "def run():\n    return 2\n")
    stale = run_project_knowledge(repo, "health")
    assert json.loads(stale.stdout)["core_status"] == "stale"

def test_real_semantic_refresh_without_backend_fails_before_extraction(tmp_path: Path) -> None:
    require_pinned_graphify()
    repo = tmp_path / "repo"
    write(repo / "docs/guide.md", "# Guide\n")
    write_manifest_v2(repo, include_roots=(PurePosixPath("docs"),))
    result = run_project_knowledge(repo, "refresh")
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "semantic_backend_required"
    assert not (repo / "graphify-out").exists()
    assert not (repo / ".project-knowledge/runs").exists()
```

- [ ] **Step 3: Run tests and verify current v1/docs/manual workflow fail**

Run: `uv run pytest -q tests/test_public_release.py tests/test_skill_contract.py tests/test_real_graphify_pipeline.py`

Expected: manifest-v2 and high-level-command assertions fail before the tracked files are updated.

- [ ] **Step 4: Migrate dogfood and example manifests canonically**

Use these exact intent blocks and unique UUIDv4 values; preserve the current include roots, output, namespace, exclusions, HTML policy, and exact Graphify version:

```yaml
# .graphify-project.yaml identity/additions
schema_version: 2
project_id: atlasweaver
project_uid: 1ed43f8e-e849-4f05-96aa-3d965723f4f9
features:
  atlas: disabled
  registry: disabled
artifacts:
  provider: none
```

```yaml
# examples/.graphify-project.yaml identity/additions
schema_version: 2
project_id: demo-project
project_uid: 77bb46f4-c44a-4fcb-884d-f8ea917f819b
features:
  atlas: disabled
  registry: disabled
artifacts:
  provider: none
```

Render both through `render_manifest_v2`, not hand-sorted YAML. Add `.atlasweaver-coverage.yaml`, `.graphify-secret-exceptions.yaml`, `.project-knowledge/`, `graphify-out/`, build outputs, and editor/runtime state to the reviewed ignore boundaries without negations. The example UUID is illustrative only; README must say new repositories run `init` and never copy an example `project_uid`.

- [ ] **Step 5: Rewrite README, changelog, and agent workflow around safe defaults**

README quick start must be executable and use this order:

```sh
project-knowledge init --repo /path/to/project --project-id my-project --include-root src --apply --json
project-knowledge doctor --repo /path/to/project --json
project-knowledge refresh --repo /path/to/project --code-only --json
project-knowledge health --repo /path/to/project --json
project-knowledge query --repo /path/to/project "authentication" --json
```

Document semantic refresh as `--backend <declared-backend> [--model <model>] [--deep]`, with credentials read only from the compatibility allowlist. Document init/migration preview versus apply, sensitive source scan versus sensitive data deny, exact coverage approval, health v2, navigation trust, post-promotion stale exit 3, registry opt-in/status/sync, and immutable query limits. Keep the manual stage/adapt/validate/promote sequence only under “low-level recovery”; state that it is not the ordinary refresh path.

Update the bundled skill to run `doctor` before substantive graph use, call `health`, use AtlasWeaver query commands rather than native opaque output, require source verification for navigation trust, and never refresh/register/publish/commit/push without exact authority. The workflow reference must include stable error-code handling and explain that disabled optional features do not make core health partial.

Add an `Unreleased` changelog section for manifest/privacy/health/lifecycle/query/registry core work. Do not assign a release version here.

- [ ] **Step 6: Keep CI on the inter-plan compatibility contract**

Preserve the compatibility plan's full-SHA action pins and registry-derived Graphify version. Add mandatory focused invocations for `tests/test_real_graphify_pipeline.py`, `tests/test_cli_adoption.py`, and `tests/test_queries.py` to both Python 3.10/3.13 matrix jobs. Do not add tokens, semantic credentials, registry writes, artifact publication, or an unpinned `pip install`.

- [ ] **Step 7: Run public, skill, real Graphify, and CLI gates**

Run: `uv run pytest -q tests/test_public_release.py tests/test_skill_contract.py tests/test_real_graphify_pipeline.py tests/test_cli.py tests/test_cli_adoption.py`

Expected: all selected tests pass with local Graphify 0.9.48. If the executable is absent locally, only the existing explicit availability skip is allowed; CI must not skip it.

- [ ] **Step 8: Commit**

```bash
git add .graphify-project.yaml examples/.graphify-project.yaml .graphifyignore examples/.graphifyignore README.md CHANGELOG.md skills/using-project-knowledge-graphs/SKILL.md skills/using-project-knowledge-graphs/references/workflow.md tests/test_real_graphify_pipeline.py tests/test_public_release.py tests/test_skill_contract.py .github/workflows/ci.yml
git commit -m "docs: adopt the safe high-level graph lifecycle"
```

### Task 13: Compatibility, migration, rollout, and full verification gate

This is an intentional evidence-only phase gate, not a behavior-producing TDD task. It adds no failing test and creates no commit: any failure routes back to the owning Tasks 1–12, where the fix is implemented test-first and committed before this entire gate is rerun.

**Files:**
- Verify all files changed by compatibility/impact Tasks 1–8 and core Tasks 1–12.
- Do not modify production behavior merely to make this gate pass; return to the owning task for fixes.

**Interfaces:**
- Produces: fresh test/build/smoke evidence for the complete core subsystem.
- Produces: a clean handoff to the artifact/fleet plan; it does not publish, merge, install globally, or delete branches.

- [ ] **Step 1: Run the phase-complete unit and integration suite**

Run: `uv run pytest -q`

Expected: all tests pass. The only locally permitted skip is the pre-existing real-Graphify availability condition; with Graphify 0.9.48 installed, require zero skips.

- [ ] **Step 2: Require the exact real Graphify executable and rerun lifecycle/compatibility**

Run: `graphify --version`

Expected stdout includes exactly `graphify 0.9.48`.

Run: `uv run pytest -q tests/test_real_graphify_pipeline.py tests/test_compatibility.py tests/test_graphify_0_9_48_adapter.py tests/test_evidence.py`

Expected: all selected tests pass with zero skips.

- [ ] **Step 3: Verify both supported Python runtimes**

Run: `uv run --python 3.10 pytest -q`

Expected: all tests pass on Python 3.10.

Run: `uv run --python 3.13 pytest -q`

Expected: all tests pass on Python 3.13.

- [ ] **Step 4: Compile and build distributable inputs**

Run: `uv run python -m compileall -q src tests`

Expected: exit 0 and no output.

Run: `uv build`

Expected: wheel and sdist build successfully. This is build verification only; artifact/fleet later owns packaged agent resources and the final versioned release.

- [ ] **Step 5: Smoke the built wheel in an isolated tool environment**

Run: `uv tool run --from dist/atlasweaver-0.2.2-py3-none-any.whl project-knowledge --version`

Expected: the installed wheel's metadata version is printed, not `0.1.0`, and no source checkout import is required.

Run: `uv tool run --from dist/atlasweaver-0.2.2-py3-none-any.whl project-knowledge --help`

Expected: help contains all high-level and legacy commands from Task 11.

- [ ] **Step 6: Dogfood read-only diagnosis, refresh, health, and queries**

Run: `uv run project-knowledge doctor --repo . --json`

Expected: no manifest/privacy/Graphify contract error; missing optional agent/artifact integrations appear only as warnings.

Run: `uv run project-knowledge refresh --repo . --code-only --json`

Expected: `refreshed` or `unchanged`, artifact schema 2, build epoch at least one, and navigation trust. This writes only ignored `.project-knowledge/` and `graphify-out/` runtime state.

Run: `uv run project-knowledge health --repo . --json`

Expected: `core_status=healthy`, `source_matches=true`, `projection_matches=true`, optional features disabled, impact navigation.

Run: `uv run project-knowledge query --repo . ProjectManifest --json`

Expected: schema-1 query envelope, navigation trust, bounded results.

Run: `uv run project-knowledge affected --repo . ProjectManifest --depth 2 --json`

Expected: schema-1 query envelope with `source_verification_required`; it makes no proof-of-no-impact claim.

- [ ] **Step 7: Verify v1 compatibility and explicit migration in a temporary fixture**

Run: `uv run pytest -q tests/test_manifest.py tests/test_cli.py -k 'v1 or legacy or migrate or low_level'`

Expected: schema-v1 load and retained low-level commands pass; refresh/registry identity remain unavailable until explicit migration; health still emits schema v2.

- [ ] **Step 8: Run static release-safety and repository checks**

Run: `git diff --check`

Expected: no whitespace errors.

Run: `uv run pytest -q tests/test_public_release.py tests/test_skill_contract.py`

Expected: no private context, forbidden package marker, stale workflow, or documentation-contract failure.

Run: `git status --short`

Expected: only intended source/test/docs/workflow changes plus ignored runtime graph state; no secret, private stage, receipt, registry state, build environment, or Obsidian output is tracked.

- [ ] **Step 9: Review rollout invariants and record the handoff**

Verify from fresh output, not memory:

1. Schema v1 loads and low-level commands remain navigation-compatible.
2. Init/migration preview is read-only and apply never clobbers caller files.
3. Sensitive source filenames are scanned; sensitive data/config and every legacy global deny stay denied.
4. Source/projection/control digests agree across receipt, graph, evidence, ownership, health, query, and registry.
5. Graphify 0.9.48 stays navigation-only.
6. Disabled optional features do not lower core health.
7. Read-only doctor/status/query commands create no repository or user-global state.
8. Refresh and registry failure injection preserves previous committed state.
9. Every Graphify subprocess receives one frozen resolved executable and revalidates its path/device/inode/launcher digest immediately before spawn; a between-call mutation yields `graphify_executable_changed` and no child call.
10. High-level commands expose no Graphify binary, arbitrary flag, provider, registry alias, or publication override.
11. No package version, global tool installation, GitHub release, main merge, or branch deletion happened in this plan.

Record the verified commit IDs and command outputs for the artifact/fleet plan. That next plan owns agent resource packaging, deterministic bundles, GitHub transport/workflows, universal fleet operations, the single version bump, clean-wheel reinstall, final dogfood bootstrap, release publication, merge into `main`, and deletion of all other local/remote branches.
