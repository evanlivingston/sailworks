"""NOAA satellite-blended wind import (no network: files are synthesized)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from sailworks.core import nbs
from sailworks.core.grib import Grib
from sailworks.core.tranchestore import TrancheStore

EPOCH = datetime(1978, 1, 1, tzinfo=timezone.utc)


def _write_day(path, times):
    import netCDF4

    dataset = netCDF4.Dataset(path, "w")
    dataset.createDimension("time", len(times))
    dataset.createDimension("zlev", 1)
    dataset.createDimension("lat", 21)
    dataset.createDimension("lon", 21)
    time = dataset.createVariable("time", "f8", ("time",))
    time.units = "hours since 1978-01-01"
    time[:] = [(value - EPOCH).total_seconds() / 3600 for value in times]
    lat = dataset.createVariable("lat", "f8", ("lat",))
    lat[:] = np.linspace(45.0, 50.0, 21)
    lon = dataset.createVariable("lon", "f8", ("lon",))
    lon[:] = np.linspace(234.0, 239.0, 21)
    for name, value in (("u_wind", 7.0), ("v_wind", -3.0)):
        variable = dataset.createVariable(name, "f4", ("time", "zlev", "lat", "lon"))
        variable[:] = value
    dataset.close()


def _plan(start, end):
    return nbs.plan_request(
        datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
        (49.54, -125.97, 45.2, -121.62),
    )


def test_plan_snaps_to_six_hourly_times():
    plan = _plan("2026-05-16T00:00", "2026-05-18T18:00")
    assert plan.times[0] == datetime(2026, 5, 16, tzinfo=timezone.utc)
    assert plan.times[-1] == datetime(2026, 5, 18, 18, tzinfo=timezone.utc)
    assert len(plan.times) == 12
    assert plan.days == (
        datetime(2026, 5, 16).date(),
        datetime(2026, 5, 17).date(),
        datetime(2026, 5, 18).date(),
    )


def test_plan_rejects_latency_and_pre_coverage():
    with pytest.raises(ValueError, match="two days"):
        nbs.plan_request(
            datetime.now(timezone.utc) - timedelta(hours=6),
            datetime.now(timezone.utc) - timedelta(hours=1),
            (50, -126, 45, -122),
        )
    with pytest.raises(ValueError, match="1987"):
        _plan("1987-06-30T00:00", "1987-06-30T18:00")


def test_build_publishes_an_nbs_tranche(tmp_path, monkeypatch):
    times = [
        datetime(2026, 5, 16, hour, tzinfo=timezone.utc) for hour in (0, 6, 12, 18)
    ]
    source = tmp_path / "day.nc"
    _write_day(source, times)
    monkeypatch.setattr(nbs, "download_day", lambda day, directory: source)

    store = TrancheStore(tmp_path / "store")
    state = SimpleNamespace(store=store)
    plan = _plan("2026-05-16T00:00", "2026-05-16T18:00")

    tranche = nbs.build(state, plan, directory=tmp_path)

    entry = tranche.entries[0]
    assert tranche.tranche_id.startswith("nbs-")
    assert entry.model_id == "NBS"
    assert entry.fields == ("wind",)
    assert entry.levels == ("10-HTGL",)
    assert entry.time_steps == plan.times

    artifact = store.root / entry.artifacts[0].path
    metadata = Grib.parse_metadata(str(artifact))
    assert metadata.model_name == "NBS"
    assert metadata.bounds == [-125.875, 45.125, -121.625, 49.625]


def test_build_rejects_missing_hours(tmp_path, monkeypatch):
    times = [datetime(2026, 5, 16, hour, tzinfo=timezone.utc) for hour in (0, 6)]
    source = tmp_path / "day.nc"
    _write_day(source, times)
    monkeypatch.setattr(nbs, "download_day", lambda day, directory: source)

    store = TrancheStore(tmp_path / "store")
    plan = _plan("2026-05-16T00:00", "2026-05-16T18:00")

    with pytest.raises(nbs.NbsError, match="every requested"):
        nbs.build(SimpleNamespace(store=store), plan, directory=tmp_path)
