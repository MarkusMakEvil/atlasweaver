# AtlasWeaver Compatibility and Impact Evidence Design

**Date:** 2026-08-26

**Status:** Approved design; implementation plan complete

**Scope:** Versioned Graphify adapters, raw diagnostic evidence, and honest
impact-trust promotion.

## Objective

Allow AtlasWeaver to add tested Graphify versions without scattering version
conditionals, and make impact trust a reproducible evidence decision rather than
a permanent hard-coded false or an optimistic claim.

## Non-goals

- No automatic Graphify upgrade.
- No support declaration without real compatibility tests.
- No reconstruction of semantic edges AtlasWeaver cannot prove.
- No claim that a post-build simple graph preserves raw multigraph semantics.
- No acceptance of missing or dangling final graph endpoints.

## Compatibility registry

AtlasWeaver contains an immutable compatibility registry keyed by exact Graphify
version. Each entry specifies:

- adapter implementation ID;
- exact required CLI commands, argv templates, flags, and output formats;
- native artifact schema fingerprint;
- extraction diagnostic capability;
- supported query commands;
- semantic backend/model and credential-environment contract;
- audited sensitive-name source extensions versus data/config suffixes;
- graph directedness, node identity, edge identity tuple, and permitted parallel
  relation semantics;
- normalization transforms and raw-to-final lineage requirements;
- whether complete impact evidence is possible;
- compatibility fixture digest.

The project manifest still pins one exact version. `doctor`, `preflight`, and
`refresh` resolve that version through the registry and reject an installed
version or capability surface that differs. Adding a version requires a new
adapter entry, captured native fixtures, and the full compatibility suite.

This registry is the sole source of accepted versions and capabilities for
manifest validation, extraction, adaptation, artifact validation, health,
queries, workflows, and tests. No second hard-coded version allowlist is
permitted. A registry consistency test scans all public version touchpoints and
fails if they do not resolve through this API.

Executable resolution produces one immutable `ResolvedGraphifyExecutable`
containing the canonical resolved regular path, `st_dev`, `st_ino`, and the
launcher SHA-256 captured with no-follow descriptor checks. Immediately before
every Graphify subprocess—including version/help/capability smoke, extraction,
diagnosis, clustering, registry projection, agent installation, and query
commands—the shared runner reopens that exact path with no-follow semantics and
requires the same device, inode, and launcher digest. Any reopen, identity, or
digest mismatch fails closed as `graphify_executable_changed` before the runner
is invoked. High-level/public commands expose no binary-path override; an
explicit path is permitted only as an injected library-test or maintainer
fixture-capture seam.

`GraphifyAdapter` owns only version-specific native-output normalization. The
existing AtlasWeaver validator, ownership, freshness, policy, and promotion
contracts remain version-independent.

Refresh records an `extraction_invocation_digest` using the domain prefix
`atlasweaver-graphify-pipeline-v1\0` and canonical UTF-8 JSON. The same closed,
sanitized invocation payload is embedded in `GRAPH_EVIDENCE.json` alongside that
digest so validators can reconstruct the canonical bytes and verify the binding
instead of trusting an orphan hash. Its payload binds
the adapter ID, Graphify executable SHA-256/version, the ordered argv arrays for
extract/diagnose/cluster (including directedness, `--no-label`, and visualization
policy), backend/model
identifiers, configuration SHA-256, staged projection digest, admitted
environment-variable *names*, and an ordered list of every native input/output
as `{path, sha256, byte_length}`. Secret values and ambient environment are never
included. Evidence and ownership bind this digest so a result cannot be
separated from the orchestrator contract that produced it.

## Evidence capture

Refresh runs official `graphify extract --no-cluster`, captures its native
graph, runs `graphify diagnose multigraph` with adapter-declared directedness
and JSON output against that graph, captures evidence, normalizes/quarantines
invalid structural endpoints into a separate private cluster input, and only
then runs `graphify cluster-only` with the adapter's exact flags. The captured
post-dedup artifact is immutable and digest-bound before normalization or
clustering.
For Graphify 0.9.48, `extract --no-cluster`
already calls native node/edge deduplication before writing JSON. Its diagnostic
therefore describes an observed post-dedup, pre-cluster graph; it is not raw
multigraph evidence and cannot reveal already dropped parallel provenance.

Graphify 0.9.48 omits `directed` in that extraction artifact but diagnoses a
missing value as directed while `cluster-only` treats it as undirected. Its
adapter therefore mandates `diagnose multigraph --undirected --json` and records
that choice. It also mandates `cluster-only --no-label`; automatic community
label generation is an uncontrolled second LLM phase and is not part of the v1
pipeline.

