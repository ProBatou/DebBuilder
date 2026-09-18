"""Internal, non-scheduled exact upstream detection for Recipe automation."""
from __future__ import annotations

from pathlib import Path
from contextlib import nullcontext

from . import github_client, recipe_store, source_acquisition, upstream_archive, upstream_artifact
from .automation_identity import (
    UpstreamIdentityError,
    identity_is_complete,
    release_asset_identity,
    source_archive_identity,
)
from .automation_ledger import AutomationLedger, AutomationLedgerError
from .build_store import canonical_recipe_sha256
from .recipe_schema import BUILTIN_RECIPE_ID, require_safe_name, validate_recipe_metadata


ERROR_CLASSIFICATIONS = {
    "github_rate_limited": "rate_limited",
    "github_unavailable": "upstream_unavailable",
    "github_api_error": "upstream_unavailable",
    "repository_not_found": "source_not_found",
    "release_not_found": "source_not_found",
    "release_asset_not_found": "explicit_asset_not_found",
    "ambiguous_release_asset": "ambiguous_asset",
    "ambiguous_archive_source": "ambiguous_asset",
    "unsupported_automation_source_mode": "unsupported_source",
    "unsupported_artifact_tracking": "unsupported_source",
    "incomplete_upstream_identity": "incomplete_identity",
    "invalid_upstream_identity": "incomplete_identity",
    "invalid_release_version": "invalid_configuration",
    "recipe_changed_during_detection": "recipe_changed",
    "invalid_automation_configuration": "invalid_configuration",
    "manual_action_required": "manual_action_required",
    "automation_ledger_bounds_exceeded": "capacity_exhausted",
    "automation_ledger_malformed": "automation_blocked",
    "automation_ledger_future_version": "automation_blocked",
    "automation_ledger_unavailable": "automation_blocked",
    "application_shutting_down": "shutting_down",
}


class UpstreamDetectionError(RuntimeError):
    """Bounded stable detection failure safe for internal orchestration results."""

    def __init__(self, code: str, message: str, *, retry_after_seconds: int | None = None, rate_limit_reset: int | None = None):
        super().__init__(message)
        self.code = code
        self.classification = ERROR_CLASSIFICATIONS.get(code, "upstream_unavailable")
        self.retry_after_seconds = retry_after_seconds
        self.rate_limit_reset = rate_limit_reset


def _convert_error(exc: Exception) -> UpstreamDetectionError:
    code = str(getattr(exc, "code", "github_api_error") or "github_api_error")
    if code == "unable_to_determine_version":
        code = "manual_action_required"
    messages = {
        "github_rate_limited": "GitHub rate limit prevents upstream detection",
        "github_unavailable": "GitHub is temporarily unavailable",
        "github_api_error": "GitHub upstream metadata is unavailable",
        "repository_not_found": "GitHub source was not found",
        "release_not_found": "GitHub source was not found",
        "release_asset_not_found": "Configured Release asset was not found",
        "ambiguous_release_asset": "Configured Release asset selector is ambiguous",
        "ambiguous_archive_source": "Configured Release source selection is ambiguous",
        "unsupported_automation_source_mode": "Recipe source mode cannot be detected before Build",
        "unsupported_artifact_tracking": "Recipe source tracking is unsupported for this artifact mode",
        "incomplete_upstream_identity": "GitHub metadata did not establish a complete immutable identity",
        "invalid_upstream_identity": "GitHub metadata did not establish a complete immutable identity",
        "invalid_release_version": "Recipe cannot derive a valid version from the selected source",
        "manual_action_required": "Recipe source version requires manual configuration",
        "recipe_changed_during_detection": "Recipe changed during upstream detection",
    }
    cursor = exc
    retry_after = None
    rate_limit_reset = None
    for _depth in range(6):
        retry_after = retry_after if retry_after is not None else getattr(cursor, "retry_after_seconds", None)
        rate_limit_reset = rate_limit_reset if rate_limit_reset is not None else getattr(cursor, "rate_limit_reset", None)
        cursor = getattr(cursor, "__cause__", None)
        if cursor is None:
            break
    return UpstreamDetectionError(
        code, messages.get(code, "Upstream detection failed"),
        retry_after_seconds=retry_after, rate_limit_reset=rate_limit_reset,
    )


def _complete(identity: dict) -> dict:
    if not identity_is_complete(identity):
        raise UpstreamDetectionError(
            "incomplete_upstream_identity", "GitHub metadata did not establish a complete immutable identity",
        )
    return identity


