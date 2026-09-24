#!/usr/bin/env python3
"""Command-line entrypoint for DebBuilder."""
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

# Package files live under /opt and must never acquire unowned Python caches.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

from debbuilder import app
from debbuilder.local_repository_bootstrap import bootstrap_repository
from debbuilder.repo_files import content_type, open_public_repo_file

REPO_ROOT = app.RUNTIME.repository_root


class RepositoryHandler(BaseHTTPRequestHandler):
    """Public repository listener with no application routing inheritance."""

    def _serve_repo_file(self, head_only=False):
        path = urlparse(self.path).path
        with open_public_repo_file(REPO_ROOT, "/index.html" if path == "/" else path) as opened:
            if not opened:
                self.send_error(404, "Not found")
                return
            file_fd, info, relative = opened
            self.send_response(200)
            self.send_header("Content-Type", content_type(relative))
            self.send_header("Content-Length", str(info.st_size))
            self.send_header("Cache-Control", "no-store" if relative.name in {"index.html", "install.sh"} else "public, max-age=60")
            self.end_headers()
            if not head_only:
                while chunk := os.read(file_fd, 1024 * 1024):
                    self.wfile.write(chunk)

    def do_HEAD(self):
        self._serve_repo_file(head_only=True)

    def do_GET(self):
        self._serve_repo_file()

    def do_POST(self):
        self.send_error(404, "Not found")

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_POST


def main():
    if app.RUNTIME.port == 0 or app.RUNTIME.repository_port == 0:
        raise ValueError("Admin and repository listeners require fixed ports from 1 to 65535")
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
    return app.serve_application(app.Handler, repository_handler_class=RepositoryHandler)


if __name__ == "__main__":
    raise SystemExit(main())
