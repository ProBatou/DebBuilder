import json
import tempfile
import threading
import unittest
from pathlib import Path

from debbuilder import app
from debbuilder import storage
from debbuilder.package_service import PackageService
from debbuilder.runtime import RuntimeConfig
from debbuilder.settings_store import load_secrets, save_github_token, save_ntfy_token


class RuntimeConfigTests(unittest.TestCase):
    def test_all_runtime_paths_and_network_defaults_come_from_one_environment(self):
        config = RuntimeConfig.from_environment(Path("/opt/demo"), {
            "DEBBUILDER_DATA_DIR": "/srv/debbuilder/data",
            "DEBBUILDER_REPO_ROOT": "/srv/debbuilder/repository",
            "DEBBUILDER_REPO_URL": "https://apt.example.test",
            "DEBBUILDER_SUITE": "testing",
            "DEBBUILDER_HOST": "127.0.0.2",
            "DEBBUILDER_PORT": "9000",
        })
        self.assertEqual(config.data, Path("/srv/debbuilder/data"))
        self.assertEqual(config.workflows, Path("/srv/debbuilder/data/workflows"))
        self.assertEqual(config.builds, Path("/srv/debbuilder/data/builds"))
        self.assertEqual(config.repository_root, Path("/srv/debbuilder/repository"))
        self.assertEqual((config.repository_url, config.suite), ("https://apt.example.test", "testing"))
        self.assertEqual((config.host, config.port), ("127.0.0.2", 9000))

    def test_notification_service_tracks_active_data_directory(self):
        old_data = app.DATA
        old_service = app.NOTIFICATION_SERVICE
        try:
            with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
                app.DATA = Path(first)
                first_service = app.notification_service()
                app.DATA = Path(second)
                second_service = app.notification_service()

                self.assertIsNot(first_service, second_service)
                self.assertEqual(second_service.data_dir, Path(second))
        finally:
            app.DATA = old_data
            app.NOTIFICATION_SERVICE = old_service


class AtomicStorageTests(unittest.TestCase):
    def test_parallel_json_replacements_never_expose_partial_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            errors = []

            def writer(index):
                try:
                    for revision in range(20):
                        storage.save_json(path, {"writer": index, "revision": revision, "payload": "x" * 4096})
                        json.loads(path.read_text())
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(index,)) for index in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(json.loads(path.read_text())["payload"]), 4096)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_secret_read_modify_write_is_serialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            barrier = threading.Barrier(2)

            def github():
                barrier.wait()
                save_github_token(data, "ghp_abcdefghijklmnopqrstuvwxyz123456")

            def ntfy():
                barrier.wait()
                save_ntfy_token(data, "ntfy-secret-value")

            first = threading.Thread(target=github)
            second = threading.Thread(target=ntfy)
            first.start()
            second.start()
            first.join()
            second.join()
            secrets = load_secrets(data)
            self.assertEqual(secrets["github"]["token"], "ghp_abcdefghijklmnopqrstuvwxyz123456")
            self.assertEqual(secrets["notifications"]["token"], "ntfy-secret-value")

    def test_package_read_modify_write_is_serialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = PackageService(
                data_dir=root,
                workspace_root=root,
                list_workflows=lambda: [],
                workflow_path=lambda _recipe_id: None,
                read_workflow=lambda _path: {},
                repo_settings=lambda: {"architecture": "all"},
                release_lookup=lambda _repository: None,
            )
            barrier = threading.Barrier(12)

            def delete(index):
                barrier.wait()
                service.mark_deleted(f"package-{index}")

            threads = [threading.Thread(target=delete, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(set(service.load_overrides()), {f"package-{index}" for index in range(12)})
