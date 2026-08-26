"""Closed public CLI surface for bundles, agents, pull, and universal fleets."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import os
from pathlib import Path
from typing import Callable

from .agent_install import AgentInstallRequest, install_agent, uninstall_agent
from .bundles import PackRequest, install_local_bundle, pack_bundle
from .compatibility import (
    bind_semantic_backend_credential,
    resolve_graphify_compatibility,
    validate_public_model_identifier,
    validate_semantic_backend,
)
from .fleet import (
    DEFAULT_FLEET_RUNNER,
    FleetBaseRequest,
    FleetProjectRunner,
    FleetQueryRequest,
    ProjectBackendEnvironment,
    PullFleetRequest,
    RefreshFleetRequest,
    load_fleet_workspace,
    require_fleet_projects_ready,
    run_fleet_operation,
    select_fleet_projects,
)
from .github_artifacts import GithubCredentials, pull_bundle
from .graphify import minimal_environment
from .locking import open_repository_access
from .manifest import inspect_init_journal, load_manifest
from .queries import RegistryQueryRequest


@dataclass(frozen=True)
class DistributionResponse:
    document: dict[str, object]
    exit_code: int = 0


def _bounded_integer(minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            parsed = int(value, 10)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError("value is not an integer") from None
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"value must be between {minimum} and {maximum}"
            )
        return parsed

    return parse


class _BoundedRelationAction(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None) -> None:
        current = list(getattr(namespace, self.dest, ()) or ())
        if len(current) >= 16:
            raise argparse.ArgumentError(self, "at most 16 relations are allowed")
        current.append(value)
        setattr(namespace, self.dest, current)


def add_distribution_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    artifact = subparsers.add_parser("artifact")
    artifact_sub = artifact.add_subparsers(dest="artifact_command", required=True)
    pack = artifact_sub.add_parser("pack")
    _repo_json(pack)
    pack.add_argument("--output", type=Path, required=True)
    install = artifact_sub.add_parser("install")
    _repo_json(install)
    install.add_argument("--bundle", type=Path, required=True)

    pull = subparsers.add_parser("pull")
    _repo_json(pull)
    for name in ("install-agent", "uninstall-agent"):
        parser = subparsers.add_parser(name)
        parser.add_argument("--platform", choices=("codex", "agents"), required=True)
        parser.add_argument("--home", type=Path)
        parser.add_argument("--json", action="store_true", dest="as_json")

    fleet = subparsers.add_parser("fleet")
    fleet_sub = fleet.add_subparsers(dest="fleet_operation", required=True)
    for operation in ("doctor", "health", "pull", "registry-sync"):
        _fleet_common(fleet_sub.add_parser(operation))
    refresh = fleet_sub.add_parser("refresh")
    _fleet_common(refresh)
    refresh.add_argument("--backend")
    refresh.add_argument("--model")
    refresh.add_argument("--deep", action="store_true")
    refresh.add_argument("--code-only", action="store_true")
    query = fleet_sub.add_parser("query")
    _fleet_common(query)
    query_sub = query.add_subparsers(dest="fleet_query_operation", required=True)
    term = query_sub.add_parser("query")
    term.add_argument("term")
    term.add_argument("--limit", type=_bounded_integer(1, 100), default=20)
    path = query_sub.add_parser("path")
    path.add_argument("source")
    path.add_argument("target")
    path.add_argument("--max-depth", type=_bounded_integer(1, 32), default=32)
    explain = query_sub.add_parser("explain")
    explain.add_argument("node")
    explain.add_argument("--depth", type=_bounded_integer(1, 2), default=1)
    affected = query_sub.add_parser("affected")
    affected.add_argument("node")
    affected.add_argument("--depth", type=_bounded_integer(0, 8), default=2)
    affected.add_argument(
        "--relation", action=_BoundedRelationAction, default=[]
    )


def _repo_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")


def _fleet_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--json", action="store_true", dest="as_json")


def is_distribution_command(arguments: argparse.Namespace) -> bool:
    return getattr(arguments, "command", None) in {
        "artifact", "pull", "install-agent", "uninstall-agent", "fleet"
    }


def dispatch_distribution(
    arguments: argparse.Namespace,
    *,
    read_secret: Callable[[str], str | None],
    fleet_runner: FleetProjectRunner = DEFAULT_FLEET_RUNNER,
) -> DistributionResponse:
    command = arguments.command
    if command in {"install-agent", "uninstall-agent"}:
        home = arguments.home if arguments.home is not None else Path.home()
        result = (
            install_agent(AgentInstallRequest(arguments.platform, home))
            if command == "install-agent"
            else uninstall_agent(arguments.platform, home)
        )
        return DistributionResponse(_success(
            command,
            result.status,
            platform=result.platform,
            resource_digest=result.resource_digest,
        ))
    if command == "fleet":
        return _dispatch_fleet(arguments, read_secret, fleet_runner)

    repo, manifest, identity = _admit_repository(arguments.repo)
    if command == "artifact" and arguments.artifact_command == "pack":
        result = pack_bundle(
            PackRequest(repo, arguments.output),
            expected_repository_identity=identity,
            expected_manifest=manifest,
        )
        return DistributionResponse(_success(
            "artifact pack",
            "packed",
            project_id=manifest.project_id,
            artifact_sha256=result.sha256,
            byte_length=result.byte_length,
        ))
    if command == "artifact":
        result = install_local_bundle(
            repo,
            arguments.bundle,
            expected_repository_identity=identity,
            expected_manifest=manifest,
        )
        return DistributionResponse(
            _install_document("artifact install", result),
            3 if result.status == "promoted_but_stale" else 0,
        )
    token = read_secret("GITHUB_TOKEN")
    credentials = GithubCredentials(token or "")
    pulled = pull_bundle(
        repo,
        credentials,
        expected_repository_identity=identity,
        expected_manifest=manifest,
    )
    return DistributionResponse(
        _install_document("pull", pulled.install),
        3 if pulled.install.status == "promoted_but_stale" else 0,
    )


def _admit_repository(repo: Path) -> tuple[Path, object, tuple[int, int]]:
    absolute = repo.absolute()
    with open_repository_access(absolute) as repository:
        if inspect_init_journal(
            absolute, repository_access=repository
        ) != "none":
            from .bundles import BundleError

            raise BundleError("init_recovery_required")
        manifest = load_manifest(
            absolute / ".graphify-project.yaml",
            absolute,
            repository_access=repository,
        )
        return absolute, manifest, repository.identity


def _dispatch_fleet(
    arguments: argparse.Namespace,
    read_secret: Callable[[str], str | None],
    runner: FleetProjectRunner,
) -> DistributionResponse:
    workspace = load_fleet_workspace(arguments.workspace)
    selected = select_fleet_projects(workspace, tuple(arguments.project))
    admissions = require_fleet_projects_ready(selected)
    operation = arguments.fleet_operation
    if operation in {"doctor", "health", "registry-sync"}:
        request = FleetBaseRequest()
    elif operation == "pull":
        request = PullFleetRequest(GithubCredentials(read_secret("GITHUB_TOKEN") or ""))
    elif operation == "refresh":
        request = _fleet_refresh_request(arguments, admissions, read_secret)
    else:
        request = FleetQueryRequest(_fleet_query_request(arguments))
    result = run_fleet_operation(
        workspace,
        operation,
        selected,
        request,
        runner,
        expected_admissions=admissions,
    )
    document = result.to_dict()
    document.pop("operation", None)
    document["command"] = f"fleet {operation}"
    if operation == "refresh" and result.status == "ok" and any(
        item.status == "promoted_but_stale" for item in result.projects
    ):
        document["status"] = "promoted_but_stale"
        exit_code = 3
    else:
        exit_code = 0 if result.status == "ok" else 1
    return DistributionResponse(document, exit_code)


def _fleet_refresh_request(
    arguments: argparse.Namespace,
    admissions: tuple[object, ...],
    read_secret: Callable[[str], str | None],
) -> RefreshFleetRequest:
    if arguments.code_only:
        if arguments.backend is not None or arguments.model is not None or arguments.deep:
            from .fleet import FleetConfigError

            raise FleetConfigError("fleet_invalid")
        return RefreshFleetRequest(None, None, False, True, ())
    if arguments.backend is None:
        from .compatibility import CompatibilityError

        raise CompatibilityError("semantic_backend_required")
    if arguments.model is None:
        from .compatibility import CompatibilityError

        raise CompatibilityError("semantic_model_required")
    validate_public_model_identifier(arguments.model)
    resolved = []
    for admission in admissions:
        contract = resolve_graphify_compatibility(admission.manifest.graphify_version)
        backend = next(
            (item for item in contract.backends if item.name == arguments.backend), None
        )
        if backend is None:
            from .compatibility import CompatibilityError

            raise CompatibilityError("semantic_backend_required")
        resolved.append((admission, contract, backend))
    generic = read_secret("ATLASWEAVER_BACKEND_TOKEN")
    environments = []
    for admission, contract, backend in resolved:
        credential = generic
        if credential is None:
            credential = read_secret(backend.canonical_credential_environment)
        bound = bind_semantic_backend_credential(
            contract, arguments.backend, credential
        )
        environment = {**minimal_environment(), **bound}
        validate_semantic_backend(contract, arguments.backend, environment)
        environments.append(ProjectBackendEnvironment.capture(
            admission.project.project_uid,
            environment,
            canonical_credential_name=backend.canonical_credential_environment,
        ))
    return RefreshFleetRequest(
        arguments.backend,
        arguments.model,
        arguments.deep,
        False,
        tuple(environments),
    )


def _fleet_query_request(arguments: argparse.Namespace) -> RegistryQueryRequest:
    operation = arguments.fleet_query_operation
    if operation == "query":
        return RegistryQueryRequest(command="query", term=arguments.term, limit=arguments.limit)
    if operation == "path":
        return RegistryQueryRequest(
            command="path",
            source=arguments.source,
            target=arguments.target,
            max_depth=arguments.max_depth,
        )
    if operation == "explain":
        return RegistryQueryRequest(command="explain", node=arguments.node, depth=arguments.depth)
    return RegistryQueryRequest(
        command="affected",
        node=arguments.node,
        depth=arguments.depth,
        relations=tuple(arguments.relation),
    )


def _install_document(command: str, result: object) -> dict[str, object]:
    document = _success(
        command,
        result.status,
        project_id=result.project_id,
        project_uid=result.project_uid,
        graph_digest=result.graph_digest,
        generation_digest=result.generation_digest,
        build_epoch=result.build_epoch,
        changed=result.changed,
    )
    if result.recovery_id is not None:
        document["recovery_id"] = result.recovery_id
    return document


def _success(command: str, status: str, **fields: object) -> dict[str, object]:
    return {"schema_version": 1, "command": command, "status": status, **fields}
