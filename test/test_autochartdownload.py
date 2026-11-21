import json
import tempfile
import time
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject

from sailworks.core.enccharts import (
    COVERAGE_SAMPLE_GRID,
    ENCCell,
    coverage_sample_points,
    display_scale_denominator,
    missing_chart_for_view,
)
from sailworks.core.mapviewport import MapViewport, MercatorProjection
from sailworks.qt.datacontroller import (
    AUTO_CHART_COOLDOWN,
    DataController,
)
from sailworks.qt.encdemand import auto_chart_ready_request, auto_chart_view
from sailworks.qt.gribviewport import _viewport_widget_class
from sailworks.qt import viewportenc

# The acceptance view: the mouth of the Columbia at z10.738, where the display
# scale is ~1:237k, not the ~1:40k of a 10.738 m/px raster assumption.
CENTER_LAT, CENTER_LON, ZOOM = 46.85, -124.03, 10.738
VIEW = (46.4, -124.5, 47.3, -123.6)


class FakeCanvas(MercatorProjection):
    def __init__(self, lat, lon, zoom, width=1200, height=800):
        self._center_lat, self._center_lon, self._zoom = lat, lon, zoom
        self._width, self._height = width, height

    def get_allocated_width(self):
        return self._width

    def get_allocated_height(self):
        return self._height


class FakeCamera:
    """The attributes ``ChartCamera`` reads from a viewport camera."""

    def __init__(self, lat=CENTER_LAT, lon=CENTER_LON, zoom=ZOOM):
        self.center_lat, self.center_lon, self.zoom = lat, lon, zoom
        self.width, self.height = 1200, 800


METERS_PER_PIXEL = MapViewport(
    FakeCanvas(CENTER_LAT, CENTER_LON, ZOOM), 0, lambda: None
).get_scale()

# The reported live camera: the Columbia mouth at z12.2591, 1512x827.  The
# 1:180k chart is installed and enabled, and three 1:45k cells outrank it.
COLUMBIA_METERS_PER_PIXEL = 22.105674
COLUMBIA_VIEW = (46.11399, -124.13047, 46.27819, -123.69677)


def cell(name, scale, box):
    south, west, north, east = box
    panel = [("E", [(south, west), (south, east), (north, east), (north, west)])]
    return ENCCell(name, name, scale, 0, panel)


def coarse():
    return cell("US2COARS", 700000, (46.8, -124.6, 47.4, -123.5))


def detail():
    return cell("US3DETAI", 180000, (46.2, -124.6, 46.8, -123.5))


def harbour():
    return cell("US5HARBO", 45000, (46.45, -124.45, 46.6, -124.32))


class TestDisplayScale(unittest.TestCase):
    def test_view_scale_is_the_real_projection_scale(self):
        # 46.85N at z10.738 is ~62.8 m/px, i.e. a ~1:237k display scale, not
        # the 1:40k one a 10.738 m/px assumption would produce.
        self.assertAlmostEqual(METERS_PER_PIXEL, 62.83, delta=0.5)
        denominator = display_scale_denominator(METERS_PER_PIXEL)
        self.assertAlmostEqual(denominator, 237500, delta=1500)

    def test_view_matches_the_viewport_projection(self):
        view = auto_chart_view(FakeCamera())
        self.assertIsNotNone(view)
        bounds, meters_per_pixel = view
        canvas = FakeCanvas(CENTER_LAT, CENTER_LON, ZOOM)
        viewport = MapViewport(canvas, 0, lambda: None)
        top_left, bottom_right = viewport.get_bbox()
        self.assertAlmostEqual(meters_per_pixel, viewport.get_scale())
        self.assertAlmostEqual(bounds[0], bottom_right.get_degrees()[0])
        self.assertAlmostEqual(bounds[1], top_left.get_degrees()[1])
        self.assertAlmostEqual(bounds[2], top_left.get_degrees()[0])
        self.assertAlmostEqual(bounds[3], bottom_right.get_degrees()[1])

    def test_no_view_without_a_camera(self):
        self.assertIsNone(auto_chart_view(None))
        self.assertIsNone(auto_chart_view(SimpleNamespace()))


