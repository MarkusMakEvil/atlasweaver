"""Install packaged AtlasWeaver guidance without claiming unmanaged user files."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from importlib import resources
from importlib.resources.abc import Traversable
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Literal
import uuid

from .compatibility import (
    AgentInstallContract,
    CompatibilityError,
    GraphifyCompatibility,
    production_graphify_compatibility,
    render_graphify_agent_install,
    resolve_graphify_compatibility,
)
from .graphify import (
    COMMAND_TIMEOUT,
    CommandRunner as GraphifyCommandRunner,
    GraphifyError,
    ResolvedGraphifyExecutable,
    SubprocessCommandRunner,
    probe_graphify,
    resolve_graphify_executable,
    run_graphify_operation,
)


AgentPlatform = Literal["codex", "agents"]
SKILL_RELATIVE = PurePosixPath("skills/using-project-knowledge-graphs")
MANAGED_MARKER = ".atlasweaver-managed.json"
_MANAGER = "atlasweaver-agent-installer"
_MARKER_KEYS = frozenset(
    {"manager", "platform", "resource_digest", "schema_version"}
)
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MARKER_BYTES = 4_096
_REQUIRED_RESOURCE_ENTRIES = frozenset(
    {
        ("SKILL.md", b"f"),
        ("agents", b"d"),
        ("agents/openai.yaml", b"f"),
        ("references", b"d"),
        ("references/workflow.md", b"f"),
    }
)


class AgentInstallError(RuntimeError):
    """Closed failure emitted by AtlasWeaver's managed resource boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AgentInstallRequest:
    platform: AgentPlatform
    user_home: Path


@dataclass(frozen=True)
class AgentInstallDependencies:
    runner: GraphifyCommandRunner
    resolve_graphify: Callable[[], ResolvedGraphifyExecutable]
    resource_root: Traversable


@dataclass(frozen=True)
class AgentInstallResult:
    platform: AgentPlatform
    status: Literal["installed", "already_current", "uninstalled"]
    resource_digest: str


REAL_AGENT_DEPENDENCIES = AgentInstallDependencies(
    runner=SubprocessCommandRunner(),
    resolve_graphify=resolve_graphify_executable,
    resource_root=resources.files("project_knowledge").joinpath(
        "resources/skills/using-project-knowledge-graphs"
    ),
)


@dataclass(frozen=True)
class _ManagedDestination:
    platform: AgentPlatform
    user_home: Path
    platform_root: Path
    skills_root: Path
    destination: Path
    root_identities: tuple[tuple[int, int], tuple[int, int], tuple[int, int]]


def install_agent(
    request: AgentInstallRequest,
    *,
    dependencies: AgentInstallDependencies = REAL_AGENT_DEPENDENCIES,
) -> AgentInstallResult:
    """Delegate the official platform install, then atomically own our skill only."""
    contract, target = _agent_contract(request.platform)
    managed = _prepare_destination(request.user_home, request.platform, target)
    current_digest = _validate_existing_destination(
        managed.destination, request.platform
    )
    try:
        resource_digest = _resource_tree_digest(dependencies.resource_root)
    except Exception:
        raise AgentInstallError("agent_resource_invalid") from None

    try:
        executable = dependencies.resolve_graphify()
        probe_graphify(executable, contract, dependencies.runner)
        rendered = render_graphify_agent_install(
            contract, binary=executable.path, platform=request.platform
        )
        run_graphify_operation(
            dependencies.runner,
            executable,
            rendered.argv,
            env=_agent_environment(managed.user_home),
            timeout=COMMAND_TIMEOUT,
            operation="agent-install",
        )
    except (CompatibilityError, GraphifyError, OSError, ValueError, TypeError):
        raise AgentInstallError("agent_install_failed") from None

    _revalidate_managed_roots(managed)
    delegated_digest = _validate_existing_destination(
        managed.destination, request.platform
    )
    if delegated_digest != current_digest:
        raise AgentInstallError("agent_destination_modified")

    if current_digest == resource_digest:
        return AgentInstallResult(
            request.platform, "already_current", resource_digest
        )

    _replace_managed_resource(
        managed.destination,
        dependencies.resource_root,
        request.platform,
        resource_digest,
    )
    return AgentInstallResult(request.platform, "installed", resource_digest)


