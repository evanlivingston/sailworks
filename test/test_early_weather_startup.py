"""Isolated Qt startup scheduling/state tests without opening application inputs."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
import unittest

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget

from gweatherrouting.qt.chartstartup import ChartStartup
from gweatherrouting.qt.lazyplots import LazyLogPlots


class FakeTask(QObject):
    changed = Signal()
    busy = False
    message = ""


class Owner(QObject):
    def __init__(self):
        super().__init__()
        self.closed = False
        self.settings = {
            "vectorCharts": [{"path": "a", "enabled": False}, {"path": "bad"}],
            "rasterCharts": [],
            "mbtilesCharts": [],
        }
        self.chart_errors = []
        self.calls = []
        self.charts = SimpleNamespace(
            charts=[],
            load_base_chart=lambda: self.add("base"),
            load_vector_layer=self.vector,
            load_raster_layer=self.vector,
            load_mbtiles_layer=self.vector,
        )
        self.map_data = SimpleNamespace(task=FakeTask())
        self.data_signal = FakeTask(self)
        self.data = SimpleNamespace(
            changed=self.data_signal.changed, redraw=lambda: self.calls.append("redraw")
        )
        self.core = SimpleNamespace(grib_manager=SimpleNamespace(gribs=["forecast"]))

    def add(self, value):
        self.charts.charts.append(value)

    def vector(self, path, metadata=None, enabled=True):
        if path == "bad":
            raise ValueError("bad chart")
        self.calls.append((path, enabled))
        self.add(path)


class TestEarlyStartup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_stages_publish_atomically_and_preserve_enabled_flag(self):
        owner = Owner()
        ready = {"current": True}
        canvas = SimpleNamespace(
            wind=SimpleNamespace(prepared_view_status=lambda _: ready)
        )
        startup = ChartStartup(owner)
        self.assertTrue(owner.map_data.task.busy)
        startup.start(canvas)
        startup.timer.stop()
        startup.advance()
        self.assertEqual(owner.charts.charts, [])
        for _ in range(3):
            startup.timer.stop()
            startup.advance()
            self.assertEqual(owner.charts.charts, [])
            self.assertTrue(owner.map_data.task.busy)
        startup.timer.stop()
        startup.advance()
        self.assertEqual(owner.charts.charts, ["base", "a"])
        self.assertIn(("a", False), owner.calls)
        self.assertFalse(owner.map_data.task.busy)
        self.assertTrue(startup.weather_current_before_charts)
        self.assertEqual(len(owner.chart_errors), 1)
        startup.close()

    def test_waiting_current_generation_and_close_reject_late_callbacks(self):
        owner = Owner()
        canvas = SimpleNamespace(
            wind=SimpleNamespace(prepared_view_status=lambda _: {"current": False})
        )
        startup = ChartStartup(owner)
        startup.start(canvas)
        startup.timer.stop()
        startup.advance()
        self.assertIsNone(startup.jobs)
        startup.close()
        startup.advance()
        self.assertEqual(startup.state, "closed")
        self.assertEqual(owner.charts.charts, [])

    def test_no_forecasts_does_not_wait_forever(self):
        owner = Owner()
        owner.core.grib_manager.gribs = []
        canvas = SimpleNamespace(
            wind=SimpleNamespace(prepared_view_status=lambda _: {"current": False})
        )
        startup = ChartStartup(owner)
        startup.start(canvas)
        startup.timer.stop()
        startup.advance()
        self.assertIsNotNone(startup.jobs)
        self.assertFalse(startup.weather_current_before_charts)
        startup.close()

    def test_weather_failure_timeout_releases_chart_initialization(self):
        owner = Owner()
        canvas = SimpleNamespace(
            wind=SimpleNamespace(prepared_view_status=lambda _: {"current": False})
        )
        startup = ChartStartup(owner)
        startup.start(canvas)
        startup.timer.stop()
        startup.began -= 6
        startup.advance()
        self.assertEqual(startup.state, "loading")
        self.assertFalse(startup.weather_current_before_charts)
        startup.close()

    def test_disabled_weather_or_hidden_map_does_not_delay_charts(self):
        for hidden in (True, False):
            with self.subTest(hidden=hidden):
                owner = Owner()
                owner.settings["gribIsobarsVisible"] = False
                canvas = SimpleNamespace(
                    wind_visible=hidden,
                    isVisible=lambda: not hidden,
                    wind=SimpleNamespace(
                        prepared_view_status=lambda _: {"current": False}
                    ),
                )
                startup = ChartStartup(owner)
                startup.start(canvas)
                startup.timer.stop()
                startup.advance()
                self.assertEqual(startup.state, "loading")
                startup.close()

    def test_superseded_weather_is_not_used_to_start_charts(self):
        owner = Owner()
        status = {"current": False}
        canvas = SimpleNamespace(
            wind=SimpleNamespace(prepared_view_status=lambda _: status)
        )
        startup = ChartStartup(owner)
        startup.start(canvas)
        startup.timer.stop()
        startup.advance()
        self.assertIsNone(startup.jobs)
        status["current"] = True
        startup.timer.stop()
        startup.advance()
        self.assertIsNotNone(startup.jobs)
        self.assertTrue(startup.weather_current_before_charts)
        startup.close()

    def test_lazy_plot_replays_latest_data_options_and_cursor(self):
        calls = []

        class Plot(QWidget):
            def set_series(self, *args):
                calls.append(("series", args))

            def set_points(self, *args):
                calls.append(("points", args))

            def set_position(self, *args):
                calls.append(("position", args))

        lazy = LazyLogPlots(factory=Plot)
        lazy.set_points(["old"], [0], False)
        lazy.set_points(["latest"], [3], True)
        lazy.set_series("speed", False)
        lazy.set_position(7)
        self.assertIsNone(lazy.plot)
        lazy.show()
        self.app.processEvents()
        self.assertIsNotNone(lazy.plot)
        self.assertIn(("points", (("latest",), (3,), True)), calls)
        self.assertIn(("series", ("speed", False)), calls)
        self.assertIn(("position", (7,)), calls)
        lazy.close()


if __name__ == "__main__":
    unittest.main()
