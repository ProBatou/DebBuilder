import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import artifact_validation, build_pipeline, workspace_cleanup
from debbuilder.build_store import BuildStore
from debbuilder.command_identity import current_recorder
from debbuilder.dependency_preparation import (
    SUPERVISOR,
    begin_lifecycle_attempt,
    complete_lifecycle_attempt,
    prepare_runtime_dependencies,
    inspect_artifact,
)
from debbuilder.execution_cancellation import ExecutionCancelled
from debbuilder.repository_lock import repository_lease
from debbuilder.resource_limits import admission_contract, requested_controls
from debbuilder.validation_backend import BackendError, OciSystemdBackend, OwnedOciSystemdBackend
from debbuilder.validation_oci import IdentityRegistry, new_identity
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


class MissingNodeServiceBackend(FakeBackend):
    def exec(self, arguments, **kwargs):
        result = super().exec(arguments, **kwargs)
        if arguments[:3] == ["systemctl", "is-active", "--quiet"]:
            result.update({"exit_code": 3, "status": "failed", "accepted": False})
        if arguments[:4] == ["systemctl", "status", "--no-pager", "--full"]:
            result.update({
                "exit_code": 3,
                "status": "failed",
                "accepted": 3 in (kwargs.get("accepted_exit_codes") or {0}),
                "stdout": "demo.service: Failed at step EXEC spawning /usr/bin/node: No such file or directory\n",
            })
        return result


class MixedPolicyBackend(FakeBackend):
    def exec(self, arguments, **kwargs):
        result = super().exec(arguments, **kwargs)
        if arguments[:3] == ["grep", "--fixed-strings", "--quiet"] and arguments[-1] == "/etc/demo/owned.sh":
            result.update({"exit_code": 1, "status": "failed", "accepted": 1 in (kwargs.get("accepted_exit_codes") or {0})})
        return result


