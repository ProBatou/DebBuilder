"""Run one argv without a shell while enforcing a combined output byte bound."""
from __future__ import annotations

import argparse
import os
import selectors
import signal
import subprocess


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not 1 <= args.limit <= 4 * 1024 * 1024:
        raise ValueError("bounded process requires an argv and a sane byte limit")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, 1)
    selector.register(process.stderr, selectors.EVENT_READ, 2)
    total = 0
    exceeded = False
    while selector.get_map():
        for key, _events in selector.select(1):
            chunk = os.read(key.fileobj.fileno(), min(65536, args.limit - total + 1))
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            remaining = max(0, args.limit - total)
            if remaining:
                os.write(key.data, chunk[:remaining])
            total += len(chunk)
            if total > args.limit:
                exceeded = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
        if exceeded:
            break
    selector.close()
    process.wait()
    if exceeded:
        os.write(2, b"\nDebBuilder: subprocess output byte limit exceeded\n")
        return 125
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())