class TestMissingChartSelection(unittest.TestCase):
    def test_samples_span_the_visible_box(self):
        points = coverage_sample_points(VIEW)
        self.assertEqual(len(points), COVERAGE_SAMPLE_GRID**2)
        self.assertTrue(all(VIEW[0] < lat < VIEW[2] for lat, _ in points))
        self.assertTrue(all(VIEW[1] < lon < VIEW[3] for lat, lon in points))
        self.assertEqual(len(coverage_sample_points(VIEW, 100)), 64)

    def test_samples_wrap_across_antimeridian(self):
        points = coverage_sample_points((-2, 179, 2, -179))
        self.assertTrue(all(-180 <= lon <= 180 for lat, lon in points))

    def test_startup_view_selects_the_chart_south_of_the_centre(self):
        bounds, meters_per_pixel = auto_chart_view(FakeCamera())
        overview = cell("US2COAST", 700000, (46.2, -125.0, 47.5, -123.0))
        south = cell("US3WA1CE", 180000, (46.6, -124.3, 46.82, -123.8))
        self.assertFalse(south.contains(CENTER_LAT, CENTER_LON))
        # A cold start has no fallback on screen, so the cell that covers the
        # whole view is fetched first.
        cold = missing_chart_for_view([overview, south], bounds, meters_per_pixel)
        self.assertIs(cold, overview)
        # The coarse chart is on screen; the detailed southern cell now wins
        # the samples it covers and is the one fetched while it stays.
        self.assertIs(
            missing_chart_for_view(
                [overview, south],
                bounds,
                meters_per_pixel,
                installed={"US2COAST"},
                enabled={"US2COAST"},
            ),
            south,
        )

    def test_detailed_chart_wins_at_the_real_display_scale(self):
        # 1:180k is nearest to the ~1:237k display; 1:700k is far too coarse.
        cells = [coarse(), detail()]
        self.assertIs(
            missing_chart_for_view(cells, VIEW, METERS_PER_PIXEL),
            cells[1],
        )

    def test_coarse_chart_wins_when_it_is_the_only_choice(self):
        cells = [coarse()]
        self.assertIs(
            missing_chart_for_view(cells, VIEW, METERS_PER_PIXEL),
            cells[0],
        )

    def test_harbour_chart_does_not_win_at_this_scale(self):
        # 1:45k is farther from the display scale than 1:180k, so the smaller
        # cell does not outrank the installed one.
        cells = [detail(), harbour()]
        self.assertIsNone(
            missing_chart_for_view(
                cells,
                VIEW,
                METERS_PER_PIXEL,
                installed={"US3DETAI"},
                enabled={"US3DETAI"},
            )
        )
        self.assertIs(
            missing_chart_for_view(cells, VIEW, METERS_PER_PIXEL),
            cells[0],
        )

    def test_skips_installed_and_disabled_cells(self):
        cells = [coarse(), detail()]
        self.assertIsNone(
            missing_chart_for_view(
                cells,
                VIEW,
                METERS_PER_PIXEL,
                installed={"US2COARS", "US3DETAI"},
                enabled={"US2COARS"},
            )
        )

    def test_skips_cell_that_does_not_beat_the_enabled_chart(self):
        worse = cell("US4WORSE", 400000, (46.2, -124.6, 46.8, -123.5))
        self.assertIsNone(
            missing_chart_for_view(
                [detail(), worse],
                VIEW,
                METERS_PER_PIXEL,
                installed={"US3DETAI"},
                enabled={"US3DETAI"},
            )
        )

    def test_skips_overview_chart_when_zoomed_in(self):
        overview = cell("US1OVERW", 1500000, (46.2, -125.0, 47.4, -123.0))
        self.assertIsNone(missing_chart_for_view([overview], VIEW, 0.5))

    def test_returns_none_without_catalog_cells(self):
        self.assertIsNone(missing_chart_for_view([], VIEW, METERS_PER_PIXEL))


