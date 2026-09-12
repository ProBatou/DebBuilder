import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import deb_inspector, debian_packaging, upstream_archive
from debbuilder.recipe_schema import validate_recipe_metadata


def packaging_recipe(*, service=True, policy="dpkg_conffile"):
    return validate_recipe_metadata({
        "name": "demo", "package": {
            "name": "demo", "architecture": "all", "maintainer": "Demo <demo@example.test>",
            "description": "Demo application\nA deterministic package.",
            "runtime_dependencies": ["ca-certificates"],
        },
        "source": {"repository": "owner/demo"},
        "build": {"commands": ["true"], "output": {"mode": "source"}},
        "install": {
            "destination": "/opt/demo", "directory_mode": "0750", "file_mode": "0640",
            "config_files": ["/etc/demo/demo.conf"], "config_policy": policy,
            "owner": {"user": "demo-app", "group": "demo-app", "create_user": True, "create_group": True},
            "maintainer_scripts": {"postinst": "echo configured"},
        },
        "service": {
            "enabled": service, "name": "demo.service", "description": "Demo daemon",
            "user": "demo-service", "group": "demo-service", "command": "/opt/demo/bin/demo",
            "after": ["network.target"],
        },
    })


def archive_packaging_recipe(payload):
    configured = packaging_recipe(service=False)
    configured["artifact"] = {
        "mode": "upstream_archive", "type": "archive", "archive_source": "github_source",
        "archive_format": "tar.gz", "payload": payload,
    }
    configured["install"]["content"] = {"source": "build_output", "path": ""}
    configured["install"]["config_files"] = []
    return validate_recipe_metadata(configured)


class DebianPackagingTests(unittest.TestCase):
    def test_package_and_inspection_commands_preserve_resource_failures(self):
        failed = {
            "status": "failed", "stdout": "", "stderr": "resource enforcement failed",
            "exit_code": None, "command": "dpkg-deb", "arguments": ["dpkg-deb"],
            "working_directory": ".", "duration": 0.01,
            "error_code": "resource_limit_enforcement_failed",
            "resource_control": {"verification": "failed"},
        }
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            debian_packaging, "validate_staging", return_value={"valid": True},
        ), self.assertRaises(debian_packaging.PackagingError) as raised:
            debian_packaging.build_deb(
                packaging_recipe(),
                {"version": "1.0-1", "staging_directory": str(Path(temporary) / "staging")},
                temporary,
                runner=lambda *_args, **_kwargs: dict(failed),
            )
        self.assertEqual(raised.exception.code, "resource_limit_enforcement_failed")
        self.assertEqual(raised.exception.details["command"]["resource_control"], {"verification": "failed"})

        with tempfile.TemporaryDirectory() as temporary:
            deb = Path(temporary) / "example.deb"
            deb.write_bytes(b"not-needed")
            with self.assertRaises(deb_inspector.DebInspectionCommandError) as inspected:
                deb_inspector.inspect_deb(
                    deb, workspace=temporary,
                    runner=lambda *_args, **_kwargs: dict(failed),
                )
        self.assertEqual(inspected.exception.code, "resource_limit_enforcement_failed")
        self.assertEqual(inspected.exception.details["command"]["resource_control"], {"verification": "failed"})

    def make_workspace(self, root):
        workspace = Path(root)
        for name in ("source", "staging", "artifacts", "logs"):
            (workspace / name).mkdir()
        (workspace / "source/bin").mkdir()
        executable = workspace / "source/bin/demo"
        executable.write_text("#!/bin/sh\necho demo\n")
        executable.chmod(0o755)
        (workspace / "source/etc/demo").mkdir(parents=True)
        (workspace / "source/etc/demo/demo.conf").write_text("port=8080\n")
        return workspace

    def test_staging_separates_payload_metadata_config_and_systemd(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            recipe = packaging_recipe()
            result = debian_packaging.prepare_staging(
                recipe, {"output": {"path": str(workspace / "source")}, "version": "1.2.3-1"}, workspace,
            )
            staging = workspace / "staging"
            self.assertTrue((staging / "opt/demo/bin/demo").is_file())
            self.assertTrue((staging / "etc/demo/demo.conf").is_file())
            self.assertTrue((staging / "usr/lib/systemd/system/demo.service").is_file())
            self.assertTrue((staging / "DEBIAN/control").is_file())
            self.assertEqual(result["conffiles"], ["/etc/demo/demo.conf"])
            self.assertIn("Depends: ca-certificates", result["control"])
            self.assertIn("Section: misc", result["control"])
            self.assertIn("Priority: optional", result["control"])
            self.assertNotIn("nodejs", result["control"])
            self.assertIn("addgroup --system demo-app", result["maintainer_scripts"]["postinst"])
            self.assertIn("echo configured", result["maintainer_scripts"]["postinst"])
            self.assertIn("systemctl restart demo.service", result["maintainer_scripts"]["postinst"])
            self.assertLess(result["maintainer_scripts"]["postinst"].index("adduser --system"), result["maintainer_scripts"]["postinst"].index("echo configured"))
            self.assertLess(result["maintainer_scripts"]["postinst"].index("echo configured"), result["maintainer_scripts"]["postinst"].index("systemctl restart demo.service"))
            self.assertIn("User=demo-service", result["systemd"]["content"])
            self.assertNotIn("User=demo-app", result["systemd"]["content"])
            self.assertEqual((staging / "opt/demo").stat().st_mode & 0o777, 0o750)
            self.assertEqual((staging / "opt/demo/etc/demo/demo.conf").stat().st_mode & 0o777, 0o640)
            self.assertEqual((staging / "opt/demo/bin/demo").stat().st_mode & 0o777, 0o751)
            self.assertEqual((staging / "etc/demo/demo.conf").stat().st_mode & 0o777, 0o640)

    def test_create_if_missing_uses_template_and_no_conffiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            result = debian_packaging.prepare_staging(
                packaging_recipe(service=False, policy="create_if_missing"),
                {"output": {"path": str(workspace / "source")}, "version": "1.0-1"}, workspace,
            )
            self.assertEqual(result["conffiles"], [])
            self.assertTrue(result["systemd"]["configured"])
            self.assertFalse(result["systemd"]["enabled"])
            self.assertTrue((workspace / "staging/usr/lib/systemd/system/demo.service").is_file())
            self.assertNotIn("systemctl enable demo.service", result["maintainer_scripts"]["postinst"])
            self.assertTrue((workspace / "staging/usr/share/demo/config-templates/etc/demo/demo.conf").is_file())
            self.assertIn("if [ ! -e /etc/demo/demo.conf ]", result["maintainer_scripts"]["postinst"])
            self.assertIn('if [ "$1" = purge ]; then rm -f /etc/demo/demo.conf; fi', result["maintainer_scripts"]["postrm"])

    def test_configured_files_only_maps_source_without_copying_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            configured = packaging_recipe(service=False)
            configured["install"]["content"]["source"] = "configured_files"
            configured["install"]["config_files"] = [{"source": "etc/demo/demo.conf", "destination": "/etc/demo/demo.conf"}]
            result = debian_packaging.prepare_staging(
                configured, {"output": {"path": str(workspace / "source")}, "version": "1.0-1"}, workspace,
            )
            self.assertFalse(result["include_output"])
            self.assertFalse((workspace / "staging/opt/demo").exists())
            self.assertEqual((workspace / "staging/etc/demo/demo.conf").read_text(), "port=8080\n")

    def test_each_mapping_uses_its_own_debian_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            (workspace / "source/profile.sh").write_text("echo demo\n")
            configured = packaging_recipe(service=False)
            configured["install"]["content"]["source"] = "configured_files"
            configured["install"]["config_files"] = [
                {"source": "profile.sh", "destination": "/etc/profile.d/demo.sh", "policy": "replace"},
                {"source": "etc/demo/demo.conf", "destination": "/etc/demo/demo.conf", "policy": "dpkg_conffile"},
            ]
            result = debian_packaging.prepare_staging(
                configured, {"output": {"path": str(workspace / "source")}, "version": "1.0-1"}, workspace,
            )
            self.assertEqual(result["conffiles"], ["/etc/demo/demo.conf"])
            self.assertEqual([row["policy"] for row in result["configurations"]], ["replace", "dpkg_conffile"])

    def test_root_owner_never_generates_account_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            configured = packaging_recipe(service=False)
            configured["install"]["owner"] = {"user": "root", "group": "root", "create_user": True, "create_group": True}
            configured["install"]["account"] = {"user": "root", "group": "root", "create_user": True, "create_group": True}
            configured["install"]["config_files"] = []
            result = debian_packaging.prepare_staging(
                configured, {"output": {"path": str(workspace / "source")}, "version": "1.0-1"}, workspace,
            )
            postinst = result["maintainer_scripts"].get("postinst", "")
            self.assertNotIn("adduser", postinst)
            self.assertNotIn("addgroup", postinst)

    def test_multiple_build_outputs_keep_relative_paths_below_install_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            (workspace / "source/dist").mkdir()
            (workspace / "source/dist/app.js").write_text("built\n")
            (workspace / "source/public").mkdir()
            (workspace / "source/public/index.html").write_text("public\n")
            (workspace / "source/package.json").write_text("{}\n")
            recipe = packaging_recipe(service=False)
            recipe["install"]["config_files"] = []
            result = debian_packaging.prepare_staging(
                recipe,
                {"output": {"mode": "paths", "paths": [
                    {"path": str(workspace / "source/dist")},
                    {"path": str(workspace / "source/public")},
                    {"path": str(workspace / "source/package.json")},
                ]}, "version": "1.0-1"},
                workspace,
            )
            staging = workspace / "staging/opt/demo"
            self.assertTrue((staging / "dist/app.js").is_file())
            self.assertTrue((staging / "public/index.html").is_file())
            self.assertTrue((staging / "package.json").is_file())
            self.assertIn("dist/app.js", result["content_files"])
            self.assertIn("public/index.html", result["content_files"])
            self.assertIn("package.json", result["content_files"])

    def test_legacy_individual_nested_file_keeps_basename_staging_behavior(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            recipe = packaging_recipe(service=False)
            recipe["install"]["config_files"] = []
            result = debian_packaging.prepare_staging(
                recipe,
                {"output": {"mode": "path", "path": str(workspace / "source/bin/demo")}, "version": "1.0-1"},
                workspace,
            )
            staging = workspace / "staging/opt/demo"
            self.assertTrue((staging / "demo").is_file())
            self.assertFalse((staging / "bin/demo").exists())
            self.assertEqual(result["content_files"], ["demo"])

    def test_archive_payload_staging_preserves_new_file_directory_and_entire_archive_layouts(self):
        cases = [
            ({"mode": "paths", "include": ["bin/demo"], "exclude": []}, ["bin/demo"]),
            ({"mode": "paths", "include": ["bin/", "public/"], "exclude": []}, ["bin/demo", "public/index.html"]),
            ({"mode": "entire_archive", "include": [], "exclude": []}, ["bin/demo", "etc/demo/demo.conf", "public/index.html"]),
            ({"mode": "entire_archive", "include": [], "exclude": ["etc/", "public/"]}, ["bin/demo"]),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                workspace = self.make_workspace(temporary)
                (workspace / "source/public").mkdir()
                (workspace / "source/public/index.html").write_text("index\n")
                recipe = archive_packaging_recipe(payload)
                plan = upstream_archive.resolve_payload(recipe, workspace / "source")
                result = debian_packaging.prepare_staging(
                    recipe, {"output": {"mode": "archive_payload", "payload": plan}, "version": "1.0-1"}, workspace,
                )
                destination = workspace / "staging/opt/demo"
                self.assertEqual(result["content_files"], expected)
                self.assertEqual(sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()), expected)
                self.assertEqual(len(result["content_files"]), len(set(result["content_files"])))

    def test_archive_payload_staging_applies_existing_modes_and_ownership_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            recipe = archive_packaging_recipe({"mode": "paths", "include": ["bin/"], "exclude": []})
            plan = upstream_archive.resolve_payload(recipe, workspace / "source")
            result = debian_packaging.prepare_staging(
                recipe, {"output": {"mode": "archive_payload", "payload": plan}, "version": "1.0-1"}, workspace,
            )
            destination = workspace / "staging/opt/demo"
            self.assertEqual(destination.stat().st_mode & 0o777, 0o750)
            self.assertEqual((destination / "bin").stat().st_mode & 0o777, 0o750)
            self.assertEqual((destination / "bin/demo").stat().st_mode & 0o777, 0o751)
            self.assertEqual(result["ownership"], {"user": "demo-app", "group": "demo-app", "applied_by": "postinst"})
            self.assertIn("chown -R demo-app:demo-app /opt/demo", result["maintainer_scripts"]["postinst"])

    def test_archive_payload_legacy_and_new_nested_file_layouts_differ_exactly(self):
        cases = [
            ({"mode": "paths", "include": ["bin/demo"], "exclude": []}, "bin/demo", "demo"),
            ({"mode": "paths", "include": ["bin/demo"], "exclude": [], "legacy_file_layout": "basename"}, "demo", "bin/demo"),
        ]
        for payload, present, absent in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                workspace = self.make_workspace(temporary)
                recipe = archive_packaging_recipe(payload)
                plan = upstream_archive.resolve_payload(recipe, workspace / "source")
                result = debian_packaging.prepare_staging(
                    recipe, {"output": {"mode": "archive_payload", "payload": plan}, "version": "1.0-1"}, workspace,
                )
                destination = workspace / "staging/opt/demo"
                self.assertTrue((destination / present).is_file())
                self.assertFalse((destination / absent).exists())
                self.assertEqual(result["content_files"], [present])

    def test_archive_exclusion_does_not_override_explicit_configuration_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            recipe = archive_packaging_recipe({
                "mode": "entire_archive", "include": [], "exclude": ["etc/demo/demo.conf"],
            })
            recipe["install"]["config_files"] = [{
                "source": "etc/demo/demo.conf", "destination": "/etc/demo/demo.conf", "policy": "replace",
            }]
            plan = upstream_archive.resolve_payload(recipe, workspace / "source")
            self.assertNotIn("etc/demo/demo.conf", [row["relative_path"] for row in plan["files"]])
            debian_packaging.prepare_staging(
                recipe, {"output": {"mode": "archive_payload", "payload": plan}, "version": "1.0-1"}, workspace,
            )
            self.assertEqual((workspace / "staging/etc/demo/demo.conf").read_text(), "port=8080\n")
            self.assertFalse((workspace / "staging/opt/demo/etc/demo/demo.conf").exists())

    def test_archive_payload_staging_revalidates_runtime_confinement(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            recipe = archive_packaging_recipe({"mode": "paths", "include": ["bin/demo"], "exclude": []})
            plan = upstream_archive.resolve_payload(recipe, workspace / "source")
            outside = workspace / "outside"
            outside.write_text("outside\n")
            plan["files"][0]["path"] = str(outside)
            with self.assertRaises(debian_packaging.PackagingError) as caught:
                debian_packaging.prepare_staging(
                    recipe, {"output": {"mode": "archive_payload", "payload": plan}, "version": "1.0-1"}, workspace,
                )
            self.assertEqual(caught.exception.code, "invalid_install_content")

    def test_configuration_mapping_is_resolved_from_source_not_first_selected_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            (workspace / "source/app").mkdir()
            (workspace / "source/app/main.py").write_text("print('ok')\n")
            (workspace / "source/packaging").mkdir()
            (workspace / "source/packaging/demo.env").write_text("DEMO=1\n")
            recipe = packaging_recipe(service=False, policy="create_if_missing")
            recipe["install"]["config_files"] = [{
                "source": "packaging/demo.env", "destination": "/etc/demo/demo.env", "policy": "create_if_missing",
            }]
            result = debian_packaging.prepare_staging(
                recipe,
                {"output": {"mode": "paths", "paths": [{"path": str(workspace / "source/app")}]}, "version": "1.0-1"},
                workspace,
            )
            self.assertEqual((workspace / "staging/usr/share/demo/config-templates/etc/demo/demo.env").read_text(), "DEMO=1\n")
            self.assertFalse((workspace / "staging/opt/demo/packaging").exists())

    def test_missing_mapping_source_reports_mapping_context_and_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            recipe = packaging_recipe(service=False)
            recipe["install"]["config_files"] = [
                {"source": "etc/demo/demo.conf", "destination": "/etc/demo/demo.conf"},
                {"source": "dist/app", "destination": "/opt/demo/app"},
            ]
            with self.assertRaisesRegex(
                debian_packaging.PackagingError,
                rf'Install mapping 2 failed: source "dist/app" resolved to "{re.escape(str(workspace / "source" / "dist" / "app"))}"; destination "/opt/demo/app"; does not exist',
            ) as raised:
                debian_packaging.prepare_staging(
                    recipe, {"output": {"path": str(workspace / "source")}, "version": "1.0-1"}, workspace,
                )
            self.assertEqual(raised.exception.details, {
                "mapping_index": 2,
                "source": "dist/app",
                "resolved_source": str(workspace / "source" / "dist" / "app"),
                "destination": "/opt/demo/app",
                "cause": "does not exist",
            })

    def test_configuration_mapping_accepts_safe_internal_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            (workspace / "source/demo-link.conf").symlink_to("etc/demo/demo.conf")
            recipe = packaging_recipe(service=False)
            recipe["install"]["content"]["source"] = "configured_files"
            recipe["install"]["config_files"] = [
                {"source": "demo-link.conf", "destination": "/etc/demo/demo.conf"},
            ]
            debian_packaging.prepare_staging(
                recipe, {"output": {"path": str(workspace / "source")}, "version": "1.0-1"}, workspace,
            )
            self.assertEqual((workspace / "staging/etc/demo/demo.conf").read_text(), "port=8080\n")

    def test_configuration_mapping_rejects_symlink_escaping_source_with_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            outside = workspace / "outside.conf"
            outside.write_text("outside\n")
            (workspace / "source/demo-link.conf").symlink_to(outside)
            recipe = packaging_recipe(service=False)
            recipe["install"]["content"]["source"] = "configured_files"
            recipe["install"]["config_files"] = [
                {"source": "demo-link.conf", "destination": "/etc/demo/demo.conf"},
            ]
            with self.assertRaisesRegex(
                debian_packaging.PackagingError,
                rf'Install mapping 1 failed: source "demo-link.conf" resolved to "{re.escape(str(outside))}"; destination "/etc/demo/demo.conf"; source escapes acquired source',
            ):
                debian_packaging.prepare_staging(
                    recipe, {"output": {"path": str(workspace / "source")}, "version": "1.0-1"}, workspace,
                )

    def test_preserves_safe_relative_symlinked_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            (workspace / "source/link").symlink_to("bin/demo")
            debian_packaging.prepare_staging(packaging_recipe(service=False), {"output": {"path": str(workspace / "source")}, "version": "1.0-1"}, workspace)
            self.assertEqual((workspace / "staging/opt/demo/link").readlink(), Path("bin/demo"))

    def test_rejects_symlink_escaping_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            (workspace / "source/link").symlink_to("../../outside")
            with self.assertRaisesRegex(debian_packaging.PackagingError, "escapes"):
                debian_packaging.prepare_staging(packaging_recipe(service=False), {"output": {"path": str(workspace / "source")}, "version": "1.0-1"}, workspace)

    def test_dry_run_generates_inspectable_previews_without_build_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            for name in ("source", "staging", "artifacts", "logs"):
                (workspace / name).mkdir()
            result = debian_packaging.prepare_staging(
                packaging_recipe(service=True),
                {"output": {"path": str(workspace / "source/dist")}, "version": "2.0-1"},
                workspace, preview=True,
            )
            self.assertTrue(result["preview"])
            self.assertFalse(result["content_available"])
            self.assertTrue(result["warnings"])
            self.assertTrue((workspace / "staging/DEBIAN/control").is_file())
            self.assertTrue((workspace / "staging/usr/lib/systemd/system/demo.service").is_file())
            self.assertFalse(any((workspace / "artifacts").iterdir()))

    def test_fhs_executable_mapping_account_and_persistent_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            recipe = packaging_recipe(service=False)
            recipe["install"].update({
                "destination": "", "content": {"source": "configured_files"},
                "owner": {"user": "root", "group": "root", "create_user": False, "create_group": False},
                "account": {"user": "demo", "group": "demo", "create_user": True, "create_group": True},
                "directories": [{"path": "/etc/demo", "owner": "demo", "group": "demo", "mode": "0750"}, {"path": "/var/lib/demo", "owner": "demo", "group": "demo", "mode": "0750"}],
                "config_files": [{"source": "bin/demo", "destination": "/usr/bin/demo", "policy": "replace", "owner": "root", "group": "root", "mode": "0755"}],
            })
            recipe = validate_recipe_metadata(recipe)
            result = debian_packaging.prepare_staging(recipe, {"output": {"path": str(workspace / "source")}, "version": "1.0-1"}, workspace)
            binary = workspace / "staging/usr/bin/demo"
            self.assertTrue(binary.is_file())
            self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
            self.assertIn("adduser --system --ingroup demo --no-create-home demo", result["maintainer_scripts"]["postinst"])
            self.assertIn("install -d -m 0750 -o demo -g demo /var/lib/demo", result["maintainer_scripts"]["postinst"])
            self.assertIn("Depends: ca-certificates, adduser", result["control"])
            self.assertEqual(result["configurations"][0]["owner"], "root")
            self.assertEqual(result["configurations"][0]["mode"], "0755")

    def test_mapping_defaults_remain_backward_compatible(self):
        recipe = packaging_recipe(service=False)
        mapping = recipe["install"]["config_files"][0]
        self.assertNotIn("mode", mapping)
        self.assertNotIn("owner", mapping)
        self.assertEqual(recipe["install"]["account"]["user"], "demo-app")

    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb unavailable")
    def test_builds_and_inspects_real_deb_in_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(temporary)
            recipe = packaging_recipe(service=False)
            staging = debian_packaging.prepare_staging(
                recipe, {"output": {"path": str(workspace / "source")}, "version": "3.4.5-1"}, workspace,
            )
            artifact = debian_packaging.build_deb(recipe, staging, workspace, inspector=deb_inspector.inspect_deb)
            self.assertTrue(Path(artifact["path"]).is_file())
            self.assertEqual(artifact["name"], "demo_3.4.5-1_all.deb")
            self.assertEqual(len(artifact["sha256"]), 64)
            self.assertGreater(artifact["size"], 0)
            self.assertEqual(artifact["inspection"]["package"], "demo")
            self.assertEqual(artifact["inspection"]["depends"], "ca-certificates, adduser")
            self.assertEqual(len(artifact["inspection"]["commands"]), 3)
            self.assertTrue(all("resource_control" in row for row in artifact["inspection"]["commands"]))
            root_entry = next(row for row in artifact["inspection"]["files"] if row["path"] == "./")
            self.assertEqual(root_entry["mode"], "drwxr-xr-x")


if __name__ == "__main__":
    unittest.main()
