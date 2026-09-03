"""Merge the live APT repository index into the package inventory.

The application historically used the live index only to refresh versions of
packages already present in its local inventory.  A repository can legitimately
contain packages that have no local recipe yet; those packages must still be
visible in the Packages page and dashboard.
"""
from __future__ import annotations


def merge_live_packages(packages: list[dict], live_rows: list[dict], package_from_inventory) -> list[dict]:
    """Return inventory rows augmented with packages found only in APT.

    Existing local rows win for source/recipe metadata.  Live-only packages are
    converted with the same inventory adapter used by the application, so they
    naturally appear as ``recipe_missing`` until a recipe is associated.
    """
    by_name = {row.get("name"): row for row in packages if row.get("name")}
    for live in live_rows:
        name = live.get("Package")
        if not name or name in by_name:
            continue
        row = package_from_inventory(live)
        row["source"] = {"type": "apt-repository"}
        by_name[name] = row
    return list(by_name.values())


def install(app_module) -> None:
    """Install the live-inventory wrapper on an imported app module."""
    original = app_module.list_packages

    def list_packages_with_live_inventory() -> list[dict]:
        # Keep the application's existing enrichment/lifecycle logic for local
        # packages, then add live-only packages using the same enrichment model.
        current = original()
        live_rows = app_module.live_published_index()
        current_names = {p.get("name") for p in current}
        apt = app_module.repo_settings()
        for live in live_rows:
            name = live.get("Package")
            if not name or name in current_names:
                continue
            base = app_module.package_from_inventory(live)
            base["source"] = {"type": "apt-repository"}
            item = app_module.package_store.enrich_package(
                base,
                published_version=live.get("Version") or "",
                source_version="",
            )
            item["repository"].update({
                "url": apt["repository"],
                "distribution": apt["distribution"],
                "component": apt["component"],
            })
            current.append(item)
        return sorted(current, key=lambda p: p.get("name", ""))

    app_module.list_packages = list_packages_with_live_inventory
