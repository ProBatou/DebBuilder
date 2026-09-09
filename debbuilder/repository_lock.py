"""Pinned, repository-root-scoped serialization and safe file access."""
from __future__ import annotations

import fcntl
import os
import re
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


LOCK_FILE = ".debbuilder-repository.lock"
REPOSITORY_DIRECTORY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]*")
_ACTIVE_LEASE: ContextVar["RepositoryLease | None"] = ContextVar(
    "debbuilder_repository_lease", default=None,
)


class RepositoryLockError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class RepositoryMutationBusy(RepositoryLockError):
    def __init__(self, root: Path, operation: str):
        super().__init__(
            "repository_mutation_busy",
            "Another repository operation is already in progress",
            details={"repository_root": str(root), "operation": str(operation)},
        )


def repository_lease_held() -> bool:
    """Return whether this execution context currently owns a repository lease."""
    lease = _ACTIVE_LEASE.get()
    return bool(lease and lease.active)


def require_no_repository_lease() -> None:
    """Prevent the forbidden repository-lock -> Run-lock acquisition order."""
    if repository_lease_held():
        raise RuntimeError("Run lock cannot be acquired while a repository lease is held")


def safe_relative_path(value: str, *, required_prefix: str | None = None) -> PurePosixPath:
    """Validate a canonical repository-relative POSIX path without normalizing it."""
    raw = str(value or "")
    path = PurePosixPath(raw)
    if (
        not raw
        or "\\" in raw
        or path.is_absolute()
        or raw != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RepositoryLockError(
            "repository_path_invalid", "Repository path is not a normalized relative path",
            details={"path": raw},
        )
    if required_prefix is not None and (not path.parts or path.parts[0] != required_prefix):
        raise RepositoryLockError(
            "repository_path_invalid", f"Repository path must be below {required_prefix}/",
            details={"path": raw},
        )
    return path


@contextmanager
def pinned_directory(path: str | Path):
    """Open every absolute directory component without following symlinks."""
    configured = Path(path).expanduser()
    if not configured.is_absolute():
        configured = Path.cwd() / configured
    absolute = Path(os.path.abspath(configured))
    if absolute == Path("/") or ".." in absolute.parts:
        raise RepositoryLockError(
            "repository_root_invalid", "Repository root is unsafe",
            details={"repository_root": str(absolute)},
        )
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except OSError as exc:
                raise RepositoryLockError(
                    "repository_root_invalid",
                    "Repository root must exist and contain no symlinked path components",
                    details={"repository_root": str(absolute)},
                ) from exc
            os.close(fd)
            fd = child
        yield absolute, fd
    finally:
        os.close(fd)


@dataclass
class RepositoryLease:
    root: Path
    root_fd: int
    lock_fd: int
    operation: str
    device: int
    inode: int
    active: bool = True
    directory_fds: dict[str, int] = field(default_factory=dict)

    @property
    def identity(self) -> dict:
        self.require_active()
        return {
            "root": str(self.root),
            "device": self.device,
            "inode": self.inode,
        }

    @property
    def inherited_fds(self) -> tuple[int, ...]:
        """Keep both the pinned root and lease owned if a mutation child outlives its parent."""
        self.require_active()
        return (self.root_fd, self.lock_fd, *self.directory_fds.values())

    def require_active(self) -> None:
        if not self.active or _ACTIVE_LEASE.get() is not self:
            raise RepositoryLockError(
                "repository_lease_required", "An active repository lease is required",
            )

    def pin_directory(self, name: str, *, create: bool = False) -> Path:
        """Pin one standard-layout root child and return its child-visible path."""
        self.require_active()
        if not REPOSITORY_DIRECTORY_RE.fullmatch(str(name)):
            raise RepositoryLockError("repository_path_invalid", "Repository directory name is unsafe")
        if name in self.directory_fds:
            return Path(f"/proc/self/fd/{self.directory_fds[name]}")
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=self.root_fd)
                os.fsync(self.root_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise RepositoryLockError(
                    "repository_configuration_unsupported",
                    f"Repository directory {name} could not be created safely",
                ) from exc
        try:
            directory_fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self.root_fd,
            )
        except OSError as exc:
            raise RepositoryLockError(
                "repository_configuration_unsupported",
                f"Repository directory {name} must be a real directory below the pinned root",
            ) from exc
        os.set_inheritable(directory_fd, True)
        self.directory_fds[name] = directory_fd
        return Path(f"/proc/self/fd/{directory_fd}")

    def pin_standard_layout(self) -> None:
        self.pin_directory("conf", create=False)
        for name in ("db", "dists", "pool", "lists", "logs", "morgue"):
            self.pin_directory(name, create=True)

    def directory_path(self, name: str) -> Path:
        self.require_active()
        if name not in self.directory_fds:
            raise RepositoryLockError(
                "repository_configuration_unsupported",
                f"Repository directory {name} is not pinned",
            )
        return Path(f"/proc/self/fd/{self.directory_fds[name]}")

    @contextmanager
    def open_regular(self, relative: str | PurePosixPath, *, required_prefix: str | None = None):
        """Open a pinned regular file beneath the repository without symlink traversal."""
        self.require_active()
        path = safe_relative_path(str(relative), required_prefix=required_prefix)
        parent_fd = os.dup(self.root_fd)
        file_fd = -1
        try:
            for part in path.parts[:-1]:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
                os.close(parent_fd)
                parent_fd = child
            file_fd = os.open(
                path.parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise RepositoryLockError(
                    "repository_file_invalid", "Repository object is not a regular file",
                    details={"path": path.as_posix()},
                )
            yield file_fd, info
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise RepositoryLockError(
                "repository_file_invalid",
                "Repository file could not be opened without following symlinks",
                details={"path": path.as_posix()},
            ) from exc
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            os.close(parent_fd)


@contextmanager
def repository_lease(repo_root: str | Path, *, operation: str, blocking: bool = False):
    """Lease one pinned repository root across threads and processes."""
    if repository_lease_held():
        raise RepositoryLockError(
            "repository_lock_order_invalid", "Repository leases are not re-entrant",
        )
    with pinned_directory(repo_root) as (root, root_fd):
        lock_fd = -1
        lease = None
        token = None
        try:
            try:
                lock_fd = os.open(
                    LOCK_FILE,
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    0o600,
                    dir_fd=root_fd,
                )
            except OSError as exc:
                raise RepositoryLockError(
                    "repository_lock_invalid", "Repository lock object is unsafe",
                    details={"repository_root": str(root)},
                ) from exc
            info = os.fstat(lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RepositoryLockError(
                    "repository_lock_invalid", "Repository lock object is unsafe",
                    details={"repository_root": str(root)},
                )
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise RepositoryMutationBusy(root, operation) from exc
            root_info = os.fstat(root_fd)
            os.set_inheritable(lock_fd, True)
            os.set_inheritable(root_fd, True)
            lease = RepositoryLease(
                root=root,
                root_fd=root_fd,
                lock_fd=lock_fd,
                operation=str(operation),
                device=root_info.st_dev,
                inode=root_info.st_ino,
            )
            token = _ACTIVE_LEASE.set(lease)
            yield lease
        finally:
            if lease is not None:
                lease.active = False
                for directory_fd in lease.directory_fds.values():
                    os.close(directory_fd)
            if token is not None:
                _ACTIVE_LEASE.reset(token)
            if lock_fd >= 0:
                os.close(lock_fd)
