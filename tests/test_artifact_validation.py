import json
import tempfile
import unittest
from pathlib import Path

from debbuilder import artifact_validation
from debbuilder.build_store import BuildStore
from debbuilder.validation_backend import BackendError, OciSystemdBackend
from debbuilder.validation_profiles import python_satisfies


def recipe(*, service=False, configs=None):
    install = {"destination": "/opt/demo", "config_files": configs or []}
    if configs:
        install.update({"content": {"source": "configured_files"}, "owner": {"user": "root", "group": "root", "create_user": False, "create_group": False}})
    return {
        "name": "demo", "package": {"name": "demo", "architecture": "all", "maintainer": "Demo <demo@example.test>", "description": "Demo"},
        "source": {"repository": "owner/demo"}, "build": {"output": {"mode": "source"}},
        "install": install,
        "service": {"enabled": service, "name": "demo.service", "command": "/opt/demo/demo"},
    }


class FakeBackend:
    def __init__(self, *, workspace, image, on_result):
        self.workspace, self.image, self.on_result = workspace, image, on_result
        self.arguments = []
        self.index = 0
        self.removed = False

    def start(self, validation_id):
        return {"runtime": "fake", "image": self.image, "container": validation_id}

    def exec(self, arguments, *, timeout=120, accepted_exit_codes=None):
        self.arguments.append(arguments)
        self.index += 1
        exit_code, stdout = 0, ""
        if arguments[:2] == ["find", "/opt/demo"]:
            stdout = "755|demo|demo|d|/opt/demo\n644|demo|demo|f|/opt/demo/readme\n777|demo|demo|l|/opt/demo/current\n"
        if arguments and arguments[0] == "stat":
            stdout = "644|root|root|regular file|/etc/demo/demo.conf\n"
        if arguments[:3] == ["dpkg-query", "--show", "--showformat=${Status}\\n"]:
            stdout = "install ok installed\n"
        if arguments[:2] == ["dpkg-query", "--show"] and len(arguments) == 3:
            exit_code = 1
        if arguments[:3] == ["systemctl", "is-active", "--quiet"] and self.removed:
            exit_code = 3
        accepted = exit_code in (accepted_exit_codes or {0})
        result = {"index": self.index, "command": "fake", "arguments": arguments, "working_directory": str(self.workspace), "status": "success" if exit_code == 0 else "failed", "exit_code": exit_code, "stdout": stdout, "stderr": "", "duration": 0.001, "timed_out": False, "accepted": accepted}
        self.on_result(result)
        if arguments[:2] == ["dpkg", "--remove"]:
            self.removed = True
        return result

    def stop(self):
        return None


class FailingBackend(FakeBackend):
    def start(self, validation_id):
        raise BackendError("validation_backend_unavailable", "no runtime")


class NodeBackend(FakeBackend):
    version = "v22.22.1\n"

    def exec(self, arguments, **kwargs):
        result = super().exec(arguments, **kwargs)
        if arguments == ["node", "--version"]:
            result["stdout"] = self.version
            result["exit_code"] = 0 if self.version else 127
            result["accepted"] = bool(self.version)
        return result


class PythonBackend(FakeBackend):
    version = "Python 3.11.2\n"

    def exec(self, arguments, **kwargs):
        result = super().exec(arguments, **kwargs)
        if arguments == ["python3", "--version"]:
            result["stdout"] = self.version
            result["exit_code"] = 0 if self.version else 127
            result["accepted"] = bool(self.version)
        return result


class MixedPolicyBackend(FakeBackend):
    def exec(self, arguments, **kwargs):
        result = super().exec(arguments, **kwargs)
        if arguments[:3] == ["grep", "--fixed-strings", "--quiet"] and arguments[-1] == "/etc/demo/owned.sh":
            result.update({"exit_code": 1, "status": "failed", "accepted": 1 in (kwargs.get("accepted_exit_codes") or {0})})
        return result