Complete future evidence requires an official, versioned pre-dedup sidecar *and*
an official total post-transform lineage, or a documented immutable source-edge
occurrence ID preserved into the final graph. A pre-dedup dump alone is
insufficient because clustering may rewrite or collapse edges. The validated
projection contains only canonical node/edge occurrence identifiers, relation,
direction, confidence, safe relative source path, and line/column coordinates.
Context strings, source fragments, symbols containing literals, and all fields
outside the adapter allowlist are excluded. Internal Graphify imports or
monkeypatch hooks are not an accepted production capture boundary.

`GRAPH_EVIDENCE.json` contains:

```json
{
  "schema_version": 1,
  "graphify_version": "0.9.48",
  "adapter_id": "graphify-0.9.48",
  "source_digest": "...",
  "projection_digest": "...",
  "extraction_invocation_digest": "...",
  "extraction_invocation": {
    "schema_version": 1,
    "adapter_id": "graphify-0.9.48",
    "graphify_version": "0.9.48",
    "executable": {"sha256": "...", "version": "0.9.48"},
    "capability_smoke_digest": "...",
    "argv": [
      {"operation": "extract", "items": ["<graphify>", "extract", "<staged-root>", "..."]},
      {"operation": "diagnose", "items": ["<graphify>", "diagnose", "multigraph", "..."]},
      {"operation": "cluster", "items": ["<graphify>", "cluster-only", "<cluster-workspace>", "..."]}
    ],
    "backend": "openai",
    "model": "...",
    "configuration_sha256": "...",
    "source_digest": "...",
    "projection_digest": "...",
    "environment_names": ["HOME", "LANG", "LC_ALL", "OPENAI_API_KEY", "PATH"],
    "artifacts": [
      {"path": "raw/graph.json", "sha256": "...", "byte_length": 123}
    ]
  },
  "observed_post_dedup": {
    "node_count": 969,
    "edge_count": 3290,
    "missing_endpoint_edges": 159,
    "self_loop_edges": 0
  },
  "pre_dedup": null,
  "evidence_complete": false,
  "limitations": ["pre_dedup_edge_projection_unavailable"]
}
```

`extraction_invocation` is the exact canonical invocation body without a
self-digest. Its recursively closed schema permits only canonical logical argv,
environment-variable names, and digest/length artifact bindings—never actual
paths or secret values. Evidence parsing canonicalizes this object, recomputes
the domain-separated digest, and requires equality with
`extraction_invocation_digest`; unknown keys, alternate encodings, or an object
whose binding differs are rejected.

Counts are recomputed during adaptation rather than copied blindly from native
metadata. Lifecycle owns the handoff: it descriptor-captures native artifacts,
passes sanitized immutable descriptors to the evidence builder, writes evidence
as a private derived candidate artifact, and makes adaptation/validation require
the binding. Evidence code never invokes Graphify, and the adapter cannot omit
the document. `GRAPH_EVIDENCE.json` is required for every schema-v2 adapter and
is bound into ownership and bundles even when incomplete. Unknown evidence is
represented as `null` plus a limitation, never as zero.

A complete future `pre_dedup` section assigns every occurrence a stable
sidecar-provided ID, or an adapter-defined ordinal over a documented canonical
native sequence plus its content digest; exact duplicate occurrences therefore
remain distinct. It records directed endpoints, relation, confidence,
provenance, resulting `final_edge_id` when any, and disposition `kept`,
`rewritten`, or `dropped` with an adapter-defined reason. Final IDs also exist in
the adapted graph or a validated overlay. This provides total raw-to-final
lineage, including many-to-one collapses. Duplicate,
conflicting, and collapse counts are computed from that adapter's declared edge
identity and parallel-edge invariants, never from a blanket same-endpoint rule.

## Endpoint normalization

The adapter may repair a dangling observed or future raw endpoint only when a
documented, deterministic version-specific rule maps it to exactly one existing
node. Examples include an official empty external sentinel or an exact canonical
source alias. An ambiguous match is retained as an unresolved diagnostic and
cannot contribute to trusted impact analysis. Unresolved edges are quarantined
from the normalized cluster input and therefore the promoted final graph. They
are represented by reason-coded aggregate evidence or complete lineage when
available, plus a navigation limitation; they are never silently accepted or
described as proven absent. Any new invalid endpoint introduced by clustering
causes final validation failure rather than a second silent rewrite.

Normalization emits reason-coded counts. Rules have regression fixtures from
real Graphify output and cannot inspect source literals outside the safe stage.
The adapted `GRAPH_REPORT.md` clearly labels native diagnostic counts and adds a
deterministic AtlasWeaver integrity appendix with final node/edge/quarantine
counts, evidence digest, and navigation limitation. A reader is never shown a
native edge count as if it described the post-quarantine graph.

## Trust decision

Impact is `trusted` only when all conditions hold:

