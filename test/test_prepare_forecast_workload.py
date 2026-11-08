"""The covered-forecast workload builder's snapshot bookkeeping."""

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "prepare_forecast_workload.py"
spec = importlib.util.spec_from_file_location("prepare_forecast_workload", SCRIPT)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def _snapshot(root):
    data = root / "data"
    (data / "grib").mkdir(parents=True)
    (data / "grib" / "old.grib2").write_bytes(b"old")
    (data / "grib-manager.json").write_text(json.dumps({"opened": ["old.grib2"]}))
    (root / "manifest.json").write_text(
        json.dumps({"snapshot_version": 1, "files": [], "content_sha256": ""})
    )


def test_parse_steps_is_sorted_and_unique():
    assert prepare.parse_steps("12, 0,3,12") == [0, 3, 12]
    try:
        prepare.parse_steps("")
    except ValueError as exc:
        assert "non-negative" in str(exc)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("empty steps must be rejected")


def test_install_and_update_preserve_the_opened_order(tmp_path):
    _snapshot(tmp_path)
    grib = tmp_path / "new.grib2"
    grib.write_bytes(b"new")
    target, opened = prepare.install_grib(tmp_path, grib)
    assert target == tmp_path / "data" / "grib" / "new.grib2"
    assert opened == ["old.grib2", "new.grib2"]
    manifest = prepare.update_manifest(tmp_path, "2026-11-03T07:00:00")
    assert manifest["forecast_time"] == "2026-11-03T07:00:00"
    paths = {entry["path"] for entry in manifest["files"]}
    assert paths == {"grib/old.grib2", "grib/new.grib2", "grib-manager.json"}
    assert len(manifest["content_sha256"]) == 64
    # Re-running the inventory is stable.
    assert prepare.snapshot_entries(tmp_path) == manifest["files"]