class ArtifactValidationTests(unittest.TestCase):
    def successful_run(self, root, configured=None):
        store = BuildStore(Path(root) / "builds")
        run = store.create(configured or recipe(), mode="build", run_id="successful-run")
        artifact = Path(run["workspace"]) / "artifacts/demo_1.0-1_all.deb"
        artifact.write_bytes(b"deb")
        run["status"] = "success"
        run["artifact"] = {"path": str(artifact), "name": artifact.name, "inspection": {"maintainer_scripts": []}}
        store.save(run)
        return store, run

    def test_validation_is_persisted_without_changing_build_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary)
            result = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=FakeBackend)
            self.assertEqual(result["status"], "success")
            self.assertTrue(any(check["name"] == "package_install" for check in result["checks"]))
            self.assertTrue(any(check["name"] == "package_purge" for check in result["checks"]))
            self.assertTrue(Path(run["workspace"], "validation", result["id"], "commands", "001.json").is_file())
            persisted = store.load(run["id"])
            self.assertEqual(persisted["status"], "success")
            self.assertEqual(persisted["validations"][0]["status"], "success")
            self.assertEqual(persisted["artifact"]["validations"][0]["id"], result["id"])
            permissions = next(check for check in result["checks"] if check["name"] == "installed_payload_permissions")
            self.assertEqual(permissions["status"], "success")
            self.assertEqual(permissions["details"]["symbolic_links"], "excluded (target permissions apply)")
            self.assertEqual(permissions["details"]["count"], 3)

    def test_backend_failure_is_a_validation_failure_not_a_build_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary)
            result = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=FailingBackend)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "validation_backend_unavailable")
            self.assertEqual(store.load(run["id"])["status"], "success")

    def test_profiles_default_explicit_and_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary)
            default = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=FakeBackend)
            explicit = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=FakeBackend, profile="bookworm-node22")
            self.assertEqual(default["profile"]["name"], "bookworm")
            self.assertEqual(explicit["profile"]["name"], "bookworm-node22")
            with self.assertRaises(artifact_validation.ValidationError) as raised:
                artifact_validation.validate_artifact(run["id"], store=store, backend_factory=FakeBackend, profile="untrusted/image")
            self.assertEqual(raised.exception.code, "validation_profile_unknown")

    def test_node_toolchain_compatible_old_and_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary)
            persisted = store.load(run["id"])
            next(step for step in persisted["steps"] if step["name"] == "detection")["details"] = {"node_version": "^22.19.0"}
            store.save(persisted)
            good = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=NodeBackend, profile="bookworm-node22")
            self.assertEqual(good["status"], "success")
            class OldNode(NodeBackend):
                version = "v18.20.0\n"
            old = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=OldNode, profile="bookworm-node22")
            self.assertEqual(old["error"]["code"], "validation_toolchain_incompatible")
            class MissingNode(NodeBackend):
                version = ""
            missing = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=MissingNode, profile="bookworm-node22")
            self.assertEqual(missing["error"]["code"], "validation_toolchain_incompatible")

    def test_python_toolchain_requirement_and_common_specifiers(self):
        self.assertTrue(python_satisfies("Python 3.11.2", ">=3.10,<4"))
        self.assertTrue(python_satisfies("Python 3.11.2", "^3.11"))
        self.assertTrue(python_satisfies("Python 3.11.2", "3.11"))
        self.assertTrue(python_satisfies("Python 3.11.2", "==3.11"))
        self.assertFalse(python_satisfies("Python 3.12.0", "==3.11"))
        self.assertFalse(python_satisfies("Python 3.10.9", ">=3.11"))
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary)
            persisted = store.load(run["id"])
            next(step for step in persisted["steps"] if step["name"] == "detection")["details"] = {"project_type": "python", "python_requirement": ">=3.10"}
            store.save(persisted)
            good = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=PythonBackend)
            self.assertEqual(good["status"], "success")
            self.assertEqual(next(check for check in good["checks"] if check["name"] == "toolchain_python")["details"]["actual"], "Python 3.11.2")
            class OldPython(PythonBackend):
                version = "Python 3.9.18\n"
            old = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=OldPython)
            self.assertEqual(old["error"]["code"], "validation_toolchain_incompatible")

    def test_upstream_artifact_uses_opaque_payload_and_detected_systemd_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            configured = recipe()
            configured["artifact"] = {"mode": "upstream_deb", "architecture": "all"}
            store, run = self.successful_run(temporary, configured)
            run = store.load(run["id"])
            run["artifact"]["inspection"].update({
                "files": [{"path": "./usr/bin/demo"}, {"path": "./etc/systemd/system/demo@.service"}],
                "conffiles": [],
            })
            run["artifact"] = store.artifact_details_for_storage(run, run["artifact"])
            store.save(run)
            created = []
            def factory(**kwargs):
                backend = FakeBackend(**kwargs)
                created.append(backend)
                return backend
            result = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=factory)
            self.assertEqual(result["status"], "success")
            self.assertIn(["test", "-e", "/usr/bin/demo"], created[0].arguments)
            self.assertIn(["systemctl", "cat", "demo@.service"], created[0].arguments)
            self.assertFalse(any(call[:2] == ["find", "/opt/demo"] for call in created[0].arguments))

    def test_upgrade_modifies_and_checks_configuration_and_systemd(self):
        with tempfile.TemporaryDirectory() as temporary:
            configured = recipe(service=True, configs=[{
                "source": "demo.conf",
                "destination": "/etc/demo/demo.conf",
                "policy": "dpkg_conffile",
            }])
            store, run = self.successful_run(temporary, configured)
            run = store.load(run["id"])
            metadata = next(step for step in run["steps"] if step["name"] == "debian_metadata")
            metadata["details"] = {"maintainer_scripts": {"postinst": "", "prerm": "", "postrm": ""}}
            staging = next(step for step in run["steps"] if step["name"] == "staging")
            staging["details"] = {"configurations": [{"destination": "/etc/demo/demo.conf"}]}
            run["artifact"]["inspection"]["maintainer_scripts"] = ["postinst", "postrm", "prerm"]
            store.save(run)
            old_dir = Path(temporary) / "builds/old-run/artifacts"
            old_dir.mkdir(parents=True)
            previous = old_dir / "demo_0.9-1_all.deb"
            previous.write_bytes(b"old")
            created = []
            def factory(**kwargs):
                backend = FakeBackend(**kwargs)
                created.append(backend)
                return backend
            result = artifact_validation.validate_artifact(run["id"], store=store, previous_artifact=str(previous), backend_factory=factory)
            self.assertEqual(result["status"], "success")
            calls = created[0].arguments
            self.assertIn(["dpkg", "--force-confnew", "--install", "/validation/validation/" + result["id"] + "/previous.deb"], calls)
            self.assertTrue(any(call[:3] == ["systemctl", "is-active", "--quiet"] for call in calls))
            self.assertEqual(sum(call[:3] == ["systemctl", "is-active", "--quiet"] for call in calls), 3)
            self.assertTrue(any(check["name"] == "systemd_active_after_grace" for check in result["checks"]))
            self.assertTrue(any(check["name"] == "configuration_preserved:/etc/demo/demo.conf" for check in result["checks"]))

    def test_upgrade_checks_each_mapping_policy_independently(self):
        with tempfile.TemporaryDirectory() as temporary:
            configured = recipe(configs=[
                {"source": "owned.sh", "destination": "/etc/demo/owned.sh", "policy": "replace"},
                {"source": "demo.conf", "destination": "/etc/demo/demo.conf", "policy": "dpkg_conffile"},
            ])
            store, run = self.successful_run(temporary, configured)
            run = store.load(run["id"])
            next(step for step in run["steps"] if step["name"] == "staging")["details"] = {"configurations": [
                {"destination": "/etc/demo/owned.sh"}, {"destination": "/etc/demo/demo.conf"},
            ]}
            store.save(run)
            old_dir = Path(temporary) / "builds/old-run/artifacts"
            old_dir.mkdir(parents=True)
            previous = old_dir / "demo_0.9-1_all.deb"
            previous.write_bytes(b"old")
            result = artifact_validation.validate_artifact(run["id"], store=store, previous_artifact=str(previous), backend_factory=MixedPolicyBackend)
            names = {check["name"] for check in result["checks"]}
            self.assertEqual(result["status"], "success")
            self.assertIn("configuration_replaced:/etc/demo/owned.sh", names)
            self.assertIn("configuration_preserved:/etc/demo/demo.conf", names)

    def test_requires_successful_run_and_confines_previous_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create(recipe(), mode="build", run_id="failed-run")
            with self.assertRaisesRegex(artifact_validation.ValidationError, "successful Build Run"):
                artifact_validation.validate_artifact(run["id"], store=store, backend_factory=FakeBackend)
            store, run = self.successful_run(Path(temporary) / "second")
            outside = Path(temporary) / "outside.deb"
            outside.write_bytes(b"old")
            with self.assertRaisesRegex(artifact_validation.ValidationError, "belong"):
                artifact_validation.validate_artifact(run["id"], store=store, previous_artifact=str(outside), backend_factory=FakeBackend)

    def test_oci_backend_reports_missing_runtime_without_host_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = OciSystemdBackend(temporary, runtime="")
            backend.runtime = ""
            with self.assertRaisesRegex(BackendError, "Docker or Podman"):
                backend.start("validation")

    def test_oci_backend_forces_network_none_and_read_only_workspace(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            stdout = '[{"Id":"image-id","RepoDigests":["image@sha256:digest"]}]' if " image inspect " in f" {command} " else "running\n"
            return {"command": command, "arguments": [], "working_directory": str(kwargs.get("workspace")), "status": "success", "exit_code": 0, "stdout": stdout, "stderr": "", "duration": 0, "timed_out": False}
        with tempfile.TemporaryDirectory() as temporary:
            backend = OciSystemdBackend(temporary, runtime="podman", runner=runner)
            context = backend.start("isolated")
            backend.stop()
        launch = next(command for command in calls if " run " in f" {command} ")
        self.assertIn("--network none", launch)
        self.assertIn(":/validation:ro", launch)
        self.assertEqual(context["network"], "disabled")


if __name__ == "__main__":
    unittest.main()
