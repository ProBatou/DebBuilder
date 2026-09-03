import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder.build_executor import execute_build
from debbuilder.dependency_checker import DependencyError, check_dependencies


class DependencyCheckerTests(unittest.TestCase):
    def test_reports_detected_manual_available_and_missing_separately(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            installed = command.endswith(" python3") or command.endswith(" cargo")
            return {"status":"success" if installed else "failed", "stdout":"ii \n" if installed else "", "exit_code":0 if installed else 1,"command":command,"arguments":command.split(),"working_directory":kwargs["working_directory"],"duration":0.01}
        with tempfile.TemporaryDirectory() as workspace:
            with self.assertRaises(DependencyError) as raised:
                check_dependencies(["python3", "python3-pip"], ["cargo", "python3"], workspace=workspace, runner=runner)
        state = raised.exception.details
        self.assertEqual(state["detected"], ["python3", "python3-pip"])
        self.assertEqual(state["manually_added"], ["cargo", "python3"])
        self.assertEqual(state["required"], ["python3", "python3-pip", "cargo"])
        self.assertEqual(state["available"], ["python3", "cargo"])
        self.assertEqual(state["missing"], ["python3-pip"])
        self.assertFalse(state["installation_attempted"])
        self.assertEqual(len(calls), 3)

    def test_all_available_returns_exploitable_state(self):
        runner = lambda command, **kwargs: {"status":"success", "stdout":"ii ", "exit_code":0,"command":command,"arguments":command.split(),"working_directory":kwargs["working_directory"],"duration":0.01}
        with tempfile.TemporaryDirectory() as workspace:
            state = check_dependencies(["nodejs"], ["npm"], workspace=workspace, runner=runner)
        self.assertEqual(state["available"], ["nodejs", "npm"])
        self.assertEqual(state["missing"], [])

    def test_invalid_package_name_is_rejected_without_running_a_command(self):
        def runner(*_args, **_kwargs):
            raise AssertionError("runner must not be called")
        with tempfile.TemporaryDirectory() as workspace:
            with self.assertRaises(DependencyError) as raised:
                check_dependencies(["python3;id"], [], workspace=workspace, runner=runner)
        self.assertEqual(raised.exception.code, "invalid_dependency")

    def test_path_tool_is_available_without_a_corresponding_debian_package(self):
        with tempfile.TemporaryDirectory() as workspace:
            bin_directory = Path(workspace) / "admin-tools"
            bin_directory.mkdir()
            cargo = bin_directory / "cargo"
            cargo.write_text("#!/bin/sh\necho 'cargo 1.98.1 (test)'\n")
            cargo.chmod(0o755)
            state = check_dependencies(
                [], [], tools=["cargo"], tool_version_requirements={"cargo": ">=1.80"},
                workspace=workspace, environment={"PATH": str(bin_directory)},
            )
        self.assertEqual(state["available_tools"], ["cargo"])
        self.assertEqual(state["missing"], [])
        self.assertEqual(state["tool_checks"][0]["path"], str(cargo))
        self.assertEqual(state["tool_checks"][0]["version"], "1.98.1")
        self.assertEqual(state["tool_checks"][0]["version_output"], "cargo 1.98.1 (test)")
        self.assertEqual(state["tool_checks"][0]["status"], "available")

    def test_build_tools_use_path_and_version_while_system_dependencies_use_dpkg(self):
        calls = []
        with tempfile.TemporaryDirectory() as workspace:
            bin_directory = Path(workspace) / "tools"
            bin_directory.mkdir()
            tool = bin_directory / "demo-tool"
            tool.write_text("#!/bin/sh\necho 'demo-tool 3.2.1'\n")
            tool.chmod(0o755)

            def runner(command, **kwargs):
                calls.append((command, kwargs))
                if command.startswith(str(tool)):
                    return {"status":"success","stdout":"demo-tool 3.2.1\n","stderr":"","exit_code":0,"command":command,"arguments":command.split(),"working_directory":kwargs["working_directory"],"duration":0.01}
                if command.startswith("dpkg-query"):
                    return {"status":"success","stdout":"ii \n","stderr":"","exit_code":0,"command":command,"arguments":command.split(),"working_directory":kwargs["working_directory"],"duration":0.01}
                raise AssertionError(command)

            state = check_dependencies(
                ["libssl-dev"], [], tools=["demo-tool"], workspace=workspace,
                environment={"PATH": str(bin_directory)}, runner=runner,
            )

        self.assertEqual(state["available_tools"], ["demo-tool"])
        self.assertEqual(state["available"], ["libssl-dev"])
        self.assertEqual(calls[0][0], f"{tool} --version")
        self.assertEqual(calls[0][1]["environment"]["PATH"], str(bin_directory))
        self.assertEqual(calls[1][0], "dpkg-query --show --showformat=${db:Status-Abbrev} libssl-dev")
        self.assertEqual(calls[1][1]["environment"]["PATH"], str(bin_directory))
        self.assertEqual(calls[1][1]["environment"]["LC_ALL"], "C")

    def test_home_dependent_path_tool_works_for_checker_and_build_executor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            source = workspace / "source"
            source.mkdir(parents=True)
            home = root / "service-home"
            home.mkdir()
            (home / "tool-home-marker").write_text("configured")
            bin_directory = root / "tools"
            bin_directory.mkdir()
            tool = bin_directory / "home-tool"
            tool.write_text(
                "#!/bin/sh\n"
                "if [ ! -f \"$HOME/tool-home-marker\" ]; then echo 'missing home config' >&2; exit 9; fi\n"
                "if [ \"$1\" = \"--version\" ]; then echo 'home-tool 2.4.0'; exit 0; fi\n"
                "printf '%s\\n' \"$HOME\" > home-used\n"
                "mkdir -p dist\n"
                "touch dist/result\n"
            )
            tool.chmod(0o755)
            build_environment = {"PATH": f"{bin_directory}:/usr/bin:/bin"}

            with mock.patch.dict("os.environ", {"HOME": str(home), "PATH": "/usr/bin"}, clear=True):
                state = check_dependencies(
                    [], [], tools=["home-tool"], tool_version_requirements={"home-tool": ">=2.0"},
                    workspace=source, environment=build_environment,
                )
                result = execute_build(
                    {"build": {"commands": ["home-tool build"], "working_directory": ".", "environment": build_environment, "output": {"mode":"path","path":"dist"}}},
                    {},
                    source,
                    dry_run=False,
                )
                used_home = (source / "home-used").read_text().strip()

        self.assertEqual(state["available_tools"], ["home-tool"])
        self.assertEqual(result["commands"][0]["status"], "success")
        self.assertEqual(used_home, str(home))

    def test_tool_version_mismatch_is_distinct_from_missing_system_packages(self):
        with tempfile.TemporaryDirectory() as workspace:
            tool = Path(workspace) / "rustc"
            tool.write_text("#!/bin/sh\necho 'rustc 1.70.0'\n")
            tool.chmod(0o755)
            with self.assertRaises(DependencyError) as raised:
                check_dependencies([], [], tools=["rustc"], tool_version_requirements={"rustc": ">=1.80"}, workspace=workspace, environment={"PATH": workspace})
        self.assertEqual(raised.exception.code, "missing_build_tools")
        self.assertEqual(raised.exception.details["tool_checks"][0]["status"], "version_mismatch")


if __name__ == "__main__":
    unittest.main()
