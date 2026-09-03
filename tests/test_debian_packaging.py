import shutil
import tempfile
import unittest
from pathlib import Path

from debbuilder import deb_inspector, debian_packaging
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


class DebianPackagingTests(unittest.TestCase):
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
            self.assertFalse(result["systemd"]["enabled"])
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
            self.assertEqual(artifact["inspection"]["depends"], "ca-certificates")
            root_entry = next(row for row in artifact["inspection"]["files"] if row["path"] == "./")
            self.assertEqual(root_entry["mode"], "drwxr-xr-x")


if __name__ == "__main__":
    unittest.main()
