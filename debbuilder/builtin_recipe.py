"""Ownership and upgrade rules for DebBuilder's application-managed Recipe."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path

from . import recipe_store, storage
from .recipe_schema import RecipeDocumentError, recipe_document_for_storage


BUILTIN_RECIPE_ID = "debbuilder"
BUILTIN_RECIPE_FILENAME = f"{BUILTIN_RECIPE_ID}.json"
BUILTIN_RECIPE_PATH = Path(__file__).with_name("builtin_recipes") / BUILTIN_RECIPE_FILENAME

# Every effective Recipe field not listed here is application-managed. This
# closed allowlist makes newly introduced fields managed by default.
OPERATOR_OVERRIDE_PATHS = (
    "active",
    "package.maintainer",
    "build.environment",
    "build.inactivity_timeout",
    "build.maximum_runtime",
    "resource_limits",
)
APPLICATION_MANAGED_AREAS = (
    "name and package identity/versioning/dependencies",
    "source repository/tracking/version selection",
    "artifact and build plan/output selection",
    "installation layout/configuration/maintainer scripts",
    "systemd service definition",
)


class BuiltinRecipeError(ValueError):
    """Structured ownership or definition-upgrade refusal."""

    def __init__(self, code: str, message: str, *, path: str = "$", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.path = path
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "path": self.path, "details": self.details}


@dataclass(frozen=True)
class BuiltinReconciliationResult:
    action: str
    recipe: dict
    definition_version: int
    previous_definition_version: int | None


def is_builtin_recipe_id(recipe_id: str) -> bool:
    return recipe_id == BUILTIN_RECIPE_ID


def ui_projection(recipe: dict) -> dict:
    """Return additive UI policy metadata for an application-managed Recipe."""
    management = recipe.get("management")
    if not isinstance(management, dict) or management.get("owner") != "application":
        return {}
    if management.get("builtin_id") != BUILTIN_RECIPE_ID or recipe.get("name") != BUILTIN_RECIPE_ID:
        return {}
    return {
        "source": "builtin",
        "managed": True,
        "editable_paths": list(OPERATOR_OVERRIDE_PATHS),
    }


def require_user_recipe_id(recipe_id: str) -> None:
    if is_builtin_recipe_id(recipe_id):
        raise BuiltinRecipeError(
            "builtin_recipe_reserved",
            "The debbuilder Recipe ID is reserved for the application-managed built-in Recipe",
            path="$.name",
        )


def _metadata(recipe: dict) -> dict:
    management = recipe.get("management")
    if not isinstance(management, dict):
        raise BuiltinRecipeError(
            "builtin_recipe_invalid", "Canonical built-in Recipe lacks management metadata",
            path="$.management",
        )
    return management


def load_builtin_definition(path: Path = BUILTIN_RECIPE_PATH) -> dict:
    """Load and verify the packaged canonical built-in definition."""
    try:
        definition = recipe_store.load_recipe(path, write_back=False)
    except recipe_store.RecipeStoreError as exc:
        raise BuiltinRecipeError(
            "builtin_recipe_invalid", "Packaged DebBuilder Recipe definition is invalid",
            path=exc.path, details={"cause_code": exc.code},
        ) from exc
    management = _metadata(definition)
    if (
        definition.get("name") != BUILTIN_RECIPE_ID
        or management.get("owner") != "application"
        or management.get("builtin_id") != BUILTIN_RECIPE_ID
        or management.get("operator_overrides") != {}
    ):
        raise BuiltinRecipeError(
            "builtin_recipe_invalid",
            "Packaged DebBuilder Recipe must have stable identity and no operator overrides",
            path="$.management",
        )
    return definition


def _value(document: dict, path: str):
    value = document
    for segment in path.split("."):
        value = value[segment]
    return value


def _set_value(document: dict, path: str, value) -> None:
    segments = path.split(".")
    target = document
    for segment in segments[:-1]:
        target = target.setdefault(segment, {})
    target[segments[-1]] = copy.deepcopy(value)


def _extract_operator_overrides(recipe: dict, definition: dict) -> dict:
    overrides: dict = {}
    for path in OPERATOR_OVERRIDE_PATHS:
        value = _value(recipe, path)
        if path == "package.maintainer" and not value.strip():
            continue
        if value != _value(definition, path):
            _set_value(overrides, path, value)
    return overrides


def effective_builtin_recipe(overrides: dict, *, definition: dict | None = None) -> dict:
    """Apply the closed operator allowlist to the canonical application base."""
    canonical_definition = copy.deepcopy(definition or load_builtin_definition())
    base = copy.deepcopy(canonical_definition)
    metadata = _metadata(base)
    metadata["operator_overrides"] = copy.deepcopy(overrides)
    try:
        declared = recipe_document_for_storage(base)
    except RecipeDocumentError as exc:
        code = exc.code if exc.code == "builtin_recipe_upgrade_required" else "builtin_recipe_override_invalid"
        raise BuiltinRecipeError(code, str(exc), path=exc.path) from exc
    normalized_overrides = declared["management"]["operator_overrides"]
    base = copy.deepcopy(canonical_definition)
    _metadata(base)["operator_overrides"] = copy.deepcopy(normalized_overrides)
    for path in OPERATOR_OVERRIDE_PATHS:
        try:
            value = _value(normalized_overrides, path)
        except KeyError:
            continue
        _set_value(base, path, value)
    try:
        return recipe_document_for_storage(base)
    except RecipeDocumentError as exc:
        code = exc.code if exc.code == "builtin_recipe_upgrade_required" else "builtin_recipe_override_invalid"
        raise BuiltinRecipeError(code, str(exc), path=exc.path) from exc


def _load_persisted_for_reconciliation(path: Path) -> dict:
    try:
        result = recipe_store.load_recipe_result(path, write_back=False)
    except recipe_store.RecipeStoreError as exc:
        if exc.code in {
            "builtin_recipe_invalid", "builtin_recipe_override_invalid", "builtin_recipe_upgrade_required",
        }:
            raise BuiltinRecipeError(exc.code, str(exc), path=exc.path) from exc
        raise BuiltinRecipeError(
            "builtin_recipe_adoption_failed",
            "Existing debbuilder Recipe cannot be safely adopted",
            path=exc.path,
            details={"cause_code": exc.code},
        ) from exc
    if result.source_version < 2 and "management" in result.recipe:
        raise BuiltinRecipeError(
            "builtin_recipe_adoption_failed",
            "Historical Recipe unexpectedly contains built-in management metadata",
            path="$.management",
        )
    return result.recipe


def reconcile_builtin_recipe(
    workflows_directory: Path,
    *,
    definition_path: Path = BUILTIN_RECIPE_PATH,
) -> BuiltinReconciliationResult:
    """Seed, safely adopt, or upgrade the persisted DebBuilder Recipe."""
    definition = load_builtin_definition(definition_path)
    current_version = _metadata(definition)["definition_version"]
    destination = Path(workflows_directory) / BUILTIN_RECIPE_FILENAME
    with storage.locked_path(destination):
        try:
            destination.lstat()
        except FileNotFoundError:
            stored = recipe_store.save_recipe(destination, definition)
            return BuiltinReconciliationResult("seeded", stored, current_version, None)

        existing = _load_persisted_for_reconciliation(destination)
        if existing.get("name") != BUILTIN_RECIPE_ID:
            raise BuiltinRecipeError(
                "builtin_recipe_adoption_failed",
                "Existing reserved Recipe file has a mismatched Recipe identity",
                path="$.name",
            )
        existing_management = existing.get("management")
        if existing_management is None:
            overrides = _extract_operator_overrides(existing, definition)
            effective = effective_builtin_recipe(overrides, definition=definition)
            stored = recipe_store.save_recipe(destination, effective)
            return BuiltinReconciliationResult("adopted", stored, current_version, None)

        previous_version = existing_management["definition_version"]
        if previous_version > current_version:
            raise BuiltinRecipeError(
                "builtin_recipe_upgrade_required",
                "Persisted built-in Recipe uses a newer definition version",
                path="$.management.definition_version",
                details={"stored_definition_version": previous_version, "current_definition_version": current_version},
            )
        overrides = existing_management["operator_overrides"]
        effective = effective_builtin_recipe(overrides, definition=definition)
        if previous_version < current_version:
            action = "upgraded"
        elif existing != effective:
            action = "reconciled"
        else:
            action = "current"
        stored = recipe_store.save_recipe(destination, effective)
        return BuiltinReconciliationResult(action, stored, current_version, previous_version)


def _managed_projection(recipe: dict) -> dict:
    projected = copy.deepcopy(recipe)
    projected.pop("management", None)
    projected.pop("active", None)
    projected.pop("resource_limits", None)
    projected.get("package", {}).pop("maintainer", None)
    build = projected.get("build", {})
    for key in ("environment", "inactivity_timeout", "maximum_runtime"):
        build.pop(key, None)
    return projected


def _first_difference(left, right, path: str = "$") -> str:
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}"
            if key not in left or key not in right:
                return child
            difference = _first_difference(left[key], right[key], child)
            if difference:
                return difference
        return ""
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return path
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            difference = _first_difference(left_item, right_item, f"{path}[{index}]")
            if difference:
                return difference
        return ""
    return "" if left == right else path


def update_builtin_recipe(
    path: Path,
    candidate: dict,
    *,
    definition_path: Path = BUILTIN_RECIPE_PATH,
) -> dict:
    """Persist only approved operator edits to an already managed built-in."""
    definition = load_builtin_definition(definition_path)
    path = Path(path)
    with storage.locked_path(path):
        try:
            path.lstat()
        except FileNotFoundError as exc:
            raise BuiltinRecipeError(
                "builtin_recipe_reserved",
                "The debbuilder Recipe ID can only be created by built-in reconciliation",
                path="$.name",
            ) from exc
        existing = _load_persisted_for_reconciliation(path)
        management = existing.get("management")
        if management is None:
            raise BuiltinRecipeError(
                "builtin_recipe_reserved",
                "The historical debbuilder Recipe must be reconciled before it can be edited",
                path="$.name",
            )
        current_definition_version = _metadata(definition)["definition_version"]
        if management["definition_version"] > current_definition_version:
            raise BuiltinRecipeError(
                "builtin_recipe_upgrade_required",
                "Persisted built-in Recipe uses a newer definition version",
                path="$.management.definition_version",
                details={
                    "stored_definition_version": management["definition_version"],
                    "current_definition_version": current_definition_version,
                },
            )
        current = effective_builtin_recipe(management["operator_overrides"], definition=definition)
        try:
            canonical_candidate = recipe_document_for_storage(candidate)
        except RecipeDocumentError as exc:
            raise BuiltinRecipeError("builtin_recipe_override_invalid", str(exc), path=exc.path) from exc
        candidate_management = canonical_candidate.get("management")
        if candidate_management is not None and candidate_management != current["management"]:
            raise BuiltinRecipeError(
                "builtin_recipe_managed_field", "Built-in management metadata cannot be edited",
                path="$.management",
            )
        difference = _first_difference(_managed_projection(canonical_candidate), _managed_projection(current))
        if difference:
            raise BuiltinRecipeError(
                "builtin_recipe_managed_field",
                "Application-managed DebBuilder Recipe fields cannot be edited",
                path=difference,
            )
        overrides = _extract_operator_overrides(canonical_candidate, definition)
        effective = effective_builtin_recipe(overrides, definition=definition)
        return recipe_store.save_recipe(path, effective)