class FakeSettings(dict):
    chartPalette = "day"


class FakeOwner(QObject):
    def __init__(self):
        super().__init__()
        self.routing = SimpleNamespace(task=SimpleNamespace(busy=False))
        self.polars = SimpleNamespace(task=SimpleNamespace(busy=False))
        self.map_data = SimpleNamespace(task=SimpleNamespace(busy=False))
        self.settings = FakeSettings()
        self.charts = SimpleNamespace(charts=[], _chart_surfaces={}, maps=[])
        self.scheduler = SimpleNamespace(submit=lambda *a, **k: None)


class FakeCatalog:
    def __init__(self, cells=(), existing=None, fail=()):
        self.cells = list(cells)
        self.existing = existing
        self.downloads = []
        self.loads = []
        self.fail = set(fail)

    def load(self, fetch=True):
        self.loads.append(fetch)
        return None

    def downloaded_path(self, cell):
        return self.existing

    def download(self, cell):
        if cell.name in self.fail:
            raise RuntimeError("offline")
        self.downloads.append(cell)
        return f"/data/enc/{cell.name}/{cell.name}.000"


class SynchronousAutoTask:
    """An auto task that runs its operation inline, as a finished slot does."""

    def __init__(self, controller):
        self.controller = controller
        self.busy = False
        self.closed = False
        self.message = ""
        self.runs = 0

    def start(self, message, operation):
        self.message = message
        self.runs += 1
        self.busy = True
        try:
            try:
                result = operation(None)
            except Exception as exc:
                # The real task reports worker exceptions through ``finished``.
                self.controller._auto_finish(None, exc)
            else:
                self.controller._auto_finish(result, None)
        finally:
            self.busy = False
        return True


class FakeViewport:
    """The viewport surface the presenter wires to the data controller."""

    def __init__(self):
        self.calls = []
        self.data_controller = None

    def _request_chart(self, cause="settle"):
        self.calls.append(cause)

    def attach_data_controller(self, controller):
        self.data_controller = controller
        controller.autoChartReady.connect(self._auto_chart_ready)

    def _auto_chart_ready(self):
        auto_chart_ready_request(self)


