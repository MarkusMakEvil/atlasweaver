# AtlasWeaver Artifact Distribution and Fleet Design

**Date:** 2026-08-26

**Status:** Approved design; implementation plan complete

**Scope:** Deterministic graph bundles, GitHub Release distribution, reusable
CI, packaged agent integration, and project-agnostic multi-repository fleets.

## Objective

Allow every authorized developer and agent to consume a validated graph for an
exact safe source snapshot without rebuilding it locally. Allow an arbitrary
workspace to inspect or refresh multiple independent repositories through one
uniform interface.

## Non-goals

- No product-specific names, defaults, repository layout, or business logic.
- No central daemon or mandatory hosted AtlasWeaver service.
- No publication of unsanitized source, staging directories, receipts, query
  memory, Obsidian notes, or environment values.
- No implicit GitHub write from local read-only commands.
- No background enrollment or refresh; every single-project or fleet mutation
  is explicit.

## Deterministic graph bundle

`artifact pack` captures only policy-approved, owned graph artifacts into a
deterministic ZIP archive. ZIP is used instead of tar to avoid platform-specific
metadata and external decompression requirements. Entries are sorted, use fixed
timestamps and permissions, use `ZIP_STORED`, and never contain links or nested
paths outside the bundle schema. V1 deliberately forgoes compression so archive
bytes do not depend on the host zlib implementation.

The bundle contains:

```text
artifact.json
graphify-out/GRAPH_EVIDENCE.json       # required for every v2 adapter
graphify-out/GRAPH_REPORT.md
graphify-out/graph.html                # only when manifest permits it
graphify-out/graph.json
```

The local ownership manifest is deliberately not distributed: it describes a
promotion performed on one machine and the existing candidate validator rejects
pre-owned candidates. `artifact.json` is the transport manifest; install
revalidates the mapped candidate and creates a fresh local ownership manifest.

`artifact.json` binds schema version, AtlasWeaver version, project ID and UID,
Graphify version, adapter ID, safe source and projection digests, graph digest,
the domain-separated complete non-ownership generation digest, Git commit
OID/algorithm and repository identity when available, build epoch,
channel, and SHA-256 plus byte length for every payload entry. It never self-hashes
`artifact.json`; GitHub attestation binds the complete ZIP digest. Absolute
paths and source literals are forbidden. Ownership schema v2 records
`build_epoch` exactly once when refresh, install, or explicit ownership migration
creates the owned generation. Pack
copies that required value and never reads its invocation clock or environment;
legacy ownership without it must be refreshed or migrated before packing.
Canonical JSON, fixed ZIP metadata, and stored payloads make repeated packs of
the same owned artifact bytes byte-identical across supported runtimes;
deterministic extraction itself is a separate non-goal.

Packing first revalidates live ownership and freshness. Unowned files,
ownership mismatch, stale source, unsupported active HTML, duplicate ZIP names,
non-UTF-8 metadata, path traversal, symlinks, oversized entries, and non-finite
JSON fail closed.

Pack holds the repository lifecycle lock while it descriptor-copies ownership
and approved artifacts into a private snapshot. It validates that completed
copy—not mutable live paths—and recomputes the safe projection before and after
capture. The lock is released only after the snapshot has a single validated
generation; archive construction then reads only that snapshot. Concurrent
promotion, replacement, or source drift has explicit race tests.
The lifecycle root descriptor is also the sole authority for journal, manifest,
ignore/policy, projection, source, ownership, and live artifact reads. An
optional fleet-captured `(device, inode)` is checked on that same open before
state lookup or output creation. A caller-admitted full manifest is also
required under that descriptor before capture; same-inode semantic rewrites
cannot redirect privacy, output, or provider policy. Replacing the repository pathname after lock
acquisition cannot mix replacement validation bytes with promotion/capture from
the original inode. Output publication is separately bound to a retained
no-follow parent descriptor.

Pack, install, pull, and privileged preparation use one shared descriptor-bound
mode-0700 operation root with an opaque recovery ID. Cleanup never leaks a raw
OS exception or suppresses a pending non-ordinary signal. Before mutation it
becomes a closed family cleanup error; after a verified archive link or changed
graph promotion it preserves the committed output/result and reports the
recovery ID. Exact-generation no-op is explicitly non-committed—status is never
used to infer mutation. Publication preserves a committed ZIP but blocks
attestation/upload until cleanup recovery is handled. The commit marker is
retained before the first post-output or post-promotion callback. Any ordinary
exception after that marker, whether cleanup also fails or not, is normalized
to the same family-specific stale/recovery outcome and never erases the
committed ZIP or graph identity. A pending non-ordinary `BaseException` still
propagates.
Pack and privileged preparation share a wrapper-owned private commit-state
object. The ZIP helper flips it immediately after the no-replace output link
and parent fsync, before its post-link checkpoint, temporary-name cleanup, or
return. Thus a helper exception combined with operation-root cleanup failure
still preserves the ZIP and reports output recovery rather than a false
pre-output cleanup failure.

