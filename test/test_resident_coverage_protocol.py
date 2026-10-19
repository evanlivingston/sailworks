"""Matched measurement rejects missing fidelity, different inputs or ownership."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import compare_resident_runs as compare
import experiment_vertical_renderer as renderer
import architecture_run_protocol as protocol


class TestResidentPairGate(unittest.TestCase):
    def reports(self):
        base = dict(
            protocol="a0h",
            snapshot_sha256="same",
            initial_view={"x": 0},
            clocks=["exact"],
            host={"backend": "Metal", "dpr": 2},
            producer_status=dict(exit_code=0, timed_out=False, exception=None),
            measured_window_has_oracle_or_readback=False,
            explicit_readback_guard={"forbidden": 0},
            post_window_validation={
                "records": [
                    dict(
                        generation=g,
                        unchanged_assets=True,
                        scene_pixels_exact=True,
                        fidelity={"exact_reference_selections": 25},
                    )
                    for g in (1, 2, 3, 5)
                ]
            },
            diagnostics=[
                dict(
                    inputs=[{"state": {"x": i}} for i in range(32)],
                    after_rendering_seconds=[0.016],
                )
                for _ in range(2)
            ],
            freshness=dict(
                after_rendering_seconds=[0.016],
                published=[{"generation": 5}],
                failures=[],
                transitions=[dict(generation=g, view={"x": g}) for g in (3, 4, 5)],
            ),
        )
        reports = [deepcopy(base), deepcopy(base)]
        for report, policy in zip(reports, ("reopen", "resident")):
            report["data_policy"] = policy
            events = (
                [
                    dict(event=e, thread=9)
                    for _ in range(8)
                    for e in ("open", "task", "close")
                ]
                if policy == "reopen"
                else [dict(event="open", thread=9)]
                + [dict(event="task", thread=9)] * 8
                + [dict(event="close", thread=9)]
            )
            report["data_worker"] = dict(owner=9, producer_gui_thread=1, events=events)
        return reports

    def test_accepts_complete_identical_measurement_and_post_validation(self):
        compare.check_reports(*self.reports())

    def test_rejects_contamination_fidelity_owner_and_input_faults(self):
        for fault in (
            "readback",
            "guard",
            "fidelity",
            "input",
            "owner",
            "close",
            "freshness",
            "exit",
            "clock",
            "render",
        ):
            reports = self.reports()
            candidate = reports[1]
            if fault == "readback":
                candidate["measured_window_has_oracle_or_readback"] = True
            elif fault == "guard":
                candidate["explicit_readback_guard"]["forbidden"] = 1
            elif fault == "fidelity":
                candidate["post_window_validation"]["records"][1][
                    "scene_pixels_exact"
                ] = False
            elif fault == "input":
                candidate["diagnostics"][0]["inputs"][0]["state"]["x"] = -1
            elif fault == "owner":
                candidate["data_worker"]["owner"] = 1
            elif fault == "close":
                candidate["data_worker"]["events"].pop()
            elif fault == "freshness":
                candidate["freshness"]["published"][0]["generation"] = 3
            elif fault == "exit":
                candidate["producer_status"]["exit_code"] = 1
            elif fault == "render":
                candidate["diagnostics"][0]["after_rendering_seconds"] = []
            else:
                candidate["clocks"] = ["changed"]
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                compare.check_reports(*reports)


class TestPreparedTransfer(unittest.TestCase):
    def test_deferred_assets_reject_dtype_pixels_and_provenance_changes(self):
        metadata = dict(
            snapshot_sha256="same",
            view={},
            clocks=[1],
            targets=[],
            dpr=2,
            opacity=1,
            sea_color=[0, 0, 0, 1],
            image_metadata=[],
            field_meta=[dict(file="field.npz", generation="source-order-hash")],
        )
        fields = {
            name: np.array([[1.0, np.nan]], dtype=np.float32)
            for name in ("first", "second", "field")
        }
        fields["points"] = np.array([[1.0, 2.0]], dtype=np.float64)
        assets = (
            metadata,
            {
                "field.npz": fields,
                "background": np.zeros(4, dtype=np.uint8),
                "navigation": np.zeros(4, dtype=np.uint8),
            },
            Path("unused"),
        )
        renderer.assert_same_prepared_assets(assets, deepcopy(assets))
        for fault in ("dtype", "pixels", "provenance"):
            changed = deepcopy(assets)
            if fault == "dtype":
                changed[1]["field.npz"]["points"] = fields["points"].astype(np.float32)
            elif fault == "pixels":
                changed[1]["background"][0] = 1
            else:
                changed[0]["field_meta"][0]["generation"] = "changed-source-order"
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                renderer.assert_same_prepared_assets(assets, changed)

    def test_gui_bundle_keeps_immutable_image_bytes(self):
        from PySide6.QtGui import QImage

        raw = np.arange(16, dtype=np.uint8)
        raw.setflags(write=False)
        info = dict(
            width=2, height=2, stride=8, format=QImage.Format.Format_RGBA8888.value
        )
        arrays = dict(background=raw, navigation=raw)
        bundle = ({"image_metadata": [info, info]}, arrays, Path("unused"))
        with patch.object(renderer, "prepared_fields", return_value=("prepared",)):
            renderer.prepare_gui_bundle(bundle)
        self.assertEqual(arrays["_gui_prepared"], ("prepared",))
        self.assertEqual(bytes(arrays["_gui_images"][0].constBits()), raw.tobytes())

    def test_pending_bundle_does_not_claim_oracle_is_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            np.save(root / "background.npy", np.zeros(4, dtype=np.uint8))
            np.save(root / "navigation.npy", np.zeros(4, dtype=np.uint8))
            np.savez(root / "field.npz", points=np.zeros((1, 2), dtype=np.float64))
            meta = dict(oracle_pending=True, field_meta=[{"file": "field.npz"}])
            meta["files"] = {
                name: protocol.file_hash(root / name)
                for name in ("background.npy", "navigation.npy", "field.npz")
            }
            (root / "resources.json").write_text(json.dumps(meta))
            loaded, _, _ = renderer.read_bundle(root)
            self.assertTrue(loaded["oracle_pending"])
            meta["oracle_pending"] = False
            (root / "resources.json").write_text(json.dumps(meta))
            with self.assertRaisesRegex(ValueError, "checksums"):
                renderer.read_bundle(root)


if __name__ == "__main__":
    unittest.main()
