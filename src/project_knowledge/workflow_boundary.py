"""Descriptor-retained repository boundary for reusable workflows."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import stat
from types import MappingProxyType
from typing import Literal

from .compatibility import (
    CompatibilityError,
    production_graphify_compatibility,
    validate_public_model_identifier,
)
from .locking import RepositoryAccess, RepositoryIdentity


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


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m project_knowledge.workflow_boundary")
    subparsers = parser.add_subparsers(dest="verb", required=True)
    subparsers.add_parser("validate-extraction")
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
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
