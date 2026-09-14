import hashlib
import functools
import gzip
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from debbuilder.build_store import BuildStore
from debbuilder.dependency_preparation import (
    ArtifactMetadata,
    DependencyPreparationError,
    SUPERVISOR,
    _snapshot_artifact,
    _download,
    _origin,
    _solve,
    begin_lifecycle_attempt,
    complete_lifecycle_attempt,
    inspect_artifact,
    parse_print_uris,
    parse_simulation,
    prepare_runtime_dependencies,
    recover_interrupted_attempts,
)
from debbuilder import storage
from debbuilder.execution_cancellation import ExecutionCancelled
from debbuilder.resource_limits import admission_contract, requested_controls
from debbuilder.validation_oci import OciOwnershipError


def recipe(name="preparation-test"):
    return {"name": name, "package": {"name": name}, "source": {"repository": f"owner/{name}"}}


def build_deb(
    root: Path,
    output: Path,
    *,
    package: str,
    version: str,
    architecture="all",
    depends="",
    postinst="",
    payload_files: dict[str, tuple[str, int]] | None = None,
    conffiles: tuple[str, ...] = (),
) -> Path:
    staging = root / f"staging-{package}-{version.replace(':', '_')}"
    (staging / "DEBIAN").mkdir(parents=True)
    control = (
        f"Package: {package}\nVersion: {version}\nArchitecture: {architecture}\n"
        "Section: utils\nPriority: optional\nMaintainer: Test <test@example.invalid>\n"
        "Description: controlled dependency preparation fixture\n"
    )
    if depends:
        control += f"Depends: {depends}\n"
    (staging / "DEBIAN/control").write_text(control, encoding="utf-8")
    if postinst:
        script = staging / "DEBIAN/postinst"
        script.write_text(postinst, encoding="utf-8")
        script.chmod(0o755)
    if conffiles:
        (staging / "DEBIAN/conffiles").write_text(
            "".join(f"{path}\n" for path in conffiles), encoding="utf-8",
        )
    for relative, (content, mode) in (payload_files or {}).items():
        target = staging / relative.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        target.chmod(mode)
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(staging), str(output)], check=True, capture_output=True)
    return output


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass


class RedirectHandler(QuietHandler):
    def do_GET(self):
        if self.path.startswith("/redirect/"):
            self.send_response(302)
            self.send_header("Location", self.path.replace("/redirect/", "/actual/", 1))
            self.end_headers()
            return
        super().do_GET()


def controlled_repository(root: Path, *, dependency_postinst="") -> tuple[Path, str, Path]:
    repo = root / "repository"
    pool = repo / "pool/main"
    packages = repo / "dists/bookworm/main/binary-amd64"
    pool.mkdir(parents=True)
    packages.mkdir(parents=True)
    build_deb(
        root, pool / "cp1b-external_1.2-1_all.deb",
        package="cp1b-external", version="1.2-1", postinst=dependency_postinst,
    )
    build_deb(root, pool / "cp1b-provider_1.0-1_all.deb", package="cp1b-provider", version="1.0-1")
    provider_root = root / "provider-control"
    (provider_root / "DEBIAN").mkdir(parents=True)
    (provider_root / "DEBIAN/control").write_text(
        "Package: cp1b-provider\nVersion: 1.0-2\nArchitecture: all\nSection: utils\nPriority: optional\n"
        "Maintainer: Test <test@example.invalid>\nProvides: cp1b-virtual (= 1.0)\nDescription: virtual provider\n",
        encoding="utf-8",
    )
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(provider_root), str(pool / "cp1b-provider_1.0-2_all.deb")], check=True, capture_output=True)
    index = subprocess.run(["apt-ftparchive", "packages", "pool"], cwd=repo, check=True, capture_output=True).stdout
    (packages / "Packages").write_bytes(index)
    (packages / "Packages.gz").write_bytes(gzip.compress(index, mtime=0))
    release = subprocess.run([
        "apt-ftparchive", "-o", "APT::FTPArchive::Release::Suite=bookworm",
        "-o", "APT::FTPArchive::Release::Codename=bookworm", "release", "dists/bookworm",
    ], cwd=repo, check=True, capture_output=True).stdout
    release_path = repo / "dists/bookworm/Release"
    release_path.write_bytes(release)
    key_home = root / "signing-home"
    fingerprint, armored = __import__("tests.test_apt_repository_trust", fromlist=["generate_key"]).generate_key(
        key_home, "Controlled APT Repository <apt@example.invalid>",
    )
    subprocess.run([
        "gpg", "--batch", "--yes", "--homedir", str(key_home), "--local-user", fingerprint,
        "--clearsign", "--output", str(repo / "dists/bookworm/InRelease"), str(release_path),
    ], check=True, capture_output=True)
    cert = root / "server.crt"
    key = root / "server.key"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=host.containers.internal",
        "-addext", "subjectAltName=DNS:host.containers.internal",
        "-addext", "basicConstraints=critical,CA:TRUE",
    ], check=True, capture_output=True)
    return repo, armored, cert


