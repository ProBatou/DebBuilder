"""Controlled validation environments and runtime capability checks."""
from __future__ import annotations

import re


PROFILES = {
    "bookworm": {"image": "debbuilder-validation:bookworm", "capabilities": {"python": "3.11"}},
    "bookworm-node22": {
        "image": "debbuilder-validation:bookworm-node22",
        "capabilities": {"node": "22.22.1", "python": "3.11"},
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


def python_satisfies(version: str, requirement: str) -> bool:
    """Evaluate common requires-python and Poetry constraint forms."""
    match = re.search(r"(?:Python\s+)?(\d+)\.(\d+)(?:\.(\d+))?", version.strip())
    if not match:
        return False
    actual = tuple(int(value or 0) for value in match.groups())

    def parsed(value: str) -> tuple[int, int, int] | None:
        found = re.fullmatch(r"\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*", value)
        return tuple(int(item or 0) for item in found.groups()) if found else None

    for raw in requirement.split(","):
        clause = raw.strip()
        if not clause:
            continue
        found = re.fullmatch(r"(\^|~=|==|>=|<=|>|<)?\s*(\d+(?:\.\d+){0,2})", clause)
        if not found:
            return False
        operator, raw_version = found.groups()
        expected = parsed(raw_version)
        if expected is None:
            return False
        if operator == ">=" and not actual >= expected:
            return False
        if operator == ">" and not actual > expected:
            return False
        if operator == "<=" and not actual <= expected:
            return False
        if operator == "<" and not actual < expected:
            return False
        if operator in {None, "=="}:
            precision = len(raw_version.split("."))
            if actual[:precision] != expected[:precision]:
                return False
        if operator in {"^", "~="}:
            if actual < expected:
                return False
            parts = raw_version.split(".")
            if operator == "^":
                nonzero = next((index for index, value in enumerate(expected) if value), 2)
                upper = list(expected)
                upper[nonzero] += 1
                upper[nonzero + 1:] = [0] * (2 - nonzero)
            elif len(parts) <= 2:
                upper = [expected[0] + 1, 0, 0]
            else:
                upper = [expected[0], expected[1] + 1, 0]
            if actual >= tuple(upper):
                return False
    return True
