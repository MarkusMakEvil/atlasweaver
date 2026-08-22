---
name: using-project-knowledge-graphs
description: Use when beginning or continuing substantive repository work involving architecture, dependencies, impact analysis, onboarding, design, or project documentation with Graphify or Obsidian context.
---

# Using Project Knowledge Graphs

Use the project graph as a privacy-safe navigation layer, not as automatic proof. This skill orchestrates the official `$graphify` skill and the deterministic `project-knowledge` CLI; it never reimplements extraction.

## Route by relevance

- For architecture, dependency, impact, onboarding, design, or substantial project documentation, run read-only `project-knowledge detect --repo . --json`, then prefer `GRAPH_REPORT.md` and graph query before broad source traversal.
- For a trivial copy, formatting, comment, or isolated one-line edit, skip graph work.
- Read `references/workflow.md` before bootstrap, refresh, export, promotion, health diagnosis, or when the graph is absent, stale, or partial. It contains the exact command choreography.

Read-only detect, query, and health checks are allowed within the task. Begin any proposed bootstrap or refresh with read-only `project-knowledge preflight`; the reference supplies its full invocation. Bootstrap, staging, refresh, export, atlas promotion, registry changes, hooks, instruction edits, and generated-file commits require exact approval for that mutation and destination. Never commit or push implicitly.

## Evidence and freshness

Label decisive claims as `Graphify-extracted`, `Graphify-inferred`, or `source-verified`. Verify inferred or decisive claims against current source. If health reports stale or source drift, say so; do not present the graph as current fact.

Refresh at checkpoints after architecture, module boundary, schema, public interface, or substantial documentation changes. Do not rebuild for trivial edits.

## Non-negotiable boundaries

Global deny paths are non-negotiable: never stage `.env*`, credentials, keys, tokens, cookies, browser/session data, private configuration, runtime workspaces, private Obsidian `Notes/`, or secrets even if a project manifest or request proposes them.

Generated Obsidian content is confined to the opted-in `Projects/<project-id>/Generated/` namespace. Human `Notes/` and `.obsidian/` are outside the transaction. If `Generated/` contains any unknown or unmanaged entry, abort the whole promotion; preserving that entry while refreshing the rest is still a failure.
