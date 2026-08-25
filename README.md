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
- `graphifyy==0.9.48`
- Obsidian is optional

## Install

```sh
uv tool install 'git+https://github.com/MarkusMakEvil/atlasweaver.git@v0.1.0'
uv tool install 'graphifyy==0.9.48'
graphify install --platform codex
git clone --depth 1 --branch v0.1.0 https://github.com/MarkusMakEvil/atlasweaver.git
cd atlasweaver
scripts/install-project-knowledge-skill --codex-home "${CODEX_HOME:-$HOME/.codex}"
```

## Add a project

Copy and edit the two files under `examples/`, then run the read-only preflight:

```sh
cp examples/.graphify-project.yaml /path/to/project/.graphify-project.yaml
cp examples/.graphifyignore /path/to/project/.graphifyignore
project-knowledge preflight --repo /path/to/project --json
```

The safe refresh sequence is deliberately explicit:

```sh
project-knowledge scan-secrets --repo /path/to/project --json
project-knowledge stage --repo /path/to/project --destination "$PRIVATE_STAGE" --receipt "$STAGE_RECEIPT" --json
# Run Graphify only against $PRIVATE_STAGE and write native output to $RAW_GRAPH_CANDIDATE.
project-knowledge adapt --repo /path/to/project --staged-input "$PRIVATE_STAGE" --receipt "$STAGE_RECEIPT" --raw-candidate "$RAW_GRAPH_CANDIDATE" --destination "$GRAPH_CANDIDATE" --json
project-knowledge validate --repo /path/to/project --candidate "$GRAPH_CANDIDATE" --json
project-knowledge promote --repo /path/to/project --candidate "$GRAPH_CANDIDATE" --json
```

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

## Health and trust

```sh
project-knowledge health --repo /path/to/project --atlas "$OBSIDIAN_ATLAS" --json
```

- `source_matches:false` means the graph is stale.
- `atlas_available:false` means strict generated-namespace ownership failed.
- `registry_matches:false` means the global graph copy differs.
- `partial` can be an honest accepted state when approved files are bound into
  the source digest but unsupported by the pinned extractor.
- `impact_analysis_trusted:false` or `graph_integrity_degraded` means the graph
  is suitable for navigation only; impact conclusions require source
  verification.

Graphify 0.9.48 does not persist enough pre-build evidence to prove that no
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
