# AtlasWeaver Core Adoption Design

**Date:** 2026-08-26

**Status:** Approved design; implementation plan complete

**Scope:** Privacy policy v2, manifest v2, high-level lifecycle commands,
health semantics, and the unified read-only query surface.

## Objective

Make the safe path the easy path. A developer or agent should be able to
onboard, inspect, refresh, and query a repository without manually coordinating
temporary directories or understanding AtlasWeaver's internal transaction
boundaries. Existing low-level commands remain available and keep their current
fail-closed behavior.

## Non-goals

- AtlasWeaver does not become a long-running daemon.
- It does not extract an unsanitized repository.
- It does not silently install hooks, edit agent instructions, publish an
  artifact, update a registry, or write an Obsidian vault.
- It does not claim that Graphify-inferred edges prove source impact.
- It does not silently downgrade a document corpus to code-only extraction.

## Command surface

### `project-knowledge init`

`init --repo <path> --project-id <id> [--project-uid <uuid>]` discovers candidate include roots and
prints a deterministic manifest template. It writes `.graphify-project.yaml` and
`.graphifyignore` only with `--apply`. It never overwrites either file;
existing configuration is handled by the dedicated migration command.
Ambiguous roots are reported for explicit selection.

Apply is a recoverable two-file transaction under the repository lifecycle
lock. AtlasWeaver writes mode-0644 sibling temporaries, fsyncs them, records an
init journal, then creates the two destinations without replacement and fsyncs
the repository directory. If either destination appears or a later step fails,
it removes only files whose inode and digest match this transaction and keeps
the journal for deterministic recovery. Stable outcomes are `initialized`,
`already_configured`, `init_conflict`, and `init_recovery_required`; a
half-written configuration is never reported as success.

Apply does not trust a preview object as write authority. Under the lock it
reopens the verified repository by descriptor, rederives the complete canonical
preview from the captured explicit intent, and requires exact repository
identity/status/root/payload equality before allocating a UUID or writing.
Migration and coverage approval use the same preview-rebinding rule; repository
rename/symlink replacement cannot redirect their transaction.

State bootstrap creates `.project-knowledge` as a no-follow mode-0700 directory
and opens the lock with no-follow/exclusive-create semantics; a symlink, wrong
type, or unsafe existing permissions fail closed. Every manifest-using command
checks for the journal before reading configuration. `doctor` remains read-only
and reports `init_recovery_required`; `init --apply` may finish or roll back a
valid journal before proceeding. A corrupt/absent journal never authorizes
deletion, and a lone destination without a valid journal is `init_conflict`.

### `project-knowledge doctor`

`doctor --repo <path>` is read-only. It validates the manifest, safe source
projection, installed AtlasWeaver version, pinned Graphify version and required
capabilities, agent skill installation, graph ownership, source freshness,
optional feature configuration, and artifact provider configuration. JSON
diagnostics contain stable codes and no absolute private paths or secret
literals.

The root `--version` value comes from installed package metadata, and doctor JSON
reports that version plus packaged resource/manifest/compatibility schema
versions. This makes stale global installations and mismatched skills visible
without relying on a repository source checkout.

### `project-knowledge refresh`

`refresh --repo <path>` is an explicit graph mutation. It performs:

1. acquire `.project-knowledge/refresh.lock`, then preflight and secret scan;
2. a mode-0700 private temporary staging directory and immutable receipt;
3. official `graphify extract --no-cluster` against the staged directory and a
   private raw-output root;
4. post-dedup native-graph diagnosis with adapter-declared directedness and
   immutable evidence capture;
5. adapter normalization into a separate cluster input, quarantining only
   reason-coded invalid endpoints while preserving diagnostic evidence;
6. official `graphify cluster-only --no-label` (and `--no-viz` unless
   `track_html` is enabled) on that normalized copy, creating a consistent
   native report/navigation graph without a second implicit LLM phase;
7. final metadata/evidence adaptation into a separate candidate;
8. validation against the same staged receipt and current safe input;
9. transactional promotion into `graphify-out`;
10. final health inspection and cleanup of temporary state.

