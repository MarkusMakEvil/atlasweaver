"""Descriptor-retained repository boundary for reusable workflows."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import stat
from types import MappingProxyType
from typing import Literal

from .compatibility import (
    CompatibilityError,
    bind_semantic_backend_credential,
    production_graphify_compatibility,
    validate_semantic_backend,
    validate_public_model_identifier,
)
from .doctor import doctor_project
from .graphify import (
    SubprocessCommandRunner,
    minimal_environment,
    probe_graphify,
    resolve_graphify_executable,
)
from .health import assess_health, inspect_project_state
from .lifecycle import RefreshOptions, refresh_project
from .locking import RepositoryAccess, RepositoryIdentity
from .manifest import load_manifest
from .bundles import PackRequest, pack_bundle
from .operation_state import render_ci_summary
from .staging import inspect_projection


WORKFLOW_BOUNDARY_PUBLIC_ERRORS = MappingProxyType({
    "workflow_root_invalid": (1, "workflow repository root is invalid"),
    "workflow_root_forbidden": (1, "workflow repository root is forbidden"),
    "workflow_root_changed": (1, "workflow repository root changed"),
    "workflow_extraction_invalid": (1, "workflow extraction mode is invalid"),
})


class WorkflowBoundaryError(ValueError):
    def __init__(
        self,
        code: Literal[
            "workflow_root_invalid",
            "workflow_root_forbidden",
            "workflow_root_changed",
            "workflow_extraction_invalid",
        ],
        message: str | None = None,
    ) -> None:
        if code not in WORKFLOW_BOUNDARY_PUBLIC_ERRORS:
            raise ValueError("workflow boundary code is invalid")
        self.code = code
        self.message = message or WORKFLOW_BOUNDARY_PUBLIC_ERRORS[code][1]
        super().__init__(self.message)


@dataclass(frozen=True)
class WorkflowGitScope:
    checkout_descriptor: int
    project_descriptor: int
    project_segments: tuple[str, ...]
    checkout_identity: RepositoryIdentity
    project_identity: RepositoryIdentity
    revalidate: Callable[[], None]


@dataclass
class WorkflowRepository(AbstractContextManager["WorkflowRepository"]):
    checkout_descriptor: int
    repository_access: RepositoryAccess
    segments: tuple[str, ...]
    checkout_identity: RepositoryIdentity
    repository_identity: RepositoryIdentity
    _stack: ExitStack
    _bindings: tuple[tuple[int, str, int, RepositoryIdentity], ...]
    _closed: bool = False

    @property
    def git_scope(self) -> WorkflowGitScope:
        if self._closed:
            raise WorkflowBoundaryError("workflow_root_changed")
        return WorkflowGitScope(
            self.checkout_descriptor,
            self.repository_access.descriptor,
            self.segments,
            self.checkout_identity,
            self.repository_identity,
            self.revalidate,
        )

    def revalidate(self) -> None:
        if self._closed or self.repository_access.descriptor < 0:
            raise WorkflowBoundaryError("workflow_root_changed")
        try:
            for parent, name, child, expected in self._bindings:
                parent_info = os.fstat(parent)
                child_info = os.fstat(child)
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(parent_info.st_mode)
                    or not stat.S_ISDIR(child_info.st_mode)
                    or _identity(child_info) != expected
                    or _identity(named) != expected
                ):
                    raise OSError
            if _identity(os.fstat(self.repository_access.descriptor)) != self.repository_identity:
                raise OSError
        except OSError:
            raise WorkflowBoundaryError("workflow_root_changed") from None

    def __enter__(self) -> "WorkflowRepository":
        self.revalidate()
        return self

    def __exit__(self, *args: object) -> None:
        if self._closed:
            return
        self._closed = True
        self.repository_access.__exit__(*args)
        self._stack.close()


def open_workflow_repository(
    consumer_checkout: Path,
    repo_root: str,
    *,
    forbidden_checkout: Path,
) -> WorkflowRepository:
    """Retain a no-follow checkout/root chain and forbid the trusted tool tree."""
    segments = _root_segments(repo_root)
    stack = ExitStack()
    try:
        checkout_fd, checkout_bindings = _open_absolute_directory(
            consumer_checkout, stack
        )
        checkout_identity = _identity(os.fstat(checkout_fd))
        forbidden_fd, _ = _open_absolute_directory(forbidden_checkout, stack)
        if checkout_identity == _identity(os.fstat(forbidden_fd)):
            raise WorkflowBoundaryError("workflow_root_forbidden")

        bindings = list(checkout_bindings)
        project_fd = checkout_fd
        for segment in segments:
            project_fd, binding = _open_child(project_fd, segment, stack)
            bindings.append(binding)
        project_identity = _identity(os.fstat(project_fd))
        if project_identity == _identity(os.fstat(forbidden_fd)):
            raise WorkflowBoundaryError("workflow_root_forbidden")
        access = RepositoryAccess(os.dup(project_fd), project_identity)
        repository = WorkflowRepository(
            checkout_fd,
            access,
            segments,
            checkout_identity,
            project_identity,
            stack,
            tuple(bindings),
        )
        repository.revalidate()
        return repository
    except WorkflowBoundaryError:
        stack.close()
        raise
    except (OSError, ValueError, TypeError):
        stack.close()
        raise WorkflowBoundaryError("workflow_root_invalid") from None


def admit_workflow_extraction_mode(
    backend: str, model: str, deep: bool
) -> Literal["code_only", "semantic"]:
    if type(backend) is not str or type(model) is not str or type(deep) is not bool:
        raise WorkflowBoundaryError("workflow_extraction_invalid")
    if backend == "" and model == "" and deep is False:
        return "code_only"
    if not backend or not model:
        raise WorkflowBoundaryError("workflow_extraction_invalid")
    contract = production_graphify_compatibility()
    if backend not in {item.name for item in contract.backends}:
        raise WorkflowBoundaryError("workflow_extraction_invalid")
    try:
        validate_public_model_identifier(model)
    except CompatibilityError:
        raise WorkflowBoundaryError("workflow_extraction_invalid") from None
    return "semantic"


def _root_segments(value: object) -> tuple[str, ...]:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 4096:
        raise WorkflowBoundaryError("workflow_root_invalid")
    if value == ".":
        return ()
    if "\\" in value or value.startswith("/"):
        raise WorkflowBoundaryError("workflow_root_invalid")
    path = PurePosixPath(value)
    if path.as_posix() != value or any(
        segment in {"", ".", ".."}
        or any(ord(character) < 32 or ord(character) == 127 for character in segment)
        for segment in path.parts
    ):
        raise WorkflowBoundaryError("workflow_root_invalid")
    return tuple(path.parts)


def _open_absolute_directory(
    path: Path, stack: ExitStack
) -> tuple[int, tuple[tuple[int, str, int, RepositoryIdentity], ...]]:
    absolute = path.absolute()
    if not absolute.is_absolute():
        raise WorkflowBoundaryError("workflow_root_invalid")
    root = os.open(
        "/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    stack.callback(os.close, root)
    current = root
    bindings: list[tuple[int, str, int, RepositoryIdentity]] = []
    for segment in absolute.parts[1:]:
        current, binding = _open_child(current, segment, stack)
        bindings.append(binding)
    return current, tuple(bindings)


def _open_child(
    parent: int, name: str, stack: ExitStack
) -> tuple[int, tuple[int, str, int, RepositoryIdentity]]:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise WorkflowBoundaryError("workflow_root_invalid")
    child = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent,
    )
    stack.callback(os.close, child)
    opened = os.fstat(child)
    rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
    identity = _identity(opened)
    if identity != _identity(before) or identity != _identity(rebound):
        raise WorkflowBoundaryError("workflow_root_changed")
    return child, (parent, name, child, identity)


def _identity(value: os.stat_result) -> RepositoryIdentity:
    return value.st_dev, value.st_ino


def _bounded_environment(name: str) -> str:
    value = os.environ.pop(name, "")
    if type(value) is not str or len(value.encode("utf-8")) > 4096:
        raise WorkflowBoundaryError("workflow_extraction_invalid")
    return value


def _prepare_output_directory(path: Path) -> Path:
    if type(path) is not Path or not path.is_absolute():
        raise WorkflowBoundaryError("workflow_root_invalid")
    try:
        if path.exists():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise OSError
        else:
            parent = path.parent
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise OSError
            path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
        return path
    except OSError:
        raise WorkflowBoundaryError("workflow_root_invalid") from None


def _write_envelope(directory: Path, name: str, document: dict[str, object]) -> None:
    payload = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    if len(payload) > 1_048_576 or "/" in name or "\\" in name:
        raise WorkflowBoundaryError("workflow_root_invalid")
    destination = directory / name
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run_workflow_check(
    consumer_checkout: Path,
    repo_root: str,
    trusted_tool_checkout: Path,
    output_directory: Path,
) -> None:
    """Run the fixed read-only workflow inspection through one root authority."""
    with open_workflow_repository(
        consumer_checkout,
        repo_root,
        forbidden_checkout=trusted_tool_checkout,
    ) as workflow:
        repository_path = consumer_checkout.absolute().joinpath(*workflow.segments)
        workflow.revalidate()
        manifest = load_manifest(
            repository_path / ".graphify-project.yaml",
            repository_path,
            repository_access=workflow.repository_access,
        )
        projection = inspect_projection(
            repository_path,
            manifest,
            repository_access=workflow.repository_access,
        )
        workflow.revalidate()

        backend = _bounded_environment("ATLASWEAVER_BACKEND")
        model = _bounded_environment("ATLASWEAVER_MODEL")
        raw_deep = _bounded_environment("ATLASWEAVER_DEEP") or "false"
        if raw_deep not in {"true", "false"}:
            raise WorkflowBoundaryError("workflow_extraction_invalid")
        mode = admit_workflow_extraction_mode(
            backend, model, raw_deep == "true"
        )
        credential_bound = False
        if mode == "semantic":
            token = os.environ.pop("ATLASWEAVER_BACKEND_TOKEN", None)
            if token is not None and (
                type(token) is not str
                or not token
                or len(token.encode("utf-8")) > 65_536
                or any(ord(character) < 32 for character in token)
            ):
                raise WorkflowBoundaryError("workflow_extraction_invalid")
            contract = production_graphify_compatibility()
            try:
                credential = bind_semantic_backend_credential(
                    contract, backend, token
                )
                validate_semantic_backend(contract, backend, credential)
            except CompatibilityError:
                raise WorkflowBoundaryError("workflow_extraction_invalid") from None
            credential_bound = bool(credential)
        else:
            os.environ.pop("ATLASWEAVER_BACKEND_TOKEN", None)

        raw_required = _bounded_environment("ATLASWEAVER_REQUIRE_IMPACT_TRUST")
        if raw_required not in {"", "true", "false"}:
            raise WorkflowBoundaryError("workflow_extraction_invalid")
        require_impact = raw_required == "true"
        executable = resolve_graphify_executable()
        capabilities = probe_graphify(
            executable,
            production_graphify_compatibility(),
            SubprocessCommandRunner(),
            before_exec=workflow.revalidate,
        )
        workflow.revalidate()
        doctor_project(
            repository_path,
            expected_repository_identity=workflow.repository_identity,
            expected_manifest=manifest,
        )
        workflow.revalidate()
        health = assess_health(
            inspect_project_state(
                repository_path,
                manifest,
                repository_access=workflow.repository_access,
            )
        )
        workflow.revalidate()
        if health.core_status not in {"healthy", "partial"}:
            raise WorkflowBoundaryError("workflow_root_invalid")
        raw_required = _bounded_environment("ATLASWEAVER_REQUIRE_IMPACT_TRUST")
        if raw_required not in {"", "true", "false"}:
            raise WorkflowBoundaryError("workflow_extraction_invalid")
        if raw_required == "true" and health.trust.impact != "trusted":
            raise WorkflowBoundaryError("workflow_root_invalid")
        if require_impact and health.trust.impact != "trusted":
            raise WorkflowBoundaryError("workflow_root_invalid")

        output = _prepare_output_directory(output_directory.absolute())
        documents = (
            (
                "preflight.json",
                {
                    "schema_version": 1,
                    "status": "ready",
                    "mode": mode,
                    "graphify_version": capabilities.version,
                    "safe_file_count": len(projection.files),
                    "credential_bound": credential_bound,
                },
            ),
            ("doctor.json", doctor.to_dict()),
            (
                "scan.json",
                {
                    "schema_version": 1,
                    "status": "clean",
                    "finding_count": 0,
                    "reason_counts": dict(projection.reason_counts),
                },
            ),
            ("health.json", health.to_dict()),
        )
        for name, document in documents:
            workflow.revalidate()
            _write_envelope(output, name, document)
        workflow.revalidate()


def _write_inspect_document(path: Path, document: dict[str, object]) -> None:
    parent = path.absolute().parent
    try:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError
        payload = json.dumps(
            document, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8") + b"\n"
        descriptor = os.open(
            path.absolute(),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise WorkflowBoundaryError("workflow_root_invalid") from None


def _emit_github_inspect_outputs(document: dict[str, object]) -> None:
    raw = os.environ.get("GITHUB_OUTPUT")
    if raw is None:
        return
    if type(raw) is not str or not raw or len(raw.encode("utf-8")) > 4096:
        raise WorkflowBoundaryError("workflow_root_invalid")
    payload = (
        f"project_uid={document['project_uid']}\n"
        f"channel={document['channel']}\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(
            Path(raw).absolute(),
            os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise WorkflowBoundaryError("workflow_root_invalid") from None


def run_workflow_inspect(
    consumer_checkout: Path,
    repo_root: str,
    trusted_tool_checkout: Path,
    output: Path,
) -> None:
    """Project only the validated portable identity needed for job wiring."""
    with open_workflow_repository(
        consumer_checkout,
        repo_root,
        forbidden_checkout=trusted_tool_checkout,
    ) as workflow:
        repository_path = consumer_checkout.absolute().joinpath(*workflow.segments)
        manifest = load_manifest(
            repository_path / ".graphify-project.yaml",
            repository_path,
            repository_access=workflow.repository_access,
        )
        artifact = manifest.artifacts
        if (
            manifest.schema_version != 2
            or manifest.project_uid is None
            or artifact.provider != "github-release"
            or artifact.channel is None
        ):
            raise WorkflowBoundaryError("workflow_root_invalid")
        workflow.revalidate()
        document = {
            "schema_version": 1,
            "project_uid": str(manifest.project_uid),
            "channel": artifact.channel,
        }
        _write_inspect_document(output, document)
        workflow.revalidate()
        _emit_github_inspect_outputs(document)
        workflow.revalidate()


def run_workflow_build(
    consumer_checkout: Path,
    repo_root: str,
    trusted_tool_checkout: Path,
    output_bundle: Path,
    output_summary: Path,
) -> None:
    """Refresh and pack through one descriptor-retained workflow authority."""
    with open_workflow_repository(
        consumer_checkout,
        repo_root,
        forbidden_checkout=trusted_tool_checkout,
    ) as workflow:
        repository_path = consumer_checkout.absolute().joinpath(*workflow.segments)
        workflow.revalidate()
        manifest = load_manifest(
            repository_path / ".graphify-project.yaml",
            repository_path,
            repository_access=workflow.repository_access,
        )
        if manifest.schema_version != 2 or manifest.project_uid is None:
            raise WorkflowBoundaryError("workflow_root_invalid")
        workflow.revalidate()

        backend = _bounded_environment("ATLASWEAVER_BACKEND")
        model = _bounded_environment("ATLASWEAVER_MODEL")
        raw_deep = _bounded_environment("ATLASWEAVER_DEEP") or "false"
        if raw_deep not in {"true", "false"}:
            raise WorkflowBoundaryError("workflow_extraction_invalid")
        mode = admit_workflow_extraction_mode(backend, model, raw_deep == "true")
        ambient = minimal_environment()
        credential_bound = False
        if mode == "semantic":
            token = os.environ.pop("ATLASWEAVER_BACKEND_TOKEN", None)
            if token is not None and (
                type(token) is not str
                or not token
                or len(token.encode("utf-8")) > 65_536
                or any(ord(character) < 32 for character in token)
            ):
                raise WorkflowBoundaryError("workflow_extraction_invalid")
            try:
                credential = bind_semantic_backend_credential(
                    production_graphify_compatibility(), backend, token
                )
                validate_semantic_backend(
                    production_graphify_compatibility(), backend, credential
                )
            except CompatibilityError:
                raise WorkflowBoundaryError("workflow_extraction_invalid") from None
            ambient.update(credential)
            credential_bound = bool(credential)
        else:
            os.environ.pop("ATLASWEAVER_BACKEND_TOKEN", None)

        workflow.revalidate()
        doctor = doctor_project(
            repository_path,
            expected_repository_identity=workflow.repository_identity,
            expected_manifest=manifest,
        )
        result = refresh_project(
            repository_path,
            manifest,
            RefreshOptions(
                None if mode == "code_only" else backend,
                None if mode == "code_only" else model,
                raw_deep == "true",
                mode == "code_only",
            ),
            ambient=ambient,
            expected_repository_identity=workflow.repository_identity,
        )
        workflow.revalidate()
        health = assess_health(
            inspect_project_state(
                repository_path,
                manifest,
                repository_access=workflow.repository_access,
            )
        )
        if health.core_status not in {"healthy", "partial"}:
            raise WorkflowBoundaryError("workflow_root_invalid")
        output_parent = _prepare_output_directory(output_bundle.absolute().parent)
        if output_summary.absolute().parent != output_parent:
            _prepare_output_directory(output_summary.absolute().parent)
        packed = pack_bundle(
            PackRequest(repository_path, output_bundle.absolute()),
            expected_repository_identity=workflow.repository_identity,
            expected_manifest=manifest,
        )
        workflow.revalidate()
        machine, _ = render_ci_summary([{
            "schema_version": 1,
            "project_id": manifest.project_id,
            "project_uid": str(manifest.project_uid),
            "operation": "artifact_pack",
            "status": result.status,
            "source_digest": packed.artifact.source_digest,
            "projection_digest": packed.artifact.projection_digest,
            "graph_digest": packed.artifact.graph_digest,
            "generation_digest": packed.artifact.generation_digest,
            "build_epoch": packed.artifact.build_epoch,
            "artifact_channel": packed.artifact.transport.channel,
            "backend": None if mode == "code_only" else backend,
            "model": None if mode == "code_only" else model,
            "deep": raw_deep == "true",
            "credential_bound": credential_bound,
            "core_status": health.core_status,
        }])
        _write_inspect_document(
            output_summary.absolute(),
            json.loads(machine.decode("utf-8")),
        )
        workflow.revalidate()


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m project_knowledge.workflow_boundary")
    subparsers = parser.add_subparsers(dest="verb", required=True)
    subparsers.add_parser("validate-extraction")
    check = subparsers.add_parser("check")
    check.add_argument("--consumer-checkout", type=Path, required=True)
    check.add_argument("--repo-root", required=True)
    check.add_argument("--trusted-tool-checkout", type=Path, required=True)
    check.add_argument("--output-directory", type=Path, required=True)
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--consumer-checkout", type=Path, required=True)
    inspect.add_argument("--repo-root", required=True)
    inspect.add_argument("--trusted-tool-checkout", type=Path, required=True)
    inspect.add_argument("--output", type=Path, required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--consumer-checkout", type=Path, required=True)
    build.add_argument("--repo-root", required=True)
    build.add_argument("--trusted-tool-checkout", type=Path, required=True)
    build.add_argument("--output-bundle", type=Path, required=True)
    build.add_argument("--output-summary", type=Path, required=True)
    arguments = parser.parse_args(argv)
    if arguments.verb == "validate-extraction":
        try:
            backend = _bounded_environment("ATLASWEAVER_BACKEND")
            model = _bounded_environment("ATLASWEAVER_MODEL")
            raw_deep = _bounded_environment("ATLASWEAVER_DEEP")
            if raw_deep not in {"true", "false"}:
                raise WorkflowBoundaryError("workflow_extraction_invalid")
            admit_workflow_extraction_mode(backend, model, raw_deep == "true")
            return 0
        except WorkflowBoundaryError:
            return 1
    if arguments.verb == "check":
        try:
            run_workflow_check(
                arguments.consumer_checkout,
                arguments.repo_root,
                arguments.trusted_tool_checkout,
                arguments.output_directory,
            )
            return 0
        except (WorkflowBoundaryError, OSError, ValueError, TypeError):
            return 1
    if arguments.verb == "inspect":
        try:
            run_workflow_inspect(
                arguments.consumer_checkout,
                arguments.repo_root,
                arguments.trusted_tool_checkout,
                arguments.output,
            )
            return 0
        except (WorkflowBoundaryError, OSError, ValueError, TypeError):
            return 1
    if arguments.verb == "build":
        try:
            run_workflow_build(
                arguments.consumer_checkout,
                arguments.repo_root,
                arguments.trusted_tool_checkout,
                arguments.output_bundle,
                arguments.output_summary,
            )
            return 0
        except (WorkflowBoundaryError, OSError, ValueError, TypeError):
            return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
