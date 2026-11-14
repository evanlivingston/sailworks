"""Chart capacity fallback through the real QML image/provider lifecycle.

This is a publication-capacity test, not a native-GPU or visual rendering gate.
The image dimensions simulate a DPR-2 composition; no screen DPR is asserted.
"""

import os
import time
from pathlib import Path
from types import MethodType
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QColor, QGuiApplication, QImage
from PySide6.QtQuick import QQuickView
from PySide6.QtTest import QTest

from sailworks.qt.gribviewport import _viewport_widget_class
from sailworks.qt.gribviewportchart import chart_image_provider
from sailworks.qt.viewportpublication import LayerRole, PublicationManager


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    yield QGuiApplication.instance() or QGuiApplication([])


def _wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate(), "timed out waiting for chart QML publication"


class _WidgetHarness:
    """A widget stand-in with the background lifecycle's real methods."""

    def __init__(self, **attributes):
        self.__dict__.update(attributes)
        for name, value in (
            ("_background_pending", None),
            ("_background_pending_camera", None),
            ("_background_presented", "basemap"),
            ("_chart_background_identity", None),
            ("_chart_swap_fade_armed", False),
            ("_satellite_visible", False),
            ("_satellite_visible_generation", None),
            ("_weather", None),
            ("_camera", None),
        ):
            self.__dict__.setdefault(name, value)
        cls = _viewport_widget_class()
        for name in (
            "_present_background",
            "_settle_background_presentation",
            "_cancel_background_switch",
        ):
            self.__dict__.setdefault(name, MethodType(getattr(cls, name), self))


def test_chart_capacity_withdrawal_loads_replacement_in_real_qml(app):
    provider = chart_image_provider()
    manager = PublicationManager()
    view = QQuickView()
    view.engine().addImageProvider("chart", provider)
    path = Path(__file__).resolve().parents[1] / "sailworks/qt/shaders/ChartLayer.qml"
    view.setSource(QUrl.fromLocalFile(str(path)))
    assert not view.errors(), str(view.errors())
    chart = view.rootObject()
    assert chart is not None
    view.resize(64, 64)
    view.show()

    widget = _WidgetHarness(
        _disposed=False,
        _chart_payload_valid=Mock(return_value=True),
        _chart_pending=None,
        _chart_attempt=0,
        _chart_redelivery_generation=None,
        _chart_front_slot=0,
        _chart_visible_generation=None,
        _chart_viewport=None,
        _chart_composition_camera=None,
        _chart_composition_dpr=None,
        _chart=chart,
        _chart_provider=provider,
        _publication_manager=manager,
        _enc_chart_layer=False,
        _update_enc_status=Mock(),
        _place_chart=Mock(),
        _chart_state_now=Mock(return_value="ready"),
        _chart_ready_at=None,
        counters={"chart_publications": 0},
    )
    cls = _viewport_widget_class()
    for name in (
        "_reject_chart_pending",
        "_clear_chart",
        "_chart_state_changed",
        "_publish_chart",
        "_chart_slot_result",
    ):
        setattr(widget, name, MethodType(getattr(cls, name), widget))

    results = []
    chart.slotResult.connect(
        lambda slot, token, success: results.append((slot, token, success))
    )
    chart.slotResult.connect(
        widget._chart_slot_result, Qt.ConnectionType.QueuedConnection
    )
    stage = manager.stage_resources
    stages = []

    def record_stage(*args, **kwargs):
        result = stage(*args, **kwargs)
        stages.append((result, manager.budget.current_usage[LayerRole.CHART]))
        return result

    manager.stage_resources = record_stage
    withdraw = manager.withdraw_visible
    withdrawals = []

    def record_withdraw(role):
        withdrawals.append(
            (manager.layers[role].visible.generation, chart.property("chartLoading"))
        )
        return withdraw(role)

    manager.withdraw_visible = record_withdraw

    # A 1712x1312 logical composition at factor 2 exceeds half the 64 MiB
    # chart cap. Measure the actual QImage allocation, including row stride.
    first = QImage(1712 * 2, 1312 * 2, QImage.Format.Format_ARGB32)
    replacement = QImage(first.size(), first.format())
    first.fill(QColor("red"))
    replacement.fill(QColor("blue"))
    size = replacement.sizeInBytes()
    cap = manager.budget.layer_caps[LayerRole.CHART]
    assert cap == 64 * 1024 * 1024
    assert first.sizeInBytes() == size and size <= cap < 2 * size

    try:
        metadata = {"viewport": object(), "factor": 2}
        widget._publish_chart((1, first, metadata))
        _wait_for(lambda: widget._chart_visible_generation == 1)
        assert chart.property("ready") and chart.property("frontStatus") == 1
        assert results == [(1, "1", True)]
        assert manager.budget.current_usage[LayerRole.CHART] == size

        widget._publish_chart((2, replacement, metadata))
        assert stages == [(True, size), (False, size), (True, size)]
        assert withdrawals == [(1, True)]
        assert widget._chart_visible_generation is None
        assert chart.property("chartLoading")
        assert not chart.property("ready")
        assert provider.requestImage("1", None, None).isNull()
        assert manager.budget.current_usage[LayerRole.CHART] == size

        _wait_for(lambda: widget._chart_visible_generation == 2)
        assert results[-1] == (0, "3", True)
        assert chart.property("ready") and chart.property("frontStatus") == 1
        assert not chart.property("chartLoading")
        assert not chart.property("chartUnavailable")
        assert manager.layers[LayerRole.CHART].pending is None
        assert manager.budget.current_usage[LayerRole.CHART] == size <= cap
        assert manager.budget.total_used() <= manager.budget.global_cap
        reported_size = QSize()
        served = provider.requestImage("2", reported_size, QSize())
        assert reported_size == replacement.size() == served.size()
        assert served.pixelColor(0, 0) == QColor("blue")
        assert served.pixelColor(served.width() - 1, served.height() - 1) == QColor(
            "blue"
        )
        assert provider.requests >= 3  # two QML loads and the explicit request
    finally:
        manager.withdraw_visible = withdraw
        widget._clear_chart()
        widget._disposed = True
        view.close()
        view.releaseResources()
