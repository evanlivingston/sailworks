"""Exercise the public log transport and production bounded timer methods."""

import os
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, QTimer, Signal, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLabel,
    QPushButton,
    QSlider,
    QToolButton,
    QWidget,
)

from gweatherrouting.core.datasource import NMEADataPacket
from gweatherrouting.core.nmeasession import (
    NMEALog,
    NMEASession,
    NMEATrack,
    NumericFieldCollector,
)
from gweatherrouting.core.utils import EventDispatcher
from gweatherrouting.qt.controller import SessionController
from gweatherrouting.qt.logscrubber import LogScrubber
from gweatherrouting.qt.plots import LogPlots
from gweatherrouting.qt.window import PreviewWindow
from test.test_polarperformance import water, wind


class ScrubController(QObject):
    changed = Signal()
    preview_log_position = SessionController.preview_log_position
    seek_log_position = SessionController.seek_log_position
    _seek_tick = SessionController._seek_tick
    _changed = SessionController._changed
    _tick = SessionController._tick

    def __init__(self, directory):
        super().__init__()
        self.closed = False
        self.log_progress = None
        self.last_tick = time.monotonic()
        self.session = NMEASession(EventDispatcher(), directory)
        self.session.connect("changed", self._changed)
        self.seek_timer = QTimer(self)
        self.seek_timer.setInterval(1)
        self.seek_timer.timeout.connect(self._seek_tick)
        packet = NMEADataPacket(water())
        log = NMEALog("race.log", tuple((i / 10, packet) for i in range(3100)), 0, True)
        self.session.finish_load(self.session.begin_load(), log)


@pytest.fixture
def transport(tmp_path):
    app = QApplication.instance() or QApplication([])
    controller = ScrubController(tmp_path)
    widget = LogScrubber(controller)
    yield app, controller, widget
    controller.closed = True
    controller.seek_timer.stop()
    controller.session.close()
    widget.close()


def drain(app, controller):
    deadline = time.monotonic() + 5
    while controller.session.seeking and time.monotonic() < deadline:
        app.processEvents()
    assert not controller.session.seeking
    assert not controller.seek_timer.isActive()