class ArtifactValidationTests(unittest.TestCase):
    def test_local_apt_argv_uses_only_explicit_files_and_disables_sources(self):
        arguments = artifact_validation._local_apt_arguments(
            conffile_option="--force-confold",
            package_paths=["/debbuilder-bundle/dependency-000.deb", "/debbuilder-input/current.deb"],
        )
        self.assertEqual(arguments[-3:], [
            "install", "/debbuilder-bundle/dependency-000.deb", "/debbuilder-input/current.deb",
        ])
        self.assertIn("Dpkg::Options::=--force-confold", arguments)
        self.assertIn("Dir::Etc::sourcelist=/dev/null", arguments)
        self.assertIn("Dir::Etc::sourceparts=/dev/null", arguments)
        self.assertNotIn("--no-download", arguments)
        self.assertNotIn("*", " ".join(arguments))

    def test_lifecycle_refuses_creation_until_preparation_identity_is_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = {"name": "debbuilder-validation:bookworm", "id": "sha256:" + "a" * 64, "digest": None}
            registry = IdentityRegistry(root / "registry")
            registry.persist(new_identity(
                run_id="run", attempt_id="attempt", role="dependency-preparation",
                runtime="podman", image=image,
            ))
            backend = OwnedOciSystemdBackend(
                root, image=image["name"], run_id="run", attempt_id="attempt",
                registry_root=registry.root, resource_policy={}, mounts=[], expected_image=image,
                runner=lambda *_args, **_kwargs: self.fail("OCI must not be called while preparation identity exists"),
            )
            with self.assertRaises(BackendError) as raised:
                backend.start("attempt")
            self.assertEqual(raised.exception.code, "validation_container_overlap")

    def test_prepared_bundle_is_reverified_and_mounted_read_only_with_explicit_package_paths(self):
        from tests.test_dependency_preparation import build_deb

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            attempt_id = "bundle-verification"
            packages = workspace / "manifests/validation-attempts" / attempt_id / "packages"
            packages.mkdir(parents=True)
            artifact = build_deb(
                workspace, workspace / "current.deb", package="demo", version="1.0-1",
                depends="cp1c-dependency (= 1.0-1)",
            )
            dependency = build_deb(
                workspace, packages / "dependency.deb",
                package="cp1c-dependency", version="1.0-1",
            )
            current_metadata = inspect_artifact(artifact, workspace=workspace)
            dependency_metadata = inspect_artifact(dependency, workspace=workspace)
            now = "2026-09-14T08:00:00+00:00"
            prepared = {
                "contract_version": 1,
                "profile_name": "bookworm",
                "image": {"name": "debbuilder-validation:bookworm", "id": "sha256:" + "a" * 64, "digest": None},
                "native_architecture": "amd64",
                "artifacts": {"current": current_metadata.identity(), "previous": None},
                "repositories": [], "base_packages": [],
                "packages": [{
                    **dependency_metadata.identity(), "role": "current", "origin": None,
                }],
                "started_at": now, "finished_at": now, "diagnostics": [], "enforcement": [],
            }
            validation_dir = workspace / "validation" / attempt_id
            validation_dir.mkdir(parents=True)
            bundle = artifact_validation._prepare_lifecycle_inputs(
                workspace=workspace, validation_dir=validation_dir, attempt_id=attempt_id,
                current_artifact=artifact, previous_artifact=None,
                prepared_dependencies=prepared,
                selected_profile={"name": "bookworm", "image": "debbuilder-validation:bookworm"},
                runner=artifact_validation.run_command, cancellation_event=None,
            )
            self.assertEqual(bundle["phase_paths"]["current"], ["/debbuilder-bundle/dependency-000.deb"])
            self.assertTrue(all(options == "ro" for _source, _target, options in bundle["mounts"]))
            self.assertEqual(
                {target for _source, target, _options in bundle["mounts"]},
                {"/debbuilder-input/current.deb", "/debbuilder-bundle"},
            )

    def test_prepared_bundle_missing_hash_architecture_and_symlink_mismatches_fail_closed(self):
        from tests.test_dependency_preparation import build_deb

        for case in ("missing", "hash", "architecture", "symlink"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                attempt_id = f"bundle-{case}"
                packages = workspace / "manifests/validation-attempts" / attempt_id / "packages"
                packages.mkdir(parents=True)
                artifact = build_deb(workspace, workspace / "current.deb", package="demo", version="1.0-1")
                dependency = build_deb(workspace, packages / "dependency.deb", package="cp1c-dependency", version="1.0-1")
                current_metadata = inspect_artifact(artifact, workspace=workspace)
                dependency_metadata = inspect_artifact(dependency, workspace=workspace)
                prepared = {
                    "contract_version": 1, "profile_name": "bookworm",
                    "image": {"name": "debbuilder-validation:bookworm", "id": "sha256:" + "a" * 64, "digest": None},
                    "native_architecture": "amd64",
                    "artifacts": {"current": current_metadata.identity(), "previous": None},
                    "repositories": [], "base_packages": [],
                    "packages": [{**dependency_metadata.identity(), "role": "current", "origin": None}],
                    "started_at": "2026-09-14T08:00:00+00:00",
                    "finished_at": "2026-09-14T08:00:00+00:00",
                    "diagnostics": [], "enforcement": [],
                }
                if case == "missing":
                    dependency.unlink()
                elif case == "hash":
                    prepared["packages"][0]["sha256"] = "f" * 64
                elif case == "architecture":
                    prepared["packages"][0]["architecture"] = "arm64"
                else:
                    outside = workspace / "outside.deb"
                    dependency.rename(outside)
                    dependency.symlink_to(outside)
                validation_dir = workspace / "validation" / attempt_id
                validation_dir.mkdir(parents=True)
                with self.assertRaises(artifact_validation.ValidationError) as raised:
                    artifact_validation._prepare_lifecycle_inputs(
                        workspace=workspace, validation_dir=validation_dir, attempt_id=attempt_id,
                        current_artifact=artifact, previous_artifact=None,
                        prepared_dependencies=prepared,
                        selected_profile={"name": "bookworm", "image": "debbuilder-validation:bookworm"},
                        runner=artifact_validation.run_command, cancellation_event=None,
                    )
                self.assertEqual(raised.exception.code, "prepared_bundle_mismatch")

    def test_cancellation_and_timeout_during_offline_apt_always_stop_lifecycle(self):
        from tests.test_dependency_preparation import build_deb

        for phase, outcome in (
            ("previous", "cancel"), ("previous", "timeout"),
            ("current", "cancel"), ("current", "timeout"),
        ):
            with self.subTest(phase=phase, outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                configured = recipe()
                configured["artifact"] = {"mode": "upstream_deb", "architecture": "all"}
                configured["service"] = {"enabled": False, "name": "", "command": ""}
                store = BuildStore(root / "builds")
                run = store.create(configured, mode="build", run_id=f"offline-{phase}-{outcome}")
                current = build_deb(root, Path(run["workspace"]) / "artifacts/demo_2.0-1_all.deb", package="demo", version="2.0-1")
                previous = None
                if phase == "previous":
                    previous_dir = store.root / "old-run" / "artifacts"
                    previous_dir.mkdir(parents=True)
                    previous = build_deb(root, previous_dir / "demo_1.0-1_all.deb", package="demo", version="1.0-1")
                persisted = store.load(run["id"])
                persisted["status"] = "success"
                persisted["artifact"] = {
                    "path": str(current), "name": current.name,
                    "inspection": {"maintainer_scripts": [], "conffiles": [], "files": [], "service_units": [], "depends": ""},
                }
                store.save(persisted)
                current_metadata = inspect_artifact(current, workspace=Path(run["workspace"]))
                previous_metadata = inspect_artifact(previous, workspace=Path(run["workspace"])) if previous else None
                attempt_id = f"attempt-{phase}-{outcome}"
                packages = Path(run["workspace"]) / "manifests/validation-attempts" / attempt_id / "packages"
                packages.mkdir(parents=True)
                prepared = {
                    "contract_version": 1, "profile_name": "bookworm",
                    "image": {"name": "debbuilder-validation:bookworm", "id": "sha256:" + "a" * 64, "digest": None},
                    "native_architecture": "amd64",
                    "artifacts": {"current": current_metadata.identity(), "previous": previous_metadata.identity() if previous_metadata else None},
                    "repositories": [], "base_packages": [], "packages": [],
                    "started_at": "2026-09-14T08:00:00+00:00", "finished_at": "2026-09-14T08:00:00+00:00",
                    "diagnostics": [], "enforcement": [],
                }
                created = []

                class InterruptingBackend(FakeBackend):
                    def __init__(self, **kwargs):
                        super().__init__(workspace=kwargs["workspace"], image=kwargs["image"], on_result=kwargs["on_result"])
                        self.phase_calls = 0
                        self.stopped = False
                        created.append(self)

                    def start(self, validation_id):
                        return {**super().start(validation_id), "network": "disabled", "network_verified": True}

                    def exec(self, arguments, **kwargs):
                        if arguments == ["dpkg", "--print-architecture"]:
                            result = super().exec(arguments, **kwargs)
                            result["stdout"] = "amd64\n"
                            return result
                        if arguments[:2] == ["dpkg-query", "--show"] and len(arguments) == 3 and "binary:Package" in arguments[2]:
                            result = super().exec(arguments, **kwargs)
                            result.update({"status": "success", "exit_code": 0, "accepted": True, "stdout": ""})
                            return result
                        if arguments and arguments[0] == "apt-get":
                            self.phase_calls += 1
                            selected = "previous" if previous and self.phase_calls == 1 else "current"
                            if selected == phase:
                                if outcome == "cancel":
                                    raise ExecutionCancelled()
                                result = super().exec(arguments, **kwargs)
                                result.update({"status": "failed", "exit_code": -9, "accepted": False, "timed_out": True, "stderr": "offline install timed out"})
                                return result
                        return super().exec(arguments, **kwargs)

                    def stop(self):
                        self.stopped = True
                        return None

                result = artifact_validation.validate_artifact(
                    run["id"], store=store, previous_artifact=str(previous or ""),
                    prepared_dependencies=prepared, attempt_id=attempt_id,
                    registry_root=root / "registry", backend_factory=InterruptingBackend,
                )
                self.assertEqual(result["status"], "cancelled" if outcome == "cancel" else "failed")
                self.assertTrue(created[0].stopped)
                if phase == "previous":
                    self.assertEqual(created[0].phase_calls, 1, "current upgrade must not run after previous failure")

    def test_cancellation_during_runtime_checks_still_stops_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary)
            persisted = store.load(run["id"])
            persisted["artifact"]["inspection"]["depends"] = "nodejs"
            store.save(persisted)
            stopped = []

            class RuntimeCancellingBackend(FakeBackend):
                def exec(self, arguments, **kwargs):
                    if arguments == ["node", "--version"]:
                        raise ExecutionCancelled()
                    return super().exec(arguments, **kwargs)

                def stop(self):
                    stopped.append(True)
                    return None

            result = artifact_validation.validate_artifact(
                run["id"], store=store, backend_factory=RuntimeCancellingBackend,
            )
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(stopped, [True])

    def test_cancellation_during_prepared_input_verification_is_durable(self):
        from tests.test_dependency_preparation import build_deb

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configured = recipe()
            configured["artifact"] = {"mode": "upstream_deb", "architecture": "all"}
            configured["service"] = {"enabled": False, "name": "", "command": ""}
            store = BuildStore(root / "builds")
            run = store.create(configured, mode="build", run_id="cancel-before-container")
            artifact = build_deb(
                root, Path(run["workspace"]) / "artifacts/demo_1.0-1_all.deb",
                package="demo", version="1.0-1",
            )
            persisted = store.load(run["id"])
            persisted["status"] = "success"
            persisted["artifact"] = {
                "path": str(artifact), "name": artifact.name,
                "inspection": {"maintainer_scripts": [], "conffiles": [], "files": [], "service_units": [], "depends": ""},
            }
            store.save(persisted)
            metadata = inspect_artifact(artifact, workspace=Path(run["workspace"]))
            attempt_id = "cancel-before-container-attempt"
            (Path(run["workspace"]) / "manifests/validation-attempts" / attempt_id / "packages").mkdir(parents=True)
            prepared = {
                "contract_version": 1, "profile_name": "bookworm",
                "image": {"name": "debbuilder-validation:bookworm", "id": "sha256:" + "a" * 64, "digest": None},
                "native_architecture": "amd64",
                "artifacts": {"current": metadata.identity(), "previous": None},
                "repositories": [], "base_packages": [], "packages": [],
                "started_at": "2026-09-14T08:00:00+00:00", "finished_at": "2026-09-14T08:00:00+00:00",
                "diagnostics": [], "enforcement": [],
            }
            cancellation = threading.Event()
            cancellation.set()
            backend_factory = mock.Mock()
            result = artifact_validation.validate_artifact(
                run["id"], store=store, prepared_dependencies=prepared,
                attempt_id=attempt_id, registry_root=root / "registry",
                cancellation_event=cancellation, backend_factory=backend_factory,
            )
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(result["error"]["code"], "validation_lifecycle_cancelled")
            backend_factory.assert_not_called()
            durable = store.load(run["id"])
            self.assertEqual(durable["validations"][-1]["status"], "cancelled")
            self.assertEqual(durable["artifact"]["validations"][-1]["status"], "cancelled")

    def test_dependency_install_failure_names_prepared_dependency(self):
        for dependency in ("cp1b-external", "g++", "libfixture-"):
            with self.subTest(dependency=dependency), self.assertRaises(artifact_validation.ValidationError) as raised:
                artifact_validation._raise_install_failure(
                    {"stdout": "", "stderr": f"{dependency}: dependency is not installable"},
                    phase="current", target_package="demo",
                    dependency_packages={dependency},
                )
            self.assertEqual(raised.exception.code, "dependency_install_failed")

    def test_bookworm_image_satisfies_builtin_debbuilder_runtime_dependencies(self):
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "validation/Dockerfile").read_text()
        install_clause = dockerfile.split(
            "apt-get install -y --no-install-recommends", 1,
        )[1].split("&& apt-get clean", 1)[0]
        image_packages = set(install_clause.replace("\\", " ").split())
        builtin = json.loads((root / "debbuilder/builtin_recipes/debbuilder.json").read_text())
        runtime_dependencies = set(builtin["package"]["runtime_dependencies"])

        self.assertEqual(runtime_dependencies, {"python3", "python3-dbus"})
        self.assertLessEqual(runtime_dependencies, image_packages)

    def test_validation_after_workspace_cleanup_preserves_artifact_and_holds_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary)
            workspace_cleanup.clean_workspace(store, run["id"])
            self.assertFalse((Path(run["workspace"]) / "source").exists())
            def backend_factory(**kwargs):
                # Even preparation, before the persisted status becomes running,
                # owns the workspace and cannot race deletion.
                with self.assertRaises(workspace_cleanup.WorkspaceBusyError):
                    store.clear_log_history(run["id"])
                return FakeBackend(**kwargs)
            result = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=backend_factory)
            self.assertEqual(result["status"], "success")
            self.assertTrue(Path(run["artifact"]["path"]).exists())
            store.clear_log_history(run["id"])
            repeated = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=FakeBackend)
            self.assertEqual(repeated["status"], "success")
            self.assertTrue(store.execution_history_deleted(run["id"]))

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

    def test_runtime_repository_declaration_does_not_change_cp1a_execution(self):
        configured = recipe()
        configured["schema_version"] = 4
        configured["runtime_apt_repositories"] = [{
            "id": "vendor-runtime",
            "uri": "https://apt.example.invalid/runtime",
            "suite": "nodistro",
            "components": ["main"],
            "signing_key": {"armored": (
                "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n"
                "dGhpcy1pcy1zdHJ1Y3R1cmFsbHktcHVibGljLWtleS1kYXRh\n"
                "-----END PGP PUBLIC KEY BLOCK-----\n"
            )},
        }]
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary, configured)
            backends = []

            def factory(**kwargs):
                backend = FakeBackend(**kwargs)
                backends.append(backend)
                return backend

            result = artifact_validation.validate_artifact(
                run["id"], store=store, backend_factory=factory,
            )

        self.assertEqual(result["status"], "success")
        self.assertIn(
            ["dpkg", "--force-confnew", "--install", "/validation/artifacts/demo_1.0-1_all.deb"],
            backends[0].arguments,
        )
        self.assertFalse(any(arguments and arguments[0] in {"apt", "apt-get", "curl"} for arguments in backends[0].arguments))

    def test_validation_running_state_is_visible_until_backend_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary)
            entered = threading.Event()
            release = threading.Event()
            result = {}

            class BlockingBackend(FakeBackend):
                def start(self, validation_id):
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError("test backend was not released")
                    return super().start(validation_id)

            thread = threading.Thread(
                target=lambda: result.setdefault("validation", artifact_validation.validate_artifact(
                    run["id"], store=store, backend_factory=BlockingBackend,
                )),
            )
            thread.start()
            self.assertTrue(entered.wait(1))
            persisted = store.load(run["id"])
            self.assertEqual(persisted["validations"][-1]["status"], "running")
            self.assertEqual(build_pipeline.execution_summary(persisted)["lifecycle_status"], "validating")
            self.assertTrue(build_pipeline.execution_summary(persisted)["lifecycle_active"])
            release.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result["validation"]["status"], "success")
            self.assertEqual(store.load(run["id"])["validations"][-1]["status"], "success")

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

    def test_node_detected_for_build_does_not_create_a_runtime_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary)
            persisted = store.load(run["id"])
            next(step for step in persisted["steps"] if step["name"] == "detection")["details"] = {"node_version": "^22.19.0"}
            store.save(persisted)
            backends = []
            def backend_factory(**kwargs):
                backend = FakeBackend(**kwargs)
                backends.append(backend)
                return backend
            good = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=backend_factory)
            self.assertEqual(good["status"], "success")
            self.assertNotIn(["node", "--version"], backends[0].arguments)
            self.assertFalse(any(check["name"] == "runtime_node" for check in good["checks"]))

    def test_declared_node_runtime_is_checked_after_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary)
            persisted = store.load(run["id"])
            persisted["artifact"]["inspection"]["depends"] = "nodejs (>= 22.19.0)"
            store.save(persisted)
            good = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=NodeBackend, profile="bookworm-node22")
            self.assertEqual(good["status"], "success")
            self.assertEqual(next(check for check in good["checks"] if check["name"] == "runtime_node")["details"]["actual"], "v22.22.1")
            class OldNode(NodeBackend):
                version = "v18.20.0\n"
            old = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=OldNode, profile="bookworm-node22")
            self.assertEqual(old["error"]["code"], "validation_runtime_incompatible")
            class MissingNode(NodeBackend):
                version = ""
            missing = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=MissingNode, profile="bookworm-node22")
            self.assertEqual(missing["error"]["code"], "validation_runtime_incompatible")

    def test_service_missing_declared_node_runtime_fails_systemd_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            configured = recipe(service=True)
            configured["service"]["command"] = "/usr/bin/node /opt/demo/dist/index.js"
            store, run = self.successful_run(temporary, configured)
            result = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=MissingNodeServiceBackend)
            self.assertEqual(result["status"], "failed")
            failed = {check["name"]: check for check in result["checks"] if check["status"] == "failed"}
            self.assertIn("systemd_active", failed)
            self.assertIn("systemd_active_after_grace", failed)
            self.assertIn("/usr/bin/node", failed["systemd_active"]["error"])
            self.assertEqual(result["error"]["code"], "systemd_check_failed")

    def test_service_with_declared_node_runtime_keeps_runtime_and_systemd_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            configured = recipe(service=True)
            configured["service"]["command"] = "/usr/bin/node /opt/demo/dist/index.js"
            store, run = self.successful_run(temporary, configured)
            persisted = store.load(run["id"])
            persisted["artifact"]["inspection"]["depends"] = "nodejs"
            store.save(persisted)
            result = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=NodeBackend, profile="bookworm-node22")
            self.assertEqual(result["status"], "success")
            self.assertTrue(any(check["name"] == "runtime_node" for check in result["checks"]))
            self.assertTrue(any(check["name"] == "systemd_active_after_grace" for check in result["checks"]))

    def test_python_runtime_requirement_and_common_specifiers(self):
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
            persisted["artifact"]["inspection"]["depends"] = "python3 (>= 3.10)"
            store.save(persisted)
            good = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=PythonBackend)
            self.assertEqual(good["status"], "success")
            self.assertEqual(next(check for check in good["checks"] if check["name"] == "runtime_python")["details"]["actual"], "Python 3.11.2")
            class OldPython(PythonBackend):
                version = "Python 3.9.18\n"
            old = artifact_validation.validate_artifact(run["id"], store=store, backend_factory=OldPython)
            self.assertEqual(old["error"]["code"], "validation_runtime_incompatible")

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
            self.assertEqual(store.load(run["id"]).get("validations", []), [])

    def test_repository_previous_artifact_is_snapshotted_and_lease_released_before_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary)
            repo = Path(temporary) / "repo"
            previous = repo / "pool/main/d/demo/demo_0.9-1_all.deb"
            previous.parent.mkdir(parents=True)
            previous.write_bytes(b"old")

            def factory(**kwargs):
                class LeaseCheckingBackend(FakeBackend):
                    def start(backend_self, validation_id):
                        with repository_lease(repo, operation="backend-start-proof"):
                            pass
                        snapshot = Path(backend_self.workspace) / "validation"
                        self.assertEqual(next(snapshot.rglob("previous.deb")).read_bytes(), b"old")
                        return super().start(validation_id)
                return LeaseCheckingBackend(**kwargs)

            result = artifact_validation.validate_artifact(
                run["id"], store=store, previous_artifact=str(previous),
                allowed_previous_roots=(repo / "pool",), backend_factory=factory,
            )
            self.assertEqual(result["status"], "success")

    def test_repository_previous_artifact_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary)
            repo = Path(temporary) / "repo"
            previous = repo / "pool/main/d/demo/demo_0.9-1_all.deb"
            previous.parent.mkdir(parents=True)
            target = Path(temporary) / "outside.deb"
            target.write_bytes(b"outside")
            previous.symlink_to(target)
            with self.assertRaises(artifact_validation.ValidationError) as raised:
                artifact_validation.validate_artifact(
                    run["id"], store=store, previous_artifact=str(previous),
                    allowed_previous_roots=(repo / "pool",), backend_factory=FakeBackend,
                )
            self.assertIn(raised.exception.code, {"repository_file_invalid", "previous_artifact_not_available"})

    def test_symlinked_configured_pool_cannot_be_reinterpreted_as_external_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.successful_run(temporary)
            repo = Path(temporary) / "repo"
            repo.mkdir()
            outside_pool = Path(temporary) / "outside/pool"
            previous = outside_pool / "main/d/demo/demo_0.9-1_all.deb"
            previous.parent.mkdir(parents=True)
            previous.write_bytes(b"outside")
            (repo / "pool").symlink_to(outside_pool, target_is_directory=True)
            with self.assertRaises(artifact_validation.ValidationError) as raised:
                artifact_validation.validate_artifact(
                    run["id"], store=store, previous_artifact=str(previous.resolve()),
                    allowed_previous_roots=(repo / "pool",), backend_factory=FakeBackend,
                )
            self.assertEqual(raised.exception.code, "previous_artifact_outside_build_store")

    def test_oci_backend_reports_missing_runtime_without_host_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = OciSystemdBackend(temporary, runtime="")
            backend.runtime = ""
            with self.assertRaisesRegex(BackendError, "Docker or Podman"):
                backend.start("validation")

    def test_lifecycle_installs_the_run_resource_identity_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            policy = {
                "memory_max_bytes": 256 * 1024 * 1024,
                "tasks_max": 64,
                "cpu_quota_percent": 125,
                "io_read_bandwidth_max_bytes_per_sec": 32 * 1024 * 1024,
                "io_write_bandwidth_max_bytes_per_sec": 16 * 1024 * 1024,
            }
            capability = {
                "backend": "systemd_cgroup", "available": True,
                "requested_controls": requested_controls(policy), "reason": "controlled test",
            }
            run = store.create(
                recipe(), mode="build", run_id="lifecycle-resource-context",
                resource_contract=admission_contract(policy, {}, capability),
            )

            def observe(*_args, **_kwargs):
                recorder = current_recorder()
                self.assertIsNotNone(recorder)
                self.assertEqual(recorder.run_id, run["id"])
                self.assertEqual(recorder.resource_policy, policy)
                return {"status": "success"}

            with mock.patch("debbuilder.artifact_validation._validate_artifact_locked", side_effect=observe):
                self.assertEqual(artifact_validation.validate_artifact(run["id"], store=store)["status"], "success")

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


