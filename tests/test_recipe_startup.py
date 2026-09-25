from tests.lifecycle_helpers import stop_partial_manager
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import app, build_pipeline, builtin_recipe, recipe_store, settings_store
from debbuilder.build_store import BuildStore
from debbuilder.execution_recovery import StartupRecoveryResult
from debbuilder.validation_oci import OciRecoveryResult


class RecipeStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workflows = Path(self.temporary.name) / "workflows"
        self.workflows.mkdir()
        self.store = BuildStore(Path(self.temporary.name) / "builds")
        self.addCleanup(app.dependency_preparation.SUPERVISOR.open_admission)
        self.owned_servers = []
        self.addCleanup(self.stop_owned_servers)
        self.user_workflows = mock.patch.object(app, "USER_WORKFLOWS", self.workflows)
        self.user_workflows.start()
        self.addCleanup(self.user_workflows.stop)

    def start_owned_manager(self, http_server, manager=None, **kwargs):
        self.owned_servers.append(http_server)
        return app.start_execution_manager(http_server, manager, **kwargs)

    def stop_owned_servers(self):
        while self.owned_servers:
            stop_partial_manager(self.owned_servers.pop(), timeout=2)

    @staticmethod
    def report(*, failed=0):
        return recipe_store.RecipeDirectoryValidationReport(
            directory="/isolated/workflows",
            inspected=failed,
            valid=0,
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
        manager = mock.Mock(store=self.store)
        validation = self.report()
        reconciliation = self.reconciliation()

        with mock.patch("debbuilder.app.prepare_application_directories", side_effect=lambda: events.append("directories")), \
                mock.patch("debbuilder.app.create_execution_manager", side_effect=lambda: events.append("construct") or manager), \
                mock.patch("debbuilder.app.prepare_authentication_for_startup", side_effect=lambda: events.append("authentication")), \
                mock.patch("debbuilder.app.recipe_store.validate_recipe_directory", side_effect=lambda _path: events.append("validation") or validation), \
                mock.patch("debbuilder.app.builtin_recipe.reconcile_builtin_recipe", side_effect=lambda _path: events.append("builtin") or reconciliation), \
                mock.patch("debbuilder.app.execution_recovery.recover_startup", side_effect=lambda _store: events.append("recovery") or StartupRecoveryResult()):
            manager.start.side_effect = lambda **_kwargs: events.append("manager")
            http_server = self.server()
            selected = self.start_owned_manager(http_server)

        self.assertIs(selected, manager)
        self.assertEqual(events, ["directories", "construct", "recovery", "authentication", "validation", "builtin", "manager"])
        manager.start.assert_called_once_with(admission_blocker=None)
        self.assertIs(http_server.execution_manager, manager)
        self.assertTrue(http_server.recipe_validation["ok"])
        self.assertEqual(http_server.builtin_recipe_reconciliation["action"], "current")

    def test_recipe_validation_failure_still_runs_recovery_then_keeps_worker_stopped(self):
        events = []
        manager = mock.Mock(store=self.store, worker=None, accepting=False)
        with mock.patch("debbuilder.app.recipe_store.validate_recipe_directory", return_value=self.report(failed=1)), \
                mock.patch("debbuilder.app.builtin_recipe.reconcile_builtin_recipe") as reconcile, \
                mock.patch("debbuilder.app.execution_recovery.recover_startup", side_effect=lambda _store: events.append("recovery") or StartupRecoveryResult()) as recover:
            http_server = self.server()
            with self.assertLogs("debbuilder.app", level="ERROR"), self.assertRaises(app.RecipeStartupValidationError) as raised:
                self.start_owned_manager(http_server, manager)

        self.assertEqual(raised.exception.code, "recipe_validation_failed")
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
        manager = mock.Mock(store=self.store, worker=None, accepting=False)
        with mock.patch(
            "debbuilder.app.execution_recovery.recover_startup",
            side_effect=lambda _store: events.append("recovery") or StartupRecoveryResult(),
        ) as recover, mock.patch(
            "debbuilder.app.prepare_authentication_for_startup",
            side_effect=lambda: events.append("authentication") or (_ for _ in ()).throw(
                app.SessionSecretError("session signing state is invalid")
            ),
        ), mock.patch("debbuilder.app.recipe_store.validate_recipe_directory") as validate:
            with self.assertRaises(app.SessionSecretError):
                self.start_owned_manager(self.server(), manager, prepare_directories=False)

        self.assertEqual(events, ["recovery", "authentication"])
        recover.assert_called_once_with(manager.store)
        validate.assert_not_called()
        manager.start.assert_not_called()

    def test_oidc_startup_creates_session_secret_once_and_reuses_it(self):
        data = Path(self.temporary.name) / "data"
        managers = [mock.Mock(store=self.store), mock.Mock(store=self.store)]
        oidc = {
            "auth_mode": "oidc",
            "oidc_issuer": "https://id.example.test",
            "oidc_client_id": "debbuilder",
            "oidc_redirect_uri": "https://apt.example.test/auth/callback",
        }
        settings = app.settings_defaults()
        settings["security"] = oidc
        settings_store.save_settings(data, settings)
        settings_store.save_secrets(data, {
            "schema_version": 1, "oidc": {"client_secret": "startup-client-secret"},
        })
        original_atomic_write = settings_store.storage.atomic_write_text

        with mock.patch.object(app, "DATA", data), \
                mock.patch("debbuilder.app.execution_recovery.recover_startup", return_value=StartupRecoveryResult()), \
                mock.patch("debbuilder.settings_store.storage.atomic_write_text", wraps=original_atomic_write) as writes:
            self.start_owned_manager(self.server(), managers[0], prepare_directories=False)
            first = (data / "secrets.json").read_bytes()
            self.start_owned_manager(self.server(), managers[1], prepare_directories=False)
            second = (data / "secrets.json").read_bytes()
            loaded = app.cookie_secret(data)

        self.assertEqual(first, second)
        self.assertGreaterEqual(len(loaded), 40)
        self.assertEqual(writes.call_count, 1)
        self.assertEqual((data / "secrets.json").stat().st_mode & 0o777, 0o600)

    def test_oidc_startup_rejects_session_secret_without_client_secret(self):
        data = Path(self.temporary.name) / "missing-client-secret"
        settings = app.settings_defaults()
        settings["security"] = {
            "auth_mode": "oidc",
            "oidc_issuer": "https://id.example.test",
            "oidc_client_id": "debbuilder",
            "oidc_redirect_uri": "https://apt.example.test/auth/callback",
        }
        settings_store.save_settings(data, settings)
        settings_store.save_secrets(data, {
            "schema_version": 1, "session": {"cookie_secret": "s" * 48},
        })
        settings_before = (data / "settings.json").read_bytes()
        secrets_before = (data / "secrets.json").read_bytes()
        manager = mock.Mock(store=self.store, worker=None, accepting=False)

        with mock.patch.object(app, "DATA", data), \
                mock.patch.dict("os.environ", {"DEBBUILDER_OIDC_CLIENT_SECRET": ""}), \
                mock.patch("debbuilder.app.execution_recovery.recover_startup", return_value=StartupRecoveryResult()), \
                mock.patch("debbuilder.app.prepare_authentication_for_startup") as authentication, \
                mock.patch("debbuilder.settings_store.storage.atomic_write_text") as writes:
            with self.assertRaises(settings_store.SettingsDocumentError) as raised:
                self.start_owned_manager(self.server(), manager, prepare_directories=False)

        self.assertEqual(raised.exception.code, "missing_required_secret")
        authentication.assert_not_called()
        manager.start.assert_not_called()
        writes.assert_not_called()
        self.assertEqual((data / "settings.json").read_bytes(), settings_before)
        self.assertEqual((data / "secrets.json").read_bytes(), secrets_before)

    def test_startup_rejects_insecure_secret_permissions_without_repair(self):
        data = Path(self.temporary.name) / "insecure-secrets"
        settings_store.save_secrets(data, {"schema_version": 1, "github": {"token": "ghp_abcdefghijklmnopqrstuvwxyz123456"}})
        secret_path = data / "secrets.json"
        secret_path.chmod(0o644)
        original = secret_path.read_bytes()
        manager = mock.Mock(store=self.store, worker=None, accepting=False)

        with mock.patch.object(app, "DATA", data), \
                mock.patch("debbuilder.app.execution_recovery.recover_startup", return_value=StartupRecoveryResult()), \
                mock.patch("debbuilder.settings_store.storage.atomic_write_text") as writes:
            with self.assertRaises(settings_store.SettingsDocumentError) as raised:
                self.start_owned_manager(self.server(), manager, prepare_directories=False)

        self.assertEqual(raised.exception.code, "insecure_secrets_permissions")
        writes.assert_not_called()
        manager.start.assert_not_called()
        self.assertEqual(secret_path.read_bytes(), original)
        self.assertEqual(secret_path.stat().st_mode & 0o777, 0o644)

    def test_startup_rejects_corrupt_secret_store_after_recovery_without_repair(self):
        data = Path(self.temporary.name) / "corrupt-data"
        data.mkdir()
        secret_path = data / "secrets.json"
        corrupt = b'{"session":{"cookie_secret":"PRIVATE_TEST_VALUE"}'
        secret_path.write_bytes(corrupt)
        secret_path.chmod(0o600)
        manager = mock.Mock(store=self.store, worker=None, accepting=False)
        oidc = {
            "auth_mode": "oidc",
            "oidc_issuer": "https://id.example.test",
            "oidc_client_id": "debbuilder",
            "oidc_redirect_uri": "https://apt.example.test/auth/callback",
        }

        with mock.patch.object(app, "DATA", data), \
                mock.patch.object(app, "effective_security", return_value=oidc), \
                mock.patch("debbuilder.app.execution_recovery.recover_startup", return_value=StartupRecoveryResult()) as recover, \
                mock.patch("debbuilder.app.recipe_store.validate_recipe_directory") as validate:
            with self.assertRaises(settings_store.SettingsDocumentError) as raised:
                self.start_owned_manager(self.server(), manager, prepare_directories=False)

        recover.assert_called_once_with(manager.store)
        validate.assert_not_called()
        manager.start.assert_not_called()
        self.assertEqual(secret_path.read_bytes(), corrupt)
        self.assertNotIn("PRIVATE_TEST_VALUE", str(raised.exception))
        self.assertNotIn(str(secret_path), str(raised.exception))

    def test_startup_rejects_unversioned_settings_after_recovery_without_rewrite(self):
        data = Path(self.temporary.name) / "legacy-settings"
        data.mkdir()
        settings_path = data / "settings.json"
        raw = b'{"general":{"app_name":"Legacy"}}'
        settings_path.write_bytes(raw)
        manager = mock.Mock(store=self.store, worker=None, accepting=False)

        with mock.patch.object(app, "DATA", data), \
                mock.patch("debbuilder.app.execution_recovery.recover_startup", return_value=StartupRecoveryResult()) as recover, \
                mock.patch("debbuilder.app.prepare_authentication_for_startup") as authentication, \
                mock.patch("debbuilder.app.recipe_store.validate_recipe_directory") as validate, \
                mock.patch("debbuilder.settings_store.storage.atomic_write_text") as writes:
            with self.assertRaises(settings_store.SettingsDocumentError) as raised:
                self.start_owned_manager(self.server(), manager, prepare_directories=False)

        self.assertEqual(raised.exception.path, "$.apt")
        recover.assert_called_once_with(manager.store)
        authentication.assert_not_called()
        validate.assert_not_called()
        writes.assert_not_called()
        manager.start.assert_not_called()
        self.assertEqual(settings_path.read_bytes(), raw)

    def test_builtin_failure_still_runs_recovery_then_keeps_worker_stopped(self):
        events = []
        manager = mock.Mock(store=self.store, worker=None, accepting=False)
        failure = builtin_recipe.BuiltinRecipeError("builtin_recipe_adoption_failed", "manual review required")
        with mock.patch("debbuilder.app.recipe_store.validate_recipe_directory", side_effect=lambda _path: events.append("validation") or self.report()), \
                mock.patch("debbuilder.app.builtin_recipe.reconcile_builtin_recipe", side_effect=lambda _path: events.append("builtin") or (_ for _ in ()).throw(failure)), \
                mock.patch("debbuilder.app.execution_recovery.recover_startup", side_effect=lambda _store: events.append("recovery") or StartupRecoveryResult()) as recover:
            http_server = self.server()
            with self.assertLogs("debbuilder.app", level="ERROR"), self.assertRaises(builtin_recipe.BuiltinRecipeError):
                self.start_owned_manager(http_server, manager)

        recover.assert_called_once_with(manager.store)
        self.assertEqual(events, ["recovery", "validation", "builtin"])
        manager.start.assert_not_called()
        self.assertIsNone(manager.worker)
        self.assertFalse(manager.accepting)
        self.assertFalse(hasattr(http_server, "execution_manager"))

    def test_recovery_blocker_remains_authoritative_after_recipe_prerequisites(self):
        manager = mock.Mock(store=self.store)
        validation = self.report()
        reconciliation = self.reconciliation()
        recovery = StartupRecoveryResult(blockers=[{"run_id": "surviving-run", "reason": "unresolved"}])
        with mock.patch("debbuilder.app.execution_recovery.recover_startup", return_value=recovery), \
                mock.patch("debbuilder.app.recipe_store.validate_recipe_directory", return_value=validation), \
                mock.patch("debbuilder.app.builtin_recipe.reconcile_builtin_recipe", return_value=reconciliation):
            http_server = self.server()
            self.start_owned_manager(http_server, manager)

        manager.start.assert_called_once_with(admission_blocker=recovery.admission_blocker)
        self.assertTrue(http_server.execution_recovery["admission_blocked"])

    def test_validation_container_recovery_blocker_closes_manager_and_cleanup_admission(self):
        manager = mock.Mock(store=self.store)
        container_recovery = OciRecoveryResult(blockers=[{
            "code": "validation_container_ownership_unverifiable", "reason": "foreign namespace container",
        }])
        with mock.patch("debbuilder.app.execution_recovery.recover_startup", return_value=StartupRecoveryResult()), \
                mock.patch("debbuilder.app.validation_oci.recover_owned_containers", return_value=container_recovery), \
                mock.patch("debbuilder.app.prepare_recipes_for_startup", return_value=(self.report(), self.reconciliation())):
            http_server = self.server()
            self.start_owned_manager(http_server, manager)
        blocker = container_recovery.admission_blocker
        manager.start.assert_called_once_with(admission_blocker=blocker)
        self.assertEqual(http_server.cleanup_authorization.global_blocker(), blocker)
        self.assertTrue(http_server.validation_container_recovery["admission_blocked"])

    def test_startup_diagnostic_is_bounded_and_does_not_expose_filesystem_paths(self):
        report = recipe_store.RecipeDirectoryValidationReport(
            directory="/srv/private/workflows",
            inspected=1,
            valid=0,
            failed=1,
            files=(recipe_store.RecipeFileValidationReport(
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

        diagnostic = app.recipe_startup_diagnostic(app.RecipeStartupValidationError(report))

        self.assertEqual(diagnostic["failures"][0]["recipe"], "broken.json")
        self.assertEqual(diagnostic["failures"][0]["code"], "invalid_recipe_json")
        self.assertEqual(diagnostic["failures"][0]["path"], "$")
        self.assertLessEqual(len(diagnostic["failures"][0]["message"]), 240)
        self.assertNotIn("/srv/private", json.dumps(diagnostic))
        self.assertNotIn("file", diagnostic["failures"][0])

    def test_repeated_full_startup_is_idempotent_and_preserves_user_recipe(self):
        user_path = self.workflows / "operator.json"
        user_path.write_text(json.dumps({
            "schema_version": 5,
            "name": "operator",
            "active": False,
            "package": {"name": "operator", "maintainer": "Ops <ops@example.test>"},
            "build": {"inactivity_timeout": 45},
        }))

        managers = [mock.Mock(store=self.store), mock.Mock(store=self.store)]
        servers = [self.server(), self.server()]
        with mock.patch("debbuilder.app.execution_recovery.recover_startup", return_value=StartupRecoveryResult()) as recover:
            self.start_owned_manager(servers[0], managers[0])
            first_bytes = {path.name: path.read_bytes() for path in self.workflows.glob("*.json")}
            first_mtimes = {path.name: path.stat().st_mtime_ns for path in self.workflows.glob("*.json")}
            self.start_owned_manager(servers[1], managers[1])

        self.assertEqual(recover.call_count, 2)
        self.assertEqual(servers[0].recipe_validation["valid"], 1)
        self.assertEqual(servers[0].builtin_recipe_reconciliation["action"], "seeded")
        self.assertEqual(servers[1].recipe_validation["valid"], 2)
        self.assertEqual(servers[1].builtin_recipe_reconciliation["action"], "current")
        for manager in managers:
            manager.start.assert_called_once_with(admission_blocker=None)
        self.assertEqual(first_bytes, {path.name: path.read_bytes() for path in self.workflows.glob("*.json")})
        self.assertEqual(first_mtimes, {path.name: path.stat().st_mtime_ns for path in self.workflows.glob("*.json")})
        user = recipe_store.load_recipe(user_path)
        self.assertEqual(user["name"], "operator")
        self.assertFalse(user["active"])
        self.assertEqual(user["build"]["inactivity_timeout"], 45)
        self.assertNotIn("management", user)

    def test_partial_startup_cleanup_stops_validation_worker_idempotently(self):
        manager = mock.Mock(store=self.store)
        manager.start.side_effect = RuntimeError("execution worker start failed")
        http_server = self.server()
        with mock.patch("debbuilder.app.execution_recovery.recover_startup", return_value=StartupRecoveryResult()), \
                mock.patch(
                    "debbuilder.app.prepare_recipes_for_startup",
                    return_value=(self.report(), self.reconciliation()),
                ), self.assertRaisesRegex(RuntimeError, "execution worker start failed"):
            self.start_owned_manager(http_server, manager)

        validation = http_server.validation_manager
        self.assertTrue(validation.worker.is_alive())
        self.assertFalse(validation.worker.daemon)
        self.stop_owned_servers()
        self.assertFalse(validation.worker.is_alive())
        self.assertIsNone(http_server.validation_manager)
        self.stop_owned_servers()

    def test_startup_builtin_is_a_valid_immutable_build_snapshot(self):
        _validation, reconciliation = app.prepare_recipes_for_startup()
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
        self.assertEqual(snapshot["package"]["runtime_dependencies"], [
            "python3", "python3-dbus", "reprepro", "gnupg", "gpgv", "podman", "kmod", "ca-certificates", "binutils",
        ])

    def test_empty_data_root_seeds_v5_and_admits_new_edited_recipe_for_test_and_build(self):
        self.assertEqual(list(self.workflows.iterdir()), [])
        validation, reconciliation = app.prepare_recipes_for_startup()
        self.assertTrue(validation.ok)
        self.assertEqual(reconciliation.action, "seeded")
        self.assertEqual(reconciliation.recipe["schema_version"], 5)

        user_path = self.workflows / "fresh.json"
        created = recipe_store.save_recipe(user_path, {
            "schema_version": 5,
            "name": "fresh",
            "active": True,
            "package": {"name": "fresh", "maintainer": "Fresh <fresh@example.test>"},
            "source": {"repository": "example/fresh"},
        })
        loaded = recipe_store.load_recipe(user_path)
        edited = recipe_store.save_recipe(user_path, {
            **loaded,
            "build": {**loaded["build"], "inactivity_timeout": 90},
        })

        test_run = build_pipeline.create_pipeline_run(
            edited, store=self.store, dry_run=True, run_id="fresh-test",
        )
        build_run = build_pipeline.create_pipeline_run(
            edited, store=self.store, dry_run=False, run_id="fresh-build",
        )

        self.assertEqual(created["schema_version"], 5)
        self.assertEqual(edited["build"]["inactivity_timeout"], 90)
        self.assertEqual((test_run["mode"], build_run["mode"]), ("dry_run", "build"))
        self.assertEqual(recipe_store.validate_recipe_directory(self.workflows).valid, 2)


if __name__ == "__main__":
    unittest.main()
