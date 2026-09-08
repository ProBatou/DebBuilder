#!/usr/bin/env python3
"""Command-line entrypoint for DebBuilder."""
from urllib.parse import urlparse

from debbuilder import app
from debbuilder.repo_files import content_type, resolve_public_repo_file

REPO_ROOT = app.RUNTIME.repository_root


class Handler(app.Handler):
    """DebBuilder handler plus read-only serving of public APT artifacts."""

    def _repo_file(self):
        return resolve_public_repo_file(REPO_ROOT, urlparse(self.path).path)

    def _serve_repo_file(self, head_only=False):
        file = self._repo_file()
        if not file:
            return False
        size = file.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type(file))
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "public, max-age=60")
        self.end_headers()
        if not head_only:
            with file.open("rb") as src:
                while chunk := src.read(1024 * 1024):
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
    # The repository-serving entrypoint did not previously run periodic
    # retention; shared lifecycle ownership must not expand that policy here.
    return app.serve_application(Handler, retention_target=None)


if __name__ == "__main__":
    raise SystemExit(main())
