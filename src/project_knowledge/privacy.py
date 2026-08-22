"""Immutable global privacy exclusions for Graphify inputs."""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from pathlib import Path
import stat

from pathspec import GitIgnoreSpec

from .models import ProjectManifest


class PrivacyError(ValueError):
    """Raised when project exclusions attempt to weaken the global deny set."""


GLOBAL_DENY_PATTERNS = (
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    ".git/**",
    ".worktrees/**",
    "workspace/**",
    "**/runtime/**",
    "**/drafts/**",
    "**/snapshots/**",
    "**/*token*",
    "**/*credential*",
    "**/*secret*",
    "**/cookies/**",
    "**/sessions/**",
    "**/.venv/**",
    "**/node_modules/**",
    "**/dist/**",
    "**/__pycache__/**",
    "**/private/**",
    "**/identity.yaml",
    "**/disclosure.yaml",
    "**/current-state.yaml",
    "**/voice-source/**",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.kdbx",
    "Projects/*/Notes/**",
)


def effective_excludes(manifest: ProjectManifest) -> tuple[str, ...]:
    """Combine immutable global denies with stricter project exclusions."""
    if any(pattern.startswith("!") for pattern in manifest.excludes):
        raise PrivacyError("negated include patterns are not allowed")
    return GLOBAL_DENY_PATTERNS + manifest.excludes


def is_denied(path: PurePosixPath, excludes: Sequence[str]) -> bool:
    """Return whether a repository-relative path matches an exclusion pattern.

    Matching is component-aware: a single ``*`` does not cross directory
    boundaries, while ``**`` matches zero or more complete path components.
    Matching is case-insensitive so sensitive file names cannot bypass denies
    through capitalization.
    """
    if path.is_absolute() or ".." in path.parts:
        return True
    if any(pattern.startswith("!") for pattern in excludes):
        raise PrivacyError("negated include patterns are not allowed")
    return any(_matches(path.parts, tuple(pattern.split("/"))) for pattern in excludes)


class RepositoryIgnores:
    """Evaluate repository ignore files with Git's ordered pattern semantics."""

    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self._git_specs: dict[PurePosixPath, GitIgnoreSpec | None] = {}
        self._graphify = self._load(PurePosixPath(), ".graphifyignore")

    def __call__(self, path: PurePosixPath, *, is_directory: bool = False) -> bool:
        candidate = path.as_posix() + ("/" if is_directory else "")
        ignored = False
        bases = (PurePosixPath(), *reversed(path.parents[:-1]))
        for base in bases:
            spec = self._git_specs.get(base)
            if base not in self._git_specs:
                spec = self._load(base, ".gitignore")
                self._git_specs[base] = spec
            if spec is None:
                continue
            relative = PurePosixPath(candidate).relative_to(base).as_posix()
            result = spec.check_file(relative)
            if result.include is not None:
                ignored = bool(result.include)
        if ignored:
            return True
        if self._graphify is None:
            return False
        return self._graphify.match_file(candidate)

    def _load(self, base: PurePosixPath, name: str) -> GitIgnoreSpec | None:
        path = self.root.joinpath(*base.parts, name)
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise PrivacyError(f"{name} must be a regular file")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise PrivacyError(f"unable to read {name}") from error
        return GitIgnoreSpec.from_lines(lines)


def repository_ignores(repo_root: Path) -> RepositoryIgnores:
    """Load `.gitignore` and `.graphifyignore` without invoking Git."""
    return RepositoryIgnores(repo_root)


def _matches(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not parts
    head, *tail = pattern
    if head == "**":
        return any(_matches(parts[index:], tuple(tail)) for index in range(len(parts) + 1))
    if not parts:
        return False
    return fnmatchcase(parts[0].casefold(), head.casefold()) and _matches(
        parts[1:], tuple(tail)
    )