The compatibility registry declares evidence required for every v2 adapter.
Graphify 0.9.48 therefore packs an incomplete-but-required evidence document
whose limitations force navigation trust. Missing, substituted, or downgraded
evidence fails ownership, pack, and install validation; only explicitly handled
legacy ownership may omit it and can never become trusted or remotely published.

The v1 archive limit is 256 MiB total with at most 255 MiB of entry payload;
individual entries are capped at 128 MiB. The parser accepts only the exact
schema paths above, UTF-8 names, single-disk non-ZIP64 archives, and stored
regular entries. It rejects encrypted or compressed entries, data descriptors
it cannot cross-check,
duplicate normalized names, local/central header disagreement, trailing
archive data, links, and special files. Limits are checked from both headers
and again while streaming through a descriptor-confined mode-0700 directory.
No manifest setting can raise them.

## Install and pull

`artifact install --repo <repo> --bundle <zip>` validates into a private
candidate, checks exact project ID/UID plus current safe-source and projection
digests, then uses the existing transactional graph promotion. It never extracts
directly over live output.

Install first validates archive structure and `artifact.json` without trusting
archive paths. It maps each permitted `graphify-out/<name>` entry to `<name>` in
a private root-level raw candidate, independently checks every byte/digest and
transport field, and runs the ordinary candidate validator on the already
adapted artifact set. It rejects any bundled ownership file. Only then does it
regenerate local ownership and invoke the existing promotion path.
From lifecycle acquisition through post-promotion verification, install uses a
single lease-captured repository descriptor for the journal, manifest, safe
projection, live ownership, promotion, and health. It never reopens the
repository pathname. A root rename/replacement either leaves the whole
transaction bound to the original inode or is rejected by the optional expected
identity before any state creation. Direct, fleet, and workflow callers also
pass their full admitted manifest; install requires semantic equality inside
the lock before staging or promotion.

For schema-v2 evidence, the closed `artifact.json` payload descriptor captured
and verified from the archive supplies the external expected digest to parsing
and candidate validation. Install never hashes the mapped candidate evidence
at the validator call site and treats that self-derived value as authority.

`pull --repo <repo>` is a networked mutation and requires manifest provider
`github-release`. It resolves a release asset for the configured repository,
project ID/UID, channel, and current safe-source plus projection digests;
downloads to a private bounded temporary file; validates the bundle; and
installs atomically. Redirects,
content length, total bytes, timeouts, TLS, repository identity, release
identity, and asset name are constrained. `GITHUB_TOKEN` is read only for the
request and is never logged or inherited by Graphify.
The credential object admits only a bounded nonempty exact string without
controls, CR/LF, or surrounding whitespace before any HTTP header/request is
constructed; invalid values map to a constant GitHub credential error.
Pull first opens a noncreating repository descriptor, validates the optional
fleet identity, and reads journal, manifest/provider, and projection only
through it before any authenticated request. Install must then reacquire the
same identity under the lifecycle lock. A replacement manifest can therefore
neither redirect GitHub credentials nor receive an installed graph.

Verified pull authorization is a same-process, identity-bound, single-use
capability keyed to the exact archive digest/length and GitHub asset ID. Its
registration context establishes cleanup before inserting into the locked
registry, so cancellation immediately after insertion cannot leak authority;
consumption atomically pops before validation/promotion.

Provider identity is configured only in the project manifest; CLI, fleet, and
workflow inputs cannot override it:

```yaml
artifacts:
  provider: github-release
  host: github.com
  repository: owner/repository
  repository_id: 123456789
  channel: main
  source_ref: refs/heads/main
  signer_workflow: owner/atlasweaver/.github/workflows/atlasweaver-publish.yml
  signer_digest: 0123456789abcdef0123456789abcdef01234567
```