@unittest.skipUnless(os.getenv("DEBBUILDER_REAL_OCI_TESTS") == "1", "controlled real OCI tests disabled")
class RealOfflineLifecycleTests(unittest.TestCase):
    def test_bookworm_node22_base_satisfied_runtime_remains_compatible(self):
        from tests.test_dependency_preparation import build_deb

        SUPERVISOR.open_admission()
        io_enabled = os.getenv("DEBBUILDER_REAL_IO_TESTS") == "1"
        temporary_parent = os.getenv("DEBBUILDER_BLOCK_TEST_ROOT", "/opt") if io_enabled else None
        with tempfile.TemporaryDirectory(dir=temporary_parent) as temporary:
            root = Path(temporary)
            configured = recipe()
            configured["artifact"] = {"mode": "upstream_deb", "architecture": "all"}
            configured["service"] = {"enabled": False, "name": "", "command": ""}
            store = BuildStore(root / "builds")
            policy = {
                "memory_max_bytes": 256 * 1024 * 1024,
                "tasks_max": 96,
                "cpu_quota_percent": 150,
            }
            if io_enabled:
                policy.update({
                    "io_read_bandwidth_max_bytes_per_sec": 100 * 1024 * 1024,
                    "io_write_bandwidth_max_bytes_per_sec": 100 * 1024 * 1024,
                })
            capability = {
                "backend": "systemd_cgroup", "available": True,
                "requested_controls": requested_controls(policy), "reason": "controlled real-system test",
            }
            run = store.create(
                configured, mode="build", run_id="offline-node22",
                resource_contract=admission_contract(policy, {}, capability),
            )
            artifact = build_deb(
                root, Path(run["workspace"]) / "artifacts/demo_1.0-1_all.deb",
                package="demo", version="1.0-1", depends="nodejs (>= 22)",
            )
            persisted = store.load(run["id"])
            persisted["status"] = "success"
            persisted["artifact"] = {
                "path": str(artifact), "name": artifact.name,
                "inspection": {
                    "maintainer_scripts": [], "conffiles": [], "files": [], "service_units": [],
                    "depends": "nodejs (>= 22)",
                },
            }
            store.save(persisted)
            registry = root / "validation-containers"
            attempt = prepare_runtime_dependencies(
                run["id"], "offline-node22-attempt", store=store,
                current_artifact=artifact, profile_name="bookworm-node22", registry_root=registry,
            )
            prepared = attempt["prepared_dependencies"]
            self.assertEqual(prepared["packages"], [])
            self.assertIn("nodejs", {row["package"] for row in prepared["base_packages"]})
            begin_lifecycle_attempt(store, run["id"], attempt["id"], prepared)
            result = artifact_validation.validate_artifact(
                run["id"], store=store, profile="bookworm-node22",
                prepared_dependencies=prepared, attempt_id=attempt["id"], registry_root=registry,
            )
            complete_lifecycle_attempt(store, run["id"], attempt["id"], result)
            self.assertEqual(result["status"], "success", result.get("error"))
            self.assertEqual(next(row for row in result["checks"] if row["name"] == "runtime_node")["status"], "success")
            self.assertEqual(list(registry.glob("*.json")), [])

    @unittest.skipUnless(
        all(shutil.which(tool) for tool in ("apt-ftparchive", "gpg", "openssl", "dpkg-deb", "podman")),
        "controlled repository tools unavailable",
    )
    def test_signed_repository_bundle_installs_with_dependency_and_target_scripts_offline(self):
        from tests.test_dependency_preparation import (
            build_deb,
            controlled_repository,
            start_https,
        )

        SUPERVISOR.open_admission()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reservation = socket.socket()
            reservation.bind(("0.0.0.0", 0))
            port = reservation.getsockname()[1]
            reservation.close()
            denial_script = (
                "#!/bin/sh\nset -eu\n"
                "if python3 -c \"import socket; socket.create_connection(('host.containers.internal', PORT), 1)\"; then\n"
                "  echo 'network unexpectedly available' >&2\n  exit 1\nfi\n"
            ).replace("PORT", str(port))
            repo, armored, cert = controlled_repository(root, dependency_postinst=denial_script)
            server, thread = start_https(repo, cert, root / "server.key", port=port)
            try:
                configured = recipe()
                configured["artifact"] = {"mode": "upstream_deb", "architecture": "all"}
                configured["service"] = {"enabled": False, "name": "", "command": ""}
                store = BuildStore(root / "builds")
                run = store.create(configured, mode="build", run_id="offline-lifecycle")
                artifact = build_deb(
                    root,
                    Path(run["workspace"]) / "artifacts/demo_1.0-1_all.deb",
                    package="demo",
                    version="1.0-1",
                    depends="cp1b-external (= 1.2-1)",
                    postinst=(
                        denial_script
                        + "systemctl daemon-reload\n"
                        + "systemctl start demo-network-check.service\n"
                    ),
                    payload_files={
                        "/usr/libexec/demo-network-check": (denial_script, 0o755),
                        "/usr/lib/systemd/system/demo-network-check.service": (
                            "[Unit]\nDescription=Offline network denial proof\n"
                            "[Service]\nType=oneshot\nExecStart=/usr/libexec/demo-network-check\nRemainAfterExit=yes\n",
                            0o644,
                        ),
                    },
                )
                persisted = store.load(run["id"])
                persisted["status"] = "success"
                persisted["artifact"] = {
                    "path": str(artifact), "name": artifact.name,
                    "inspection": {
                        "maintainer_scripts": ["postinst"], "conffiles": [],
                        "files": [
                            {"path": "./usr/libexec/demo-network-check"},
                            {"path": "./usr/lib/systemd/system/demo-network-check.service"},
                        ],
                        "service_units": [{"path": "./usr/lib/systemd/system/demo-network-check.service"}],
                        "depends": "cp1b-external (= 1.2-1)",
                    },
                }
                store.save(persisted)
                repository = {
                    "id": "controlled",
                    "uri": f"https://host.containers.internal:{server.server_address[1]}",
                    "suite": "bookworm", "components": ["main"],
                    "signing_key": {"armored": armored},
                }
                registry = root / "validation-containers"
                attempt = prepare_runtime_dependencies(
                    run["id"], "offline-lifecycle-attempt", store=store,
                    current_artifact=artifact, repositories=[repository],
                    registry_root=registry, test_ca_certificate=cert,
                )
                prepared = attempt["prepared_dependencies"]
                begin_lifecycle_attempt(store, run["id"], attempt["id"], prepared)
                result = artifact_validation.validate_artifact(
                    run["id"], store=store,
                    prepared_dependencies=prepared, attempt_id=attempt["id"],
                    registry_root=registry,
                )
                complete_lifecycle_attempt(store, run["id"], attempt["id"], result)
                self.assertEqual(result["status"], "success", result.get("error"))
                self.assertEqual(next(row for row in result["checks"] if row["name"] == "lifecycle_network_disabled")["status"], "success")
                self.assertEqual(next(row for row in result["checks"] if row["name"] == "modeled_state_current")["status"], "success")
                self.assertTrue(any(row["name"] == "package_remove" for row in result["checks"]))
                self.assertTrue(any(row["name"] == "package_purge" for row in result["checks"]))
                self.assertEqual(list(registry.glob("*.json")), [])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    @unittest.skipUnless(
        all(shutil.which(tool) for tool in ("apt-ftparchive", "gpg", "openssl", "dpkg-deb", "podman")),
        "controlled repository tools unavailable",
    )
    def test_previous_to_current_uses_prepared_dependency_transition(self):
        from tests.test_dependency_preparation import build_deb, controlled_repository, start_https

        SUPERVISOR.open_admission()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, armored, cert = controlled_repository(root)
            server, thread = start_https(repo, cert, root / "server.key")
            try:
                configured = recipe()
                configured["artifact"] = {"mode": "upstream_deb", "architecture": "all"}
                configured["service"] = {"enabled": False, "name": "", "command": ""}
                store = BuildStore(root / "builds")
                run = store.create(configured, mode="build", run_id="offline-upgrade")
                previous_dir = store.root / "previous-run" / "artifacts"
                previous_dir.mkdir(parents=True)
                previous = build_deb(
                    root, previous_dir / "demo_1.0-1_all.deb",
                    package="demo", version="1.0-1", depends="cp1b-external (= 1.2-1)",
                    payload_files={"/etc/demo.conf": ("previous\n", 0o644)},
                    conffiles=("/etc/demo.conf",),
                )
                current = build_deb(
                    root, Path(run["workspace"]) / "artifacts/demo_2.0-1_all.deb",
                    package="demo", version="2.0-1", depends="cp1b-provider (= 1.0-2)",
                    payload_files={"/etc/demo.conf": ("current\n", 0o644)},
                    conffiles=("/etc/demo.conf",),
                )
                persisted = store.load(run["id"])
                persisted["status"] = "success"
                persisted["artifact"] = {
                    "path": str(current), "name": current.name,
                    "inspection": {
                        "maintainer_scripts": [], "conffiles": ["/etc/demo.conf"], "files": [], "service_units": [],
                        "depends": "cp1b-provider (= 1.0-2)",
                    },
                }
                store.save(persisted)
                repository = {
                    "id": "controlled",
                    "uri": f"https://host.containers.internal:{server.server_address[1]}",
                    "suite": "bookworm", "components": ["main"],
                    "signing_key": {"armored": armored},
                }
                registry = root / "validation-containers"
                attempt = prepare_runtime_dependencies(
                    run["id"], "offline-upgrade-attempt", store=store,
                    current_artifact=current, previous_artifact=previous,
                    repositories=[repository], registry_root=registry,
                    test_ca_certificate=cert,
                )
                prepared = attempt["prepared_dependencies"]
                self.assertIn(("cp1b-external", "previous"), {(row["package"], row["role"]) for row in prepared["packages"]})
                self.assertIn(("cp1b-provider", "current"), {(row["package"], row["role"]) for row in prepared["packages"]})
                begin_lifecycle_attempt(store, run["id"], attempt["id"], prepared)
                result = artifact_validation.validate_artifact(
                    run["id"], store=store, previous_artifact=str(previous),
                    prepared_dependencies=prepared, attempt_id=attempt["id"], registry_root=registry,
                )
                complete_lifecycle_attempt(store, run["id"], attempt["id"], result)
                self.assertEqual(result["status"], "success", result.get("error"))
                names = {row["name"] for row in result["checks"]}
                self.assertIn("modeled_state_previous", names)
                self.assertIn("modeled_state_current", names)
                commands = "\n".join(str(row.get("command") or "") for row in result["commands"])
                self.assertIn("Dpkg::Options::=--force-confnew", commands)
                self.assertIn("Dpkg::Options::=--force-confold", commands)
                self.assertEqual(
                    next(row for row in result["checks"] if row["name"] == "configuration_preserved:/etc/demo.conf")["status"],
                    "success",
                )
                self.assertEqual(list(registry.glob("*.json")), [])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
