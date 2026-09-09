from copy import deepcopy
from unittest import mock

import debbuilder.app as server
from tests.admin_api_case import AdminApiCase


def snapshot(state="ready"):
    return {
        "state": state,
        "measured_at": "2026-09-08T12:00:00+00:00" if state != "collecting" else None,
        "last_successful_measurement": "2026-09-08T12:00:00+00:00" if state not in {"collecting", "error"} else None,
        "partial": state != "ready",
        "diagnostics": ["bounded diagnostic"] if state in {"partial", "stale", "error"} else [],
        "roots": {"data_root": "/data", "repository_root": "/repo", "repository_within_data": False},
        "bytes": {"managed_total": 123, "data_root": 100, "repository": 23},
        "categories": {
            "metadata": 10, "logs_manifests": 20, "artifacts": 30,
            "validation_previous": 5, "disposable": 15, "cache": 10, "unknown": 10,
        },
        "runs": {
            "count": 2, "by_mode": {"build": 1, "dry_run": 1},
            "by_status": {"success": 1, "failed": 1}, "failed_count": 1,
            "test_count": 1, "artifact_count": 1, "artifact_bytes": 30, "largest": [],
        },
        "retention_policy": {"periodic_destructive_cleanup": False},
    }


class FakeInventory:
    def __init__(self, value):
        self.value = deepcopy(value)
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        return deepcopy(self.value)


class StorageApiTests(AdminApiCase):
    def test_get_storage_returns_cached_snapshot_without_walk_or_write(self):
        inventory = FakeInventory(snapshot())
        self.httpd.storage_inventory = inventory
        with mock.patch("debbuilder.storage_inventory.collect_storage_snapshot") as collect, \
                mock.patch("debbuilder.storage.atomic_write_text") as write, \
                mock.patch("debbuilder.storage_inventory.os.scandir") as walk:
            status, response = self.request("GET", "/api/storage")

        self.assertEqual(status, 200)
        self.assertEqual(response["storage"]["bytes"]["managed_total"], 123)
        self.assertEqual(inventory.snapshot_calls, 1)
        collect.assert_not_called()
        walk.assert_not_called()
        write.assert_not_called()

    def test_get_storage_exposes_all_cached_states(self):
        for state in ("collecting", "ready", "partial", "stale", "error"):
            with self.subTest(state=state):
                self.httpd.storage_inventory = FakeInventory(snapshot(state))
                status, response = self.request("GET", "/api/storage")
                self.assertEqual(status, 200)
                self.assertEqual(response["storage"]["state"], state)

    def test_storage_remains_readable_with_global_recovery_blocker(self):
        self.httpd.cleanup_authorization.update_global_blocker({
            "code": "execution_recovery_unresolved",
            "message": "Build/Test blocked by recovery",
        })
        self.httpd.storage_inventory = FakeInventory(snapshot("partial"))

        status, response = self.request("GET", "/api/storage")

        self.assertEqual(status, 200)
        self.assertEqual(response["storage"]["state"], "partial")


if __name__ == "__main__":
    import unittest
    unittest.main()
