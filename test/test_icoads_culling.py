"""Compare geographic pre-culling against the original project-all path."""

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from PySide6.QtQuick import QQuickItem
from PySide6.QtWidgets import QApplication

from sailworks.core.mapviewport import MapPoint, MercatorProjection
from sailworks.core.marine.icoads import ShipReport
from sailworks.qt.gribviewport import ViewportCamera
from sailworks.qt.icoadsquick import IcoadsQuickItem


@pytest.fixture(scope="module", autouse=True)
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    yield application


def check(camera, coords, padded=False):
    parent = QQuickItem() if padded else None
    item = IcoadsQuickItem(parent)
    item.setWidth(camera.width)
    item.setHeight(camera.height)
    item.set_camera(camera, reference=camera if padded else None)
    reports = tuple(ShipReport(lat, lon, camera.time) for lat, lon in coords)
    item.set_reports(reports, camera)
    view = camera.view()
    expected = []
    for report in reports:
        x, y = view.convert_geographic_to_screen(
            MapPoint(report.latitude, report.longitude)
        )
        x, y = x + item._padding, y + item._padding
        if 0 <= x <= item.width() and 0 <= y <= item.height():
            expected.append((x, y, report))
    original = MercatorProjection.convert_geographic_to_screen
    calls = []

    def project(view, point):
        calls.append(point.get_degrees())
        return original(view, point)

    with patch.object(MercatorProjection, "convert_geographic_to_screen", project):
        actual = item._positions()
    assert actual == tuple(expected)
    if actual:
        x, y, report = actual[0]
        assert item._report_at(x, y) is report
    return actual, calls


def camera(lat=0, lon=0, zoom=8, width=900, height=700):
    return ViewportCamera(lat, lon, zoom, width, height, datetime.now(timezone.utc))


def test_zoom_culls_before_projection():
    actual, calls = check(camera(), [(0, 0), (1, 1), (20, 20), (-30, -30)])
    assert len(actual) == len(calls) == 2


def test_antimeridian_candidates_preserve_unwrapped_projection():
    actual, calls = check(camera(lon=179, zoom=4), [(0, 179), (0, -179), (0, 0)])
    assert len(actual) == 1
    assert len(calls) == 2


def test_world_spanning_view_projects_all_longitudes():
    actual, calls = check(camera(zoom=0), [(0, -179), (0, 0), (0, 179)])
    assert len(actual) == len(calls) == 3


def test_padding_retains_outside_viewport():
    actual, calls = check(camera(), [(0, 3), (0, 15)], padded=True)
    assert len(actual) == len(calls) == 1


def test_clamped_latitudes_match_projection():
    actual, calls = check(camera(lat=85, zoom=4), [(85.1, 0), (90, 0), (-90, 0)])
    assert len(actual) == len(calls) == 2