def uninstall_agent(
    platform: AgentPlatform,
    user_home: Path,
) -> AgentInstallResult:
    """Remove only an unchanged AtlasWeaver-owned resource tree."""
    _, target = _agent_contract(platform)
    managed = _existing_destination(user_home, platform, target)
    resource_digest = _validate_existing_destination(managed.destination, platform)
    if resource_digest is None:
        raise AgentInstallError("agent_destination_unmanaged")

    tombstone = managed.skills_root / (
        f".{SKILL_RELATIVE.name}.tombstone-{uuid.uuid4().hex}"
    )
    try:
        os.replace(managed.destination, tombstone)
        _fsync_directory(managed.skills_root)
        _remove_owned_tree(tombstone)
        _fsync_directory(managed.skills_root)
    except Exception:
        if tombstone.exists() and not managed.destination.exists():
            try:
                os.replace(tombstone, managed.destination)
                _fsync_directory(managed.skills_root)
            except Exception:
                pass
        raise AgentInstallError("agent_install_failed") from None
    return AgentInstallResult(platform, "uninstalled", resource_digest)


def _install_agent_for_platform_root(
    platform: AgentPlatform,
    platform_root: Path,
    *,
    dependencies: AgentInstallDependencies = REAL_AGENT_DEPENDENCIES,
) -> AgentInstallResult:
    """Legacy script seam whose argument remains the platform root, not HOME."""
    if not isinstance(platform_root, Path) or platform_root.name != f".{platform}":
        raise AgentInstallError("agent_home_unmanaged")
    user_home = platform_root.parent
    expected = user_home / f".{platform}"
    if expected.absolute() != platform_root.absolute():
        raise AgentInstallError("agent_home_unmanaged")
    return install_agent(
        AgentInstallRequest(platform, user_home), dependencies=dependencies
    )


def _agent_contract(
    platform: object,
) -> tuple[GraphifyCompatibility, AgentInstallContract]:
    if platform not in {"codex", "agents"}:
        raise AgentInstallError("agent_platform_unsupported")
    production = production_graphify_compatibility()
    contract = resolve_graphify_compatibility(production.version)
    targets = tuple(item for item in contract.agent_installs if item.platform == platform)
    if len(targets) != 1:
        raise AgentInstallError("agent_install_failed")
    target = targets[0]
    parts = target.home_relative_skill.parts
    if parts != (f".{platform}", "skills", "graphify", "SKILL.md"):
        raise AgentInstallError("agent_install_failed")
    return contract, target


def _prepare_destination(
    user_home: Path,
    platform: AgentPlatform,
    target: AgentInstallContract,
) -> _ManagedDestination:
    home = _real_or_create_directory(user_home, mode=0o700, home=True)
    platform_root = _real_or_create_directory(
        home / target.home_relative_skill.parts[0], mode=0o700
    )
    skills_root = _real_or_create_directory(platform_root / "skills", mode=0o700)
    destination = skills_root.joinpath(*SKILL_RELATIVE.parts[1:])
    _reject_symlink_or_nondirectory(destination, absent_ok=True)
    return _ManagedDestination(
        platform,
        home,
        platform_root,
        skills_root,
        destination,
        _root_identities(home, platform_root, skills_root),
    )


def _existing_destination(
    user_home: Path,
    platform: AgentPlatform,
    target: AgentInstallContract,
) -> _ManagedDestination:
    home = _real_existing_directory(user_home, home=True)
    platform_root = _real_existing_directory(
        home / target.home_relative_skill.parts[0]
    )
    skills_root = _real_existing_directory(platform_root / "skills")
    destination = skills_root.joinpath(*SKILL_RELATIVE.parts[1:])
    _reject_symlink_or_nondirectory(destination, absent_ok=True)
    return _ManagedDestination(
        platform,
        home,
        platform_root,
        skills_root,
        destination,
        _root_identities(home, platform_root, skills_root),
    )


