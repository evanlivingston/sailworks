"""Frozen synthetic routing inputs: identity, formulas and sampler semantics."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
from osgeo import gdal, osr
import pytest

from sailworks.core.grib import Grib
from sailworks.core.gribsampling import WeatherSampler
from sailworks.core.marine.routing import KNOT
from sailworks.core.polarfiles import read_polar

sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_routing_fixtures as fixtures  # noqa: E402

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent / "fixtures" / "native_routing"


def _load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def _write_forecast(
    path,
    forecast_data,
    contract_data,
    *,
    center=None,
    subcenter=None,
    pdt=None,
    process=None,
    levels=None,
    ensemble_member=None,
):
    source_metadata = contract_data["source_metadata"]
    center = source_metadata["center"] if center is None else center
    subcenter = source_metadata["subcenter"] if subcenter is None else subcenter
    pdt = source_metadata["pdt"] if pdt is None else pdt
    process = source_metadata["process"] if process is None else process
    records = [
        (band, frame) for band in forecast_data["bands"] for frame in band["frames"]
    ]
    dataset = gdal.GetDriverByName("GTiff").Create(
        str(path),
        contract_data["grid"]["nx"],
        contract_data["grid"]["ny"],
        len(records),
        gdal.GDT_Float64,
    )
    dataset.SetGeoTransform(contract_data["orientation"]["geotransform"])
    projection = osr.SpatialReference()
    projection.ImportFromEPSG(4326)
    dataset.SetProjection(projection.ExportToWkt())
    for band_id, (band, frame) in enumerate(records, 1):
        element, hour = band["element"], frame["hour"]
        level = (levels or {}).get(element, band["level"])
        template = f"2 2 2 0 {process} 0 0 1 {hour} 103 0 10 255 0 0"
        if ensemble_member is not None:
            template += f" 1 {ensemble_member} 10"
        metadata = {
            "GRIB_ELEMENT": element,
            "GRIB_SHORT_NAME": level,
            "GRIB_UNIT": band["unit"],
            "GRIB_FORECAST_SECONDS": str(hour * 3600),
            "GRIB_REF_TIME": str(START.timestamp()),
            "GRIB_VALID_TIME": str((START + timedelta(hours=hour)).timestamp()),
            "GRIB_IDS": f"CENTER={center} SUBCENTER={subcenter}",
            "GRIB_PDS_PDTN": str(1 if ensemble_member is not None else pdt),
            "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": template,
        }
        raster = dataset.GetRasterBand(band_id)
        raster.SetMetadata(metadata)
        raster.SetNoDataValue(-9999)
        raster.WriteArray(np.asarray(frame["values"], dtype=float))
    dataset = None
    return path


def _source(path):
    return Grib(str(path), Path(path).stem, "7", [], START, 6, "synthetic")


@contextmanager
def _forecast_inputs(tmp_path, **overrides):
    contract_data, forecast_data = _load("contract.json"), _load("forecast.json")
    path = _write_forecast(
        tmp_path / "forecast.tif", forecast_data, contract_data, **overrides
    )
    source = _source(path)
    try:
        yield source
    finally:
        source.close()


def _wind(sampler, instant, lat, lon):
    value = sampler.wind_at(instant, lat, lon)
    assert value is not None
    return value


def test_manifest_hashes_sizes_and_generator_identity():
    manifest = _load("manifest.json")
    assert manifest["fixture_id"] == fixtures.FIXTURE_ID
    assert manifest["revision"] == fixtures.REVISION
    assert manifest["identity"]["baseline_commit"] == (
        "b6258343592e104d754297d504c381ebae78947d"
    )
    paths = [entry["path"] for entry in manifest["artifacts"]]
    assert "manifest.json" not in paths
    assert len(paths) == len(set(paths)) == 5
    for entry in manifest["artifacts"]:
        data = (ROOT / entry["path"]).read_bytes()
        assert entry["bytes"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()

    source = Path(fixtures.__file__).read_bytes()
    generator = manifest["identity"]["generator"]
    assert generator["path"] == fixtures.GENERATOR_PATH
    assert generator["bytes"] == len(source)
    assert generator["sha256"] == hashlib.sha256(source).hexdigest()


def test_manifest_exposes_source_and_band_metadata():
    contract, manifest = _load("contract.json"), _load("manifest.json")
    forecast, current = _load("forecast.json"), _load("current.json")
    assert manifest["source_metadata"] == contract["source_metadata"]
    assert manifest["temporal_interpolation"] == contract["temporal_interpolation"]
    shape = [contract["grid"]["ny"], contract["grid"]["nx"]]
    for kind, key, data in (
        ("forecast", "element", forecast),
        ("current", "component", current),
    ):
        entries = manifest["bands"][kind]
        assert [entry[key] for entry in entries] == [
            band[key] for band in data["bands"]
        ]
        for entry, band in zip(entries, data["bands"]):
            assert entry["unit"] == band["unit"]
            assert entry["hours"] == contract["valid_hours"]
            assert entry["shapes"] == [shape] * len(contract["valid_hours"])
            if key == "element":
                assert entry["level"] == band["level"]
            else:
                assert "level" not in entry
    levels = {
        entry["element"]: entry["level"] for entry in manifest["bands"]["forecast"]
    }
    assert levels["UGRD"] == contract["source_metadata"]["wind_level"]
    assert levels["PRMSL"] == contract["source_metadata"]["pressure_level"]


def test_regeneration_is_byte_identical(tmp_path):
    manifest = _load("manifest.json")
    source = Path(fixtures.__file__).read_bytes()
    fixtures.write_fixtures(
        tmp_path,
        manifest["identity"]["baseline_commit"],
        source,
        generator_path=fixtures.GENERATOR_PATH,
    )
    for entry in manifest["artifacts"]:
        assert (tmp_path / entry["path"]).read_bytes() == (
            ROOT / entry["path"]
        ).read_bytes()
    assert (tmp_path / "manifest.json").read_bytes() == (
        ROOT / "manifest.json"
    ).read_bytes()


def test_generator_preserves_frozen_baseline_and_requires_one_for_new_dirs(tmp_path):
    manifest = _load("manifest.json")
    baseline = "b6258343592e104d754297d504c381ebae78947d"
    assert manifest["identity"]["baseline_commit"] == baseline

    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "manifest.json").write_bytes((ROOT / "manifest.json").read_bytes())
    fixtures.main(["--root", str(existing)])
    assert (existing / "manifest.json").read_bytes() == (
        ROOT / "manifest.json"
    ).read_bytes()

    new_root = tmp_path / "new"
    with pytest.raises(SystemExit):
        fixtures.main(["--root", str(new_root)])
    assert not (new_root / "manifest.json").exists()


def test_grid_orientation_pixel_centers_and_temporal_blend(tmp_path):
    contract = _load("contract.json")
    expected = contract["expected_observables"]
    with _forecast_inputs(tmp_path) as source:
        assert list(source.sampling.transform) == pytest.approx(
            contract["orientation"]["geotransform"]
        )
        sampler = WeatherSampler([source])
        sw = _wind(sampler, START, contract["grid"]["lat0"], contract["grid"]["lon0"])
        assert sw[1] == pytest.approx(expected["wind_sw_h0"]["speed_ms"])
        assert sw[0] == pytest.approx(expected["wind_sw_h0"]["from_deg"])
        ne_lat = contract["grid"]["lat0"] + 4 * contract["grid"]["dy"]
        ne_lon = contract["grid"]["lon0"] + 4 * contract["grid"]["dx"]
        ne = _wind(sampler, START, ne_lat, ne_lon)
        assert ne[1] == pytest.approx(expected["wind_ne_h0"]["speed_ms"])
        assert ne[0] == pytest.approx(expected["wind_ne_h0"]["from_deg"])

        # Half a cell east of the southwest center: bilinear mean of columns 0/1.
        midpoint = _wind(sampler, START, contract["grid"]["lat0"], -123.75)
        assert midpoint[1] == pytest.approx(math.hypot(6.25, 2.125))
        blended = _wind(
            sampler,
            START + timedelta(hours=1),
            contract["grid"]["lat0"],
            contract["grid"]["lon0"],
        )
        assert blended[1] == pytest.approx(expected["wind_sw_h1_blended"]["speed_ms"])


def test_wind_is_mps_and_direction_is_from(tmp_path):
    expected = _load("contract.json")["expected_observables"]["wind_sw_h0"]
    with _forecast_inputs(tmp_path) as source:
        direction, speed = _wind(WeatherSampler([source]), START, 48.0, -124.0)
    assert speed == pytest.approx(6.324555320336759, abs=1e-12)
    assert direction == pytest.approx(251.565051177078, abs=1e-12)
    assert speed == pytest.approx(expected["speed_ms"])
    assert speed / KNOT == pytest.approx(expected["speed_knots"])
    assert speed != pytest.approx(expected["speed_knots"])
    # Flow is toward the northeast, so the meteorological direction is FROM southwest.
    assert direction == pytest.approx(
        (math.degrees(math.atan2(-6.0, -2.0)) + 360) % 360
    )


def test_pressure_keeps_established_pa_to_hpa_sampler_boundary(tmp_path):
    contract = _load("contract.json")
    expected = contract["expected_observables"]
    with _forecast_inputs(tmp_path) as source:
        pressure = next(
            record for record in source.sampling.records if record.element == "PRMSL"
        )
        assert pressure.scale == pytest.approx(0.01)
        sampler = WeatherSampler([source])
        sw = sampler.sample("pressure", START, np.array([48.0]), np.array([-124.0]))
        ne_lat = contract["grid"]["lat0"] + (contract["grid"]["ny"] - 1) * (
            contract["grid"]["dy"]
        )
        ne_lon = contract["grid"]["lon0"] + (contract["grid"]["nx"] - 1) * (
            contract["grid"]["dx"]
        )
        ne = sampler.sample("pressure", START, np.array([ne_lat]), np.array([ne_lon]))
    assert sw[0, 0] == pytest.approx(expected["pressure_sw_h0_hpa"])
    assert ne[0, 0] == pytest.approx(expected["pressure_ne_h0_hpa"])


def test_forecast_and_current_metadata_match_contract():
    contract = _load("contract.json")
    forecast, current = _load("forecast.json"), _load("current.json")
    metadata = contract["source_metadata"]
    hours, nx, ny = (
        contract["valid_hours"],
        contract["grid"]["nx"],
        contract["grid"]["ny"],
    )

    assert contract["temporal_interpolation"].startswith("linear")
    assert forecast["valid_hours"] == current["valid_hours"] == hours
    assert {
        band["element"]: (band["level"], band["unit"]) for band in forecast["bands"]
    } == {
        "UGRD": (metadata["wind_level"], metadata["wind_unit"]),
        "VGRD": (metadata["wind_level"], metadata["wind_unit"]),
        "PRMSL": (metadata["pressure_level"], metadata["pressure_unit"]),
    }
    assert {band["component"]: band["unit"] for band in current["bands"]} == {
        "east": metadata["current_unit"],
        "north": metadata["current_unit"],
    }
    for band in forecast["bands"] + current["bands"]:
        assert [frame["hour"] for frame in band["frames"]] == hours
        for frame in band["frames"]:
            assert np.asarray(frame["values"]).shape == (ny, nx)


def test_polar_contract_axes_units_formula_and_frozen_values():
    contract = _load("contract.json")
    polar = contract["polar"]
    twa_axis, tws_axis = polar["axes"]["twa_deg"], polar["axes"]["tws_kn"]

    assert polar["synthetic"] is True
    assert polar["source"] == "synthetic"
    assert polar["units"] == {
        "twa_deg": "degrees true wind angle",
        "tws_kn": "knots true wind speed",
        "boatspeed_kn": "knots",
    }
    assert polar["formula"] == "boatspeed_kn = round(0.02*twa_deg + 0.1*tws_kn, 2)"

    lines = (ROOT / "polar.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0].split("\t") == polar["columns"]
    assert len(lines) == len(twa_axis) + 1
    for line, twa in zip(lines[1:], twa_axis):
        cells = line.split("\t")
        assert float(cells[0]) == twa
        assert len(cells) == len(tws_axis) + 1
        for cell, tws in zip(cells[1:], tws_axis):
            assert float(cell) == pytest.approx(round(0.02 * twa + 0.1 * tws, 2))

    polar_file = read_polar(ROOT / "polar.csv")
    for twa in twa_axis:
        for tws in tws_axis:
            assert polar_file.get_speed(tws, math.radians(twa)) == pytest.approx(
                round(0.02 * twa + 0.1 * tws, 2)
            )


def test_current_polar_and_obstacle_expected_values():
    contract = _load("contract.json")
    expected = contract["expected_observables"]
    current = _load("current.json")
    east, north = current["bands"]
    sw = east["frames"][0]["values"][-1][0]
    ne_h0 = east["frames"][0]["values"][0][-1]
    assert sw == expected["current_sw_h0"]["east_ms"]
    assert ne_h0 == expected["current_ne_h0"]["east_ms"]
    assert north["frames"][0]["values"][-1][0] == expected["current_sw_h0"]["north_ms"]
    assert north["frames"][0]["values"][0][-1] == expected["current_ne_h0"]["north_ms"]
    for frame_index, key in ((1, "current_ne_h3"), (2, "current_ne_h6")):
        assert east["frames"][frame_index]["values"][0][-1] == expected[key]["east_ms"]
        assert (
            north["frames"][frame_index]["values"][0][-1] == expected[key]["north_ms"]
        )

    polar = read_polar(ROOT / "polar.csv")
    for expected_key, tws, twa in (
        ("polar_60_8_kn", 8, 60),
        ("polar_90_12_kn", 12, 90),
        ("polar_180_16_kn", 16, 180),
    ):
        assert polar.get_speed(tws, math.radians(twa)) == pytest.approx(
            expected[expected_key]
        )

    obstacle = json.loads((ROOT / "obstacle.geojson").read_text(encoding="utf-8"))
    ring = obstacle["geometry"]["coordinates"][0]
    xs, ys = [point[0] for point in ring], [point[1] for point in ring]
    assert ring[0] == ring[-1]
    assert [min(xs), min(ys), max(xs), max(ys)] == pytest.approx(
        expected["obstacle_bbox"]
    )
    assert [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2] == pytest.approx(
        expected["obstacle_center"]
    )
    assert obstacle["properties"]["synthetic"] is True


def test_second_center_is_distinct_identity_not_rejected(tmp_path):
    contract_data, forecast_data = _load("contract.json"), _load("forecast.json")
    _write_forecast(tmp_path / "c7.tif", forecast_data, contract_data, center=7)
    _write_forecast(tmp_path / "c8.tif", forecast_data, contract_data, center=8)
    first, second = _source(tmp_path / "c7.tif"), _source(tmp_path / "c8.tif")
    try:
        assert first.sampling.records and second.sampling.records
        first_identity = first.sampling.records[0].identity
        second_identity = second.sampling.records[0].identity
        assert first_identity != second_identity
        assert first_identity[1][0] == ("7", "0")
        assert second_identity[1][0] == ("8", "0")
        sampler = WeatherSampler([first, second])
        assert {key[1][0] for key in sampler.groups} == {("7", "0"), ("8", "0")}
        assert _wind(sampler, START, 48.0, -124.0) is not None
    finally:
        first.close()
        second.close()


def test_ensemble_pdt1_is_a_separate_accepted_identity(tmp_path):
    contract_data, forecast_data = _load("contract.json"), _load("forecast.json")
    _write_forecast(tmp_path / "det.tif", forecast_data, contract_data, pdt=0)
    _write_forecast(
        tmp_path / "ens.tif",
        forecast_data,
        contract_data,
        ensemble_member=1,
    )
    deterministic = _source(tmp_path / "det.tif")
    ensemble = _source(tmp_path / "ens.tif")
    try:
        deterministic_identity = deterministic.sampling.records[0].identity
        ensemble_identity = ensemble.sampling.records[0].identity
        assert deterministic_identity[1][2] == ()
        assert ensemble_identity[1][2] == ("1", "1", "10")
        assert deterministic_identity != ensemble_identity
        assert _wind(WeatherSampler([ensemble]), START, 48.0, -124.0) is not None
    finally:
        deterministic.close()
        ensemble.close()


def test_unsupported_pdt2_and_wrong_wind_level_are_skipped(tmp_path):
    contract_data, forecast_data = _load("contract.json"), _load("forecast.json")
    _write_forecast(tmp_path / "pdt2.tif", forecast_data, contract_data, pdt=2)
    _write_forecast(
        tmp_path / "upper.tif",
        forecast_data,
        contract_data,
        levels={"UGRD": "500-ISBL", "VGRD": "500-ISBL"},
    )
    unsupported, upper = _source(tmp_path / "pdt2.tif"), _source(tmp_path / "upper.tif")
    try:
        assert unsupported.sampling.records == []
        assert WeatherSampler([unsupported]).wind_at(START, 48.0, -124.0) is None
        assert [
            record
            for record in upper.sampling.records
            if record.element in ("UGRD", "VGRD")
        ] == []
        assert WeatherSampler([upper]).wind_at(START, 48.0, -124.0) is None
        assert [
            record for record in upper.sampling.records if record.element == "PRMSL"
        ]
        pressure = WeatherSampler([upper]).sample(
            "pressure", START, np.array([48.0]), np.array([-124.0])
        )
        assert pressure[0, 0] == pytest.approx(
            contract_data["expected_observables"]["pressure_sw_h0_hpa"]
        )
    finally:
        unsupported.close()
        upper.close()