Schema-2 candidate validation always requires an external evidence-digest
anchor from the in-process evidence builder or a descriptor-captured bundle/
registry binding. Hashing candidate evidence at the validator call site never
creates authority; arbitrary low-level schema-2 validate therefore refuses
with `evidence_anchor_required`.

The lifecycle lock is held from projection through cleanup, and lock order is
always lifecycle lock before the narrower promotion lock. It serializes init,
migration, coverage approval, refresh, install, pack snapshot capture, and
registry mutations for the same repository.
All retained low-level graph-affecting commands, including `adapt`, `promote`,
Atlas promotion, and global-registry sync, enter the same lifecycle boundary;
mixed legacy/high-level races are tested. A v2 command never relies on the
promotion lock alone.
The boundary opens the repository root once with no-follow semantics and treats
that descriptor—not the pathname—as authority for its complete lifetime.
Journal, manifest, ignore/policy, projection, secret-scan, staging, live graph,
ownership, post-promotion health, query capture, pack/install capture, and
registry input are all opened relative to that same descriptor. Renaming or
replacing the requested root after lock acquisition cannot split validated
input from the descriptor-bound promotion destination. An optional expected
`(device, inode)` captured by a fleet is compared to `fstat()` of this same
descriptor before state lookup/creation or credential-consuming work.

Read-only doctor and health do not create lifecycle state. They use the same
noncreating repository-root descriptor boundary directly, carry it through all
repository reads, and optionally enforce the fleet-captured identity. Query and
registry status use existing noncreating locks where atomic managed state is
required, but all project-side bytes still come from the already bound root
descriptor. A check followed by reopening the repository pathname is never an
authorization boundary.
The preflight verifies the exact command/flag/backend/model contract from the
compatibility registry, including `extract --no-cluster`, `cluster-only`, and
`diagnose multigraph --json`; help-text resemblance alone is insufficient.
For 0.9.48 the registry requires `diagnose multigraph --undirected --json` to
match `cluster-only` semantics. Community-label generation is a future explicit
feature with its own pinned backend/model and credential boundary.
Preflight resolves the Graphify launcher once into its canonical regular path,
device, inode, and SHA-256 identity. The shared process boundary reopens and
revalidates that immutable identity immediately before every Graphify child; a
replacement, in-place mutation, symlink swap, or read failure aborts before
spawn with `graphify_executable_changed`. No public command accepts a Graphify
binary override; explicit executable paths exist only as injected library-test
seams.

The command exposes Graphify backend/model/deep-mode options as validated argv
items. Semantic mode requires both an explicit registry-supported backend and
a bounded public model identifier; AtlasWeaver never relies on an upstream
default model. It never forwards `--allow-partial`, `--global`, database introspection,
Git-ignore bypass, or arbitrary native flags. A semantic corpus without an
available backend fails before extraction with `semantic_backend_required`.
`--code-only` must be explicit. Backend credentials enter only an
operation-specific environment allowlist; Graphify never inherits the complete
parent environment. Temporary state is retained only when cleanup itself
fails, and the diagnostic reports a redacted recovery identifier rather than a
source path.

Backend/model pairing and lexical model admission happen before the ambient
mapping or any fixed-name secret is read. Refresh then opens a noncreating
repository descriptor, validates the optional expected identity, and gates the
init journal before credential binding. The lifecycle lock must reacquire that
same identity before any Graphify child. Thus malformed public options and
replaced/recovery-required repositories cannot trigger credential lookup or
subprocess execution.

Atlas export, registry sync, remote publication, and hook installation remain
separate explicit operations. Remote publication is workflow-internal only and
has no public CLI subcommand or installed script; the other operations retain
their explicitly declared public boundaries.

### Query commands

`query`, `path`, `explain`, and `affected` operate only on an owned, validated
manifest-v2 live graph. Schema-v1 graphs remain available to retained low-level
navigation commands but must be explicitly migrated/refreshed before the
immutable high-level query surface. High-level queries run `detect`/health
first, refuse stale or invalid graphs, and
return a trust field:

- `trusted`: impact evidence passed the configured compatibility contract;
- `navigation`: graph traversal is useful but decisive conclusions require
  source verification.

AtlasWeaver implements deterministic, version-neutral traversal directly over
the validated graph: bounded lexical node search for `query`, shortest path for
`path`, node/neighborhood projection for `explain`, and reverse relation-aware
traversal for `affected`. Exact node IDs win; ambiguous labels return bounded
candidates and require explicit selection. Graphify 0.9.48's human-formatted
query commands are never parsed as structured data.

AtlasWeaver copies only approved graph artifacts into a mode-0700 query sandbox
and validates that snapshot. Input/output counts, depth, bytes, and result
cardinality have fixed caps. Successful results use a stable envelope:

```json
{"schema_version":1,"command":"path","trust":"navigation","result":{},"limitations":[]}
```

Malformed, oversized, ambiguous, or cap-exceeded operations produce a stable
nonzero error and no partial result. All rendered strings pass the literal
redactor and output byte cap. Read-only commands never persist query text,
caches, or results; future persistence requires a separately named explicit
mutation rather than a query flag. A future Graphify adapter may expose a native
query only if it has a bounded structured-output contract and still normalizes
to this envelope.

Snapshot capture holds the lifecycle lock, opens every live artifact without
following links, copies from the opened descriptors, and validates digests and
ownership on the completed private copy before releasing the lock. Query runs
only against that immutable copy. `affected` additionally joins validated
evidence IDs; Graphify 0.9.48's opaque text-only command is not used as
structured evidence.

Query acquisition is byte-for-byte repository read-only. It uses only an
already existing lifecycle state directory and lock; missing state or a v1
manifest is a stable refusal and never creates `.project-knowledge` or a lock
file.

### Registry commands

`registry-sync` and `registry-status` put an AtlasWeaver-owned atomic registry in
front of the existing Graphify global-registry adapter. Sync is an explicit
mutation. Registry failure does not invalidate the local project graph.

`registry-status` is read-only and reports `disabled`, `missing`, `stale`,
`mismatch`, or `current`. `registry-sync` accepts only a healthy, source-current,
owned graph with `features.registry: enabled`, writes the namespaced identity
defined by the fleet specification into one canonical AtlasWeaver registry JSON,
atomically replaces it, then rereads and validates the entry before returning
`synced`.
Ownership, digest, or alias mismatch fails closed with stable codes. Calling
sync while disabled returns `registry_disabled` and performs no write; the
explicit manifest migration/edit is the opt-in.

When managed state exists, status uses the already existing global lock in
shared non-creating mode so it cannot observe half of a journaled generation;
contention is a stable read-only mismatch/busy issue. The Graphify compatibility
manifest records exact durable journal-bound snapshot graph paths, never paths
inside a disposable projector HOME. Those paths use a dedicated snapshot
digest over generation identity plus exact ownership and canonical manifest
hashes; generation identity alone cannot collide snapshots that differ only in
build epoch, Git ownership, or configuration bytes.

Because registry state is user-global, sync also takes a separate per-user lock
after the repository lifecycle lock. The AtlasWeaver registry is the sole trust
source. Under the same lock, a journaled compatibility projector derives
Graphify 0.9.48's two fixed files (`global-graph.json` and
`global-manifest.json`), records old/new hashes and backups, fsyncs each write,
and marks the journal committed only after both reread correctly. The exclusive
mutating sync path performs deterministic recovery before any new write. The
shared `create=False` status/snapshot paths are byte-for-byte read-only: if a
journal is present, absent where required, or corrupt, they report the stable
recovery-required/mismatch state and never roll forward, roll back, create a
lock, or mutate a byte. An absent/corrupt journal never authorizes deletion.

AtlasWeaver status/query uses the atomic source registry and accepts the
Graphify projection only when both files match the committed generation hashes.
Any managed Graphify subprocess holds the global lock for that verified read.
An independently launched Graphify 0.9.48 process cannot participate in this
lock and is therefore navigation-only, never a trusted registry reader. Fleet
sync acquires repository locks one at a time, then the global lock per
transaction; contention is bounded and reported per project as `registry_busy`,
never resolved by clobbering.

