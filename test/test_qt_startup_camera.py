"""The saved viewport camera must survive startup data refreshes."""

from types import SimpleNamespace
from unittest.mock import Mock

from gweatherrouting.qt.window import PreviewWindow


def test_initial_data_refresh_does_not_fit_or_switch_from_saved_view():
    window = Mock()
    window.session = SimpleNamespace(
        source="log", log=SimpleNamespace(point_offsets=[])
    )
    window.controller.points = [SimpleNamespace(lat=90, lon=-180)]
    window.data_key = None
    PreviewWindow._refresh_data(window)
    window.map.set_points.assert_called_once_with(
        window.controller.points, [], fit=False
    )
    window.log_map.set_points.assert_called_once_with(
        window.controller.points, [], fit=False
    )
    window.views.setCurrentWidget.assert_not_called()


def test_deliberate_source_switch_still_fits_track():
    window = Mock()
    log = SimpleNamespace(point_offsets=[0])
    window.session = SimpleNamespace(source="log", log=log)
    window.controller.points = [SimpleNamespace(lat=48.4, lon=-123.4)]
    window.data_key = ("live", 1)
    PreviewWindow._refresh_data(window)
    window.map.set_points.assert_called_once_with(
        window.controller.points, log.point_offsets, fit=True
    )
    window.views.setCurrentWidget.assert_called_once_with(window.logs_view)
