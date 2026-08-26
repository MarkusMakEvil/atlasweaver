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
graphify-out/graph.json
graphify-out/GRAPH_REPORT.md
graphify-out/GRAPH_EVIDENCE.json       # required for every v2 adapter
graphify-out/graph.html                # only when manifest permits it
```

The local ownership manifest is deliberately not distributed: it describes a
promotion performed on one machine and the existing candidate validator rejects
pre-owned candidates. `artifact.json` is the transport manifest; install
revalidates the mapped candidate and creates a fresh local ownership manifest.

`artifact.json` binds schema version, AtlasWeaver version, project ID and UID,
Graphify version, adapter ID, safe source and projection digests, graph digest,
Git commit OID/algorithm and repository identity when available, build epoch,
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

`pull --repo <repo>` is a networked mutation and requires manifest provider
`github-release`. It resolves a release asset for the configured repository,
project ID/UID, channel, and current safe-source plus projection digests;
downloads to a private bounded temporary file; validates the bundle; and
installs atomically. Redirects,
content length, total bytes, timeouts, TLS, repository identity, release
identity, and asset name are constrained. `GITHUB_TOKEN` is read only for the
request and is never logged or inherited by Graphify.

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
transport and attestation design. Name lookup must return the configured
immutable numeric repository ID, which is also bound into the bundle and local
registry namespace. Release resolution binds repository ID, release ID, tag,
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
`atlasweaver-graph-<project-uid>-<source-digest>-<projection-digest>.zip`.
Including both full digests prevents policy/ignore changes with identical safe
bytes from colliding or resolving an older generation. `pull` first performs
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
required token, and a minimal environment, with bounded/redacted output. Exactly
one verification result must match the ZIP subject digest. AtlasWeaver treats
only signature certificate/timestamp claims and values explicitly enforced by
the verifier flags as authenticated; free-form statement predicate values remain
untrusted. It requires the authenticated immutable repository ID, workflow
identity, source ref, and source commit to match the manifest, untrusted bundle
claim, and authenticated GitHub API receipt. Missing, ambiguous, or unverifiable
provenance fails closed; a self-consistent `artifact.json` is never sufficient
authority.

Download and attestation verification may happen before locking. Install then
acquires the repository lifecycle lock, recomputes the current projection, and
requires it to match the bundle immediately before promotion. Pre-promotion
drift leaves the old graph intact. Post-promotion drift returns
`promoted_but_stale`, marks health stale, and exits nonzero as defined by the
core lifecycle contract.

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
content requires a named backend secret; the workflow never silently switches
to code-only extraction. Caller-provided shell, test commands, arbitrary flags,
and output paths are not accepted.

Publish requires `github.ref_type == 'branch'`, `github.ref_protected == true`,
the exact manifest `source_ref`, `HEAD == github.sha`, the configured numeric
`github.repository_id`, and a clean tracked checkout whose safe projection is
entirely represented by that commit. The reusable workflow and every action are
referenced by full commit SHA. Concurrency uses immutable repository ID, project
UID, and channel; the privileged job rechecks all assertions instead of trusting
caller booleans.

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

Fleet health, pull, and local refresh do not require Git and operate on the
safe filesystem projection. Shared publication requires CI to prove a clean
checkout at an exact commit. `--allow-dirty` does not exist in the publish
workflow.

## Local state and telemetry

Content-free state under ignored `.project-knowledge/` records last successful
operation, duration, safe file count, coverage counts, source and graph digests,
artifact channel, and stable failure code. No source labels, query text,
filenames denied by policy, environment values, or secret fingerprints are
sent remotely. AtlasWeaver has no automatic telemetry endpoint.

CI exposes the same data through a job summary and machine-readable artifact.

## Module boundaries

- `bundles.py`: deterministic pack, parse, validate, and install;
- `github_artifacts.py`: bounded GitHub Release resolution and download;
- `fleet.py`: universal configuration and bounded operation coordinator;
- `agent_install.py`: packaged resource installation and ownership;
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
- Attestation tests for subject digest, immutable repository ID, signer
  workflow/digest, source ref/commit, predicate, unavailable provenance, and
  malformed verifier JSON.
- Workflow contract tests that pin actions by full commit SHA and assert least
  permissions and concurrency controls.
- Fleet tests for path confinement, duplicate IDs/paths, worktrees, stable
  ordering, bounded parallelism, partial failure, and project selection.
- Installer ownership and rollback tests for packaged resources.
- End-to-end pack/install and multi-project health tests.

## Rollout

First ship local pack/install and reusable read-only checks. Enable GitHub
publication for AtlasWeaver itself, then test pull from a clean clone. Only
after that enable generic fleet pull/refresh. Consumer repositories opt in by
manifest and workflow reference; no repository is enrolled implicitly.
