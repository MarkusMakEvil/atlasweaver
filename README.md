# AtlasWeaver

AtlasWeaver turns a privacy-filtered source tree into a validated Graphify code
graph and an optional transactionally managed Obsidian atlas.

It is designed for agents and developers who need architecture, dependency,
impact-analysis, and onboarding context without exposing credentials, runtime
state, private notes, or arbitrary local files to the graph builder.

## What it provides

- deterministic allowlisted staging with non-negotiable secret and private-path
  exclusions;
- validation and crash-safe promotion of `graph.json`, `GRAPH_REPORT.md`, and
  optional `graph.html`;
- strict provenance and source-digest freshness checks;
- a generated Obsidian namespace that never owns human `Notes/` or `.obsidian/`;
- a global Agent Skill for safe graph-first repository navigation;
- fail-closed handling of symlinks, unknown generated files, stale graphs, and
  interrupted promotions.

AtlasWeaver wraps the official Graphify workflow; it does not replace Graphify
or treat inferred graph relationships as proof.

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- `graphifyy==0.9.51`
- Obsidian is optional

## Install

Install the reviewed release stack as one uv tool. This exposes
`project-knowledge`, `graphify`, and `graphify-mcp` from the same environment:

```sh
uv tool install --force --with-executables-from 'graphifyy==0.9.51' 'https://github.com/MarkusMakEvil/atlasweaver/releases/download/v0.3.5/atlasweaver-0.3.5-py3-none-any.whl'
project-knowledge install-agent --platform codex --json
```

For a reviewed source checkout instead:

```sh
git clone --depth 1 --branch v0.3.5 https://github.com/MarkusMakEvil/atlasweaver.git
cd atlasweaver
python3 scripts/install-project-knowledge-tool
project-knowledge install-agent --platform codex --json
```

## Add a project

Initialize each repository so it receives its own UUID; do not copy the
`project_uid` from the example manifest. The ordinary safe lifecycle is:

```sh
project-knowledge init --repo /path/to/project --project-id my-project --include-root src --apply --json
project-knowledge doctor --repo /path/to/project --json
project-knowledge refresh --repo /path/to/project --code-only --json
project-knowledge health --repo /path/to/project --json
project-knowledge query --repo /path/to/project "authentication" --json
```

`init` and `manifest-migrate` are preview-only unless `--apply` is explicit.
Code-only refresh needs no model or credential. Semantic refresh requires a
declared backend and public model identifier:

```sh
project-knowledge refresh --repo /path/to/project --backend <declared-backend> --model <public-model-id> --deep --json
```

Credentials are read only from the selected compatibility entry's environment
allowlist. AtlasWeaver has no implicit backend or model. Sensitive source names
are scanned; sensitive data names are denied without reading their content.
Known extractor omissions require an exact path/content/adapter/reason approval
in `.atlasweaver-coverage.yaml`; a byte change invalidates that approval.

Registry use is optional and universal across unrelated repositories:

```sh
project-knowledge registry-status --json
project-knowledge registry-sync --repo /path/to/project --apply --json
```

Disabled optional features do not lower core health. Query operations are
immutable, bounded snapshots. A `navigation` trust result is useful for finding
code, but every consequential impact claim still needs source verification.

## Portable bundles and universal fleets

An exact validated generation can be moved to a matching clean clone without
re-running Graphify:

```sh
project-knowledge artifact pack --repo <repo> --output graph.zip --json
project-knowledge artifact install --repo <matching-clone> --bundle graph.zip --json
project-knowledge pull --repo <repo> --json
project-knowledge install-agent --platform codex --json
```

Local installation accepts only `provider: none`. `pull` is the explicit
networked path for `github-release`: it binds the GitHub repository numeric ID,
commit, tag, asset digest, workflow signer, attestation, source digest, and
privacy projection before promotion. AtlasWeaver follows at most three HTTPS
redirects across its fixed GitHub host allowlist and never forwards API
authorization across hosts. Bundles are deterministic uncompressed ZIPs capped
at 256 MiB total, 255 MiB aggregate payload, and 128 MiB per entry. There is no
local publish command.

The optional provider block is project-owned and contains no product defaults:

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

Fleet mode is a universal coordinator over arbitrary schema-v2 repositories.
It contains no organization-specific IDs, paths, defaults, or business logic:

```yaml
schema_version: 1
projects:
  - id: api
    repository: repos/api
  - id: documentation
    repository: repos/documentation
defaults:
  max_parallel: 2
```

```sh
project-knowledge fleet health --workspace fleet.yaml --json
project-knowledge fleet refresh --workspace fleet.yaml --code-only --json
project-knowledge fleet registry-sync --workspace fleet.yaml --json
project-knowledge fleet query --workspace fleet.yaml query ProjectManifest --limit 20 --json
```

