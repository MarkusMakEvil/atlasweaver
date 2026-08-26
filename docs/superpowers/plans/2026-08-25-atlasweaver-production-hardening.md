# AtlasWeaver Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the pinned Graphify workflow end-to-end executable, safely triage false-positive secrets, and expose an honest graph-integrity trust state suitable for universal rollout.

**Architecture:** Staging produces an out-of-tree immutable receipt. A separate failure-atomic adapter verifies that receipt and enriches an allowlisted subset of native Graphify output before the existing validator and transaction layer see it. Secret scanning and graph integrity are focused modules consumed by staging, adaptation, validation, health, and the CLI.

**Tech Stack:** Python 3.10+, PyYAML, pathspec, pytest, Graphify `0.9.48`, POSIX descriptor-safe filesystem operations.

**Spec:** `docs/superpowers/specs/2026-08-25-atlasweaver-production-hardening-design.md`

## Global Constraints

- Keep Graphify pinned to exactly `0.9.48`.
- Preserve fail-closed privacy, path confinement, and promotion transactions.
- Do not expose matched secret bytes or absolute local paths in CLI output.
- Do not commit, push, register graphs, or write an Obsidian vault implicitly.
- Use TDD for every production behavior.

---

### Task 1: Immutable staging receipts

**Files:**
- Create: `src/project_knowledge/receipt.py`
- Create: `tests/test_receipt.py`
- Modify: `src/project_knowledge/staging.py`
- Modify: `src/project_knowledge/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `StagingReceipt(project_id: str, graphify_version: str, source_digest: str, files: tuple[PurePosixPath, ...])`
- Produces: `write_staging_receipt(path: Path, staged: StagedInput, manifest: ProjectManifest) -> StagingReceipt`
- Produces: `load_staging_receipt(path: Path, manifest: ProjectManifest) -> StagingReceipt`
- Produces: `verify_staged_input(root: Path, receipt: StagingReceipt) -> StagedInput`

- [ ] Write failing receipt round-trip tests that assert deterministic sorted JSON, `0600` mode, exclusive creation, and no absolute paths.
- [ ] Run `uv run pytest -q tests/test_receipt.py` and confirm failure because `project_knowledge.receipt` does not exist.
- [ ] Implement the receipt dataclass, strict duplicate-key JSON loading, exact schema validation, private exclusive write, and staged-tree digest verification.
- [ ] Run `uv run pytest -q tests/test_receipt.py` and confirm all receipt tests pass.
- [ ] Write failing CLI tests requiring `stage --receipt`, returning no receipt path, and leaving neither stage nor receipt after a receipt-write failure.
- [ ] Run the focused CLI tests and confirm the parser/behavior failures.
- [ ] Extend `stage_input`/CLI orchestration so stage and receipt form one cleanup unit, then run `uv run pytest -q tests/test_cli.py tests/test_staging.py tests/test_receipt.py`.

### Task 2: Failure-atomic native Graphify adapter

**Files:**
- Create: `src/project_knowledge/adapter.py`
- Create: `tests/test_adapter.py`
- Modify: `src/project_knowledge/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `verify_staged_input(...) -> StagedInput`
- Produces: `AdaptedCandidate(root: Path, source_digest: str, node_count: int, edge_count: int, skipped_count: int)`
- Produces: `adapt_candidate(raw_candidate: Path, destination: Path, staged: StagedInput, manifest: ProjectManifest) -> AdaptedCandidate`

- [ ] Write failing adapter tests using native Graphify top-level keys (`nodes`, `links`, `graph`, `directed`, `multigraph`) and asserting injected identity/coverage without mutation of raw bytes.
- [ ] Run `uv run pytest -q tests/test_adapter.py` and confirm failure because the adapter is absent.
- [ ] Implement allowlisted root-level safe capture, strict JSON parsing, link normalization, represented-path collection, exclusive destination creation, and cleanup on every failure.
- [ ] Run `uv run pytest -q tests/test_adapter.py` and confirm the happy path and failure-path tests pass.
- [ ] Write failing CLI tests for the exact `adapt --staged-input --receipt --raw-candidate --destination` contract and source-drift rejection.
- [ ] Run focused CLI tests and confirm failures are caused by the missing command.
- [ ] Wire `adapt` into the parser/dispatcher, verify receipt and current repository snapshot before and after adaptation, and return only safe counts/digests.
- [ ] Run `uv run pytest -q tests/test_adapter.py tests/test_cli.py tests/test_artifacts.py`.

### Task 3: Redacted secret triage

