"""Identity, schedule, pressure-alias and opt-in live-probe regressions."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re

import numpy as np
import pytest
from osgeo import osr

from gweatherrouting.core.forecastcapabilities import (
    MODEL_CAPABILITIES,
    model_from_grib_metadata,
)
from gweatherrouting.core.forecastrefreshcontracts import PlannedModel
from gweatherrouting.core.forecastrefreshplan import (
    UnsupportedConfigurationError,
    default_provider_factories,
    plan_refresh,
)
from gweatherrouting.core.forecasttranche import (
    AnalysisSelection,
    ForecastTranche,
    ModelConfiguration,
    ModelEntry,
)
from gweatherrouting.core.forecastvalidation import inspect_download
from gweatherrouting.core.forecastboundrouting import BoundWorkerGrib
from gweatherrouting.core.grib import Grib, PRESSURE_ELEMENTS, forecast_model_name
from gweatherrouting.core.gribfields import is_mean_sea_level_pressure
from gweatherrouting.core.gribsampling import FIELDS, GridIndex, wind_value
from gweatherrouting.core.tranchestore import TrancheStore

RUN = datetime(2026, 10, 24, tzinfo=timezone.utc)
REQUESTED = (-1.0, -1.0, 1.0, 1.0)


def public_metadata(model, *, gpi=None, pdt="0", message_type=None):
    cap = MODEL_CAPABILITIES[model]
    product = cap.grib_product
    if gpi is not None:
        product = (*product[:2], gpi)
    typ = cap.grib_type if message_type is None else message_type
    return {
        "GRIB_IDS": (
            f"CENTER={cap.grib_center} SUBCENTER={cap.grib_subcenter} TYPE={typ}"
        ),
        "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": (
            f"2 2 {product[0]} {product[1]} {product[2]}"
        ),
        "GRIB_PDS_PDTN": pdt,
    }


@pytest.mark.parametrize("model", ["IFS", "AIFS", "AIGFS"])
def test_public_identity_is_metadata_only_and_rejects_near_misses(model):
    metadata = public_metadata(model)
    assert model_from_grib_metadata(None, metadata) == model
    assert forecast_model_name(None, metadata) == model
    assert model_from_grib_metadata(None, public_metadata(model, gpi=999)) == "Unknown"
    assert model_from_grib_metadata(None, public_metadata(model, pdt="1")) == "Unknown"
    assert (
        model_from_grib_metadata(None, public_metadata(model, message_type=99))
        == "Unknown"
    )


def test_aifs_type10_is_deterministic_only_when_pdt_is_zero():
    assert (
        model_from_grib_metadata(
            None, public_metadata("AIFS", pdt="0", message_type=10)
        )
        == "AIFS"
    )
    assert (
        model_from_grib_metadata(
            None, public_metadata("AIFS", pdt="1", message_type=10)
        )
        == "Unknown"
    )


def test_capability_ui_contract_and_lazy_source_registry():
    assert set(MODEL_CAPABILITIES) == {"GFS", "HRRR", "HRDPS", "IFS", "AIFS", "AIGFS"}
    assert MODEL_CAPABILITIES["GFS"].default_hours == 120
    assert MODEL_CAPABILITIES["HRRR"].default_hours == 48
    assert MODEL_CAPABILITIES["HRDPS"].default_hours == 48
    assert MODEL_CAPABILITIES["IFS"].intervals == (0, 3, 6, 12)
    assert MODEL_CAPABILITIES["AIFS"].intervals == (0, 6, 12)
    assert MODEL_CAPABILITIES["AIGFS"].global_download
    assert MODEL_CAPABILITIES["IFS"].license_url
    factories = default_provider_factories()
    assert {"IFS", "AIFS", "AIGFS"} <= set(factories)


def test_plan_prefers_long_ifs_run_and_rejects_unsupported_interval_before_factory():
    class FakeIFS:
        name = "ECMWF IFS"

        def get_download_list(self, options):
            rows = []
            for cycle in ("06", "00"):
                schedule = MODEL_CAPABILITIES["IFS"].available_steps(int(cycle), "wind")
                steps = tuple(
                    h
                    for h in schedule
                    if h <= options.hours and h % options.interval == 0
                )
                if int(cycle) == 6:
                    steps = tuple(h for h in steps if h <= 144)
                payload = {
                    "source": self.name,
                    "date": "20261024",
                    "cycle": cycle,
                    "hours": max(steps),
                    "steps": list(steps),
                    "fields": list(options.fields),
                }
                rows.append(["label", self.name, "size", "date", json.dumps(payload)])
            return rows

    calls = []

    def make_source():
        calls.append(True)
        return FakeIFS()

    configuration = ModelConfiguration(
        "IFS", ("wind",), REQUESTED, "native", 200, (), 6
    )
    plan = plan_refresh((configuration,), "IFS", factories={"IFS": make_source})
    assert plan.models[0].run_time.hour == 0
    assert max(plan.models[0].field_steps[0][1]) == 198
    assert calls == [True]

    calls.clear()
    invalid = ModelConfiguration("AIFS", ("wind",), REQUESTED, "native", 12, (), 3)
    with pytest.raises(UnsupportedConfigurationError, match="interval"):
        plan_refresh((invalid,), "AIFS", factories={"AIFS": make_source})
    assert calls == []


def test_pres_alias_is_only_msl_and_sampling_indexes_only_that_level(monkeypatch):
    assert PRESSURE_ELEMENTS == FIELDS["pressure"] == ("PRMSL", "MSLMA", "PRES")
    msl = {"GRIB_ELEMENT": "PRES", "GRIB_SHORT_NAME": "0-MSL"}
    assert is_mean_sea_level_pressure(msl)
    assert not is_mean_sea_level_pressure(
        {"GRIB_ELEMENT": "PRES", "GRIB_SHORT_NAME": "0-SFC"}
    )
    assert not is_mean_sea_level_pressure(
        {"GRIB_ELEMENT": "PRES", "GRIB_SHORT_NAME": "500-ISBL"}
    )
    assert not is_mean_sea_level_pressure({"GRIB_ELEMENT": "PRES"})

    class Band:
        def __init__(self, metadata):
            self.metadata = metadata

        def GetMetadata(self):
            return self.metadata

    class Dataset:
        RasterXSize = 4
        RasterYSize = 4

        def __init__(self, bands):
            self.bands = [Band(item) for item in bands]
            spatial = osr.SpatialReference()
            spatial.ImportFromEPSG(4326)
            self.projection = spatial.ExportToWkt()

        @property
        def RasterCount(self):
            return len(self.bands)

        def GetGeoTransform(self):
            return (-2.0, 1.0, 0.0, 2.0, 0.0, -1.0)

        def GetProjection(self):
            return self.projection

        def GetDescription(self):
            return "pressure-fixture"

        def GetRasterBand(self, index):
            return self.bands[index - 1]

    class Source:
        path = "pressure-fixture"
        name = "IFS"
        centre = "ECMWF"
        model_name = "IFS"
        start_time = RUN
        end_time = RUN

        def __init__(self, dataset):
            self.dataset = dataset

    base = {
        **public_metadata("IFS"),
        "GRIB_ELEMENT": "PRES",
        "GRIB_SHORT_NAME": "0-MSL",
        "GRIB_UNIT": "[Pa]",
        "GRIB_REF_TIME": str(int(RUN.timestamp())),
        "GRIB_VALID_TIME": str(int(RUN.timestamp())),
        "GRIB_FORECAST_SECONDS": "0",
    }
    wrong_level = {**base, "GRIB_SHORT_NAME": "500-ISBL"}
    missing_level = {
        key: value for key, value in base.items() if key != "GRIB_SHORT_NAME"
    }
    dataset = Dataset([base, wrong_level, missing_level])
    monkeypatch.setattr(
        "gweatherrouting.core.gribsampling.vector_flags", lambda path: []
    )
    index = GridIndex(Source(dataset))
    assert [record.element for record in index.records] == ["PRES"]


def test_live_public_model_gribs_validate_sample_and_bind_when_probe_root_is_set(
    tmp_path,
):
    probe_root = os.environ.get("GWR_TEST_PUBLIC_GRIB_ROOT")
    if not probe_root:
        pytest.skip("set GWR_TEST_PUBLIC_GRIB_ROOT to the supplied public GRIB probes")
    from osgeo import gdal

    from gweatherrouting.core.forecastboundrouting import BoundWorkerGrib

    filenames = {
        "IFS": "ifs-surface-0h.grib2",
        "AIFS": "aifs-surface-0h.grib2",
        "AIGFS": "aigfs-surface-0h.grib2",
    }
    metadata_files = {
        "IFS": "ifs-metadata.json",
        "AIFS": "aifs-metadata.json",
        "AIGFS": "aigfs-metadata.json",
    }
    root = Path(probe_root)
    for model, filename in filenames.items():
        path = root / filename
        probe = json.loads((root / metadata_files[model]).read_text())
        bands = probe["bands"]
        run = datetime.fromtimestamp(float(bands[0]["GRIB_REF_TIME"]), timezone.utc)
        configuration = ModelConfiguration(
            model, ("wind", "pressure"), REQUESTED, "native", 0, (0,), 0
        )
        capability = MODEL_CAPABILITIES[model]
        payload = {
            "source": capability.provider_name,
            "date": run.strftime("%Y%m%d"),
            "cycle": run.strftime("%H"),
            "hours": 0,
            "steps": [0],
            "fields": ["wind", "pressure"],
        }
        planned = PlannedModel(
            configuration,
            run,
            capability.provider_name,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            capability.native_resolution,
            (("wind", (0,)), ("pressure", (0,))),
        )
        validated = inspect_download(path, planned)
        assert validated.model_id == model
        assert validated.region == (-90.0, -180.0, 90.0, 180.0)

        grib = Grib.parse(str(path))
        try:
            pressure = grib.get_pressure(run, ((-0.25, -0.25), (0.25, 0.25)))
            assert pressure
            raster = gdal.Open(str(path))
            pressure_band = next(
                raster.GetRasterBand(index)
                for index in range(1, raster.RasterCount + 1)
                if raster.GetRasterBand(index).GetMetadata().get("GRIB_ELEMENT")
                in PRESSURE_ELEMENTS
            )
            raw_hpa = float(pressure_band.ReadAsArray()[360, 720]) / 100.0
            point = min(pressure, key=lambda value: abs(value[0]) + abs(value[1]))
            assert point[2] == pytest.approx(raw_hpa)
            raster = None
        finally:
            grib.close()

        store = TrancheStore(tmp_path / model)
        artifact = store.register_file(path)
        entry = ModelEntry(
            model,
            model,
            run,
            "deterministic",
            validated.fields,
            validated.levels,
            validated.region,
            validated.resolution,
            validated.time_steps,
            (artifact,),
            run + timedelta(hours=1),
            validated.field_times,
        )
        tranche = ForecastTranche(
            "probe-" + model,
            run + timedelta(hours=1),
            (configuration,),
            (entry,),
        )
        store.publish(tranche)
        binding = store.resolve(AnalysisSelection(tranche.tranche_id, model))
        worker = BoundWorkerGrib(binding)
        try:
            actual_wind = worker.get_wind_at(run, 0.0, 0.0)
            dataset = gdal.Open(str(path))
            components = {}
            for band_id in range(1, dataset.RasterCount + 1):
                band = dataset.GetRasterBand(band_id)
                element = band.GetMetadata().get("GRIB_ELEMENT")
                if element in ("UGRD", "VGRD"):
                    components[element] = float(band.ReadAsArray()[360, 720])
            expected_wind = wind_value((components["UGRD"], components["VGRD"]))
            assert actual_wind == pytest.approx(expected_wind)
            dataset = None
        finally:
            worker.close()
