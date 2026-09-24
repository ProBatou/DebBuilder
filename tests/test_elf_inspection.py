"""Local, network-free fixtures for the static ELF metadata boundary."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import elf_inspection


@unittest.skipUnless(shutil.which("gcc") and shutil.which("readelf"), "gcc and binutils are required")
class ElfInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="debbuilder-elf-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "main.c").write_text('#include <stdio.h>\nint main(void) { puts("fixture"); return 0; }\n')

    def compile(self, name, *options, source="main.c"):
        target = self.root / name
        subprocess.run(
            ["gcc", "-O0", *options, "-o", str(target), str(self.root / source)],
            check=True, capture_output=True, timeout=30,
        )
        return target

    def test_non_elf_and_malformed_elf_are_distinct(self):
        text = self.root / "text"
        text.write_text("ordinary payload\n")
        truncated = self.root / "truncated"
        truncated.write_bytes(b"\x7fELF\x02\x01")
        plain = elf_inspection.inspect_elf(text)
        damaged = elf_inspection.inspect_elf(truncated)
        self.assertEqual(plain.kind, "not_elf")
        self.assertEqual(plain.sha256, hashlib.sha256(text.read_bytes()).hexdigest())
        self.assertEqual(elf_inspection.inspect_elf(text, expected_sha256=plain.sha256).kind, "not_elf")
        with self.assertRaises(elf_inspection.ElfInspectionError) as caught:
            elf_inspection.inspect_elf(text, expected_sha256="0" * 64)
        self.assertEqual(caught.exception.code, "elf_identity_mismatch")
        self.assertEqual(damaged.kind, "malformed_elf")
        self.assertTrue(damaged.diagnostics)

    def test_classic_dynamic_executable_and_pie_have_separate_type_and_linkage(self):
        classic = elf_inspection.inspect_elf(self.compile("classic", "-no-pie"))
        pie = elf_inspection.inspect_elf(self.compile("pie", "-fPIE", "-pie"))
        self.assertEqual(classic.kind, "elf")
        self.assertEqual(classic.elf_type, "ET_EXEC")
        self.assertEqual(pie.elf_type, "ET_DYN")
        for result in (classic, pie):
            self.assertEqual(result.linkage, "dynamic")
            self.assertEqual(result.machine, "EM_X86_64")
            self.assertEqual(result.debian_architecture, "amd64")
            self.assertEqual(result.elf_class, "ELF64")
            self.assertEqual(result.endianness, "little")
            self.assertTrue(result.interpreter.startswith("/"))
            self.assertIn("libc.so.6", result.needed)
            self.assertIn("GLIBC_2.34", [row.name for row in result.version_requirements])
        self.assertEqual(
            elf_inspection.planned_resolution_capability(pie, distribution="bookworm", architecture="amd64"),
            "planned_bookworm_amd64",
        )
        self.assertEqual(
            elf_inspection.planned_resolution_capability(pie, distribution="trixie", architecture="amd64"),
            "unsupported",
        )

    def test_shared_library_can_be_dynamic_without_interpreter(self):
        (self.root / "library.c").write_text('#include <stdio.h>\nint library(void) { return puts("fixture"); }\n')
        library = elf_inspection.inspect_elf(self.compile("libfixture.so.1", "-shared", "-fPIC", "-Wl,-soname,libfixture.so.1", source="library.c"))
        self.assertEqual(library.elf_type, "ET_DYN")
        self.assertEqual(library.linkage, "dynamic")
        self.assertEqual(library.interpreter, "")
        self.assertEqual(library.soname, "libfixture.so.1")
        self.assertIn("libc.so.6", library.needed)
        (self.root / "standalone.c").write_text("int library(void) { return 42; }\n")
        standalone = elf_inspection.inspect_elf(self.compile("libstandalone.so", "-shared", "-fPIC", "-nostdlib", source="standalone.c"))
        self.assertEqual(standalone.elf_type, "ET_DYN")
        self.assertEqual(standalone.linkage, "dynamic")
        self.assertEqual(standalone.needed, ())

    def test_static_executable_has_no_dynamic_requirements(self):
        static = elf_inspection.inspect_elf(self.compile("static", "-static", "-no-pie"))
        self.assertEqual(static.kind, "elf")
        self.assertEqual(static.elf_type, "ET_EXEC")
        self.assertEqual(static.linkage, "static")
        self.assertEqual(static.interpreter, "")
        self.assertEqual(static.needed, ())
        object_file = elf_inspection.inspect_elf(self.compile("main.o", "-c"))
        self.assertEqual(object_file.elf_type, "ET_REL")
        self.assertEqual(object_file.linkage, "not_applicable")

    def test_runpath_preserves_origin_without_resolving_it(self):
        result = elf_inspection.inspect_elf(self.compile("runpath", "-Wl,-rpath,$ORIGIN/../lib:$ORIGIN/plugins"))
        self.assertEqual(result.runpath, ("$ORIGIN/../lib", "$ORIGIN/plugins"))
        self.assertEqual(result.rpath, ())
        old_tags = elf_inspection.inspect_elf(self.compile("rpath", "-Wl,--disable-new-dtags", "-Wl,-rpath,$ORIGIN/lib"))
        self.assertEqual(old_tags.rpath, ("$ORIGIN/lib",))
        self.assertEqual(old_tags.runpath, ())

    def test_bounds_and_missing_tool_fail_explicitly(self):
        binary = self.compile("bounded")
        with mock.patch.object(elf_inspection, "MAX_FILE_BYTES", 128):
            with self.assertRaises(elf_inspection.ElfInspectionError) as caught:
                elf_inspection.inspect_elf(binary)
            self.assertEqual(caught.exception.code, "elf_file_limit")
        with mock.patch.object(elf_inspection, "MAX_OUTPUT_BYTES", 64):
            with self.assertRaises(elf_inspection.ElfInspectionError) as caught:
                elf_inspection.inspect_elf(binary)
            self.assertEqual(caught.exception.code, "readelf_output_limit")
        with mock.patch.object(elf_inspection.shutil, "which", return_value=None):
            with self.assertRaises(elf_inspection.ElfInspectionError) as caught:
                elf_inspection.inspect_elf(binary)
            self.assertEqual(caught.exception.code, "readelf_unavailable")

    def test_success_exit_without_elf_structure_is_malformed(self):
        binary = self.compile("structure")
        with mock.patch.object(elf_inspection, "_readelf", return_value=("unrelated output", "", 0)):
            result = elf_inspection.inspect_elf(binary)
        self.assertEqual(result.kind, "malformed_elf")

    def test_symlink_is_rejected_and_architecture_mapping_is_conservative(self):
        target = self.compile("target")
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaises(elf_inspection.ElfInspectionError) as caught:
            elf_inspection.inspect_elf(link)
        self.assertEqual(caught.exception.code, "unsafe_elf_file")
        header = bytearray(target.read_bytes()[:64])
        header[4] = 1  # ARM hard-float uses the ELF32 e_flags location.
        header[18:20] = (40).to_bytes(2, "little")  # EM_ARM
        header[36:40] = (0x05000400).to_bytes(4, "little")  # EABI 5 + hard float
        self.assertEqual(elf_inspection._header(bytes(header))[3], "armhf")
        header[36:40] = (0x05000000).to_bytes(4, "little")
        self.assertEqual(elf_inspection._header(bytes(header))[3], "")
        header[4] = 2
        header[18:20] = (183).to_bytes(2, "little")  # EM_AARCH64
        self.assertEqual(elf_inspection._header(bytes(header))[3], "arm64")
        header[4] = 1
        self.assertEqual(elf_inspection._header(bytes(header))[3], "")


if __name__ == "__main__":
    unittest.main()
