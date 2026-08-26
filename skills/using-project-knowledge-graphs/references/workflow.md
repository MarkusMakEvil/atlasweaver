# Project knowledge workflow

Read only the section for the active mode. Commands use explicit paths; inspect `--help` if the installed contract differs. Prefer `--json` for machine-readable decisions.

## Detect and query

1. Run `project-knowledge detect --repo <repo> --json`.
2. Run `project-knowledge doctor --repo <repo> --json`, then
   `project-knowledge health --repo <repo> --json`. If admitted, use
   `project-knowledge query`, `path`, `explain`, or `affected`; do not query
   opaque native output directly.
3. Open only the source files needed to verify decisive or inferred claims. Report provenance as `Graphify-extracted`, `Graphify-inferred`, or `source-verified`.
4. If missing or stale, disclose that state. Do not silently rebuild.

## Health

Run:

```sh
project-knowledge health --repo <repo> --json
```

Supply `--atlas <atlas>` only when that local state is in scope. Treat source-digest mismatch as authoritative freshness evidence; registry matching is an internal health input, not a CLI path argument.
Disabled optional features do not make core health partial. Treat
`trust.impact:navigation` and its limitations as a hard boundary on impact
claims: navigation remains useful, but source verification is mandatory.

## Secret triage

Run the read-only redacted scan before requesting staging approval:

```sh
project-knowledge scan-secrets --repo <repo> --json
```

The command returns detector IDs, confined paths, line numbers, and
fingerprints, never matched values. Structured credential findings are
non-bypassable. A reviewed `.graphify-secret-exceptions.yaml` entry can suppress
only `generic_secret_assignment` and must bind the exact path, detector,
fingerprint, and review reason.

## Bootstrap or refresh

If `project-knowledge` is unavailable, run the reviewed repository onboarding
command once: `python3 scripts/install-project-knowledge-tool`. Then verify
`command -v project-knowledge` before continuing.

Before mutation, show the repository, include roots, immutable global deny set,
project output, and optional destinations. Obtain exact approval for the
proposed mutation and paths.

1. Initialize or migrate only through preview then explicit apply:

   ```sh
   project-knowledge init --repo <repo> --project-id <id> --include-root src --json
   project-knowledge init --repo <repo> --project-id <id> --include-root src --apply --json
   project-knowledge manifest-migrate --repo <repo> --json
   ```

2. Run the read-only doctor, then the authorized high-level refresh:

   ```sh
   project-knowledge doctor --repo <repo> --json
   project-knowledge refresh --repo <repo> --code-only --json
   ```

   Semantic mode must declare both backend and public model ID; credentials
   come only from the compatibility allowlist:

   ```sh
   project-knowledge refresh --repo <repo> --backend <backend> --model <model> --deep --json
   ```

3. Run health and bounded queries. Treat exit status 3 after promotion as
   post-promotion source drift, not success.

   ```sh
   project-knowledge health --repo <repo> --json
   project-knowledge query --repo <repo> <term> --json
   project-knowledge affected --repo <repo> <node-id> --json
   project-knowledge registry-status --json
   ```

Stable JSON error codes are the automation contract; never parse exception
text or absolute paths. Registry synchronization is an explicit opt-in
mutation and is never implied by refresh.

### Low-level recovery

Only for diagnosis/recovery, create a private stage, invoke the official
`$graphify` skill against that stage (never the repository root), then adapt,
validate, and promote the exact candidate:

```sh
project-knowledge stage --repo <repo> --destination <staged-input> --receipt <staging-receipt> --json
project-knowledge adapt --repo <repo> --staged-input <staged-input> --receipt <staging-receipt> --raw-candidate <raw-graph-candidate> --destination <graph-candidate> --json
project-knowledge validate --repo <repo> --candidate <graph-candidate> --json
project-knowledge promote --repo <repo> --candidate <graph-candidate> --json
```

Run health again. Registry synchronization and any Git commit remain separate,
visible mutations.

Refresh only after architecture, module-boundary, schema, public-interface, or substantial documentation changes. Skip it for trivial copy, formatting, comments, and isolated one-line edits.

## Obsidian export

Graphify writes only to a staged atlas candidate. Never point it at the live atlas.

```sh
project-knowledge atlas-prepare --repo <repo> --candidate <atlas-candidate> --json
project-knowledge atlas-promote --repo <repo> --candidate <atlas-candidate> --atlas <atlas> --json
```

The transaction owns only `Projects/<project-id>/Generated/`. It never edits `Notes/` or `.obsidian/`. If any unknown or unmanaged entry exists in `Generated/`, abort the entire promotion. Do not preserve the unknown entry and continue refreshing owned files.

## Stop conditions

Stop without mutation when any of these occurs:

- a project attempts to weaken the non-negotiable global deny set;
- Graphify version/capabilities do not match the pinned contract;
- output validation, provenance, path confinement, ownership, or health fails;
- a structured secret finding is present or an exception attempts to bypass it;
- final graph endpoints are missing/dangling;
- the graph is stale and the user has not approved refresh;
- `Generated/` contains an unknown or unmanaged entry;
- the requested mutation or destination lacks exact approval.

Never install hooks, edit project instructions, alter human notes, expose secrets, publish, send messages, commit, or push unless that exact action is separately in scope.