def detect_upstream(recipe_snapshot: dict, *, token: str = "") -> dict:
    """Resolve one canonical Recipe snapshot without lifecycle side effects."""
    try:
        recipe = validate_recipe_metadata(recipe_snapshot)
        if recipe["source"]["version"]["source"] == "build":
            raise UpstreamDetectionError(
                "unsupported_automation_source_mode", "Recipe source mode cannot be detected before Build",
            )
        mode = recipe["artifact"]["mode"]
        if mode == "source_build":
            resolved = source_acquisition.resolve_source(recipe, token=token, exact=True)
            identity = _complete(resolved["upstream_identity"])
            display = resolved["upstream_version"]
            return {"identity": identity, "display_version": display, "display_ref": resolved["ref"]}
        if mode == "upstream_deb":
            release = upstream_artifact.resolve_release(recipe, token=token)
            selected = upstream_artifact.select_asset(release, recipe["artifact"])
            identity = _complete(release_asset_identity(recipe, release, selected, "deb"))
            return {"identity": identity, "display_version": release["upstream_version"], "display_ref": release["ref"]}

        release = upstream_archive.resolve_release(recipe, token=token)
        selected = upstream_archive.select_archive(release, recipe["artifact"])
        if selected.get("source") == "release_asset":
            identity = release_asset_identity(
                recipe, release, selected, str(selected.get("payload_kind") or "archive"),
            )
        else:
            if recipe["source"]["tracking"] == "latest_release" and not release.get("commit"):
                target = github_client.resolve_ref(
                    release["repository"], release.get("tag") or release.get("ref"), kind="tag", token=token,
                )
                release = {**release, "commit": target["commit"], "ref_object_sha": target.get("ref_object_sha", "")}
            identity = source_archive_identity(
                recipe, release,
                generated_release=recipe["source"]["tracking"] == "latest_release",
                archive_format=str(selected.get("archive_format") or "tar.gz"),
            )
        return {
            "identity": _complete(identity),
            "display_version": release["upstream_version"],
            "display_ref": release["ref"],
        }
    except UpstreamDetectionError:
        raise
    except (github_client.GitHubError, source_acquisition.SourceError,
            upstream_archive.UpstreamArchiveError, upstream_artifact.UpstreamArtifactError,
            UpstreamIdentityError, KeyError, TypeError, ValueError) as exc:
        raise _convert_error(exc) from exc


class AutomationDetectionService:
    """Explicit internal check path; no scheduler, Run creation, or enqueueing."""

    def __init__(self, recipe_directory: str | Path, ledger: AutomationLedger):
        self.recipe_directory = Path(recipe_directory)
        self.ledger = ledger

    def _path(self, recipe_id: str) -> Path:
        require_safe_name(recipe_id, "Recipe ID")
        return self.recipe_directory / f"{recipe_id}.json"

    @staticmethod
    def _failure(recipe_id: str, recipe_sha256: str, policy: str, error: UpstreamDetectionError) -> dict:
        return {
            "recipe_id": recipe_id,
            "recipe_sha256": recipe_sha256,
            "policy": policy,
            "identity": None,
            "display_version": "",
            "display_ref": "",
            "classification": error.classification,
            "change": "none",
            "attempt_key": None,
            "generation": None,
            "attempt_state": None,
            "claim_eligible": False,
            "diagnostic": error.code,
            "retry_after_seconds": error.retry_after_seconds,
            "rate_limit_reset": error.rate_limit_reset,
        }

    def check(self, recipe_id: str, *, token: str = "", detector=detect_upstream, mutation_lease=None) -> dict:
        """Detect and atomically deduplicate one explicitly requested Recipe."""
        path = self._path(recipe_id)
        try:
            snapshot = recipe_store.load_recipe(path, write_back=False)
            canonical = validate_recipe_metadata(snapshot)
        except (OSError, TypeError, ValueError, recipe_store.RecipeStoreError):
            error = UpstreamDetectionError("invalid_automation_configuration", "Recipe is missing or invalid")
            return self._failure(recipe_id, "", "", error)
        recipe_sha = canonical_recipe_sha256(canonical)
        policy = canonical["automation"]["policy"]
        if canonical.get("name") != recipe_id:
            error = UpstreamDetectionError("invalid_automation_configuration", "Recipe identity is invalid")
            return self._failure(recipe_id, recipe_sha, policy, error)
        if (recipe_id == BUILTIN_RECIPE_ID or not canonical["active"] or
                not canonical["automation"]["enabled"] or policy == "manual"):
            code = "manual_action_required" if canonical["source"]["version"]["source"] == "build" else "invalid_automation_configuration"
            return self._failure(recipe_id, recipe_sha, policy, UpstreamDetectionError(code, "Recipe is not automation eligible"))
        try:
            detection = detector(canonical, token=token)
        except UpstreamDetectionError as exc:
            return self._failure(recipe_id, recipe_sha, policy, exc)
        try:
            lease = mutation_lease() if mutation_lease is not None else nullcontext()
            with lease:
                claim = self.ledger.claim_current_recipe(
                    path, recipe_id, detection["identity"], recipe_sha, policy,
                    detect_only=policy == "detect",
                )
        except (AutomationLedgerError, UpstreamIdentityError) as exc:
            return self._failure(recipe_id, recipe_sha, policy, _convert_error(exc))
        except RuntimeError as exc:
            if getattr(exc, "code", None) != "application_shutting_down":
                raise
            return self._failure(recipe_id, recipe_sha, policy, _convert_error(exc))
        entry = self.ledger.read()["attempts"][claim.attempt_key]
        row = entry["generations"][claim.generation]
        classification = "detected" if claim.created else "no_change"
        if not claim.created and row["state"] == "retry_delayed":
            classification = "retry_delayed"
        elif not claim.created and row["state"] == "blocked":
            classification = "blocked"
        elif not claim.created and row["state"] == "terminal":
            classification = "suppressed_terminal"
        return {
            "recipe_id": recipe_id,
            "recipe_sha256": recipe_sha,
            "policy": policy,
            "identity": entry["upstream_identity"],
            "display_version": str(detection.get("display_version") or "")[:200],
            "display_ref": str(detection.get("display_ref") or "")[:200],
            "classification": classification,
            "change": "new" if claim.created else "existing",
            "attempt_key": claim.attempt_key,
            "generation": claim.generation,
            "attempt_state": row["state"],
            "claim_eligible": self.ledger.may_create_run(claim.attempt_key, claim.generation),
            "diagnostic": None,
            "retry_after_seconds": None,
            "rate_limit_reset": None,
        }
