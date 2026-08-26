"""Immutable global privacy exclusions for Graphify inputs."""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from pathlib import Path
import hashlib
import stat

from pathspec import GitIgnoreSpec

from .models import PrivacyDecision, ProjectManifest


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
    "**/*creds*",
    "**/*secret*",
    "**/*token*/**",
    "**/*credential*/**",
    "**/*creds*/**",
    "**/*secret*/**",
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

_SENSITIVE_FRAGMENTS = ("token", "credential", "creds", "secret")
_DATA_SUFFIXES = frozenset({
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".tfvars", ".sqlite", ".sqlite3", ".db", ".kdbx", ".p12", ".pfx",
})
GLOBAL_DENY_RULES: tuple[tuple[str, str], ...] = (
    ("environment_file", ".env"),
    ("environment_file", ".env.*"),
    ("environment_file", "**/.env"),
    ("environment_file", "**/.env.*"),
    ("git_metadata", ".git/**"),
    ("worktree_metadata", ".worktrees/**"),
    ("root_workspace", "workspace/**"),
    ("runtime_state", "**/runtime/**"),
    ("draft_content", "**/drafts/**"),
    ("snapshot_state", "**/snapshots/**"),
    ("cookie_store", "**/cookie/**"),
    ("cookie_store", "**/cookies/**"),
    ("session_store", "**/session/**"),
    ("session_store", "**/sessions/**"),
    ("virtual_environment", "**/.venv/**"),
    ("dependency_tree", "**/node_modules/**"),
    ("build_output", "**/dist/**"),
    ("bytecode_cache", "**/__pycache__/**"),
    ("private_content", "**/private/**"),
    ("identity_control", "**/identity.yaml"),
    ("disclosure_control", "**/disclosure.yaml"),
    ("runtime_control", "**/current-state.yaml"),
    ("voice_source", "**/voice-source/**"),
    ("private_key_material", "**/*.pem"),
    ("private_key_material", "**/*.key"),
    ("private_key_material", "**/*.p12"),
    ("private_key_material", "**/*.pfx"),
    ("credential_store", "**/*.kdbx"),
    ("human_notes", "Projects/*/Notes/**"),
)


def classify_path(
    path: PurePosixPath,
    *,
    project_excludes: Sequence[str],
    sensitive_source_suffixes: frozenset[str],
) -> PrivacyDecision:
    """Return the closed v2 path decision without inspecting denied bytes."""
    from .manifest import ManifestError, validate_project_excludes

    try:
        exact_excludes = validate_project_excludes(tuple(project_excludes))
    except ManifestError:
        raise PrivacyError(
            "negated or invalid project exclude is forbidden"
        ) from None
    if (
        not path.parts
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in path.as_posix()
    ):
        return PrivacyDecision(path, "deny", "path_escape")
    folded = tuple(part.casefold() for part in path.parts)
    name = folded[-1]
    suffix = PurePosixPath(name).suffix.casefold()
    if any(
        fragment in component
        for component in folded[:-1]
        for fragment in _SENSITIVE_FRAGMENTS
    ):
        return PrivacyDecision(path, "deny", "sensitive_directory")
    for rule_id, pattern in GLOBAL_DENY_RULES:
        if _matches(folded, tuple(part.casefold() for part in pattern.split("/"))):
            return PrivacyDecision(path, "deny", rule_id)
    if any(
        _matches(folded, tuple(part.casefold() for part in pattern.split("/")))
        for pattern in exact_excludes
    ):
        return PrivacyDecision(path, "deny", "project_exclude")
    if any(fragment in name for fragment in _SENSITIVE_FRAGMENTS):
        if suffix in sensitive_source_suffixes and suffix not in _DATA_SUFFIXES:
            return PrivacyDecision(path, "scan", "sensitive_source_name")
        return PrivacyDecision(path, "deny", "sensitive_data_name")
    return PrivacyDecision(path, "allow", "ordinary_source")


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
        self._digest_entries: dict[PurePosixPath, str] = {}
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
            payload = path.read_bytes()
            lines = payload.decode("utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise PrivacyError(f"unable to read {name}") from error
        relative = base / name
        self._digest_entries[relative] = hashlib.sha256(
            b"atlasweaver-ignore-v2\0"
            + relative.as_posix().encode("utf-8") + b"\0" + payload
        ).hexdigest()
        return GitIgnoreSpec.from_lines(lines)

    def digests(self) -> tuple[str, ...]:
        """Return path-bound ignore-file digests in confined path order."""
        return tuple(
            self._digest_entries[path] for path in sorted(
                self._digest_entries, key=lambda item: item.as_posix().encode("utf-8")
            )
        )


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
