"""Deterministic, bounded ZIP assembly of existing safe operator projections.

This module deliberately has no access to application stores or probes. Callers
must supply the canonical diagnostics and optional inspector results.
"""
from __future__ import annotations

import io
import json
import zipfile

from . import __version__
from .inspectors import RECIPE_INSPECTION_VERSION, RUN_INSPECTION_VERSION
from .system_diagnostics import CHECK_IDS, SCHEMA_VERSION_DIAGNOSTICS


SCHEMA_VERSION = 1
MAX_ENTRY_BYTES = 512 * 1024
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024
MAX_BUNDLE_BYTES = MAX_UNCOMPRESSED_BYTES + 4096
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class SupportBundleError(ValueError):
    """The supplied projections cannot be assembled into a safe bundle."""


def _json_bytes(value: dict) -> bytes:
    if not isinstance(value, dict):
        raise SupportBundleError("Invalid support projection")
    try:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SupportBundleError("Invalid support projection") from exc
    if len(payload) > MAX_ENTRY_BYTES:
        raise SupportBundleError("Support projection exceeds the size limit")
    return payload


def _check_projection(value: dict, *, kind: str, version: int, fields: set[str]) -> None:
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != version:
        raise SupportBundleError("Invalid support projection")
    if set(value) != fields:
        raise SupportBundleError("Invalid support projection")
    if kind == "diagnostics":
        checks = value["checks"]
        if not isinstance(checks, list) or [row.get("id") if isinstance(row, dict) else None for row in checks] != list(CHECK_IDS):
            raise SupportBundleError("Invalid support projection")


def build_support_bundle(*, diagnostics: dict, recipe_inspection: dict | None = None,
                         run_inspection: dict | None = None) -> bytes:
    """Assemble at most one Recipe and Run inspection with system diagnostics."""
    _check_projection(diagnostics, kind="diagnostics", version=SCHEMA_VERSION_DIAGNOSTICS,
                      fields={"schema_version", "status", "checks"})
    if recipe_inspection is not None:
        _check_projection(recipe_inspection, kind="recipe", version=RECIPE_INSPECTION_VERSION,
                          fields={"schema_version", "counts_truncated", "identity", "source", "build", "artifact", "installation", "service", "automation", "observation"})
    if run_inspection is not None:
        _check_projection(run_inspection, kind="run", version=RUN_INSPECTION_VERSION,
                          fields={"schema_version", "identity", "lifecycle", "artifact", "validation", "publication", "execution", "error"})

    names = ["manifest.json", "system-diagnostics.json"]
    if recipe_inspection is not None:
        names.append("recipe-inspection.json")
    if run_inspection is not None:
        names.append("run-inspection.json")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "application": {"name": "DebBuilder", "version": __version__},
        "contents": names,
        "selection": {"recipe": recipe_inspection is not None, "run": run_inspection is not None},
    }
    entries = [("manifest.json", manifest), ("system-diagnostics.json", diagnostics)]
    if recipe_inspection is not None:
        entries.append(("recipe-inspection.json", recipe_inspection))
    if run_inspection is not None:
        entries.append(("run-inspection.json", run_inspection))
    encoded = [(name, _json_bytes(value)) for name, value in entries]
    if len(encoded) > 4 or sum(len(data) for _, data in encoded) > MAX_UNCOMPRESSED_BYTES:
        raise SupportBundleError("Support bundle exceeds the size limit")

    output = io.BytesIO()
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
            for name, data in encoded:
                info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, data)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        raise SupportBundleError("Support bundle cannot be assembled") from exc
    result = output.getvalue()
    if len(result) > MAX_BUNDLE_BYTES:
        raise SupportBundleError("Support bundle exceeds the size limit")
    return result
