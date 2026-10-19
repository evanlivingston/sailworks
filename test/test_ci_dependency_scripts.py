"""Exercise dependency scripts without installing packages or using the network."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("bash"), "Dependency scripts require bash")
class TestDependencyScripts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.log = self.path / "commands"
        self.environment = dict(
            os.environ, PATH=str(self.path), COMMAND_LOG=str(self.log)
        )
        self.bash = shutil.which("bash")
        self.command(
            "gdal-config",
            'printf "%s\\n" "${GDAL_VERSION-3.11.4}"; exit "${GDAL_EXIT-0}"',
        )
        self.command("python", 'exit "${PYTHON_EXIT-0}"')
        self.command("sudo", 'exit "${SUDO_EXIT-0}"')

    def command(self, name, body):
        path = self.path / name
        path.write_text(
            f"#!{shutil.which('bash')}\n"
            f"printf '{name}' >> \"$COMMAND_LOG\"\n"
            'printf " <%s>" "$@" >> "$COMMAND_LOG"\n'
            'printf "\\n" >> "$COMMAND_LOG"\n' + body + "\n"
        )
        path.chmod(0o755)

    def run_script(self, name, **environment):
        return subprocess.run(
            [self.bash, str(ROOT / ".github/workflows/script" / name)],
            cwd=self.path,
            env=dict(self.environment, **environment),
            capture_output=True,
            text=True,
            timeout=10,
        )

    def commands(self):
        return self.log.read_text().splitlines()

    def test_python_requirements_are_arguments_not_shell_redirections(self):
        result = self.run_script("python_dep.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        self.assertEqual(commands[0], "gdal-config <--version>")
        self.assertIn("<numpy>1.0.0> <wheel> <setuptools>=67>", commands[2])
        self.assertIn("<gdal[numpy]==3.11.4.*>", commands[3])
        self.assertEqual(commands[4], "python <-c> <from osgeo import gdal_array>")
        self.assertFalse((self.path / "1.0.0").exists())
        self.assertFalse((self.path / "=67").exists())

    def test_missing_gdal_stops_before_pip(self):
        result = self.run_script("python_dep.sh", GDAL_EXIT="127")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commands(), ["gdal-config <--version>"])

    def test_empty_gdal_version_stops_before_pip(self):
        result = self.run_script("python_dep.sh", GDAL_VERSION="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("empty version", result.stderr)
        self.assertEqual(self.commands(), ["gdal-config <--version>"])

    def test_pip_failure_stops_remaining_installs(self):
        result = self.run_script("python_dep.sh", PYTHON_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.commands()), 2)

    def test_native_install_uses_matching_runtime_and_retries(self):
        result = self.run_script("ubuntu_dep.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        self.assertEqual(len(commands), 4)
        self.assertIn("<Acquire::Retries=3> <update>", commands[1])
        self.assertIn("<libgdal-dev> <gdal-data> <gdal-plugins>", commands[2])
        self.assertNotIn("libgdal34", commands[2])

    def test_native_install_failure_is_not_masked(self):
        self.command(
            "sudo",
            'if [[ "$*" == *"libgdal-dev"* ]]; then exit 42; fi',
        )
        result = self.run_script("ubuntu_dep.sh")
        self.assertEqual(result.returncode, 42)
        self.assertEqual(len(self.commands()), 3)
