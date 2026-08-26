# Security Policy

## Supported versions

Security fixes are provided for the latest tagged release and the current
`main` branch.

## Reporting a vulnerability

Please use GitHub's private security-advisory flow for this repository. Do not
open a public issue containing credentials, exploit details, private paths, or
affected project data.

Include the affected version, operating system, minimal reproduction, expected
security boundary, and observed impact. Reports involving staging confinement,
symlink races, secret redaction, artifact promotion, or Obsidian ownership are
treated as security-sensitive.

## Security boundaries

AtlasWeaver must fail closed when it cannot prove that a source, candidate,
generated namespace, or transaction belongs to the selected project. A project
manifest cannot weaken the built-in deny set for credentials, sessions, private
configuration, runtime state, or human Obsidian notes.

Graph bundles are hostile input until their byte-level ZIP structure, closed
entry set, canonical manifest, per-entry digests, project UUID, source digest,
projection digest, evidence, and ownership are validated. Archives with links,
ZIP64, duplicate or ambiguous records, path traversal, compression, oversized
entries, or transport authority inconsistent with the manifest are rejected.
An offline `artifact install` accepts only locally authorized bundles with
`provider: none`; GitHub Release bundles must pass the same-process `pull`
download and attestation boundary.

GitHub transport is restricted to `github.com` and its documented API/release
asset hosts with at most three HTTPS redirects. Authorization is sent only to
`api.github.com` and is stripped before any cross-host redirect. Release tags,
asset names, repository numeric identity, source ref, commit, signer workflow,
and signer digest are all immutable inputs; a matching filename alone conveys
no authority.

Reusable publication separates unprivileged graph construction from the
privileged attestation/upload job. The privileged job does not execute consumer
commands, Graphify, tests, package hooks, or semantic credentials. Every action
and trusted AtlasWeaver checkout is pinned by full commit SHA, and upload occurs
only after root, manifest, clean source, bundle, workflow signer, and attestation
are revalidated.

Agent resources are replaced only inside an AtlasWeaver-owned managed tree.
Pre-existing unmanaged destinations, symlinked homes, marker mismatch, or a
changed resource digest abort installation and preserve caller-owned data.

Fleet manifests are project-agnostic but not ambient authority: repositories
must be real, non-nested descriptor-confined directories with unique UUIDv4
identities. Duplicate paths, symlink aliases, and linked Git worktrees sharing a
common directory are rejected. Operations are isolated per repository; one
failure never rolls back another repository, and registry snapshots are read
atomically under the shared registry lock.
