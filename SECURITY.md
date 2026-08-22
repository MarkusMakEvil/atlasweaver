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
