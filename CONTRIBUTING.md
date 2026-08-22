# Contributing

1. Open an issue describing the behavior or security boundary being changed.
2. Create a focused branch from `main`.
3. Add a failing regression test before changing behavior.
4. Run `uv run pytest -q`, compileall, and `uv build`.
5. Keep credentials, generated project graphs, vault contents, absolute personal
   paths, and private fixtures out of commits.

Changes to staging, path confinement, subprocesses, promotion transactions,
ownership, redaction, or installation require explicit security-focused tests.
Do not weaken fail-closed behavior to make a candidate pass.
