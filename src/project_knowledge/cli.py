"""Deterministic, least-authority CLI for the project knowledge workflow."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import importlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from types import MappingProxyType
from typing import Callable, Sequence
from uuid import UUID

from .adapter import AdapterError, adapt_candidate
from .agent_install import AgentInstallError
from .artifacts import ArtifactValidationError, promote_graph, validate_candidate
from .atlas import (
    AtlasOwnershipError,
    generated_root,
    load_prepared_atlas_candidate,
    prepare_atlas_candidate,
    promote_atlas,
    validate_atlas_candidate,
)
from .compatibility import (
    CompatibilityError,
    bind_semantic_backend_credential,
    render_graphify_argv,
    resolve_graphify_compatibility,
    validate_public_model_identifier,
)
from .bundles import BundleError
from .cli_distribution import (
    add_distribution_commands,
    dispatch_distribution,
    is_distribution_command,
)
from .coverage import (
    CoverageError,
    apply_coverage_approval,
    preview_coverage_approval,
)
from .doctor import doctor_project
from .graphify import (
    GraphifyError,
    SubprocessCommandRunner,
    minimal_environment,
    probe_graphify,
    resolve_graphify_executable,
)
from .fleet import FleetConfigError
from .github_artifacts import GithubArtifactError
from .health import assess_health, inspect_project_state, safe_input_snapshot
from .lifecycle import RefreshError, RefreshOptions, refresh_project
from .locking import (
    RepositoryAccess,
    TransactionLockError,
    capture_lifecycle_repository,
    open_repository_access,
    repository_lifecycle_lock,
)
from .manifest import (
    ManifestError,
    apply_init,
    apply_manifest_migration,
    assert_current_manifest_unchanged,
    inspect_init_journal,
    load_manifest,
    preview_init,
    preview_manifest_migration,
    require_current_manifest,
)
from .models import ProjectManifest
from .privacy import PrivacyError
from .queries import (
    DOCUMENTED_QUERY_ERROR_CODES,
    QueryError,
    affected_nodes,
    explain_node,
    open_query_snapshot,
    query_nodes,
    shortest_path,
)
from .receipt import ReceiptError, load_staging_receipt, verify_staged_input
from .registry import (
    DOCUMENTED_REGISTRY_ERROR_CODES,
    RegistryError,
    registry_status as inspect_registry_status,
    registry_sync as synchronize_registry,
)
from .secrets_scan import (
    SecretExceptionError,
    load_secret_exceptions,
    scan_repository,
)
from .staging import (
    StagedInput,
    StagingError,
    inspect_projection,
    stage_input,
    stage_input_with_receipt,
)


CLI_SCHEMA_VERSION = 1
SCHEMA_VERSION = CLI_SCHEMA_VERSION
_RECOVERY_ID = re.compile(r"[0-9a-f]{32}\Z")
_PUBLIC_RECOVERY_ID = re.compile(r"[0-9a-f]{16,128}\Z")
_MAX_CANDIDATE_GRAPH_BYTES = 128 * 1024 * 1024
_GENERIC_BACKEND_TOKEN = "ATLASWEAVER_BACKEND_TOKEN"
_SECRET_ENVIRONMENT_NAMES = frozenset(
    {
        _GENERIC_BACKEND_TOKEN,
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "MOONSHOT_API_KEY",
        "OLLAMA_API_KEY",
    }
)

SELF_LOADING_COMMANDS = frozenset({"init", "manifest-migrate", "doctor"})
LOCKED_LEGACY_COMMANDS = frozenset(
    {
        "stage",
        "adapt",
        "validate",
        "promote",
        "atlas-prepare",
        "atlas-promote",
    }
)

DOCUMENTED_REFRESH_ERROR_CODES = frozenset(
    {
        "init_recovery_required",
        "manifest_migration_required",
        "projection_failed",
        "projection_changed",
        "adaptation_failed",
        "refresh_verification_failed",
        "cleanup_failed",
        "build_epoch_exhausted",
        "invalid_refresh_options",
        "semantic_backend_required",
        "semantic_model_required",
    }
)

MANIFEST_ERROR_CODES = {
    "invalid": ("invalid_manifest", "project manifest is invalid"),
    "changed": ("manifest_changed", "project manifest changed"),
}

_ADMISSION_PUBLIC_ERRORS = {
    "init_recovery_required": (1, "configuration recovery is required"),
    "manifest_migration_required": (1, "manifest migration is required"),
}
BUNDLE_PUBLIC_ERRORS = MappingProxyType(
    {
        **_ADMISSION_PUBLIC_ERRORS,
        "bundle_invalid": (1, "bundle validation failed"),
        "bundle_too_large": (1, "bundle exceeds its size limit"),
        "bundle_changed_during_capture": (1, "bundle changed during capture"),
        "bundle_output_exists": (1, "bundle output already exists"),
        "bundle_output_in_source": (1, "bundle output overlaps its source"),
        "bundle_cleanup_failed": (1, "private bundle cleanup failed"),
        "bundle_output_recovery_required": (
            1,
            "bundle output recovery is required",
        ),
        "bundle_source_drift": (1, "project source changed"),
        "bundle_generation_changed": (1, "graph generation changed"),
        "bundle_identity_mismatch": (1, "bundle identity does not match"),
        "bundle_stale": (1, "bundle is stale"),
        "bundle_pull_required": (1, "remote bundle requires verified pull"),
        "bundle_unattested": (1, "bundle is not authorized for install"),
    }
)
GITHUB_PUBLIC_ERRORS = MappingProxyType(
    {
        **_ADMISSION_PUBLIC_ERRORS,
        "github_token_required": (1, "GitHub credential is required"),
        "github_resolution_failed": (1, "GitHub artifact resolution failed"),
        "github_redirect_invalid": (1, "GitHub artifact redirect is invalid"),
        "github_cleanup_failed": (1, "private GitHub cleanup failed"),
        "attestation_failed": (1, "artifact attestation verification failed"),
        "attestation_ambiguous": (1, "artifact attestation is ambiguous"),
    }
)
AGENT_PUBLIC_ERRORS = MappingProxyType(
    {
        "agent_destination_unmanaged": (1, "agent destination is unmanaged"),
        "agent_destination_modified": (
            1,
            "managed agent destination was modified",
        ),
        "agent_install_failed": (1, "agent resource operation failed"),
    }
)
FLEET_PUBLIC_ERRORS = MappingProxyType(
    {
        **_ADMISSION_PUBLIC_ERRORS,
        "fleet_invalid": (1, "fleet request is invalid"),
        "fleet_document_too_large": (
            1,
            "fleet document exceeds its size limit",
        ),
        "fleet_manifest_too_large": (
            1,
            "project manifest exceeds its size limit",
        ),
        "fleet_repository_changed": (
            1,
            "fleet repository identity changed",
        ),
        "fleet_manifest_changed": (1, "fleet project contract changed"),
        "fleet_git_changed": (1, "fleet Git identity changed"),
        "fleet_worktree_alias": (
            1,
            "fleet repositories alias one Git worktree",
        ),
        "fleet_result_invalid": (1, "fleet project result is invalid"),
        "fleet_partial_failure": (1, "one or more fleet operations failed"),
    }
)
REGISTRY_PUBLIC_ERRORS = MappingProxyType(
    {
        "registry_disabled": (1, "registry is disabled"),
        "registry_busy": (1, "registry is busy"),
        "registry_unmanaged_state": (1, "registry state is unmanaged"),
        "registry_recovery_required": (1, "registry recovery is required"),
        "registry_source_changed": (1, "project source changed"),
        "manifest_migration_required": (1, "manifest migration is required"),
        "registry_snapshot_missing": (1, "registry snapshot is missing"),
        "registry_snapshot_stale": (1, "registry snapshot is stale"),
        "registry_snapshot_mismatch": (
            1,
            "registry snapshot identity does not match",
        ),
        "registry_snapshot_too_large": (
            1,
            "registry snapshot exceeds its size limit",
        ),
        "registry_snapshot_busy": (1, "registry snapshot is busy"),
    }
)


@dataclass(frozen=True)
class CliResponse:
    document: dict[str, object]
    exit_code: int = 0


@dataclass
class CliFailure(Exception):
    code: str
    message: str
    exit_code: int = 1
    recovery_id: str | None = None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="project-knowledge")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {importlib.metadata.version('atlasweaver')}",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for add_command in (
        add_init,
        add_manifest_migrate,
        add_doctor,
        add_refresh,
        add_query_commands,
        add_coverage,
        add_registry_status,
        add_registry_sync,
        add_detect,
        add_preflight,
        add_stage,
        add_adapt,
        add_scan_secrets,
        add_validate,
        add_promote,
        add_atlas_prepare,
        add_atlas_promote,
        add_health,
    ):
        add_command(sub)
    add_distribution_commands(sub)
    return parser


def _command(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
) -> argparse.ArgumentParser:
    parser = sub.add_parser(name)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def add_detect(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    _command(sub, "detect")


def add_init(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = _command(sub, "init")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--project-uid")
    parser.add_argument("--include-root", action="append", default=[])
    parser.add_argument("--apply", action="store_true")


def add_manifest_migrate(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = _command(sub, "manifest-migrate")
    parser.add_argument("--project-uid")
    parser.add_argument("--apply", action="store_true")


def add_doctor(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    _command(sub, "doctor")


def add_refresh(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = _command(sub, "refresh")
    parser.add_argument("--backend")
    parser.add_argument("--model")
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--code-only", action="store_true")


def add_query_commands(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    query = _command(sub, "query")
    query.add_argument("term")
    query.add_argument("--limit", type=int, default=20)
    path = _command(sub, "path")
    path.add_argument("source")
    path.add_argument("target")
    path.add_argument("--max-depth", type=int, default=32)
    explain = _command(sub, "explain")
    explain.add_argument("node")
    explain.add_argument("--depth", type=int, default=1)
    affected = _command(sub, "affected")
    affected.add_argument("node")
    affected.add_argument("--depth", type=int, default=2)
    affected.add_argument("--relation", action="append", default=[])


def add_coverage(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = _command(sub, "coverage")
    nested = parser.add_subparsers(dest="coverage_command", required=True)
    approve = nested.add_parser("approve")
    approve.add_argument("--file", required=True)
    approve.add_argument("--reason", required=True)
    approve.add_argument("--rationale", required=True)
    approve.add_argument("--apply", action="store_true")
    approve.add_argument(
        "--json", action="store_true", dest="as_json", default=argparse.SUPPRESS
    )


def add_registry_status(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    _command(sub, "registry-status")


def add_registry_sync(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    _command(sub, "registry-sync")


def add_preflight(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = _command(sub, "preflight")
    parser.add_argument("--backend")
    parser.add_argument("--model")
    parser.add_argument("--deep", action="store_true")


def add_stage(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = _command(sub, "stage")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)


def add_adapt(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = _command(sub, "adapt")
    parser.add_argument("--staged-input", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--raw-candidate", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)


def add_scan_secrets(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    _command(sub, "scan-secrets")


def _add_graph_candidate_command(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], name: str
) -> None:
    parser = _command(sub, name)
    parser.add_argument("--candidate", type=Path, required=True)


def add_validate(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    _add_graph_candidate_command(sub, "validate")


def add_promote(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    _add_graph_candidate_command(sub, "promote")


def add_atlas_prepare(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = _command(sub, "atlas-prepare")
    parser.add_argument("--candidate", type=Path, required=True)


def add_atlas_promote(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = _command(sub, "atlas-promote")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--atlas", type=Path, required=True)


def add_health(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = _command(sub, "health")
    parser.add_argument("--atlas", type=Path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if is_distribution_command(arguments):
            distributed = dispatch_distribution(
                arguments, read_secret=_read_secret_environment
            )
            response = CliResponse(distributed.document, distributed.exit_code)
        else:
            repo = _real_repo(arguments.repo)
        if is_distribution_command(arguments):
            pass
        elif arguments.command == "init":
            response = _init(arguments, repo)
        elif arguments.command == "manifest-migrate":
            response = _manifest_migrate(arguments, repo)
        elif arguments.command == "doctor":
            response = CliResponse(doctor_project(repo).to_dict())
        else:
            with open_repository_access(repo) as admission_repository:
                if (
                    inspect_init_journal(
                        repo, repository_access=admission_repository
                    )
                    != "none"
                ):
                    raise CliFailure(
                        "init_recovery_required",
                        "configuration recovery is required",
                    )
                manifest = _manifest(
                    repo, repository_access=admission_repository
                )
                expected_identity = admission_repository.identity
                if arguments.command in LOCKED_LEGACY_COMMANDS:
                    with repository_lifecycle_lock(
                        repo,
                        expected_repository_identity=expected_identity,
                    ), capture_lifecycle_repository(repo) as repository:
                        if (
                            inspect_init_journal(
                                repo, repository_access=repository
                            )
                            != "none"
                        ):
                            raise CliFailure(
                                "init_recovery_required",
                                "configuration recovery is required",
                            )
                        manifest = require_current_manifest(
                            repo, manifest, repository_access=repository
                        )
                        response = CliResponse(
                            _dispatch(
                                arguments,
                                repo,
                                manifest,
                                repository_access=repository,
                            )
                        )
                else:
                    response = _dispatch_response(
                        arguments,
                        repo,
                        manifest,
                        repository_access=admission_repository,
                        expected_repository_identity=expected_identity,
                    )
    except CliFailure as error:
        return _emit_error(arguments, error)
    except Exception as error:
        failure = _map_domain_error(error)
        if failure is None:
            raise
        return _emit_error(arguments, failure)
    _emit(response.document, as_json=arguments.as_json)
    return response.exit_code


def _dispatch(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> dict[str, object]:
    handlers: dict[str, Callable[..., dict[str, object]]] = {
        "detect": _detect,
        "preflight": _preflight,
        "stage": _stage,
        "adapt": _adapt,
        "scan-secrets": _scan_secrets,
        "validate": _validate,
        "promote": _promote,
        "atlas-prepare": _atlas_prepare,
        "atlas-promote": _atlas_promote,
        "health": _health,
    }
    return handlers[arguments.command](
        arguments, repo, manifest, repository_access=repository_access
    )


def _dispatch_response(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
    expected_repository_identity: tuple[int, int],
) -> CliResponse:
    handlers: dict[str, Callable[..., CliResponse]] = {
        "refresh": _refresh,
        "query": _query,
        "path": _query,
        "explain": _query,
        "affected": _query,
        "coverage": _coverage,
        "registry-status": _registry_status,
        "registry-sync": _registry_sync,
    }
    handler = handlers.get(arguments.command)
    if handler is not None:
        return handler(
            arguments,
            repo,
            manifest,
            repository_access=repository_access,
            expected_repository_identity=expected_repository_identity,
        )
    return CliResponse(
        _dispatch(
            arguments,
            repo,
            manifest,
            repository_access=repository_access,
        )
    )


def _init(arguments: argparse.Namespace, repo: Path) -> CliResponse:
    if inspect_init_journal(repo) != "none":
        raise CliFailure(
            "init_recovery_required", "configuration recovery is required"
        )
    preview = preview_init(
        repo,
        arguments.project_id,
        project_uid=_optional_uuid(arguments.project_uid),
        include_roots=tuple(PurePosixPath(value) for value in arguments.include_root),
    )
    if not arguments.apply:
        return CliResponse(_configuration_preview("init", preview, include_ignore=True))
    status = apply_init(repo, preview)
    if status == "init_recovery_required":
        raise CliFailure(
            "init_recovery_required", "configuration recovery is required"
        )
    if status == "init_conflict":
        raise CliFailure("init_conflict", "project initialization conflicted")
    project_uid = preview.project_uid
    if status == "initialized":
        with open_repository_access(
            repo, expected_repository_identity=preview.repository_identity
        ) as repository:
            project_uid = _manifest(repo, repository_access=repository).project_uid
    return CliResponse(
        _success(
            "init",
            status,
            project_id=preview.project_id,
            project_uid=None if project_uid is None else str(project_uid),
        )
    )


def _manifest_migrate(arguments: argparse.Namespace, repo: Path) -> CliResponse:
    if inspect_init_journal(repo) != "none":
        raise CliFailure(
            "init_recovery_required", "configuration recovery is required"
        )
    preview = preview_manifest_migration(
        repo, project_uid=_optional_uuid(arguments.project_uid)
    )
    if not arguments.apply:
        return CliResponse(
            _configuration_preview(
                "manifest-migrate", preview, include_ignore=False
            )
        )
    status = apply_manifest_migration(repo, preview)
    if status == "init_recovery_required":
        raise CliFailure(
            "init_recovery_required", "configuration recovery is required"
        )
    if status == "init_conflict":
        raise CliFailure("init_conflict", "manifest migration conflicted")
    project_uid = preview.project_uid
    if status in {"migrated", "already_current"}:
        with open_repository_access(
            repo, expected_repository_identity=preview.repository_identity
        ) as repository:
            project_uid = _manifest(repo, repository_access=repository).project_uid
    return CliResponse(
        _success(
            "manifest-migrate",
            status,
            project_id=preview.project_id,
            project_uid=None if project_uid is None else str(project_uid),
        )
    )


def _configuration_preview(
    command: str, preview: object, *, include_ignore: bool
) -> dict[str, object]:
    document = _success(
        command,
        preview.status,
        project_id=preview.project_id,
        project_uid=(
            "<generated-on-apply>"
            if preview.project_uid is None
            else str(preview.project_uid)
        ),
        candidate_roots=[path.as_posix() for path in preview.candidate_roots],
        include_roots=[
            path.as_posix() for path in preview.requested_include_roots
        ],
        manifest=preview.manifest_payload.decode("utf-8"),
    )
    if include_ignore:
        document["graphifyignore"] = preview.ignore_payload.decode("utf-8")
    return document


def _optional_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    if type(value) is not str:
        raise CliFailure("invalid_manifest", "project UUID is invalid")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise CliFailure("invalid_manifest", "project UUID is invalid") from None
    if parsed.version != 4 or str(parsed) != value:
        raise CliFailure("invalid_manifest", "project UUID is invalid")
    return parsed


def _refresh(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
    expected_repository_identity: tuple[int, int],
) -> CliResponse:
    del repository_access
    options = RefreshOptions(
        backend=arguments.backend,
        model=arguments.model,
        deep=arguments.deep,
        code_only=arguments.code_only,
    )
    _validate_refresh_cli_options(options)
    call_options: dict[str, object] = {
        "expected_repository_identity": expected_repository_identity
    }
    if options.backend is not None:
        ambient = dict(os.environ)
        ambient.pop(_GENERIC_BACKEND_TOKEN, None)
        token = _read_secret_environment(_GENERIC_BACKEND_TOKEN)
        if token:
            contract = resolve_graphify_compatibility(manifest.graphify_version)
            ambient = {
                **minimal_environment(),
                **bind_semantic_backend_credential(
                    contract, options.backend, token
                ),
            }
        call_options["ambient"] = ambient
    result = refresh_project(repo, manifest, options, **call_options)
    document = _success(
        "refresh",
        result.status,
        project_id=manifest.project_id,
        source_digest=result.source_digest,
        projection_digest=result.projection_digest,
        graph_digest=result.graph_digest,
        generation_digest=result.generation_digest,
        build_epoch=result.build_epoch,
        core_status=result.core_status,
        trust=result.trust,
        limitations=list(result.limitations),
    )
    if result.recovery_id is not None:
        if (
            "cleanup_failed" not in result.limitations
            or _RECOVERY_ID.fullmatch(result.recovery_id) is None
        ):
            raise CliFailure("refresh_failed", "refresh result is invalid")
        document["recovery_id"] = result.recovery_id
    return CliResponse(document, 3 if result.status == "promoted_but_stale" else 0)


def _validate_refresh_cli_options(options: RefreshOptions) -> None:
    if options.code_only:
        if options.backend is not None or options.model is not None or options.deep:
            raise CliFailure(
                "invalid_refresh_options", "semantic refresh options are invalid"
            )
        return
    if options.backend is None:
        if options.model is not None or options.deep:
            raise CliFailure(
                "semantic_backend_required", "semantic backend is required"
            )
        raise CliFailure(
            "semantic_backend_required", "semantic backend is required"
        )
    if options.model is None:
        raise CliFailure("semantic_model_required", "semantic model is required")
    try:
        validate_public_model_identifier(options.model)
    except CompatibilityError:
        raise CliFailure(
            "semantic_model_required", "semantic model is required"
        ) from None


def _read_secret_environment(name: str) -> str | None:
    if name not in _SECRET_ENVIRONMENT_NAMES:
        raise ValueError("secret environment name is not admitted")
    value = os.environ.get(name)
    return value if isinstance(value, str) and value else None


def _query(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
    expected_repository_identity: tuple[int, int],
) -> CliResponse:
    del repository_access
    pending: QueryError | None = None
    envelope = None
    with open_query_snapshot(
        repo,
        manifest,
        expected_repository_identity=expected_repository_identity,
    ) as snapshot:
        try:
            if arguments.command == "query":
                envelope = query_nodes(
                    snapshot, arguments.term, limit=arguments.limit
                )
            elif arguments.command == "path":
                envelope = shortest_path(
                    snapshot,
                    arguments.source,
                    arguments.target,
                    max_depth=arguments.max_depth,
                )
            elif arguments.command == "explain":
                envelope = explain_node(
                    snapshot, arguments.node, depth=arguments.depth
                )
            else:
                envelope = affected_nodes(
                    snapshot,
                    arguments.node,
                    depth=arguments.depth,
                    relations=tuple(arguments.relation),
                )
        except QueryError as error:
            pending = error
    if pending is not None:
        raise pending
    assert envelope is not None
    return CliResponse(envelope.to_dict())


def _coverage(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
    expected_repository_identity: tuple[int, int],
) -> CliResponse:
    del expected_repository_identity
    preview = preview_coverage_approval(
        repo,
        manifest,
        PurePosixPath(arguments.file),
        arguments.reason,
        arguments.rationale,
        repository_access=repository_access,
    )
    if arguments.apply:
        status = apply_coverage_approval(repo, preview)
    else:
        status = "preview"
    return CliResponse(
        _success(
            "coverage",
            status,
            project_id=manifest.project_id,
            file=preview.approval.path.as_posix(),
            reason=preview.approval.reason_code,
            rationale=preview.approval.rationale,
            content_sha256=preview.approval.content_sha256,
            content=preview.payload.decode("utf-8"),
        )
    )


def _registry_status(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
    expected_repository_identity: tuple[int, int],
) -> CliResponse:
    del arguments, repository_access
    result = inspect_registry_status(
        repo,
        manifest,
        expected_repository_identity=expected_repository_identity,
    )
    return CliResponse(
        _success(
            "registry-status",
            result.status,
            project_id=manifest.project_id,
            issues=list(result.issues),
        )
    )


def _registry_sync(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
    expected_repository_identity: tuple[int, int],
) -> CliResponse:
    del arguments, repository_access
    result = synchronize_registry(
        repo,
        manifest,
        expected_repository_identity=expected_repository_identity,
    )
    return CliResponse(
        _success(
            "registry-sync",
            result.status,
            project_id=manifest.project_id,
            key=result.key,
            generation=result.generation,
            graph_digest=result.graph_digest,
            generation_digest=result.generation_digest,
            snapshot_digest=result.snapshot_digest,
        )
    )


def _detect(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> dict[str, object]:
    del arguments
    health = assess_health(
        inspect_project_state(
            repo, manifest, repository_access=repository_access
        )
    )
    return _success(
        "detect",
        "detected",
        project_id=manifest.project_id,
        graph_status=health.status,
        source_matches=health.source_matches,
    )


def _preflight(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> dict[str, object]:
    _validate_preflight_cli_options(
        arguments.backend, arguments.model, arguments.deep
    )
    contract = resolve_graphify_compatibility(manifest.graphify_version)
    credential_bound = False
    if arguments.backend is not None:
        token = _read_secret_environment(_GENERIC_BACKEND_TOKEN)
        bound = bind_semantic_backend_credential(
            contract, arguments.backend, token
        )
        credential_bound = bool(bound)
        render_graphify_argv(
            contract,
            "extract",
            binary=Path("/graphify"),
            source=Path("/staged-source"),
            output=Path("/raw-output"),
            backend=arguments.backend,
            model=arguments.model,
            deep=arguments.deep,
        )
    capabilities = probe_graphify(
        resolve_graphify_executable(),
        contract,
        SubprocessCommandRunner(),
        before_exec=lambda: assert_current_manifest_unchanged(
            repo, manifest, repository_access=repository_access
        ),
    )
    return _success(
        "preflight",
        "ready",
        graphify_version=capabilities.version,
        backend=arguments.backend,
        model=arguments.model,
        deep=arguments.deep,
        credential_bound=credential_bound,
    )


def _validate_preflight_cli_options(
    backend: object, model: object, deep: object
) -> None:
    if backend is None:
        if model is not None or deep is True:
            raise CliFailure(
                "semantic_backend_required", "semantic backend is required"
            )
        return
    if type(backend) is not str:
        raise CliFailure(
            "semantic_backend_required", "semantic backend is required"
        )
    if model is None:
        raise CliFailure("semantic_model_required", "semantic model is required")
    try:
        validate_public_model_identifier(model)
    except CompatibilityError:
        raise CliFailure(
            "semantic_model_required", "semantic model is required"
        ) from None


def _stage(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> dict[str, object]:
    staged = stage_input_with_receipt(
        repo,
        manifest,
        arguments.destination,
        arguments.receipt,
        repository_access=repository_access,
    )
    return _success(
        "stage",
        "staged",
        project_id=manifest.project_id,
        source_digest=staged.source_digest,
        file_count=len(staged.files),
    )


def _adapt(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> dict[str, object]:
    receipt = load_staging_receipt(arguments.receipt, manifest)
    staged = verify_staged_input(arguments.staged_input, receipt)
    _require_current_input(
        repo, manifest, staged, repository_access=repository_access
    )
    adapted = adapt_candidate(
        arguments.raw_candidate,
        arguments.destination,
        staged,
        manifest,
        post_write_check=lambda: _require_current_input(
            repo, manifest, staged, repository_access=repository_access
        ),
    )
    return _success(
        "adapt",
        "adapted",
        project_id=manifest.project_id,
        source_digest=adapted.source_digest,
        node_count=adapted.node_count,
        edge_count=adapted.edge_count,
        skipped_count=adapted.skipped_count,
    )


def _scan_secrets(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> dict[str, object]:
    del arguments
    source_root = _repository_path(repository_access)
    findings = scan_repository(source_root, manifest)
    accepted = load_secret_exceptions(
        repo,
        findings,
        manifest=manifest,
        repository_descriptor=repository_access.descriptor,
    )
    unaccepted = [
        finding for finding in findings if finding.fingerprint not in accepted
    ]
    return _success(
        "scan-secrets",
        "scanned",
        finding_count=len(unaccepted),
        excepted_count=len(findings) - len(unaccepted),
        findings=[
            {
                "detector": finding.detector,
                "path": finding.path.as_posix(),
                "line": finding.line,
                "fingerprint": finding.fingerprint,
            }
            for finding in unaccepted
        ],
    )


def _validate(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> dict[str, object]:
    staged = _current_input(
        repo, manifest, repository_access=repository_access
    )
    with tempfile.TemporaryDirectory(prefix="project-knowledge-validate-") as temporary:
        clone = _clone_graph_candidate(arguments.candidate, Path(temporary) / "candidate")
        if inspect_candidate_schema(clone) == 2:
            raise CliFailure(
                "evidence_anchor_required",
                "schema-2 validation requires a trusted evidence anchor",
            )
        validated = validate_candidate(clone, staged, manifest)
        _require_current_input(
            repo, manifest, staged, repository_access=repository_access
        )
        return _success(
            "validate",
            "valid",
            project_id=manifest.project_id,
            graph_digest=validated.graph_digest,
            node_count=validated.node_count,
            edge_count=validated.edge_count,
        )


def _promote(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> dict[str, object]:
    staged = _current_input(
        repo, manifest, repository_access=repository_access
    )
    with tempfile.TemporaryDirectory(prefix="project-knowledge-promote-") as temporary:
        clone = _clone_graph_candidate(arguments.candidate, Path(temporary) / "candidate")
        if inspect_candidate_schema(clone) == 2:
            raise CliFailure(
                "evidence_anchor_required",
                "schema-2 validation requires a trusted evidence anchor",
            )
        validated = validate_candidate(clone, staged, manifest)
        _require_current_input(
            repo, manifest, staged, repository_access=repository_access
        )
        promoted = promote_graph(
            validated, repo, repository_access=repository_access
        )
        return _success(
            "promote",
            "promoted",
            project_id=manifest.project_id,
            graph_digest=promoted.digest,
        )


def _atlas_prepare(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> dict[str, object]:
    del repo, repository_access
    prepared = prepare_atlas_candidate(
        arguments.candidate, manifest, manifest.graphify_version
    )
    return _success(
        "atlas-prepare",
        "prepared",
        project_id=manifest.project_id,
        file_count=len(prepared.files),
        generator_version=prepared.generator_version,
    )


def _atlas_promote(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> dict[str, object]:
    del repo, repository_access
    prepared = load_prepared_atlas_candidate(arguments.candidate, manifest)
    validated = validate_atlas_candidate(prepared, manifest)
    promoted = promote_atlas(validated, arguments.atlas, manifest)
    return _success(
        "atlas-promote",
        "promoted",
        project_id=manifest.project_id,
        atlas_digest=promoted.digest,
        changed=promoted.changed,
    )


def _health(
    arguments: argparse.Namespace,
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> dict[str, object]:
    atlas_available = (
        None
        if arguments.atlas is None
        else _atlas_available(arguments.atlas, manifest)
    )
    state = inspect_project_state(
        repo,
        manifest,
        atlas_available=atlas_available,
        repository_access=repository_access,
    )
    health = assess_health(state)
    return {"schema_version": SCHEMA_VERSION, "command": "health", **health.to_dict()}


def _current_input(
    repo: Path,
    manifest: ProjectManifest,
    *,
    repository_access: RepositoryAccess,
) -> StagedInput:
    if manifest.schema_version == 2:
        projection = inspect_projection(
            repo, manifest, repository_access=repository_access
        )
        return StagedInput(
            root=repo,
            source_digest=projection.source_digest,
            files=tuple(item.path for item in projection.files),
            projection_digest=projection.projection_digest,
            reason_counts=projection.reason_counts,
            coverage_approvals=projection.coverage_approvals,
            projection_files=projection.files,
        )
    snapshot = safe_input_snapshot(_repository_path(repository_access), manifest)
    return StagedInput(
        root=repo,
        source_digest=snapshot.source_digest,
        files=snapshot.files,
    )


def _require_current_input(
    repo: Path,
    manifest: ProjectManifest,
    expected: StagedInput,
    *,
    repository_access: RepositoryAccess,
) -> None:
    current = _current_input(
        repo, manifest, repository_access=repository_access
    )
    if (
        current.source_digest != expected.source_digest
        or current.files != expected.files
        or current.projection_digest != expected.projection_digest
    ):
        raise StagingError("safe source changed during candidate validation")


def _clone_graph_candidate(source: Path, destination: Path) -> Path:
    """Capture root-level regular candidate files without following links."""
    try:
        root = _real_directory(source, "graph candidate")
    except StagingError as error:
        raise ArtifactValidationError("graph candidate must be a real directory") from error
    source_fd = _open_directory(root)
    destination.mkdir(mode=0o700, exist_ok=False)
    try:
        with os.scandir(os.dup(source_fd)) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            before = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise ArtifactValidationError(
                    "graph candidate entries must be regular files"
                )
            descriptor = os.open(
                name, os.O_RDONLY | _nofollow_flag(), dir_fd=source_fd
            )
            try:
                opened = os.fstat(descriptor)
                if _identity(before) != _identity(opened):
                    raise ArtifactValidationError("graph candidate changed during capture")
                chunks: list[bytes] = []
                while chunk := os.read(descriptor, 1024 * 1024):
                    chunks.append(chunk)
                after = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
                if _identity(opened) != _identity(after):
                    raise ArtifactValidationError("graph candidate changed during capture")
            finally:
                os.close(descriptor)
            target = destination / name
            with target.open("xb") as output:
                os.chmod(target, 0o600)
                output.write(b"".join(chunks))
        return destination.resolve()
    finally:
        os.close(source_fd)


def inspect_candidate_schema(candidate: Path) -> int:
    """Descriptor-read only the bounded candidate schema discriminator."""
    root_fd = -1
    descriptor = -1
    try:
        root_fd = os.open(candidate.absolute(), _directory_flags())
        named = os.stat("graph.json", dir_fd=root_fd, follow_symlinks=False)
        descriptor = os.open(
            "graph.json", os.O_RDONLY | _nofollow_flag(), dir_fd=root_fd
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _identity(named) != _identity(opened)
            or opened.st_size > _MAX_CANDIDATE_GRAPH_BYTES
        ):
            raise ArtifactValidationError("candidate graph schema is unavailable")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(1024 * 1024, _MAX_CANDIDATE_GRAPH_BYTES + 1 - total)):
            total += len(chunk)
            if total > _MAX_CANDIDATE_GRAPH_BYTES:
                raise ArtifactValidationError("candidate graph schema is unavailable")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat("graph.json", dir_fd=root_fd, follow_symlinks=False)
        if _identity(opened) != _identity(after) or _identity(after) != _identity(current):
            raise ArtifactValidationError("candidate graph changed during schema inspection")
    except ArtifactValidationError:
        raise
    except OSError:
        raise ArtifactValidationError("candidate graph schema is unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if root_fd >= 0:
            os.close(root_fd)
    try:
        document = json.loads(b"".join(chunks), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise ArtifactValidationError("candidate graph schema is invalid") from None
    if type(document) is not dict:
        raise ArtifactValidationError("candidate graph schema is invalid")
    schema = document.get("artifact_schema_version", 1)
    if type(schema) is not int or schema not in {1, 2}:
        raise ArtifactValidationError("candidate graph schema is invalid")
    return schema


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _atlas_available(atlas: Path | None, manifest: ProjectManifest) -> bool:
    if atlas is None:
        return False
    try:
        root = _real_directory(atlas, "atlas")
        generated = generated_root(root, manifest)
        _real_directory(generated, "generated atlas namespace")
        load_prepared_atlas_candidate(generated, manifest)
    except (OSError, AtlasOwnershipError, CliFailure, StagingError):
        return False
    return True


def _registry_matches(repo: Path, manifest: ProjectManifest) -> bool:
    try:
        ownership = json.loads((repo / "graphify-out/.project-knowledge-ownership.json").read_text())
        registry = Path(os.environ.get("HOME", "")) / ".graphify/global-manifest.json"
        document = json.loads(registry.read_text())
        entry = document["repos"][manifest.project_id]
        return document.get("version") == 1 and entry.get("source_hash") == str(ownership["graph_digest"])[:16]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _manifest(
    repo: Path, *, repository_access: RepositoryAccess | None = None
) -> ProjectManifest:
    return load_manifest(
        repo / ".graphify-project.yaml",
        repo,
        repository_access=repository_access,
    )


def _repository_path(repository: RepositoryAccess) -> Path:
    try:
        if hasattr(fcntl, "F_GETPATH"):
            raw = fcntl.fcntl(
                repository.descriptor, fcntl.F_GETPATH, b"\0" * 1024
            )
            path = Path(os.fsdecode(raw.split(b"\0", 1)[0]))
        else:
            path = Path(os.readlink(f"/proc/self/fd/{repository.descriptor}"))
        opened = os.fstat(repository.descriptor)
        named = path.stat(follow_symlinks=False)
    except OSError:
        raise TransactionLockError(
            "repository access is unavailable", kind="authority"
        ) from None
    if (
        stat.S_ISLNK(named.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        or repository.identity != (opened.st_dev, opened.st_ino)
    ):
        raise TransactionLockError(
            "repository access is unavailable", kind="authority"
        )
    return path


def _real_repo(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError as error:
        raise CliFailure("invalid_repo", "repository must be a real directory") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CliFailure("invalid_repo", "repository must be a real directory")
    return path.absolute()


def _real_directory(path: Path, description: str) -> Path:
    absolute = path.absolute()
    try:
        info = absolute.lstat()
    except OSError as error:
        raise StagingError(f"{description} is unavailable") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise StagingError(f"{description} must be a real directory")
    descriptor = _open_directory(absolute)
    os.close(descriptor)
    return absolute


def _open_directory(path: Path) -> int:
    return os.open(path, _directory_flags())


def _directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    except AttributeError as error:
        raise StagingError("safe directory traversal is unavailable") from error


def _nofollow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as error:
        raise StagingError("safe file traversal is unavailable") from error


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev


def _success(command: str, status: str, **fields: object) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": status,
        **fields,
    }


def _emit(document: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(document, sort_keys=True, separators=(",", ":")))
        return
    command = str(document["command"])
    status = document.get("status")
    print(command if status is None else f"{command}: {status}")
    for key in sorted(document):
        if key not in {"schema_version", "command", "status"}:
            value = document[key]
            if isinstance(value, (dict, list, tuple)) or value is None or type(value) is bool:
                value = json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            print(f"{key}: {value}")


def _emit_error(arguments: argparse.Namespace, error: CliFailure) -> int:
    command = _command_label(arguments)
    document = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": "error",
        "error": {"code": error.code, "message": error.message},
    }
    if error.recovery_id is not None:
        error_document = document["error"]
        assert isinstance(error_document, dict)
        error_document["recovery_id"] = error.recovery_id
    if getattr(arguments, "as_json", False):
        print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    else:
        print(f"{error.code}: {error.message}", file=sys.stderr)
    return error.exit_code


def _command_label(arguments: argparse.Namespace) -> str:
    command = getattr(arguments, "command", "unknown")
    if command == "artifact":
        operation = getattr(arguments, "artifact_command", None)
        return command if operation is None else f"{command} {operation}"
    if command == "fleet":
        operation = getattr(arguments, "fleet_operation", None)
        return command if operation is None else f"{command} {operation}"
    return command


def _map_domain_error(error: Exception) -> CliFailure | None:
    family_failure = _map_distribution_error(error)
    if family_failure is not None:
        return family_failure
    if isinstance(error, ManifestError):
        code, message = MANIFEST_ERROR_CODES[error.kind]
        return CliFailure(code, message)
    if isinstance(error, CompatibilityError):
        code = error.args[0] if len(error.args) == 1 else None
        if code == "semantic_backend_required":
            return CliFailure(code, "semantic backend is required")
        if code == "semantic_model_required":
            return CliFailure(code, "semantic model is required")
        return CliFailure(
            "graphify_contract_failed", "Graphify contract failed"
        )
    if isinstance(error, PrivacyError):
        return CliFailure("privacy_invalid", "privacy policy is invalid")
    if isinstance(error, SecretExceptionError):
        return CliFailure(
            "secret_policy_invalid", "secret exception policy is invalid"
        )
    if isinstance(error, CoverageError):
        return CliFailure("coverage_invalid", "coverage control is invalid")
    if isinstance(error, (StagingError, ReceiptError)):
        return CliFailure("staging_failed", "safe input staging failed")
    if isinstance(error, AdapterError):
        return CliFailure("adaptation_failed", "Graphify adaptation failed")
    if isinstance(error, ArtifactValidationError):
        return CliFailure("artifact_invalid", "graph artifact is invalid")
    if isinstance(error, AtlasOwnershipError):
        return CliFailure("atlas_invalid", "atlas operation is invalid")
    if isinstance(error, GraphifyError):
        return CliFailure(
            "graphify_contract_failed", "Graphify contract failed"
        )
    if isinstance(error, TransactionLockError):
        return CliFailure(
            "lifecycle_lock_failed", "repository lifecycle lock failed"
        )
    if isinstance(error, QueryError):
        code = error.code
        if code in DOCUMENTED_QUERY_ERROR_CODES:
            return CliFailure(code, "graph query failed")
        return CliFailure("query_failed", "graph query failed")
    if isinstance(error, RefreshError):
        code = error.code
        if code not in DOCUMENTED_REFRESH_ERROR_CODES:
            return CliFailure("refresh_failed", "project refresh failed")
        recovery_id = (
            error.recovery_id
            if code == "cleanup_failed"
            and error.recovery_id is not None
            and _RECOVERY_ID.fullmatch(error.recovery_id) is not None
            else None
        )
        return CliFailure(code, _refresh_error_message(code), recovery_id=recovery_id)
    registry_failure = _map_registry_error(error)
    if registry_failure is not None:
        return registry_failure
    if isinstance(error, (OSError, ValueError, TypeError, json.JSONDecodeError)):
        return CliFailure(
            "operation_failed", "project knowledge operation failed"
        )
    return None


def _map_distribution_error(error: Exception) -> CliFailure | None:
    families: tuple[
        tuple[type[Exception], MappingProxyType, tuple[int, str, str]], ...
    ] = (
        (BundleError, BUNDLE_PUBLIC_ERRORS, (1, "bundle_failed", "bundle operation failed")),
        (
            GithubArtifactError,
            GITHUB_PUBLIC_ERRORS,
            (1, "github_artifact_failed", "GitHub artifact operation failed"),
        ),
        (
            AgentInstallError,
            AGENT_PUBLIC_ERRORS,
            (1, "agent_install_failed", "agent resource operation failed"),
        ),
        (FleetConfigError, FLEET_PUBLIC_ERRORS, (1, "fleet_invalid", "fleet request is invalid")),
    )
    for family, table, fallback in families:
        if not isinstance(error, family):
            continue
        if type(error) is not family:
            exit_code, code, message = fallback
            return CliFailure(code, message, exit_code)
        raw_code = getattr(error, "code", None)
        if type(raw_code) is not str:
            exit_code, code, message = fallback
            return CliFailure(code, message, exit_code)
        selected = table.get(raw_code)
        if selected is None:
            exit_code, code, message = fallback
            return CliFailure(code, message, exit_code)
        exit_code, message = selected
        recovery_id = getattr(error, "recovery_id", None)
        recovery_allowed = (
            (family is BundleError and raw_code in {
                "bundle_cleanup_failed",
                "bundle_output_recovery_required",
            })
            or (
                family is GithubArtifactError
                and raw_code == "github_cleanup_failed"
            )
        )
        if (
            not recovery_allowed
            or type(recovery_id) is not str
            or _PUBLIC_RECOVERY_ID.fullmatch(recovery_id) is None
        ):
            recovery_id = None
        return CliFailure(raw_code, message, exit_code, recovery_id)
    return None


def _refresh_error_message(code: str) -> str:
    messages = {
        "init_recovery_required": "configuration recovery is required",
        "manifest_migration_required": "manifest migration is required",
        "projection_failed": "safe projection failed",
        "projection_changed": "safe projection changed",
        "adaptation_failed": "Graphify adaptation failed",
        "refresh_verification_failed": "refresh verification failed",
        "cleanup_failed": "private refresh cleanup failed",
        "build_epoch_exhausted": "graph ownership epoch is exhausted",
        "invalid_refresh_options": "semantic refresh options are invalid",
        "semantic_backend_required": "semantic backend is required",
        "semantic_model_required": "semantic model is required",
    }
    return messages.get(code, "project refresh failed")


def _map_registry_error(error: Exception) -> CliFailure | None:
    if type(error) is not RegistryError:
        return None
    code = error.code
    if code not in DOCUMENTED_REGISTRY_ERROR_CODES:
        return CliFailure("registry_failed", "registry operation failed")
    exit_code, message = REGISTRY_PUBLIC_ERRORS[code]
    return CliFailure(code, message, exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
