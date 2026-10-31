"""Source clocks stay separate from the selected scrubber; expansion slides up."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from types import SimpleNamespace
from unittest.mock import Mock

from gweatherrouting.core.timecontrol import TimeControl


@unittest.skipUnless(find_spec("PySide6"), "Requires PySide6")
class TestForecastSourceOverview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QWidget

        from gweatherrouting.qt.forecasttimeline import ForecastTimeline

        class Changes(QObject):
            changed = Signal()

        self.changes = Changes()
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.stamp = self.start.timestamp()
        self.active = self.forecast("gfs", (0, 10800, 21600))
        self.other = self.forecast("ecmwf", (-3600, 3600, 43200))
        self.selection = SimpleNamespace(
            sources=lambda: (self.active,),
            choices=(self.active, self.other),
            changed=self.changes.changed,
        )
        self.marine = Mock()
        self.marine.ordered_datasets.return_value = ()
        self.marine.fields.fields = {}
        self.marine.fields.excluded = set()
        self.clock = TimeControl()
        self.clock.set_time(self.start)
        controller = SimpleNamespace(
            time_control=self.clock,
            core=SimpleNamespace(marine_manager=self.marine),
            display_forecast=self.selection,
            scheduler=Mock(),
        )
        self.host = QWidget()
        self.host.resize(900, 700)
        self.timeline = ForecastTimeline(controller, self.host)

        # Same bottom-pinned layout contract as ViewportMap._layout_timeline.
        def layout():
            height = self.timeline.sizeHint().height()
            self.timeline.setGeometry(
                0, self.host.height() - height, self.host.width(), height
            )

        self.layout = layout
        self.timeline.expansion_changed.connect(layout)
        self.layout()
        self.host.show()
        self.app.processEvents()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.timeline.dispose)

    def forecast(self, key, offsets):
        times = tuple(self.stamp + offset for offset in offsets)
        return SimpleNamespace(
            key=key,
            label=f"{key} run 2026-01-01",
            valid_times=times,
            first_valid=min(times),
            last_valid=max(times),
        )

    def test_all_models_have_actual_dots_on_shared_axis_without_changing_scrubber(self):
        from PySide6.QtGui import QColor, QImage

        plot = self.timeline.source_overview.plot
        self.assertEqual([row[0] for row in plot.rows], ["gfs", "ecmwf"])
        self.assertEqual([row[3] for row in plot.rows], [True, False])
        self.assertEqual(plot.rows[1][2], self.other.valid_times)
        self.assertEqual(self.timeline.bounds, (self.stamp, self.stamp + 21600))
        self.assertEqual(self.timeline.day_strip.valid_times, self.active.valid_times)
        self.assertEqual(self.timeline.slider.maximum(), 21600)
        plot.resize(800, plot.height())
        other_row = next(
            index for index, item in enumerate(plot.tree_rows)
            if item[0] == "leaf" and item[2][0] == "ecmwf"
        )
        points = plot.dot_points(self.other.valid_times, other_row)
        self.assertEqual(len(points), 3)
        self.assertAlmostEqual(
            (points[1].x() - points[0].x()) / (points[2].x() - points[0].x()),
            7200 / 46800,
            delta=0.001,
        )
        # Exercise the real painter, not just metadata storage.
        image = QImage(plot.size(), QImage.Format.Format_ARGB32)
        image.fill(0)
        plot.render(image)
        active_row = next(
            index for index, item in enumerate(plot.tree_rows)
            if item[0] == "leaf" and item[2][0] == "gfs"
        )
        point = plot.dot_points(self.active.valid_times, active_row)[1]
        self.assertEqual(
            image.pixelColor(round(point.x()), round(point.y())),
            QColor("#777777"),
        )

    def test_out_of_range_source_dots_are_clipped_before_the_label_column(self):
        from PySide6.QtGui import QColor, QImage

        plot = self.timeline.source_overview.plot
        plot.set_rows(
            (("old", "Old run", (self.stamp - 3600,), False, "Weather models", "GFS"),),
            (self.stamp, self.stamp + 21600),
        )
        plot.resize(800, plot.height())
        leaf = next(
            index for index, item in enumerate(plot.tree_rows) if item[0] == "leaf"
        )
        point = plot.dot_points((self.stamp - 3600,), leaf)[0]
        self.assertLess(point.x(), plot._plot_left())
        image = QImage(plot.size(), QImage.Format.Format_ARGB32)
        image.fill(0)
        plot.render(image)
        self.assertNotEqual(
            image.pixelColor(round(point.x()), round(point.y())), QColor("#777777")
        )

    def test_collapsed_resize_reserves_track_scrollbar_width(self):
        drawer = self.timeline.source_overview
        drawer.plot.setFixedHeight(400)
        for width in (1000, 1200):
            self.host.resize(width, 700)
            self.layout()
            self.app.processEvents()
            self.timeline._sync_time_axis_geometry()
            self.assertEqual(
                self.timeline.day_strip.axis_right_margin,
                drawer.scroll.verticalScrollBar().sizeHint().width(),
            )
            self.assertTrue(drawer.isHidden())

    def test_model_type_tree_collapses_and_dots_share_scrubber_axis(self):
        from PySide6.QtCore import QPointF

        plot = self.timeline.source_overview.plot
        self.assertTrue(any(item[0] == "subgroup" for item in plot.tree_rows))
        root = next(item for item in plot.tree_rows if item[0] == "group")
        subgroup = next(item for item in plot.tree_rows if item[0] == "subgroup")
        expected = tuple(sorted(set(self.active.valid_times + self.other.valid_times)))
        self.assertEqual(root[2], expected)
        self.assertEqual(subgroup[2], expected)
        for item in (root, subgroup):
            row_index = plot.tree_rows.index(item)
            dots = plot.dot_points(item[2], row_index)
            self.assertEqual(len(dots), len(expected))
            self.assertEqual(
                [round(point.x()) for point in dots],
                [round(point.x()) for point in plot.dot_points(expected, row_index)],
            )
        other_point = plot.dot_points(self.other.valid_times, next(
            index for index, item in enumerate(plot.tree_rows)
            if item[0] == "leaf" and item[2][0] == "ecmwf"
        ))[1]
        scrubber_x = self.timeline.day_strip.axis_left_margin + round(
            (self.other.valid_times[1] - self.timeline.bounds[0])
            / (self.timeline.bounds[1] - self.timeline.bounds[0])
            * (
                self.timeline.day_strip.width()
                - self.timeline.day_strip.axis_left_margin
                - self.timeline.day_strip.axis_right_margin
                - 1
            )
        )
        self.assertEqual(round(other_point.x()), scrubber_x)
        subgroup_index = next(
            index for index, item in enumerate(plot.tree_rows) if item[0] == "subgroup"
        )
        plot.mousePressEvent(type("MouseEvent", (), {
            "position": lambda _self: QPointF(30, subgroup_index * plot.ROW_HEIGHT + 2),
            "accept": lambda _self: None,
        })())
        self.assertFalse(any(item[0] == "leaf" for item in plot.tree_rows))

    def test_day_grid_matches_scrubber_cells_and_tooltip_is_not_model_list(self):
        plot = self.timeline.source_overview.plot
        strip = self.timeline.day_strip
        plot.resize(strip.width(), plot.height())
        strip._cell_rects = strip._day_cells(*self.timeline.bounds)
        expected = tuple(rect.x() for rect, _ in strip._cell_rects[1:])
        self.assertEqual(plot._day_tick_positions(), expected)
        self.assertEqual(plot.toolTip(), "")

    def test_now_and_selected_time_cursors_span_the_track_rows(self):
        from PySide6.QtGui import QImage

        plot = self.timeline.source_overview.plot
        selected = self.stamp + 3600
        now = self.stamp + 18000
        plot.set_cursors(now, selected)
        image = QImage(plot.size(), QImage.Format.Format_ARGB32)
        image.fill(0)
        plot.render(image)
        y = plot.ROW_HEIGHT // 2
        self.assertEqual(
            image.pixelColor(round(plot._time_to_x(selected)), y).name(), "#0066cc"
        )
        self.assertEqual(
            image.pixelColor(round(plot._time_to_x(now)), y).name(), "#e6c229"
        )

    def test_marine_rows_use_samples_and_field_times_not_requested_bounds(self):
        from gweatherrouting.core.marine.models import Dataset, Sample, Series, Station

        def series(code, offsets):
            return Series(
                code,
                "m",
                tuple(
                    Sample(self.start + timedelta(seconds=offset), value, qc)
                    for offset, value, qc in offsets
                ),
            )

        dataset = Dataset(
            "CHS",
            Station("one", "one", "Harbour", 48, -123, ("water_level",)),
            self.start,
            self.start - timedelta(days=1),
            self.start + timedelta(days=3),
            (
                series(
                    "water_level",
                    ((0, 1, "1"), (1800, None, "1"), (3600, 2, "2"), (5400, 3, "4")),
                ),
                series("current_speed", ((0, 1, "1"), (3600, 2, "2"))),
                series("current_direction", ((0, 90, "1"), (3600, 90, "4"))),
            ),
            "{}",
            "[]",
            "CHS",
            "https://example.com",
        )
        self.marine.ordered_datasets.return_value = (dataset,)
        self.marine.is_enabled.return_value = False
        self.marine.fields.fields = {
            "field": SimpleNamespace(
                times=(self.stamp - 7200, self.stamp + 7200),
                manifest=lambda: {"provider": "CMEMS", "model": "Global"},
            )
        }
        self.timeline.refresh()
        rows = self.timeline.source_overview.plot.rows
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[2][2], (self.stamp, self.stamp + 3600))
        self.assertEqual(rows[3][2], (self.stamp,))
        self.assertFalse(rows[2][3])
        self.assertEqual(rows[4][2], (self.stamp - 7200, self.stamp + 7200))
        marine_root = next(
            item for item in self.timeline.source_overview.plot.tree_rows
            if item[0] == "group" and item[1] == "Tides"
        )
        self.assertEqual(marine_root[2], rows[2][2])
        self.assertTrue(all("wave" not in row[1].lower() for row in rows))
        self.assertEqual(self.timeline.bounds, (self.stamp, self.stamp + 21600))
        self.assertEqual(self.timeline.day_strip.valid_times, self.active.valid_times)

    def test_catalog_updates_even_when_selected_range_does_not_change(self):
        plot = self.timeline.source_overview.plot
        original = plot.rows
        self.timeline.refresh()
        self.assertIs(plot.rows, original)
        self.selection.choices = (self.active, self.forecast("new model", (0, 7200)))
        self.changes.changed.emit()
        self.assertEqual(plot.rows[1][0], "new model")
        self.assertEqual(self.timeline.day_strip.valid_times, self.active.valid_times)
        self.selection.choices = ()
        self.selection.sources = lambda: ()
        self.changes.changed.emit()
        self.assertEqual(plot.rows, ())
        self.assertIsNone(plot.bounds)
        self.assertIsNone(self.timeline.bounds)

    def test_source_overview_failure_does_not_hide_selected_model_timeline(self):
        self.marine.ordered_datasets.side_effect = RuntimeError("catalog unavailable")
        self.timeline.refresh()
        self.assertEqual(self.timeline.bounds, (self.stamp, self.stamp + 21600))
        self.assertEqual(self.timeline.day_strip.valid_times, self.active.valid_times)
        self.assertTrue(self.timeline.slider.isEnabled())

    def test_drawer_animates_upward_reverses_and_finishes_collapsed(self):
        from PySide6.QtCore import QAbstractAnimation

        drawer = self.timeline.source_overview
        self.assertLess(
            self.timeline.layout().indexOf(self.timeline.day_strip),
            self.timeline.layout().indexOf(drawer),
        )
        animation = drawer.animation
        base = self.timeline.geometry()
        self.assertTrue(drawer.isHidden())
        self.timeline.overview_button.click()
        self.assertTrue(drawer.expanded)
        self.assertEqual(animation.state(), QAbstractAnimation.State.Running)
        animation.pause()
        animation.setCurrentTime(animation.duration() // 2)
        self.app.processEvents()
        halfway = drawer.maximumHeight()
        middle = self.timeline.geometry()
        self.assertGreater(halfway, 0)
        self.assertLess(halfway, drawer.target_height())
        self.assertLess(middle.top(), base.top())
        self.assertEqual(middle.bottom(), base.bottom())
        self.timeline.overview_button.click()
        self.assertEqual(animation.startValue(), halfway)
        animation.setCurrentTime(animation.duration())
        self.app.processEvents()
        self.assertTrue(drawer.isHidden())
        self.assertFalse(drawer.expanded)
        self.assertEqual(drawer.maximumHeight(), 0)
        self.assertEqual(self.timeline.geometry(), base)
        self.timeline.overview_button.click()
        animation.setCurrentTime(animation.duration())
        self.app.processEvents()
        self.assertFalse(drawer.isHidden())
        self.assertEqual(drawer.height(), drawer.target_height())
        self.assertEqual(self.timeline.geometry().bottom(), base.bottom())
        self.assertIn("Collapse", self.timeline.overview_button.accessibleName())
        self.assertEqual(self.timeline.day_strip.valid_times, self.active.valid_times)
        self.timeline.dispose()
        self.assertEqual(animation.state(), QAbstractAnimation.State.Stopped)
