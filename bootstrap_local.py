#!/usr/bin/python3
"""Packaged, noninteractive local repository preflight and bootstrap."""
from __future__ import annotations

import os
import re
import shlex
import stat
import sys
from pathlib import Path


ENVIRONMENT_FILE = Path("/etc/debbuilder/debbuilder.env")
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _load_environment_file(path: Path) -> None:
    if path != ENVIRONMENT_FILE:
        raise ValueError("bootstrap environment file path is not canonical")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
            raise ValueError("bootstrap environment file is not a bounded regular file")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
            lines = stream.read(64 * 1024 + 1).splitlines()
    finally:
        os.close(fd)
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = shlex.split(line, comments=False, posix=True)
        if len(parts) != 1:
            raise ValueError("bootstrap environment file contains an unsupported assignment")
        name, separator, value = parts[0].partition("=")
        if not separator or not ENVIRONMENT_NAME.fullmatch(name) or "\x00" in value:
            raise ValueError("bootstrap environment file contains an invalid assignment")
        os.environ[name] = value


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--environment-file", str(ENVIRONMENT_FILE)]:
        _load_environment_file(ENVIRONMENT_FILE)
    elif arguments:
        raise ValueError("unsupported local bootstrap argument")

    # A preserved environment file may predate the packaged bytecode setting.
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

    # The application reads its environment at import time.
    from debbuilder import app
    from debbuilder.local_repository_bootstrap import bootstrap_repository

    gpg_home = Path(os.environ.get("GNUPGHOME") or app.RUNTIME.data / ".gnupg").absolute()
    os.environ["GNUPGHOME"] = str(gpg_home)
    bootstrap_repository(
        repository_root=app.RUNTIME.repository_root,
        data_root=app.RUNTIME.data,
        suite=app.repo_settings()["distribution"],
        component=app.repo_settings()["component"],
        gpg_home=gpg_home,
        public_url=app.repo_settings()["repository"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