### Coverage approval

Candidate output cannot approve its own omissions. The optional tracked
`.atlasweaver-coverage.yaml` is the only approval source and has a closed schema
of exact safe relative path, current content SHA-256, adapter ID, omission reason
code, and non-empty rationale. Globs, directories, denied paths, secret-scan
findings, unknown reasons, duplicate keys, and approvals of missing files are
forbidden. The control file itself is never staged as corpus content; its digest
is bound into the projection receipt. It uses the same strict YAML and payload
scanner boundary and a fixed size cap.

`coverage approve --repo <path> --file <relative> --reason <code> --rationale <text>`
prints a canonical preview and writes only with `--apply`, using the lifecycle
lock and journaled no-clobber update. Validation ignores any native/candidate
`approved` boolean and recomputes approval from this tracked document. A content,
adapter, or reason change invalidates the approval. Approved omissions permit a
`partial` navigation query with a mandatory limitation; they can never promote
impact trust. Repository review is the approval authority—AtlasWeaver does not
invent reviewer identity or auto-approve a skip.

## Manifest schema v2

Schema v2 retains the established project fields and adds explicit feature and
artifact intent:

```yaml
schema_version: 2
project_id: demo-project
project_uid: 4ed9af24-5aa2-4eac-8d0a-3f622cc74948
display_name: Demo Project
include_roots: [src, docs]
output_dir: graphify-out
obsidian_namespace: Projects/demo-project/Generated
excludes: []
track_html: false
graphify_version: 0.9.48
features:
  atlas: disabled
  registry: disabled
artifacts:
  provider: none
```

Allowed feature values are `disabled` and `enabled`. Provider values are
`none` and `github-release`. A GitHub provider requires a confined
`owner/repository` identifier and the full transport identity defined by the
artifact specification. `project_uid` is the portable registry and bundle
identity; `project_id` remains the human-facing local name. Init and migration
generate one cryptographically random RFC 9562 UUIDv4 during apply and persist
it in the same journaled transaction; clones retain the committed value. A
caller may supply an already allocated UUID explicitly. Preview shows the
stable placeholder `<generated-on-apply>` when none is supplied, so it never
pretends a random value is deterministic. The persisted value is immutable
across moves, forks, and renames and is never derived from manifest, history,
path, or source digests.

Schema v1 remains readable and maps both optional features and remote artifacts
to `disabled`/`none`; operations needing portable identity remain unavailable
until explicit migration persists `project_uid`. `init` writes v2; a separate
explicit `manifest-migrate --apply` rewrites v1.

Unknown fields, invalid combinations, booleans used as integers, duplicate YAML
keys, aliases, merge keys, non-string mapping keys, path escapes, and attempts
to weaken global policy fail closed. The parser accepts exactly one YAML
document and applies a closed field/type schema recursively. Migration provides
the same journaled/no-clobber guarantees through its distinct one-file manifest
replacement transaction, writes a canonical preview first, never mutates
`.graphifyignore`, and never changes graph output.

## Privacy policy v2

The current policy conflates a sensitive payload with source code whose filename
contains words such as `secret`, `credential`, or `token`. Policy v2 returns a
structured decision with an action and reason:

- `deny`: non-bypassable private/runtime directories, `.env*`, private keys,
  credential databases, cookies, sessions, and sensitive-name data/config
  files;
- `scan`: recognized source-code files with a sensitive name;
- `allow`: ordinary files still subject to payload scanning.

