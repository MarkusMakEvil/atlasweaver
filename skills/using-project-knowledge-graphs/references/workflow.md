# Project knowledge workflow

Read only the section for the active mode. Commands use explicit paths; inspect `--help` if the installed contract differs. Prefer `--json` for machine-readable decisions.

## Detect and query

1. Run `project-knowledge detect --repo <repo> --json`.
2. If opted in and healthy, read `graphify-out/GRAPH_REPORT.md`, then use the official `$graphify` query workflow for relationship/path questions.
3. Open only the source files needed to verify decisive or inferred claims. Report provenance as `Graphify-extracted`, `Graphify-inferred`, or `source-verified`.
4. If missing or stale, disclose that state. Do not silently rebuild.

## Health

Run:

```sh
project-knowledge health --repo <repo> --json
```

Supply `--atlas <atlas>` or `--registry-state ...` only when those local states are in scope. Treat source-digest mismatch as authoritative freshness evidence.

## Bootstrap or refresh

If `project-knowledge` is unavailable, run the reviewed repository onboarding
command once: `python3 scripts/install-project-knowledge-tool`. Then verify
`command -v project-knowledge` before continuing.

Before mutation, show the repository, include roots, immutable global deny set, staged destination, project output, and atlas namespace. Obtain exact approval for the proposed mutation and paths.

1. Run the read-only check:

   ```sh
   project-knowledge preflight --repo <repo> --json
   ```

2. Create a private temporary destination and stage only sanitized input:

   ```sh
   project-knowledge stage --repo <repo> --destination <staged-input> --json
   ```

3. Invoke the official `$graphify` skill on `<staged-input>`, never the unsanitized repository. Direct output to a private candidate. Use only capabilities discovered from the pinned installation and official skill; do not guess shell flags.
4. Validate, then promote the same candidate through the wrapper:

   ```sh
   project-knowledge validate --repo <repo> --candidate <graph-candidate> --json
   project-knowledge promote --repo <repo> --candidate <graph-candidate> --json
   ```

5. Run health again. Registry synchronization and any Git commit remain separate, visible mutations.

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
- the graph is stale and the user has not approved refresh;
- `Generated/` contains an unknown or unmanaged entry;
- the requested mutation or destination lacks exact approval.

Never install hooks, edit project instructions, alter human notes, expose secrets, publish, send messages, commit, or push unless that exact action is separately in scope.
