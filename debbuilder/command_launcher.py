"""Execution gate used to verify a finite-policy cgroup before exec."""
from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--":
        return 125
    try:
        released = os.read(0, 1)
    except OSError:
        return 125
    if released != b"1":
        return 125
    arguments = sys.argv[2:]
    os.execvpe(arguments[0], arguments, os.environ)
    return 125


if __name__ == "__main__":
    raise SystemExit(main())
