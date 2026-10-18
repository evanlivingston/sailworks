from datetime import timedelta
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import Qt

from gweatherrouting.qt.gpuweatherjobs import WeatherStager
from test.test_weather_staging import gribs, START  # noqa: F401


def canvas_for(source):
    return SimpleNamespace(
        controller=SimpleNamespace(
            settings=SimpleNamespace(gribArrowOnGround=True),
            core=SimpleNamespace(grib_manager=SimpleNamespace(gribs=[source])),
        ),
        width=lambda: 640,
        height=lambda: 480,
        _center_lat=0.5,
        _center_lon=0.5,
        _zoom=9,
        wind_visible=True,
        time_control=SimpleNamespace(time=START + timedelta(hours=1)),
    )


class Interval:
    def contains(self, stamp):
        return START.timestamp() < stamp < START.timestamp() + 10800

    def weight(self, stamp):
        return (stamp - START.timestamp()) / 10800


def test_time_scrub_keeps_one_active_job_and_drops_covered_pending(gribs):
    canvas = canvas_for(gribs.file(gribs.field(0) + gribs.field(3), end=3))
    stager = WeatherStager()
    entered, release, finished = Event(), Event(), Event()
    captured = []
    worker_threads = []
    atlas = SimpleNamespace(staged=Interval())

    def stage(*args):
        worker_threads.append(get_ident())
        entered.set()
        assert release.wait(5)
        return atlas

    def complete(*args):
        captured.append(args)
        finished.set()

    stager.completed.connect(complete, Qt.ConnectionType.DirectConnection)
    try:
        with patch(
            "gweatherrouting.qt.gpuweatherjobs.stage_barbs", side_effect=stage
        ) as build:
            assert stager.request(canvas) is None
            assert entered.wait(5)
            for minute in range(61, 121):
                canvas.time_control.time = START + timedelta(minutes=minute)
                stager.request(canvas)
            assert not stager._active[3].is_set()
            assert stager._pending[1] == (START + timedelta(hours=2)).timestamp()
            release.set()
            assert finished.wait(5)
            # Simulate another render request before the queued GUI completion
            # slot has run: it must adopt the worker's mailbox, not rebuild.
            result, weight = stager.request(canvas)
            assert result is atlas and weight == 2 / 3
            assert stager._pending is None
            stager._accept(*captured[-1])
            assert build.call_count == 1
            assert worker_threads[0] != get_ident()
    finally:
        release.set()
        stager.close()
        if stager._thread is not None:
            stager._thread.join(5)
            assert not stager._thread.is_alive()


def test_view_change_cancels_stale_work_and_close_does_not_publish(gribs):
    canvas = canvas_for(gribs.file(gribs.field(0) + gribs.field(3), end=3))
    stager = WeatherStager()
    entered, release = Event(), Event()
    captured = []

    def stage(*args):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(staged=Interval())

    stager.completed.connect(
        lambda *args: captured.append(args), Qt.ConnectionType.DirectConnection
    )
    with patch("gweatherrouting.qt.gpuweatherjobs.stage_barbs", side_effect=stage):
        try:
            stager.request(canvas)
            assert entered.wait(5)
            active = stager._active
            old_key = active[0]
            canvas._center_lon = 0.6
            stager.request(canvas)
            assert active[3].is_set()
            stager._accept(old_key, SimpleNamespace(staged=Interval()), "")
            assert stager.frame is None
            stager.close()
            release.set()
            stager._thread.join(5)
            assert not stager._thread.is_alive()
            assert not captured
        finally:
            release.set()
            stager.close()


def test_unsupported_styles_and_hidden_wind_invalidate(gribs):
    canvas = canvas_for(gribs.file(gribs.field(0)))
    stager = WeatherStager()
    try:
        for name, value in (
            ("gribWindStyle", "streamlines"),
            ("gribBarbDensity", "all"),
        ):
            settings = canvas.controller.settings
            setattr(settings, name, value)
            assert stager.request(canvas) is None
            assert stager._thread is None
            delattr(settings, name)
        canvas.wind_visible = False
        assert stager.request(canvas) is None
        assert stager.latest is None
    finally:
        stager.close()


def test_potential_unread_cache_does_not_reject_selected_sources(gribs):
    canvas = canvas_for(gribs.file(gribs.field(0) + gribs.field(3), end=3))
    stager = WeatherStager()
    finished, captured = Event(), []
    atlas = SimpleNamespace(staged=Interval())
    # Metadata-only source whose possible cache would exceed the old guard.
    index = SimpleNamespace(
        width=100000, height=100000, dataset=SimpleNamespace(RasterCount=6)
    )

    def complete(*args):
        captured.append(args)
        finished.set()

    stager.completed.connect(complete, Qt.ConnectionType.DirectConnection)
    try:
        with (
            patch(
                "gweatherrouting.qt.gpuweatherjobs.WeatherSampler",
                return_value=SimpleNamespace(indices=[index]),
            ),
            patch(
                "gweatherrouting.qt.gpuweatherjobs.stage_barbs", return_value=atlas
            ) as build,
        ):
            stager.request(canvas)
            assert finished.wait(5)
            assert captured[-1][1] is atlas
            assert captured[-1][2] == ""
            assert build.call_count == 1
    finally:
        stager.close()
        stager._thread.join(5)
        assert not stager._thread.is_alive()