All fields are required for `github-release` and forbidden for `none`.
AtlasWeaver v1 supports only `github.com`; enterprise hosts require a separate
transport and attestation design. Authenticated TLS GitHub API name lookup must
return the configured immutable numeric repository ID, which is also bound into
the bundle and local registry namespace. Release resolution binds repository ID, release ID, tag,
asset ID, exact asset name, size, and digest in a private download receipt.

In bundle schema v1, transport fields are present only for
`github-release`; a locally packed `provider: none` bundle has `channel: null`
and no repository/release identity. Install always enforces project and snapshot
identity. A `github-release` bundle is installable only through the same-process
verified receipt created by `pull`; the public `artifact install` command does
not accept a caller-fabricated receipt or bypass flag. Offline attestation
installation is outside v1.

The transport talks only to `api.github.com`, `github.com`,
`objects.githubusercontent.com`, and `release-assets.githubusercontent.com`.
At most three HTTPS redirects are accepted, each URL and resolved host is
revalidated, and redirects may never downgrade to HTTP. Authorization is sent
only to `api.github.com` and is stripped before every cross-host redirect. The
client ignores ambient proxy, netrc, cookie, and redirect-auth configuration.

The rolling release tag is `atlasweaver-graph-<project-uid>-<channel>`. The exact
asset name is
`atlasweaver-graph-<project-uid>-<source-digest>-<projection-digest>-<bundle-sha256>.zip`.
The source/projection digests prevent policy/ignore changes with identical safe
bytes from colliding; the final component is the SHA-256 of the complete
deterministic ZIP, so changes limited to transport metadata such as Git
identity, build epoch, or AtlasWeaver version cannot collide. The manifest's
separate generation digest still distinguishes changed evidence, report, or
optional HTML even when graph bytes are identical. A clean clone
selects the newest strictly ordered asset matching its UID/source/projection
prefix, then requires the streamed ZIP SHA-256 and GitHub asset digest to equal
the exact name component. `pull` first performs
bounded structural parsing of `artifact.json` as untrusted data to obtain its
claimed `git_commit_oid`, but it
does not parse graph content or install anything. It then requires successful
GitHub artifact-attestation verification of the downloaded ZIP SHA-256 with the
equivalent exact policy:

```text
gh attestation verify <zip> --hostname github.com --repo <owner/repository>
  --signer-workflow <configured-workflow> --signer-digest <configured-sha>
  --source-ref <configured-ref> --source-digest <claimed-git-commit-oid>
  --predicate-type https://slsa.dev/provenance/v1 --deny-self-hosted-runners
  --format json
```

The capability-checked verifier receives only an isolated `GH_CONFIG_DIR`, the
required token, and a minimal environment, with bounded/redacted output. At
least one verification result must match the ZIP subject digest, and every
returned result must have an identical authority-relevant authenticated
projection. Repeated identical valid attestations are accepted idempotently;
any conflicting or malformed result fails closed. AtlasWeaver treats
only signature certificate/timestamp claims and values explicitly enforced by
the verifier flags as authenticated; free-form statement predicate values remain
untrusted. The attestation authenticates the repository slug, workflow identity,
source ref, source commit, and subject digest. Independently, the TLS GitHub API
receipt authenticates the immutable numeric repository ID plus release/asset
identity. Pull requires those two projections to agree with the strict manifest
and bounded untrusted bundle claims in their corresponding domains. Missing, ambiguous, or unverifiable
provenance fails closed; a self-consistent `artifact.json` is never sufficient
authority.

The exact subject/signer/source flag projection is implemented once as a
receipt-free attestation-policy verifier. The privileged publication workflow
can call that primitive immediately after GitHub creates an attestation and
before any Release asset exists, using its descriptor-validated prepared bundle,
strict manifest, and authenticated workflow context. It cannot fabricate a
download receipt or create pull authorization. Pull wraps the same primitive
with the independently authenticated repository/release/asset
`DownloadReceipt` and all bundle/config comparisons; only that wrapper may lead
to same-process install authorization. Thus pre-upload signer verification and
post-download asset verification share cryptographic policy without conflating
their distinct authorities.

Download and attestation verification may happen before locking. Install then
acquires the repository lifecycle lock, recomputes the current projection, and
requires it to match the bundle immediately before promotion. Pre-promotion
drift leaves the old graph intact. Post-promotion drift returns
`promoted_but_stale`, marks health stale, and returns the global result exit
code 3 defined by the core lifecycle contract instead of an error envelope.
Pull retains the install execution's private promotion bit immediately on
return from the nested installer. Therefore an outer manifest/callback failure,
alone or coincident with pull-root cleanup failure, preserves only the nested
descriptor-revalidated installed identity and returns `promoted_but_stale`;
pre-commit failures remain closed GitHub cleanup errors.

