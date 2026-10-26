from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from osgeo import gdal, osr

from gweatherrouting.core.forecastboundrouting import (
    BoundForecastError,
    BoundWorkerGrib,
    calculate_bound_route,
)
from gweatherrouting.core.forecasttranche import (
    AnalysisSelection,
    ForecastTranche,
    ModelConfiguration,
    ModelEntry,
)
from gweatherrouting.core.routingjob import CalculatedRoute, WorkerGrib
from gweatherrouting.core.tranchestore import TrancheStore

RUN = datetime(2025, 1, 1, tzinfo=timezone.utc)
ACQUIRED = datetime(2025, 1, 2, tzinfo=timezone.utc)


def binding(tmp_path):
    store = TrancheStore(tmp_path)
    source = tmp_path / "download"
    source.write_bytes(b"pinned forecast bytes")
    artifact = store.register_file(source)
    configuration = ModelConfiguration(
        "GFS", ("wind",), (-1, -1, 1, 1), "native", 3, (0, 3), 0
    )
    times = (RUN, RUN + timedelta(hours=3))
    entry = ModelEntry(
        "GFS",
        "GFS",
        RUN,
        "deterministic",
        ("wind",),
        ("10-HTGL",),
        (-1, -1, 1, 1),
        "0.25deg",
        times,
        (artifact,),
        ACQUIRED,
        (("wind", times),),
    )
    tranche = ForecastTranche("batch", ACQUIRED, (configuration,), (entry,))
    store.publish(tranche)
    store.activate(AnalysisSelection("batch", "GFS"))
    return store.resolve(AnalysisSelection("batch", "GFS"))


def band_metadata(element, hour, *, process="81", member="0", run=RUN):
    return {
        "GRIB_ELEMENT": element,
        "GRIB_SHORT_NAME": "10-HTGL",
        "GRIB_UNIT": "[m/s]",
        "GRIB_IDS": "CENTER=7",
        "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": f"0 0 0 0 {process}",
        "GRIB_PDS_PDTN": member,
        "GRIB_REF_TIME": str(int(run.timestamp())),
        "GRIB_FORECAST_SECONDS": str(hour * 3600),
        "GRIB_VALID_TIME": str(int(run.timestamp()) + hour * 3600),
    }


class RasterBand:
    def __init__(self, metadata, values):
        self.metadata = metadata
        self.values = values

    def GetMetadata(self):
        return self.metadata

    def ReadAsArray(self):
        return self.values

    def GetNoDataValue(self):
        return None

    def GetMaskFlags(self):
        return gdal.GMF_ALL_VALID


class Dataset:
    RasterXSize = 4
    RasterYSize = 4

    def __init__(self, bands, path="fixture"):
        self.bands = bands
        self.path = path
        spatial = osr.SpatialReference()
        spatial.ImportFromEPSG(4326)
        spatial.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        self.projection = spatial.ExportToWkt()

    @property
    def RasterCount(self):
        return len(self.bands)

    def GetRasterBand(self, index):
        return self.bands[index - 1]

    def GetGeoTransform(self):
        return (-2.0, 1.0, 0.0, 2.0, 0.0, -1.0)

    def GetProjection(self):
        return self.projection

    def GetDescription(self):
        return self.path


def wind_dataset(path, *, extra=(), process="81", member="0", run=RUN):
    rows = []
    for hour in (0, 3):
        rows.extend(
            [
                RasterBand(
                    band_metadata(
                        element, hour, process=process, member=member, run=run
                    ),
                    np.full((4, 4), -1.0 if element == "UGRD" else 0.0),
                )
                for element in ("UGRD", "VGRD")
            ]
        )
    rows.extend(extra)
    return Dataset(rows, str(path))


def patch_open(monkeypatch, dataset):
    opened = []

    def opener(path):
        opened.append(path)
        return dataset

    monkeypatch.setattr("gweatherrouting.core.grib.gdal.Open", opener)
    monkeypatch.setattr(
        "gweatherrouting.core.gribsampling.vector_flags",
        lambda path: [False] * dataset.RasterCount,
    )
    return opened


def test_production_bound_worker_samples_only_registered_pinned_artifact(
    tmp_path, monkeypatch
):
    captured = binding(tmp_path)
    dataset = wind_dataset(captured.artifact_paths[0])
    opened = patch_open(monkeypatch, dataset)
    worker = BoundWorkerGrib(captured)
    try:
        assert isinstance(worker, WorkerGrib)
        assert worker._index_data.cache_info().maxsize == 2
        wind = worker.get_wind_at(RUN, 0.0, 0.0)
        assert wind == pytest.approx((90.0, 1.0))
    finally:
        worker.close()
    assert opened == [str(captured.artifact_paths[0])]