The sensitive-name source-extension allowlist is audited and immutable in each
compatibility-registry entry; a project manifest cannot expand it. The 0.9.48
entry covers programming/infra source, including Python, JS/TS and component
files, Go, Rust, JVM/.NET, C-family/GPU, Ruby, Swift, PHP, Scala, Lua, shell and
PowerShell, Elixir, Objective-C/ML, Julia/R, Dart/Zig, HDL, SQL, Terraform/HCL,
Fortran/Pascal, Apex, and Lisp suffixes supported by that adapter. Data/config
suffixes such as `.json`, `.yaml`, `.toml`, `.ini`, `.tfvars`, key stores, and
project metadata remain deny-classified even if Graphify can parse them.
Matching is case-folded against the final suffix. A `scan` file is staged only
when it is regular, non-symlink, text source and the same literal-aware payload
scanner used for ordinary allowed files reports no unaccepted finding.

Policy precedence is unconditional global denial, binary/type rejection,
sensitive basename classification, then payload scanning. Private/runtime
directories, `.env*`, key material, credential stores, cookies, and sessions
always deny regardless of extension. Only the lexical sensitive-basename rule
may change from `deny` to `scan` for an allowlisted source extension. Structured
credentials and private keys remain non-bypassable. Contextual false-positive
exceptions stay bound to exact path, detector, and fingerprint.

The immutable `**/*creds*` lexical baseline remains as defense in depth:
`database-creds.json` is denied before staging even though payload scanning would
also catch it, while `creds.py` is downgraded only to mandatory `scan`, never to
an unconditional allow.

Coverage metadata records every manifest-scope file as a private reason-coded
decision containing normalized relative path, action, rule ID, and only the
content/detector data permitted for that action. The staging receipt binds the
canonical ordered decision list, policy version, ignore-rule digests, safe
content digests, aggregate reason counts, and a canonical manifest projection
containing only identity, include/exclude, Graphify, visualization, and
extraction fields into a domain-separated `projection_digest`. Display,
Obsidian, feature, and artifact transport settings are deliberately excluded
because they do not change extractor input. Only aggregate action/reason counts and
that digest leave private state; denied filenames and detector fingerprints
never enter graph output, telemetry, or public bundles. Candidate metadata and
ownership bind both `source_digest` and `projection_digest`, and validation
recomputes both. This makes the conjunction of `source_matches` and
`projection_matches` precise: together they mean the graph matches the
documented safe bytes, scope, and policy. `source_matches` alone compares only
the safe-content digest; `scope_coverage` separately reports how much of the
requested tracked scope was represented, scanned, or denied.

Ownership also binds a domain-separated `generation_digest` over the exact
approved non-ownership artifact path/SHA-256/byte-length set. Refresh no-op
identity uses this complete generation digest, not graph bytes alone: an exact
no-op returns the already installed build epoch, while any report, evidence,
HTML, or graph change installs the next epoch. No response may report an
identity component that was not descriptor-revalidated from committed live
ownership. A committed result whose live ownership cannot be revalidated uses
`null` for graph digest, generation digest, and epoch as one indivisible unknown
identity; a malformed promotion summary is never serialized. Refresh result and
CLI envelopes expose the post-commit descriptor-validated installed generation
digest alongside graph digest and epoch, so graph-identical evidence/report
generations remain distinguishable.

Refresh records the changed-promotion commit bit immediately after
`promote_graph` returns and places every subsequent ordinary operation,
including the first live-ownership revalidation, inside the post-commit
normalization boundary. An unexpected revalidation failure after a commit
returns a navigation-only `promoted_but_stale` result with the complete
installed identity or all three identity fields null; a coincident cleanup
failure adds only its opaque recovery ID. After an exact-generation no-op, the
same failure is the closed `refresh_verification_failed` error instead because
no mutation committed. Cleanup never suppresses a pending non-ordinary signal.

Both digests use canonical UTF-8 JSON with explicit domain prefixes.
`source_digest` hashes the ordered safe entries as relative path, SHA-256, and
byte length; `projection_digest` hashes the decision/policy payload above and
the source digest. Neither digest is a Git object ID or a hash of secret literal
values outside the approved safe stage.

## Health schema v2

Health separates core graph correctness from optional integrations. For schema
v2, the legacy `status` alias equals `core_status` rather than an aggregate of
disabled optional features:

