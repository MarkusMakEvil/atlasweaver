# AtlasWeaver Production Hardening Design

## Objective

Make the documented AtlasWeaver workflow operate end to end with the pinned
Graphify release, make secret-shaped source triage usable without weakening the
privacy boundary, and distinguish a navigable graph from one trusted for impact
analysis.

## Non-negotiable constraints

- Graphify remains pinned to `0.9.48` until a separately tested compatibility
  change proves a newer release.
- Graphify only receives a private staged source tree.
- Validation remains a non-mutating trust boundary. It must never enrich a raw
  candidate in place.
- High-confidence structured credential rules cannot be bypassed by repository
  configuration.
- Diagnostics never emit secret values, absolute repository paths, environment
  values, or private atlas paths.
- Promotion continues to require an object validated in the same process and
  preserves its crash-safe rollback behavior.
- Human Obsidian `Notes/` and `.obsidian/` remain outside AtlasWeaver ownership.

## Staging receipt

`project-knowledge stage` gains a required `--receipt <path>` argument. The
receipt is written outside the staged tree so Graphify cannot index it. Its JSON
schema contains the project identity, pinned Graphify version, source digest,
and sorted staged paths. The receipt is written only after the staged tree is
complete, with private permissions and an exclusive create.

The receipt destination is rejected before staging when it is equal to or below
the staged destination. Its parent descriptor and the completed staged-root
identity remain bound across the write; swaps, write failures, and parent-fsync
failures remove both artifacts.

The new `StagingReceipt` type can reload the receipt and verify the staged tree
by recomputing its digest and exact file list. A changed, missing, extra,
symlinked, or non-regular staged entry fails closed.

## Candidate adapter

A new command has this interface:

```text
project-knowledge adapt \
  --repo <repo> \
  --staged-input <private-stage> \
  --receipt <receipt.json> \
  --raw-candidate <graphify-output> \
  --destination <adapted-candidate> \
  --json
```

The adapter verifies the receipt and staged input, captures only the allowlisted
root-level artifacts without following links, parses native Graphify
`graph.json`, and writes a separate private destination with cleanup on failure. It injects
`project_id`, `graphify_version`, `source_digest`, `extraction_coverage`, and
`graph_health` while preserving native graph nodes, links/edges, graph metadata,
and report/HTML bytes.

Graphify `links` are normalized to the validator's edge contract without
discarding the original relation and confidence fields. Represented source paths
are collected recursively from provenance path fields. Every staged path not
represented is recorded as an unapproved skip with the stable reason
`not represented by Graphify 0.9.48`. The existing health model therefore
reports degraded coverage honestly instead of silently claiming completeness.

The adapter creates a new private destination with descriptor-relative writes,
keeps its selected parent bound across the transaction, and removes the
destination after any failure, including a post-write source-drift check.
It refuses existing destinations, pre-enriched graphs, reserved ownership files,
missing required artifacts, unsafe artifact types, and changes to the repository
source between staging and adaptation. Normal Graphify sidecars are ignored rather
than copied into the trusted wrapper candidate.

## Secret triage

Secret matching moves into a focused `secrets_scan.py` module. Every rule has a
stable detector ID and a severity class:

- non-bypassable structured rules: Telegram token, private key, AWS access key,
  GitHub token, Slack token, bearer token, and credentialed URL;
- contextual rule: `generic_secret_assignment`.

The contextual rule ignores non-literal environment/config references such as
`process.env.NAME`, `import.meta.env.NAME`, `os.environ[...]`, `getenv(...)`, and
`${NAME}`. Quoted long literals remain findings.

`project-knowledge scan-secrets --repo <repo> --json` is read-only and returns
only detector ID, confined relative path, one-based line number, and a SHA-256
fingerprint. It never returns the match or line contents.

Optional repository exceptions live in `.graphify-secret-exceptions.yaml`.
Only `generic_secret_assignment` may be excepted. Each entry binds path,
detector, fingerprint, and a non-empty review reason. Exceptions for structured
rules, unknown findings, duplicate entries, or globally denied paths fail
closed. The same scan implementation is used by both reporting and staging.
Contextual fingerprints bind the complete assignment expression and occurrence,
so changing the key, appending another literal, moving, or duplicating it
invalidates an earlier review.

## Graph integrity and impact trust

The adapter computes deterministic final-graph checks:

- unique node IDs;
- edge endpoints present in the node set;
- missing or malformed endpoints;
- self-loops;
- duplicate edges;
- source paths confined to the staged snapshot.

The injected `graph_health` schema records counts and an
`impact_analysis_trusted` boolean. Missing/dangling endpoints and duplicate node
IDs are hard adaptation failures. Self-loops and duplicate edges are recorded
and make impact analysis untrusted without preventing navigation.

Graphify 0.9.48 does not preserve enough pre-build evidence to prove how many
edges were collapsed before `graph.json`. The adapter records
`collapsed_edges: null` and therefore sets `impact_analysis_trusted: false`.
This is intentional and honest. A future pinned Graphify contract may set an
integer only when Graphify supplies a persisted diagnostic produced from the
same extraction.

Validation requires the exact `graph_health` schema and recomputes every
observable counter. Health exposes `impact_analysis_trusted` and an
`graph_integrity_degraded` issue when it is false.

## Dogfooding and release verification

AtlasWeaver gains its own `.graphify-project.yaml` and `.graphifyignore` so
`detect`, `preflight`, and health can be exercised on the product repository.
Generated output remains ignored.

The public-context test scans tracked and unignored release inputs (matching the
source builder) rather than arbitrary ignored workspace state. CI runs unit
tests, compilation, build, and a real
Graphify adapter integration test. The integration test may skip only when the
pinned executable is unavailable locally; CI installs the pinned executable so
the gate is mandatory there.

## Rollout contract

BrandMap onboarding is repository-scoped: `web`, `server`, `docs`, and
`extensions` receive distinct project IDs and output ownership. Ephemeral
worktrees are not globally registered by default. Obsidian export is enabled
only after web and server complete a stable shadow period.

Readiness requires: no unreviewed secret findings, no dangling endpoints, no
unapproved coverage skips for supported source, source-drift detection, rollback
verification, and freshness checks within five seconds for web and ten seconds
for server.
