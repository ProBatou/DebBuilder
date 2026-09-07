#!/usr/bin/env python3
"""Harmless process tree used by the isolated manual cancellation review."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


level = int(sys.argv[1]) if len(sys.argv) > 1 else 2
stopping = False


def stop(_signal, _frame) -> None:
    global stopping
    stopping = True
    print(f"worker-{level} received SIGTERM", flush=True)


signal.signal(signal.SIGTERM, stop)
if level:
    subprocess.Popen([sys.executable, __file__, str(level - 1)])
print(f"worker-{level} ready pid={os.getpid()} pgid={os.getpgid(0)}", flush=True)
index = 0
while not stopping:
    print(f"worker-{level} tick {index}", flush=True)
    index += 1
    time.sleep(1)
print(f"worker-{level} stopped", flush=True)