**Files:**
- Create: `src/project_knowledge/secrets_scan.py`
- Create: `tests/test_secrets_scan.py`
- Modify: `src/project_knowledge/staging.py`
- Modify: `src/project_knowledge/cli.py`
- Modify: `src/project_knowledge/privacy.py`
- Test: `tests/test_staging.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `SecretFinding(detector: str, path: PurePosixPath, line: int, fingerprint: str, bypassable: bool)`
- Produces: `scan_payload(path: PurePosixPath, payload: bytes) -> tuple[SecretFinding, ...]`
- Produces: `scan_repository(repo_root: Path, manifest: ProjectManifest) -> tuple[SecretFinding, ...]`
- Produces: `load_secret_exceptions(repo_root: Path, findings: tuple[SecretFinding, ...]) -> frozenset[str]`

- [ ] Write failing detector tests covering every existing credential shape, redacted fingerprints, line numbers, env/config references, quoted literals, malformed UTF-8, and non-bypassable rule classification.
- [ ] Run `uv run pytest -q tests/test_secrets_scan.py` and confirm module/API failures.
- [ ] Implement named structured detectors and the conservative contextual assignment classifier.
- [ ] Run detector tests and confirm green.
- [ ] Write failing exception tests for exact fingerprint acceptance and rejection of structured-rule, unknown, duplicate, denied-path, malformed, and stale exceptions.
- [ ] Implement strict `.graphify-secret-exceptions.yaml` loading without adding it to staged inputs.
- [ ] Write failing CLI tests proving `scan-secrets` is read-only, path-confined, value-redacted, and stable JSON.
- [ ] Wire the scanner into `stage_input` and the CLI, replacing the anonymous regex tuple while preserving cleanup behavior.
- [ ] Run `uv run pytest -q tests/test_secrets_scan.py tests/test_staging.py tests/test_cli.py tests/test_privacy.py`.

### Task 4: Graph integrity and trust state

**Files:**
- Create: `src/project_knowledge/integrity.py`
- Create: `tests/test_integrity.py`
- Modify: `src/project_knowledge/adapter.py`
- Modify: `src/project_knowledge/artifacts.py`
- Modify: `src/project_knowledge/health.py`
- Test: `tests/test_adapter.py`
- Test: `tests/test_artifacts.py`
- Test: `tests/test_health.py`

**Interfaces:**
- Produces: `GraphIntegrity(schema_version: int, dangling_edges: int, missing_endpoints: int, self_loops: int, duplicate_edges: int, collapsed_edges: int | None, impact_analysis_trusted: bool)`
- Produces: `analyze_graph(nodes: list[dict[str, object]], edges: list[dict[str, object]], collapsed_edges: int | None = None) -> GraphIntegrity`

- [ ] Write failing integrity tests for duplicate IDs, dangling/missing endpoints, self-loops, duplicate edges, and unknown collapsed-edge evidence.
- [ ] Run `uv run pytest -q tests/test_integrity.py` and confirm the missing API failure.
- [ ] Implement deterministic analysis with hard errors for duplicate IDs and invalid endpoints.
- [ ] Run integrity tests and confirm green.
- [ ] Write failing adapter/artifact tests requiring the exact `graph_health` schema and recomputation rather than trusting candidate counters.
- [ ] Inject health during adaptation and validate it in `validate_candidate`.
- [ ] Write failing health tests for `impact_analysis_trusted` and `graph_integrity_degraded`.
- [ ] Extend ownership/state/health serialization and run `uv run pytest -q tests/test_integrity.py tests/test_adapter.py tests/test_artifacts.py tests/test_health.py`.

### Task 5: Dogfooding, real Graphify gate, and documentation

**Files:**
- Create: `.graphify-project.yaml`
- Create: `.graphifyignore`
- Create: `tests/test_real_graphify_pipeline.py`
- Modify: `tests/test_public_release.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `skills/using-project-knowledge-graphs/SKILL.md`
- Modify: `skills/using-project-knowledge-graphs/references/workflow.md`
- Modify: `tests/test_skill_contract.py`

**Interfaces:**
- Consumes the public `stage`, `adapt`, `validate`, `promote`, and `health` CLI commands.

- [ ] Write a failing public-release regression test proving ignored `graphify-out/.graphify_python` cannot poison the product-context scan.
- [ ] Change the test to inspect tracked and unignored release inputs and verify it fails when a release-bound forbidden marker is introduced in a controlled fixture.
- [ ] Add the repository manifest/ignore files and run `project-knowledge detect --repo . --json` to confirm the repository is opted in without mutation.
- [ ] Write a real Graphify integration test that stages a tiny project, runs pinned Graphify, adapts, validates, promotes, and checks source drift; locally skip only when the executable/version is absent.
- [ ] Add pinned Graphify installation and the mandatory integration invocation to CI.
- [ ] Update README and the bundled skill with the exact receipt/adapt workflow, secret triage rules, and navigation-versus-impact trust semantics.
- [ ] Update changelog and contract tests, then run `uv run pytest -q tests/test_public_release.py tests/test_real_graphify_pipeline.py tests/test_skill_contract.py tests/test_cli.py`.

### Task 6: Full verification and release evidence

**Files:**
- Verify all changed production, test, documentation, manifest, and workflow files.

**Interfaces:**
- Produces fresh evidence for the approved design and every readiness gate.

- [ ] Run `uv run pytest -q` and require zero failures/skips except the documented local real-Graphify availability condition.
- [ ] Run the real Graphify integration test explicitly with `graphify 0.9.48` and require PASS.
- [ ] Run `uv run python -m compileall -q src tests`.
- [ ] Run `uv build` and inspect wheel/sdist contents for manifests, docs, and private-context leakage.
- [ ] Run `project-knowledge detect --repo . --json`, `preflight`, a temporary stage/adapt/validate/promote cycle, and `health` without touching an Obsidian vault or global registry.
- [ ] Run `git diff --check`, inspect `git status --short`, and review the complete diff against the spec.
- [ ] Report any consumer-workspace-only measurements separately; do not invent external repository evidence from this repository.