def start_https(directory: Path, cert: Path, key: Path, *, handler_class=QuietHandler, port=0):
    handler = functools.partial(handler_class, directory=str(directory))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=False)
    thread.start()
    return server, thread


class DependencyPreparationUnitTests(unittest.TestCase):
    def setUp(self):
        SUPERVISOR.open_admission()

    def test_attempt_recovery_bounds_run_and_attempt_directory_streams(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            store.root.mkdir(parents=True)
            for name in ("run-a", "run-b"):
                (store.root / name).mkdir()
            with mock.patch("debbuilder.dependency_preparation.MAX_RECOVERY_RUN_ENTRIES", 1):
                self.assertTrue(recover_interrupted_attempts(store, [])["blockers"])
            shutil.rmtree(store.root)
            run = store.create(recipe("bounded"), mode="build", run_id="bounded-run")
            attempts = store.run_dir(run["id"]) / "manifests" / "validation-attempts"
            (attempts / "one").mkdir(parents=True)
            (attempts / "two").mkdir()
            with mock.patch("debbuilder.dependency_preparation.MAX_RECOVERY_ATTEMPTS", 1):
                self.assertTrue(recover_interrupted_attempts(store, [])["blockers"])

    def test_solver_and_both_download_passes_disable_recommends_and_capture_apt_base_choices(self):
        target = ArtifactMetadata(Path("demo.deb"), "demo", "2.0-1", "all", "", "", 1, "0" * 64)

        class Container:
            def __init__(self):
                self.calls = []

            def exec(self, arguments, *, timeout):
                self.calls.append(arguments)
                if "--simulate" in arguments:
                    return {"stdout": "Inst demo (2.0-1 local-deb [all])\n", "stderr": "  MarkInstall ca-certificates:amd64 < 1.0 @ii pK > FU=0\n"}
                if "--print-uris" in arguments:
                    return {"stdout": "'https://repo.invalid/a.deb' 'a.deb' 42 SHA256:abcd\n", "stderr": ""}
                return {"stdout": "", "stderr": ""}

        container = Container()
        decisions, satisfied = _solve(container, "demo.deb", target, status_path="/var/lib/dpkg/status", timeout=1)
        plan = _download(container, "demo.deb", status_path="/var/lib/dpkg/status", timeout=1)
        self.assertEqual(decisions[0].package, "demo")
        self.assertEqual(satisfied, [{"package": "ca-certificates", "version": "1.0", "architecture": "amd64"}])
        self.assertEqual(plan[0]["filename"], "a.deb")
        self.assertTrue(all("--no-install-recommends" in call for call in container.calls))

    def test_external_origin_requires_the_selected_download_uri_to_match(self):
        decision = __import__("debbuilder.dependency_preparation", fromlist=["AptDecision"]).AptDecision(
            "runtime-lib", None, "1.0-1", "amd64", "",
        )
        repository = {"id": "vendor", "uri": "https://vendor.invalid/apt", "suite": "stable", "components": ["main"]}

        class Container:
            def exec(self, _arguments, *, timeout):
                return {"stdout": "  1.0-1 500\n        500 https://vendor.invalid/apt stable/main amd64 Packages\n", "stderr": ""}

        self.assertIsNone(_origin(Container(), decision, [repository], download_uri="https://deb.debian.org/debian/pool/r/runtime.deb"))
        self.assertEqual(
            _origin(Container(), decision, [repository], download_uri="https://vendor.invalid/apt/pool/r/runtime.deb")["repository_id"],
            "vendor",
        )

    def test_parses_apt_simulation_and_rejects_removal(self):
        target = ArtifactMetadata(Path("demo.deb"), "demo", "2.0-1", "all", "", "", 1, "0" * 64)
        decisions, removal = parse_simulation(
            "Inst jq (1.6-2.1 Debian:12.12/oldstable [amd64])\nInst demo (2.0-1 local-deb [all])\n",
            target=target,
        )
        self.assertFalse(removal)
        self.assertEqual([(row.package, row.version) for row in decisions], [("jq", "1.6-2.1"), ("demo", "2.0-1")])
        with self.assertRaises(DependencyPreparationError) as raised:
            parse_simulation("Remv conflict [1.0]\nInst demo (2.0-1 local-deb [all])\n", target=target)
        self.assertEqual(raised.exception.code, "apt_removal_required")

    def test_pre_download_plan_enforces_individual_and_aggregate_bounds(self):
        self.assertEqual(parse_print_uris("'https://repo.invalid/a.deb' 'a.deb' 42 SHA256:abcd\n")[0]["size"], 42)
        with self.assertRaises(DependencyPreparationError):
            parse_print_uris("'https://repo.invalid/a.deb' 'a.deb' 999999999 SHA256:abcd\n")

    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb unavailable")
    def test_artifact_identity_comes_from_actual_deb(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = build_deb(root, root / "demo.deb", package="demo", version="2.0-1", depends="jq, ca-certificates")
            metadata = inspect_artifact(artifact, workspace=root)
            self.assertEqual((metadata.package, metadata.version, metadata.architecture), ("demo", "2.0-1", "all"))
            self.assertEqual(metadata.depends, "jq, ca-certificates")
            self.assertEqual(metadata.sha256, hashlib.sha256(artifact.read_bytes()).hexdigest())
            symlink = root / "linked.deb"
            symlink.symlink_to(artifact)
            with self.assertRaises(DependencyPreparationError):
                inspect_artifact(symlink, workspace=root)
            hardlink = root / "hardlinked.deb"
            os.link(artifact, hardlink)
            with self.assertRaises(DependencyPreparationError):
                inspect_artifact(hardlink, workspace=root)

    def test_artifact_snapshot_pins_the_inspected_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = build_deb(root, root / "demo.deb", package="demo", version="2.0-1", depends="jq")
            metadata = inspect_artifact(artifact, workspace=root)
            pinned = _snapshot_artifact(metadata, root / "pinned.deb")
            self.assertEqual(pinned.identity(), metadata.identity())
            self.assertEqual(pinned.path.read_bytes(), artifact.read_bytes())
            artifact.write_bytes(b"replaced")
            with self.assertRaises(DependencyPreparationError) as raised:
                _snapshot_artifact(metadata, root / "changed.deb")
            self.assertEqual(raised.exception.code, "artifact_changed_during_snapshot")
            self.assertFalse((root / "changed.deb").exists())

    def test_existing_attempt_identity_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            run = store.create(recipe("attempt-conflict"), mode="build", run_id="attempt-conflict-run")
            attempt_root = store.run_dir(run["id"]) / "manifests/validation-attempts/existing"
            attempt_root.mkdir(parents=True)
            with self.assertRaises(DependencyPreparationError) as raised:
                prepare_runtime_dependencies(
                    run["id"], "existing", store=store, current_artifact=root / "unused.deb",
                    registry_root=root / "validation-containers",
                    runner=lambda *_args, **_kwargs: self.fail("attempt collision must precede execution"),
                )
            self.assertEqual(raised.exception.code, "validation_attempt_conflict")

    def test_failure_before_attempt_manifest_removes_only_the_new_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            run = store.create(recipe("premanifest"), mode="build", run_id="premanifest-run")

            def unavailable(_command, **_kwargs):
                return {"status": "failed", "exit_code": 125, "stdout": "", "stderr": "Podman unavailable"}

            with self.assertRaises(OciOwnershipError):
                prepare_runtime_dependencies(
                    run["id"], "reserved", store=store, current_artifact=root / "unused.deb",
                    registry_root=root / "validation-containers", runner=unavailable,
                )
            attempts = store.run_dir(run["id"]) / "manifests" / "validation-attempts"
            self.assertFalse((attempts / "reserved").exists())
            self.assertEqual(recover_interrupted_attempts(store, [])["blockers"], [])

    def test_admitted_preparation_does_not_overwrite_concurrent_durable_cancellation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            run = store.create(recipe("cancel-race"), mode="build", run_id="cancel-race-run")
            attempt_id = "cancel-race-attempt"
            attempt_root = store.run_dir(run["id"]) / "manifests/validation-attempts" / attempt_id
            attempt_root.mkdir(parents=True)
            artifact_path = root / "current.deb"
            artifact_path.write_bytes(b"immutable fixture")
            artifact = ArtifactMetadata(
                artifact_path, "cancel-race", "1.0-1", "all", "", "",
                artifact_path.stat().st_size, hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            )
            image = {
                "name": "debbuilder-validation:bookworm",
                "id": "sha256:" + "1" * 64,
                "digest": None,
            }
            storage.save_json(attempt_root / "attempt.json", {
                "contract_version": 1, "id": attempt_id, "build_run_id": run["id"],
                "inputs": {"profile": "bookworm", "artifact": artifact.identity(), "previous_artifact": None},
                "selected_profile": {"name": "bookworm", "image": image},
                "created_at": "2026-09-14T08:00:00+00:00", "started_at": None,
                "finished_at": None, "status": "queued", "prepared_dependencies": None,
                "result": None, "error": None,
            })
            cancelled = threading.Event()

            def inspect_and_cancel(*_args, **_kwargs):
                path = attempt_root / "attempt.json"
                current = storage.load_json(path, {})
                self.assertEqual(current["status"], "running")
                current["status"] = "cancelling"
                storage.save_json(path, current)
                cancelled.set()
                return artifact

            with (
                mock.patch("debbuilder.dependency_preparation.PodmanRuntime.inspect_image", return_value=image),
                mock.patch("debbuilder.dependency_preparation.inspect_artifact", side_effect=inspect_and_cancel),
                self.assertRaises(ExecutionCancelled),
            ):
                prepare_runtime_dependencies(
                    run["id"], attempt_id, store=store, current_artifact=artifact_path,
                    registry_root=root / "validation-containers", cancellation_event=cancelled,
                    admitted=True,
                )
            durable = storage.load_json(attempt_root / "attempt.json", {})
            self.assertEqual(durable["status"], "cancelled")
            self.assertEqual(durable["error"]["code"], "validation_preparation_cancelled")

    def test_durable_preparation_cancellation_wins_over_an_ordinary_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            run = store.create(recipe("cancel-error-race"), mode="build", run_id="cancel-error-race-run")
            attempt_id = "cancel-error-race-attempt"
            attempt_root = store.run_dir(run["id"]) / "manifests/validation-attempts" / attempt_id
            attempt_root.mkdir(parents=True)
            artifact_path = root / "current.deb"
            artifact_path.write_bytes(b"immutable fixture")
            artifact = ArtifactMetadata(
                artifact_path, "cancel-error-race", "1.0-1", "all", "", "",
                artifact_path.stat().st_size, hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            )
            image = {
                "name": "debbuilder-validation:bookworm",
                "id": "sha256:" + "1" * 64,
                "digest": None,
            }
            storage.save_json(attempt_root / "attempt.json", {
                "contract_version": 1, "id": attempt_id, "build_run_id": run["id"],
                "inputs": {"profile": "bookworm", "artifact": artifact.identity(), "previous_artifact": None},
                "selected_profile": {"name": "bookworm", "image": image},
                "created_at": "2026-09-14T08:00:00+00:00", "started_at": None,
                "finished_at": None, "status": "queued", "prepared_dependencies": None,
                "result": None, "error": None,
            })

            def cancel_then_fail(*_args, **_kwargs):
                path = attempt_root / "attempt.json"
                current = storage.load_json(path, {})
                self.assertEqual(current["status"], "running")
                current["status"] = "cancelling"
                storage.save_json(path, current)
                raise RuntimeError("controlled preparation failure")

            with (
                mock.patch("debbuilder.dependency_preparation.PodmanRuntime.inspect_image", side_effect=cancel_then_fail),
                self.assertRaisesRegex(RuntimeError, "controlled preparation failure"),
            ):
                prepare_runtime_dependencies(
                    run["id"], attempt_id, store=store, current_artifact=artifact_path,
                    registry_root=root / "validation-containers", admitted=True,
                )
            durable = storage.load_json(attempt_root / "attempt.json", {})
            self.assertEqual(durable["status"], "cancelled")
            self.assertEqual(durable["error"]["code"], "validation_preparation_cancelled")

    def test_attempt_recovery_fails_incomplete_and_completes_durable_prepared_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            run = store.create(recipe("attempt-recovery"), mode="build", run_id="attempt-recovery-run")
            image = {"name": "debbuilder-validation:bookworm", "id": "sha256:" + "1" * 64, "digest": None}
            artifact = {"package": "demo", "version": "1.0-1", "architecture": "all", "size": 10, "sha256": "a" * 64}
            base_attempt = {
                "contract_version": 1, "build_run_id": run["id"],
                "inputs": {"profile": "bookworm", "artifact": artifact, "previous_artifact": None},
                "selected_profile": {"name": "bookworm", "image": image},
                "created_at": "2026-09-13T08:00:00+00:00", "started_at": "2026-09-13T08:00:01+00:00",
                "finished_at": None, "status": "running", "prepared_dependencies": None, "result": None, "error": None,
            }
            identities = []
            for attempt_id in ("incomplete", "durable"):
                attempt_root = store.run_dir(run["id"]) / "manifests/validation-attempts" / attempt_id
                attempt_root.mkdir(parents=True)
                storage.save_json(attempt_root / "attempt.json", {**base_attempt, "id": attempt_id})
                identities.append({"run_id": run["id"], "attempt_id": attempt_id})
                if attempt_id == "durable":
                    storage.save_json(attempt_root / "prepared.json", {
                        "contract_version": 1, "profile_name": "bookworm", "image": image,
                        "native_architecture": "amd64", "artifacts": {"current": artifact, "previous": None},
                        "repositories": [], "base_packages": [], "packages": [],
                        "started_at": "2026-09-13T08:00:01+00:00", "finished_at": "2026-09-13T08:00:02+00:00",
                        "diagnostics": [], "enforcement": [],
                    })
            blocked = recover_interrupted_attempts(store, [], inventory_trustworthy=False)
            self.assertEqual(len(blocked["blockers"]), 2)
            self.assertEqual(
                storage.load_json(store.run_dir(run["id"]) / "manifests/validation-attempts/incomplete/attempt.json", {})["status"],
                "running",
            )
            result = recover_interrupted_attempts(store, [])
            self.assertEqual(result["blockers"], [])
            self.assertEqual(set(result["recovered"]), {"incomplete", "durable"})
            incomplete = storage.load_json(store.run_dir(run["id"]) / "manifests/validation-attempts/incomplete/attempt.json", {})
            durable = storage.load_json(store.run_dir(run["id"]) / "manifests/validation-attempts/durable/attempt.json", {})
            self.assertEqual(incomplete["status"], "failed")
            self.assertEqual(incomplete["error"]["code"], "validation_preparation_interrupted")
            self.assertEqual(durable["status"], "success")
            self.assertEqual(durable["prepared_dependencies"]["artifacts"]["current"], artifact)

    def test_lifecycle_attempt_reopens_and_terminalizes_only_after_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            run = store.create(recipe("lifecycle-attempt"), mode="build", run_id="lifecycle-attempt-run")
            attempt_id = "lifecycle-attempt"
            attempt_root = store.run_dir(run["id"]) / "manifests/validation-attempts" / attempt_id
            attempt_root.mkdir(parents=True)
            image = {"name": "debbuilder-validation:bookworm", "id": "sha256:" + "1" * 64, "digest": None}
            artifact = {"package": "demo", "version": "1.0-1", "architecture": "all", "size": 10, "sha256": "a" * 64}
            prepared = {
                "contract_version": 1, "profile_name": "bookworm", "image": image,
                "native_architecture": "amd64", "artifacts": {"current": artifact, "previous": None},
                "repositories": [], "base_packages": [], "packages": [],
                "started_at": "2026-09-13T08:00:01+00:00", "finished_at": "2026-09-13T08:00:02+00:00",
                "diagnostics": [], "enforcement": [],
            }
            attempt = {
                "contract_version": 1, "id": attempt_id, "build_run_id": run["id"],
                "inputs": {"profile": "bookworm", "artifact": artifact, "previous_artifact": None},
                "selected_profile": {"name": "bookworm", "image": image},
                "created_at": "2026-09-13T08:00:00+00:00", "started_at": "2026-09-13T08:00:01+00:00",
                "finished_at": "2026-09-13T08:00:02+00:00", "status": "success",
                "prepared_dependencies": prepared, "result": {"status": "success", "reference": "prepared.json"},
                "error": None,
            }
            storage.save_json(attempt_root / "attempt.json", attempt)
            storage.save_json(attempt_root / "prepared.json", prepared)
            active = begin_lifecycle_attempt(store, run["id"], attempt_id, prepared)
            self.assertEqual(active["status"], "running")
            self.assertIsNone(active["finished_at"])
            terminal = complete_lifecycle_attempt(store, run["id"], attempt_id, {
                "status": "failed", "error": {"code": "modeled_state_drift"},
            })
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(terminal["error"]["code"], "modeled_state_drift")

            terminal.update({"status": "cancelling", "finished_at": None, "result": None, "error": None})
            storage.save_json(attempt_root / "attempt.json", terminal)
            cancelled = complete_lifecycle_attempt(store, run["id"], attempt_id, {"status": "success"})
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(cancelled["error"]["code"], "validation_lifecycle_cancelled")

    def test_recovery_distinguishes_interrupted_lifecycle_from_completed_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            run = store.create(recipe("lifecycle-recovery"), mode="build", run_id="lifecycle-recovery-run")
            attempt_id = "interrupted-lifecycle"
            attempt_root = store.run_dir(run["id"]) / "manifests/validation-attempts" / attempt_id
            attempt_root.mkdir(parents=True)
            image = {"name": "debbuilder-validation:bookworm", "id": "sha256:" + "1" * 64, "digest": None}
            artifact = {"package": "demo", "version": "1.0-1", "architecture": "all", "size": 10, "sha256": "a" * 64}
            prepared = {
                "contract_version": 1, "profile_name": "bookworm", "image": image,
                "native_architecture": "amd64", "artifacts": {"current": artifact, "previous": None},
                "repositories": [], "base_packages": [], "packages": [],
                "started_at": "2026-09-13T08:00:01+00:00", "finished_at": "2026-09-13T08:00:02+00:00",
                "diagnostics": [], "enforcement": [],
            }
            storage.save_json(attempt_root / "prepared.json", prepared)
            storage.save_json(attempt_root / "attempt.json", {
                "contract_version": 1, "id": attempt_id, "build_run_id": run["id"],
                "inputs": {"profile": "bookworm", "artifact": artifact, "previous_artifact": None},
                "selected_profile": {"name": "bookworm", "image": image},
                "created_at": "2026-09-13T08:00:00+00:00", "started_at": "2026-09-13T08:00:01+00:00",
                "finished_at": None, "status": "running", "prepared_dependencies": prepared,
                "result": None, "error": None,
            })
            result = recover_interrupted_attempts(store, [{"run_id": run["id"], "attempt_id": attempt_id}])
            self.assertEqual(result["blockers"], [])
            recovered = storage.load_json(attempt_root / "attempt.json", {})
            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(recovered["error"]["code"], "validation_lifecycle_interrupted")

    def test_recovery_completes_requested_cancellation_after_oci_absence_is_proved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            run = store.create(recipe("cancel-recovery"), mode="build", run_id="cancel-recovery-run")
            attempt_id = "cancelled-on-restart"
            attempt_root = store.run_dir(run["id"]) / "manifests/validation-attempts" / attempt_id
            attempt_root.mkdir(parents=True)
            image = {"name": "debbuilder-validation:bookworm", "id": "sha256:" + "1" * 64, "digest": None}
            artifact = {"package": "demo", "version": "1.0-1", "architecture": "all", "size": 10, "sha256": "a" * 64}
            storage.save_json(attempt_root / "attempt.json", {
                "contract_version": 1, "id": attempt_id, "build_run_id": run["id"],
                "inputs": {"profile": "bookworm", "artifact": artifact, "previous_artifact": None},
                "selected_profile": {"name": "bookworm", "image": image},
                "created_at": "2026-09-13T08:00:00+00:00", "started_at": "2026-09-13T08:00:01+00:00",
                "finished_at": None, "status": "cancelling", "prepared_dependencies": None,
                "result": None, "error": None,
            })
            result = recover_interrupted_attempts(store, [])
            self.assertEqual(result["blockers"], [])
            recovered = storage.load_json(attempt_root / "attempt.json", {})
            self.assertEqual(recovered["status"], "cancelled")
            self.assertEqual(recovered["error"]["code"], "validation_recovery_cancelled")

    def test_async_attempt_recovery_never_treats_preparation_as_full_validation_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            run = store.create(recipe("async-recovery"), mode="build", run_id="async-recovery-run")
            attempt_id = "prepared-before-restart"
            attempt_root = store.run_dir(run["id"]) / "manifests/validation-attempts" / attempt_id
            attempt_root.mkdir(parents=True)
            image = {"name": "debbuilder-validation:bookworm", "id": "sha256:" + "1" * 64, "digest": None}
            artifact = {"package": "demo", "version": "1.0-1", "architecture": "all", "size": 10, "sha256": "a" * 64}
            prepared = {
                "contract_version": 1, "profile_name": "bookworm", "image": image,
                "native_architecture": "amd64", "artifacts": {"current": artifact, "previous": None},
                "repositories": [], "base_packages": [], "packages": [],
                "started_at": "2026-09-13T08:00:01+00:00", "finished_at": "2026-09-13T08:00:02+00:00",
                "diagnostics": [], "enforcement": [],
            }
            storage.save_json(attempt_root / "prepared.json", prepared)
            storage.save_json(attempt_root / "automation.json", {"automatic": False, "publish_after_success": False})
            storage.save_json(attempt_root / "attempt.json", {
                "contract_version": 1, "id": attempt_id, "build_run_id": run["id"],
                "inputs": {"profile": "bookworm", "artifact": artifact, "previous_artifact": None},
                "selected_profile": {"name": "bookworm", "image": image},
                "created_at": "2026-09-13T08:00:00+00:00", "started_at": "2026-09-13T08:00:01+00:00",
                "finished_at": None, "status": "running", "prepared_dependencies": None,
                "result": None, "error": None,
            })
            result = recover_interrupted_attempts(store, [])
            self.assertEqual(result["blockers"], [])
            recovered = storage.load_json(attempt_root / "attempt.json", {})
            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(recovered["error"]["code"], "validation_lifecycle_interrupted")


@unittest.skipUnless(os.getenv("DEBBUILDER_REAL_OCI_TESTS") == "1", "controlled real OCI tests disabled")
class RealDependencyPreparationTests(unittest.TestCase):
    def test_bookworm_resolves_downloads_and_removes_preparation_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            run = store.create(recipe(), mode="build", run_id="real-preparation")
            artifact = build_deb(
                root, Path(run["workspace"]) / "artifacts/preparation-test_1.0-1_all.deb",
                package="preparation-test", version="1.0-1", depends="jq (>= 1.6)",
            )
            attempt = prepare_runtime_dependencies(
                run["id"], "controlled-bookworm", store=store, current_artifact=artifact,
                registry_root=root / "validation-containers",
            )
            self.assertEqual(attempt["status"], "success")
            prepared = attempt["prepared_dependencies"]
            self.assertEqual(prepared["native_architecture"], "amd64")
            self.assertIn("jq", {row["package"] for row in prepared["packages"]})
            self.assertTrue(prepared["base_packages"])
            self.assertTrue(all(row["role"] == "current" for row in prepared["base_packages"]))
            self.assertTrue(all(row["sha256"] for row in prepared["packages"]))
            self.assertEqual(list((root / "validation-containers").glob("*.json")), [])

    @unittest.skipUnless(all(shutil.which(tool) for tool in ("apt-ftparchive", "gpg", "openssl")), "controlled repository tools unavailable")
    def test_controlled_https_signed_repository_resolves_version_alternative_and_virtual(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, armored, cert = controlled_repository(root)
            server, thread = start_https(repo, cert, root / "server.key")
            try:
                store = BuildStore(root / "builds")
                run = store.create(recipe("external-preparation"), mode="build", run_id="real-external-preparation")
                artifact = build_deb(
                    root, Path(run["workspace"]) / "artifacts/external-preparation_1.0-1_all.deb",
                    package="external-preparation", version="1.0-1",
                    depends="missing-alternative | cp1b-external (>= 1.2), cp1b-virtual (>= 1.0)",
                )
                repository = {
                    "id": "controlled", "uri": f"https://host.containers.internal:{server.server_address[1]}",
                    "suite": "bookworm", "components": ["main"], "signing_key": {"armored": armored},
                }
                attempt = prepare_runtime_dependencies(
                    run["id"], "controlled-signed-repo", store=store, current_artifact=artifact,
                    repositories=[repository], registry_root=root / "validation-containers",
                    test_ca_certificate=cert,
                )
                self.assertEqual(attempt["status"], "success")
                rows = attempt["prepared_dependencies"]["packages"]
                self.assertEqual({"cp1b-external", "cp1b-provider"}, {row["package"] for row in rows})
                self.assertTrue(
                    all((row["origin"] or {}).get("repository_id") == "controlled" for row in rows),
                    rows,
                )
                provenance = attempt["prepared_dependencies"]["repositories"][0]
                self.assertEqual(provenance["signing_key_sha256"], hashlib.sha256(armored.encode()).hexdigest())
                self.assertTrue(provenance["signing_key_fingerprints"])
                self.assertEqual(list((root / "validation-containers").glob("*.json")), [])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    @unittest.skipUnless(all(shutil.which(tool) for tool in ("apt-ftparchive", "gpg", "openssl")), "controlled repository tools unavailable")
    def test_controlled_repository_rejects_wrong_key_and_https_redirect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, _armored, cert = controlled_repository(root)
            _fingerprint, wrong_key = __import__("tests.test_apt_repository_trust", fromlist=["generate_key"]).generate_key(
                root / "wrong-key-home", "Wrong Repository <wrong@example.invalid>",
            )
            (root / "actual").mkdir()
            for child in repo.iterdir():
                child.rename(root / "actual" / child.name)
            repo.rmdir()
            shutil.copytree(root / "actual", root / "unsigned")
            (root / "unsigned/dists/bookworm/InRelease").unlink()
            server, thread = start_https(root, cert, root / "server.key", handler_class=RedirectHandler)
            try:
                for suffix, uri, key_material in (
                    ("wrong-key", f"https://host.containers.internal:{server.server_address[1]}/actual", wrong_key),
                    ("redirect", f"https://host.containers.internal:{server.server_address[1]}/redirect", _armored),
                    ("unsigned", f"https://host.containers.internal:{server.server_address[1]}/unsigned", _armored),
                ):
                    store = BuildStore(root / f"builds-{suffix}")
                    run = store.create(recipe(f"rejected-{suffix}"), mode="build", run_id=f"real-{suffix}")
                    artifact = build_deb(
                        root, Path(run["workspace"]) / f"artifacts/rejected-{suffix}_1.0-1_all.deb",
                        package=f"rejected-{suffix}", version="1.0-1", depends="cp1b-external",
                    )
                    repository = {
                        "id": "controlled", "uri": uri, "suite": "bookworm", "components": ["main"],
                        "signing_key": {"armored": key_material},
                    }
                    with self.subTest(case=suffix), self.assertRaises(OciOwnershipError):
                        prepare_runtime_dependencies(
                            run["id"], f"controlled-{suffix}", store=store, current_artifact=artifact,
                            repositories=[repository], registry_root=root / f"registry-{suffix}",
                            test_ca_certificate=cert,
                        )
                    self.assertEqual(list((root / f"registry-{suffix}").glob("*.json")), [])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
            remaining = subprocess.run(
                ["podman", "ps", "--all", "--filter", "name=^debbuilder-validation-controlled-bookworm", "--format", "{{.ID}}"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(remaining, "")

    def test_bookworm_node22_uses_the_exact_selected_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            run = store.create(recipe("node-profile"), mode="build", run_id="real-node-profile")
            artifact = build_deb(
                root, Path(run["workspace"]) / "artifacts/node-profile_1.0-1_all.deb",
                package="node-profile", version="1.0-1", depends="dpkg",
            )
            attempt = prepare_runtime_dependencies(
                run["id"], "controlled-node-profile", store=store, current_artifact=artifact,
                profile_name="bookworm-node22", registry_root=root / "validation-containers",
            )
            prepared = attempt["prepared_dependencies"]
            self.assertEqual(prepared["profile_name"], "bookworm-node22")
            self.assertEqual(prepared["image"]["name"], "debbuilder-validation:bookworm-node22")
            self.assertTrue(prepared["image"]["id"].startswith("sha256:"))
            self.assertEqual(prepared["packages"], [], "dpkg is already satisfied by the profile")

    def test_bookworm_applies_and_verifies_finite_memory_tasks_and_cpu(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = {
                "memory_max_bytes": 256 * 1024 * 1024,
                "tasks_max": 96,
                "cpu_quota_percent": 150,
            }
            capability = {
                "backend": "systemd_cgroup", "available": True,
                "requested_controls": requested_controls(policy), "reason": "controlled real-system test",
            }
            store = BuildStore(root / "builds")
            run = store.create(
                recipe("limited-preparation"), mode="build", run_id="real-limited-preparation",
                resource_contract=admission_contract(policy, {}, capability),
            )
            artifact = build_deb(
                root, Path(run["workspace"]) / "artifacts/limited-preparation_1.0-1_all.deb",
                package="limited-preparation", version="1.0-1", depends="jq",
            )
            attempt = prepare_runtime_dependencies(
                run["id"], "controlled-limits", store=store, current_artifact=artifact,
                registry_root=root / "validation-containers",
            )
            enforcement = {row["control"]: row for row in attempt["prepared_dependencies"]["enforcement"]}
            self.assertEqual(enforcement["memory"]["status"], "enforced")
            self.assertEqual(enforcement["tasks"]["status"], "enforced")
            self.assertEqual(enforcement["cpu"]["status"], "enforced")
            self.assertEqual(enforcement["launcher"]["status"], "enforced")

    @unittest.skipUnless(os.getenv("DEBBUILDER_REAL_IO_TESTS") == "1", "real block-device I/O test disabled")
    def test_bookworm_applies_and_verifies_finite_io(self):
        test_root = os.getenv("DEBBUILDER_BLOCK_TEST_ROOT", "/opt")
        with tempfile.TemporaryDirectory(dir=test_root) as temporary:
            root = Path(temporary)
            policy = {
                "io_read_bandwidth_max_bytes_per_sec": 100 * 1024 * 1024,
                "io_write_bandwidth_max_bytes_per_sec": 100 * 1024 * 1024,
            }
            capability = {
                "backend": "systemd_cgroup", "available": True,
                "requested_controls": requested_controls(policy), "reason": "controlled real block-device test",
            }
            store = BuildStore(root / "builds")
            run = store.create(
                recipe("io-limited-preparation"), mode="build", run_id="real-io-preparation",
                resource_contract=admission_contract(policy, {}, capability),
            )
            artifact = build_deb(
                root, Path(run["workspace"]) / "artifacts/io-limited-preparation_1.0-1_all.deb",
                package="io-limited-preparation", version="1.0-1", depends="jq",
            )
            attempt = prepare_runtime_dependencies(
                run["id"], "controlled-io", store=store, current_artifact=artifact,
                registry_root=root / "validation-containers",
            )
            enforcement = {row["control"]: row for row in attempt["prepared_dependencies"]["enforcement"]}
            self.assertEqual(enforcement["io_read"]["status"], "enforced")
            self.assertEqual(enforcement["io_write"]["status"], "enforced")

    def test_bookworm_models_previous_then_current_without_installing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            run = store.create(recipe("upgrade-preparation"), mode="build", run_id="real-upgrade-preparation")
            previous = build_deb(
                root, Path(run["workspace"]) / "artifacts/upgrade-preparation_1.0-1_all.deb",
                package="upgrade-preparation", version="1.0-1", depends="jq (>= 1.6)",
            )
            current = build_deb(
                root, Path(run["workspace"]) / "artifacts/upgrade-preparation_2.0-1_all.deb",
                package="upgrade-preparation", version="2.0-1", depends="jq (>= 1.6), tree | nano",
            )
            attempt = prepare_runtime_dependencies(
                run["id"], "controlled-upgrade", store=store, current_artifact=current,
                previous_artifact=previous, registry_root=root / "validation-containers",
            )
            self.assertEqual(attempt["status"], "success")
            rows = attempt["prepared_dependencies"]["packages"]
            self.assertIn(("jq", "previous"), {(row["package"], row["role"]) for row in rows})
            self.assertIn("modeled_transition", {row["code"] for row in attempt["prepared_dependencies"]["diagnostics"]})
            self.assertEqual(list((root / "validation-containers").glob("*.json")), [])