def test_worker_retains_only_bounded_private_arrays_and_releases_on_close(gribs):
    import numpy as np
    from gweatherrouting.rendering.weathercache import (
        DisplayGridIndex,
        DisplayWeatherSource,
    )

    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    canvas = canvas_for(source)
    stager = WeatherStager()
    stager.BAND_BYTE_LIMIT = 32
    finished, captured = Event(), []

    def stage(sampler, stamp, *_args):
        assert all(isinstance(index, DisplayGridIndex) for index in sampler.indices)
        values = sampler.sample_timestamps(
            "wind", stamp, np.array([0.25]), np.array([0.25])
        )
        assert np.isfinite(values).all()
        return SimpleNamespace(staged=Interval())

    def complete(*args):
        captured.append(args)
        finished.set()

    stager.completed.connect(complete, Qt.ConnectionType.DirectConnection)
    try:
        with patch("gweatherrouting.qt.gpuweatherjobs.stage_barbs", side_effect=stage):
            stager.request(canvas)
            assert finished.wait(5)
            assert captured[-1][2] == ""
            stats = stager.band_cache_statistics()
            assert stats["current_bytes"] == stats["peak_bytes"] == 32
            assert stats["evictions"] > 0
            assert "_sampling" not in source.__dict__
            released = Event()
            close_source = DisplayWeatherSource.close

            def close_private(provider):
                assert get_ident() != get_ident_gui
                close_source(provider)
                assert provider.band_cache.current_bytes == 0
                released.set()

            get_ident_gui = get_ident()
            with patch.object(DisplayWeatherSource, "close", close_private):
                canvas.wind_visible = False
                stager.request(canvas)
                assert released.wait(5)
            assert stager._thread.is_alive()
            # Source handles and their arrays close even when the worker is kept
            # alive for a later supported view, rather than only at map shutdown.
    finally:
        stager.close()
        stager._thread.join(5)
        assert not stager._thread.is_alive()
    assert stager.band_cache_statistics()["current_bytes"] == 0


def test_shutdown_during_staging_does_not_release_arrays_under_worker(gribs):
    canvas = canvas_for(gribs.file(gribs.field(0) + gribs.field(3), end=3))
    stager = WeatherStager()
    entered, release = Event(), Event()
    indices, completions = [], []

    def stage(sampler, stamp, *_args):
        sampler.sample_timestamps("wind", stamp, [0.5], [0.5])
        indices.extend(sampler.indices)
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(staged=Interval())

    stager.completed.connect(
        lambda *args: completions.append(args), Qt.ConnectionType.DirectConnection
    )
    try:
        with patch("gweatherrouting.qt.gpuweatherjobs.stage_barbs", side_effect=stage):
            stager.request(canvas)
            assert entered.wait(5)
            stager.close()
            assert stager._thread.is_alive()
            assert indices[0].dataset is not None
            assert indices[0].band_cache.current_bytes > 0
            release.set()
            stager._thread.join(5)
            assert not stager._thread.is_alive()
            assert indices[0].dataset is None
            assert not completions
            assert stager.band_cache_statistics()["current_bytes"] == 0
    finally:
        release.set()
        stager.close()
        if stager._thread is not None:
            stager._thread.join(5)


def test_worker_oversized_band_falls_back_without_publishing_partial_atlas(gribs):
    from gweatherrouting.rendering.weathercache import WeatherBandLimitError

    canvas = canvas_for(gribs.file(gribs.field(0) + gribs.field(3), end=3))
    stager = WeatherStager()
    stager.BAND_BYTE_LIMIT = 1
    finished, captured = Event(), []

    def stage(sampler, stamp, *_args):
        sampler.sample_timestamps("wind", stamp, [0.5], [0.5])
        raise AssertionError("Oversized-band rejection was swallowed by sampler")

    def complete(*args):
        captured.append(args)
        finished.set()

    stager.completed.connect(complete, Qt.ConnectionType.DirectConnection)
    try:
        with patch("gweatherrouting.qt.gpuweatherjobs.stage_barbs", side_effect=stage):
            stager.request(canvas)
            assert finished.wait(5)
            key, atlas, error = captured[-1]
            assert atlas is None and "worker limit" in error
            stager._accept(key, atlas, error)
            assert stager.frame is None and stager._prepared is None
            assert stager.request(canvas) is None
            assert stager.band_cache_statistics()["current_bytes"] == 0
            assert not issubclass(WeatherBandLimitError, ValueError)
    finally:
        stager.close()
        stager._thread.join(5)
        assert not stager._thread.is_alive()
