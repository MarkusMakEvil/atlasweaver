# Changelog

All notable changes to AtlasWeaver are documented here.

## 0.3.6 - 2026-08-29

- Made the source installer bootstrap correctly under the stock Python 3.9
  shipped on older macOS systems while delegating the supported runtime to uv.
- Forced source installs to rebuild AtlasWeaver instead of reusing a stale
  same-version wheel from uv's cache.
- Migrated intact legacy managed Codex skill installations to the current
  ownership marker without weakening tamper or symlink protections.
- Required the Python 3.10, Python 3.13, and Graphify fixture checks on `main`.

## 0.3.5 - 2026-08-29

- Added a fixture-bound Graphify 0.9.51 production adapter while retaining exact
  0.9.48 support for existing manifests; both remain navigation-only.
- Fixed the scheduled compatibility probe module entrypoint so helper functions
  are defined before its exact `python -m` invocation executes.
- Kept managed graphs valid when Graphify writes its exact bounded runtime
  query stamp while continuing to reject every unknown output entry; cache
  validation is descriptor-bound against concurrent symlink substitution.
- Allowed a fully validated owned generation to migrate between declared,
  supported Graphify contracts while keeping ordinary ownership validation
  pinned to the current manifest.
- Made projection identity stable across local bytecode, virtual environments,
  dependency trees, and build outputs without removing them from privacy audit
  decisions.
- Reported valid remote artifact providers as configured rather than falsely
  invalid when no network verification was requested, with health payloads
  versioned as schema 3 for the new closed status value.
- Preserved the documented query error across descriptor-bound snapshot
  failures instead of leaking a frozen-dataclass traceback error.
- Updated every workflow action to a reviewed immutable Node 24 release and
  added a one-command AtlasWeaver/Graphify runtime installer.

## 0.3.4 - 2026-08-27

- Isolated GitHub CLI capability probes as well as attestation verification,
  preventing device-state files from appearing in consumer repositories.

## 0.3.3 - 2026-08-26

- Isolated all GitHub CLI config, cache, home, and state writes inside the
  verified-pull private temporary directory.

## 0.3.2 - 2026-08-26

- Allowed securely resolved package-manager symlinks for the GitHub CLI while
  preserving executable identity and mutation checks during attestation.

## 0.3.1 - 2026-08-26

- Canonicalized Graphify 0.9.48 diagnostic supersets before evidence binding.
- Accepted privacy-scanned sensitive filenames only when they are exact members
  of the authorized staged snapshot.
- Completed AtlasWeaver's code-only self-hosting scope and coverage policy.

## 0.3.0 - 2026-08-26

- Added manifest schema v2 with recoverable initialization and explicit v1
  migration previews.
- Added descriptor-bound lifecycle authority, privacy projection digests,
  tracked extraction-coverage approvals, and content-free operation state.
- Added evidence-bound refresh foundations, core-vs-optional health semantics,
  immutable bounded queries, and an opt-in project-agnostic registry/fleet
  model.
- Added packaged agent resources, a bounded upstream Graphify compatibility
  probe, and artifact transport/workflow foundations.
- Added deterministic portable graph bundles, provenance-verified GitHub
  Release pull, managed Codex/Agents resources, and split-privilege reusable
  publication workflows.
- Added a bounded universal fleet coordinator and atomic UUID-keyed registry;
  fleet configuration and behavior contain no organization-specific defaults.
- Added the Graphify compatibility registry, evidence-bound
  `GRAPH_EVIDENCE.json`, reason-coded endpoint quarantine, deterministic final
  edge IDs, and read-only CI compatibility probes; Graphify 0.9.48 remains
  navigation-only.

## 0.2.2 - 2026-08-25

- Normalized Graphify's empty source-path sentinels and package-relative module
  aliases before strict candidate validation.

## 0.2.1 - 2026-08-25

- Added `**/*creds*` to the immutable filename deny set as a defensive layer
  alongside non-bypassable payload scanning.

## 0.2.0 - 2026-08-25

- Added immutable out-of-tree staging receipts and a separate native Graphify
  candidate adapter.
- Added redacted named secret triage with literal-aware environment references
  and fingerprint-bound contextual exceptions.
- Added recomputed graph-integrity evidence and an explicit
  `impact_analysis_trusted` health boundary.
- Added self-hosting project manifests and a real Graphify end-to-end test.
- Made public-context release tests ignore untracked runtime outputs while still
  scanning every tracked and unignored product file.

## 0.1.0 - 2026-08-23

- Initial public release.
- Privacy-safe deterministic staging.
- Validated crash-safe graph promotion.
- Transactional Obsidian generated-namespace management.
- Graphify capability adapter and source-digest health checks.
- Managed Codex Agent Skill installer.