## Publish

Local `publish` is intentionally absent in v1. Publication happens only in the
reusable GitHub workflow after build, validation, packing, and a second health
check. The publish/attest job receives `contents: write`, `attestations: write`,
and `id-token: write`; every other job has explicit `contents: read` or no token
permissions. Pull-request checks never upload a release graph.

Refresh/pack runs in a read-only-token build job. The privileged job does not
run consumer tests, caller shell, Graphify extraction, or a semantic backend. It
checks out the exact source commit as data, downloads the workflow artifact,
recomputes the safe projection, validates and repacks through trusted
AtlasWeaver code, generates the attestation, and uploads the release asset. No
credential is present in a job that executes consumer-configured commands.

The workflow updates the rolling release and retains the 20 most recent
digest-addressed assets. It never deletes an asset until the new bundle has
uploaded and its remote size and digest metadata have been verified.

## Reusable GitHub workflows

AtlasWeaver provides two `workflow_call` entry points:

- `atlasweaver-check.yml`: install pinned versions, run doctor, secret scan,
  manifest validation, graph freshness, and coverage;
- `atlasweaver-publish.yml`: on a protected main-like branch, run the approved
  refresh, validate, pack, attest, and upload the release asset.

Inputs are typed and limited to a confined repository root, supported Python
version, compatibility-registry backend/model, deep mode, and whether impact
trust is required. Provider identity, source ref, artifact channel, project ID,
and signer identity come only from the manifest and GitHub context. Semantic
credential admission follows the selected compatibility-registry entry: a
credential-required backend requires its named secret, while an admitted
credentialless backend proceeds without a synthetic token. The workflow never
silently switches to code-only extraction. A trusted no-secret admission step
runs before Graphify installation and output: code-only requires empty
backend/model and `deep-mode=false`, while semantic mode requires both a
non-empty admitted backend and public model and alone may enable deep mode.
Deep-only and half-configured inputs fail before secret lookup. Caller-provided shell, test commands, arbitrary flags,
and output paths are not accepted.

Publish requires `github.ref_type == 'branch'`, `github.ref_protected == true`,
the exact manifest `source_ref`, `HEAD == github.sha`, the configured numeric
`github.repository_id`, and a clean tracked checkout whose safe projection is
entirely represented by that commit. The reusable workflow and every action are
referenced by full commit SHA. Concurrency uses immutable repository ID, project
UID, and channel; the privileged job rechecks all assertions instead of trusting
caller booleans.

The consumer `repo-root` is not concatenated into a pathname. One trusted
internal boundary accepts only `.` or a bounded POSIX-relative segment list,
rejects absolute/parent/control/backslash forms, descriptor-walks from the
no-follow consumer checkout, rejects symlink/non-directory components and an
inode alias to the trusted tool checkout, and retains the resulting access.
Check runs preflight/doctor/scan/health in one scope; inspect/build/publish reuse
the same resolver and pass its expected root identity/full admitted manifest
into every lifecycle boundary. A root-entry swap fails before secret, Graphify,
Git, preparation, or GitHub mutation; replacement bytes are never adopted.

Privileged preparation writes a bounded canonical private handoff under
`RUNNER_TEMP` binding the admitted root identity, full-manifest digest,
authenticated workflow context, and prepared ZIP inode/size/digest. The later
attestation-verification and upload verbs are separate processes: each reopens
the same descriptor boundary, requires exact handoff/root/manifest/context/ZIP
equality, and only then reads its step-scoped token or invokes `gh`/GitHub. A
swap or handoff mutation between phases therefore cannot inherit authority from
an earlier successful prepare.

Those Git assertions are rooted in the same retained repository descriptor as
the lifecycle-locked manifest and safe projection. For a nested project, the
workflow authority retains both the consumer-checkout descriptor and the exact
selected-root segment tuple: `.git` is opened only at checkout root, while
index/worktree/commit-tree checks are literal-prefix scoped and strip that
prefix before comparison with the project's staged paths. Tracked sibling
changes outside the project are irrelevant; any selected-root tracked delta or
untracked safe byte still fails. The real private, bounded Git authority is workflow-internal and Linux-only: the privileged workflow is
fixed to GitHub-hosted Ubuntu and capability-checks traversable `/proc/self/fd`
directory bindings. It opens `.git`/worktree metadata with no-follow descriptor
operations and runs fixed read-only plumbing from an `fchdir()`-bound child
using inherited descriptor paths. Non-Linux internal invocation fails closed;
cross-platform unit tests use a non-public injected runner and the rest of the
product remains cross-platform. Publication never uses `git -C <repo>`, a pathname `cwd`, or
an executable/config supplied by a caller. It proves object format, exact HEAD,
index/tree equality, tracked cleanliness, and byte-for-byte equality between
the commit's privacy-filtered tree and the staged safe projection. A repository
path or `.git` swap therefore either leaves every check on the original opened
inode or fails closed before bundle parsing/output; it can never authorize a
replacement checkout.

