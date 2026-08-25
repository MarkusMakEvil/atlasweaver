# Changelog

All notable changes to AtlasWeaver are documented here.

## Unreleased

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
