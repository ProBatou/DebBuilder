"""Controlled validation environments and runtime capability checks."""
from __future__ import annotations

import re


PROFILES = {
    "bookworm": {"image": "debbuilder-validation:bookworm", "capabilities": {}},
    "bookworm-node22": {
        "image": "debbuilder-validation:bookworm-node22",
        "capabilities": {"node": "22.22.1"},
    },
}


def resolve_profile(name: str = "bookworm") -> dict:
    if name not in PROFILES:
        raise ValueError(f"unknown validation profile: {name}")
    return {"name": name, **PROFILES[name]}


def node_satisfies(version: str, requirement: str) -> bool:
    """Evaluate the Node engine forms used by upstream manifests."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version.strip())
    required = re.fullmatch(r"\s*(\^|>=)?\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*", requirement)
    if not match or not required:
        return False
    actual = tuple(map(int, match.groups()))
    minimum = tuple(int(value or 0) for value in required.groups()[1:])
    operator = required.group(1) or ""
    if operator == "^":
        return actual >= minimum and actual[0] == minimum[0]
    if operator == ">=":
        return actual >= minimum
    return actual == minimum
