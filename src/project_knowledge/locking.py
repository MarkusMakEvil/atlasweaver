"""Descriptor-confined POSIX locks and repository mutation authority."""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
import contextvars
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
import stat
import threading
import time
from typing import Iterator, Literal


LockFailureKind = Literal["busy", "unavailable", "authority"]
RepositoryIdentity = tuple[int, int]


class TransactionLockError(RuntimeError):
    """A closed lifecycle-lock failure safe for domain error mapping."""

    def __init__(
        self, message: str, *, kind: LockFailureKind = "unavailable"
    ) -> None:
        if kind not in {"busy", "unavailable", "authority"}:
            raise ValueError("transaction lock error kind is invalid")
        super().__init__(message)
        self.kind: LockFailureKind = kind


def _close_quietly(descriptor: int) -> bool:
    try:
        os.close(descriptor)
        return True
    except OSError:
        return False


@dataclass
class _ManifestBinding:
    descriptor: int
    identity: RepositoryIdentity
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str

    def close(self) -> bool:
        descriptor, self.descriptor = self.descriptor, -1
        return descriptor < 0 or _close_quietly(descriptor)


@dataclass
class RepositoryAccess(AbstractContextManager["RepositoryAccess"]):
    """One no-follow opened repository root and its pinned manifest, if any."""

    descriptor: int
    identity: RepositoryIdentity
    _manifest_binding: _ManifestBinding | None = field(
        default=None, init=False, repr=False
    )

    def _replace_manifest_binding(self, binding: _ManifestBinding) -> None:
        previous, self._manifest_binding = self._manifest_binding, binding
        if previous is not None:
            previous.close()

    def __enter__(self) -> "RepositoryAccess":
        if self.descriptor < 0:
            raise TransactionLockError(
                "repository access is unavailable", kind="unavailable"
            )
        return self

    def __exit__(self, *args: object) -> None:
        binding, self._manifest_binding = self._manifest_binding, None
        descriptor, self.descriptor = self.descriptor, -1
        binding_closed = binding is None or binding.close()
        root_closed = descriptor < 0 or _close_quietly(descriptor)
        if (not binding_closed or not root_closed) and not args[0]:
            raise TransactionLockError(
                "repository access cleanup failed", kind="unavailable"
            )


