"""Bounded exact inventory of the configured reprepro database."""
from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import apt_repo, artifact_publication
from .repository_lock import RepositoryLockError, RepositoryMutationBusy, repository_lease


MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
MAX_ENTRIES = 10000
TIMEOUT_SECONDS = 15
_LINE = re.compile(r"([^|\r\n]+)\|([^|\r\n]+)\|([^:\r\n]+): ([^ \r\n]+) ([^ \r\n]+)\Z")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]{0,127}\Z")


class RepositoryInventoryError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code = code
        self.status = status


def parse_inventory(text: str, *, codename: str, components: list[str], architectures: list[str]) -> list[dict]:
    """Reject every unexpected line; never mistake a partial parse for an empty repo."""
    rows = []
    allowed_components = set(components)
    allowed_architectures = set(architectures) | {"all"}
    seen = set()
    for line in text.splitlines():
        if len(rows) >= MAX_ENTRIES:
            raise RepositoryInventoryError("repository_inventory_too_large", "Repository inventory exceeds its entry limit")
        match = _LINE.fullmatch(line)
        if not match:
            raise RepositoryInventoryError("repository_inventory_invalid", "Repository inventory output is invalid", 502)
        suite, component, architecture, name, version = match.groups()
        if (suite != codename or component not in allowed_components
                or architecture not in allowed_architectures or not _PACKAGE.fullmatch(name)
                or not apt_repo.DEBIAN_VERSION_PATTERN.fullmatch(version)):
            raise RepositoryInventoryError("repository_inventory_invalid", "Repository inventory output is invalid", 502)
        identity = (name, version, architecture, component)
        if identity in seen:
            raise RepositoryInventoryError("repository_inventory_invalid", "Repository inventory contains a duplicate entry", 502)
        seen.add(identity)
        rows.append(dict(zip(("name", "version", "architecture", "component"), identity)))
    return sorted(rows, key=lambda row: (row["name"], row["version"], row["architecture"], row["component"]))


def _run_list(argv: list[str], *, workspace: Path, pass_fds: tuple[int, ...]) -> str:
    try:
        process = subprocess.Popen(argv, cwd=workspace, env={**os.environ, "LC_ALL": "C"},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   pass_fds=pass_fds, start_new_session=True)
    except OSError as exc:
        raise RepositoryInventoryError("repository_inventory_unavailable", "Repository inventory cannot be queried") from exc
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RepositoryInventoryError("repository_inventory_timeout", "Repository inventory query timed out")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    stream = output[key.data]
                    stream.extend(chunk)
                    if len(stream) > (MAX_OUTPUT_BYTES if key.data == "stdout" else MAX_ERROR_BYTES):
                        raise RepositoryInventoryError("repository_inventory_too_large", "Repository inventory output exceeds its size limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RepositoryInventoryError("repository_inventory_timeout", "Repository inventory query timed out")
        if process.wait(timeout=remaining) != 0:
            raise RepositoryInventoryError("repository_inventory_unavailable", "Repository inventory cannot be queried")
        try:
            return bytes(output["stdout"]).decode("utf-8", "strict")
        except UnicodeError as exc:
            raise RepositoryInventoryError("repository_inventory_invalid", "Repository inventory output is invalid", 502) from exc
    except subprocess.TimeoutExpired as exc:
        raise RepositoryInventoryError("repository_inventory_timeout", "Repository inventory query timed out") from exc
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        process.stdout.close()
        process.stderr.close()


def inventory(repo_root: Path, *, distribution: str, component: str, architecture: str) -> dict:
    binary = shutil.which("reprepro")
    if not binary:
        raise RepositoryInventoryError("repository_inventory_unavailable", "reprepro is unavailable")
    if not all(artifact_publication.REPOSITORY_TOKEN_RE.fullmatch(value or "")
               for value in (distribution, component, architecture)):
        raise RepositoryInventoryError("repository_inventory_unavailable", "Repository configuration is unavailable")
    try:
        # Bootstrap creates the lock. Reads never create a lock or layout directory.
        with repository_lease(repo_root, operation="inventory", create_lock=False) as lease:
            config = artifact_publication._repository_config(lease, distribution, create_layout=False)
            components = config["components"]
            architectures = config["architectures"]
            if component not in components or architecture not in architectures:
                raise RepositoryInventoryError("repository_inventory_unavailable", "Repository configuration does not match the active settings")
            codename = config["codename"]
            command_root = Path(f"/proc/self/fd/{lease.root_fd}")
            argv = [binary, *apt_repo._reprepro_layout_arguments(command_root, lease=lease)[1:], "list", codename]
            text = _run_list(argv, workspace=command_root, pass_fds=lease.inherited_fds)
            packages = parse_inventory(text, codename=codename, components=components, architectures=architectures)
            return {"repository": {"suite": config["suite"] or distribution,
                                   "codename": codename, "components": components,
                                   "architectures": architectures}, "packages": packages}
    except RepositoryMutationBusy as exc:
        raise RepositoryInventoryError("repository_mutation_busy", "The repository is busy with another operation", 409) from exc
    except (RepositoryLockError, artifact_publication.PublicationError, OSError, ValueError) as exc:
        raise RepositoryInventoryError("repository_inventory_unavailable", "Repository inventory is unavailable") from exc
