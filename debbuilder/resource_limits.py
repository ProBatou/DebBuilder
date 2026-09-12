"""Canonical per-Run resource policy and admission contracts."""
from __future__ import annotations

import copy
import os
from pathlib import Path


POLICY_SCHEMA_VERSION = 1
POLICY_SCOPE = "each_command_cgroup"
FIELDS = (
    "memory_max_bytes",
    "tasks_max",
    "cpu_quota_percent",
    "io_read_bandwidth_max_bytes_per_sec",
    "io_write_bandwidth_max_bytes_per_sec",
)
CONTROL_FOR_FIELD = {
    "memory_max_bytes": "memory",
    "tasks_max": "tasks",
    "cpu_quota_percent": "cpu",
    "io_read_bandwidth_max_bytes_per_sec": "io_read",
    "io_write_bandwidth_max_bytes_per_sec": "io_write",
}
JS_SAFE_INTEGER = (1 << 53) - 1
DBUS_UINT64_MAX = (1 << 64) - 1
MAXIMUMS = {
    **{field: JS_SAFE_INTEGER for field in FIELDS},
    "cpu_quota_percent": min(JS_SAFE_INTEGER, DBUS_UINT64_MAX // 10_000),
}


class ResourceLimitError(ValueError):
    """Structured refusal for an invalid or unenforceable resource policy."""

    def __init__(self, code: str, message: str, *, path: str = "$.resource_limits", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.path = path
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "path": self.path, "details": copy.deepcopy(self.details)}


def empty_policy() -> dict:
    return {field: None for field in FIELDS}


def normalize_policy(value, *, path: str = "$.resource_limits") -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ResourceLimitError("invalid_resource_limits", "Resource limits must be an object", path=path)
    unknown = sorted(set(value) - set(FIELDS))
    if unknown:
        child = f"{path}.{unknown[0]}"
        raise ResourceLimitError("unknown_resource_limit", f"Unknown resource limit field: {child}", path=child)
    result = empty_policy()
    for field in FIELDS:
        raw = value.get(field)
        if raw is None:
            continue
        child = f"{path}.{field}"
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ResourceLimitError(
                "invalid_resource_limit", f"{field} must be a positive integer or null", path=child,
            )
        if raw <= 0:
            raise ResourceLimitError(
                "invalid_resource_limit", f"{field} must be greater than zero or null", path=child,
            )
        if raw > MAXIMUMS[field]:
            reason = "cannot map exactly to CPUQuotaPerSecUSec" if field == "cpu_quota_percent" else "cannot round-trip exactly through JSON clients"
            raise ResourceLimitError(
                "resource_limit_out_of_range", f"{field} {reason}", path=child,
                details={"maximum": MAXIMUMS[field]},
            )
        result[field] = raw
    return result


def resolve_policy(global_policy, recipe_policy) -> tuple[dict, dict]:
    global_limits = normalize_policy(global_policy, path="$.settings.resource_limits")
    recipe_limits = normalize_policy(recipe_policy, path="$.recipe.resource_limits")
    effective, origins = empty_policy(), {}
    for field in FIELDS:
        global_value, recipe_value = global_limits[field], recipe_limits[field]
        if global_value is None and recipe_value is None:
            origins[field] = "unset"
        elif global_value is None:
            effective[field], origins[field] = recipe_value, "recipe"
        elif recipe_value is None:
            effective[field], origins[field] = global_value, "global"
        elif global_value < recipe_value:
            effective[field], origins[field] = global_value, "global"
        elif recipe_value < global_value:
            effective[field], origins[field] = recipe_value, "recipe"
        else:
            effective[field], origins[field] = global_value, "global_and_recipe"
    return effective, origins


def requested_controls(policy) -> list[str]:
    canonical = normalize_policy(policy)
    return [CONTROL_FOR_FIELD[field] for field in FIELDS if canonical[field] is not None]


def enforcement_required(policy) -> bool:
    return bool(requested_controls(policy))


def validate_host_enforceability(policy, *, path: str = "$.resource_limits") -> dict:
    """Reject host representations that systemd/cgroup would silently round to zero."""
    canonical = normalize_policy(policy, path=path)
    memory = canonical["memory_max_bytes"]
    if memory is not None:
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, TypeError, ValueError) as exc:
            raise ResourceLimitError(
                "resource_limits_unavailable",
                "host memory enforcement granularity cannot be determined",
                path=f"{path}.memory_max_bytes",
                details={"cause": type(exc).__name__},
            ) from exc
        if page_size <= 0:
            raise ResourceLimitError(
                "resource_limits_unavailable",
                "host memory enforcement granularity is invalid",
                path=f"{path}.memory_max_bytes",
                details={"reported_page_size": page_size},
            )
        if memory < page_size:
            raise ResourceLimitError(
                "resource_limits_unavailable",
                "memory_max_bytes is below this host's page-size enforcement granularity",
                path=f"{path}.memory_max_bytes",
                details={"minimum_enforceable_bytes": page_size},
            )
    return canonical


