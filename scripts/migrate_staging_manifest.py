#!/usr/bin/env python3
"""Externalize one legacy Build Run staging inventory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from debbuilder.build_migrations import compact_large_run_payloads, migrate_staging_manifest
from debbuilder.build_store import BuildStore


parser = argparse.ArgumentParser()
parser.add_argument("run_id")
parser.add_argument("--builds-root", type=Path, default=Path("data/builds"))
args = parser.parse_args()
result = migrate_staging_manifest(BuildStore(args.builds_root), args.run_id)
compact = compact_large_run_payloads(BuildStore(args.builds_root), args.run_id)
print(json.dumps({**{key: value for key, value in result.items() if key != "run"}, "other_payloads_changed": compact["changed"]}, sort_keys=True))
