"""Evidence-bound, descriptor-authorized project graph refresh orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import tempfile
from typing import Literal, NoReturn

from .adapter import adapt_candidate
from .adapters import CapturedArtifact, adapter_for, capture_native_artifact
from .artifacts import (
    ArtifactValidationError,
    ValidatedGraph,
    promote_graph,
    validate_candidate,
    validate_owned_graph,
)
from .compatibility import (
    CompatibilityError,
    RenderedCommand,
    admitted_graphify_environment,
    render_graphify_argv,
    resolve_graphify_compatibility,
    validate_public_model_identifier,
    validate_semantic_backend,
)
from .evidence import (
    CommandEnvironmentBinding,
    build_extraction_invocation,
    build_graph_evidence,
)
from .graphify import (
    CommandRunner,
    SubprocessCommandRunner,
    _load_bounded_json_text,
    _sanitize_diagnosis,
    probe_graphify,
    resolve_graphify_executable,
    run_graphify_operation,
)
from .health import assess_health, inspect_project_state
from .locking import (
    RepositoryAccess,
    RepositoryIdentity,
    capture_lifecycle_repository,
    open_repository_access,
    repository_lifecycle_lock,
)
from .manifest import (
    assert_current_manifest_unchanged,
    inspect_init_journal,
    require_current_manifest,
)
from .models import ProjectManifest
from .staging import StagedInput, inspect_projection, stage_input_with_receipt


EXTRACT_TIMEOUT_SECONDS = 7_200.0
DIAGNOSE_TIMEOUT_SECONDS = 60.0
CLUSTER_TIMEOUT_SECONDS = 1_800.0

_BASE_ENVIRONMENT_NAMES = frozenset({"HOME", "LANG", "LC_ALL", "PATH"})
_RECOVERY_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BUILD_EPOCH = 2**63 - 1
_ARTIFACT_CAP = 134_217_728


@dataclass(frozen=True)
class RefreshOptions:
    backend: str | None
    model: str | None
    deep: bool
    code_only: bool


@dataclass(frozen=True)
class RefreshResult:
    status: Literal["refreshed", "unchanged", "promoted_but_stale"]
    source_digest: str
    projection_digest: str
    graph_digest: str | None
    generation_digest: str | None
    build_epoch: int | None
    core_status: str
    trust: Literal["trusted", "navigation"]
    limitations: tuple[str, ...]
    recovery_id: str | None = None

    def __post_init__(self) -> None:
        if (
            self.status not in {"refreshed", "unchanged", "promoted_but_stale"}
            or self.trust not in {"trusted", "navigation"}
            or type(self.limitations) is not tuple
            or self.limitations != tuple(sorted(set(self.limitations)))
            or _DIGEST.fullmatch(self.source_digest) is None
            or _DIGEST.fullmatch(self.projection_digest) is None
        ):
            raise ValueError("refresh result is invalid")
        identity = (self.graph_digest, self.generation_digest, self.build_epoch)
        if self.status in {"refreshed", "unchanged"}:
            if (
                any(value is None for value in identity)
                or _DIGEST.fullmatch(self.graph_digest or "") is None
                or _DIGEST.fullmatch(self.generation_digest or "") is None
                or type(self.build_epoch) is not int
                or not 1 <= self.build_epoch <= _MAX_BUILD_EPOCH
            ):
                raise ValueError("refresh result installed identity is invalid")
        elif any(value is not None for value in identity) and (
            any(value is None for value in identity)
            or _DIGEST.fullmatch(self.graph_digest or "") is None
            or _DIGEST.fullmatch(self.generation_digest or "") is None
            or type(self.build_epoch) is not int
            or not 1 <= self.build_epoch <= _MAX_BUILD_EPOCH
        ):
            raise ValueError("refresh result installed identity is invalid")
        if self.recovery_id is not None and _RECOVERY_ID.fullmatch(
            self.recovery_id
        ) is None:
            raise ValueError("refresh recovery ID is invalid")


@dataclass(frozen=True)
class _InstalledGenerationIdentity:
    graph_digest: str
    generation_digest: str
    build_epoch: int


@dataclass
class RefreshError(Exception):
    code: str
    message: str
    recovery_id: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.code) is not str
            or not self.code
            or type(self.message) is not str
            or not self.message
            or (
                self.recovery_id is not None
                and _RECOVERY_ID.fullmatch(self.recovery_id) is None
            )
        ):
            raise ValueError("refresh error is invalid")
        Exception.__init__(self, self.message)


class RefreshFileSystem:
    """Private refresh-run filesystem boundary with failure checkpoints."""

    def __init__(self) -> None:
        self._runs: dict[Path, tuple[int, int]] = {}

    @property
    def live_run_roots(self) -> tuple[Path, ...]:
        return tuple(sorted(self._runs, key=lambda path: path.as_posix()))

    def checkpoint(self, operation: str) -> None:
        del operation

    def create_run_root(self, recovery_id: str) -> Path:
        if type(recovery_id) is not str or _RECOVERY_ID.fullmatch(recovery_id) is None:
            raise OSError("refresh recovery identity is invalid")
        root = Path(
            tempfile.mkdtemp(prefix=f"atlasweaver-refresh-{recovery_id}-")
        ).absolute()
        try:
            os.chmod(root, 0o700)
            info = root.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise OSError("refresh run root is invalid")
            self._runs[root] = (info.st_dev, info.st_ino)
            return root
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise

    def create_private_directory(self, path: Path, *, mode: int = 0o700) -> Path:
        root = self._bound_root(path)
        if type(mode) is not int or mode != 0o700 or path == root:
            raise OSError("refresh private directory is invalid")
        self._require_bound_root(root)
        relative = path.absolute().relative_to(root)
        current = root
        for component in relative.parts:
            current = current / component
            try:
                current.mkdir(mode=mode)
            except FileExistsError:
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise OSError("refresh private directory is invalid") from None
            os.chmod(current, mode)
        return path.absolute()

    def write_private_artifact(
        self,
        run: Path,
        logical_path: PurePosixPath,
        payload: bytes,
        *,
        max_bytes: int,
    ) -> CapturedArtifact:
        root = run.absolute()
        self._require_bound_root(root)
        if (
            type(logical_path) is not PurePosixPath
            or logical_path.is_absolute()
            or not logical_path.parts
            or any(part in {"", ".", ".."} for part in logical_path.parts)
            or type(payload) is not bytes
            or type(max_bytes) is not int
            or max_bytes <= 0
            or len(payload) > max_bytes
        ):
            raise OSError("refresh artifact is invalid")
        parent = root
        for component in logical_path.parts[:-1]:
            parent = self.create_private_directory(parent / component)
        target = parent / logical_path.name
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(target, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("refresh artifact write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)
        return CapturedArtifact.from_payload(logical_path, payload)

    def remove_run_root(self, run: Path) -> None:
        root = run.absolute()
        self._require_bound_root(root)
        shutil.rmtree(root)
        self._runs.pop(root, None)

    def _bound_root(self, path: Path) -> Path:
        absolute = path.absolute()
        matches = [
            root
            for root in self._runs
            if absolute == root or root in absolute.parents
        ]
        if len(matches) != 1:
            raise OSError("refresh path is outside its run root")
        return matches[0]

    def _require_bound_root(self, root: Path) -> None:
        expected = self._runs.get(root)
        if expected is None:
            raise OSError("refresh run root is unavailable")
        info = root.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != expected
        ):
            raise OSError("refresh run root changed")


REAL_REFRESH_FS = RefreshFileSystem()

_SEMANTIC_REFRESH_ERRORS = {
    "semantic_backend_required": (
        "semantic extraction requires an admitted backend credential"
    ),
    "semantic_model_required": (
        "semantic extraction requires a public model identifier"
    ),
}


def _raise_refresh_compatibility(error: CompatibilityError) -> NoReturn:
    code = error.args[0] if len(error.args) == 1 else None
    if code not in _SEMANTIC_REFRESH_ERRORS:
        raise RefreshError(
            "invalid_refresh_options", "semantic refresh options are invalid"
        ) from None
    raise RefreshError(code, _SEMANTIC_REFRESH_ERRORS[code]) from None


def refresh_project(
    repo_root: Path,
    manifest: ProjectManifest,
    options: RefreshOptions,
    *,
    runner: CommandRunner | None = None,
    fs: RefreshFileSystem = REAL_REFRESH_FS,
    ambient: Mapping[str, str] | None = None,
    graphify_binary: Path | None = None,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> RefreshResult:
    """Run the official evidence pipeline and atomically promote one generation."""
    _validate_options(options)
    with open_repository_access(
        repo_root,
        expected_repository_identity=expected_repository_identity,
    ) as admission_repository:
        if inspect_init_journal(
            repo_root, repository_access=admission_repository
        ) != "none":
            raise RefreshError(
                "init_recovery_required", "configuration recovery is required"
            )
        current_manifest = require_current_manifest(
            repo_root, manifest, repository_access=admission_repository
        )
        if current_manifest.schema_version != 2 or current_manifest.project_uid is None:
            raise RefreshError(
                "manifest_migration_required", "refresh requires manifest schema 2"
            )
        contract = resolve_graphify_compatibility(current_manifest.graphify_version)
        admitted_identity = admission_repository.identity
        ambient_values = dict(os.environ if ambient is None else ambient)
        if not options.code_only:
            assert options.backend is not None
            try:
                validate_semantic_backend(contract, options.backend, ambient_values)
            except CompatibilityError as error:
                _raise_refresh_compatibility(error)
        try:
            admitted = admitted_graphify_environment(
                contract, options.backend, ambient_values
            )
        except CompatibilityError as error:
            _raise_refresh_compatibility(error)
        extract_additional_environment = {
            name: value
            for name, value in admitted.items()
            if name not in _BASE_ENVIRONMENT_NAMES
        }
        del admitted, ambient_values

    executable = resolve_graphify_executable(test_override=graphify_binary)
    selected_runner = runner or SubprocessCommandRunner()
    with repository_lifecycle_lock(
        repo_root,
        expected_repository_identity=admitted_identity,
    ), capture_lifecycle_repository(repo_root) as repository:
        if inspect_init_journal(repo_root, repository_access=repository) != "none":
            raise RefreshError(
                "init_recovery_required", "configuration recovery is required"
            )
        current_manifest = require_current_manifest(
            repo_root, current_manifest, repository_access=repository
        )
        fs.checkpoint("preflight")
        assert_current_manifest_unchanged(
            repo_root, current_manifest, repository_access=repository
        )
        capabilities = probe_graphify(
            executable,
            contract,
            selected_runner,
            before_exec=lambda: assert_current_manifest_unchanged(
                repo_root, current_manifest, repository_access=repository
            ),
        )
        recovery_id = secrets.token_hex(16)
        run = fs.create_run_root(recovery_id)
        private_home = fs.create_private_directory(run / "home", mode=0o700)
        local_environment = {
            "HOME": str(private_home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.defpath,
        }
        semantic_environment = {
            **local_environment,
            **extract_additional_environment,
        }
        promotion_committed = False
        installed: _InstalledGenerationIdentity | None = None
        validated: ValidatedGraph | None = None
        staged: StagedInput | None = None
        result: RefreshResult | None = None
        pending: BaseException | None = None
        try:
            fs.checkpoint("projection")
            staged = stage_input_with_receipt(
                repo_root,
                current_manifest,
                run / "stage",
                run / "receipt.json",
                repository_access=repository,
            )
            if staged.projection_digest is None:
                raise RefreshError(
                    "projection_failed", "safe projection identity is unavailable"
                )
            commands: list[RenderedCommand] = []

            extract = render_graphify_argv(
                contract,
                "extract",
                binary=executable.path,
                source=staged.root,
                output=run / "raw",
                backend=options.backend,
                model=options.model,
                code_only=options.code_only,
                deep=options.deep,
            )
            commands.append(extract)
            fs.checkpoint("extract")
            _run_bound_operation(
                repo_root,
                current_manifest,
                repository,
                selected_runner,
                executable,
                extract,
                semantic_environment,
                EXTRACT_TIMEOUT_SECONDS,
            )

            fs.checkpoint("capture-native")
            native = capture_native_artifact(
                run / "raw/graphify-out/graph.json",
                PurePosixPath("raw/graph.json"),
                max_bytes=_ARTIFACT_CAP,
            )
            implementation = adapter_for(contract)
            native_graph = implementation.parse_post_dedup(native)

            diagnose = render_graphify_argv(
                contract,
                "diagnose",
                binary=executable.path,
                source=staged.root,
                output=run / "raw",
                graph=run / "raw/graphify-out/graph.json",
            )
            commands.append(diagnose)
            fs.checkpoint("diagnose")
            diagnosis_result = _run_bound_operation(
                repo_root,
                current_manifest,
                repository,
                selected_runner,
                executable,
                diagnose,
                local_environment,
                DIAGNOSE_TIMEOUT_SECONDS,
            )
            diagnosis_document = _sanitize_diagnosis(
                _load_bounded_json_text(diagnosis_result.stdout)
            )
            diagnosis = fs.write_private_artifact(
                run,
                PurePosixPath("raw/diagnose.json"),
                json.dumps(
                    diagnosis_document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
                max_bytes=4_194_304,
            )

            fs.checkpoint("normalize")
            normalization = implementation.normalize_for_cluster(native_graph)
            cluster_physical = fs.write_private_artifact(
                run,
                PurePosixPath("cluster/graphify-out/graph.json"),
                normalization.cluster_input.payload,
                max_bytes=_ARTIFACT_CAP,
            )
            cluster_input = CapturedArtifact.from_payload(
                PurePosixPath("cluster-input/graph.json"),
                cluster_physical.payload,
            )
            cluster = render_graphify_argv(
                contract,
                "cluster",
                binary=executable.path,
                source=run / "cluster",
                output=run / "cluster",
                graph=run / "cluster/graphify-out/graph.json",
                track_html=current_manifest.track_html,
            )
            commands.append(cluster)
            fs.checkpoint("cluster")
            _run_bound_operation(
                repo_root,
                current_manifest,
                repository,
                selected_runner,
                executable,
                cluster,
                local_environment,
                CLUSTER_TIMEOUT_SECONDS,
            )

            fs.checkpoint("capture-final")
            clustered_graph = capture_native_artifact(
                run / "cluster/graphify-out/graph.json",
                PurePosixPath("clustered/graph.json"),
                max_bytes=_ARTIFACT_CAP,
            )
            report = capture_native_artifact(
                run / "cluster/graphify-out/GRAPH_REPORT.md",
                PurePosixPath("clustered/GRAPH_REPORT.md"),
                max_bytes=_ARTIFACT_CAP,
            )
            final_graph = CapturedArtifact.from_payload(
                PurePosixPath("adapted/graph.json"),
                implementation.adapt_clustered_graph(
                    clustered_graph, staged_files=frozenset(staged.files)
                ),
            )
            captured = [native, diagnosis, cluster_input, clustered_graph, report]
            if current_manifest.track_html:
                captured.append(
                    capture_native_artifact(
                        run / "cluster/graphify-out/graph.html",
                        PurePosixPath("clustered/graph.html"),
                        max_bytes=_ARTIFACT_CAP,
                    )
                )
            invocation = build_extraction_invocation(
                contract,
                executable_sha256=capabilities.executable.launcher_sha256,
                capability_smoke_digest=capabilities.capability_probe.digest,
                commands=tuple(commands),
                backend=options.backend,
                model=options.model,
                configuration_sha256=_configuration_sha256(
                    current_manifest, options
                ),
                source_digest=staged.source_digest,
                projection_digest=staged.projection_digest,
                environments=(
                    CommandEnvironmentBinding(
                        "extract", tuple(sorted(semantic_environment))
                    ),
                    CommandEnvironmentBinding(
                        "diagnose", tuple(sorted(local_environment))
                    ),
                    CommandEnvironmentBinding(
                        "cluster", tuple(sorted(local_environment))
                    ),
                ),
                artifacts=tuple(captured),
            )

            fs.checkpoint("evidence")
            evidence = build_graph_evidence(
                contract,
                source_digest=staged.source_digest,
                projection_digest=staged.projection_digest,
                invocation=invocation,
                native_graph=native,
                diagnosis=diagnosis,
                normalization=normalization,
                clustered_graph=clustered_graph,
                final_graph=final_graph,
                staged_files=frozenset(staged.files),
            )

            fs.checkpoint("adapt")
            adapted = adapt_candidate(
                run / "cluster/graphify-out",
                run / "candidate",
                staged,
                current_manifest,
                evidence=evidence,
                post_write_check=lambda: _require_current_projection(
                    repo_root,
                    current_manifest,
                    staged,
                    repository_access=repository,
                ),
            )
            if (
                adapted.artifact_schema_version != 2
                or adapted.projection_digest != staged.projection_digest
                or adapted.evidence_digest != evidence.digest
            ):
                raise RefreshError(
                    "adaptation_failed",
                    "adapter did not preserve projection evidence",
                )
            existing = _current_owned_generation(
                current_manifest,
                staged,
                repository_access=repository,
            )
            generation_unchanged = (
                existing is not None
                and existing.generation_digest == adapted.generation_digest
            )
            build_epoch = (
                existing.build_epoch
                if generation_unchanged and existing is not None
                else _next_build_epoch(existing)
            )

            fs.checkpoint("validate")
            validated = validate_candidate(
                adapted.root,
                staged,
                current_manifest,
                expected_projection_digest=staged.projection_digest,
                expected_evidence_digest=adapted.evidence_digest,
                build_epoch=build_epoch,
                git_identity=None,
            )
            fs.checkpoint("pre-promote-projection")
            _require_current_projection(
                repo_root,
                current_manifest,
                staged,
                repository_access=repository,
            )
            assert_current_manifest_unchanged(
                repo_root, current_manifest, repository_access=repository
            )
            fs.checkpoint("promote")
            promoted = promote_graph(
                validated, repo_root, repository_access=repository
            )
            promotion_committed = promoted.changed is True
            try:
                installed = _revalidate_installed_generation(
                    current_manifest, repository_access=repository
                )
                summary_matches = (
                    installed is not None
                    and promoted.digest == installed.graph_digest
                    and promoted.generation_digest == installed.generation_digest
                    and promoted.build_epoch == installed.build_epoch
                    and installed.graph_digest == validated.graph_digest
                    and installed.generation_digest == validated.generation_digest
                    and (
                        not promoted.changed
                        or installed.build_epoch == validated.build_epoch
                    )
                    and promoted.changed != generation_unchanged
                )
                if not summary_matches or installed is None:
                    raise RuntimeError("installed promotion identity mismatch")
                fs.checkpoint("post-promote-projection")
                current = inspect_projection(
                    _repository_path(repository),
                    current_manifest,
                    repository_access=repository,
                )
                stale_limitations: set[str] = set()
                if current.source_digest != staged.source_digest:
                    stale_limitations.add("source_changed_after_promotion")
                if current.projection_digest != staged.projection_digest:
                    stale_limitations.add("projection_changed_after_promotion")
                fs.checkpoint("health")
                health = assess_health(
                    inspect_project_state(
                        _repository_path(repository),
                        current_manifest,
                        repository_access=repository,
                    )
                )
                assert_current_manifest_unchanged(
                    repo_root, current_manifest, repository_access=repository
                )
            except Exception:
                if not promotion_committed:
                    raise RefreshError(
                        "refresh_verification_failed",
                        "refresh verification failed",
                    ) from None
                result = _stale_result(
                    staged,
                    validated,
                    installed,
                    {"post_promotion_verification_failed"},
                )
            else:
                unhealthy = health.core_status in {"error", "missing", "stale"}
                limitations = set(validated.impact_limitations) | stale_limitations
                if unhealthy:
                    limitations.add(f"post_promotion_health_{health.core_status}")
                stale = bool(stale_limitations) or unhealthy
                result = RefreshResult(
                    status=(
                        "promoted_but_stale"
                        if stale
                        else ("unchanged" if generation_unchanged else "refreshed")
                    ),
                    source_digest=staged.source_digest,
                    projection_digest=staged.projection_digest,
                    graph_digest=installed.graph_digest,
                    generation_digest=installed.generation_digest,
                    build_epoch=installed.build_epoch,
                    core_status=(
                        "stale"
                        if stale_limitations
                        and health.core_status not in {"error", "missing"}
                        else health.core_status
                    ),
                    trust="navigation" if stale else validated.impact_trust,
                    limitations=tuple(sorted(limitations)),
                )
        except BaseException as error:
            pending = error

        cleanup_error: Exception | None = None
        try:
            fs.checkpoint("cleanup")
        except Exception as error:
            cleanup_error = error
        try:
            fs.remove_run_root(run)
        except OSError as error:
            cleanup_error = cleanup_error or error

        if pending is not None and not isinstance(pending, Exception):
            raise pending
        if cleanup_error is not None:
            if promotion_committed:
                assert staged is not None and validated is not None
                return _stale_result(
                    staged,
                    validated,
                    installed,
                    {"cleanup_failed"},
                    recovery_id=recovery_id,
                )
            raise RefreshError(
                "cleanup_failed",
                "private refresh cleanup failed",
                recovery_id,
            ) from pending or cleanup_error
        if pending is not None:
            raise pending
        assert result is not None
        return result


def _validate_options(options: object) -> None:
    if (
        type(options) is not RefreshOptions
        or (options.backend is not None and type(options.backend) is not str)
        or (options.model is not None and type(options.model) is not str)
        or type(options.deep) is not bool
        or type(options.code_only) is not bool
    ):
        raise RefreshError(
            "invalid_refresh_options", "semantic refresh options are invalid"
        )
    if options.code_only:
        if options.backend is not None or options.model is not None or options.deep:
            raise RefreshError(
                "invalid_refresh_options", "code-only cannot select semantic options"
            )
        return
    if options.backend is None:
        raise RefreshError(
            "semantic_backend_required", "semantic extraction requires a backend"
        )
    if options.model is None:
        raise RefreshError(
            "semantic_model_required",
            "semantic extraction requires a public model identifier",
        )
    try:
        validate_public_model_identifier(options.model)
    except CompatibilityError as error:
        _raise_refresh_compatibility(error)


def _run_bound_operation(
    repo_root: Path,
    manifest: ProjectManifest,
    repository: RepositoryAccess,
    runner: CommandRunner,
    executable: object,
    command: RenderedCommand,
    environment: Mapping[str, str],
    timeout: float,
):
    assert_current_manifest_unchanged(
        repo_root, manifest, repository_access=repository
    )
    return run_graphify_operation(
        runner,
        executable,  # type: ignore[arg-type]
        command.argv,
        env=environment,
        timeout=timeout,
        operation=command.operation,
        before_exec=lambda: assert_current_manifest_unchanged(
            repo_root, manifest, repository_access=repository
        ),
    )


def _configuration_sha256(
    manifest: ProjectManifest, options: RefreshOptions
) -> str:
    document = {
        "manifest_schema_version": manifest.schema_version,
        "project_uid": None if manifest.project_uid is None else str(manifest.project_uid),
        "graphify_version": manifest.graphify_version,
        "track_html": manifest.track_html,
        "backend": options.backend,
        "model": options.model,
        "deep": options.deep,
        "code_only": options.code_only,
    }
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_current_projection(
    repo_root: Path,
    manifest: ProjectManifest,
    staged: StagedInput,
    *,
    repository_access: RepositoryAccess,
) -> None:
    current = inspect_projection(
        repo_root, manifest, repository_access=repository_access
    )
    if (
        current.source_digest != staged.source_digest
        or current.projection_digest != staged.projection_digest
        or current.files != staged.projection_files
        or current.reason_counts != staged.reason_counts
        or current.coverage_approvals != staged.coverage_approvals
    ):
        raise RefreshError(
            "projection_changed", "safe projection changed during refresh"
        )


def _current_owned_generation(
    manifest: ProjectManifest,
    staged: StagedInput,
    *,
    repository_access: RepositoryAccess,
) -> _InstalledGenerationIdentity | None:
    relative = manifest.output_dir.as_posix()
    try:
        info = os.stat(
            relative,
            dir_fd=repository_access.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArtifactValidationError("existing graph output is invalid")
    root = _repository_path(repository_access)
    owned = validate_owned_graph(
        root.joinpath(*manifest.output_dir.parts),
        manifest,
        expected_source_digest=staged.source_digest,
        expected_projection_digest=staged.projection_digest,
        repository_access=repository_access,
    )
    if owned.artifact_schema_version != 2:
        return None
    return _installed_identity(owned)


def _next_build_epoch(
    existing: _InstalledGenerationIdentity | None,
) -> int:
    if existing is None:
        return 1
    if (
        type(existing.build_epoch) is not int
        or existing.build_epoch < 1
        or existing.build_epoch >= _MAX_BUILD_EPOCH
    ):
        raise RefreshError(
            "build_epoch_exhausted", "graph ownership epoch is exhausted"
        )
    return existing.build_epoch + 1


def _revalidate_installed_generation(
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> _InstalledGenerationIdentity | None:
    try:
        root = _repository_path(repository_access)
        owned = validate_owned_graph(
            root.joinpath(*manifest.output_dir.parts),
            manifest,
            repository_access=repository_access,
        )
        if owned.artifact_schema_version != 2:
            return None
        return _installed_identity(owned)
    except ArtifactValidationError:
        return None


def _installed_identity(owned: ValidatedGraph) -> _InstalledGenerationIdentity:
    if (
        _DIGEST.fullmatch(owned.graph_digest) is None
        or _DIGEST.fullmatch(owned.generation_digest) is None
        or type(owned.build_epoch) is not int
        or not 1 <= owned.build_epoch <= _MAX_BUILD_EPOCH
    ):
        raise ArtifactValidationError("owned generation identity is invalid")
    return _InstalledGenerationIdentity(
        owned.graph_digest, owned.generation_digest, owned.build_epoch
    )


def _repository_path(repository: RepositoryAccess) -> Path:
    try:
        if hasattr(fcntl, "F_GETPATH"):
            raw = fcntl.fcntl(repository.descriptor, fcntl.F_GETPATH, b"\0" * 1024)
            path = Path(os.fsdecode(raw.split(b"\0", 1)[0]))
        else:
            path = Path(os.readlink(f"/proc/self/fd/{repository.descriptor}"))
        opened = os.fstat(repository.descriptor)
        named = path.stat(follow_symlinks=False)
    except OSError:
        raise ArtifactValidationError("repository descriptor is unavailable") from None
    if (
        stat.S_ISLNK(named.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        or repository.identity != (opened.st_dev, opened.st_ino)
    ):
        raise ArtifactValidationError("repository descriptor is unavailable")
    return path


def _stale_result(
    staged: StagedInput,
    validated: ValidatedGraph,
    installed: _InstalledGenerationIdentity | None,
    limitations: set[str],
    *,
    recovery_id: str | None = None,
) -> RefreshResult:
    return RefreshResult(
        status="promoted_but_stale",
        source_digest=staged.source_digest,
        projection_digest=staged.projection_digest or "",
        graph_digest=None if installed is None else installed.graph_digest,
        generation_digest=None if installed is None else installed.generation_digest,
        build_epoch=None if installed is None else installed.build_epoch,
        core_status="error",
        trust="navigation",
        limitations=tuple(
            sorted(set(validated.impact_limitations) | limitations)
        ),
        recovery_id=recovery_id,
    )


__all__ = [
    "CLUSTER_TIMEOUT_SECONDS",
    "DIAGNOSE_TIMEOUT_SECONDS",
    "EXTRACT_TIMEOUT_SECONDS",
    "REAL_REFRESH_FS",
    "RefreshError",
    "RefreshFileSystem",
    "RefreshOptions",
    "RefreshResult",
    "refresh_project",
]
