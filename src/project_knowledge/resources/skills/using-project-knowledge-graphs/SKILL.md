---
name: using-project-knowledge-graphs
description: Use when beginning or continuing substantive repository work involving architecture, dependencies, impact analysis, onboarding, design, or project documentation with Graphify or Obsidian context.
---

# Using Project Knowledge Graphs

Use the project graph as a privacy-safe navigation layer, not as automatic proof. This skill orchestrates the official `$graphify` skill and the deterministic `project-knowledge` CLI; it never reimplements extraction.

## Route by relevance

- For architecture, dependency, impact, onboarding, design, or substantial project documentation, run read-only `project-knowledge detect --repo . --json`, then `project-knowledge doctor --repo . --json` before substantive graph use. Prefer AtlasWeaver's bounded `project-knowledge query`, `path`, `explain`, and `affected` commands before broad source traversal.
- For a trivial copy, formatting, comment, or isolated one-line edit, skip graph work.
- Read `references/workflow.md` before bootstrap, refresh, export, promotion, health diagnosis, or when the graph is absent, stale, or partial. It contains the exact command choreography.

Read-only detect, doctor, query, and health checks are allowed within the task. Begin any proposed bootstrap or refresh with read-only `project-knowledge doctor`; the reference supplies its full invocation. Bootstrap, refresh, export, atlas promotion, registry changes, hooks, instruction edits, and generated-file commits require exact approval for that mutation and destination. Never refresh, register, or publish implicitly. Never commit or push implicitly.

`project-knowledge preflight` remains a read-only low-level recovery diagnostic;
it is not a replacement for the ordinary doctor/refresh lifecycle.
Never point Graphify at the repository root; only AtlasWeaver's private staged
projection may be extracted.

Local bundle pack/install, verified remote pull, agent-resource installation,
fleet refresh/pull/registry synchronization, and workflow publication are
separate explicit mutations. Never infer one from permission to run another.
There is no local publish command; publication is available only through the
reviewed split-privilege reusable workflow.

Use `project-knowledge scan-secrets` before an approved stage. Its findings are
redacted fingerprints. High-confidence structured credentials are
non-bypassable; only an exact reviewed contextual false positive may be
excepted. Never request or print the matching source literal.

## Evidence and freshness

Label decisive claims as `Graphify-extracted`, `Graphify-inferred`, or `source-verified`. Verify inferred or decisive claims against current source. If health reports stale or source drift, say so; do not present the graph as current fact.

If health or a query returns `navigation` trust, use the graph for navigation
only and verify every impact conclusion against current source. Absence of a
path is never proof of no impact. Legacy JSON exposes the same boundary as
`impact_analysis_trusted:false` and `graph_integrity_degraded`.

Refresh at checkpoints after architecture, module boundary, schema, public interface, or substantial documentation changes. Do not rebuild for trivial edits.

## Non-negotiable boundaries

Global deny paths are non-negotiable: never stage `.env*`, credentials, keys, tokens, cookies, browser/session data, private configuration, runtime workspaces, private Obsidian `Notes/`, or secrets even if a project manifest or request proposes them.

Generated Obsidian content is confined to the opted-in `Projects/<project-id>/Generated/` namespace. Human `Notes/` and `.obsidian/` are outside the transaction. If `Generated/` contains any unknown or unmanaged entry, abort the whole promotion; preserving that entry while refreshing the rest is still a failure.
