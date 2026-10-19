"""The diagnostic launcher must isolate application data before importing Qt."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "pan_diagnostic",
    Path(__file__).resolve().parents[1] / "tools/profile_pan_stalls.py",
)
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)


class TestPanDiagnosticLauncher(unittest.TestCase):
    def test_worker_gets_private_home_xdg_and_frozen_snapshot_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "trace.json"
            original_home = os.environ.get("HOME")
            manifest = dict(
                forecast_time="2026-10-17T12:00:00", content_sha256="fixture"
            )

            def launch(command, *, env, cwd, check, timeout):
                config_path = Path(command[command.index("--worker") + 1])
                config = json.loads(config_path.read_text())
                self.assertEqual(config["forecast_time"], manifest["forecast_time"])
                self.assertEqual(config["snapshot_sha256"], "fixture")
                self.assertEqual(config["scenario"], "pan")
                self.assertNotEqual(env["HOME"], original_home)
                for key in (
                    "HOME",
                    "XDG_DATA_HOME",
                    "XDG_CONFIG_HOME",
                    "XDG_CACHE_HOME",
                ):
                    self.assertTrue(Path(env[key]).is_relative_to(config_path.parent))
                self.assertEqual(env["QT_QPA_PLATFORM"], "offscreen")
                self.assertEqual(env["PYTHONPATH"], str(diagnostic.ROOT))
                self.assertEqual(cwd, diagnostic.ROOT)
                self.assertTrue(check)
                self.assertEqual(timeout, 600)
                self.assertEqual(
                    command[command.index("--output") + 1], str(output.resolve())
                )

            with (
                patch.object(
                    sys,
                    "argv",
                    ["profile", "--snapshot", str(root), "--output", str(output)],
                ),
                patch.object(
                    diagnostic.protocol, "verify_snapshot", return_value=manifest
                ),
                patch.object(diagnostic.protocol, "prepare_sandbox") as prepare,
                patch.object(diagnostic.protocol.protocol, "revision", return_value={}),
                patch.object(diagnostic.subprocess, "run", side_effect=launch) as run,
            ):
                diagnostic.main()
            prepare.assert_called_once()
            run.assert_called_once()
            self.assertEqual(os.environ.get("HOME"), original_home)

    def test_invalid_snapshot_never_launches_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "profile",
                        "--snapshot",
                        temporary,
                        "--output",
                        str(Path(temporary) / "trace.json"),
                    ],
                ),
                patch.object(
                    diagnostic.protocol,
                    "verify_snapshot",
                    side_effect=ValueError("changed snapshot"),
                ),
                patch.object(diagnostic.subprocess, "run") as run,
            ):
                with self.assertRaisesRegex(ValueError, "changed snapshot"):
                    diagnostic.main()
            run.assert_not_called()
