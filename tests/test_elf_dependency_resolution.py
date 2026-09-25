"""Network-free checks for Bookworm ELF dependency proposals."""
from __future__ import annotations

import dataclasses
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import elf_dependency_resolution as resolver
from debbuilder.elf_inspection import inspect_elf


class FakeBookworm:
    def __init__(self, outputs=None, *, tool=True, release="bookworm", architecture="amd64"):
        self.outputs = outputs or {}
        self.tool = tool
        self.release = release
        self.architecture = architecture
        self.calls = []

    def execute(self, arguments, *, timeout):
        self.calls.append(tuple(arguments))
        if timeout != resolver.COMMAND_TIMEOUT:
            raise AssertionError("unbounded command")
        if arguments == ["cat", "/etc/os-release"]:
            value = f"ID=debian\nVERSION_CODENAME={self.release}\n"
        elif arguments == ["dpkg", "--print-architecture"]:
            value = self.architecture + "\n"
        elif arguments == ["test", "-x", "/usr/bin/dpkg-shlibdeps"]:
            return {"stdout": "", "stderr": "", "exit_code": 0 if self.tool else 1}
        elif arguments == ["test", "-f", resolver.WORK_ROOT + "/debian/control"]:
            return {"stdout": "", "stderr": "", "exit_code": 0}
        elif arguments[:2] == ["realpath", "-e"]:
            value = ("/usr/lib/libc.so.6" if arguments[2] == "/lib/libc.so.6" else arguments[2]) + "\n"
        elif arguments[:2] == ["dpkg-query", "-S"]:
            path = arguments[2]
            package = {"/lib64/ld-linux-x86-64.so.2": "libc6", "/lib/libc.so.6": "libc6", "/lib/libz.so.1": "zlib1g"}.get(path)
            if package is None:
                return {"stdout": "", "stderr": "unknown path", "exit_code": 1}
            value = f"{package}:amd64: {path}\n"
        elif "dpkg-shlibdeps" in arguments:
            installed = arguments[-1].removeprefix("-e" + resolver.MOUNT_ROOT).removeprefix("-e" + resolver.WORK_ROOT + "/filtered")
            value = self.outputs[installed]
        elif arguments[:2] == ["dpkg", "--compare-versions"]:
            return {"stdout": "", "stderr": "", "exit_code": 0 if arguments[2] > arguments[4] else 1}
        else:
            raise AssertionError(f"unexpected target command: {arguments!r}")
        return {"stdout": value, "stderr": "", "exit_code": 0}


LIBC = "\n".join((
    "dpkg-shlibdeps: debug: Library libc.so.6 found in /lib/libc.so.6",
    "dpkg-shlibdeps: debug: Using symbols file /var/lib/dpkg/info/libc6:amd64.symbols for libc.so.6",
    "shlibs:Depends=libc6 (>= 2.34)",
)) + "\n"
LIBC_ZLIB = "\n".join((
    "dpkg-shlibdeps: debug: Library libc.so.6 found in /lib/libc.so.6",
    "dpkg-shlibdeps: debug: Using symbols file /var/lib/dpkg/info/libc6:amd64.symbols for libc.so.6",
    "dpkg-shlibdeps: debug: Library libz.so.1 found in /lib/libz.so.1",
    "dpkg-shlibdeps: debug: Using shlibs+objdump for libz.so.1 (file /lib/libz.so.1)",
    "shlibs:Depends=libc6 (>= 2.34), zlib1g (>= 1:1.2.13)",
)) + "\n"


