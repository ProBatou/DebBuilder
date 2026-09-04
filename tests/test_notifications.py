import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import app as server
from debbuilder import build_pipeline, source_acquisition
from debbuilder.build_store import BuildStore
from debbuilder import notifications


def fake_app(data_dir: Path, sent: list[dict]):
    def send(title, message, **kwargs):
        sent.append({"title": title, "message": message, **kwargs})
        return {"ok": True}

    module = types.SimpleNamespace()
    module.DATA = data_dir
    module.BuildStore = server.BuildStore
    module.settings_view = lambda: {"notifications": {}}
    module.update_settings = lambda payload: {"settings": payload}
    module.validate_build_artifact = lambda run_id, payload=None: {
        "id": "validation-1",
        "build_run_id": run_id,
        "status": "failed",
        "error": {"message": "validation failed with token=validation-secret"},
    }
    module.publish_build_artifact = lambda run_id, payload=None: {
        "id": "publication-1",
        "build_run_id": run_id,
        "package": "demo",
        "version": "1.0-1",
        "status": "failed",
        "error": {"message": "publication failed"},
    }
    module.run_recipe_pipeline_with_automation = lambda workflow, dry_run=True: {
        "run_id": "run-auto",
        "status": "success",
        "automation": {"publication": {"build_run_id": "run-auto", "package": "demo", "version": "1.0-1", "status": "success"}},
    }
    module.recipe_package_name = lambda recipe: (recipe.get("package") or {}).get("name") or recipe.get("name") or ""
    module.build_run_package = lambda run: (((run.get("artifact") or {}).get("inspection") or {}).get("package")) or run.get("package") or run.get("recipe_id") or ""
    module.app_settings = lambda: {
        "general": {"url": "https://debbuilder.example.test"},
        "notifications": {"type": "ntfy", "server_url": "https://ntfy.example.test", "topic": "debbuilder"},
    }
    module._fake_sender = send
    return module


def notification_service(module, *, sender=None):
    return notifications.NotificationService(
        module.DATA,
        module.app_settings,
        run_loader=BuildStore(module.DATA / "builds").load,
        package_resolver=lambda run, recipe: module.recipe_package_name(recipe) if recipe else module.build_run_package(run or {}),
        sender=sender,
    )


class NotificationServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def create_run(self, run_id: str, package: str = "demo", version: str = "1.0-1"):
        recipe = {
            "schema_version": 1,
            "name": f"{package}-recipe",
            "active": True,
            "package": {"name": package, "architecture": "all", "maintainer": "Demo <demo@example.test>", "description": "Demo"},
            "source": {"provider": "github", "repository": f"owner/{package}", "tracking": "latest_release", "version": {"source": "tag"}},
        }
        store = server.BuildStore(self.data / "builds")
        run = store.create(recipe, recipe_id=f"{package}-recipe", mode="build", run_id=run_id)
        run.update({
            "status": "success",
            "version": {"upstream": version.split("-")[0], "debian": version},
            "artifact": {"path": str(Path(run["workspace"]) / f"artifacts/{package}_{version}_all.deb"), "inspection": {"package": package, "version": version, "architecture": "all"}},
        })
        store.save(run)
        return run

    def test_failure_notifications_include_context_url_are_redacted_and_deduped(self):
        sent = []
        module = fake_app(self.data, sent)
        service = notification_service(module, sender=module._fake_sender)
        run = {
            "id": "run-1",
            "recipe_id": "demo-recipe",
            "artifact": {"inspection": {"package": "demo", "version": "1.0-1"}},
            "error": {"message": "failed with Bearer secret-token"},
        }

        first = service.notify_failure("build", run=run)
        second = service.notify_failure("build", run=run)

        self.assertTrue(first["ok"])
        self.assertEqual(second["reason"], "duplicate")
        self.assertEqual(len(sent), 1)
        message = sent[0]["message"]
        self.assertIn("Recipe/package: demo", message)
        self.assertIn("Version: 1.0-1", message)
        self.assertIn("Failed stage: build", message)
        self.assertIn("Run: run-1", message)
        self.assertIn("Open run: https://debbuilder.example.test/?view=logs&run=run-1", message)
        self.assertIn("Bearer [redacted]", message)
        self.assertNotIn("secret-token", message)

    def test_recovery_notification_is_sent_once_for_previously_failed_stage(self):
        sent = []
        module = fake_app(self.data, sent)
        service = notification_service(module, sender=module._fake_sender)
        run = {
            "id": "run-2",
            "recipe_id": "demo-recipe",
            "artifact": {"inspection": {"package": "demo", "version": "1.0-1"}},
            "error": {"message": "validation failed"},
        }

        service.notify_failure("validation", run=run)
        recovered = service.notify_recovery("validation", run=run)
        duplicate = service.notify_recovery("validation", run=run)

        self.assertTrue(recovered["ok"])
        self.assertEqual(duplicate["reason"], "no active failure")
        self.assertEqual([row["title"] for row in sent], ["DebBuilder attention: demo", "DebBuilder recovered: demo"])

    def test_automatic_completion_is_skipped_when_recovery_was_already_reported_for_run(self):
        sent = []
        module = fake_app(self.data, sent)
        service = notification_service(module, sender=module._fake_sender)
        failed = {"id": "run-old", "recipe_id": "demo-recipe", "artifact": {"inspection": {"package": "demo", "version": "0.9-1"}}}
        recovered = self.create_run("run-new")

        service.notify_failure("publication", run=failed)
        service.notify_recovery("publication", run=recovered)
        result = service.notify_automatic_completion({
            "run_id": "run-new",
            "automation": {"publication": {"build_run_id": "run-new", "package": "demo", "version": "1.0-1", "status": "success"}},
        })

        self.assertEqual(result["reason"], "recovery already reported")
        self.assertEqual([row["title"] for row in sent], ["DebBuilder attention: demo", "DebBuilder recovered: demo"])

    def test_delivery_exceptions_are_redacted_and_non_fatal(self):
        module = fake_app(self.data, [])

        def failing_sender(*_args, **_kwargs):
            raise RuntimeError("ntfy failed with token=runtime-secret")

        service = notification_service(module, sender=failing_sender)
        result = service.notify_failure("publication", run={"id": "run-3", "recipe_id": "demo"})

        self.assertFalse(result["ok"])
        self.assertIn("token=[redacted]", result["error"])
        self.assertNotIn("runtime-secret", result["error"])

    def test_service_covers_all_canonical_lifecycle_paths(self):
        sent = []
        module = fake_app(self.data, sent)
        self.create_run("run-validation")
        self.create_run("run-publication")
        self.create_run("run-auto")
        with mock.patch("debbuilder.notifications.send_ntfy", side_effect=lambda data_dir, settings, title, message, **kwargs: module._fake_sender(title, message, **kwargs)):
            service = notification_service(module)
            service.notify_build_lifecycle("build_started", run={"id": "run-build", "recipe_id": "demo-recipe"}, recipe={"package": {"name": "demo"}})
            service.notify_build_lifecycle("build_failed", run={"id": "run-build", "recipe_id": "demo-recipe", "error": {"message": "source failed"}}, recipe={"package": {"name": "demo"}})
            service.notify_validation_result(module.validate_build_artifact("run-validation", {}))
            service.notify_publication_result(module.publish_build_artifact("run-publication", {}))
            service.notify_automatic_completion(module.run_recipe_pipeline_with_automation({"name": "demo-recipe"}, dry_run=False))

        titles = [row["title"] for row in sent]
        self.assertNotIn("Build started", titles)
        self.assertIn("DebBuilder attention: demo", titles)
        self.assertIn("DebBuilder attention: demo", titles)
        self.assertIn("DebBuilder automatic update complete: demo", titles)
        self.assertTrue(any("Failed stage: validation" in row["message"] for row in sent))
        self.assertTrue(any("Failed stage: publication" in row["message"] for row in sent))
        self.assertTrue(all("validation-secret" not in row["message"] for row in sent))

    def test_app_run_recipe_pipeline_passes_lifecycle_callback_to_structured_engine(self):
        events = []

        def fake_run_pipeline(_workflow, **kwargs):
            callback = kwargs.get("lifecycle_callback")
            self.assertTrue(callable(callback))
            callback("build_failed", run={"id": "run-callback", "recipe_id": "demo", "mode": "build"}, recipe={"package": {"name": "demo"}})
            return {"run_id": "run-callback", "status": "failed"}

        class Recorder:
            def notify_build_lifecycle(self, event, **payload):
                events.append((event, payload))

        old = server.NOTIFICATION_SERVICE
        server.NOTIFICATION_SERVICE = Recorder()
        try:
            with mock.patch("debbuilder.app.build_pipeline.run_pipeline", side_effect=fake_run_pipeline):
                result = server.run_recipe_pipeline({"name": "demo", "package": {"name": "demo"}}, dry_run=False)
        finally:
            server.NOTIFICATION_SERVICE = old

        self.assertEqual(result["run_id"], "run-callback")
        self.assertEqual(events[0][0], "build_failed")

    def test_structured_build_pipeline_emits_started_and_failed_events(self):
        events = []
        store = BuildStore(self.data / "builds")

        def fail_acquire(_recipe, _workspace, token=""):
            raise source_acquisition.SourceError("download_failed", "source failed")

        result = build_pipeline.run_pipeline(
            {
                "name": "demo",
                "active": True,
                "package": {"name": "demo", "architecture": "all", "maintainer": "Demo <demo@example.test>", "description": "Demo package"},
                "source": {"repository": "owner/demo"},
            },
            store=store,
            dry_run=False,
            acquire=fail_acquire,
            lifecycle_callback=lambda event, **payload: events.append((event, payload["run"]["id"])),
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual([event for event, _run_id in events], ["build_started", "build_failed"])


if __name__ == "__main__":
    unittest.main()