def test_drag_previews_without_advancing_and_release_commits_in_batches(transport):
    app, controller, widget = transport
    session = controller.session
    session.set_playing(True)
    controller._tick()
    cursor = session.cursor
    widget.slider.setSliderDown(True)
    widget.slider.setValue(widget.slider.maximum() // 2)
    target = session.display_position
    assert target > 100
    controller._tick()
    assert session.cursor == cursor
    assert session.playing
    widget.refresh()
    assert widget.slider.value() == widget.slider.maximum() // 2
    widget.slider.setSliderDown(False)
    assert session.seeking
    assert session.cursor == 0  # release never synchronously replays the log
    controller._seek_tick()
    assert session.cursor == 1000
    assert session.seeking
    drain(app, controller)
    assert session.position == target
    assert session.playing
    assert session.cursor == sum(offset <= target for offset, _ in session.log.entries)
    widget.slider.setValue(widget.slider.maximum() // 4)
    assert session.cursor == 0  # backwards also resets the consumers
    drain(app, controller)
    assert session.position < target
    assert session.playing


def test_keyboard_seek_and_programmatic_sync_do_not_feed_back(transport):
    app, controller, widget = transport
    session = controller.session
    seek = Mock(wraps=controller.seek_log_position)
    controller.seek_log_position = seek
    widget.refresh()
    seek.assert_not_called()
    QTest.keyClick(widget.slider, Qt.Key.Key_End)
    seek.assert_called_once()
    assert session.seeking
    drain(app, controller)
    assert session.cursor == len(session.log.entries)
    assert not session.playing
    QTest.keyClick(widget.slider, Qt.Key.Key_Left)
    drain(app, controller)
    assert session.position < session.log.duration
    assert not session.playing


@pytest.mark.parametrize("cancel", ["source", "load", "close"])
def test_lifecycle_changes_cancel_timer_and_drag(transport, cancel):
    app, controller, widget = transport
    controller.seek_log_position(200)
    controller._seek_tick()
    widget.slider.setSliderDown(True)
    widget.slider.setValue(widget.slider.maximum())
    if cancel == "source":
        controller.session.select_source("live")
    elif cancel == "load":
        controller.session.begin_load()
    else:
        controller.closed = True
        controller.session.close()
        controller.seek_timer.stop()
        controller.changed.emit()
    cursor = controller.session.cursor
    widget.slider.setSliderDown(False)
    app.processEvents()
    assert not controller.seek_timer.isActive()
    assert not controller.session.seeking
    assert controller.session.preview_position is None
    assert controller.session.cursor == cursor


def test_preview_is_forwarded_to_existing_maps_and_plot_cursor(transport):
    _, controller, widget = transport
    session = controller.session
    window = SimpleNamespace(
        session=session, plots=Mock(), map=Mock(), log_map=Mock(), log_time=Mock()
    )
    controller.changed.connect(lambda: PreviewWindow._refresh_log_position(window))
    widget.slider.setSliderDown(True)
    widget.slider.setValue(widget.slider.maximum() // 2)
    target = session.display_position
    assert session.position == 0
    window.plots.set_position.assert_called_with(target)
    window.map.set_position.assert_called_with(target)
    window.log_map.set_position.assert_called_with(target)


def plot_points():
    return tuple(
        SimpleNamespace(time=datetime(2026, 1, 1, 12, 0, second))
        for second in (10, 20, 30)
    )


def test_plain_drag_scrubs_and_shift_drag_selects_a_range(transport):
    _, controller, slider = transport
    # Deliberately parent to a container without a controller attribute.
    container = QWidget()
    preview = Mock(wraps=controller.preview_log_position)
    seek = Mock(wraps=controller.seek_log_position)
    plot = LogPlots(container, preview=preview, seek=seek)
    plot.set_points(plot_points(), (10, 20, 30), True)

    def event(x, key=None):
        return SimpleNamespace(button=1, key=key, inaxes=plot.axes[0], xdata=x)

    try:
        plot._scrub_press(event(12.5))
        assert controller.session.display_position == 12.5
        assert controller.session.cursor == 0
        slider.refresh()
        assert slider.slider.value() == round(slider.slider.maximum() * 12.5 / 309.9)
        plot._scrub_motion(event(26.25))
        preview.assert_called_with(26.25)
        seek.assert_not_called()
        plot._scrub_release(event(28.5))
        seek.assert_called_once_with(28.5)
        seek.reset_mock()
        plot._scrub_press(event(20, key="shift"))
        plot._scrub_motion(event(26.25, key="shift"))
        plot._scrub_release(event(28.5, key="shift"))
        assert plot.selection == (20, 28.5)
        seek.assert_not_called()
    finally:
        plot.close()
        container.close()


def test_hover_shows_nearest_sample_values_on_every_axis_without_scrubbing():
    preview, seek = Mock(), Mock()
    plot = LogPlots(preview=preview, seek=seek)
    points = (
        SimpleNamespace(speed=0, tws=4, hdg=5, awa=10, twa=20, depth=None),
        SimpleNamespace(speed=12, tws=14, hdg=15, awa=30, twa=40, depth=None),
        SimpleNamespace(speed=22, tws=24, hdg=25, awa=50, twa=60, depth=None),
    )
    plot.set_series("depth", True)
    plot.set_points(points, (10, 20, 30), False)
    try:
        plot._hover_motion(SimpleNamespace(inaxes=plot.axes[0], xdata=18))
        assert plot._hover_index == 1
        assert all(cursor.get_visible() for cursor in plot.hover_cursors)
        assert all(cursor.get_xdata() == [20, 20] for cursor in plot.hover_cursors)
        labels = [artist.get_text() for artist in plot.hover_labels]
        assert "Speed: 12" in labels[0]
        assert "TWS: 14" in labels[1]
        assert "Depth: —" in labels[2]
        assert "HDG: 15" in labels[3]

        plot._hover_motion(SimpleNamespace(inaxes=plot.axes[0], xdata=10))
        assert "Speed: 0" in plot.hover_labels[0].get_text()
        plot._hover_motion(SimpleNamespace(inaxes=None, xdata=10))
        assert not any(cursor.get_visible() for cursor in plot.hover_cursors)
        plot._hover_motion(SimpleNamespace(inaxes=plot.axes[0], xdata=20))
        plot._selecting = True
        plot._hover_motion(SimpleNamespace(inaxes=plot.axes[0], xdata=30))
        assert not any(cursor.get_visible() for cursor in plot.hover_cursors)
        plot._selecting = False
        plot._scrubbing = True
        plot._hover_motion(SimpleNamespace(inaxes=plot.axes[0], xdata=30))
        assert not any(cursor.get_visible() for cursor in plot.hover_cursors)
        preview.assert_not_called()
        seek.assert_not_called()
    finally:
        plot.close()


def test_shift_drag_selects_range_and_delete_uses_range_callback(transport):
    delete_range = Mock(return_value=True)
    plot = LogPlots(delete_range=delete_range)
    plot.set_points(plot_points(), (10, 20, 30), True)
    plot.set_time_bounds(0, 30, reset=True)

    def event(x):
        return SimpleNamespace(button=1, key="shift", inaxes=plot.axes[0], xdata=x)

    try:
        plot._scrub_press(event(12))
        plot._scrub_motion(event(24))
        plot._scrub_release(event(24))
        assert plot.selection == (12, 24)
        assert plot.delete_selected_range()
        delete_range.assert_called_once_with(12, 24)
        assert plot.selection is None
    finally:
        plot.close()


def test_delete_selected_range_ripple_updates_loaded_session(transport, monkeypatch):
    _, controller, _ = transport
    session = controller.session
    before = session.log

    class ActionSignal:
        def connect(self, callback):
            self.callback = callback

        def emit(self, *args):
            self.callback(*args)

    class MenuAction:
        def __init__(self, text):
            self.text = text
            self.triggered = ActionSignal()
            self.checkable = False
            self.checked = False

        def setCheckable(self, checkable):
            self.checkable = checkable

        def setChecked(self, checked):
            self.checked = checked

        def trigger(self):
            self.triggered.emit(False)

    class Menu:
        def __init__(self, parent):
            self.actions = []

        def addAction(self, text, callback=None):
            action = MenuAction(text)
            if callback is not None:
                action.triggered.connect(callback)
            self.actions.append(action)
            return action

        def addSeparator(self):
            pass

        def addMenu(self, text):
            self.actions.append(MenuAction(text))
            return Menu(self)

        def exec(self, _position):
            self.actions[0].trigger()

    monkeypatch.setattr("gweatherrouting.qt.plots.QMenu", Menu)
    plot = LogPlots(delete_range=session.delete_log_range)
    plot.set_points(plot_points(), (10, 20, 30), True)
    plot.set_time_bounds(0, before.duration, reset=True)
    try:
        plot._scrub_press(
            SimpleNamespace(button=1, key="shift", inaxes=plot.axes[0], xdata=100)
        )
        plot._scrub_motion(
            SimpleNamespace(button=1, key="shift", inaxes=plot.axes[0], xdata=150)
        )
        plot._scrub_release(
            SimpleNamespace(button=1, key="shift", inaxes=plot.axes[0], xdata=150)
        )
        assert plot.selection == (100, 150)
        plot._show_range_menu()
        assert len(session.log.entries) < len(before.entries)
        assert session.log.duration == pytest.approx(before.duration - 50)
        assert session.log_edits_dirty
    finally:
        plot.close()


def test_controller_builds_large_log_trim_in_worker_then_commits_on_gui(transport):
    from gweatherrouting.qt.controller import SessionController

    app, scrub_controller, _ = transport

    class WorkerController(QObject):
        changed = Signal()
        log_edit_finished = Signal(object, object, object)
        delete_log_range = SessionController.delete_log_range
        _finish_log_range_delete = SessionController._finish_log_range_delete

        def __init__(self, session):
            super().__init__()
            self.session = session
            self.closed = False
            self.log_edit_busy = False
            self.log_edit_error = None
            self.seek_timer = QTimer(self)
            self.log_edit_finished.connect(
                self._finish_log_range_delete,
                Qt.ConnectionType.QueuedConnection,
            )

    controller = WorkerController(scrub_controller.session)
    original = controller.session.log
    try:
        assert controller.delete_log_range(100, 150)
        deadline = time.monotonic() + 5
        while controller.log_edit_busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        app.processEvents()
        assert not controller.log_edit_busy
        assert controller.log_edit_error is None
        assert controller.session.log is not original
        assert controller.session.log.duration == pytest.approx(original.duration - 50)
    finally:
        controller.seek_timer.stop()
        controller.deleteLater()


def test_wheel_zoom_and_scrollbar_pan_only_change_plot_viewport():
    preview, seek, viewport = Mock(), Mock(), Mock()
    plot = LogPlots(preview=preview, seek=seek, viewport_changed=viewport)
    plot.set_points(plot_points(), (10, 20, 30), True)
    plot.set_time_bounds(0, 30, reset=True)
    try:
        plot._zoom(SimpleNamespace(inaxes=plot.axes[0], xdata=15, step=1))
        assert plot.axes[0].get_xlim() == pytest.approx((3, 27))
        plot.set_viewport_fraction(1)
        assert plot.axes[0].get_xlim() == pytest.approx((6, 30))
        assert viewport.call_args.args[0] == pytest.approx(1)
        preview.assert_not_called()
        seek.assert_not_called()
    finally:
        plot.close()


def test_chart_context_menu_toggles_instrument_series():
    plot = LogPlots()
    try:
        menu = plot._build_context_menu()
        action = next(
            action
            for action in menu.actions()
            if action.text() == "Apparent wind speed"
        )
        assert action.isCheckable()
        assert not action.isChecked()
        action.trigger()
        assert plot.series["aws"]
        assert any(
            "AWS" in line.get_label() for axis in plot.axes for line in axis.lines
        )
        menu.close()
    finally:
        plot.close()


def test_chart_menu_discovers_numeric_fields_from_loaded_log():
    collector = NumericFieldCollector()
    for offset, sentence in ((0, water()), (1, wind())):
        collector.add(offset, NMEADataPacket(sentence))
    fields = collector.snapshot()
    keys = {field.key for field in fields}
    assert "VHW.water_speed_knots" in keys
    assert "MWV.wind_angle" in keys
    assert "MWV.wind_speed[kn]" in keys

    plot = LogPlots()
    plot.set_points((), (), True)
    plot.set_time_bounds(0, 1, reset=True)
    plot.set_logged_fields(fields, reset=True)
    try:
        menu = plot._build_context_menu()
        recorded = next(
            action.menu()
            for action in menu.actions()
            if action.text() == "Recorded log fields"
        )
        field = next(item for item in fields if item.label == "MWV · Wind speed (kn)")
        action = next(
            action for action in recorded.actions() if action.text() == field.label
        )
        action.trigger()
        assert field.key in plot.selected_logged_fields
        assert field.label in [axis.get_ylabel() for axis in plot.axes]
        plot._hover_motion(SimpleNamespace(inaxes=plot.axes[-1], xdata=0.8))
        assert f"{field.label}:" in plot.hover_labels[-1].get_text()
        menu.close()
    finally:
        plot.close()


@pytest.mark.parametrize("release_outside", [True, False])
def test_plot_release_without_x_commits_last_preview_and_resumes_playback(
    transport, release_outside
):
    app, controller, _ = transport
    session = controller.session
    session.set_playing(True)
    preview = Mock(wraps=controller.preview_log_position)
    seek = Mock(wraps=controller.seek_log_position)
    plot = LogPlots(preview=preview, seek=seek)
    plot.set_points(plot_points(), (10, 20, 30), True)
    try:
        plot._scrub_press(SimpleNamespace(button=1, inaxes=plot.axes[0], xdata=12.5))
        plot._scrub_motion(SimpleNamespace(button=1, inaxes=plot.axes[0], xdata=26.25))
        preview.assert_called_with(26.25)
        assert session.preview_position == 26.25
        session.advance(1)
        assert session.position == 0
        release = SimpleNamespace(
            button=1,
            inaxes=None if release_outside else plot.axes[0],
            xdata=None,
        )
        plot._scrub_motion(release)
        preview.assert_called_with(26.25)
        seek.assert_not_called()
        plot._scrub_release(release)
        seek.assert_called_once_with(26.25)
        assert not plot._scrubbing
        assert session.seeking
        drain(app, controller)
        assert session.preview_position is None
        assert session.position == 26.25
        assert session.playing
        cursor = session.cursor
        session.advance(1)
        assert session.position == 27.25
        assert session.cursor > cursor
        plot._scrub_release(release)
        seek.assert_called_once_with(26.25)
    finally:
        plot.close()


@pytest.mark.parametrize("invalid", ["outside", "no-x", "live", "right", "empty"])
def test_plot_does_not_scrub_without_valid_log_axis(transport, invalid, monkeypatch):
    preview, seek = Mock(), Mock()
    plot = LogPlots(preview=preview, seek=seek)
    plot.set_points(plot_points(), (10, 20, 30), invalid != "live")
    event = SimpleNamespace(button=1, inaxes=plot.axes[0], xdata=20)
    if invalid == "outside":
        event.inaxes = None
    elif invalid == "no-x":
        event.xdata = None
    elif invalid == "right":
        event.button = 3
        monkeypatch.setattr(LogPlots, "_show_range_menu", lambda _self: None)
    elif invalid == "empty":
        plot.set_points((), (), True)
    try:
        plot._scrub_press(event)
        plot._scrub_motion(event)
        plot._scrub_release(event)
        preview.assert_not_called()
        seek.assert_not_called()
    finally:
        plot.close()


def test_lazy_plot_factory_injects_callbacks_into_nested_canvas(transport):
    from gweatherrouting.qt.lazyplots import LazyLogPlots

    app, controller, _ = transport
    window = QWidget()
    window._interactive_plots = PreviewWindow._interactive_plots
    controller.early_weather = True
    lazy = PreviewWindow._build_plots(window, controller)
    assert isinstance(lazy, LazyLogPlots)
    lazy.set_points(plot_points(), (10, 20, 30), True)
    try:
        window.show()
        lazy.show()
        lazy.materialize()
        assert lazy.plot is not None
        event = SimpleNamespace(button=1, inaxes=lazy.plot.axes[0], xdata=20)
        lazy.plot._scrub_press(event)
        lazy.plot._scrub_release(event)
        assert controller.session.display_position == 20
        assert controller.session.seeking
        drain(app, controller)
        assert controller.session.position == 20
    finally:
        lazy.close()
        window.close()


@pytest.mark.parametrize("early_weather", [False, True])
def test_clean_nmea_panel_is_scrub_only_and_keeps_production_chart_scrubbing(
    transport, early_weather
):
    app, controller, _ = transport
    controller.early_weather = early_weather
    controller.connection_task = SimpleNamespace(busy=False)
    window = QWidget()
    window.controller = controller
    window.session = controller.session
    window.syncing = False
    for name in (
        "_source_changed",
        "show_log_library",
        "_track_source_changed",
        "_record",
    ):
        setattr(window, name, lambda *args: None)
    window._interactive_plots = PreviewWindow._interactive_plots
    window._refresh_data = Mock()
    window._refresh_log_position = Mock()
    window.cursor_status = QLabel()
    window.statusBar = lambda: Mock()
    window._connection_message = lambda: ""
    panel = PreviewWindow._nmea_panel(window)
    canvas = PreviewWindow._build_plots(window, controller)
    canvas.set_points(plot_points(), (10, 20, 30), True)
    try:
        window.show()
        canvas.show()
        if early_weather:
            canvas.materialize()
        plot = canvas.plot if early_weather else canvas
        PreviewWindow.refresh(window)
        assert not panel.findChildren(QSlider)
        assert not panel.findChildren(LogScrubber)
        assert not any(
            hasattr(window, name)
            for name in (
                "restart",
                "play",
                "stop",
                "speed",
                "speed_group",
                "progress",
                "status",
                "recording_file",
            )
        )
        texts = "\n".join(label.text() for label in panel.findChildren(QLabel))
        for noise in (
            "Paused",
            "sentences",
            "Partial EBL",
            "unsupported",
            "malformed",
            "corrupt",
            "Recordings folder",
            "Transport",
            "Playback speed",
        ):
            assert noise not in texts
        buttons = {button.text() for button in panel.findChildren(QPushButton)}
        assert buttons == {"Log library…"}
        assert panel.findChildren(QToolButton) == [window.record]
        assert window.record.isCheckable()
        assert window.record.isHidden()  # no recording control while replaying
        assert not hasattr(window, "file_info_button")
        assert window.track_source.accessibleName() == "GPS track source"
        assert not controller.session.playing
        plot._scrub_press(
            SimpleNamespace(button=1, key="shift", inaxes=plot.axes[0], xdata=12.5)
        )
        plot._scrub_motion(
            SimpleNamespace(button=1, key="shift", inaxes=plot.axes[0], xdata=26.25)
        )
        plot._scrub_release(SimpleNamespace(button=1, inaxes=plot.axes[0], xdata=28.5))
        assert plot.selection == (12.5, 28.5)
        assert controller.session.preview_position is None
        assert controller.session.position == 0
        assert not controller.session.playing
        PreviewWindow.refresh(window)
    finally:
        canvas.close()
        panel.close()
        window.close()


def test_source_dropdown_is_the_live_reading_control(transport):
    _, controller, _ = transport
    controller.connection_task = SimpleNamespace(busy=False)
    controller.set_live_reading = lambda active: controller.session.set_streaming(
        active
    )
    window = QWidget()
    window.controller = controller
    window.session = controller.session
    window.syncing = False
    window._source_changed = lambda index: PreviewWindow._source_changed(window, index)
    window.show_log_library = lambda: None
    window._track_source_changed = lambda _index: None
    window._record = lambda _active: None
    panel = PreviewWindow._nmea_panel(window)
    try:
        window.source.setCurrentIndex(1)
        assert controller.session.source == "log"
        assert not controller.session.streaming
        window.source.setCurrentIndex(0)
        assert controller.session.source == "live"
        assert controller.session.streaming
    finally:
        panel.close()
        window.close()


def test_gps_picker_updates_plot_both_maps_and_marker_without_replay(transport):
    from gweatherrouting.core.n2kdevices import DeviceCollector
    from test.test_n2kdevices import claim, fragments

    _, controller, _ = transport
    session = controller.session
    points = plot_points()
    tracks = {
        6: NMEATrack(points, (10, 20, 30)),
        43: NMEATrack(points[1:], (20, 30)),
    }
    metadata = DeviceCollector()
    for message in [claim(1), *fragments()]:
        metadata.observe(message)
    log = NMEALog(
        "race.ebl",
        session.log.entries,
        0,
        True,
        points=points,
        point_offsets=(10, 20, 30),
        source_tracks=tracks,
        default_track_source=6,
        devices=metadata.devices(),
        device_metadata_available=True,
    )
    session.finish_load(session.begin_load(), log)
    session.preview(25)
    original_entries = log.entries
    original_tracks = dict(log.source_tracks)
    controller.points = log.points
    picker = QComboBox()
    group = QWidget()
    window = SimpleNamespace(
        session=session,
        controller=controller,
        plots=Mock(),
        map=Mock(),
        log_map=Mock(),
        log_time=Mock(),
        data_key=None,
        track_source=picker,
        track_source_group=group,
        _track_source_log=None,
        views=Mock(),
        logs_view=Mock(),
    )
    window._refresh_data = lambda: PreviewWindow._refresh_data(window)
    window._refresh_log_position = lambda: PreviewWindow._refresh_log_position(window)
    picker.currentIndexChanged.connect(
        lambda index: PreviewWindow._track_source_changed(window, index)
    )
    try:
        window._refresh_data()
        assert picker.itemText(0) == "Auto (most fixes)"
        assert picker.currentData() is None
        assert (
            picker.itemText(picker.findData(6)) == "Captured GPS (Source 6) (3 fixes)"
        )
        assert picker.itemText(picker.findData(43)) == "Source 43 (2 fixes)"
        assert picker.isEnabled()
        assert log.default_track_source == max(
            tracks, key=lambda source: len(tracks[source].points)
        )
        window.plots.set_points.assert_called_with(points, (10, 20, 30), True)
        for canvas in (window.map, window.log_map):
            canvas.set_points.assert_called_with(points, (10, 20, 30), fit=False)
        cursor = session.cursor
        picker.setCurrentIndex(picker.findData(43))
        assert picker.currentData() == 43
        window.plots.set_points.assert_called_with(points[1:], (20, 30), True)
        for canvas in (window.map, window.log_map):
            canvas.set_points.assert_called_with(points[1:], (20, 30), fit=False)
            canvas.set_position.assert_called_with(25)
        window.plots.set_position.assert_called_with(25)
        assert session.cursor == cursor
        assert not session.seeking
        assert session.log is log
        assert log.entries is original_entries
        assert log.source_tracks == original_tracks
        assert log.source_tracks[6].points == points
        assert log.source_tracks[6].point_offsets == (10, 20, 30)
        assert log.source_tracks[43].points == points[1:]
        assert log.source_tracks[43].point_offsets == (20, 30)
        picker.setCurrentIndex(0)
        window.plots.set_points.assert_called_with(points, (10, 20, 30), True)
        for canvas in (window.map, window.log_map):
            canvas.set_points.assert_called_with(points, (10, 20, 30), fit=False)
            canvas.set_position.assert_called_with(25)
        assert log.source_tracks == original_tracks
        assert log.points == points
        assert log.point_offsets == (10, 20, 30)
        assert log.default_track_source == 6
        assert log.entries is original_entries
        text_log = NMEALog("text.log", log.entries, 0, True)
        session.finish_load(session.begin_load(), text_log)
        controller.points = ()
        window._refresh_data()
        assert group.isHidden()
        assert not picker.isEnabled()
        assert picker.count() == 1
    finally:
        picker.close()
        group.close()
