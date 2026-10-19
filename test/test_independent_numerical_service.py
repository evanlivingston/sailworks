"""A headless startup must preserve saved source order and reject unsafe input."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import independent_numerical_service as service


class TestIndependentSources(unittest.TestCase):
    def test_changed_fixture_fails_before_any_worker_launch(self):
        import experiment_vertical_renderer as renderer

        with tempfile.TemporaryDirectory() as temporary:
            args = SimpleNamespace(
                directory=Path(temporary) / "attempt", snapshot=Path("not-used")
            )
            with (
                patch.object(
                    renderer.startup,
                    "verify_snapshot",
                    side_effect=ValueError("snapshot changed"),
                ),
                patch.object(service, "PersistentPreparation") as chart,
            ):
                with self.assertRaisesRegex(ValueError, "snapshot changed"):
                    service.run(args)
                chart.assert_not_called()
            diagnostic = json.loads(
                (args.directory / "preflight-error.json").read_text()
            )
            self.assertFalse(diagnostic["workers_launched"])
            self.assertFalse(diagnostic["snapshot_accepted"])

    def test_saved_order_is_not_sorted_or_pruned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "grib-manager.json").write_text(
                json.dumps({"opened": ["z.grib", "a.grib"]})
            )
            self.assertEqual(service.selected_names(root), ("z.grib", "a.grib"))

    def test_empty_escaped_or_duplicated_sources_are_explicit_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for names in (
                [],
                ["../escape.grib"],
                ["/absolute.grib"],
                ["x.grib", "x.grib"],
                [None],
            ):
                (root / "grib-manager.json").write_text(json.dumps({"opened": names}))
                with self.subTest(names=names), self.assertRaises(ValueError):
                    service.selected_names(root)


if __name__ == "__main__":
    unittest.main()