## Packaged agent integration

The reviewed `using-project-knowledge-graphs` skill and workflow reference ship
as package resources in the wheel. `install-agent --platform <name>` delegates
Graphify platform integration to the pinned Graphify command and atomically
installs AtlasWeaver's managed skill/instruction fragment for supported
platforms. Initial supported targets are `codex` and generic `agents`.
Additional targets are outside this release and require their own adapter and
ownership tests.

The command refuses to replace unmanaged or locally modified destinations.
`uninstall-agent` removes only entries carrying AtlasWeaver ownership metadata.
Repository hooks remain separate `hook install`/`hook uninstall` commands and
are never part of ordinary package installation.

## Universal fleet model

Fleet configuration is project-agnostic and stored outside individual project
manifests:

```yaml
schema_version: 1
projects:
  - id: api
    repository: repos/api
  - id: documentation
    repository: repos/docs
defaults:
  max_parallel: 2
```

There are no product-specific fields. Repository paths must be unique,
confined beneath the real fleet root, non-symlink directories, and contain a
manifest whose `project_id` equals the fleet entry ID. Nested repositories,
duplicate canonical paths, duplicate IDs, and worktree aliases fail validation.
`max_parallel` defaults to 2 and is confined to the inclusive range 1 through
8.

Every project manifest carries an immutable `project_uid`. The only Graphify
global-registry key is `atlasweaver/<project_uid>`; project ID is display data,
never the lookup key. Fleet validation rejects duplicate UIDs, and query output
is namespaced by UID plus ID. The UID is also bound into bundle metadata,
release tags, ownership, and download receipts, so unrelated fleets can safely
reuse labels such as `api`.

Each loaded fleet project also retains the `(device, inode)` from the exact
no-follow repository descriptor used to capture its manifest. This identity is
library-only and never serialized. Aggregate admission and every per-project
Core/artifact boundary must atomically compare it to `fstat()` of their own
opened root before journal/manifest reads, state creation, credential lookup,
network, subprocess, or registry capture. A replacement is the stable path-free
`fleet_repository_changed`; an aggregate replacement discovered before dispatch
prevents all credential lookup, while a later per-project replacement fails
only that project and never touches the replacement. A pre-dispatch `stat()`
followed by pathname reopening is not authority.

Root identity is necessary but not sufficient for credential authority. The
pre-secret aggregate gate retains each exact full semantic manifest. After the
closed credential/environment request is constructed, the coordinator reloads
every manifest and requires full equality with that tuple before it may inspect
credential fields or allocate workers. Each worker reload repeats equality with
the retained aggregate admission, and the consuming Core/artifact API receives
that same expected manifest for its final descriptor-rooted check. Changes to
provider/repository/signer/source ref, compatibility, privacy, features, or any
other field while ID/UID stay fixed are `fleet_manifest_changed`; they can
never redirect an already-read token or expand staged content.

Commands:

- `fleet doctor --workspace <file>`: read-only configuration and capability
  inspection;
- `fleet health --workspace <file>`: aggregate per-project core, feature, and
  trust state;
- `fleet pull --workspace <file>`: bounded-parallel artifact downloads and
  atomic per-project installs;
- `fleet refresh --workspace <file>`: bounded-parallel independent refreshes;
- `fleet registry-sync --workspace <file>`: explicit, globally serialized sync
  for projects whose manifests enable the registry feature;
- `fleet query --workspace <file>`: resolve the atomic AtlasWeaver global
  registry and require its Graphify compatibility projection to match after
  explicit sync.

Operations are isolated per repository. A failure never rolls back a different
repository that already completed, but the aggregate command returns nonzero
and lists stable per-project statuses. Output ordering follows configuration,
not completion order. `--project <id>` can select a subset.

