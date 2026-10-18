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