def _root_identities(
    *paths: Path,
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    identities: list[tuple[int, int]] = []
    try:
        for path in paths:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise OSError("managed root is not a real directory")
            identities.append((metadata.st_dev, metadata.st_ino))
    except OSError:
        raise AgentInstallError("agent_destination_unmanaged") from None
    if len(identities) != 3:
        raise AgentInstallError("agent_destination_unmanaged")
    return (identities[0], identities[1], identities[2])


def _revalidate_managed_roots(managed: _ManagedDestination) -> None:
    try:
        current = _root_identities(
            managed.user_home, managed.platform_root, managed.skills_root
        )
    except AgentInstallError:
        raise AgentInstallError("agent_destination_modified") from None
    if current != managed.root_identities:
        raise AgentInstallError("agent_destination_modified")


def _real_or_create_directory(
    path: Path, *, mode: int, home: bool = False
) -> Path:
    if not isinstance(path, Path):
        raise AgentInstallError("agent_home_unmanaged")
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        try:
            parent = absolute.parent.resolve(strict=True)
            parent_metadata = parent.lstat()
            if not stat.S_ISDIR(parent_metadata.st_mode):
                raise OSError("parent is not a directory")
            created = parent / absolute.name
            os.mkdir(created, mode)
            return created.resolve(strict=True)
        except (OSError, ValueError):
            raise AgentInstallError("agent_home_unmanaged") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        code = "agent_home_unmanaged" if home else "agent_destination_unmanaged"
        raise AgentInstallError(code)
    try:
        return absolute.resolve(strict=True)
    except (OSError, ValueError):
        raise AgentInstallError("agent_home_unmanaged") from None


def _real_existing_directory(path: Path, *, home: bool = False) -> Path:
    if not isinstance(path, Path):
        raise AgentInstallError("agent_home_unmanaged")
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise OSError("not a directory")
        return absolute.resolve(strict=True)
    except (OSError, ValueError):
        code = "agent_home_unmanaged" if home else "agent_destination_unmanaged"
        raise AgentInstallError(code) from None


def _reject_symlink_or_nondirectory(path: Path, *, absent_ok: bool) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if absent_ok:
            return
        raise AgentInstallError("agent_destination_unmanaged") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AgentInstallError("agent_destination_unmanaged")


def _agent_environment(user_home: Path) -> dict[str, str]:
    return {
        "HOME": str(user_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
    }


def _validate_existing_destination(
    destination: Path,
    platform: AgentPlatform,
) -> str | None:
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AgentInstallError("agent_destination_unmanaged")
    marker = _read_marker(destination / MANAGED_MARKER, platform)
    try:
        actual = _filesystem_tree_digest(destination, ignore_marker=True)
    except AgentInstallError:
        raise AgentInstallError("agent_destination_modified") from None
    if actual != marker["resource_digest"]:
        raise AgentInstallError("agent_destination_modified")
    return actual


def _read_marker(path: Path, platform: AgentPlatform) -> dict[str, object]:
    try:
        payload = _read_regular_file(path, max_bytes=_MAX_MARKER_BYTES)
        document = _strict_json_object(payload)
    except (AgentInstallError, UnicodeError, ValueError):
        raise AgentInstallError("agent_destination_unmanaged") from None
    if (
        set(document) != _MARKER_KEYS
        or document.get("manager") != _MANAGER
        or document.get("platform") != platform
        or document.get("schema_version") != 1
        or type(document.get("schema_version")) is not int
        or not isinstance(document.get("resource_digest"), str)
        or _HEX_DIGEST.fullmatch(document["resource_digest"]) is None
    ):
        raise AgentInstallError("agent_destination_unmanaged")
    return document


def _strict_json_object(payload: bytes) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(_: str) -> object:
        raise ValueError("non-finite number")

    value = json.loads(
        payload,
        object_pairs_hook=unique,
        parse_constant=reject_constant,
        parse_float=lambda raw: _finite_float(raw),
    )
    if not isinstance(value, dict):
        raise ValueError("marker is not an object")
    return value


def _finite_float(raw: str) -> float:
    value = float(raw)
    if not (float("-inf") < value < float("inf")):
        raise ValueError("non-finite number")
    return value


def _read_regular_file(path: Path, *, max_bytes: int | None = None) -> bytes:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not regular")
        if max_bytes is not None and before.st_size > max_bytes:
            raise OSError("oversized")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or opened.st_size != before.st_size
            ):
                raise OSError("unstable")
            chunks: list[bytes] = []
            total = 0
            while True:
                allowance = 65_536
                if max_bytes is not None:
                    allowance = min(allowance, max_bytes + 1 - total)
                chunk = os.read(descriptor, allowance)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise OSError("oversized")
            final = os.fstat(descriptor)
            if (
                (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino)
                or final.st_size != opened.st_size
                or final.st_mtime_ns != opened.st_mtime_ns
                or final.st_ctime_ns != opened.st_ctime_ns
                or total != opened.st_size
            ):
                raise OSError("unstable")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError:
        raise AgentInstallError("agent_resource_invalid") from None


def _filesystem_tree_digest(root: Path, *, ignore_marker: bool) -> str:
    digest = hashlib.sha256()
    try:
        entries = sorted(
            root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
        )
    except OSError:
        raise AgentInstallError("agent_resource_invalid") from None
    for path in entries:
        relative = path.relative_to(root).as_posix()
        if ignore_marker and relative == MANAGED_MARKER:
            continue
        try:
            metadata = path.lstat()
        except OSError:
            raise AgentInstallError("agent_resource_invalid") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise AgentInstallError("agent_resource_invalid")
        if stat.S_ISDIR(metadata.st_mode):
            kind = b"d"
            payload = b""
        elif stat.S_ISREG(metadata.st_mode):
            kind = b"f"
            payload = _read_regular_file(path)
        else:
            raise AgentInstallError("agent_resource_invalid")
        digest.update(kind + b"\0" + relative.encode("utf-8") + b"\0")
        if kind == b"f":
            digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def _resource_tree_digest(root: Traversable) -> str:
    digest = hashlib.sha256()
    for relative, kind, payload in _walk_resource_tree(root):
        digest.update(kind + b"\0" + relative.encode("utf-8") + b"\0")
        if kind == b"f":
            digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def _walk_resource_tree(root: Traversable) -> tuple[tuple[str, bytes, bytes], ...]:
    if isinstance(root, Path):
        try:
            if stat.S_ISLNK(root.lstat().st_mode):
                raise AgentInstallError("agent_resource_invalid")
        except OSError:
            raise AgentInstallError("agent_resource_invalid") from None
    if not root.is_dir():
        raise AgentInstallError("agent_resource_invalid")
    entries: list[tuple[str, bytes, bytes]] = []

    def visit(directory: Traversable, prefix: PurePosixPath) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            raise AgentInstallError("agent_resource_invalid") from None
        for child in children:
            _require_resource_name(child.name)
            relative = prefix / child.name
            if isinstance(child, Path):
                try:
                    if stat.S_ISLNK(child.lstat().st_mode):
                        raise AgentInstallError("agent_resource_invalid")
                except OSError:
                    raise AgentInstallError("agent_resource_invalid") from None
            if child.is_dir() and not child.is_file():
                entries.append((relative.as_posix(), b"d", b""))
                visit(child, relative)
            elif child.is_file() and not child.is_dir():
                try:
                    with resources.as_file(child) as materialized:
                        payload = _read_regular_file(materialized)
                except AgentInstallError:
                    raise
                except Exception:
                    raise AgentInstallError("agent_resource_invalid") from None
                entries.append((relative.as_posix(), b"f", payload))
            else:
                raise AgentInstallError("agent_resource_invalid")

    visit(root, PurePosixPath())
    if frozenset((relative, kind) for relative, kind, _ in entries) != (
        _REQUIRED_RESOURCE_ENTRIES
    ):
        raise AgentInstallError("agent_resource_invalid")
    return tuple(entries)


def _require_resource_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or name in {"", ".", "..", MANAGED_MARKER}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise AgentInstallError("agent_resource_invalid")


def _replace_managed_resource(
    destination: Path,
    resource_root: Traversable,
    platform: AgentPlatform,
    resource_digest: str,
) -> None:
    skills_root = destination.parent
    stage = Path(
        tempfile.mkdtemp(prefix=f".{SKILL_RELATIVE.name}.stage-", dir=skills_root)
    )
    stage.chmod(0o700)
    backup: Path | None = None
    committed = False
    rollback_tree: Path | None = None
    try:
        _copy_resource_tree(resource_root, stage)
        if _filesystem_tree_digest(stage, ignore_marker=False) != resource_digest:
            raise AgentInstallError("agent_resource_invalid")
        _write_marker(stage, platform, resource_digest)
        _fsync_tree(stage)

        if destination.exists():
            backup = skills_root / (
                f".{SKILL_RELATIVE.name}.backup-{uuid.uuid4().hex}"
            )
            os.replace(destination, backup)
            _fsync_directory(skills_root)
        os.replace(stage, destination)
        committed = True
        _fsync_directory(skills_root)
        if backup is not None:
            _remove_owned_tree(backup)
            backup = None
            _fsync_directory(skills_root)
    except AgentInstallError:
        _rollback_resource_replace(
            destination, stage, backup, committed, rollback_tree
        )
        raise
    except Exception:
        _rollback_resource_replace(
            destination, stage, backup, committed, rollback_tree
        )
        raise AgentInstallError("agent_install_failed") from None
    finally:
        if stage.exists():
            try:
                _remove_owned_tree(stage)
            except Exception:
                pass


def _rollback_resource_replace(
    destination: Path,
    stage: Path,
    backup: Path | None,
    committed: bool,
    rollback_tree: Path | None,
) -> None:
    skills_root = destination.parent
    try:
        if committed and destination.exists():
            rollback_tree = skills_root / (
                f".{SKILL_RELATIVE.name}.failed-{uuid.uuid4().hex}"
            )
            os.replace(destination, rollback_tree)
        if backup is not None and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        if rollback_tree is not None and rollback_tree.exists():
            _remove_owned_tree(rollback_tree)
        if stage.exists():
            _remove_owned_tree(stage)
        _fsync_directory(skills_root)
    except Exception:
        raise AgentInstallError("agent_install_failed") from None


def _copy_resource_tree(root: Traversable, stage: Path) -> None:
    entries = _walk_resource_tree(root)
    for relative, kind, payload in entries:
        target = stage.joinpath(*PurePosixPath(relative).parts)
        if kind == b"d":
            target.mkdir(mode=0o700)
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            target,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_marker(stage: Path, platform: AgentPlatform, digest: str) -> None:
    payload = (
        json.dumps(
            {
                "manager": _MANAGER,
                "platform": platform,
                "resource_digest": digest,
                "schema_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    marker = stage / MANAGED_MARKER
    descriptor = os.open(
        marker,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise AgentInstallError("agent_resource_invalid")
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(path)
        elif stat.S_ISREG(metadata.st_mode):
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            raise AgentInstallError("agent_resource_invalid")
    for directory in reversed(directories):
        _fsync_directory(directory)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_owned_tree(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise AgentInstallError("agent_resource_invalid")
    for entry in path.rglob("*"):
        metadata = entry.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            raise AgentInstallError("agent_resource_invalid")
    shutil.rmtree(path)


__all__ = [
    "AgentInstallDependencies",
    "AgentInstallError",
    "AgentInstallRequest",
    "AgentInstallResult",
    "AgentPlatform",
    "REAL_AGENT_DEPENDENCIES",
    "install_agent",
    "uninstall_agent",
]