@pytest.mark.parametrize(
    "extra,process,member,run",
    [
        ((RasterBand(band_metadata("UGRD", 6), np.ones((4, 4))),), "81", "0", RUN),
        ((), "83", "0", RUN),
        ((), "81", "1", RUN),
        ((), "81", None, RUN),
        ((), "81", "0", RUN + timedelta(hours=6)),
    ],
)
def test_bound_worker_rejects_mixed_or_unplanned_records(
    tmp_path, monkeypatch, extra, process, member, run
):
    captured = binding(tmp_path)
    dataset = wind_dataset(
        captured.artifact_paths[0],
        extra=extra,
        process=process,
        member=member,
        run=run,
    )
    patch_open(monkeypatch, dataset)
    with pytest.raises(BoundForecastError):
        BoundWorkerGrib(captured)


def test_bound_api_pins_provenance_to_the_published_tranche(tmp_path, monkeypatch):
    from gweatherrouting.core import forecastboundrouting as bound_module

    captured = binding(tmp_path)
    patch_open(monkeypatch, wind_dataset(captured.artifact_paths[0]))
    monkeypatch.setattr(
        bound_module,
        "create_requested_routing",
        lambda *args: SimpleNamespace(current_environment=None),
    )
    monkeypatch.setattr(
        bound_module,
        "run_requested_routing",
        lambda *args: CalculatedRoute((), (), {}),
    )
    request = SimpleNamespace(
        use_currents=False,
        auto_download=True,
    )
    result = calculate_bound_route(
        SimpleNamespace(marine_manager=None),
        request,
        captured,
        (),
        SimpleNamespace(is_set=lambda: False),
        lambda *args: None,
    )
    provenance = result.provenance["weather_binding"]
    assert provenance["tranche_id"] == "batch"
    assert provenance["entry_id"] == "GFS"
    assert provenance["artifact"]["sha256"] == captured.entry.artifacts[0].sha256
    assert result.provenance["weather_selection_mode"] == "tranche"

def test_missing_bound_artifact_fails_before_any_reader_open(tmp_path, monkeypatch):
    from dataclasses import replace

    captured = binding(tmp_path)
    missing = replace(captured, artifact_paths=(tmp_path / "absent.grib",))
    opened = []
    monkeypatch.setattr(
        "gweatherrouting.core.grib.gdal.Open", lambda path: opened.append(path)
    )
    with pytest.raises(BoundForecastError, match="missing or unsafe"):
        BoundWorkerGrib(missing)
    assert opened == []


def test_missing_component_at_planned_hour_is_not_borrowed_from_another_time(
    tmp_path, monkeypatch
):
    captured = binding(tmp_path)
    dataset = wind_dataset(captured.artifact_paths[0])
    dataset.bands.pop()
    patch_open(monkeypatch, dataset)
    with pytest.raises(BoundForecastError, match="coherent U/V pair"):
        BoundWorkerGrib(captured)


def test_bound_current_preflight_only_prefetches_current_fields(tmp_path, monkeypatch):
    from threading import Event
    from gweatherrouting.core import routingcoverage

    monkeypatch.setattr(
        routingcoverage,
        "check_current_coverage",
        lambda *args: SimpleNamespace(needs_warning=False, download_gaps=()),
    )
    monkeypatch.setattr(routingcoverage, "CoveragePrefetch", lambda *args: object())
    environment = SimpleNamespace(cancel=None, prefetch=None)
    routing = SimpleNamespace(
        current_environment=environment,
        grib=SimpleNamespace(get_wind_at=lambda *args: (90.0, 1.0)),
        position=(0.0, 0.0),
        time=RUN,
    )
    routingcoverage.prepare_coverage(
        routing,
        object(),
        True,
        Event(),
        lambda message: None,
        lambda result: True,
    )
    assert environment.prefetch is not None


def test_bound_frozen_current_wind_keeps_single_source_and_scenario_modifier(
    tmp_path, monkeypatch
):
    from threading import Event

    from gweatherrouting.core.marine.routing import FrozenRoutingWind
    from gweatherrouting.core.modifiedgrib import ModifiedGribManager
    from gweatherrouting.core.forecastboundrouting import BoundRoutingWind

    captured = binding(tmp_path)
    patch_open(monkeypatch, wind_dataset(captured.artifact_paths[0]))
    wind = BoundRoutingWind(captured, Event())
    try:
        source = wind.manager.gribs[0]
        override = ModifiedGribManager(wind, 2.0, 15.0)
        currents_wind = FrozenRoutingWind(wind.manager, override)
        assert len(currents_wind.gribs) == 1
        assert currents_wind.gribs[0].grib is source
        assert currents_wind.get_wind_at(RUN, 0.0, 0.0) == pytest.approx((105.0, 2.0))
    finally:
        wind.close()
