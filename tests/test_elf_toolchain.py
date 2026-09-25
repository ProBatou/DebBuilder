"""Local policy tests; real OCI qualification is opt-in below."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from debbuilder.elf_dependency_resolution import Requirement, ResolutionError, ResolutionResult
from debbuilder.elf_inspection import inspect_elf
from debbuilder.elf_override_snapshot import filtered_snapshot
from debbuilder.elf_toolchain import analyze_staged_payload, prepared_bookworm_toolchain


@unittest.skipUnless(shutil.which("gcc") and shutil.which("readelf"), "gcc/readelf required")
class ToolchainPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="elf-policy-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.staging = self.root / "staging"
        (self.staging / "DEBIAN").mkdir(parents=True)
        (self.staging / "DEBIAN/control").write_text("Package: probe\nVersion: 1\nArchitecture: amd64\nDescription: probe\n")
        (self.staging / "opt/app/bin").mkdir(parents=True)

    def compile(self, *, source='extern int puts(const char *); int main(void) { return puts("x"); }', flags=()):
        source_path = self.root / "source.c"
        source_path.write_text(source)
        binary = self.staging / "opt/app/bin/app"
        subprocess.run(["gcc", "-o", str(binary), str(source_path), *flags], check=True, capture_output=True, timeout=30)
        return binary

    def policy(self, overrides=()):
        return {"package": {"runtime_dependencies": ["ca-certificates"],
                            "runtime_dependency_detection": {"enabled": True, "overrides": list(overrides)}}}

    def test_explicit_ignored_needed_is_removed_only_from_verified_snapshot(self):
        library = self.root / "libghost.so.1"
        source = self.root / "ghost.c"
        source.write_text("int ghost(void) { return 1; }")
        subprocess.run(["gcc", "-shared", "-fPIC", "-Wl,-soname,libghost.so.1", "-o", str(library), str(source)], check=True, capture_output=True)
        binary = self.compile(source="extern int ghost(void); int main(void) { return ghost(); }", flags=("-L" + str(self.root), "-l:libghost.so.1"))
        original = inspect_elf(binary)
        self.assertIn("libghost.so.1", original.needed)
        snapshot = self.root / "filtered/app"
        filtered_snapshot(binary, snapshot, original, {"libghost.so.1"})
        self.assertNotIn("libghost.so.1", inspect_elf(snapshot).needed)
        self.assertIn("libghost.so.1", inspect_elf(binary).needed)
        with self.assertRaises(ResolutionError):
            filtered_snapshot(binary, self.root / "bad/app", original, {"absent.so"})

    def test_staged_analysis_keeps_manual_and_versioned_detected_relations(self):
        self.compile()
        (self.staging / "opt/app/README").write_text("plain text")
        resolved = ResolutionResult(1, "success", "bookworm", "amd64", ("libc6 (>= 2.34)",),
                                    (Requirement("/opt/app/bin/app", "libc.so.6", "/lib/libc.so.6", "resolved_external", "libc6", "libc6 (>= 2.34)", "symbols"),), ())
        class Environment:
            def execute(self, arguments, *, timeout):
                return {"exit_code": 0, "stdout": "", "stderr": ""}
        @contextmanager
        def prepared(*_args, **_kwargs):
            yield Environment()
        with mock.patch("debbuilder.elf_toolchain.prepared_bookworm_toolchain", prepared), mock.patch(
            "debbuilder.elf_toolchain.resolve_elf_dependencies", return_value=resolved,
        ) as resolve:
            summary = analyze_staged_payload(self.policy(), self.staging, ["bin/app", "README"], "/opt/app", self.root)
        self.assertEqual(summary["effective_depends"], ["ca-certificates", "libc6 (>= 2.34)"])
        self.assertEqual(summary["detected_packages"], ["libc6 (>= 2.34)"])
        self.assertEqual(list(resolve.call_args.args[1]), ["/opt/app/bin/app"])

    def test_out_of_scope_and_unsafe_inputs(self):
        (self.staging / "opt/app/bin/README").write_text("plain text")
        summary = analyze_staged_payload(self.policy(), self.staging, ["bin/README"], "/opt/app", self.root)
        self.assertEqual(summary["detected_packages"], [])
        with self.assertRaises(ResolutionError) as caught:
            analyze_staged_payload(self.policy(), self.staging, ["bin/README"] * 257, "/opt/app", self.root)
        self.assertEqual(caught.exception.code, "resolver_traversal_limit")

    def test_static_elf_needs_no_target_toolchain(self):
        self.compile(flags=("-static", "-no-pie"))
        with mock.patch("debbuilder.elf_toolchain.prepared_bookworm_toolchain", side_effect=AssertionError("OCI not needed")):
            summary = analyze_staged_payload(self.policy(), self.staging, ["bin/app"], "/opt/app", self.root)
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["effective_depends"], ["ca-certificates"])


@unittest.skipUnless(os.environ.get("DEBBUILDER_REAL_OCI_TESTS") == "1" and shutil.which("podman") and shutil.which("gcc"),
                     "set DEBBUILDER_REAL_OCI_TESTS=1 for admitted Bookworm OCI qualification")
class RealBookwormToolchainTests(ToolchainPolicyTests):
    def test_real_generated_depends_install_offline_via_validation_27(self):
        from debbuilder.artifact_validation import validate_artifact
        from debbuilder.build_store import BuildStore
        from debbuilder.debian_packaging import generate_control
        from debbuilder.dependency_preparation import begin_lifecycle_attempt, complete_lifecycle_attempt
        from debbuilder.recipe_schema import validate_recipe_metadata
        from tests.validation_helpers import prepare_admitted_for_test

        store = BuildStore(self.root / "builds")
        authored = {
            "schema_version": 5, "name": "elf-probe",
            "package": {"name": "elf-probe", "architecture": "amd64",
                        "maintainer": "Probe <probe@example.invalid>", "description": "Controlled ELF probe",
                        "runtime_dependency_detection": {"enabled": True}},
            "source": {"repository": "owner/elf-probe"},
            "install": {"destination": "/opt/app", "owner": {"user": "root", "group": "root"}},
            "artifact": {"mode": "upstream_archive", "archive_source": "release_asset",
                         "name_pattern": "elf-probe.tar.gz", "payload": {"mode": "entire_archive"}},
        }
        run = store.create(authored, mode="build")
        recipe = validate_recipe_metadata(authored)
        workspace = Path(run["workspace"])
        shutil.copytree(self.staging, workspace / "staging", dirs_exist_ok=True)
        self.staging = workspace / "staging"
        self.compile(source='extern int puts(const char *); extern unsigned long compressBound(unsigned long); int main(void) { puts("probe"); return (int)compressBound(42); }', flags=("-lz",))
        (self.staging / "DEBIAN/control").write_text(generate_control(recipe, "1.0-1"))
        proposal = analyze_staged_payload(recipe, self.staging, ["bin/app"], "/opt/app", workspace)
        self.assertIn("zlib1g", " ".join(proposal["effective_depends"]))
        (self.staging / "DEBIAN/control").write_text(generate_control(
            recipe, "1.0-1", effective_dependencies=tuple(proposal["effective_depends"]),
        ))
        artifact = workspace / "artifacts/elf-probe_1.0-1_amd64.deb"
        subprocess.run(["dpkg-deb", "--build", str(self.staging), str(artifact)], check=True, capture_output=True, timeout=60)
        from debbuilder.deb_inspector import inspect_deb
        inspected = inspect_deb(artifact, workspace=workspace)
        self.assertIn("zlib1g", inspected["depends"])
        registry = self.root / "validation-containers"
        attempt = prepare_admitted_for_test(run["id"], store=store, current_artifact=artifact, registry_root=registry)
        begin_lifecycle_attempt(store, run["id"], attempt["id"], attempt["prepared"])
        result = validate_artifact(run["id"], store=store, profile="bookworm",
                                   prepared_dependencies=attempt["prepared"], attempt_id=attempt["id"],
                                   registry_root=registry)
        complete_lifecycle_attempt(store, run["id"], attempt["id"], result)
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertTrue(result["backend"]["network_verified"])
        self.assertEqual(result["backend"]["network"], "disabled")

    def test_real_libc_zlib_and_bundled(self):
        # Uses the admitted image, #27 APT preparation, and offline lifecycle.
        libc = self.compile()
        libc.rename(self.staging / "opt/app/bin/libc-probe")
        zlib = self.compile(source="extern unsigned long compressBound(unsigned long); int main(void) { return (int)compressBound(42); }", flags=("-lz",))
        zlib.rename(self.staging / "opt/app/bin/zlib-probe")
        library_source = self.root / "foo.c"
        library_source.write_text('extern int puts(const char *); int foo(void) { return puts("foo"); }')
        library_dir = self.staging / "opt/app/lib"
        library_dir.mkdir()
        library = library_dir / "libfoo.so.1"
        subprocess.run(["gcc", "-shared", "-fPIC", "-Wl,-soname,libfoo.so.1", "-o", str(library), str(library_source)], check=True, capture_output=True)
        (library_dir / "libfoo.so").symlink_to("libfoo.so.1")
        bundled = self.compile(source="extern int foo(void); int main(void) { return foo(); }",
                               flags=("-L" + str(library_dir), "-lfoo", "-Wl,-rpath,$ORIGIN/../lib"))
        bundled.rename(self.staging / "opt/app/bin/bundled-probe")
        ghost_source = self.root / "ghost.c"
        ghost_source.write_text("int ghost(void) { return 1; }")
        ghost_library = self.root / "libghost.so.1"
        subprocess.run(["gcc", "-shared", "-fPIC", "-Wl,-soname,libghost.so.1", "-o", str(ghost_library), str(ghost_source)], check=True, capture_output=True)
        ghost = self.compile(source="extern int ghost(void); int main(void) { return ghost(); }",
                             flags=("-L" + str(self.root), "-l:libghost.so.1"))
        ghost.rename(self.staging / "opt/app/bin/ghost-probe")
        ghost_inspection = inspect_elf(self.staging / "opt/app/bin/ghost-probe")
        with prepared_bookworm_toolchain(
            self.staging, self.root, extra_relations=("libc6 (>= 2.34)",),
            filtered_entrypoints={"/opt/app/bin/ghost-probe": ghost_inspection},
            ignored_sonames=frozenset({"libghost.so.1"}),
        ) as environment:
            from debbuilder.elf_dependency_resolution import resolve_elf_dependencies
            version = environment.execute(["dpkg-shlibdeps", "--version"], timeout=30)
            self.assertEqual(version["exit_code"], 0)
            self.assertIn("1.21.23", version["stdout"])
            results = {}
            for name in ("libc-probe", "zlib-probe", "bundled-probe"):
                installed = "/opt/app/bin/" + name
                results[name] = resolve_elf_dependencies(
                    self.staging, {installed: inspect_elf(self.staging / installed.lstrip("/"))},
                    distribution="bookworm", architecture="amd64", environment=environment,
                )
            results["ghost-probe"] = resolve_elf_dependencies(
                self.staging, {"/opt/app/bin/ghost-probe": ghost_inspection},
                distribution="bookworm", architecture="amd64", environment=environment,
                overrides=({"soname": "libghost.so.1", "action": "ignore", "reason": "controlled optional fixture"},),
            )
            results["ghost-manual"] = resolve_elf_dependencies(
                self.staging, {"/opt/app/bin/ghost-probe": ghost_inspection},
                distribution="bookworm", architecture="amd64", environment=environment,
                overrides=({"soname": "libghost.so.1", "action": "manual", "reason": "controlled manual fixture",
                            "relation": "libc6 (>= 2.34)"},),
            )
        self.assertTrue(any(row.startswith("libc6 (>= ") for row in results["libc-probe"].depends))
        self.assertTrue(any(row.startswith("zlib1g (>= ") for row in results["zlib-probe"].depends))
        self.assertTrue(any(row.soname == "libfoo.so.1" and row.status == "bundled" for row in results["bundled-probe"].requirements))
        self.assertFalse(any("libfoo" in row for row in results["bundled-probe"].depends))
        self.assertTrue(any(row.soname == "libghost.so.1" and row.status == "ignored_explicitly" for row in results["ghost-probe"].requirements))
        self.assertTrue(any(row.soname == "libghost.so.1" and row.status == "manually_resolved" for row in results["ghost-manual"].requirements))
