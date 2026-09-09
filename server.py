#!/usr/bin/env python3
"""Command-line entrypoint for DebBuilder."""
import os
from urllib.parse import urlparse

from debbuilder import app
from debbuilder.repo_files import content_type, open_public_repo_file

REPO_ROOT = app.RUNTIME.repository_root


class Handler(app.Handler):
    """DebBuilder handler plus read-only serving of public APT artifacts."""

    def _serve_repo_file(self, head_only=False):
        with open_public_repo_file(REPO_ROOT, urlparse(self.path).path) as opened:
            if not opened:
                return False
            file_fd, info, relative = opened
            self.send_response(200)
            self.send_header("Content-Type", content_type(relative))
            self.send_header("Content-Length", str(info.st_size))
            self.send_header("Cache-Control", "public, max-age=60")
            self.end_headers()
            if not head_only:
                while chunk := os.read(file_fd, 1024 * 1024):
                    self.wfile.write(chunk)
            return True

    def do_HEAD(self):
        if self._serve_repo_file(head_only=True):
            return
        super().do_HEAD()

    def do_GET(self):
        if self._serve_repo_file():
            return
        super().do_GET()


def main():
    return app.serve_application(Handler)


if __name__ == "__main__":
    raise SystemExit(main())
