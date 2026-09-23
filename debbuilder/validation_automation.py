"""Canonical durable metadata contract for validation automation."""
from __future__ import annotations

import re


PUBLICATION_NOT_REQUESTED = "not_requested"
PUBLICATION_PENDING = "pending"
PUBLICATION_COMPLETE = "complete"
PUBLICATION_CANCELLED = "cancelled"


def normalize_validation_automation(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("validation automation metadata is invalid")
    required = {"automatic", "publish_after_success", "publication_state"}
    optional = {"attempt_key", "generation", "policy"}
    if not required.issubset(value) or set(value) - required - optional:
        raise ValueError("validation automation metadata is invalid")
    coordination_fields = {"attempt_key", "generation", "policy"}
    present = coordination_fields & set(value)
    if present and present != coordination_fields:
        raise ValueError("validation automation metadata is invalid")
    if not all(isinstance(value.get(key), bool) for key in ("automatic", "publish_after_success")):
        raise ValueError("validation automation metadata is invalid")
    automatic = value["automatic"] is True
    publish_after_success = value["publish_after_success"] is True
    if publish_after_success and not automatic:
        raise ValueError("validation automation metadata is invalid")
    publication_state = value["publication_state"]
    if publication_state not in {
        PUBLICATION_NOT_REQUESTED,
        PUBLICATION_PENDING,
        PUBLICATION_COMPLETE,
        PUBLICATION_CANCELLED,
    }:
        raise ValueError("validation automation metadata is invalid")
    if (not publish_after_success and publication_state != PUBLICATION_NOT_REQUESTED) or (
        publish_after_success and publication_state == PUBLICATION_NOT_REQUESTED
    ):
        raise ValueError("validation automation metadata is invalid")
    result = {
        "automatic": automatic,
        "publish_after_success": publish_after_success,
        "publication_state": publication_state,
    }
    if present:
        attempt_key = value.get("attempt_key")
        generation = value.get("generation")
        policy = value.get("policy")
        if (
            not automatic
            or not isinstance(attempt_key, str)
            or not re.fullmatch(r"automation-v1-[0-9a-f]{64}", attempt_key)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or not 0 <= generation < 8
            or policy not in {"build_validate", "full"}
            or publish_after_success != (policy == "full")
        ):
            raise ValueError("validation automation coordination metadata is invalid")
        result.update({"attempt_key": attempt_key, "generation": generation, "policy": policy})
    return result