```json
{
  "schema_version": 2,
  "core_status": "healthy",
  "source_matches": true,
  "features": {
    "atlas": {"status": "disabled"},
    "registry": {"status": "disabled"},
    "artifacts": {"status": "disabled"}
  },
  "trust": {"impact": "navigation"},
  "issues": [],
  "warnings": ["impact_evidence_incomplete"]
}
```

Core precedence is `error -> missing -> stale -> partial -> healthy`. `partial`
means the owned graph is usable and source-current but one or more staged,
allowed files lack extractor representation under an explicitly approved
coverage reason; it never means policy-denied scope or an optional integration
is disabled. Fully accounted policy denials do not lower core status. Disabled
or enabled-but-unavailable features also do not lower it; the latter appear in
feature state and warnings so callers can apply policy. Query admission requires
`healthy`, or `partial` only when every limitation is a reason-coded approved
coverage exclusion; `error`, `missing`, `stale`, and unapproved partial states
are refused.

For every loaded manifest version, health emits schema v2 and the compatibility
alias `status` equals `core_status`. The legacy booleans `atlas_available`,
`registry_matches`, and `impact_analysis_trusted` remain during the v2
compatibility window and are derived from feature/trust state. This is an
intentional pre-1.0 correction: consumers needing the old optional-feature
aggregate must pin AtlasWeaver 0.2.x until they migrate.

## Module boundaries

- `privacy.py`: structured path decisions and reason codes;
- `locking.py`: no-follow repository access, lifecycle leases, and descriptor-
  rooted lock authority;
- `manifest.py`/`models.py`: v1/v2 loading and immutable configuration;
- `coverage.py`: strict tracked approvals and recomputed omission admission;
- `lifecycle.py`: orchestration with injected staging, Graphify, adapter, and
  promotion boundaries;
- `queries.py`: health-gated read-only traversal adapters;
- `registry.py`: atomic Atlas registry and journaled Graphify compatibility view;
- `health.py`: core/feature/trust classification only;
- `cli.py`: parsing, stable JSON, and error-code mapping.

No lifecycle module may bypass an existing validator or call Graphify against
the repository root.

## Failure handling

Every mutation validates against a final in-lock projection immediately before
promotion. Drift before promotion aborts and leaves the previous owned output
intact. The graph directory promotion itself is atomic, but repository source
cannot be transacted with it: drift detected after commit returns
`promoted_but_stale`, marks health stale, and exits nonzero rather than claiming
rollback. Cleanup failure follows the same commit boundary: before commit it is
stable `cleanup_failed`; after commit it is `promoted_but_stale` with the
revalidated installed identity (or a completely null identity), a recovery ID,
and no rollback claim. Subprocess output remains bounded and redacted. Signals and timeouts
terminate the owned Graphify process group; cleanup and lock release are covered
by failure-injection tests.

## Testing

- Unit tests for every policy decision and manifest migration.
- Red/green tests proving sensitive-name source is scanned while similarly
  named config/data remains denied.
- Approval tests for exact path/content/adapter/reason binding, self-approval
  rejection, lifecycle races, and trust demotion.
- CLI contract tests for preview/apply, doctor, refresh, query, and stable error
  codes.
- Failure-injection tests at every lifecycle boundary and cleanup point.
- Executable-identity tests for path replacement, in-place launcher mutation,
  symlink swap, and mutation between consecutive Graphify subprocesses.
- Real Graphify 0.9.48 lifecycle tests for code-only and semantic-backend
  preflight behavior.
- Compatibility tests for all legacy low-level commands and health fields.
- Python 3.10 and 3.13 CI matrix.

## Rollout

Ship this subsystem first behind manifest v2 while retaining schema v1 loading.
AtlasWeaver dogfoods `init` preview, `doctor`, and `refresh` before remote
artifact publication is enabled. Release work bumps the single package version,
builds and installs the wheel in a clean smoke environment, force-reinstalls the
local global tool through the reviewed installer, verifies `--version` and new
subcommands, and bootstraps AtlasWeaver's own managed v2 graph. A source-tree
version bump without installed-CLI and resource checks is not a completed
release.
