"""Session-level forecast metadata and decoded-band sharing across layers.

These checks are model-level and headless.  They prove that wind, wave fill,
wave arrows and chart pressure consumers hit one immutable session cache
without sharing GDAL handles.
"""

from datetime import timedelta
import os
from threading import Event, Thread

import pytest

from sailworks.core.grib import Grib
from sailworks.qt.gribviewport import GribViewportModel, ViewportCamera
from sailworks.rendering.weathercache import ForecastSession
from test.test_weather_barbs import Land
from test.test_weather_staging import gribs, START  # noqa: F401


def _camera(**overrides):
    values = dict(
        center_lat=0.5,
        center_lon=0.5,
        zoom=9,
        width=640,
        height=480,
        time=START + timedelta(hours=1),
    )
    values.update(overrides)
    return ViewportCamera(**values)


def _wait_ready(model, timeout=20.0, camera=None):
    import time as clock

    deadline = clock.monotonic() + timeout
    while clock.monotonic() < deadline:
        state, field, _selection = model.update(camera or _camera())
        if state == "ready":
            return field
        if model.state == "unavailable":
            raise AssertionError(model.error)
        clock.sleep(0.02)
    raise AssertionError("the viewport never prepared a field")


def _wave_source(gribs):
    metadata = {"GRIB_SHORT_NAME": "0-SFC", "GRIB_UNIT": "[m]"}
    return gribs.file(
        [
            ("SWH", 0, [[1.0, 1.5], [2.0, 2.5]], metadata),
            ("SWH", 3, [[3.0, 3.5], [4.0, 4.5]], metadata),
        ],
        end=3,
    )


def test_metadata_is_parsed_once_per_exact_file_version(gribs, monkeypatch):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    calls = []
    original = Grib.parse_metadata

    def counting(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(Grib, "parse_metadata", staticmethod(counting))
    session = ForecastSession()
    try:
        first = session.metadata(source.path)
        second = session.metadata(source.path)
        assert first is second
        assert len(calls) == 1
        statistics = session.statistics()
        assert statistics["metadata_parses"] == 1
        assert statistics["metadata_entries"] == 1
    finally:
        session.close()


def test_metadata_deduplicates_concurrent_first_use(gribs, monkeypatch):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    started = Event()
    release = Event()
    calls = []
    original = Grib.parse_metadata

    def slow(path):
        calls.append(path)
        started.set()
        assert release.wait(5)
        return original(path)

    monkeypatch.setattr(Grib, "parse_metadata", staticmethod(slow))
    session = ForecastSession()
    results = []
    try:
        first = Thread(target=lambda: results.append(session.metadata(source.path)))
        first.start()
        assert started.wait(5)
        second = Thread(target=lambda: results.append(session.metadata(source.path)))
        second.start()
        release.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert len(calls) == 1
        assert results[0] is results[1]
        assert session.statistics()["metadata_deduplicated"] == 1
    finally:
        session.close()


def test_metadata_identity_tracks_an_in_place_file_version(gribs, monkeypatch):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    calls = []
    original = Grib.parse_metadata

    def counting(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(Grib, "parse_metadata", staticmethod(counting))
    session = ForecastSession()
    try:
        session.metadata(source.path)
        stat = os.stat(source.path)
        os.utime(
            source.path,
            ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000),
        )
        session.metadata(source.path)
        assert len(calls) == 2
    finally:
        session.close()


def test_two_wind_consumers_share_the_same_decoded_bands(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    session = ForecastSession()
    first = GribViewportModel((source.path,), land_mask=Land(), session=session)
    second = GribViewportModel((source.path,), land_mask=Land(), session=session)
    try:
        _wait_ready(first)
        after_first = session.band_cache.statistics()
        assert after_first["decodes"] > 0
        _wait_ready(second)
        after_second = session.band_cache.statistics()
        assert after_second["decodes"] == after_first["decodes"]
        assert after_second["hits"] > 0
    finally:
        first.close()
        second.close()
        session.close()


def test_wave_fill_and_arrows_reuse_the_same_decoded_input(gribs):
    source = _wave_source(gribs)
    session = ForecastSession()
    fill = GribViewportModel(
        (source.path,),
        land_mask=Land(),
        parameter="wave_height",
        components=1,
        selection=False,
        grid=True,
        session=session,
    )
    arrows = GribViewportModel(
        (source.path,),
        land_mask=Land(),
        parameter="wave_height",
        components=1,
        selection=False,
        session=session,
    )
    try:
        _wait_ready(fill)
        after_fill = session.band_cache.statistics()
        _wait_ready(arrows)
        after_arrows = session.band_cache.statistics()
        # Height at the two endpoints is decoded once for both layers.
        assert after_fill["decodes"] == 2
        assert after_arrows["decodes"] == after_fill["decodes"]
    finally:
        fill.close()
        arrows.close()
        session.close()


def test_a_closed_session_does_not_retain_new_decodes():
    from collections import OrderedDict

    import numpy as np

    from sailworks.rendering.weathercache import SourceBandCache

    session = ForecastSession()
    session.close()

    class Owner:
        cache_identity = "after-close"

    owner = Owner()
    owner.arrays = SourceBandCache(session.band_cache, owner)
    value = session.band_cache.read(
        owner,
        session.band_cache.key(owner, 1),
        32,
        lambda: np.ones((2, 2)),
    )
    assert np.array_equal(value, np.ones((2, 2)))
    assert session.band_cache.statistics()["closed"] is True
    assert session.band_cache.current_bytes == 0
    assert isinstance(owner.arrays, OrderedDict)
