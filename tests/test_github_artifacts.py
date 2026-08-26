from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
from uuid import UUID
import zipfile

import pytest

import project_knowledge.github_artifacts as github_artifacts

from project_knowledge.bundles import (
    ArtifactManifest,
    GitIdentity,
    GithubTransport,
    PayloadDescriptor,
    _consume_pull_authorization,
    active_pull_authorization_count,
    registered_pull_authorization,
)
from project_knowledge.github_artifacts import (
    AttestationPolicy,
    DownloadReceipt,
    GithubArtifactError,
    GithubCredentials,
    ReleaseAssetIdentity,
    ResolvedGhExecutable,
    VerifiedAttestation,
    resolve_and_download,
    verify_attestation_policy,
)
from project_knowledge.models import ArtifactIntent, ProjectionSnapshot
from tests.support import manifest_v2


PROJECT_UID = UUID("4ed9af24-5aa2-4eac-8d0a-3f622cc74948")
SOURCE = "1" * 64
PROJECTION = "2" * 64
GENERATION = "3" * 64
COMMIT = "b" * 40


def _config() -> ArtifactIntent:
    return ArtifactIntent(
        provider="github-release",
        host="github.com",
        repository="acme/widgets",
        repository_id=123456789,
        channel="main",
        source_ref="refs/heads/main",
        signer_workflow="acme/atlasweaver/.github/workflows/atlasweaver-publish.yml",
        signer_digest="c" * 40,
    )


def _projection() -> ProjectionSnapshot:
    return ProjectionSnapshot(
        source_digest=SOURCE,
        projection_digest=PROJECTION,
        files=(),
        decisions=(),
        reason_counts=(),
        ignore_digests=(),
        secret_exception_digest=None,
        coverage_digest=None,
    )


def _bundle_bytes() -> bytes:
    payloads = {
        "graphify-out/GRAPH_EVIDENCE.json": b"{}\n",
        "graphify-out/GRAPH_REPORT.md": b"# report\n",
        "graphify-out/graph.json": b'{"nodes":[],"links":[]}\n',
    }
    manifest = ArtifactManifest(
        schema_version=1,
        atlasweaver_version="0.2.2",
        project_id="demo",
        project_uid=str(PROJECT_UID),
        graphify_version="0.9.48",
        adapter_id="graphify-0.9.48",
        source_digest=SOURCE,
        projection_digest=PROJECTION,
        graph_digest=hashlib.sha256(payloads["graphify-out/graph.json"]).hexdigest(),
        generation_digest=GENERATION,
        git=GitIdentity(COMMIT, "sha1"),
        build_epoch=1_777_777_777,
        transport=GithubTransport(
            "github-release", "main", "github.com", "acme/widgets",
            123456789, "refs/heads/main",
        ),
        payloads=tuple(
            PayloadDescriptor(PurePosixPath(name), hashlib.sha256(data).hexdigest(), len(data))
            for name, data in payloads.items()
        ),
    )
    import io

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name, data in {"artifact.json": manifest.to_bytes(), **payloads}.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, data)
    return output.getvalue()


class _Response:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.closed = False

    def read(self, size: int) -> bytes:
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def close(self) -> None:
        self.closed = True


