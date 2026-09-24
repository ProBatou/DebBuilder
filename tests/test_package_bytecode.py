"""Packaged Python entrypoints must leave no unowned application bytecode."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]


class PackagedBytecodeTests(unittest.TestCase):
    def test_bootstrap_and_service_import_suppress_application_bytecode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(
                SOURCE / "debbuilder", root / "debbuilder",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            for name in ("bootstrap_local.py", "server.py"):
                shutil.copy2(SOURCE / name, root / name)
            environment = {
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "0",  # An older preserved environment file.
                "DEBBUILDER_DATA_DIR": str(root / "state"),
            }
            bootstrap = (
                'import runpy, sys, types; '
                'module = types.ModuleType("debbuilder.local_repository_bootstrap"); '
                'module.bootstrap_repository = lambda **kwargs: None; '
                'sys.modules[module.__name__] = module; '
                'entry = runpy.run_path("bootstrap_local.py", run_name="probe"); '
                'assert entry["main"]([]) == 0'
            )
            service = 'import runpy; runpy.run_path("server.py", run_name="probe")'
            for name, script in (("bootstrap", bootstrap), ("service", service)):
                with self.subTest(entrypoint=name):
                    result = subprocess.run(
                        [sys.executable, "-c", script], cwd=root, env=environment,
                        capture_output=True, text=True, timeout=20,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(list(root.rglob("*.pyc")), [])
                    self.assertEqual(list(root.rglob("__pycache__")), [])
