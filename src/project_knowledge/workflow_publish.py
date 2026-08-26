"""Internal, descriptor-authorized publication boundary for reusable workflows."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import ssl
import stat
import subprocess
import sys
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlencode, urlsplit

from .artifacts import GitIdentity as ArtifactGitIdentity, validate_candidate
from .bundles import (
    BundleError,
    GithubTransport,
    PackedBundle,
    _PackCommitState,
    _capture_validated_generation,
    _pack_captured_generation,
    managed_operation_temp_root,
    OperationTempCleanupError,
    parse_bundle,
)
from .compatibility import resolve_graphify_compatibility
from .evidence import GRAPH_EVIDENCE_MAX_BYTES, parse_graph_evidence
from .github_artifacts import (
    AttestationPolicy,
    GithubCredentials,
    resolve_gh_executable,
    verify_attestation_policy,
)
from .locking import (
    RepositoryIdentity,
    capture_lifecycle_repository,
    repository_lifecycle_lock,
)
from .manifest import (
    assert_current_manifest_unchanged,
    inspect_init_journal,
    load_manifest,
    render_manifest_v2,
    require_current_manifest,
)
from .models import ProjectManifest
from .staging import StagedInput, stage_input
from .workflow_boundary import (
    WorkflowBoundaryError,
    WorkflowGitScope,
    open_workflow_repository,
)


_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_RECOVERY = re.compile(r"[0-9a-f]{16,128}\Z")
_ASSET_CREATED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_PREDICATE = "https://slsa.dev/provenance/v1"
_HANDOFF_KEYS = frozenset({
    "schema_version", "repository_identity", "manifest_sha256", "context",
    "bundle_sha256", "bundle_size", "bundle_identity",
})
_CONTEXT_KEYS = frozenset({
    "repository", "repository_id", "ref", "ref_type", "ref_protected",
    "sha", "run_id",
})
_IDENTITY_KEYS = frozenset({"device", "inode"})


def _noop() -> None:
    pass


class WorkflowPublishError(RuntimeError):
    """Stable, content-free workflow publication failure."""

    def __init__(
        self, code: str, message: str | None = None, recovery_id: str | None = None
    ) -> None:
        if type(code) is not str or not (
            code.startswith("workflow_")
            or code in {"init_recovery_required", "remote_asset_mismatch"}
        ):
            raise ValueError("workflow publication code is invalid")
        allowed = code in {
            "workflow_cleanup_failed", "workflow_output_recovery_required"
        }
        if (recovery_id is not None) != allowed or (
            recovery_id is not None and _RECOVERY.fullmatch(recovery_id) is None
        ):
            raise ValueError("workflow publication recovery ID is invalid")
        self.code = code
        self.recovery_id = recovery_id
        super().__init__(message or code)


@dataclass(frozen=True)
class WorkflowContext:
    repository: str
    repository_id: int
    ref: str
    ref_type: str
    ref_protected: bool
    sha: str
    run_id: int

    def __post_init__(self) -> None:
        if (
            type(self.repository) is not str
            or _REPOSITORY.fullmatch(self.repository) is None
            or type(self.repository_id) is not int
            or self.repository_id <= 0
            or type(self.ref) is not str
            or not self.ref.startswith("refs/heads/")
            or self.ref_type != "branch"
            or self.ref_protected is not True
            or type(self.sha) is not str
            or _HEX40.fullmatch(self.sha) is None
            or type(self.run_id) is not int
            or self.run_id <= 0
        ):
            raise WorkflowPublishError("workflow_source_mismatch")

    @classmethod
    def from_environment(cls) -> "WorkflowContext":
        try:
            if os.environ.get("GITHUB_ACTIONS") != "true":
                raise ValueError
            protected = os.environ["GITHUB_REF_PROTECTED"]
            if protected not in {"true", "false"}:
                raise ValueError
            return cls(
                os.environ["GITHUB_REPOSITORY"],
                int(os.environ["GITHUB_REPOSITORY_ID"]),
                os.environ["GITHUB_REF"],
                os.environ["GITHUB_REF_TYPE"],
                protected == "true",
                os.environ["GITHUB_SHA"],
                int(os.environ["GITHUB_RUN_ID"]),
            )
        except (KeyError, ValueError, TypeError, UnicodeError):
            raise WorkflowPublishError("workflow_source_mismatch") from None

    def to_dict(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "repository_id": self.repository_id,
            "ref": self.ref,
            "ref_type": self.ref_type,
            "ref_protected": self.ref_protected,
            "sha": self.sha,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> "WorkflowContext":
        if type(value) is not dict or set(value) != _CONTEXT_KEYS:
            raise WorkflowPublishError("workflow_source_mismatch")
        try:
            return cls(**value)
        except TypeError:
            raise WorkflowPublishError("workflow_source_mismatch") from None


@dataclass(frozen=True)
class WorkflowPublicationHandoff:
    schema_version: int
    repository_identity: RepositoryIdentity
    manifest_sha256: str
    context: WorkflowContext
    bundle_sha256: str
    bundle_size: int
    bundle_identity: RepositoryIdentity

    def __post_init__(self) -> None:
        for identity in (self.repository_identity, self.bundle_identity):
            if (
                type(identity) is not tuple
                or len(identity) != 2
                or any(type(item) is not int or item < 0 for item in identity)
                or identity[1] <= 0
            ):
                raise WorkflowPublishError("workflow_source_mismatch")
        if (
            self.schema_version != 1
            or _HEX64.fullmatch(self.manifest_sha256) is None
            or _HEX64.fullmatch(self.bundle_sha256) is None
            or type(self.bundle_size) is not int
            or self.bundle_size < 0
            or type(self.context) is not WorkflowContext
        ):
            raise WorkflowPublishError("workflow_source_mismatch")

    @classmethod
    def capture(
        cls,
        repository_identity: RepositoryIdentity,
        manifest_sha256: str,
        context: WorkflowContext,
        bundle: Path,
    ) -> "WorkflowPublicationHandoff":
        digest, size, identity = _hash_bound_regular(bundle)
        return cls(1, repository_identity, manifest_sha256, context, digest, size, identity)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "repository_identity": _identity_document(self.repository_identity),
            "manifest_sha256": self.manifest_sha256,
            "context": self.context.to_dict(),
            "bundle_sha256": self.bundle_sha256,
            "bundle_size": self.bundle_size,
            "bundle_identity": _identity_document(self.bundle_identity),
        }

    @classmethod
    def from_dict(cls, value: object) -> "WorkflowPublicationHandoff":
        if type(value) is not dict or set(value) != _HANDOFF_KEYS:
            raise WorkflowPublishError("workflow_source_mismatch")
        return cls(
            value["schema_version"],
            _parse_identity(value["repository_identity"]),
            value["manifest_sha256"],
            WorkflowContext.from_dict(value["context"]),
            value["bundle_sha256"],
            value["bundle_size"],
            _parse_identity(value["bundle_identity"]),
        )


def _identity_document(identity: RepositoryIdentity) -> dict[str, int]:
    return {"device": identity[0], "inode": identity[1]}


def _parse_identity(value: object) -> RepositoryIdentity:
    if type(value) is not dict or set(value) != _IDENTITY_KEYS:
        raise WorkflowPublishError("workflow_source_mismatch")
    device, inode = value["device"], value["inode"]
    if type(device) is not int or type(inode) is not int:
        raise WorkflowPublishError("workflow_source_mismatch")
    return device, inode


def _canonical_json(value: object) -> bytes:
    return (json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ) + "\n").encode("utf-8")


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _runner_temp_path(path: Path) -> Path:
    try:
        raw = os.environ["RUNNER_TEMP"]
        if not raw or len(raw.encode("utf-8")) > 4096 or not path.is_absolute():
            raise ValueError
        root = Path(raw).resolve(strict=True)
        root_info = Path(raw).lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ValueError
        parent = path.parent.resolve(strict=True)
        relative_parent = parent.relative_to(root)
        cursor = root
        for segment in relative_parent.parts:
            cursor = cursor / segment
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError
        return path
    except (KeyError, OSError, ValueError, TypeError, UnicodeError):
        raise WorkflowPublishError("workflow_source_mismatch") from None


def write_publication_handoff(
    path: Path, handoff: WorkflowPublicationHandoff
) -> None:
    destination = _runner_temp_path(path)
    payload = _canonical_json(handoff.to_dict())
    if len(payload) > 65_536:
        raise WorkflowPublishError("workflow_source_mismatch")
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        parent = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except OSError:
        raise WorkflowPublishError("workflow_source_mismatch") from None


def load_publication_handoff(path: Path) -> WorkflowPublicationHandoff:
    source = _runner_temp_path(path)
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 65_536
            ):
                raise OSError
            payload = _read_capped(descriptor, 65_536)
            after = os.fstat(descriptor)
            named = os.stat(source, follow_symlinks=False)
            if (
                (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)
                or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)
                or after.st_size != info.st_size
                or after.st_mtime_ns != info.st_mtime_ns
                or after.st_ctime_ns != info.st_ctime_ns
            ):
                raise OSError
        finally:
            os.close(descriptor)
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            parse_float=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        handoff = WorkflowPublicationHandoff.from_dict(value)
        if payload != _canonical_json(handoff.to_dict()):
            raise ValueError
        return handoff
    except WorkflowPublishError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise WorkflowPublishError("workflow_source_mismatch") from None


@dataclass(frozen=True)
class GitCheckoutSnapshot:
    object_format: Literal["sha1", "sha256"]
    commit_oid: str
    commit_tree_oid: str
    index_tree_oid: str
    safe_projection_digest: str


@dataclass(frozen=True)
class GitCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class DescriptorGitRunner(Protocol):
    def run(
        self,
        scope: WorkflowGitScope,
        arguments: tuple[str, ...],
        *,
        before_exec: Callable[[], None] = _noop,
    ) -> GitCommandResult: ...


class _SystemDescriptorGitRunner:
    def run(
        self,
        scope: WorkflowGitScope,
        arguments: tuple[str, ...],
        *,
        before_exec: Callable[[], None] = _noop,
    ) -> GitCommandResult:
        if sys.platform != "linux" or not Path("/proc/self/fd").is_dir():
            raise WorkflowPublishError("workflow_platform_unsupported")
        before_exec()
        scope.revalidate()
        git_fd = -1
        try:
            git_fd = os.open(
                ".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=scope.checkout_descriptor,
            )
            executable = "/usr/bin/git"
            info = os.stat(executable, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise OSError
            environment = {
                "GIT_DIR": f"/proc/self/fd/{git_fd}",
                "GIT_WORK_TREE": f"/proc/self/fd/{scope.checkout_descriptor}",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_LITERAL_PATHSPECS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
            }
            result = subprocess.run(
                (executable, "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *arguments),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
                pass_fds=(git_fd, scope.checkout_descriptor),
                preexec_fn=lambda: os.fchdir(scope.checkout_descriptor),
            )
            if len(result.stdout) + len(result.stderr) > 8 * 1024 * 1024:
                raise WorkflowPublishError("workflow_git_unavailable")
            return GitCommandResult(result.returncode, result.stdout, result.stderr)
        except WorkflowPublishError:
            raise
        except Exception:
            raise WorkflowPublishError("workflow_git_unavailable") from None
        finally:
            if git_fd >= 0:
                os.close(git_fd)


SYSTEM_DESCRIPTOR_GIT_RUNNER: DescriptorGitRunner = _SystemDescriptorGitRunner()


@dataclass(frozen=True)
class PublicationDependencies:
    git_runner: DescriptorGitRunner


REAL_PUBLICATION_DEPENDENCIES = PublicationDependencies(
    SYSTEM_DESCRIPTOR_GIT_RUNNER
)


def _git(
    runner: DescriptorGitRunner,
    scope: WorkflowGitScope,
    arguments: tuple[str, ...],
    before_exec: Callable[[], None],
    *,
    allow_one: bool = False,
) -> bytes:
    scope.revalidate()
    before_exec()
    result = runner.run(scope, arguments, before_exec=_noop)
    if type(result) is not GitCommandResult or result.returncode not in ({0, 1} if allow_one else {0}):
        raise WorkflowPublishError("workflow_git_unavailable")
    if result.returncode == 1:
        raise WorkflowPublishError("workflow_source_mismatch")
    return result.stdout


def capture_descriptor_git_checkout(
    scope: WorkflowGitScope,
    staged: StagedInput,
    expected_oid: str,
    runner: DescriptorGitRunner,
    *,
    before_exec: Callable[[], None] = _noop,
) -> GitCheckoutSnapshot:
    if type(scope) is not WorkflowGitScope or type(staged) is not StagedInput:
        raise WorkflowPublishError("workflow_git_unavailable")
    object_format = _git(runner, scope, ("rev-parse", "--show-object-format"), before_exec).decode().strip()
    oid_pattern = _HEX40 if object_format == "sha1" else _HEX64 if object_format == "sha256" else None
    if oid_pattern is None or oid_pattern.fullmatch(expected_oid) is None:
        raise WorkflowPublishError("workflow_source_mismatch")
    commit = _git(runner, scope, ("rev-parse", "HEAD^{commit}"), before_exec).decode().strip()
    tree = _git(runner, scope, ("rev-parse", "HEAD^{tree}"), before_exec).decode().strip()
    index_tree = _git(runner, scope, ("write-tree",), before_exec).decode().strip()
    if commit != expected_oid or any(oid_pattern.fullmatch(value) is None for value in (tree, index_tree)):
        raise WorkflowPublishError("workflow_source_mismatch")
    prefix = "/".join(scope.project_segments)
    pathspec = prefix if prefix else "."
    _git(runner, scope, ("diff-index", "--quiet", expected_oid, "--", pathspec), before_exec, allow_one=True)
    untracked = _git(
        runner, scope,
        ("ls-files", "--others", "--exclude-standard", "-z", "--", pathspec),
        before_exec,
    )
    if untracked:
        raise WorkflowPublishError("workflow_source_mismatch")
    projection_by_path = {item.path: item for item in staged.projection_files}
    if set(projection_by_path) != set(staged.files):
        raise WorkflowPublishError("workflow_source_mismatch")
    for relative in staged.files:
        git_path = "/".join((*scope.project_segments, *relative.parts))
        payload = _git(runner, scope, ("show", f"{expected_oid}:{git_path}"), before_exec)
        item = projection_by_path[relative]
        if len(payload) != item.byte_length or hashlib.sha256(payload).hexdigest() != item.sha256:
            raise WorkflowPublishError("workflow_source_mismatch")
    if staged.projection_digest is None or _HEX64.fullmatch(staged.projection_digest) is None:
        raise WorkflowPublishError("workflow_source_mismatch")
    return GitCheckoutSnapshot(
        object_format, commit, tree, index_tree, staged.projection_digest
    )


def _same_projection(left: StagedInput, right: StagedInput) -> None:
    if (
        left.source_digest != right.source_digest
        or left.projection_digest != right.projection_digest
        or left.files != right.files
        or left.projection_files != right.projection_files
        or left.reason_counts != right.reason_counts
        or left.coverage_approvals != right.coverage_approvals
    ):
        raise WorkflowPublishError("workflow_source_mismatch")


def _root_git_scope(repository: Any) -> WorkflowGitScope:
    def revalidate() -> None:
        try:
            info = os.fstat(repository.descriptor)
            if (info.st_dev, info.st_ino) != repository.identity:
                raise OSError
        except OSError:
            raise WorkflowPublishError("workflow_git_unavailable") from None

    return WorkflowGitScope(
        repository.descriptor,
        repository.descriptor,
        (),
        repository.identity,
        repository.identity,
        revalidate,
    )


def _git_identity(sha: str) -> ArtifactGitIdentity:
    return ArtifactGitIdentity(sha, "sha1" if len(sha) == 40 else "sha256")


def _require_publication_identity(
    artifact: Any,
    manifest: ProjectManifest,
    staged: StagedInput,
    git: GitCheckoutSnapshot,
    context: WorkflowContext,
) -> None:
    transport = artifact.transport
    config = manifest.artifacts
    expected_git = _git_identity(context.sha)
    if (
        manifest.schema_version != 2
        or manifest.project_uid is None
        or config.provider != "github-release"
        or not isinstance(transport, GithubTransport)
        or artifact.project_id != manifest.project_id
        or artifact.project_uid != str(manifest.project_uid)
        or artifact.graphify_version != manifest.graphify_version
        or artifact.source_digest != staged.source_digest
        or artifact.projection_digest != staged.projection_digest
        or context.repository != config.repository
        or context.repository_id != config.repository_id
        or context.ref != config.source_ref
        or transport.repository != config.repository
        or transport.repository_id != config.repository_id
        or transport.source_ref != config.source_ref
        or transport.channel != config.channel
        or git.commit_oid != context.sha
        or (artifact.git is not None and (
            artifact.git.commit_oid != expected_git.commit_oid
            or artifact.git.algorithm != expected_git.algorithm
        ))
    ):
        raise WorkflowPublishError("workflow_source_mismatch")


def prepare_publication(
    repo_root: Path,
    build_bundle: Path,
    output: Path,
    context: WorkflowContext,
    *,
    dependencies: PublicationDependencies = REAL_PUBLICATION_DEPENDENCIES,
    expected_repository_identity: RepositoryIdentity | None = None,
    expected_manifest: ProjectManifest | None = None,
    workflow_git_scope: WorkflowGitScope | None = None,
) -> PackedBundle:
    context.__post_init__()
    recovery_id = secrets.token_hex(16)
    commit_state = _PackCommitState()
    prepared: PackedBundle | None = None
    temporary: Any = None
    try:
        with repository_lifecycle_lock(
            repo_root, expected_repository_identity=expected_repository_identity
        ), capture_lifecycle_repository(repo_root) as repository:
            if inspect_init_journal(repo_root, repository_access=repository) != "none":
                raise WorkflowPublishError("init_recovery_required")
            if expected_manifest is None:
                manifest = load_manifest(
                    repo_root / ".graphify-project.yaml", repo_root,
                    repository_access=repository,
                )
            else:
                manifest = expected_manifest
            manifest = require_current_manifest(
                repo_root, manifest, repository_access=repository
            )
            try:
                with managed_operation_temp_root(
                    "publication", recovery_id
                ) as temporary, ExitStack() as parsed_scope:
                    private = temporary.path
                    staged = stage_input(
                        repo_root, manifest, private / "source",
                        repository_access=repository,
                    )
                    scope = workflow_git_scope or _root_git_scope(repository)
                    if scope.project_identity != repository.identity:
                        raise WorkflowPublishError("workflow_source_mismatch")

                    def git_checkpoint() -> None:
                        scope.revalidate()
                        assert_current_manifest_unchanged(
                            repo_root, manifest, repository_access=repository
                        )

                    git = capture_descriptor_git_checkout(
                        scope, staged, context.sha, dependencies.git_runner,
                        before_exec=git_checkpoint,
                    )
                    parsed = parsed_scope.enter_context(
                        parse_bundle(build_bundle, private / "candidate")
                    )
                    _require_publication_identity(
                        parsed.artifact, manifest, staged, git, context
                    )
                    evidence_path = PurePosixPath("graphify-out/GRAPH_EVIDENCE.json")
                    evidence_binding = next(
                        item for item in parsed.payloads if item.path == evidence_path
                    )
                    parse_graph_evidence(
                        parsed.read_payload(evidence_path, GRAPH_EVIDENCE_MAX_BYTES),
                        resolve_graphify_compatibility(manifest.graphify_version),
                        expected_digest=evidence_binding.sha256,
                    )
                    validated = validate_candidate(
                        parsed.root,
                        staged,
                        manifest,
                        expected_projection_digest=staged.projection_digest,
                        expected_evidence_digest=evidence_binding.sha256,
                        build_epoch=parsed.artifact.build_epoch,
                        git_identity=_git_identity(context.sha),
                    )
                    before = stage_input(
                        repo_root, manifest, private / "source-check",
                        repository_access=repository,
                    )
                    _same_projection(staged, before)
                    captured = _capture_validated_generation(
                        validated, manifest, private / "captured-generation"
                    )
                    after = stage_input(
                        repo_root, manifest, private / "source-after",
                        repository_access=repository,
                    )
                    _same_projection(staged, after)
                    assert_current_manifest_unchanged(
                        repo_root, manifest, repository_access=repository
                    )

                    def precommit() -> None:
                        assert_current_manifest_unchanged(
                            repo_root, manifest, repository_access=repository
                        )
                        final = stage_input(
                            repo_root, manifest, private / "source-precommit",
                            repository_access=repository,
                        )
                        _same_projection(staged, final)
                        assert_current_manifest_unchanged(
                            repo_root, manifest, repository_access=repository
                        )

                    prepared = _pack_captured_generation(
                        captured, output, commit_state=commit_state,
                        precommit_check=precommit,
                    )
            except OperationTempCleanupError:
                code = (
                    "workflow_output_recovery_required"
                    if commit_state.output_committed or commit_state.output_recovery_required
                    else "workflow_cleanup_failed"
                )
                raise WorkflowPublishError(code, recovery_id=recovery_id) from None
            except Exception:
                if commit_state.output_committed or commit_state.output_recovery_required:
                    raise WorkflowPublishError(
                        "workflow_output_recovery_required", recovery_id=recovery_id
                    ) from None
                raise
            if temporary.cleanup_failed:
                code = (
                    "workflow_output_recovery_required"
                    if commit_state.output_committed else "workflow_cleanup_failed"
                )
                raise WorkflowPublishError(code, recovery_id=recovery_id)
        assert prepared is not None
        if (
            prepared.artifact.git is None
            or prepared.artifact.git.commit_oid != context.sha
            or prepared.artifact.generation_digest != captured.validated.generation_digest
        ):
            raise WorkflowPublishError("workflow_source_mismatch")
        return prepared
    except WorkflowPublishError:
        raise
    except BundleError as error:
        raise WorkflowPublishError("workflow_source_mismatch") from error


@dataclass(frozen=True)
class PublishedAsset:
    asset_id: int
    name: str
    size: int
    sha256: str
    tag: str


class GithubMutationClient(Protocol):
    def get_repository(self, repository: str, credentials: GithubCredentials) -> Mapping[str, object]: ...
    def get_or_create_release(self, repository: str, tag: str, context: WorkflowContext, credentials: GithubCredentials) -> Mapping[str, object]: ...
    def upload_asset(self, repository: str, release: Mapping[str, object], name: str, bundle: PackedBundle, credentials: GithubCredentials) -> Mapping[str, object]: ...
    def get_asset(self, repository: str, asset_id: int, credentials: GithubCredentials) -> Mapping[str, object]: ...
    def move_tag(self, repository: str, tag: str, sha: str, credentials: GithubCredentials) -> None: ...
    def update_release(self, repository: str, release_id: int, sha: str, credentials: GithubCredentials) -> None: ...
    def list_assets(self, repository: str, release_id: int, credentials: GithubCredentials) -> Sequence[Mapping[str, object]]: ...
    def delete_asset(self, repository: str, asset_id: int, credentials: GithubCredentials) -> None: ...


class _GithubRestMutations:
    def _request(self, method: str, repository: str, path: str, credentials: GithubCredentials, *, body: bytes | None = None, headers: Mapping[str, str] | None = None, host: str = "api.github.com") -> tuple[int, object]:
        connection = http.client.HTTPSConnection(host, timeout=30, context=ssl.create_default_context())
        request_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {credentials.token}",
            "User-Agent": "atlasweaver",
            "X-GitHub-Api-Version": "2022-11-28",
            **dict(headers or {}),
        }
        try:
            connection.request(method, f"/repos/{repository}{path}", body=body, headers=request_headers)
            response = connection.getresponse()
            payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise WorkflowPublishError("workflow_github_failed")
            document = None if not payload else json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_pairs)
            return response.status, document
        except WorkflowPublishError:
            raise
        except Exception:
            raise WorkflowPublishError("workflow_github_failed") from None
        finally:
            connection.close()

    def get_repository(self, repository: str, credentials: GithubCredentials) -> Mapping[str, object]:
        status, value = self._request("GET", repository, "", credentials)
        if status != 200 or type(value) is not dict:
            raise WorkflowPublishError("workflow_github_failed")
        return value

    def get_or_create_release(self, repository: str, tag: str, context: WorkflowContext, credentials: GithubCredentials) -> Mapping[str, object]:
        status, value = self._request("GET", repository, "/releases/tags/" + quote(tag, safe=""), credentials)
        if status == 200 and type(value) is dict:
            return {**value, "_atlasweaver_created": False}
        if status != 404:
            raise WorkflowPublishError("workflow_github_failed")
        body = _canonical_json({"tag_name": tag, "target_commitish": context.sha, "name": tag, "draft": False, "prerelease": False})
        status, value = self._request("POST", repository, "/releases", credentials, body=body, headers={"Content-Type": "application/json"})
        if status != 201 or type(value) is not dict:
            raise WorkflowPublishError("workflow_github_failed")
        return {**value, "_atlasweaver_created": True}

    def upload_asset(self, repository: str, release: Mapping[str, object], name: str, bundle: PackedBundle, credentials: GithubCredentials) -> Mapping[str, object]:
        upload = release.get("upload_url")
        if type(upload) is not str:
            raise WorkflowPublishError("workflow_github_failed")
        split = urlsplit(upload.partition("{")[0])
        if split.scheme != "https" or split.hostname != "uploads.github.com":
            raise WorkflowPublishError("workflow_github_failed")
        payload = bundle.path.read_bytes()
        connection = http.client.HTTPSConnection("uploads.github.com", timeout=30, context=ssl.create_default_context())
        try:
            connection.request("POST", split.path + "?" + urlencode({"name": name}), body=payload, headers={
                "Accept": "application/vnd.github+json", "Authorization": f"Bearer {credentials.token}",
                "Content-Type": "application/zip", "Content-Length": str(bundle.byte_length),
                "User-Agent": "atlasweaver", "X-GitHub-Api-Version": "2022-11-28",
            })
            response = connection.getresponse()
            value = json.loads(response.read(4 * 1024 * 1024).decode("utf-8"), object_pairs_hook=_unique_pairs)
            if response.status != 201 or type(value) is not dict:
                raise WorkflowPublishError("workflow_github_failed")
            return value
        finally:
            connection.close()

    def get_asset(self, repository: str, asset_id: int, credentials: GithubCredentials) -> Mapping[str, object]:
        status, value = self._request("GET", repository, f"/releases/assets/{asset_id}", credentials)
        if status != 200 or type(value) is not dict:
            raise WorkflowPublishError("workflow_github_failed")
        return value

    def move_tag(self, repository: str, tag: str, sha: str, credentials: GithubCredentials) -> None:
        body = _canonical_json({"sha": sha, "force": True})
        status, _ = self._request("PATCH", repository, "/git/refs/tags/" + quote(tag, safe=""), credentials, body=body, headers={"Content-Type": "application/json"})
        if status != 200:
            raise WorkflowPublishError("workflow_github_failed")

    def update_release(self, repository: str, release_id: int, sha: str, credentials: GithubCredentials) -> None:
        body = _canonical_json({"target_commitish": sha})
        status, _ = self._request("PATCH", repository, f"/releases/{release_id}", credentials, body=body, headers={"Content-Type": "application/json"})
        if status != 200:
            raise WorkflowPublishError("workflow_github_failed")

    def list_assets(self, repository: str, release_id: int, credentials: GithubCredentials) -> Sequence[Mapping[str, object]]:
        status, value = self._request("GET", repository, f"/releases/{release_id}/assets?per_page=100", credentials)
        if status != 200 or type(value) is not list or any(type(item) is not dict for item in value):
            raise WorkflowPublishError("workflow_github_failed")
        return value

    def delete_asset(self, repository: str, asset_id: int, credentials: GithubCredentials) -> None:
        status, _ = self._request("DELETE", repository, f"/releases/assets/{asset_id}", credentials)
        if status != 204:
            raise WorkflowPublishError("workflow_github_failed")


REAL_GITHUB_MUTATIONS: GithubMutationClient = _GithubRestMutations()


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise WorkflowPublishError("remote_asset_mismatch")
    return value


def _asset_name(bundle: PackedBundle) -> str:
    artifact = bundle.artifact
    transport = artifact.transport
    if not isinstance(transport, GithubTransport):
        raise WorkflowPublishError("workflow_source_mismatch")
    return (
        f"atlasweaver-graph-{artifact.project_uid}-{artifact.source_digest}-"
        f"{artifact.projection_digest}-{bundle.sha256}.zip"
    )


def _verify_asset(value: Mapping[str, object], name: str, bundle: PackedBundle) -> int:
    asset_id = _positive_int(value.get("id"))
    if (
        value.get("name") != name
        or value.get("size") != bundle.byte_length
        or value.get("digest") != f"sha256:{bundle.sha256}"
    ):
        raise WorkflowPublishError("remote_asset_mismatch")
    return asset_id


def publish_release_asset(
    bundle: PackedBundle,
    context: WorkflowContext,
    credentials: GithubCredentials,
    client: GithubMutationClient = REAL_GITHUB_MUTATIONS,
) -> PublishedAsset:
    context.__post_init__()
    credentials.__post_init__()
    digest, size, _ = _hash_bound_regular(bundle.path)
    if digest != bundle.sha256 or size != bundle.byte_length:
        raise WorkflowPublishError("workflow_source_mismatch")
    artifact = bundle.artifact
    transport = artifact.transport
    if (
        not isinstance(transport, GithubTransport)
        or transport.repository != context.repository
        or transport.repository_id != context.repository_id
        or artifact.git is None
        or artifact.git.commit_oid != context.sha
    ):
        raise WorkflowPublishError("workflow_source_mismatch")
    repository = client.get_repository(context.repository, credentials)
    if repository.get("id") != context.repository_id or repository.get("full_name") != context.repository:
        raise WorkflowPublishError("workflow_source_mismatch")
    tag = f"atlasweaver-graph-{artifact.project_uid}-{transport.channel}"
    release = client.get_or_create_release(context.repository, tag, context, credentials)
    release_id = _positive_int(release.get("id"))
    created = release.get("_atlasweaver_created") is True
    name = _asset_name(bundle)
    existing = next(
        (item for item in release.get("assets", ()) if type(item) is dict and item.get("name") == name),
        None,
    )
    if existing is None:
        uploaded = client.upload_asset(context.repository, release, name, bundle, credentials)
        asset_id = _positive_int(uploaded.get("id"))
    else:
        asset_id = _verify_asset(existing, name, bundle)
    remote = client.get_asset(context.repository, asset_id, credentials)
    asset_id = _verify_asset(remote, name, bundle)
    if not created:
        client.move_tag(context.repository, tag, context.sha, credentials)
        client.update_release(context.repository, release_id, context.sha, credentials)
    assets = client.list_assets(context.repository, release_id, credentials)
    grammar = re.compile(
        rf"atlasweaver-graph-{re.escape(artifact.project_uid)}-[0-9a-f]{{64}}-"
        rf"[0-9a-f]{{64}}-[0-9a-f]{{64}}\.zip\Z"
    )
    matching: list[tuple[str, int, Mapping[str, object]]] = []
    for item in assets:
        item_name, created_at, item_id = item.get("name"), item.get("created_at"), item.get("id")
        if (
            type(item_name) is str and grammar.fullmatch(item_name)
            and type(created_at) is str and _ASSET_CREATED.fullmatch(created_at)
            and type(item_id) is int and item_id > 0
        ):
            matching.append((created_at, item_id, item))
    matching.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _, old_id, _ in matching[20:]:
        if old_id != asset_id:
            client.delete_asset(context.repository, old_id, credentials)
    return PublishedAsset(asset_id, name, bundle.byte_length, bundle.sha256, tag)


def _manifest_digest(manifest: ProjectManifest) -> str:
    return hashlib.sha256(render_manifest_v2(manifest)).hexdigest()


def _hash_bound_regular(path: Path) -> tuple[str, int, RepositoryIdentity]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise OSError
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
            after = os.fstat(descriptor)
            named = os.stat(path, follow_symlinks=False)
            identity = (before.st_dev, before.st_ino)
            if identity != (after.st_dev, after.st_ino) or identity != (named.st_dev, named.st_ino) or before.st_size != size:
                raise OSError
            return digest.hexdigest(), size, identity
        finally:
            os.close(descriptor)
    except OSError:
        raise WorkflowPublishError("workflow_source_mismatch") from None


def _validate_handoff(
    workflow: Any,
    manifest: ProjectManifest,
    context: WorkflowContext,
    bundle: Path,
    handoff_path: Path,
) -> tuple[WorkflowPublicationHandoff, PackedBundle]:
    _runner_temp_path(bundle)
    handoff = load_publication_handoff(handoff_path)
    digest, size, identity = _hash_bound_regular(bundle)
    if (
        handoff.repository_identity != workflow.repository_identity
        or handoff.manifest_sha256 != _manifest_digest(manifest)
        or handoff.context != context
        or handoff.bundle_sha256 != digest
        or handoff.bundle_size != size
        or handoff.bundle_identity != identity
    ):
        raise WorkflowPublishError("workflow_source_mismatch")
    artifact = parse_bundle(bundle, Path(os.environ["RUNNER_TEMP"]) / f"validate-{secrets.token_hex(16)}")
    try:
        packed = PackedBundle(bundle, digest, size, artifact.artifact)
    finally:
        artifact.close()
        try:
            import shutil
            shutil.rmtree(artifact.root)
        except OSError:
            pass
    if packed.artifact.git is None or packed.artifact.git.commit_oid != context.sha:
        raise WorkflowPublishError("workflow_source_mismatch")
    workflow.revalidate()
    return handoff, packed


def _open_admitted(
    consumer_checkout: Path, repo_root: str, trusted_tool_checkout: Path
) -> tuple[Any, Path, ProjectManifest, WorkflowContext]:
    workflow = open_workflow_repository(
        consumer_checkout, repo_root, forbidden_checkout=trusted_tool_checkout
    )
    try:
        path = consumer_checkout.absolute().joinpath(*workflow.segments)
        manifest = load_manifest(
            path / ".graphify-project.yaml", path,
            repository_access=workflow.repository_access,
        )
        manifest = require_current_manifest(
            path, manifest, repository_access=workflow.repository_access
        )
        context = WorkflowContext.from_environment()
        workflow.revalidate()
        return workflow, path, manifest, context
    except (WorkflowBoundaryError, WorkflowPublishError):
        workflow.__exit__(None, None, None)
        raise
    except Exception:
        workflow.__exit__(None, None, None)
        raise WorkflowPublishError("workflow_source_mismatch") from None


def run_workflow_prepare(
    consumer_checkout: Path,
    repo_root: str,
    trusted_tool_checkout: Path,
    input_bundle: Path,
    output: Path,
    handoff: Path,
) -> None:
    _runner_temp_path(input_bundle)
    _runner_temp_path(output)
    _runner_temp_path(handoff)
    workflow, path, manifest, context = _open_admitted(
        consumer_checkout, repo_root, trusted_tool_checkout
    )
    try:
        packed = prepare_publication(
            path, input_bundle, output, context,
            expected_repository_identity=workflow.repository_identity,
            expected_manifest=manifest,
            workflow_git_scope=workflow.git_scope,
        )
        workflow.revalidate()
        document = WorkflowPublicationHandoff.capture(
            workflow.repository_identity, _manifest_digest(manifest), context, packed.path
        )
        write_publication_handoff(handoff, document)
        workflow.revalidate()
    finally:
        workflow.__exit__(None, None, None)


def _read_token() -> GithubCredentials:
    token = os.environ.pop("GITHUB_TOKEN", None)
    try:
        return GithubCredentials(token)  # type: ignore[arg-type]
    except Exception:
        raise WorkflowPublishError("workflow_github_failed") from None


def run_workflow_verify_attestation(
    consumer_checkout: Path,
    repo_root: str,
    trusted_tool_checkout: Path,
    bundle: Path,
    handoff: Path,
) -> None:
    workflow, _, manifest, context = _open_admitted(
        consumer_checkout, repo_root, trusted_tool_checkout
    )
    try:
        _, packed = _validate_handoff(workflow, manifest, context, bundle, handoff)
        config = manifest.artifacts
        if any(value is None for value in (config.signer_workflow, config.signer_digest, config.source_ref)):
            raise WorkflowPublishError("workflow_source_mismatch")
        credentials = _read_token()
        gh = resolve_gh_executable()
        verify_attestation_policy(
            packed.path,
            AttestationPolicy(
                context.repository,
                config.signer_workflow or "",
                config.signer_digest or "",
                context.ref,
                context.sha,
                _PREDICATE,
            ),
            credentials,
            gh,
            before_exec=workflow.revalidate,
        )
        workflow.revalidate()
    finally:
        workflow.__exit__(None, None, None)


def run_workflow_upload(
    consumer_checkout: Path,
    repo_root: str,
    trusted_tool_checkout: Path,
    bundle: Path,
    handoff: Path,
) -> None:
    workflow, _, manifest, context = _open_admitted(
        consumer_checkout, repo_root, trusted_tool_checkout
    )
    try:
        _, packed = _validate_handoff(workflow, manifest, context, bundle, handoff)
        credentials = _read_token()
        publish_release_asset(packed, context, credentials)
        workflow.revalidate()
    finally:
        workflow.__exit__(None, None, None)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--consumer-checkout", type=Path, required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--trusted-tool-checkout", type=Path, required=True)


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m project_knowledge.workflow_publish")
    subparsers = parser.add_subparsers(dest="verb", required=True)
    prepare = subparsers.add_parser("prepare")
    _add_common(prepare)
    prepare.add_argument("--input", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--handoff", type=Path, required=True)
    verify = subparsers.add_parser("verify-attestation")
    _add_common(verify)
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--handoff", type=Path, required=True)
    upload = subparsers.add_parser("upload")
    _add_common(upload)
    upload.add_argument("--bundle", type=Path, required=True)
    upload.add_argument("--handoff", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.verb == "prepare":
            run_workflow_prepare(
                arguments.consumer_checkout, arguments.repo_root,
                arguments.trusted_tool_checkout, arguments.input,
                arguments.output, arguments.handoff,
            )
        elif arguments.verb == "verify-attestation":
            run_workflow_verify_attestation(
                arguments.consumer_checkout, arguments.repo_root,
                arguments.trusted_tool_checkout, arguments.bundle,
                arguments.handoff,
            )
        elif arguments.verb == "upload":
            run_workflow_upload(
                arguments.consumer_checkout, arguments.repo_root,
                arguments.trusted_tool_checkout, arguments.bundle,
                arguments.handoff,
            )
        else:
            return 1
        return 0
    except Exception:
        return 1


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError
        view = view[written:]


def _read_capped(descriptor: int, cap: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(65_536, cap + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > cap:
            raise OSError
        chunks.append(chunk)


if __name__ == "__main__":
    raise SystemExit(_main())