class _Https:
    def __init__(self) -> None:
        self.queued: dict[str, list[_Response]] = {}
        self.requests = []

    def add_json(self, url: str, value: object) -> None:
        self.add(url, 200, json.dumps(value).encode(), {"Content-Length": str(len(json.dumps(value).encode()))})

    def add(self, url: str, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.queued.setdefault(url, []).append(_Response(status, body, headers))

    def open(self, request):
        self.requests.append(request)
        return self.queued[request.url].pop(0)


def _seed(transport: _Https, *, redirect: str | None = None) -> bytes:
    bundle = _bundle_bytes()
    digest = hashlib.sha256(bundle).hexdigest()
    tag = f"atlasweaver-graph-{PROJECT_UID}-main"
    name = f"atlasweaver-graph-{PROJECT_UID}-{SOURCE}-{PROJECTION}-{digest}.zip"
    transport.add_json("https://api.github.com/repos/acme/widgets", {"id": 123456789, "full_name": "acme/widgets"})
    transport.add_json(
        f"https://api.github.com/repos/acme/widgets/releases/tags/{tag}",
        {
            "id": 44,
            "tag_name": tag,
            "assets": [{
                "id": 55, "name": name, "size": len(bundle),
                "digest": "sha256:" + digest, "created_at": "2026-08-26T00:00:00Z",
            }],
        },
    )
    asset_url = "https://api.github.com/repos/acme/widgets/releases/assets/55"
    if redirect is None:
        transport.add(asset_url, 200, bundle, {"Content-Length": str(len(bundle))})
    else:
        transport.add(asset_url, 302, headers={"Location": redirect})
        if redirect.startswith("https://release-assets.githubusercontent.com/"):
            transport.add(redirect, 200, bundle, {"Content-Length": str(len(bundle))})
    transport.add_json(
        "https://api.github.com/repos/acme/widgets/commits/" + COMMIT,
        {"sha": COMMIT},
    )
    return bundle


@pytest.mark.parametrize("token", ["", " token", "token ", "a\nb", "a\rb", "a\0b", "x" * 4097, b"bad", True])
def test_credentials_fail_before_transport(token, tmp_path: Path) -> None:
    transport = _Https()
    with pytest.raises(GithubArtifactError) as raised:
        resolve_and_download(
            _config(), PROJECT_UID, _projection(), tmp_path / "bundle.zip",
            GithubCredentials(token), transport,
        )
    assert raised.value.code == "github_token_required"
    assert transport.requests == []


def test_download_binds_all_immutable_identities_and_redirect_headers(tmp_path: Path) -> None:
    transport = _Https()
    redirected = "https://release-assets.githubusercontent.com/object/bundle.zip"
    bundle = _seed(transport, redirect=redirected)
    checks: list[int] = []
    destination = tmp_path / "private" / "bundle.zip"

    receipt = resolve_and_download(
        _config(), PROJECT_UID, _projection(), destination,
        GithubCredentials("token-value"), transport,
        before_request=lambda: checks.append(len(transport.requests)),
    )

    digest = hashlib.sha256(bundle).hexdigest()
    assert receipt.archive_sha256 == digest
    assert receipt.archive_size == len(bundle)
    assert receipt.artifact_git_commit_oid == COMMIT
    assert receipt.identity.generation_digest == GENERATION
    assert len(checks) == len(transport.requests) == 5
    assert destination.read_bytes() == bundle
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert "Authorization" not in dict(transport.requests[-2].headers)
    assert all(response.closed for responses in transport.queued.values() for response in responses) is True


def test_unsafe_redirect_removes_partial_file(tmp_path: Path) -> None:
    transport = _Https()
    _seed(transport, redirect="https://evil.example/object")
    destination = tmp_path / "bundle.zip"
    with pytest.raises(GithubArtifactError) as raised:
        resolve_and_download(
            _config(), PROJECT_UID, _projection(), destination,
            GithubCredentials("token-value"), transport,
        )
    assert raised.value.code == "github_redirect_invalid"
    assert not destination.exists()


def test_existing_destination_is_never_removed_and_manifest_callback_propagates(tmp_path: Path) -> None:
    transport = _Https()
    _seed(transport)
    destination = tmp_path / "bundle.zip"
    destination.write_bytes(b"caller-owned")
    with pytest.raises(GithubArtifactError) as raised:
        resolve_and_download(
            _config(), PROJECT_UID, _projection(), destination,
            GithubCredentials("token-value"), transport,
        )
    assert raised.value.code == "github_destination_invalid"
    assert destination.read_bytes() == b"caller-owned"

    class Drift(RuntimeError):
        pass

    transport = _Https()
    _seed(transport)
    with pytest.raises(Drift):
        resolve_and_download(
            _config(), PROJECT_UID, _projection(), tmp_path / "other.zip",
            GithubCredentials("token-value"), transport,
            before_request=lambda: (_ for _ in ()).throw(Drift()),
        )
    assert transport.requests == []


@dataclass
class _Completed:
    returncode: int
    stdout: str
    stderr: str = ""


class _Runner:
    def __init__(self, result: _Completed) -> None:
        self.result = result
        self.calls = []

    def run(self, argv, env, timeout_seconds, output_limit):
        config = Path(env["GH_CONFIG_DIR"])
        assert config.is_dir()
        assert stat.S_IMODE(config.stat().st_mode) == 0o700
        assert list(config.iterdir()) == []
        sandbox = config.parent
        assert Path(env["HOME"]).parent == sandbox
        assert Path(env["XDG_CONFIG_HOME"]).parent == sandbox
        assert Path(env["XDG_CACHE_HOME"]).parent == sandbox
        state = Path(env["XDG_STATE_HOME"])
        assert state.parent == sandbox
        state.mkdir()
        (state / "device-id").write_text("isolated", encoding="utf-8")
        self.calls.append((argv, dict(env), timeout_seconds, output_limit, config))
        return self.result


class _ResolverRunner:
    def run(self, argv, env, timeout_seconds, output_limit):
        if argv[-1] == "--version":
            return _Completed(0, "gh version 2.96.0 (test)\n")
        return _Completed(
            0,
            " ".join((
                "--hostname", "--repo", "--signer-workflow", "--signer-digest",
                "--source-ref", "--source-digest", "--predicate-type",
                "--deny-self-hosted-runners", "--format",
            )),
        )


def test_system_gh_resolver_accepts_safe_package_manager_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "cellar" / "gh"
    target.parent.mkdir()
    target.write_bytes(b"#!/bin/sh\nexit 0\n")
    target.chmod(0o700)
    candidate = tmp_path / "bin" / "gh"
    candidate.parent.mkdir()
    candidate.symlink_to(target)
    resolver = github_artifacts._SystemGhResolver()
    monkeypatch.setattr(resolver, "_candidates", (candidate,))
    monkeypatch.setattr(github_artifacts, "SUBPROCESS_GH_RUNNER", _ResolverRunner())

    resolved = resolver.resolve()

    assert resolved.path == target.resolve()


def _resolved_gh(tmp_path: Path) -> ResolvedGhExecutable:
    path = tmp_path / "gh"
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    info = path.stat()
    return ResolvedGhExecutable(
        path, info.st_dev, info.st_ino,
        hashlib.sha256(path.read_bytes()).hexdigest(), "2.80.0",
    )


def _gh_result(subject: str) -> dict[str, object]:
    return {
        "attestation": {
            "repository": "acme/widgets",
            "signerWorkflow": "acme/atlasweaver/.github/workflows/atlasweaver-publish.yml",
            "signerDigest": "c" * 40,
            "sourceRef": "refs/heads/main",
            "sourceDigest": COMMIT,
            "runnerEnvironment": "github-hosted",
        },
        "verificationResult": {
            "statement": {
                "predicateType": "https://slsa.dev/provenance/v1",
                "subject": [{"name": "bundle.zip", "digest": {"sha256": subject}}],
            }
        },
    }


def test_attestation_uses_exact_argv_minimal_environment_and_cleans(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"verified bytes")
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    runner = _Runner(_Completed(0, json.dumps([_gh_result(digest)])))
    policy = AttestationPolicy(
        "acme/widgets",
        "acme/atlasweaver/.github/workflows/atlasweaver-publish.yml",
        "c" * 40,
        "refs/heads/main",
        COMMIT,
        "https://slsa.dev/provenance/v1",
    )
    gh = _resolved_gh(tmp_path)

    verified = verify_attestation_policy(
        bundle, policy, GithubCredentials("secret"), gh, runner,
    )

    argv, env, timeout, limit, config_dir = runner.calls[0]
    assert argv == (
        str(gh.path), "attestation", "verify", str(bundle),
        "--hostname", "github.com", "--repo", "acme/widgets",
        "--signer-workflow", policy.signer_workflow,
        "--signer-digest", policy.signer_digest,
        "--source-ref", policy.source_ref, "--source-digest", policy.source_digest,
        "--predicate-type", policy.predicate_type,
        "--deny-self-hosted-runners", "--format", "json",
    )
    assert set(env) == {
        "GH_CONFIG_DIR", "GH_TOKEN", "HOME", "LANG", "LC_ALL",
        "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
    }
    assert timeout == 60.0 and limit == 256 * 1024
    assert not config_dir.exists()
    assert verified.subject_sha256 == digest


def test_attestation_deduplicates_identical_results_and_rejects_duplicate_json(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"verified bytes")
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    policy = AttestationPolicy(
        "acme/widgets", "acme/atlasweaver/.github/workflows/atlasweaver-publish.yml",
        "c" * 40, "refs/heads/main", COMMIT, "https://slsa.dev/provenance/v1",
    )
    gh = _resolved_gh(tmp_path)
    result = _gh_result(digest)
    assert verify_attestation_policy(
        bundle, policy, GithubCredentials("secret"), gh,
        _Runner(_Completed(0, json.dumps([result, result]))),
    ).subject_sha256 == digest

    with pytest.raises(GithubArtifactError) as raised:
        verify_attestation_policy(
            bundle, policy, GithubCredentials("secret"), gh,
            _Runner(_Completed(0, '[{"attestation":{},"attestation":{}}]')),
        )
    assert raised.value.code == "attestation_invalid"


def test_attestation_accepts_official_certificate_projection(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"verified bytes")
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    result = _gh_result(digest)
    result["attestation"] = {"bundle": {"mediaType": "application/json"}}
    result["verificationResult"]["signature"] = {
        "certificate": {"extensions": {
            "sourceRepositoryURI": "https://github.com/acme/widgets",
            "buildSignerURI": (
                "https://github.com/acme/atlasweaver/.github/workflows/"
                "atlasweaver-publish.yml@refs/heads/main"
            ),
            "buildSignerDigest": "c" * 40,
            "sourceRepositoryRef": "refs/heads/main",
            "sourceRepositoryDigest": COMMIT,
            "runnerEnvironment": "github-hosted",
        }}
    }
    policy = AttestationPolicy(
        "acme/widgets", "acme/atlasweaver/.github/workflows/atlasweaver-publish.yml",
        "c" * 40, "refs/heads/main", COMMIT, "https://slsa.dev/provenance/v1",
    )
    assert verify_attestation_policy(
        bundle, policy, GithubCredentials("secret"), _resolved_gh(tmp_path),
        _Runner(_Completed(0, json.dumps([result]))),
    ).subject_sha256 == digest


def test_pull_authorization_is_exact_single_use_and_bound_to_bytes(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.zip"
    payload = _bundle_bytes()
    bundle.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    name = f"atlasweaver-graph-{PROJECT_UID}-{SOURCE}-{PROJECTION}-{digest}.zip"
    identity = ReleaseAssetIdentity(
        123456789, 44, f"atlasweaver-graph-{PROJECT_UID}-main", 55,
        name, len(payload), digest, GENERATION, COMMIT,
    )
    receipt = DownloadReceipt(identity, digest, len(payload), COMMIT)
    verified = VerifiedAttestation(
        digest, "acme/atlasweaver/.github/workflows/atlasweaver-publish.yml",
        "c" * 40, "refs/heads/main", COMMIT,
        "https://slsa.dev/provenance/v1",
    )
    manifest = manifest_v2(artifacts=_config())
    with registered_pull_authorization(bundle, receipt, verified, manifest) as authorization:
        assert active_pull_authorization_count() == 1
        _consume_pull_authorization(bundle, authorization)
        with pytest.raises(Exception, match="bundle_unattested"):
            _consume_pull_authorization(bundle, authorization)
    assert active_pull_authorization_count() == 0