def cpu_quota_usec(percent: int) -> int:
    canonical = normalize_policy({"cpu_quota_percent": percent})
    value = canonical["cpu_quota_percent"]
    assert value is not None
    result = value * 10_000
    if result > DBUS_UINT64_MAX:
        raise ResourceLimitError(
            "resource_limit_out_of_range", "cpu_quota_percent cannot map to CPUQuotaPerSecUSec",
            path="$.resource_limits.cpu_quota_percent",
        )
    return result


def io_target_paths(workspace: str | Path) -> list[str]:
    """Return deterministic representatives for the root and workspace filesystems."""
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        raise ResourceLimitError(
            "resource_limits_unavailable", "The Run workspace filesystem cannot be resolved",
            details={"control": "io", "target": "workspace"},
        )
    targets: list[str] = []
    devices: set[int] = set()
    for path in (Path("/"), workspace):
        try:
            device = os.stat(path, follow_symlinks=False).st_dev
        except OSError as exc:
            raise ResourceLimitError(
                "resource_limits_unavailable", "An I/O limit target filesystem cannot be resolved",
                details={"control": "io", "target": str(path), "cause": type(exc).__name__},
            ) from exc
        if device not in devices:
            devices.add(device)
            targets.append(str(path))
    return targets


def admission_contract(global_policy, recipe_policy, capability: dict) -> dict:
    global_limits = normalize_policy(global_policy, path="$.settings.resource_limits")
    recipe_limits = normalize_policy(recipe_policy, path="$.recipe.resource_limits")
    effective, origins = resolve_policy(global_limits, recipe_limits)
    return {
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "scope": POLICY_SCOPE,
        "global": global_limits,
        "recipe": recipe_limits,
        "effective": effective,
        "origins": origins,
        "enforcement_required": enforcement_required(effective),
        "admission_capability": copy.deepcopy(capability),
    }


def historical_contract() -> dict:
    return admission_contract(empty_policy(), empty_policy(), {
        "backend": "historical",
        "available": True,
        "requested_controls": [],
        "reason": "resource policy was not evaluated for this historical Run",
    })


def default_contract() -> dict:
    return admission_contract(empty_policy(), empty_policy(), {
        "backend": "not_evaluated",
        "available": True,
        "requested_controls": [],
        "reason": "no finite resource control was requested",
    })


def validate_contract(value, *, path: str = "$.resource_limits") -> dict:
    if not isinstance(value, dict):
        raise ResourceLimitError("invalid_resource_contract", "Run resource policy must be an object", path=path)
    required = {
        "policy_schema_version", "scope", "global", "recipe", "effective", "origins",
        "enforcement_required", "admission_capability",
    }
    if set(value) != required:
        raise ResourceLimitError("invalid_resource_contract", "Run resource policy has invalid fields", path=path)
    if value.get("policy_schema_version") != POLICY_SCHEMA_VERSION or value.get("scope") != POLICY_SCOPE:
        raise ResourceLimitError("invalid_resource_contract", "Run resource policy version or scope is unsupported", path=path)
    for policy_field in ("global", "recipe", "effective"):
        if not isinstance(value.get(policy_field), dict):
            raise ResourceLimitError(
                "invalid_resource_contract", "Run resource policies must be objects",
                path=f"{path}.{policy_field}",
            )
    global_limits = normalize_policy(value.get("global"), path=f"{path}.global")
    recipe_limits = normalize_policy(value.get("recipe"), path=f"{path}.recipe")
    effective, origins = resolve_policy(global_limits, recipe_limits)
    if value.get("effective") != effective or value.get("origins") != origins:
        raise ResourceLimitError("invalid_resource_contract", "Run effective resource policy does not match its inputs", path=path)
    if value.get("enforcement_required") is not enforcement_required(effective):
        raise ResourceLimitError("invalid_resource_contract", "Run resource enforcement requirement is inconsistent", path=path)
    capability = value.get("admission_capability")
    if (
        not isinstance(capability, dict)
        or set(capability) != {"backend", "available", "requested_controls", "reason"}
        or capability.get("backend") not in {"systemd_cgroup", "process_group", "historical", "not_evaluated"}
        or not isinstance(capability.get("available"), bool)
        or not isinstance(capability.get("reason"), str)
        or len(capability["reason"]) > 500
    ):
        raise ResourceLimitError("invalid_resource_contract", "Run admission capability is invalid", path=f"{path}.admission_capability")
    if value["enforcement_required"] and not capability["available"]:
        raise ResourceLimitError("invalid_resource_contract", "A finite Run policy lacks admission capability", path=path)
    if value["enforcement_required"] and capability["backend"] != "systemd_cgroup":
        raise ResourceLimitError(
            "invalid_resource_contract",
            "A finite Run policy requires authenticated systemd/cgroup admission capability",
            path=f"{path}.admission_capability.backend",
        )
    if capability.get("requested_controls") != requested_controls(effective):
        raise ResourceLimitError(
            "invalid_resource_contract",
            "Run admission capability does not match the effective resource policy",
            path=f"{path}.admission_capability.requested_controls",
        )
    return copy.deepcopy(value)