def open_repository_access(
    repo_root: Path,
    *,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> RepositoryAccess:
    """Open a repository read-only without creating any repository state."""
    descriptor = -1
    try:
        descriptor = os.open(
            repo_root.absolute(),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        if (
            expected_repository_identity is not None
            and identity != expected_repository_identity
        ):
            raise TransactionLockError(
                "repository identity changed", kind="authority"
            )
        return RepositoryAccess(descriptor, identity)
    except TransactionLockError:
        if descriptor >= 0:
            _close_quietly(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            _close_quietly(descriptor)
        raise TransactionLockError(
            "repository access is unavailable", kind="unavailable"
        ) from None


@dataclass
class StateRoot(AbstractContextManager["StateRoot"]):
    path: Path
    descriptor: int
    repository_descriptor: int
    repository_identity: RepositoryIdentity

    def __enter__(self) -> "StateRoot":
        return self

    def __exit__(self, *args: object) -> None:
        descriptor, self.descriptor = self.descriptor, -1
        repository, self.repository_descriptor = self.repository_descriptor, -1
        state_closed = descriptor < 0 or _close_quietly(descriptor)
        root_closed = repository < 0 or _close_quietly(repository)
        if not (state_closed and root_closed) and not args[0]:
            raise TransactionLockError(
                "repository lifecycle cleanup failed", kind="unavailable"
            )


def open_state_root(
    repo_root: Path,
    *,
    create: bool,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> StateRoot | None:
    """Open the private state directory relative to one verified root fd."""
    try:
        repo = repo_root.absolute()
    except OSError:
        raise TransactionLockError(
            "repository lifecycle state is unavailable", kind="unavailable"
        ) from None
    root_fd = -1
    state_fd = -1
    keep = False
    try:
        try:
            root_fd = os.open(
                repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            root_info = os.fstat(root_fd)
        except OSError:
            raise TransactionLockError(
                "repository lifecycle state is unavailable", kind="unavailable"
            ) from None
        identity = (root_info.st_dev, root_info.st_ino)
        if expected_repository_identity is not None and identity != expected_repository_identity:
            raise TransactionLockError(
                "repository identity changed", kind="authority"
            )

        created = False
        try:
            named = os.stat(
                ".project-knowledge", dir_fd=root_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(".project-knowledge", 0o700, dir_fd=root_fd)
                os.fsync(root_fd)
                created = True
                named = os.stat(
                    ".project-knowledge", dir_fd=root_fd, follow_symlinks=False
                )
            except OSError:
                raise TransactionLockError(
                    "repository lifecycle state is unavailable", kind="unavailable"
                ) from None
        except OSError:
            raise TransactionLockError(
                "repository lifecycle state is unavailable", kind="unavailable"
            ) from None

        if not stat.S_ISDIR(named.st_mode) or stat.S_ISLNK(named.st_mode):
            raise TransactionLockError(
                "private state must be a real directory", kind="unavailable"
            )
        try:
            state_fd = os.open(
                ".project-knowledge",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
            if created:
                os.fchmod(state_fd, 0o700)
                os.fsync(state_fd)
                os.fsync(root_fd)
            opened = os.fstat(state_fd)
            rebound = os.stat(
                ".project-knowledge", dir_fd=root_fd, follow_symlinks=False
            )
        except OSError:
            raise TransactionLockError(
                "repository lifecycle state is unavailable", kind="unavailable"
            ) from None
        if (
            (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or (opened.st_dev, opened.st_ino) != (rebound.st_dev, rebound.st_ino)
        ):
            raise TransactionLockError(
                "private state changed while opening", kind="unavailable"
            )
        if (
            opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise TransactionLockError(
                "private state permissions are unsafe", kind="unavailable"
            )
        keep = True
        return StateRoot(repo / ".project-knowledge", state_fd, root_fd, identity)
    finally:
        if not keep:
            if state_fd >= 0:
                _close_quietly(state_fd)
            if root_fd >= 0:
                _close_quietly(root_fd)


def _valid_lock_name(name: object) -> bool:
    return (
        type(name) is str
        and bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and not any(ord(character) < 32 or ord(character) == 127 for character in name)
    )


class ExclusiveDescriptorLock(AbstractContextManager["ExclusiveDescriptorLock"]):
    """An exact-mode flock opened relative to a verified directory fd."""

    def __init__(
        self,
        parent_fd: int,
        name: str,
        timeout: float = 5.0,
        *,
        create: bool = True,
    ) -> None:
        if not _valid_lock_name(name):
            raise TransactionLockError(
                "transaction lock name is invalid", kind="unavailable"
            )
        try:
            self.parent_fd = os.dup(parent_fd)
        except OSError:
            raise TransactionLockError(
                "transaction lock is unavailable", kind="unavailable"
            ) from None
        self.name = name
        self.timeout = timeout
        self.create = create
        self.fd: int | None = None

    def __enter__(self) -> "ExclusiveDescriptorLock":
        created = False
        try:
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            if self.create:
                try:
                    self.fd = os.open(
                        self.name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=self.parent_fd,
                    )
                    created = True
                except FileExistsError:
                    self.fd = os.open(self.name, flags, dir_fd=self.parent_fd)
            else:
                self.fd = os.open(self.name, flags, dir_fd=self.parent_fd)
            if created:
                os.fchmod(self.fd, 0o600)
                os.fsync(self.fd)
                os.fsync(self.parent_fd)
            opened = os.fstat(self.fd)
            named = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                raise TransactionLockError(
                    "transaction lock file is unsafe", kind="unavailable"
                )
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TransactionLockError(
                            "transaction lock timed out", kind="busy"
                        )
                    time.sleep(0.01)
                except OSError:
                    raise TransactionLockError(
                        "transaction lock is unavailable", kind="unavailable"
                    ) from None
        except TransactionLockError as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise
        except OSError:
            error = TransactionLockError(
                "transaction lock is unavailable", kind="unavailable"
            )
            self.__exit__(type(error), error, error.__traceback__)
            raise error from None

    def __exit__(self, *args: object) -> None:
        cleanup_ok = True
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                cleanup_ok = False
            cleanup_ok = _close_quietly(self.fd) and cleanup_ok
            self.fd = None
        if self.parent_fd >= 0:
            cleanup_ok = _close_quietly(self.parent_fd) and cleanup_ok
            self.parent_fd = -1
        if not cleanup_ok and not args[0]:
            raise TransactionLockError(
                "transaction lock cleanup failed", kind="unavailable"
            )


class ExclusiveFileLock(AbstractContextManager["ExclusiveFileLock"]):
    """Backward-compatible path lock retained for low-level callers."""

    def __init__(self, path: Path, timeout: float = 5.0) -> None:
        self.path = path
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> "ExclusiveFileLock":
        try:
            self.fd = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TransactionLockError(
                            "project knowledge transaction is already active",
                            kind="busy",
                        )
                    time.sleep(0.01)
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise

    def __exit__(self, *args: object) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                _close_quietly(self.fd)
                self.fd = None


@dataclass
class _LifecycleLease:
    repository_path: Path
    repository_identity: RepositoryIdentity
    owner_thread_id: int
    repository_descriptor: int
    state_descriptor: int
    active: bool = True


_LIFECYCLE_LEASES: contextvars.ContextVar[tuple[_LifecycleLease, ...]] = (
    contextvars.ContextVar("atlasweaver_lifecycle_leases", default=())
)


def _matching_live_lease(repo_root: Path) -> _LifecycleLease | None:
    try:
        requested_path = repo_root.absolute()
    except OSError:
        raise TransactionLockError(
            "repository lifecycle authority is unavailable", kind="authority"
        ) from None
    owner = threading.get_ident()
    return next(
        (
            lease
            for lease in reversed(_LIFECYCLE_LEASES.get())
            if lease.active
            and lease.owner_thread_id == owner
            and lease.repository_path == requested_path
        ),
        None,
    )


@dataclass
class LifecycleDescriptors:
    repository_descriptor: int
    state_descriptor: int
    repository_identity: RepositoryIdentity


@contextmanager
def capture_lifecycle_descriptors(
    repo_root: Path,
) -> Iterator[LifecycleDescriptors]:
    lease = _matching_live_lease(repo_root)
    if lease is None:
        raise TransactionLockError(
            "repository lifecycle lock is required", kind="authority"
        )
    repository = -1
    state = -1
    try:
        repository = os.dup(lease.repository_descriptor)
        state = os.dup(lease.state_descriptor)
    except OSError:
        if repository >= 0:
            _close_quietly(repository)
        raise TransactionLockError(
            "repository lifecycle descriptors are unavailable", kind="unavailable"
        ) from None
    try:
        yield LifecycleDescriptors(repository, state, lease.repository_identity)
    finally:
        _close_quietly(state)
        _close_quietly(repository)


@contextmanager
def capture_lifecycle_repository(repo_root: Path) -> Iterator[RepositoryAccess]:
    lease = _matching_live_lease(repo_root)
    if lease is None:
        raise TransactionLockError(
            "repository lifecycle lock is required", kind="authority"
        )
    try:
        descriptor = os.dup(lease.repository_descriptor)
    except OSError:
        raise TransactionLockError(
            "repository access is unavailable", kind="unavailable"
        ) from None
    with RepositoryAccess(descriptor, lease.repository_identity) as repository:
        yield repository


class RepositoryLifecycleLock(AbstractContextManager["RepositoryLifecycleLock"]):
    def __init__(
        self,
        repo_root: Path,
        timeout: float,
        *,
        create: bool,
        expected_repository_identity: RepositoryIdentity | None,
    ) -> None:
        try:
            self.repo_root = repo_root.absolute()
        except OSError:
            raise TransactionLockError(
                "repository lifecycle state is unavailable", kind="unavailable"
            ) from None
        self.timeout = timeout
        self.create = create
        self.expected_repository_identity = expected_repository_identity
        self.state: StateRoot | None = None
        self.lock: ExclusiveDescriptorLock | None = None
        self.lease: _LifecycleLease | None = None
        self.token: contextvars.Token[tuple[_LifecycleLease, ...]] | None = None

    def __enter__(self) -> "RepositoryLifecycleLock":
        try:
            self.state = open_state_root(
                self.repo_root,
                create=self.create,
                expected_repository_identity=self.expected_repository_identity,
            )
            if self.state is None:
                raise TransactionLockError(
                    "repository lifecycle state is unavailable", kind="unavailable"
                )
            self.lock = ExclusiveDescriptorLock(
                self.state.descriptor,
                "refresh.lock",
                self.timeout,
                create=self.create,
            )
            self.lock.__enter__()
            self.lease = _LifecycleLease(
                self.repo_root,
                self.state.repository_identity,
                threading.get_ident(),
                self.state.repository_descriptor,
                self.state.descriptor,
            )
            self.token = _LIFECYCLE_LEASES.set(
                (*_LIFECYCLE_LEASES.get(), self.lease)
            )
            return self
        except BaseException as error:
            if self.lock is not None:
                self.lock.__exit__(type(error), error, error.__traceback__)
                self.lock = None
            if self.state is not None:
                self.state.__exit__(type(error), error, error.__traceback__)
                self.state = None
            raise

    def __exit__(self, *args: object) -> None:
        if self.lease is not None:
            self.lease.active = False
        try:
            if self.token is not None:
                _LIFECYCLE_LEASES.reset(self.token)
                self.token = None
        finally:
            try:
                if self.lock is not None:
                    self.lock.__exit__(*args)
                    self.lock = None
            finally:
                if self.state is not None:
                    self.state.__exit__(*args)
                    self.state = None


def repository_lifecycle_lock(
    repo_root: Path,
    timeout: float = 5.0,
    *,
    create: bool = True,
    expected_repository_identity: RepositoryIdentity | None = None,
) -> RepositoryLifecycleLock:
    return RepositoryLifecycleLock(
        repo_root,
        timeout,
        create=create,
        expected_repository_identity=expected_repository_identity,
    )


def assert_lifecycle_lock_held(repo_root: Path) -> None:
    if _matching_live_lease(repo_root) is None:
        raise TransactionLockError(
            "repository lifecycle lock is required", kind="authority"
        )
