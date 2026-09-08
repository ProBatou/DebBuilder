#!/usr/bin/env python3
"""Compatibility launcher for the interactive cancellation Behavior Lab."""
from __future__ import annotations

import argparse

from . import behavior_lab


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=behavior_lab.tcp_port, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    try:
        behavior_lab.preflight_bind(args.host, args.port)
        behavior_lab.serve(behavior_lab.scenario("cancellation-running"), host=args.host, port=args.port)
    except OSError as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
