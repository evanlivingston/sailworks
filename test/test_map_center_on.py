"""Map camera helper used by place search."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sailworks.qt.mapstate import MapStateMixin


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


class FlyHost(QObject):
    def __init__(self):
        super().__init__()
        self._center_lat = 0.0
        self._center_lon = 0.0
        self._zoom = 3.0
        self.background = None
        self.zooms = []
        self._cancel_cursor = Mock()
        self._cancel_inertia = Mock()
        self._finish_zoom_silent = Mock()
        self._invalidate_base = Mock()
        self.invalidate_navigation = Mock()
        self.view_changed = SimpleNamespace(emit=Mock())

    def cancel_fly(self):
        MapStateMixin.cancel_fly(self)

    def update(self):
        self.zooms.append(self._zoom)


def test_fly_to_dips_then_reaches_the_target(app):
    host = FlyHost()
    MapStateMixin.fly_to(host, 40.0, 100.0, 8.0, duration_ms=250)
    QTest.qWait(450)

    assert host._center_lat == pytest.approx(40.0)
    assert host._center_lon == pytest.approx(100.0)
    assert host._zoom == pytest.approx(8.0)
    assert min(host.zooms) < 3.0
    assert host.view_changed.emit.called


def test_retained_viewport_cameras_do_not_cancel_a_fly_to():
    from sailworks.qt.viewportmap import ViewportMap

    host = SimpleNamespace(_fly_tick=SimpleNamespace(isActive=lambda: True))
    camera = SimpleNamespace(center_lat=1.0, center_lon=2.0, zoom=3.0)
    assert ViewportMap.adopt_viewport_camera(host, camera) is False


def test_center_on_sets_the_camera_and_zoom():
    changed = Mock()
    host = SimpleNamespace(
        _center_lat=0.0,
        _center_lon=0.0,
        _zoom=5.0,
        background=object(),
        _cancel_cursor=Mock(),
        _cancel_inertia=Mock(),
        _finish_zoom_silent=Mock(),
        _invalidate_base=Mock(),
        invalidate_navigation=Mock(),
        update=Mock(),
        view_changed=SimpleNamespace(emit=changed),
        cancel_fly=Mock(),
    )

    MapStateMixin.center_on(host, 48.1, -122.75, 14.0)

    assert (host._center_lat, host._center_lon, host._zoom) == (48.1, -122.75, 14.0)
    assert host.background is None
    host._cancel_cursor.assert_called_once_with()
    host._cancel_inertia.assert_called_once_with(settle=False)
    host._finish_zoom_silent.assert_called_once_with()
    host._invalidate_base.assert_called_once_with()
    host.invalidate_navigation.assert_called_once_with()
    host.update.assert_called_once_with()
    changed.assert_called_once_with()


def test_center_on_clamps_the_zoom_and_keeps_the_current_zoom_by_default():
    host = SimpleNamespace(
        _center_lat=0.0,
        _center_lon=0.0,
        _zoom=8.5,
        background=None,
        _cancel_cursor=Mock(),
        _cancel_inertia=Mock(),
        _finish_zoom_silent=Mock(),
        _invalidate_base=Mock(),
        invalidate_navigation=Mock(),
        update=Mock(),
        view_changed=SimpleNamespace(emit=Mock()),
        cancel_fly=Mock(),
    )

    MapStateMixin.center_on(host, 1.0, 2.0)
    assert host._zoom == 8.5
    MapStateMixin.center_on(host, 1.0, 2.0, 99)
    assert host._zoom == 14.0
    MapStateMixin.center_on(host, 1.0, 2.0, -5)
    assert host._zoom == 1.0