- evidence capture is complete for the selected adapter;
- current source digest matches the graph and evidence;
- staged extraction coverage has no skip, including repository-approved
  navigation omissions;
- final graph has no missing/dangling endpoints, invalid self-loop, duplicate,
  or conflicting relation under the adapter invariants;
- raw evidence has no unresolved endpoint, invalid duplicate, or unexplained
  transform under those same invariants;
- every impact edge has source provenance accepted by validation;
- graph, report, evidence, and ownership digests match.

Otherwise impact is `navigation`, accompanied by stable limitations. A project
cannot override this decision in its manifest.

Graphify 0.9.48 is unconditionally `navigation` under its declared CLI
capabilities because pre-dedup lineage is unavailable. Zero observed diagnostics
cannot promote it. Only a newly verified official sidecar/capability or a later
supported Graphify adapter can make complete evidence possible; AtlasWeaver will
improve transparency without inferring missing history.

Ownership is non-circular: its canonical payload lists SHA-256 and byte length
for graph, report, evidence, and other approved artifacts, but never a digest of
the ownership file itself. Schema v2 also requires the generation's immutable
integer `build_epoch`. Validation recomputes artifact digests and then validates
the ownership payload. Adapters must preserve evidence and invocation bindings
while normalizing; promotion and packing reject a candidate that drops or
rewrites them without a declared transform.

Digest names are disjoint across every schema: wire field `source_digest` is
always the SHA-256 of canonical safe source content, `projection_digest` binds
policy/manifest decisions, and `git_commit_oid` is a separate optional value
paired with `git_commit_algorithm`. No validator compares a Git object ID with a
safe-source or projection digest.

## Impact query

`affected` uses the validated navigation graph for traversal and, when evidence
is complete, annotates every returned edge with its evidence relation,
confidence, source file, and source location. In navigation mode it returns the
same candidates with a mandatory source-verification warning and the limitations
that prevented trust.

AtlasWeaver performs this traversal and final-edge-ID join itself and emits the
stable JSON query envelope. It does not parse Graphify 0.9.48's opaque
human-formatted `affected` output as structured evidence. Other native query
commands are admitted only when their compatibility entry declares a bounded
structured-output contract.

No command calls a candidate “unaffected” when evidence is incomplete. An empty
navigation traversal means “no path found in this graph,” not proof of no
impact.

## Scheduled compatibility CI

CI runs:

- the pinned production version on every push and pull request;
- all declared supported versions against captured fixtures;
- a scheduled probe of the newest upstream Graphify release without declaring
  support or changing project manifests.

The scheduled probe runs with `contents: read`, uploads a machine-readable
compatibility report as a workflow artifact, and reports network/API failure as
`inconclusive`. It does not open a pull request, edit a branch, publish a package,
or move a supported-version alias. Declaring support always requires a separate
reviewed code change with fixtures.

The resolver reads the bounded official PyPI JSON document for the `graphifyy`
distribution, ignores yanked/prerelease files, and selects the highest stable
PEP 440 version with an available wheel for the test interpreter. Resolver/tool
versions and every workflow action are pinned by full digest or commit SHA.

## Module boundaries

- `compatibility.py`: immutable version registry and capability checks;
- `adapters/base.py`: version-neutral protocol;
- `adapters/graphify_0_9_48.py`: native normalization rules;
- `evidence.py`: diagnostic capture, recomputation, validation, and trust;
- `integrity.py`: final graph structural analysis;
- `queries.py`: trust-aware `affected` presentation.

Version-specific adapter code cannot promote artifacts. Evidence code cannot
run Graphify or read the unsanitized repository.

Core lifecycle depends on the registry and evidence contracts for refresh,
health, and query admission. Artifact pack/pull and fleet aggregation depend on
the same canonical evidence and ownership validators; they may not create a
parallel trust decision. These dependencies are implementation prerequisites,
not optional follow-up integrations.

## Testing

- Registry tests rejecting undeclared versions and capability drift.
- Real 0.9.48 fixture tests for metadata adaptation, empty sentinels, canonical
  aliases, dangling endpoints, duplicate edges, and collapse counts.
- Mutation tests proving every trust predicate can independently demote impact.
- Evidence JSON duplicate-key, non-finite-number, path, provenance, digest, and
  size-limit tests.
- Query tests forbidding “no impact” language in navigation mode.
- Scheduled workflow contract tests and pinned upstream actions.
- End-to-end evidence inclusion in promotion, health, pack, pull, and fleet
  aggregation.

## Rollout

Introduce the registry with only Graphify 0.9.48 and no behavioral upgrade.
Then add evidence artifacts and trust-aware queries. New Graphify versions enter
only through reviewed adapter pull requests with real fixtures and green
compatibility CI.
