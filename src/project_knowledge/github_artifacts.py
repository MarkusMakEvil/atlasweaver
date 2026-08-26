"""Bounded GitHub Release transport and provenance verification."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import selectors
import secrets
import signal
import ssl
import stat
import subprocess
import tempfile
import time
from typing import Any, Literal, Protocol
from urllib.parse import quote, urljoin, urlsplit
from uuid import UUID

from .bundles import ArtifactManifest, GithubTransport, V1_LIMITS, inspect_bundle_manifest
from .models import ArtifactIntent, ProjectionSnapshot
from .locking import RepositoryIdentity, open_repository_access
from .manifest import (
    ManifestError,
    assert_current_manifest_unchanged,
    inspect_init_journal,
    load_manifest,
    require_current_manifest,
)
from .models import ProjectManifest
from .staging import inspect_projection

if False:  # pragma: no cover - typing-only circular import
    from .bundles import InstallResult


ALLOWED_GITHUB_HOSTS = frozenset({
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})
MAX_REDIRECTS = 3
CONNECT_TIMEOUT_SECONDS = 15.0
READ_CHUNK_BYTES = 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
GH_TIMEOUT_SECONDS = 60.0
GH_OUTPUT_LIMIT = 256 * 1024
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_CHANNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
_RECOVERY = re.compile(r"[0-9a-f]{16,128}\Z")
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_PREDICATE = "https://slsa.dev/provenance/v1"


def _noop() -> None:
    pass


class GithubArtifactError(RuntimeError):
    """Stable, redacted GitHub artifact failure."""

    def __init__(
        self, code: str, message: str | None = None, recovery_id: str | None = None
    ) -> None:
        if type(code) is not str or not (
            code.startswith("github_")
            or code.startswith("attestation_")
            or code in {"bundle_source_drift", "manifest_migration_required", "init_recovery_required"}
        ):
            raise ValueError("GitHub artifact error code is invalid")
        if recovery_id is not None and (
            code != "github_cleanup_failed" or _RECOVERY.fullmatch(recovery_id) is None
        ):
            raise ValueError("GitHub recovery ID is invalid")
        if code == "github_cleanup_failed" and recovery_id is None:
            raise ValueError("GitHub cleanup failure requires recovery ID")
        self.code = code
        self.recovery_id = recovery_id
        super().__init__(message or code)


@dataclass(frozen=True)
class GithubCredentials:
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        valid = type(self.token) is str
        try:
            encoded = self.token.encode("utf-8") if valid else b""
        except UnicodeEncodeError:
            valid = False
            encoded = b""
        if (
            not valid
            or not (1 <= len(encoded) <= 4096)
            or self.token != self.token.strip()
            or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in self.token)
        ):
            raise GithubArtifactError(
                "github_token_required", "GitHub credential is required"
            )


@dataclass(frozen=True)
class HttpsRequest:
    method: Literal["GET"]
    url: str
    headers: tuple[tuple[str, str], ...]
    timeout_seconds: float

    def __post_init__(self) -> None:
        if self.method != "GET" or self.timeout_seconds != CONNECT_TIMEOUT_SECONDS:
            raise ValueError("invalid HTTPS request")


class HttpsResponse(Protocol):
    status: int
    headers: Any

    def read(self, size: int) -> bytes: ...
    def close(self) -> None: ...


class HttpsTransport(Protocol):
    def open(self, request: HttpsRequest) -> HttpsResponse: ...


class _HttpsClientTransport:
    def open(self, request: HttpsRequest) -> HttpsResponse:
        split = _validated_url(request.url)
        connection = http.client.HTTPSConnection(
            split.hostname,
            port=443,
            timeout=request.timeout_seconds,
            context=ssl.create_default_context(),
        )
        try:
            target = split.path + (("?" + split.query) if split.query else "")
            connection.request(request.method, target, headers=dict(request.headers))
            response = connection.getresponse()
        except BaseException:
            connection.close()
            raise
        return _ConnectionResponse(connection, response)


class _ConnectionResponse:
    def __init__(self, connection: http.client.HTTPSConnection, response: http.client.HTTPResponse):
        self._connection = connection
        self._response = response
        self.status = response.status
        self.headers = response.headers

    def read(self, size: int) -> bytes:
        return self._response.read(size)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


REAL_HTTPS: HttpsTransport = _HttpsClientTransport()


@dataclass(frozen=True)
class ReleaseAssetIdentity:
    repository_id: int
    release_id: int
    tag: str
    asset_id: int
    asset_name: str
    asset_size: int
    asset_digest: str
    generation_digest: str
    source_commit_oid: str

    def __post_init__(self) -> None:
        if (
            type(self.repository_id) is not int or self.repository_id <= 0
            or type(self.release_id) is not int or self.release_id <= 0
            or type(self.asset_id) is not int or self.asset_id <= 0
            or type(self.asset_size) is not int or self.asset_size < 0
            or type(self.tag) is not str or not self.tag
            or type(self.asset_name) is not str
            or not self.asset_name.endswith("-" + self.asset_digest + ".zip")
            or _HEX64.fullmatch(self.asset_digest) is None
            or _HEX64.fullmatch(self.generation_digest) is None
            or _HEX40.fullmatch(self.source_commit_oid) is None
        ):
            raise GithubArtifactError("github_receipt_invalid")


@dataclass(frozen=True)
class DownloadReceipt:
    identity: ReleaseAssetIdentity
    archive_sha256: str
    archive_size: int
    artifact_git_commit_oid: str

    def __post_init__(self) -> None:
        if (
            type(self.identity) is not ReleaseAssetIdentity
            or self.archive_sha256 != self.identity.asset_digest
            or self.archive_size != self.identity.asset_size
            or self.artifact_git_commit_oid != self.identity.source_commit_oid
        ):
            raise GithubArtifactError("github_receipt_invalid")


def resolve_and_download(
    config: ArtifactIntent,
    project_uid: UUID,
    projection: ProjectionSnapshot,
    destination: Path,
    credentials: GithubCredentials,
    transport: HttpsTransport = REAL_HTTPS,
    *,
    before_request: Callable[[], None] = _noop,
) -> DownloadReceipt:
    """Resolve one immutable digest-named release asset and download it safely."""
    _require_credentials(credentials)
    repository, repository_id, channel, source_ref = _require_github_config(config)
    if type(project_uid) is not UUID or project_uid.version != 4:
        raise GithubArtifactError("github_config_invalid")
    if (
        type(projection) is not ProjectionSnapshot
        or _HEX64.fullmatch(projection.source_digest) is None
        or type(projection.projection_digest) is not str
        or _HEX64.fullmatch(projection.projection_digest) is None
    ):
        raise GithubArtifactError("github_projection_invalid")
    if not isinstance(destination, Path):
        raise GithubArtifactError("github_destination_invalid")

    tag = f"atlasweaver-graph-{project_uid}-{channel}"
    encoded_repository = "/".join(quote(part, safe="") for part in repository.split("/"))
    repository_url = f"https://api.github.com/repos/{encoded_repository}"
    release_url = repository_url + "/releases/tags/" + quote(tag, safe="")
    downloaded = False
    try:
        repository_document = _get_json(
            repository_url, credentials, transport, before_request
        )
        if (
            _positive_int(repository_document.get("id")) != repository_id
            or _exact_string(repository_document.get("full_name")) != repository
        ):
            raise GithubArtifactError("github_repository_mismatch")

        release = _get_json(release_url, credentials, transport, before_request)
        release_id = _positive_int(release.get("id"))
        if _exact_string(release.get("tag_name")) != tag:
            raise GithubArtifactError("github_release_mismatch")
        asset = _select_asset(
            release.get("assets"), project_uid, projection.source_digest,
            projection.projection_digest,
        )
        asset_id, asset_name, asset_size, asset_digest, _created = asset
        asset_url = repository_url + f"/releases/assets/{asset_id}"
        archive_sha256, archive_size = _download_asset(
            asset_url,
            destination,
            credentials,
            transport,
            before_request,
            expected_size=asset_size,
            expected_digest=asset_digest,
        )
        downloaded = True
        manifest = inspect_bundle_manifest(destination)
        _require_downloaded_manifest(
            manifest,
            config,
            project_uid,
            projection,
            repository_id,
        )
        assert manifest.git is not None
        commit_oid = manifest.git.commit_oid
        commit = _get_json(
            repository_url + "/commits/" + quote(commit_oid, safe=""),
            credentials,
            transport,
            before_request,
        )
        if _exact_string(commit.get("sha")) != commit_oid:
            raise GithubArtifactError("github_source_commit_mismatch")
        identity = ReleaseAssetIdentity(
            repository_id=repository_id,
            release_id=release_id,
            tag=tag,
            asset_id=asset_id,
            asset_name=asset_name,
            asset_size=asset_size,
            asset_digest=asset_digest,
            generation_digest=manifest.generation_digest,
            source_commit_oid=commit_oid,
        )
        return DownloadReceipt(identity, archive_sha256, archive_size, commit_oid)
    except GithubArtifactError:
        if downloaded:
            _unlink_destination(destination)
        raise
    except BaseException:
        if downloaded:
            _unlink_destination(destination)
        raise


def _get_json(
    url: str,
    credentials: GithubCredentials,
    transport: HttpsTransport,
    before_request: Callable[[], None],
) -> dict[str, object]:
    response, _ = _open_following_redirects(
        url, credentials, transport, before_request, asset=False
    )
    try:
        if response.status != 200:
            raise GithubArtifactError("github_api_failed")
        payload = _read_capped(response, MAX_JSON_BYTES, "github_json_oversized")
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
        if type(value) is not dict:
            raise ValueError
        return value
    except GithubArtifactError:
        raise
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise GithubArtifactError("github_json_invalid") from None
    finally:
        response.close()


def _select_asset(
    raw_assets: object,
    project_uid: UUID,
    source_digest: str,
    projection_digest: str,
) -> tuple[int, str, int, str, datetime]:
    if type(raw_assets) is not list:
        raise GithubArtifactError("github_release_invalid")
    prefix = f"atlasweaver-graph-{project_uid}-{source_digest}-{projection_digest}-"
    pattern = re.compile(re.escape(prefix) + r"([0-9a-f]{64})\.zip\Z")
    candidates: list[tuple[int, str, int, str, datetime]] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for raw in raw_assets:
        if type(raw) is not dict:
            raise GithubArtifactError("github_release_invalid")
        name = _exact_string(raw.get("name"))
        if not name.startswith(prefix):
            continue
        match = pattern.fullmatch(name)
        if match is None:
            raise GithubArtifactError("github_asset_ambiguous")
        asset_id = _positive_int(raw.get("id"))
        size = _nonnegative_int(raw.get("size"))
        digest_value = _exact_string(raw.get("digest"))
        if not digest_value.startswith("sha256:") or _HEX64.fullmatch(digest_value[7:]) is None:
            raise GithubArtifactError("github_asset_digest_invalid")
        digest = digest_value[7:]
        if digest != match.group(1):
            raise GithubArtifactError("github_asset_digest_mismatch")
        created = _parse_timestamp(raw.get("created_at"))
        if asset_id in seen_ids or name in seen_names:
            raise GithubArtifactError("github_asset_ambiguous")
        seen_ids.add(asset_id)
        seen_names.add(name)
        candidates.append((asset_id, name, size, digest, created))
    if not candidates:
        raise GithubArtifactError("github_asset_not_found")
    candidates.sort(key=lambda item: (item[4], item[0]), reverse=True)
    return candidates[0]


def _download_asset(
    url: str,
    destination: Path,
    credentials: GithubCredentials,
    transport: HttpsTransport,
    before_request: Callable[[], None],
    *,
    expected_size: int,
    expected_digest: str,
) -> tuple[str, int]:
    response, _ = _open_following_redirects(
        url, credentials, transport, before_request, asset=True
    )
    descriptor = -1
    created = False
    try:
        if response.status != 200:
            raise GithubArtifactError("github_download_failed")
        content_length = _content_length(response.headers)
        if content_length is None:
            raise GithubArtifactError("github_content_length_required")
        if content_length != expected_size or content_length > V1_LIMITS.total_bytes:
            raise GithubArtifactError("github_asset_size_mismatch")
        _prepare_private_parent(destination.parent)
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        created = True
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = response.read(READ_CHUNK_BYTES)
            if type(chunk) is not bytes:
                raise GithubArtifactError("github_download_failed")
            if not chunk:
                break
            total += len(chunk)
            if total > content_length or total > V1_LIMITS.total_bytes:
                raise GithubArtifactError("github_asset_size_mismatch")
            digest.update(chunk)
            _write_all(descriptor, chunk)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        actual_digest = digest.hexdigest()
        if total != content_length or total != expected_size:
            raise GithubArtifactError("github_asset_size_mismatch")
        if actual_digest != expected_digest:
            raise GithubArtifactError("github_asset_digest_mismatch")
        return actual_digest, total
    except GithubArtifactError:
        if created:
            _unlink_destination(destination)
        raise
    except OSError:
        if created:
            _unlink_destination(destination)
        raise GithubArtifactError("github_destination_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        response.close()


def _open_following_redirects(
    url: str,
    credentials: GithubCredentials,
    transport: HttpsTransport,
    before_request: Callable[[], None],
    *,
    asset: bool,
) -> tuple[HttpsResponse, str]:
    current = url
    for redirects in range(MAX_REDIRECTS + 1):
        split = _validated_url(current)
        before_request()
        try:
            headers = _headers(split.hostname or "", credentials, asset=asset)
            response = transport.open(
                HttpsRequest("GET", current, headers, CONNECT_TIMEOUT_SECONDS)
            )
        except GithubArtifactError:
            raise
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise GithubArtifactError("github_transport_failed") from None
        if response.status not in _REDIRECT_CODES:
            return response, current
        try:
            location = _header(response.headers, "Location")
        finally:
            response.close()
        if redirects == MAX_REDIRECTS or location is None:
            raise GithubArtifactError("github_redirect_invalid")
        if location.startswith("//"):
            raise GithubArtifactError("github_redirect_invalid")
        candidate = urljoin(current, location)
        _validated_url(candidate)
        current = candidate
    raise GithubArtifactError("github_redirect_invalid")


def _validated_url(url: str):
    try:
        split = urlsplit(url)
        host = split.hostname
        if (
            split.scheme != "https"
            or host not in ALLOWED_GITHUB_HOSTS
            or split.username is not None
            or split.password is not None
            or split.fragment
            or split.port not in {None, 443}
            or not split.path.startswith("/")
        ):
            raise ValueError
        return split
    except (TypeError, ValueError):
        raise GithubArtifactError("github_redirect_invalid") from None


def _headers(
    host: str, credentials: GithubCredentials, *, asset: bool
) -> tuple[tuple[str, str], ...]:
    _require_credentials(credentials)
    headers = [
        ("Accept", "application/octet-stream" if asset else "application/vnd.github+json"),
        ("User-Agent", "atlasweaver-artifact-client"),
        ("X-GitHub-Api-Version", "2022-11-28"),
    ]
    if host == "api.github.com":
        headers.append(("Authorization", f"Bearer {credentials.token}"))
    return tuple(headers)


def _require_downloaded_manifest(
    manifest: ArtifactManifest,
    config: ArtifactIntent,
    project_uid: UUID,
    projection: ProjectionSnapshot,
    repository_id: int,
) -> None:
    transport = manifest.transport
    if (
        manifest.project_uid != str(project_uid)
        or manifest.source_digest != projection.source_digest
        or manifest.projection_digest != projection.projection_digest
        or manifest.git is None
        or not isinstance(transport, GithubTransport)
        or transport.host != config.host
        or transport.repository != config.repository
        or transport.repository_id != repository_id
        or transport.channel != config.channel
        or transport.source_ref != config.source_ref
    ):
        raise GithubArtifactError("github_bundle_identity_mismatch")


def _require_github_config(config: ArtifactIntent) -> tuple[str, int, str, str]:
    if (
        type(config) is not ArtifactIntent
        or config.provider != "github-release"
        or config.host != "github.com"
        or type(config.repository) is not str
        or _REPOSITORY.fullmatch(config.repository) is None
        or type(config.repository_id) is not int
        or isinstance(config.repository_id, bool)
        or config.repository_id <= 0
        or type(config.channel) is not str
        or _CHANNEL.fullmatch(config.channel) is None
        or type(config.source_ref) is not str
        or not config.source_ref
    ):
        raise GithubArtifactError("github_config_invalid")
    return config.repository, config.repository_id, config.channel, config.source_ref


def _require_credentials(credentials: GithubCredentials) -> None:
    if type(credentials) is not GithubCredentials:
        raise GithubArtifactError("github_token_required", "GitHub credential is required")
    credentials.__post_init__()


def _prepare_private_parent(parent: Path) -> None:
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise OSError
        os.chmod(parent, 0o700)
    except OSError:
        raise GithubArtifactError("github_destination_invalid") from None


def _unlink_destination(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _header(headers: Any, name: str) -> str | None:
    if hasattr(headers, "get"):
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        return value if type(value) is str else None
    return None


def _content_length(headers: Any) -> int | None:
    raw = _header(headers, "Content-Length")
    if raw is None or re.fullmatch(r"0|[1-9][0-9]*", raw) is None:
        return None
    value = int(raw)
    return value


def _read_capped(response: HttpsResponse, limit: int, code: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(65_536, limit + 1 - total))
        if type(chunk) is not bytes:
            raise GithubArtifactError(code)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise GithubArtifactError(code)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError
        remaining = remaining[written:]


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise GithubArtifactError("github_json_invalid")
    return value


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise GithubArtifactError("github_json_invalid")
    return value


def _exact_string(value: object) -> str:
    if type(value) is not str:
        raise GithubArtifactError("github_json_invalid")
    return value


def _parse_timestamp(value: object) -> datetime:
    raw = _exact_string(value)
    try:
        if not raw.endswith("Z"):
            raise ValueError
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except ValueError:
        raise GithubArtifactError("github_json_invalid") from None


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite JSON")


def _finite_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON")
    return value


# -- GitHub CLI provenance boundary ---------------------------------------


@dataclass(frozen=True)
class CompletedCommand:
    returncode: int
    stdout: str
    stderr: str


class GhCommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout_seconds: float,
        output_limit: int,
    ) -> CompletedCommand: ...


class _GhRunnerError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _SubprocessGhRunner:
    def run(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout_seconds: float,
        output_limit: int,
    ) -> CompletedCommand:
        if not argv or timeout_seconds <= 0 or output_limit <= 0:
            raise _GhRunnerError("attestation_execution_failed")
        options: dict[str, object] = {}
        if os.name == "posix":
            options["start_new_session"] = True
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env),
                shell=False,
                **options,
            )
        except OSError:
            raise _GhRunnerError("attestation_execution_failed") from None
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        output = {process.stdout: bytearray(), process.stderr: bytearray()}
        total = 0
        deadline = time.monotonic() + timeout_seconds
        try:
            for stream in output:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _GhRunnerError("attestation_timeout")
                events = selector.select(min(remaining, 0.25))
                for key, _ in events:
                    stream = key.fileobj
                    chunk = os.read(stream.fileno(), min(65_536, output_limit + 1 - total))
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    output[stream].extend(chunk)
                    total += len(chunk)
                    if total > output_limit:
                        raise _GhRunnerError("attestation_output_oversized")
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            return CompletedCommand(
                returncode,
                bytes(output[process.stdout]).decode("utf-8", "replace"),
                bytes(output[process.stderr]).decode("utf-8", "replace"),
            )
        except subprocess.TimeoutExpired:
            raise _GhRunnerError("attestation_timeout") from None
        finally:
            selector.close()
            if process.poll() is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            process.stdout.close()
            process.stderr.close()


SUBPROCESS_GH_RUNNER: GhCommandRunner = _SubprocessGhRunner()


@dataclass(frozen=True)
class VerifiedAttestation:
    subject_sha256: str
    signer_workflow: str
    signer_digest: str
    source_ref: str
    source_digest: str
    predicate_type: str


@dataclass(frozen=True)
class ResolvedGhExecutable:
    path: Path
    device: int
    inode: int
    sha256: str
    version: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, Path)
            or not self.path.is_absolute()
            or type(self.device) is not int
            or type(self.inode) is not int
            or self.device < 0
            or self.inode <= 0
            or _HEX64.fullmatch(self.sha256) is None
            or type(self.version) is not str
            or not self.version
        ):
            raise GithubArtifactError("github_gh_unavailable")


class GhToolResolver(Protocol):
    def resolve(
        self, *, before_exec: Callable[[], None] = _noop
    ) -> ResolvedGhExecutable: ...


class _SystemGhResolver:
    _candidates = (Path("/usr/bin/gh"), Path("/opt/homebrew/bin/gh"))

    def resolve(
        self, *, before_exec: Callable[[], None] = _noop
    ) -> ResolvedGhExecutable:
        for path in self._candidates:
            try:
                resolved = _capture_gh(path, version="pending")
            except GithubArtifactError:
                continue
            environment = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
            before_exec()
            _revalidate_gh(resolved)
            version_result = SUBPROCESS_GH_RUNNER.run(
                (str(path), "--version"), environment, GH_TIMEOUT_SECONDS, GH_OUTPUT_LIMIT
            )
            if version_result.returncode != 0:
                continue
            match = re.search(r"(?m)^gh version ([0-9]+(?:\.[0-9]+){1,3})\b", version_result.stdout)
            if match is None:
                continue
            before_exec()
            _revalidate_gh(resolved)
            help_result = SUBPROCESS_GH_RUNNER.run(
                (str(path), "attestation", "verify", "--help"),
                environment,
                GH_TIMEOUT_SECONDS,
                GH_OUTPUT_LIMIT,
            )
            required = (
                "--hostname", "--repo", "--signer-workflow", "--signer-digest",
                "--source-ref", "--source-digest", "--predicate-type",
                "--deny-self-hosted-runners", "--format",
            )
            if help_result.returncode != 0 or any(flag not in help_result.stdout for flag in required):
                continue
            return ResolvedGhExecutable(
                resolved.path, resolved.device, resolved.inode, resolved.sha256, match.group(1)
            )
        raise GithubArtifactError("github_gh_unavailable")


SYSTEM_GH_RESOLVER: GhToolResolver = _SystemGhResolver()


def resolve_gh_executable(
    resolver: GhToolResolver = SYSTEM_GH_RESOLVER,
) -> ResolvedGhExecutable:
    return resolver.resolve()


@dataclass(frozen=True)
class AttestationPolicy:
    repository: str
    signer_workflow: str
    signer_digest: str
    source_ref: str
    source_digest: str
    predicate_type: str
    deny_self_hosted_runners: bool = True

    def __post_init__(self) -> None:
        if (
            type(self.repository) is not str
            or _REPOSITORY.fullmatch(self.repository) is None
            or type(self.signer_workflow) is not str
            or not self.signer_workflow
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in self.signer_workflow)
            or _HEX40.fullmatch(self.signer_digest) is None
            or type(self.source_ref) is not str
            or not self.source_ref.startswith("refs/")
            or _HEX40.fullmatch(self.source_digest) is None
            or self.predicate_type != _PREDICATE
            or type(self.deny_self_hosted_runners) is not bool
            or not self.deny_self_hosted_runners
        ):
            raise GithubArtifactError("attestation_policy_invalid")


def verify_attestation_policy(
    bundle: Path,
    policy: AttestationPolicy,
    credentials: GithubCredentials,
    gh: ResolvedGhExecutable,
    runner: GhCommandRunner = SUBPROCESS_GH_RUNNER,
    *,
    before_exec: Callable[[], None] = _noop,
) -> VerifiedAttestation:
    _require_credentials(credentials)
    if type(policy) is not AttestationPolicy or type(gh) is not ResolvedGhExecutable:
        raise GithubArtifactError("attestation_policy_invalid")
    policy.__post_init__()
    subject = _hash_regular_file(bundle)
    config_root = Path(tempfile.mkdtemp(prefix="atlasweaver-gh-"))
    config_root.chmod(0o700)
    argv = (
        str(gh.path), "attestation", "verify", str(bundle),
        "--hostname", "github.com", "--repo", policy.repository,
        "--signer-workflow", policy.signer_workflow,
        "--signer-digest", policy.signer_digest,
        "--source-ref", policy.source_ref,
        "--source-digest", policy.source_digest,
        "--predicate-type", policy.predicate_type,
        "--deny-self-hosted-runners", "--format", "json",
    )
    environment = {
        "GH_CONFIG_DIR": str(config_root),
        "GH_TOKEN": credentials.token,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    try:
        before_exec()
        _revalidate_gh(gh)
        try:
            result = runner.run(argv, environment, GH_TIMEOUT_SECONDS, GH_OUTPUT_LIMIT)
        except _GhRunnerError as error:
            raise GithubArtifactError(error.code) from None
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise GithubArtifactError("attestation_execution_failed") from None
        if (
            type(result.returncode) is not int
            or type(result.stdout) is not str
            or type(result.stderr) is not str
        ):
            raise GithubArtifactError("attestation_execution_failed")
        if len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")) > GH_OUTPUT_LIMIT:
            raise GithubArtifactError("attestation_output_oversized")
        if result.returncode != 0:
            raise GithubArtifactError("attestation_verification_failed")
        projected = _parse_attestation_results(result.stdout, subject, policy)
        if len(projected) != 1:
            raise GithubArtifactError("attestation_ambiguous")
        return next(iter(projected))
    finally:
        try:
            for child in config_root.iterdir():
                if child.is_dir() and not child.is_symlink():
                    import shutil
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
            config_root.rmdir()
        except OSError:
            pass


def verify_attestation(
    bundle: Path,
    receipt: DownloadReceipt,
    config: ArtifactIntent,
    credentials: GithubCredentials,
    gh: ResolvedGhExecutable,
    runner: GhCommandRunner = SUBPROCESS_GH_RUNNER,
    *,
    before_exec: Callable[[], None] = _noop,
) -> VerifiedAttestation:
    _require_credentials(credentials)
    repository, repository_id, _channel, source_ref = _require_github_config(config)
    if type(receipt) is not DownloadReceipt:
        raise GithubArtifactError("attestation_receipt_invalid")
    identity = receipt.identity
    if (
        type(identity) is not ReleaseAssetIdentity
        or identity.repository_id != repository_id
        or receipt.archive_sha256 != identity.asset_digest
        or receipt.archive_size != identity.asset_size
        or receipt.artifact_git_commit_oid != identity.source_commit_oid
    ):
        raise GithubArtifactError("attestation_receipt_invalid")
    manifest = inspect_bundle_manifest(bundle)
    expected_tag = f"atlasweaver-graph-{manifest.project_uid}-{config.channel}"
    expected_name = (
        f"atlasweaver-graph-{manifest.project_uid}-{manifest.source_digest}-"
        f"{manifest.projection_digest}-{receipt.archive_sha256}.zip"
    )
    if (
        manifest.generation_digest != identity.generation_digest
        or manifest.git is None
        or manifest.git.commit_oid != identity.source_commit_oid
        or not isinstance(manifest.transport, GithubTransport)
        or manifest.transport.repository != repository
        or manifest.transport.repository_id != repository_id
        or manifest.transport.host != config.host
        or manifest.transport.channel != config.channel
        or manifest.transport.source_ref != source_ref
        or identity.tag != expected_tag
        or identity.asset_name != expected_name
    ):
        raise GithubArtifactError("attestation_receipt_invalid")
    policy = AttestationPolicy(
        repository=repository,
        signer_workflow=_required_config_string(config.signer_workflow),
        signer_digest=_required_config_string(config.signer_digest),
        source_ref=source_ref,
        source_digest=identity.source_commit_oid,
        predicate_type=_PREDICATE,
    )
    verified = verify_attestation_policy(
        bundle, policy, credentials, gh, runner, before_exec=before_exec
    )
    if verified.subject_sha256 != receipt.archive_sha256:
        raise GithubArtifactError("attestation_subject_mismatch")
    return verified


def _parse_attestation_results(
    raw: str, subject: str, policy: AttestationPolicy
) -> set[VerifiedAttestation]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        raise GithubArtifactError("attestation_invalid") from None
    if type(value) is not list or not value:
        raise GithubArtifactError("attestation_invalid")
    projections: set[tuple[str, str, str, str, str, str, str, str]] = set()
    try:
        for raw_result in value:
            if type(raw_result) is not dict:
                raise ValueError
            attestation = raw_result.get("attestation")
            verification = raw_result.get("verificationResult")
            if type(attestation) is not dict or type(verification) is not dict:
                raise ValueError
            statement = verification.get("statement")
            if type(statement) is not dict:
                raise ValueError
            subjects = statement.get("subject")
            if type(subjects) is not list or not subjects:
                raise ValueError
            subject_digests = set()
            for item in subjects:
                if type(item) is not dict or type(item.get("digest")) is not dict:
                    raise ValueError
                digest = item["digest"].get("sha256")
                if type(digest) is not str or _HEX64.fullmatch(digest) is None:
                    raise ValueError
                subject_digests.add(digest)
            signature = verification.get("signature")
            certificate: Mapping[str, object] = {}
            if type(signature) is dict and type(signature.get("certificate")) is dict:
                certificate = signature["certificate"]
                if type(certificate.get("extensions")) is dict:
                    certificate = certificate["extensions"]
            authority = _merged_authority(attestation, certificate)
            repository = _authority_string(
                authority, "repository", "sourceRepository", "sourceRepositoryURI"
            )
            if repository.startswith("https://github.com/"):
                repository = repository.removeprefix("https://github.com/")
            workflow = _authority_string(authority, "signerWorkflow", "buildSignerURI")
            if workflow.startswith("https://github.com/"):
                workflow = workflow.removeprefix("https://github.com/").split("@", 1)[0]
            signer_digest = _authority_string(authority, "signerDigest", "buildSignerDigest")
            source_ref = _authority_string(authority, "sourceRef", "sourceRepositoryRef")
            source_digest = _authority_string(authority, "sourceDigest", "sourceRepositoryDigest")
            runner_environment = _authority_string(authority, "runnerEnvironment")
            predicate_type = _authority_string(statement, "predicateType")
            projections.add((
                (
                    subject
                    if subject in subject_digests
                    else next(iter(subject_digests))
                    if len(subject_digests) == 1
                    else ""
                ),
                repository, workflow, signer_digest, source_ref, source_digest,
                predicate_type, runner_environment,
            ))
    except GithubArtifactError:
        raise
    except (KeyError, TypeError, ValueError):
        raise GithubArtifactError("attestation_invalid") from None
    if len(projections) != 1:
        raise GithubArtifactError("attestation_ambiguous")
    (
        projected_subject, repository, workflow, signer_digest, source_ref,
        source_digest, predicate_type, runner_environment,
    ) = next(iter(projections))
    if projected_subject != subject:
        raise GithubArtifactError("attestation_subject_mismatch")
    if repository != policy.repository:
        raise GithubArtifactError("attestation_repository_mismatch")
    if workflow != policy.signer_workflow:
        raise GithubArtifactError("attestation_workflow_mismatch")
    if signer_digest != policy.signer_digest:
        raise GithubArtifactError("attestation_signer_mismatch")
    if source_ref != policy.source_ref:
        raise GithubArtifactError("attestation_source_ref_mismatch")
    if source_digest != policy.source_digest:
        raise GithubArtifactError("attestation_source_digest_mismatch")
    if predicate_type != policy.predicate_type:
        raise GithubArtifactError("attestation_predicate_mismatch")
    if policy.deny_self_hosted_runners and runner_environment not in {
        "github-hosted", "github-hosted-runner", "platform-hosted"
    }:
        raise GithubArtifactError("attestation_runner_invalid")
    return {
        VerifiedAttestation(
            projected_subject, workflow, signer_digest, source_ref,
            source_digest, predicate_type,
        )
    }


def _merged_authority(
    primary: Mapping[str, object], secondary: Mapping[str, object]
) -> dict[str, object]:
    result = dict(primary)
    for key, value in secondary.items():
        if key in result and result[key] != value:
            raise ValueError
        result[key] = value
    return result


def _authority_string(value: Mapping[str, object], *names: str) -> str:
    candidates = {value[name] for name in names if name in value}
    if len(candidates) != 1:
        raise ValueError
    result = candidates.pop()
    if type(result) is not str or not result:
        raise ValueError
    return result


def _hash_regular_file(path: Path) -> str:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > V1_LIMITS.total_bytes:
            raise OSError
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise OSError
        return digest.hexdigest()
    except OSError:
        raise GithubArtifactError("attestation_subject_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _capture_gh(path: Path, *, version: str) -> ResolvedGhExecutable:
    descriptor = -1
    try:
        info = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.getuid()}
            or info.st_mode & 0o022
            or not info.st_mode & 0o111
        ):
            raise OSError
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise OSError
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        final = os.fstat(descriptor)
        if (
            (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise OSError
        return ResolvedGhExecutable(path, info.st_dev, info.st_ino, digest.hexdigest(), version)
    except (OSError, GithubArtifactError):
        raise GithubArtifactError("github_gh_unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _revalidate_gh(gh: ResolvedGhExecutable) -> None:
    current = _capture_gh(gh.path, version=gh.version)
    if (
        current.device != gh.device
        or current.inode != gh.inode
        or current.sha256 != gh.sha256
    ):
        raise GithubArtifactError("github_gh_changed")


def _required_config_string(value: object) -> str:
    if type(value) is not str or not value:
        raise GithubArtifactError("attestation_policy_invalid")
    return value


__all__ = [
    "ALLOWED_GITHUB_HOSTS", "AttestationPolicy", "CONNECT_TIMEOUT_SECONDS",
    "CompletedCommand", "DownloadReceipt", "GH_OUTPUT_LIMIT",
    "GH_TIMEOUT_SECONDS", "GhCommandRunner", "GhToolResolver",
    "GithubArtifactError", "GithubCredentials", "HttpsRequest",
    "HttpsResponse", "HttpsTransport", "MAX_REDIRECTS", "PullDependencies",
    "PullResult", "REAL_PULL_DEPENDENCIES",
    "READ_CHUNK_BYTES", "REAL_HTTPS", "ReleaseAssetIdentity",
    "ResolvedGhExecutable", "SUBPROCESS_GH_RUNNER", "V1_LIMITS",
    "VerifiedAttestation", "resolve_and_download", "resolve_gh_executable",
    "pull_bundle", "verify_attestation", "verify_attestation_policy",
]


@dataclass(frozen=True)
class PullDependencies:
    transport: HttpsTransport
    runner: GhCommandRunner
    gh_resolver: GhToolResolver


REAL_PULL_DEPENDENCIES = PullDependencies(
    REAL_HTTPS, SUBPROCESS_GH_RUNNER, SYSTEM_GH_RESOLVER
)


@dataclass(frozen=True)
class PullResult:
    download: DownloadReceipt
    install: "InstallResult"


def pull_bundle(
    repo_root: Path,
    credentials: GithubCredentials,
    *,
    dependencies: PullDependencies = REAL_PULL_DEPENDENCIES,
    expected_repository_identity: RepositoryIdentity | None = None,
    expected_manifest: ProjectManifest | None = None,
) -> PullResult:
    """Authenticate and install one GitHub bundle under immutable authority."""
    from .bundles import (
        OperationTempCleanupError,
        _install_verified_pull_execution,
        managed_operation_temp_root,
        registered_pull_authorization,
    )

    _require_credentials(credentials)
    started = time.monotonic()
    projection: ProjectionSnapshot | None = None
    manifest: ProjectManifest | None = None
    admitted_identity: RepositoryIdentity | None = None
    result: PullResult | None = None
    receipt: DownloadReceipt | None = None
    execution: Any = None
    recovery_id = secrets.token_hex(16)
    try:
        with open_repository_access(
            repo_root,
            expected_repository_identity=expected_repository_identity,
        ) as repository:
            admitted_identity = repository.identity
            if inspect_init_journal(repo_root, repository_access=repository) != "none":
                raise GithubArtifactError(
                    "init_recovery_required", "configuration recovery is required"
                )
            if expected_manifest is None:
                manifest = load_manifest(
                    repo_root / ".graphify-project.yaml",
                    repo_root,
                    repository_access=repository,
                )
                manifest = require_current_manifest(
                    repo_root, manifest, repository_access=repository
                )
            else:
                manifest = require_current_manifest(
                    repo_root, expected_manifest, repository_access=repository
                )
            config = manifest.artifacts
            _require_github_config(config)
            projection = inspect_projection(
                repo_root, manifest, repository_access=repository
            )
            if manifest.project_uid is None or projection.projection_digest is None:
                raise GithubArtifactError(
                    "manifest_migration_required", "portable identity is required"
                )
            try:
                with managed_operation_temp_root("pull", recovery_id) as temporary:
                    bundle = temporary.path / "bundle.zip"
                    assert_current_manifest_unchanged(
                        repo_root, manifest, repository_access=repository
                    )
                    receipt = resolve_and_download(
                        config,
                        manifest.project_uid,
                        projection,
                        bundle,
                        credentials,
                        dependencies.transport,
                        before_request=lambda: assert_current_manifest_unchanged(
                            repo_root, manifest, repository_access=repository
                        ),
                    )
                    assert_current_manifest_unchanged(
                        repo_root, manifest, repository_access=repository
                    )
                    gh = dependencies.gh_resolver.resolve(
                        before_exec=lambda: assert_current_manifest_unchanged(
                            repo_root, manifest, repository_access=repository
                        )
                    )
                    assert_current_manifest_unchanged(
                        repo_root, manifest, repository_access=repository
                    )
                    verified = verify_attestation(
                        bundle,
                        receipt,
                        config,
                        credentials,
                        gh,
                        dependencies.runner,
                        before_exec=lambda: assert_current_manifest_unchanged(
                            repo_root, manifest, repository_access=repository
                        ),
                    )
                    assert_current_manifest_unchanged(
                        repo_root, manifest, repository_access=repository
                    )
                    with registered_pull_authorization(
                        bundle, receipt, verified, manifest
                    ) as authorization:
                        _pull_handoff_checkpoint()
                        assert_current_manifest_unchanged(
                            repo_root, manifest, repository_access=repository
                        )
                        execution = _install_verified_pull_execution(
                            repo_root,
                            bundle,
                            authorization,
                            expected_repository_identity=admitted_identity,
                            expected_manifest=manifest,
                            recovery_id=recovery_id,
                        )
                        _after_pull_install_checkpoint()
                        installed = execution.result
                        try:
                            assert_current_manifest_unchanged(
                                repo_root, manifest, repository_access=repository
                            )
                        except ManifestError:
                            installed = replace(
                                installed, status="promoted_but_stale"
                            )
                        result = PullResult(receipt, installed)
                if temporary.cleanup_failed:
                    if execution is None or not execution.promotion_committed:
                        raise GithubArtifactError(
                            "github_cleanup_failed",
                            "private GitHub cleanup failed",
                            recovery_id,
                        )
                    assert receipt is not None
                    result = _postcommit_pull_result(
                        receipt, execution, recovery_id=recovery_id
                    )
            except OperationTempCleanupError:
                if (
                    receipt is not None
                    and execution is not None
                    and execution.promotion_committed
                ):
                    result = _postcommit_pull_result(
                        receipt, execution, recovery_id=recovery_id
                    )
                else:
                    raise GithubArtifactError(
                        "github_cleanup_failed",
                        "private GitHub cleanup failed",
                        recovery_id,
                    ) from None
            except Exception:
                if (
                    receipt is not None
                    and execution is not None
                    and execution.promotion_committed
                ):
                    result = _postcommit_pull_result(receipt, execution)
                else:
                    raise
    except BaseException:
        raise
    assert result is not None
    assert manifest is not None and projection is not None and admitted_identity is not None
    try:
        _record_pull_outcome(
            repo_root,
            admitted_identity,
            manifest,
            projection,
            result,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
    except Exception:
        if execution is not None and execution.promotion_committed:
            return _postcommit_pull_result(result.download, execution)
        raise
    return result


def _postcommit_pull_result(
    receipt: DownloadReceipt,
    execution: Any,
    recovery_id: str | None = None,
) -> PullResult:
    installed = execution.result
    return PullResult(
        receipt,
        replace(
            installed,
            status="promoted_but_stale",
            recovery_id=(installed.recovery_id if recovery_id is None else recovery_id),
        ),
    )


def _pull_handoff_checkpoint() -> None:
    pass


def _after_pull_install_checkpoint() -> None:
    pass


def _record_pull_outcome(
    repo_root: Path,
    repository_identity: RepositoryIdentity,
    manifest: ProjectManifest,
    projection: ProjectionSnapshot,
    result: PullResult,
    *,
    duration_ms: int,
) -> None:
    from .operation_state import (
        CoverageMetrics,
        FailureRecord,
        SuccessfulOperation,
        record_failure,
        record_success,
    )

    install = result.install
    if install.graph_digest is not None:
        denied = sum(
            count for key, count in projection.reason_counts if key.startswith("deny:")
        )
        record_success(
            repo_root,
            SuccessfulOperation(
                operation="artifact_pull",
                duration_ms=duration_ms,
                safe_file_count=len(projection.files),
                coverage=CoverageMetrics(
                    represented=max(
                        0, len(projection.files) - len(projection.coverage_approvals)
                    ),
                    approved_omissions=len(projection.coverage_approvals),
                    denied=denied,
                ),
                source_digest=projection.source_digest,
                projection_digest=projection.projection_digest or "",
                graph_digest=install.graph_digest,
                generation_digest=install.generation_digest,
                build_epoch=install.build_epoch,
                artifact_channel=manifest.artifacts.channel,
            ),
            expected_repository_identity=repository_identity,
        )
    if install.status == "promoted_but_stale":
        record_failure(
            repo_root,
            FailureRecord("artifact_pull", "bundle_source_drift"),
            expected_repository_identity=repository_identity,
        )
