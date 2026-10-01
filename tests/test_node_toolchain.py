import base64
import hashlib
import io
import json
import os
import tarfile
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
import subprocess

from debbuilder import node_toolchain


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def archive(mode, files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode=mode) as tar:
        for name, content, permissions in files:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = permissions
            tar.addfile(info, io.BytesIO(content))
    return output.getvalue()


class FixtureNetwork:
    def __init__(self, node_version="24.12.0", manager="npm", manager_version="11.7.0"):
        self.node_version = node_version
        self.manager = manager
        self.manager_version = manager_version
        self.node_archive = archive("w:xz", [
            (f"node-v{node_version}-linux-x64/bin/node", b"#!/bin/sh\necho v%s\n" % node_version.encode(), 0o755),
        ])
        cli = "bin/npm-cli.js" if manager == "npm" else "bin/pnpm.cjs"
        self.manager_archive = archive("w:gz", [(f"package/{cli}", b"// prepared cli\n", 0o644)])
        digest = base64.b64encode(hashlib.sha512(self.manager_archive).digest()).decode()
        self.integrity = f"sha512-{digest}"
        self.calls = []
        self.lock = threading.Lock()

    @property
    def releases(self):
        return [{"version": f"v{self.node_version}", "npm": "11.7.0", "files": ["linux-x64"]}]

    @property
    def manager_metadata(self):
        return {"versions": {self.manager_version: {"dist": {"tarball": f"https://registry.invalid/{self.manager}.tgz", "integrity": self.integrity}}}}

    def open(self, url, timeout=0):
        with self.lock:
            self.calls.append(url)
        filename = f"node-v{self.node_version}-linux-x64.tar.xz"
        if url.endswith("SHASUMS256.txt"):
            return Response(f"{hashlib.sha256(self.node_archive).hexdigest()}  {filename}\n".encode())
        if url.endswith(filename):
            return Response(self.node_archive)
        if url.endswith(f"/{self.manager}.tgz"):
            return Response(self.manager_archive)
        raise AssertionError(url)


class NodeToolchainTests(unittest.TestCase):
    def test_node_range_semantics_cover_real_projects(self):
        self.assertTrue(node_toolchain.version_satisfies("22.19.1", "^22.19.0"))
        self.assertFalse(node_toolchain.version_satisfies("23.0.0", "^22.19.0"))
        self.assertTrue(node_toolchain.version_satisfies("24.12.0", ">=24 <25"))
        self.assertTrue(node_toolchain.version_satisfies("24.12.0", "24.x"))
        self.assertIsNone(node_toolchain.version_satisfies("24.12.0", "latest"))

    def test_debbuilder_and_seerr_resolve_independently(self):
        releases = [
            {"version": "v24.12.0", "npm": "11.7.0", "files": ["linux-x64"]},
            {"version": "v22.21.1", "npm": "10.9.4", "files": ["linux-x64"]},
            {"version": "v22.19.0", "npm": "10.9.3", "files": ["linux-x64"]},
        ]
        npm = {"versions": {"11.6.2": {"dist": {"tarball": "https://registry.invalid/npm", "integrity": "sha512-AA=="}}}}
        pnpm = {"versions": {"10.24.0": {"dist": {"tarball": "https://registry.invalid/pnpm", "integrity": "sha512-AA=="}}}}
        debbuilder = node_toolchain.resolve({"node": ">=24 <25", "npm": "11.x"}, package_manager="npm", releases=releases, manager_metadata=npm)
        seerr = node_toolchain.resolve({"node": "^22.19.0", "pnpm": "10.24.0"}, package_manager="pnpm", releases=releases, manager_metadata=pnpm)
        self.assertEqual((debbuilder["node"]["version"], debbuilder["package_manager"]["version"]), ("24.12.0", "11.6.2"))
        self.assertEqual((seerr["node"]["version"], seerr["package_manager"]["version"]), ("22.21.1", "10.24.0"))

    def test_unsupported_and_unsatisfied_ranges_fail_closed(self):
        releases = [{"version": "v24.12.0", "npm": "11.7.0", "files": ["linux-x64"]}]
        with self.assertRaisesRegex(node_toolchain.NodeToolchainError, "unsupported") as unsupported:
            node_toolchain.resolve_node("latest", releases, platform_name="linux", node_arch="x64")
        self.assertEqual(unsupported.exception.code, "node_range_unsupported")
        with self.assertRaisesRegex(node_toolchain.NodeToolchainError, "satisfies") as unsatisfied:
            node_toolchain.resolve_node("^22.19.0", releases, platform_name="linux", node_arch="x64")
        self.assertEqual(unsatisfied.exception.code, "node_range_unsatisfied")

    def test_registry_acquisition_failure_is_package_manager_specific(self):
        releases = [{"version": "v24.12.0", "npm": "11.7.0", "files": ["linux-x64"]}]

        def unavailable(_request, timeout=0):
            raise OSError("registry unavailable")

        with self.assertRaises(node_toolchain.NodeToolchainError) as raised:
            node_toolchain.resolve(
                {"node": "24.x", "npm": "11.x"}, package_manager="npm",
                releases=releases, opener=unavailable,
            )
        self.assertEqual(raised.exception.code, "package_manager_acquisition_failed")
        self.assertEqual(raised.exception.details["package_manager"], "npm")
        self.assertEqual(raised.exception.details["requested_range"], "11.x")

    def test_prepare_is_run_local_cached_and_offline_afterward(self):
        network = FixtureNetwork()
        resolution = node_toolchain.resolve({"node": "24.x", "npm": "11.x"}, package_manager="npm", releases=network.releases, manager_metadata=network.manager_metadata)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = node_toolchain.prepare(resolution, workspace=root / "run-a", cache=root / "cache", opener=network.open)
            calls = len(network.calls)
            second = node_toolchain.prepare(resolution, workspace=root / "run-b", cache=root / "cache", opener=network.open)
            self.assertEqual(len(network.calls), calls + 1)  # official checksum remains authority
            for run, prepared in ((root / "run-a", first), (root / "run-b", second)):
                environment = node_toolchain.validate_prepared(prepared, workspace=run)
                self.assertEqual(environment["PATH"].split(":")[0], str(run / "toolchain/bin"))
                self.assertEqual(prepared["identity"]["node"]["version"], "24.12.0")
            self.assertNotEqual(first["environment"]["PATH"], second["environment"]["PATH"])

    def test_prepared_node_executes_without_any_runtime_acquisition(self):
        network = FixtureNetwork()
        resolution = node_toolchain.resolve({"node": "24.x", "npm": "11.x"}, package_manager="npm", releases=network.releases, manager_metadata=network.manager_metadata)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = node_toolchain.prepare(resolution, workspace=root / "run", cache=root / "cache", opener=network.open)
            environment = {"PATH": node_toolchain.validate_prepared(prepared, workspace=root / "run")["PATH"]}
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("offline execution attempted acquisition")):
                result = subprocess.run(["node", "--version"], env=environment, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), "v24.12.0")
            self.assertNotIn("corepack", prepared["environment"]["PATH"])

    def test_concurrent_runs_share_cache_without_sharing_run_path(self):
        network = FixtureNetwork()
        resolution = node_toolchain.resolve({"node": "24.x", "npm": "11.x"}, package_manager="npm", releases=network.releases, manager_metadata=network.manager_metadata)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda name: node_toolchain.prepare(resolution, workspace=root / name, cache=root / "cache", opener=network.open), ("a", "b")))
            self.assertNotEqual(results[0]["environment"]["PATH"], results[1]["environment"]["PATH"])
            self.assertEqual(len(list((root / "cache/node/linux-x64/24.12.0").glob("manifest.json"))), 1)

    def test_conflicting_node_22_pnpm_and_node_24_npm_use_distinct_roots(self):
        npm = FixtureNetwork("24.12.0", "npm", "11.7.0")
        pnpm = FixtureNetwork("22.21.1", "pnpm", "10.24.0")
        npm_resolution = node_toolchain.resolve({"node": "24.x", "npm": "11.x"}, package_manager="npm", releases=npm.releases, manager_metadata=npm.manager_metadata)
        pnpm_resolution = node_toolchain.resolve({"node": "^22.19.0", "pnpm": "10.24.0"}, package_manager="pnpm", releases=pnpm.releases, manager_metadata=pnpm.manager_metadata)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = node_toolchain.prepare(npm_resolution, workspace=root / "node-24", cache=root / "cache", opener=npm.open)
            second = node_toolchain.prepare(pnpm_resolution, workspace=root / "node-22", cache=root / "cache", opener=pnpm.open)
            self.assertEqual(first["identity"]["node"]["version"], "24.12.0")
            self.assertEqual(second["identity"]["node"]["version"], "22.21.1")
            self.assertTrue((root / "node-24/toolchain/bin/npm").is_file())
            self.assertTrue((root / "node-22/toolchain/bin/pnpm").is_file())
            self.assertNotEqual(first["environment"]["PATH"], second["environment"]["PATH"])

    def test_cancellation_checkpoint_removes_partial_cache_staging(self):
        network = FixtureNetwork()
        resolution = node_toolchain.resolve({"node": "24.x", "npm": "11.x"}, package_manager="npm", releases=network.releases, manager_metadata=network.manager_metadata)
        calls = 0

        def cancel_during_archive():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("cancelled fixture")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(KeyboardInterrupt):
                node_toolchain.prepare(resolution, workspace=root / "run", cache=root / "cache", opener=network.open, checkpoint=cancel_during_archive)
            version_root = root / "cache/node/linux-x64"
            self.assertFalse((version_root / "24.12.0").exists())
            self.assertEqual([path for path in version_root.glob(".24.12.0.*") if path.is_dir()], [])

    def test_integrity_failure_and_missing_offline_toolchain_fail_closed(self):
        network = FixtureNetwork()
        resolution = node_toolchain.resolve({"node": "24.x", "npm": "11.x"}, package_manager="npm", releases=network.releases, manager_metadata=network.manager_metadata)
        resolution["package_manager"]["integrity"] = "sha512-" + base64.b64encode(b"wrong").decode()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(node_toolchain.NodeToolchainError) as mismatch:
                node_toolchain.prepare(resolution, workspace=root / "run", cache=root / "cache", opener=network.open)
            self.assertEqual(mismatch.exception.code, "package_manager_integrity_mismatch")
            with self.assertRaises(node_toolchain.NodeToolchainError) as missing:
                node_toolchain.validate_prepared({"identity": {"package_manager": {"name": "npm"}}, "environment": {}}, workspace=root / "run")
            self.assertEqual(missing.exception.code, "prepared_node_toolchain_missing")


if __name__ == "__main__":
    unittest.main()