Fleet ordering follows configuration order, parallelism is bounded to 1–8,
and one repository's failure never rolls back another completed repository.
The registry key is always `atlasweaver/<project_uid>`; display IDs are never
authority. Registry synchronization remains a separate explicit mutation.
Reusable check and split-privilege publication workflows are opt-in. Rolling
publication retains the 20 newest digest-addressed assets only after the new
asset has been remotely verified.

### Low-level recovery

Use the manual pipeline only when diagnosing or recovering a failed lifecycle:

```sh
project-knowledge scan-secrets --repo /path/to/project --json
project-knowledge stage --repo /path/to/project --destination "$PRIVATE_STAGE" --receipt "$STAGE_RECEIPT" --json
# Run Graphify only against $PRIVATE_STAGE and write native output to $RAW_GRAPH_CANDIDATE.
project-knowledge adapt --repo /path/to/project --staged-input "$PRIVATE_STAGE" --receipt "$STAGE_RECEIPT" --raw-candidate "$RAW_GRAPH_CANDIDATE" --destination "$GRAPH_CANDIDATE" --json
project-knowledge validate --repo /path/to/project --candidate "$GRAPH_CANDIDATE" --json
project-knowledge promote --repo /path/to/project --candidate "$GRAPH_CANDIDATE" --json
```

Never point Graphify at the repository root. AtlasWeaver never commits or
pushes implicitly.

`adapt` never mutates Graphify's raw output. It verifies the staged receipt,
copies only the policy-approved artifacts into a separate candidate, and adds
the project identity, source digest, extraction coverage, and graph-integrity
metadata required by validation.

`scan-secrets` returns only detector IDs, relative paths, line numbers, and
SHA-256 fingerprints. Structured credentials and private keys are
non-bypassable. Reviewed false positives from the contextual assignment rule
can be bound to an exact path and fingerprint in
`.graphify-secret-exceptions.yaml`; changing the source invalidates the
exception.

For Obsidian, export into a private candidate directory first:

```sh
project-knowledge atlas-prepare --repo /path/to/project --candidate "$ATLAS_CANDIDATE" --json
project-knowledge atlas-promote --repo /path/to/project --candidate "$ATLAS_CANDIDATE" --atlas "$OBSIDIAN_ATLAS" --json
```

Never point Graphify at an unsanitized repository or write its export directly
into a live vault.

## Graphify compatibility and evidence

The compatibility registry is the sole authority for supported Graphify
versions. AtlasWeaver supports reviewed 0.9.48 manifests and uses 0.9.51 for
new projects. Both run the reviewed official sequence: code-only
`extract --no-cluster`, `diagnose multigraph --undirected --json`, then
normalized `cluster-only --no-label --no-viz` when HTML is disabled. The
resulting schema-2 graph is bound to `GRAPH_EVIDENCE.json`, its extraction
invocation, source and projection digests, and the ownership record.

Graphify 0.9.51 remains navigation-only because its post-dedup artifact cannot
prove complete pre-dedup occurrence lineage. A future trusted-impact adapter
therefore requires sanitized official fixtures, an explicit complete-lineage
capability, a reviewed fixture digest, compatibility tests, and reviewed
adapter code. Scheduled upstream-probe reports are advisory and never declare
a version supported; support changes only through the reviewed registry.

## Health and trust

```sh
project-knowledge doctor --repo /path/to/project --json
project-knowledge health --repo /path/to/project --json
```

- `source_matches:false` or `projection_matches:false` means the graph is stale;
  successful promotion followed by immediate drift exits with status 3.
- `atlas_available:false` means strict generated-namespace ownership failed.
- `registry_matches:false` means the global graph copy differs.
- `features.artifacts.status:configured` means a remote provider passed strict
  configuration validation; it does not claim that a network pull was attempted.
- `partial` can be an honest accepted state when approved files are bound into
  the source digest but unsupported by the pinned extractor.
- `impact_analysis_trusted:false` or `graph_integrity_degraded` means the graph
  is suitable for navigation only; impact conclusions require source
  verification.

Graphify 0.9.51 does not persist enough pre-build evidence to prove that no
same-endpoint edges collapsed. AtlasWeaver therefore records unknown collapsed
edge evidence and keeps impact analysis untrusted while still rejecting final
graphs with missing or dangling endpoints.

Use graph results as navigation evidence. Verify decisive or inferred claims
against current source before changing behavior.

`track_html` defaults to `false` in the example because HTML is active content.
Enable it only when you trust the Graphify executable and intend to open the
local visualization.

## Development

```sh
uv sync --dev
uv run pytest -q
uv run python -m compileall -q src tests
uv build
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

Apache-2.0.