@unittest.skipUnless(shutil.which("gcc") and shutil.which("readelf"), "gcc and readelf are required")
class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="debbuilder-resolver-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "DEBIAN").mkdir()
        (self.root / "DEBIAN/control").write_text("Package: fixture\nVersion: 1\nArchitecture: amd64\nDescription: fixture\n")
        (self.root / "opt/app/bin").mkdir(parents=True)
        (self.root / "opt/app/lib").mkdir(parents=True)

    def compile(self, installed="/opt/app/bin/app", *, source='extern int puts(const char *); int main(void) { return puts("x"); }', options=()):
        output = self.root / installed.lstrip("/")
        source_path = self.root / "source.c"
        source_path.write_text(source)
        subprocess.run(["gcc", "-O0", "-o", str(output), str(source_path), *options], check=True, capture_output=True, timeout=30)
        return inspect_elf(output)

    def resolve(self, entrypoints, outputs=None, **kwargs):
        environment = kwargs.pop("environment", FakeBookworm(outputs))
        return resolver.resolve_elf_dependencies(
            self.root, entrypoints, distribution="bookworm", architecture="amd64", environment=environment, **kwargs,
        )

    def test_dynamic_libc_versioned_provenance_and_no_host_metadata(self):
        binary = self.compile()
        fake = FakeBookworm({"/opt/app/bin/app": LIBC})
        result = self.resolve({"/opt/app/bin/app": binary}, environment=fake)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.depends, ("libc6 (>= 2.34)",))
        self.assertEqual(dataclasses.asdict(result.requirements[0]), {
            "requester": "/opt/app/bin/app", "soname": "libc.so.6", "selected_library": "/lib/libc.so.6",
            "status": "resolved_external", "package": "libc6", "version_constraint": "libc6 (>= 2.34)",
            "metadata_source": "symbols",
            "operator_decision": "",
        })
        self.assertTrue(all("/var/lib/dpkg" not in " ".join(call) for call in fake.calls))
        self.assertTrue(any("dpkg-shlibdeps" in call for call in fake.calls))
        self.assertIn(("dpkg-query", "-S", "/lib/libc.so.6"), fake.calls)
        self.assertNotIn(("dpkg-query", "-S", "/usr/lib/libc.so.6"), fake.calls)

    def test_explicit_manual_and_ignore_decisions_are_provenanced(self):
        library_source = self.root / "ghost.c"
        library_source.write_text("int ghost(void) { return 1; }")
        library = self.root / "libghost.so.1"
        subprocess.run(["gcc", "-shared", "-fPIC", "-Wl,-soname,libghost.so.1", "-o", str(library), str(library_source)], check=True, capture_output=True)
        binary = self.compile(source='extern int puts(const char *); extern int ghost(void); int main(void) { puts("x"); return ghost(); }',
                              options=("-L" + str(self.root), "-l:libghost.so.1"))
        self.assertIn("libghost.so.1", binary.needed)
        for action in ("ignore", "manual"):
            with self.subTest(action=action):
                env = FakeBookworm({"/opt/app/bin/app": LIBC})
                env.filtered_installed = frozenset({"/opt/app/bin/app"})
                decision = {"soname": "libghost.so.1", "action": action, "reason": "operator verified"}
                if action == "manual":
                    decision["relation"] = "libghost1 (>= 1.0)"
                result = self.resolve({"/opt/app/bin/app": binary}, environment=env, overrides=(decision,))
                row = next(row for row in result.requirements if row.soname == "libghost.so.1")
                self.assertEqual(row.status, "ignored_explicitly" if action == "ignore" else "manually_resolved")
                self.assertEqual(row.operator_decision, "operator verified")
                self.assertEqual("libghost1 (>= 1.0)" in result.depends, action == "manual")
                self.assertTrue(any("/filtered/opt/app/bin/app" in " ".join(call) for call in env.calls))

    def test_merge_keeps_strongest_detected_constraint(self):
        env = FakeBookworm()
        merged = resolver._merge_depends(env, {
            "libc6", "libc6 (>= 2.31)", "libc6 (>= 2.34)", "ca-certificates",
        })
        self.assertEqual(merged, ("ca-certificates", "libc6 (>= 2.34)"))
        self.assertTrue(any(call[:2] == ("dpkg", "--compare-versions") for call in env.calls))

    def test_additional_debian_package_and_version(self):
        binary = self.compile(source="extern unsigned long compressBound(unsigned long); int main(void) { return (int)compressBound(42); }", options=("-Wl,--no-as-needed", "-lz"))
        self.assertIn("libz.so.1", binary.needed)
        # The fixture has no libc reference, so the metadata output is for zlib alone.
        output = LIBC_ZLIB if "libc.so.6" in binary.needed else "\n".join(LIBC_ZLIB.splitlines()[2:]).replace("shlibs:Depends=libc6 (>= 2.34), ", "shlibs:Depends=") + "\n"
        result = self.resolve({"/opt/app/bin/app": binary}, {"/opt/app/bin/app": output})
        self.assertTrue(any(row.package == "zlib1g" and row.version_constraint == "zlib1g (>= 1:1.2.13)" for row in result.requirements))

    def test_bundled_origin_and_transitive_external(self):
        library = self.compile("/opt/app/lib/libfoo.so.1.2", source='extern int puts(const char *); int foo(void) { return puts("foo"); }', options=("-shared", "-fPIC", "-Wl,-soname,libfoo.so.1"))
        link = self.root / "opt/app/lib/libfoo.so"
        (self.root / "opt/app/lib/libfoo.so.1").symlink_to("libfoo.so.1.2")
        link.symlink_to("libfoo.so.1")
        binary = self.compile(source="extern int foo(void); int main(void) { return foo(); }", options=("-L" + str(link.parent), "-lfoo", "-Wl,-rpath,$ORIGIN/../lib"))
        result = self.resolve({"/opt/app/bin/app": binary}, {"/opt/app/bin/app": LIBC, "/opt/app/lib/libfoo.so.1.2": LIBC})
        self.assertEqual(result.requirements[0].status, "bundled")
        self.assertEqual(result.requirements[0].selected_library, "/opt/app/lib/libfoo.so.1.2")
        self.assertIn(("/opt/app/lib/libfoo.so.1.2", "libc.so.6"), [(row.requester, row.soname) for row in result.requirements])
        self.assertEqual(result.depends, ("libc6 (>= 2.34)",))

    def test_missing_soname_and_ambiguous_mapping_fail_closed(self):
        binary = self.compile()
        bad = LIBC.replace("Library libc.so.6 found in /lib/libc.so.6\n", "")
        with self.assertRaises(resolver.ResolutionError) as caught:
            self.resolve({"/opt/app/bin/app": binary}, {"/opt/app/bin/app": bad})
        self.assertEqual(caught.exception.code, "soname_unresolved")
        self.assertEqual(caught.exception.requirements[0].status, "unresolved")
        ambiguous = LIBC.replace("shlibs:Depends=", "dpkg-shlibdeps: debug: Library libc.so.6 found in /other/libc.so.6\nshlibs:Depends=")
        with self.assertRaises(resolver.ResolutionError) as caught:
            self.resolve({"/opt/app/bin/app": binary}, {"/opt/app/bin/app": ambiguous})
        self.assertEqual(caught.exception.code, "resolver_mapping_ambiguous")

    def test_duplicate_loader_alias_is_not_ambiguous(self):
        binary = self.compile()
        output = LIBC.replace(
            "dpkg-shlibdeps: debug: Using symbols file",
            "dpkg-shlibdeps: debug: Library libc.so.6 found in /usr/lib/libc.so.6\n"
            "dpkg-shlibdeps: debug: Using symbols file",
        )
        self.assertEqual(self.resolve({"/opt/app/bin/app": binary}, {"/opt/app/bin/app": output}).status, "success")

    def test_static_non_elf_malformed_and_architecture(self):
        static = self.compile("/opt/app/bin/static", options=("-static", "-no-pie"))
        self.assertEqual(self.resolve({"/opt/app/bin/static": static}).depends, ())
        text_file = self.root / "opt/app/bin/readme"
        text_file.write_text("plain text")
        self.assertEqual(self.resolve({"/opt/app/bin/readme": inspect_elf(text_file)}).requirements, ())
        damaged = self.root / "opt/app/bin/damaged"
        damaged.write_bytes(b"\x7fELF\x02\x01")
        with self.assertRaises(resolver.ResolutionError) as caught:
            self.resolve({"/opt/app/bin/damaged": inspect_elf(damaged)})
        self.assertEqual(caught.exception.code, "malformed_elf")
        binary = self.compile()
        with mock.patch.object(resolver, "inspect_elf", return_value=dataclasses.replace(binary, debian_architecture="arm64")):
            with self.assertRaises(resolver.ResolutionError) as caught:
                self.resolve({"/opt/app/bin/app": dataclasses.replace(binary, debian_architecture="arm64")})
        self.assertEqual(caught.exception.code, "elf_architecture_mismatch")

    def test_unsupported_and_incomplete_environment(self):
        self.assertEqual(resolver.resolve_elf_dependencies(self.root, {}, distribution="trixie", architecture="amd64", environment=None).status, "unsupported")
        self.assertEqual(resolver.resolve_elf_dependencies(self.root, {}, distribution="bookworm", architecture="arm64", environment=None).status, "unsupported")
        with self.assertRaises(resolver.ResolutionError) as caught:
            self.resolve({}, environment=FakeBookworm(tool=False))
        self.assertEqual(caught.exception.code, "resolver_environment_incomplete")
        with self.assertRaises(resolver.ResolutionError) as caught:
            self.resolve({}, environment=FakeBookworm(release="trixie"))
        self.assertEqual(caught.exception.code, "resolver_environment_mismatch")

    def test_cycle_and_traversal_bounds(self):
        binary = self.compile()
        # Simulate valid bundled metadata to exercise traversal, independent of linker fixture ordering.
        a = dataclasses.replace(binary, interpreter="", soname="liba.so", needed=("libb.so",), runpath=("$ORIGIN",))
        b = dataclasses.replace(binary, interpreter="", soname="libb.so", needed=("liba.so",), runpath=("$ORIGIN",))
        (self.root / "opt/app/lib/liba.so").write_bytes(b"a")
        (self.root / "opt/app/lib/libb.so").write_bytes(b"b")
        def inspection(path, **_kwargs):
            return a if Path(path).name == "liba.so" else b
        with mock.patch.object(resolver, "inspect_elf", side_effect=inspection):
            result = self.resolve({"/opt/app/lib/liba.so": a}, {"/opt/app/lib/liba.so": "", "/opt/app/lib/libb.so": ""})
            self.assertEqual(len(result.requirements), 2)
            with mock.patch.object(resolver, "MAX_DEPTH", 0):
                with self.assertRaises(resolver.ResolutionError) as caught:
                    self.resolve({"/opt/app/lib/liba.so": a}, {"/opt/app/lib/liba.so": "", "/opt/app/lib/libb.so": ""})
                self.assertEqual(caught.exception.code, "resolver_traversal_limit")
            with mock.patch.object(resolver, "MAX_REQUIREMENTS", 1):
                with self.assertRaises(resolver.ResolutionError):
                    self.resolve({"/opt/app/lib/liba.so": a}, {"/opt/app/lib/liba.so": "", "/opt/app/lib/libb.so": ""})
            with mock.patch.object(resolver, "MAX_ELF_FILES", 1):
                with self.assertRaises(resolver.ResolutionError):
                    self.resolve({"/opt/app/lib/liba.so": a}, {"/opt/app/lib/liba.so": "", "/opt/app/lib/libb.so": ""})


if __name__ == "__main__":
    unittest.main()
