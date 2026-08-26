"""Redacted, named secret-shape detection for staged project sources."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

import yaml

from .models import ProjectManifest
from .privacy import GLOBAL_DENY_PATTERNS, is_denied


_STRUCTURED_RULES = (
    (
        "telegram_token",
        re.compile(rb"(?<![0-9])[0-9]{8,10}:[A-Za-z0-9_-]{30,}"),
    ),
    (
        "private_key",
        re.compile(
            rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s*[\r\n]+[A-Za-z0-9+/=]{32,}"
        ),
    ),
    ("aws_access_key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("google_api_key", re.compile(rb"\bAIza[A-Za-z0-9_-]{35}\b")),
    ("stripe_live_key", re.compile(rb"\b[rs]k_live_[A-Za-z0-9]{16,}\b")),
    ("slack_token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    (
        "jwt",
        re.compile(
            rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
        ),
    ),
    (
        "bearer_token",
        re.compile(
            rb"(?i)(?:\bauthorization\b\s*[:=]\s*[\"']?)?"
            rb"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"
        ),
    ),
    (
        "credentialed_url",
        re.compile(rb"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]{12,}@[^\s/]+"),
    ),
)
_ASSIGNMENT = re.compile(
    rb"(?i)\b(?:password|passwd|client_secret|aws_secret_access_key|"
    rb"access_token|api_key)\b\s*[:=]\s*(?P<value>[^\r\n]+)"
)
_SECRET_COMPONENT = re.compile(
    rb"(?:^|[._:/-])(?:sk|secret|token|bearer|api[-_]?key|credential|password|passwd)"
    rb"(?:$|[._:/-])",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(rb"\$\{[A-Za-z_][A-Za-z0-9_]*\}\Z")
_ENV_REFERENCE = re.compile(
    rb"(?:"
    rb"(?:process\.env|import\.meta\.env)\.[A-Za-z_][A-Za-z0-9_]*|"
    rb"os\.environ\[\s*['\"][A-Za-z_][A-Za-z0-9_]*['\"]\s*\]|"
    rb"(?:os\.)?getenv\(\s*['\"][A-Za-z_][A-Za-z0-9_]*['\"]\s*\)"
    rb")\Z"
)
_CONFIG_EXTENSIONS = frozenset(
    {".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".txt"}
)
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EXCEPTION_NAME = ".graphify-secret-exceptions.yaml"


class SecretExceptionError(ValueError):
    """Raised when a repository secret exception weakens the scanner contract."""


@dataclass(frozen=True)
class SecretFinding:
    """A path-confined diagnostic that never retains the matched bytes."""

    detector: str
    path: PurePosixPath
    line: int
    fingerprint: str
    bypassable: bool


def has_secret_shape(payload: bytes) -> bool:
    """Return whether bytes have a credential shape unsafe for persistence."""
    if type(payload) is not bytes:
        return True
    return (
        any(pattern.search(payload) is not None for _, pattern in _STRUCTURED_RULES)
        or _ASSIGNMENT.search(payload) is not None
        or _SECRET_COMPONENT.search(payload) is not None
    )


def scan_payload(path: PurePosixPath, payload: bytes) -> tuple[SecretFinding, ...]:
    """Return redacted findings for one safe source payload."""
    findings: list[SecretFinding] = []
    structured_spans: list[tuple[int, int]] = []
    for detector, pattern in _STRUCTURED_RULES:
        for match in pattern.finditer(payload):
            structured_spans.append(match.span())
            findings.append(_finding(detector, path, payload, match.start(), match.group(), False))

    for match in _ASSIGNMENT.finditer(payload):
        start, end = match.span("value")
        if any(start < structured_end and end > structured_start for structured_start, structured_end in structured_spans):
            continue
        literal = _literal_assignment(path, match.group("value"))
        if literal is None:
            continue
        findings.append(
            _finding(
                "generic_secret_assignment",
                path,
                payload,
                match.start(),
                match.group(),
                True,
            )
        )
    return tuple(
        sorted(
            findings,
            key=lambda item: (item.line, item.detector, item.fingerprint),
        )
    )


def scan_repository(
    repo_root: Path, manifest: ProjectManifest
) -> tuple[SecretFinding, ...]:
    """Scan exactly the source files eligible for staging."""
    from .staging import iter_safe_files

    root = repo_root.absolute()
    root_fd = _open_directory(root)
    try:
        findings: list[SecretFinding] = []
        for relative in sorted(set(iter_safe_files(root, manifest))):
            findings.extend(scan_payload(relative, _read_regular(root_fd, relative)))
        return tuple(findings)
    finally:
        os.close(root_fd)


def load_secret_exceptions(
    repo_root: Path, findings: tuple[SecretFinding, ...]
) -> frozenset[str]:
    """Load exact reviewed exceptions for contextual findings only."""
    try:
        payload = _read_optional_regular(repo_root.absolute(), _EXCEPTION_NAME)
    except OSError as error:
        raise SecretExceptionError("secret exception file is invalid") from error
    if payload is None:
        return frozenset()
    try:
        document = yaml.load(payload.decode("utf-8"), Loader=_UniqueLoader)
    except SecretExceptionError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise SecretExceptionError("secret exception file is invalid") from error
    if not isinstance(document, dict) or set(document) != {"schema_version", "exceptions"}:
        raise SecretExceptionError("secret exception schema is invalid")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise SecretExceptionError("secret exception schema is invalid")
    entries = document["exceptions"]
    if not isinstance(entries, list):
        raise SecretExceptionError("secret exception entries are invalid")

    by_identity = {
        (item.path, item.detector, item.fingerprint): item for item in findings
    }
    accepted: set[str] = set()
    seen: set[tuple[PurePosixPath, str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "detector",
            "fingerprint",
            "reason",
        }:
            raise SecretExceptionError("secret exception entry is invalid")
        relative = _confined_path(entry["path"])
        detector = entry["detector"]
        fingerprint = entry["fingerprint"]
        reason = entry["reason"]
        if (
            not isinstance(detector, str)
            or not isinstance(fingerprint, str)
            or not _FINGERPRINT.fullmatch(fingerprint)
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise SecretExceptionError("secret exception entry is invalid")
        identity = (relative, detector, fingerprint)
        if identity in seen:
            raise SecretExceptionError("duplicate secret exception entry")
        seen.add(identity)
        finding = by_identity.get(identity)
        if finding is None:
            raise SecretExceptionError("secret exception does not match a current finding")
        if detector != "generic_secret_assignment" or not finding.bypassable:
            raise SecretExceptionError("structured secret findings cannot be excepted")
        if is_denied(relative, GLOBAL_DENY_PATTERNS):
            raise SecretExceptionError("denied source paths cannot be excepted")
        accepted.add(fingerprint)
    return frozenset(accepted)


def _finding(
    detector: str,
    path: PurePosixPath,
    payload: bytes,
    start: int,
    matched: bytes,
    bypassable: bool,
) -> SecretFinding:
    digest = hashlib.sha256()
    digest.update(detector.encode("ascii") + b"\0")
    digest.update(path.as_posix().encode("utf-8") + b"\0")
    digest.update(str(start).encode("ascii") + b"\0")
    digest.update(matched)
    return SecretFinding(
        detector=detector,
        path=path,
        line=payload.count(b"\n", 0, start) + 1,
        fingerprint="sha256:" + digest.hexdigest(),
        bypassable=bypassable,
    )


def _literal_assignment(path: PurePosixPath, raw: bytes) -> bytes | None:
    value = raw.strip().rstrip(b",;").strip()
    if not value:
        return None
    if value[:1] in {b'"', b"'"}:
        quote = value[:1]
        closing = value.find(quote, 1)
        if closing < 0:
            return None
        literal = value[1:closing]
        if _PLACEHOLDER.fullmatch(literal) or len(literal) < 16:
            return None
        return value
    if _PLACEHOLDER.fullmatch(value):
        return None
    scalar = value.split(b" #", 1)[0].strip()
    if _ENV_REFERENCE.fullmatch(scalar):
        return None
    if path.suffix.casefold() not in _CONFIG_EXTENSIONS:
        return None
    if len(scalar) < 16 or any(character in scalar for character in b" \t()[]{}"):
        return None
    return scalar


def _confined_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SecretExceptionError("secret exception path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SecretExceptionError("secret exception path is invalid")
    return path


class _UniqueLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueLoader, node: yaml.MappingNode, deep: bool = False):
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise SecretExceptionError("duplicate key in secret exception file")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _read_regular(root_fd: int, relative: PurePosixPath) -> bytes:
    parent_fd = os.dup(root_fd)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, _directory_flags(), dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child
        before = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(
            relative.name, os.O_RDONLY | _nofollow_flag(), dir_fd=parent_fd
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
                raise OSError("source changed during secret scan")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
            if _identity(opened) != _identity(after):
                raise OSError("source changed during secret scan")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _read_optional_regular(root: Path, name: str) -> bytes | None:
    root_fd = _open_directory(root)
    try:
        try:
            before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode):
            raise SecretExceptionError("secret exception file must be a regular file")
        descriptor = os.open(name, os.O_RDONLY | _nofollow_flag(), dir_fd=root_fd)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
                raise SecretExceptionError("secret exception file changed during read")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if _identity(opened) != _identity(after):
                raise SecretExceptionError("secret exception file changed during read")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(root_fd)


def _open_directory(path: Path) -> int:
    return os.open(path, _directory_flags())


def _directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    except AttributeError as error:
        raise OSError("platform lacks safe directory operations") from error


def _nofollow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as error:
        raise OSError("platform lacks safe file operations") from error


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev
