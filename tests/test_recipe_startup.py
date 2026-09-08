import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import app, build_pipeline, builtin_recipe, recipe_store, settings_store
from debbuilder.build_store import BuildStore
from debbuilder.execution_recovery import StartupRecoveryResult


class RecipeStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workflows = Path(self.temporary.name) / "workflows"
        self.workflows.mkdir()
        self.user_workflows = mock.patch.object(app, "USER_WORKFLOWS", self.workflows)
        self.user_workflows.start()
        self.addCleanup(self.user_workflows.stop)

    @staticmethod
    def report(*, failed=0):
        return recipe_store.RecipeDirectoryMigrationReport(
            directory="/isolated/workflows",
            inspected=failed,
            current=0,
            migrated=0,
            failed=failed,
            files=(),
        )

    @staticmethod
    def reconciliation(action="current"):
        return builtin_recipe.BuiltinReconciliationResult(
            action=action,
            recipe={"name": "debbuilder"},
            definition_version=1,
            previous_definition_version=1,
        )

    @staticmethod
    def server():
        return type("HttpServer", (), {})()

    def test_startup_orders_recovery_before_recipe_preparation_and_manager_start(self):
        events = []
        manager = mock.Mock(store=object())
        migration = self.report()
        reconciliation = self.reconciliation()

        with mock.patch("debbuilder.app.prepare_application_directories", side_effect=lambda: events.append("directories")), \
                mock.patch("debbuilder.app.create_execution_manager", side_effect=lambda: events.append("construct") or manager), \
                mock.patch("debbuilder.app.prepare_authentication_for_startup", side_effect=lambda: events.append("authentication")), \
                mock.patch("debbuilder.app.recipe_store.migrate_recipe_directory", side_effect=lambda _path: events.append("migration") or migration), \
                mock.patch("debbuilder.app.builtin_recipe.reconcile_builtin_recipe", side_effect=lambda _path: events.append("builtin") or reconciliation), \
                mock.patch("debbuilder.app.execution_recovery.recover_startup", side_effect=lambda _store: events.append("recovery") or StartupRecoveryResult()):
            manager.start.side_effect = lambda **_kwargs: events.append("manager")
            http_server = self.server()
            selected = app.start_execution_manager(http_server)

        self.assertIs(selected, manager)
        self.assertEqual(events, ["directories", "construct", "recovery", "authentication", "migration", "builtin", "manager"])
        manager.start.assert_called_once_with(admission_blocker=None)
        self.assertIs(http_server.execution_manager, manager)
        self.assertTrue(http_server.recipe_migration["ok"])
        self.assertEqual(http_server.builtin_recipe_reconciliation["action"], "current")

    def test_migration_failure_still_runs_recovery_then_keeps_worker_stopped(self):
        events = []
        manager = mock.Mock(store=object(), worker=None, accepting=False)
        with mock.patch("debbuilder.app.recipe_store.migrate_recipe_directory", return_value=self.report(failed=1)), \
                mock.patch("debbuilder.app.builtin_recipe.reconcile_builtin_recipe") as reconcile, \
                mock.patch("debbuilder.app.execution_recovery.recover_startup", side_effect=lambda _store: events.append("recovery") or StartupRecoveryResult()) as recover:
            http_server = self.server()
            with self.assertLogs("debbuilder.app", level="ERROR"), self.assertRaises(app.RecipeStartupError) as raised:
                app.start_execution_manager(http_server, manager)

        self.assertEqual(raised.exception.code, "recipe_migration_failed")
        self.assertFalse(raised.exception.report.ok)
        reconcile.assert_not_called()
        recover.assert_called_once_with(manager.store)
        self.assertEqual(events, ["recovery"])
        manager.start.assert_not_called()
        self.assertIsNone(manager.worker)
        self.assertFalse(manager.accepting)
        self.assertFalse(hasattr(http_server, "execution_manager"))

    def test_authentication_failure_occurs_after_recovery_and_before_recipe_preparation(self):
        events = []
        manager = mock.Mock(store=object(), worker=None, accepting=False)
        with mock.patch(
            "debbuilder.app.execution_recovery.recover_startup",
            side_effect=lambda _store: events.append("recovery") or StartupRecoveryResult(),
        ) as recover, mock.patch(
            "debbuilder.app.prepare_authentication_for_startup",
            side_effect=lambda: events.append("authentication") or (_ for _ in ()).throw(
                app.SessionSecretError("session signing state is invalid")
            ),
        ), mock.patch("debbuilder.app.recipe_store.migrate_recipe_directory") as migrate:
            with self.assertRaises(app.SessionSecretError):
                app.start_execution_manager(self.server(), manager, prepare_directories=False)

        self.assertEqual(events, ["recovery", "authentication"])
        recover.assert_called_once_with(manager.store)
        migrate.assert_not_called()
        manager.start.assert_not_called()

    def test_oidc_startup_creates_session_secret_once_and_reuses_it(self):
        data = Path(self.temporary.name) / "data"
        managers = [mock.Mock(store=object()), mock.Mock(store=object())]
        oidc = {
            "auth_mode": "oidc",
            "oidc_issuer": "https://id.example.test",
            "oidc_client_id": "debbuilder",
            "oidc_redirect_uri": "https://apt.example.test/auth/callback",
        }
        original_atomic_write = settings_store.storage.atomic_write_text

        with mock.patch.object(app, "DATA", data), \
                mock.patch.object(app, "effective_security", return_value=oidc), \
                mock.patch("debbuilder.app.execution_recovery.recover_startup", return_value=StartupRecoveryResult()), \
                mock.patch("debbuilder.settings_store.storage.atomic_write_text", wraps=original_atomic_write) as writes:
            app.start_execution_manager(self.server(), managers[0], prepare_directories=False)
            first = (data / "secrets.json").read_bytes()
            (data / "secrets.json").chmod(0o644)
            app.start_execution_manager(self.server(), managers[1], prepare_directories=False)
            second = (data / "secrets.json").read_bytes()
            loaded = app.cookie_secret(data)

        self.assertEqual(first, second)
        self.assertGreaterEqual(len(loaded), 40)
        self.assertEqual(writes.call_count, 1)
        self.assertEqual((data / "secrets.json").stat().st_mode & 0o777, 0o600)

    def test_oidc_startup_rejects_corrupt_session_secret_after_recovery_without_repair(self):
        data = Path(self.temporary.name) / "corrupt-data"
        data.mkdir()
        secret_path = data / "secrets.json"
        corrupt = b'{"session":{"cookie_secret":"PRIVATE_TEST_VALUE"}'
        secret_path.write_bytes(corrupt)
        manager = mock.Mock(store=object(), worker=None, accepting=False)
        oidc = {
            "auth_mode": "oidc",
            "oidc_issuer": "https://id.example.test",
            "oidc_client_id": "debbuilder",
            "oidc_redirect_uri": "https://apt.example.test/auth/callback",
        }

        with mock.patch.object(app, "DATA", data), \
                mock.patch.object(app, "effective_security", return_value=oidc), \
                mock.patch("debbuilder.app.execution_recovery.recover_startup", return_value=StartupRecoveryResult()) as recover, \
                mock.patch("debbuilder.app.recipe_store.migrate_recipe_directory") as migrate:
            with self.assertRaisesRegex(app.SessionSecretError, "unavailable or invalid") as raised:
                app.start_execution_manager(self.server(), manager, prepare_directories=False)

        recover.assert_called_once_with(manager.store)
        migrate.assert_not_called()
        manager.start.assert_not_called()
        self.assertEqual(secret_path.read_bytes(), corrupt)
        self.assertNotIn("PRIVATE_TEST_VALUE", str(raised.exception))
        self.assertNotIn(str(secret_path), str(raised.exception))

    def test_builtin_failure_still_runs_recovery_then_keeps_worker_stopped(self):
        events = []
        manager = mock.Mock(store=object(), worker=None, accepting=False)
        failure = builtin_recipe.BuiltinRecipeError("builtin_recipe_adoption_failed", "manual review required")
        with mock.patch("debbuilder.app.recipe_store.migrate_recipe_directory", side_effect=lambda _path: events.append("migration") or self.report()), \
                mock.patch("debbuilder.app.builtin_recipe.reconcile_builtin_recipe", side_effect=lambda _path: events.append("builtin") or (_ for _ in ()).throw(failure)), \
                mock.patch("debbuilder.app.execution_recovery.recover_startup", side_effect=lambda _store: events.append("recovery") or StartupRecoveryResult()) as recover:
            http_server = self.server()
            with self.assertLogs("debbuilder.app", level="ERROR"), self.assertRaises(builtin_recipe.BuiltinRecipeError):
                app.start_execution_manager(http_server, manager)

        recover.assert_called_once_with(manager.store)
        self.assertEqual(events, ["recovery", "migration", "builtin"])
        manager.start.assert_not_called()
        self.assertIsNone(manager.worker)
        self.assertFalse(manager.accepting)
        self.assertFalse(hasattr(http_server, "execution_manager"))

    def test_recovery_blocker_remains_authoritative_after_recipe_prerequisites(self):
        manager = mock.Mock(store=object())
        migration = self.report()
        reconciliation = self.reconciliation()
        recovery = StartupRecoveryResult(blockers=[{"run_id": "surviving-run", "reason": "unresolved"}])
        with mock.patch("debbuilder.app.execution_recovery.recover_startup", return_value=recovery), \
                mock.patch("debbuilder.app.recipe_store.migrate_recipe_directory", return_value=migration), \
                mock.patch("debbuilder.app.builtin_recipe.reconcile_builtin_recipe", return_value=reconciliation):
            http_server = self.server()
            app.start_execution_manager(http_server, manager)

        manager.start.assert_called_once_with(admission_blocker=recovery.admission_blocker)
        self.assertTrue(http_server.execution_recovery["admission_blocked"])

    def test_startup_diagnostic_is_bounded_and_does_not_expose_filesystem_paths(self):
        report = recipe_store.RecipeDirectoryMigrationReport(
            directory="/srv/private/workflows",
            inspected=1,
            current=0,
            migrated=0,
            failed=1,
            files=(recipe_store.RecipeFileReport(
                file="broken.json",
                status="failed",
                error={
                    "code": "invalid_recipe_json",
                    "path": "$",
                    "message": "Could not read /srv/private/workflows/broken.json: " + ("x" * 400),
                    "file": "/srv/private/workflows/broken.json",
                },
            ),),
        )

        diagnostic = app.recipe_startup_diagnostic(app.RecipeStartupError(report))

        self.assertEqual(diagnostic["failures"][0]["recipe"], "broken.json")
        self.assertEqual(diagnostic["failures"][0]["code"], "invalid_recipe_json")
        self.assertEqual(diagnostic["failures"][0]["path"], "$")
        self.assertLessEqual(len(diagnostic["failures"][0]["message"]), 240)
        self.assertNotIn("/srv/private", json.dumps(diagnostic))
        self.assertNotIn("file", diagnostic["failures"][0])

    def test_repeated_full_startup_is_idempotent_and_preserves_user_recipe(self):
        user_path = self.workflows / "operator.json"
        user_path.write_text(json.dumps({
            "schema_version": 1,
            "name": "operator",
            "active": False,
            "package": {"name": "operator", "maintainer": "Ops <ops@example.test>"},
            "build": {"timeout": 45},
        }))

        managers = [mock.Mock(store=object()), mock.Mock(store=object())]
        servers = [self.server(), self.server()]
        with mock.patch("debbuilder.app.execution_recovery.recover_startup", return_value=StartupRecoveryResult()) as recover:
            app.start_execution_manager(servers[0], managers[0])
            first_bytes = {path.name: path.read_bytes() for path in self.workflows.glob("*.json")}
            first_mtimes = {path.name: path.stat().st_mtime_ns for path in self.workflows.glob("*.json")}
            app.start_execution_manager(servers[1], managers[1])

        self.assertEqual(recover.call_count, 2)
        self.assertEqual(servers[0].recipe_migration["migrated"], 1)
        self.assertEqual(servers[0].builtin_recipe_reconciliation["action"], "seeded")
        self.assertEqual(servers[1].recipe_migration["migrated"], 0)
        self.assertEqual(servers[1].builtin_recipe_reconciliation["action"], "current")
        for manager in managers:
            manager.start.assert_called_once_with(admission_blocker=None)
        self.assertEqual(first_bytes, {path.name: path.read_bytes() for path in self.workflows.glob("*.json")})
        self.assertEqual(first_mtimes, {path.name: path.stat().st_mtime_ns for path in self.workflows.glob("*.json")})
        user = recipe_store.load_recipe(user_path, write_back=False)
        self.assertEqual(user["name"], "operator")
        self.assertFalse(user["active"])
        self.assertEqual(user["build"]["inactivity_timeout"], 45)
        self.assertNotIn("management", user)

    def test_startup_builtin_is_a_valid_immutable_build_snapshot(self):
        _migration, reconciliation = app.prepare_recipes_for_startup()
        store = BuildStore(Path(self.temporary.name) / "builds")

        run = build_pipeline.create_pipeline_run(
            reconciliation.recipe,
            store=store,
            dry_run=True,
            recipe_id="debbuilder",
        )
        snapshot = json.loads((store.run_dir(run["id"]) / "recipe.json").read_text())

        self.assertEqual(snapshot, reconciliation.recipe)
        self.assertEqual(snapshot["management"]["builtin_id"], "debbuilder")
        self.assertEqual(snapshot["package"]["runtime_dependencies"], ["python3", "python3-dbus"])


if __name__ == "__main__":
    unittest.main()