class TestAutoDownloadController(unittest.TestCase):
    def setUp(self):
        # The install writes the user's entry list, and the controller merges
        # the entries already in the settings file.  That file lives in the
        # application data directory, so the test points it at a temporary one
        # and never reads, or leaves behind, the developer's own charts.
        self.data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.data_dir.cleanup)
        data_dir_patch = patch(
            "sailworks.qt.datacontroller.DATA_DIR", self.data_dir.name
        )
        data_dir_patch.start()
        self.addCleanup(data_dir_patch.stop)
        self.owner = FakeOwner()
        self.controller = DataController(self.owner)
        self.start = Mock(return_value=True)
        self.controller.auto_task = Mock(busy=False, closed=False, start=self.start)

    def test_disabled_by_setting(self):
        self.owner.settings["chartAutoDownload"] = False
        self.assertFalse(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        self.start.assert_not_called()

    def test_disabled_while_enc_class_hidden(self):
        self.owner.settings["chartENCVisible"] = False
        self.assertFalse(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        self.start.assert_not_called()

    def test_prior_installs_do_not_block_a_new_view(self):
        # Three installs are already recorded for this session, as they are
        # once the Columbia view is filled.  A new view must still be covered:
        # there is no per-session success limit to stop it.
        self.owner.settings["vectorCharts"] = []
        self.controller.auto_downloads = 3
        self.controller._auto_attempts.update({"US4OR1ND", "US4OR1NE", "US4WA1CF"})
        catalog = FakeCatalog([cell("US4PUGET", 45000, (47.0, -122.9, 47.5, -122.3))])
        self.controller.catalog = catalog
        self.controller._auto_chart = lambda path, palette: (
            SimpleNamespace(ctype="vector", path=path, metadata={}, enabled=True),
            Mock(),
        )
        self.controller.auto_task = SynchronousAutoTask(self.controller)

        moved = (47.0, -122.95, 47.55, -122.25)
        self.assertTrue(
            self.controller.download_missing_chart(moved, COLUMBIA_METERS_PER_PIXEL)
        )
        self.assertEqual([c.name for c in catalog.downloads], ["US4PUGET"])
        self.assertEqual(self.controller.auto_downloads, 4)

    def test_cooldown_after_failure(self):
        self.controller._auto_operation = "chart-auto"
        self.controller._auto_cell = "US3DETAI"
        self.controller._auto_finish(None, RuntimeError("offline"))
        self.assertFalse(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        self.start.assert_not_called()

    def test_rejects_degenerate_views(self):
        for bounds, meters_per_pixel in (
            (VIEW, 0),
            (VIEW, -1),
            (VIEW, float("nan")),
            ((47.3, -124.5, 46.4, -123.6), METERS_PER_PIXEL),
            ("bad", METERS_PER_PIXEL),
        ):
            self.assertFalse(
                self.controller.download_missing_chart(bounds, meters_per_pixel)
            )
        self.start.assert_not_called()

    def test_manual_busy_task_does_not_block_auto(self):
        self.controller.task.busy = True
        self.assertTrue(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        self.start.assert_called_once()

    def test_manual_failure_does_not_start_the_auto_cooldown(self):
        self.controller.operation = "chart"
        self.controller._finish(None, RuntimeError("disk full"))
        self.assertEqual(self.controller.error, "disk full")
        self.assertEqual(self.controller._auto_cooldown_until, 0.0)

    def test_auto_failure_leaves_the_manual_task_alone(self):
        message = self.controller.task.message
        self.controller._auto_operation = "chart-auto"
        self.controller._auto_cell = "US3DETAI"
        self.controller._auto_finish(None, RuntimeError("offline"))
        self.assertEqual(self.controller.task.message, message)
        self.assertEqual(self.controller.error, "")
        self.assertIsNone(self.controller._auto_cell)
        self.assertGreater(self.controller._auto_cooldown_until, time.monotonic())
        self.assertGreater(
            self.controller._auto_cooldown_until,
            time.monotonic() + AUTO_CHART_COOLDOWN / 2,
        )

    def test_requests_selection_in_the_auto_task(self):
        self.assertTrue(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        message, operation = self.start.call_args.args
        self.assertIn("coverage", message)
        self.assertEqual(self.controller._auto_operation, "chart-auto-select")
        self.assertTrue(callable(operation))
        # No catalog or network work ran on this thread: the task owns it.
        self.assertEqual(self.owner.settings, {})

    def test_same_settled_view_is_checked_once(self):
        self.assertTrue(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        self.assertFalse(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        self.start.assert_called_once()

    def test_stationary_view_fills_missing_cells_in_sequence(self):
        # A coarse chart covers the view cold; the detailed southern cell is
        # only worth fetching once the coarse one is on screen.
        overview = cell("US2COAST", 700000, (46.2, -125.0, 47.5, -123.0))
        south = cell("US3WA1CE", 180000, (46.6, -124.3, 46.82, -123.8))
        catalog = FakeCatalog([overview, south])
        self.controller.catalog = catalog
        # The controller persists installs into the configured chart list, so
        # the settings store must already hold it, as the real one does.
        self.owner.settings["vectorCharts"] = []

        def load(path, palette):
            return (
                SimpleNamespace(ctype="vector", path=path, metadata={}, enabled=True),
                Mock(),
            )

        self.controller._auto_chart = load
        task = SynchronousAutoTask(self.controller)
        self.controller.auto_task = task

        # Each settled demand after an install offers the same stationary view
        # again: the next missing cell is selected until coverage is complete.
        self.assertTrue(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        self.assertTrue(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        self.assertEqual([c.name for c in catalog.downloads], ["US2COAST", "US3WA1CE"])
        self.assertEqual(self.controller.auto_downloads, 2)
        self.assertEqual(
            [entry["path"] for entry in self.owner.settings["vectorCharts"]],
            [
                "/data/enc/US2COAST/US2COAST.000",
                "/data/enc/US3WA1CE/US3WA1CE.000",
            ],
        )
        # Full coverage: the settled view is checked once more, finds nothing,
        # and does not download again or re-check on a later demand.
        self.assertTrue(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        self.assertIn("cover", task.message)
        runs = task.runs
        self.assertFalse(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        self.assertEqual(task.runs, runs)
        self.assertEqual(len(catalog.downloads), 2)

    def test_pictured_columbia_view_requests_every_candidate(self):
        # The live camera at 46.19609,-123.91362 z12.2591, 22.1 m/px: the
        # 1:180k chart is installed and enabled, and three 1:45k cells outrank
        # it.  Without panning, the same settled view must request all three.
        self.owner.settings["vectorCharts"] = [
            {"path": "/data/enc/US3WA1CE/US3WA1CE.000", "enabled": True}
        ]
        installed = cell("US3WA1CE", 180000, (46.05, -124.20, 46.35, -123.60))
        west = cell("US4OR1ND", 45000, (46.10, -124.15, 46.20, -124.02))
        centre = cell("US4OR1NE", 45000, (46.10, -124.02, 46.20, -123.90))
        north = cell("US4WA1CF", 45000, (46.20, -124.02, 46.30, -123.90))
        catalog = FakeCatalog([installed, west, centre, north])
        self.controller.catalog = catalog
        self.controller._auto_chart = lambda path, palette: (
            SimpleNamespace(ctype="vector", path=path, metadata={}, enabled=True),
            Mock(),
        )
        self.controller.auto_task = SynchronousAutoTask(self.controller)
        ready = []
        self.controller.autoChartReady.connect(lambda: ready.append(1))

        for _ in range(3):
            self.assertTrue(
                self.controller.download_missing_chart(
                    COLUMBIA_VIEW, COLUMBIA_METERS_PER_PIXEL
                )
            )
        self.assertEqual(
            [c.name for c in catalog.downloads],
            ["US4OR1ND", "US4OR1NE", "US4WA1CF"],
        )
        self.assertEqual(self.controller.auto_downloads, 3)
        self.assertEqual(len(ready), 3)

    def test_a_new_view_is_checked_again(self):
        self.assertTrue(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        moved = (VIEW[0] + 0.4, VIEW[1], VIEW[2] + 0.4, VIEW[3])
        self.assertTrue(self.controller.download_missing_chart(moved, METERS_PER_PIXEL))
        self.assertEqual(self.start.call_count, 2)

    def test_sources_split_disabled_entries(self):
        self.owner.settings["vectorCharts"] = [
            {"path": "/data/enc/US2COARS/US2COARS.000", "enabled": False},
            {"path": "/data/enc/US3DETAI/US3DETAI.000", "enabled": True},
        ]
        installed, enabled = self.controller._chart_sources()
        self.assertEqual(installed, frozenset({"US2COARS", "US3DETAI"}))
        self.assertEqual(enabled, frozenset({"US3DETAI"}))

    def test_selection_reports_an_existing_exchange_set(self):
        catalog = FakeCatalog(
            [coarse(), detail()], existing="/data/enc/US3DETAI/US3DETAI.000"
        )
        self.controller.catalog = catalog
        selected, existing = self.controller._auto_select(
            VIEW, METERS_PER_PIXEL, frozenset(), frozenset()
        )
        self.assertEqual(selected.name, "US3DETAI")
        self.assertEqual(existing, "/data/enc/US3DETAI/US3DETAI.000")

    def test_selection_reports_no_existing_exchange_set(self):
        self.controller.catalog = FakeCatalog([coarse(), detail()])
        selected, existing = self.controller._auto_select(
            VIEW, METERS_PER_PIXEL, frozenset(), frozenset()
        )
        self.assertEqual(selected.name, "US3DETAI")
        self.assertIsNone(existing)

    def test_existing_exchange_set_is_registered_not_downloaded(self):
        self.controller.catalog = FakeCatalog([detail()])
        self.controller._auto_chart = Mock(return_value=("chart", "settings"))
        self.controller._auto_selected((detail(), "/data/enc/US3DETAI/US3DETAI.000"))
        self.start.assert_called_once()
        self.start.call_args.args[1](None)
        self.controller._auto_chart.assert_called_once_with(
            "/data/enc/US3DETAI/US3DETAI.000", "day"
        )
        self.assertEqual(self.controller.catalog.downloads, [])

    def test_missing_exchange_set_is_downloaded(self):
        self.controller.catalog = FakeCatalog([detail()])
        self.controller._auto_chart = Mock(return_value=("chart", "settings"))
        self.controller._auto_selected((detail(), None))
        self.start.call_args.args[1](None)
        self.assertEqual(len(self.controller.catalog.downloads), 1)
        self.controller._auto_chart.assert_called_once_with(
            "/data/enc/US3DETAI/US3DETAI.000", "day"
        )
        self.assertEqual(self.controller._auto_cell, "US3DETAI")

    def test_skips_cell_already_attempted(self):
        self.controller._auto_attempts.add("US3DETAI")
        self.controller._auto_selected((detail(), None))
        self.start.assert_not_called()
        self.assertIsNone(self.controller._auto_cell)

    def test_failed_cell_gives_way_to_the_next_candidate_after_cooldown(self):
        # A cell that failed is not retried forever: the next settled demand
        # after the cooldown offers the next missing candidate instead.
        overview = cell("US2COAST", 700000, (46.2, -125.0, 47.5, -123.0))
        south = cell("US3WA1CE", 180000, (46.6, -124.3, 46.82, -123.8))
        catalog = FakeCatalog([overview, south], fail={"US2COAST"})
        self.controller.catalog = catalog
        self.owner.settings["vectorCharts"] = []
        self.controller._auto_chart = lambda path, palette: (
            SimpleNamespace(ctype="vector", path=path, metadata={}, enabled=True),
            Mock(),
        )
        task = SynchronousAutoTask(self.controller)
        self.controller.auto_task = task

        self.assertTrue(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        self.assertIn("US2COAST", self.controller._auto_attempts)
        self.assertGreater(self.controller._auto_cooldown_until, time.monotonic())

        self.controller._auto_cooldown_until = 0.0
        self.assertTrue(self.controller.download_missing_chart(VIEW, METERS_PER_PIXEL))
        self.assertEqual([c.name for c in catalog.downloads], ["US3WA1CE"])
        self.assertEqual(
            [entry["path"] for entry in self.owner.settings["vectorCharts"]],
            ["/data/enc/US3WA1CE/US3WA1CE.000"],
        )

    def test_auto_selection_uses_a_cached_catalog_without_refreshing(self):
        self.controller.catalog = FakeCatalog([detail()])
        self.controller._auto_select(VIEW, METERS_PER_PIXEL, frozenset(), frozenset())
        self.assertEqual(self.controller.catalog.loads, [False])

    def test_view_key_centres_an_antimeridian_view_on_the_line(self):
        crossing = self.controller._auto_view_key((-2, 179, 2, -179), 1000)
        self.assertAlmostEqual(abs(crossing[1]), 180.0, delta=0.01)
        gulf = self.controller._auto_view_key((-2, -1, 2, 1), 1000)
        self.assertNotEqual(crossing, gulf)
        east = self.controller._auto_view_key((0, 170, 10, -170), 1000)
        self.assertAlmostEqual(abs(east[1]), 180.0, delta=0.01)

    def test_a_suppressed_install_does_not_count_or_announce(self):
        ready = []
        self.controller.autoChartReady.connect(lambda: ready.append(1))
        self.controller._install_chart = Mock(return_value=False)
        self.controller._auto_installed((Mock(), Mock()))
        self.assertEqual(self.controller.auto_downloads, 0)
        self.assertEqual(ready, [])
        self.assertIn("disabled", self.controller.auto_task.message)
        self.assertIsNone(self.controller._auto_cell)

    def test_no_download_without_a_candidate(self):
        self.controller._auto_selected(None)
        self.start.assert_not_called()
        self.assertEqual(self.owner.settings, {})

    def test_selection_does_not_persist_settings(self):
        self.controller.catalog = FakeCatalog([detail()], existing="/tmp/US3DETAI.000")
        self.controller._auto_selected((detail(), "/tmp/US3DETAI.000"))
        self.assertEqual(self.owner.settings, {})

    def test_install_emits_ready_after_registration(self):
        order = []

        def install(*args, **kwargs):
            order.append("install")
            return True

        self.controller._install_chart = Mock(side_effect=install)
        self.controller.autoChartReady.connect(lambda: order.append("ready"))
        self.controller._auto_installed((Mock(), Mock()))
        self.assertEqual(order, ["install", "ready"])
        self.assertEqual(self.controller.auto_downloads, 1)
        self.assertIsNone(self.controller._auto_cell)
        self.assertIs(
            self.controller._install_chart.call_args.kwargs["task"],
            self.controller.auto_task,
        )
        self.assertIsNone(self.controller._install_chart.call_args.kwargs["enabled"])

    def test_ready_event_reaches_a_stationary_viewport(self):
        viewport = FakeViewport()
        viewport.attach_data_controller(self.controller)
        self.controller._install_chart = Mock()
        self.controller._auto_installed((Mock(), Mock()))
        # The camera has not moved, so both layers are asked directly.
        self.assertEqual(viewport.calls, ["auto-chart"])
        self.assertTrue(viewport._chart_force_replace)
        self.assertTrue(viewport._chart_scene_replace)

    def test_install_keeps_a_disabled_entry_disabled(self):
        path = "/data/enc/US3DETAI/US3DETAI.000"
        self.owner.settings["vectorCharts"] = [
            {"path": path, "metadata": {}, "enabled": False}
        ]
        chart = Mock(ctype="vector", path=path, metadata={}, enabled=True)
        settings = Mock()
        drawable = self.controller._install_chart(
            (chart, settings), enabled=None, task=self.controller.auto_task
        )
        self.assertFalse(drawable)
        self.assertFalse(self.owner.settings["vectorCharts"][0]["enabled"])
        self.assertFalse(chart.enabled)
        settings.attach.assert_called_once_with(self.owner.settings)

    def test_install_preserves_an_on_disk_only_legacy_entry(self):
        """An automatic install keeps the user's file whole, not just memory.

        The automatic install loads only the entries this process knows; a
        chart a predecessor left in settings.json is on disk but not in that
        list.  Saving the install must not shorten the file: the viewport's
        ENC catalog reads the file, so a dropped entry is a cell that stopped
        being drawn.
        """
        legacy_path = "/data/enc/US5LEGACY/US5LEGACY.000"
        known_path = "/data/enc/US2OLD/US2OLD.000"
        installed_path = "/data/enc/US3DETAI/US3DETAI.000"
        legacy = {
            "path": legacy_path,
            "metadata": {"name": "US5LEGACY"},
            "enabled": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "settings.json").write_text(
                json.dumps({"vectorCharts": [legacy]})
            )
            # Memory holds only the entry this install is writing.
            self.owner.settings["vectorCharts"] = [
                {"path": known_path, "metadata": {}, "enabled": True}
            ]
            chart = Mock(ctype="vector", path=installed_path, metadata={}, enabled=True)
            settings = Mock()
            with patch("sailworks.qt.datacontroller.DATA_DIR", str(data_dir)):
                self.controller._install_chart(
                    (chart, settings), enabled=True, task=self.controller.auto_task
                )
            # The settings store persists the merged list; the file is what the
            # viewport's ENC catalog reads afterwards.
            (data_dir / "settings.json").write_text(
                json.dumps({"vectorCharts": self.owner.settings["vectorCharts"]})
            )
            saved = json.loads((data_dir / "settings.json").read_text())
            self.assertEqual(
                sorted(entry["path"] for entry in saved["vectorCharts"]),
                sorted([legacy_path, known_path, installed_path]),
            )
            self.assertEqual(
                set(viewportenc.read_manifest(data_dir).enabled),
                {(legacy_path, True), (known_path, True), (installed_path, True)},
            )

    def test_ready_request_forces_both_layers_at_an_unchanged_camera(self):
        viewport = FakeViewport()
        self.assertTrue(auto_chart_ready_request(viewport))
        self.assertTrue(viewport._chart_force_replace)
        self.assertTrue(viewport._chart_scene_replace)
        self.assertEqual(viewport.calls, ["auto-chart"])


class TestAutoChartDemandGate(unittest.TestCase):
    def setUp(self):
        widget_class = _viewport_widget_class()
        self.downloads = []
        self.widget = SimpleNamespace(
            _chart_camera_changed=time.perf_counter(),
            _camera=FakeCamera(),
            data_controller=SimpleNamespace(
                download_missing_chart=lambda bounds, meters: self.downloads.append(
                    (bounds, meters)
                )
            ),
        )
        self.widget._auto_chart_demand = MethodType(
            widget_class._auto_chart_demand, self.widget
        )

    def test_preview_and_interactive_causes_start_no_download(self):
        for cause in ("preview", "camera", "partial", "interactive"):
            self.widget._auto_chart_demand(cause)
        self.assertEqual(self.downloads, [])

    def test_a_still_moving_view_is_not_offered(self):
        self.widget._chart_camera_changed = time.perf_counter()
        self.widget._auto_chart_demand("settle")
        self.assertEqual(self.downloads, [])

    def test_a_settled_startup_view_is_offered(self):
        # The camera has not moved since the viewport was created, so the view
        # is settled once the idle window has passed.
        self.widget._chart_camera_changed = time.perf_counter() - 2.0
        self.widget._auto_chart_demand("settle")
        self.assertEqual(len(self.downloads), 1)
        bounds, _ = auto_chart_view(FakeCamera())
        self.assertAlmostEqual(self.downloads[0][0][0], bounds[0])


class TestControllerBinding(unittest.TestCase):
    def _viewport(self, calls):
        widget_class = _viewport_widget_class()
        widget = SimpleNamespace(
            _request_chart=lambda cause="settle": calls.append(cause)
        )
        for name in (
            "attach_data_controller",
            "_detach_data_controller",
            "_auto_chart_ready",
        ):
            setattr(widget, name, MethodType(getattr(widget_class, name), widget))
        return widget

    def test_reattach_and_detach_drop_the_previous_ready_connection(self):
        calls = []
        widget = self._viewport(calls)
        first = DataController(FakeOwner())
        second = DataController(FakeOwner())
        widget.attach_data_controller(first)
        widget.attach_data_controller(second)

        first.autoChartReady.emit()
        self.assertEqual(calls, [])
        second.autoChartReady.emit()
        self.assertEqual(calls, ["auto-chart"])

        widget._detach_data_controller()
        second.autoChartReady.emit()
        self.assertEqual(calls, ["auto-chart"])
        self.assertIsNone(widget.data_controller)


if __name__ == "__main__":
    unittest.main()
