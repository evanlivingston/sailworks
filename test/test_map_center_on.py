"""Map camera helper used by place search."""

from types import SimpleNamespace
from unittest.mock import Mock

from sailworks.qt.mapstate import MapStateMixin


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
    )

    MapStateMixin.center_on(host, 1.0, 2.0)
    assert host._zoom == 8.5
    MapStateMixin.center_on(host, 1.0, 2.0, 99)
    assert host._zoom == 14.0
    MapStateMixin.center_on(host, 1.0, 2.0, -5)
    assert host._zoom == 1.0
