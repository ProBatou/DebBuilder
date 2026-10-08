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
        "filesystems": {
            "builds": {
                "measurement_state": "ready", "pressure_state": "normal",
                "measured_scope": "builds", "device_id": 7,
                "total_bytes": 1000, "used_bytes": 400, "free_bytes": 600,
                "available_bytes": 550, "available_percent": 55.0,
                "utilized_percent": 45.0, "effective_start_bytes": 100,
                "effective_target_bytes": 150,
            },
            "repository": {
                "measurement_state": "ready", "pressure_state": "normal",
                "same_as_builds": True, "device_id": 7,
            },
        },
        "categories": {
            "metadata": 10, "logs_manifests": 20, "artifacts": 30,
            "validation_previous": 5, "disposable": 15, "cache": 0, "unknown": 20,
        },
        "runs": {
            "count": 2, "by_mode": {"build": 1, "dry_run": 1},
            "by_status": {"success": 1, "failed": 1}, "failed_count": 1,
            "test_count": 1, "artifact_count": 1, "artifact_bytes": 30, "largest": [],
        },
        "recent_workspace_cleanups": {
            "entries": [{
                "run_id": "failed-run", "reason": "storage_pressure",
                "cleaned_at": "2026-09-08T12:00:00+00:00",
                "removed": ["source", "downloads"],
            }],
            "total_marked": 1,
            "omitted": 0,
        },
        "retention_policy": {
            "enabled": True,
            "failed_workspaces_to_retain": 5,
            "pressure_minimum_free_bytes": 536_870_912,
            "pressure_minimum_free_percent": 10,
            "pressure_target_free_bytes": 1_073_741_824,
            "pressure_target_free_percent": 15,
            "startup_destructive_cleanup": True,
            "periodic_destructive_cleanup": True,
            "cleanup_interval_seconds": 300,
            "lifecycle_destructive_cleanup": True,
        },
    }


class FakeInventory:
    def __init__(self, value):
        self.value = deepcopy(value)
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        return deepcopy(self.value)

    def collect(self, *args, **kwargs):
        raise AssertionError("GET /api/storage must not collect storage")

    def collect_capacity(self, *args, **kwargs):
        raise AssertionError("GET /api/storage must not measure capacity")


class StorageApiTests(AdminApiCase):
    def test_get_storage_returns_cached_snapshot_without_walk_or_write(self):
        inventory = FakeInventory(snapshot())
        self.httpd.storage_inventory = inventory
        with mock.patch("debbuilder.storage_inventory.collect_storage_snapshot") as collect, \
                mock.patch("debbuilder.storage.atomic_write_text") as write, \
                mock.patch("debbuilder.storage_inventory.os.open") as marker_open, \
                mock.patch("debbuilder.storage_inventory.os.scandir") as scandir, \
                mock.patch("debbuilder.storage_inventory.os.walk") as walk, \
                mock.patch("debbuilder.workspace_cleanup.read_json") as read_json, \
                mock.patch("debbuilder.storage_inventory._read_workspace_cleanup_marker") as marker_read, \
                mock.patch("debbuilder.app.request_maintenance") as maintenance:
            status, response = self.request("GET", "/api/storage")

        self.assertEqual(status, 200)
        self.assertEqual(response["storage"]["bytes"]["managed_total"], 123)
        self.assertEqual(response["storage"]["categories"]["cache"], 0)
        self.assertEqual(response["storage"]["filesystems"]["builds"]["available_bytes"], 550)
        self.assertTrue(response["storage"]["filesystems"]["repository"]["same_as_builds"])
        self.assertEqual(
            response["storage"]["retention_policy"]["pressure_target_free_percent"], 15,
        )
        self.assertEqual(
            response["storage"]["recent_workspace_cleanups"]["entries"][0]["reason"],
            "storage_pressure",
        )
        for future_field in ("admission_blocked", "last_pressure_cleanup", "recovered_bytes"):
            self.assertNotIn(future_field, response["storage"])
        self.assertTrue(response["storage"]["retention_policy"]["periodic_destructive_cleanup"])
        self.assertEqual(response["storage"]["retention_policy"]["cleanup_interval_seconds"], 300)
        self.assertEqual(inventory.snapshot_calls, 1)
        collect.assert_not_called()
        marker_open.assert_not_called()
        scandir.assert_not_called()
        walk.assert_not_called()
        read_json.assert_not_called()
        marker_read.assert_not_called()
        write.assert_not_called()
        maintenance.assert_not_called()

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