Fleet validates every selected repository identity and init journal through one
descriptor before manifest use, credential lookup, or dispatch and rechecks the
same expected identity immediately at each operation boundary; root replacement
is `fleet_repository_changed`, while recoverable/corrupt state is
`init_recovery_required`. Neither is bypassed by direct library delegation.
The same three-stage pre-secret/aggregate/worker sequence binds full manifest
semantics, not only repository identity.
Semantic refresh requires a paired
backend and Core-validated public model identifier before reading either the
generic bridge secret or a backend credential.
The aggregate semantic request may temporarily hold one immutable captured
environment per selected UID only inside the coordinator. After full admission,
the coordinator derives a fresh closed work request for each project containing
only that project's options and matching environment; only these per-project
requests cross the executor boundary. Neither the runner argument nor its
future closure can inspect the aggregate tuple, another UID, credential name,
or value. The production runner materializes one fresh mapping for its own
Graphify child and discards it afterward.

Fleet health, pull, and local refresh do not require Git and operate on the
safe filesystem projection. Shared publication requires CI to prove a clean
checkout at an exact commit. `--allow-dirty` does not exist in the publish
workflow.

## Local state and telemetry

Content-free state under ignored `.project-knowledge/` records last successful
operation, duration, safe file count, coverage counts, source/projection/graph/
generation digests, the descriptor-validated installed build epoch, artifact
channel, and stable failure code. A normal install result, state, and CI summary
preserve that same verified generation identity. After commit, live ownership
is descriptor-revalidated and promotion summary fields are comparison-only. A
committed-but-unverifiable install result uses null graph digest, generation
digest, epoch, and changed state as one indivisible unknown identity; state
retains the last verified success and records only the new stable failure,
never the unverifiable identity as a success. No source labels, query text,
filenames denied by policy, environment values, or secret fingerprints are
sent remotely. AtlasWeaver has no automatic telemetry endpoint.

CI exposes the same data through a job summary and machine-readable artifact.

## Module boundaries

- `bundles.py`: deterministic pack, parse, validate, and install;
- `github_artifacts.py`: bounded GitHub Release resolution and download;
- `fleet.py`: universal configuration and bounded operation coordinator;
- `agent_install.py`: packaged resource installation and ownership;
- `workflow_boundary.py`: descriptor-confined internal check/inspect/build roots;
- `workflow_publish.py`: privileged prepare/handoff/verify/upload sequencing;
- `.github/workflows/atlasweaver-check.yml`: reusable read-only workflow;
- `.github/workflows/atlasweaver-publish.yml`: reusable publish workflow.

Network transport never validates or promotes artifacts itself. Fleet code
never reimplements project lifecycle operations.

The implementation injects clock/epoch selection, ZIP reader/writer, HTTPS
transport, release resolver, attestation verifier, filesystem promotion, and
fleet operation runners behind narrow protocols. Tests therefore assert actual
request construction, redirect header stripping, streamed limits, and rollback
rather than relying on log inspection.

## Testing

- Deterministic bundle golden tests across Python 3.10 and 3.13.
- Archive traversal, duplicate normalized name, local/central mismatch,
  encryption/compression, ZIP64/multi-disk, trailing-data, symlink/special-file,
  aggregate/entry limit, malformed
  JSON, digest mismatch, wrong-project, and stale-source tests.
- Fake HTTPS transport tests for redirects, auth redaction, truncation,
  timeouts, cross-host authorization stripping, and repository/release/asset
  mismatch.
- Attestation tests for subject digest, repository slug, signer workflow/digest,
  source ref/commit, predicate, unavailable provenance, and malformed verifier
  JSON; separate API-receipt tests cover immutable numeric repository ID and
  release/asset identity plus cross-agreement with config/bundle.
- Workflow contract tests that pin actions by full commit SHA and assert least
  permissions and concurrency controls; root escape/symlink/swap plus immutable
  handoff races run for check, inspect, build, prepare, verify, and upload.
- Fleet tests for path confinement, duplicate IDs/paths, worktrees, stable
  ordering, bounded parallelism, partial failure, project selection, and full
  pre-secret/aggregate/worker manifest binding.
- Cleanup fault tests before/after archive publication and changed/no-op graph
  promotion require closed recovery semantics and no raw OS errors.
- Installer ownership and rollback tests for packaged resources.
- End-to-end pack/install and multi-project health tests.

## Rollout

First ship local pack/install and reusable read-only checks. Enable GitHub
publication for AtlasWeaver itself, then test pull from a clean clone. Only
after that enable generic fleet pull/refresh. Consumer repositories opt in by
manifest and workflow reference; no repository is enrolled implicitly.
