"""App-level wiring for the retained QSG routing overlay.

Requires the built QML module (tools/build_routing_qsg_module.py) and
SAILWORKS_ROUTING_QML_PATH pointing at its parent directory. The module is
relinked against PySide6's frameworks, so the default runtime loads it; without
the module the viewport falls back to the image-backed canvas overlay, covered
by the routing-overlay-window gate.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickView

from sailworks.core.marine.routingrecord import (
    Capabilities,
    Contour,
    EngineIdentity,
    Layer,
    Leg,
    Outcome,
    Point,
    Provenance,
    Validation,
)
from sailworks.core.marine.routingresult import RoutingResult, canonical_utc
from sailworks.qt.viewportmap import ViewportMap
from sailworks.rendering.routingoverlay import RoutingOverlayModel

ROOT = Path(__file__).resolve().parents[1]


def _qml_dir():
    configured = os.environ.get("SAILWORKS_ROUTING_QML_PATH")
    if configured:
        return configured
    for candidate in (
        ROOT / "build" / "routing-qsg-module",
        ROOT / "build" / "routing-qt" / "qml",
    ):
        if candidate.is_dir():
            return str(candidate)
    return None


QML_DIR = _qml_dir()

pytestmark = pytest.mark.skipif(
    not QML_DIR or not Path(QML_DIR).is_dir(),
    reason="needs the built retained routing QML module",
)

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000015"


def _result() -> RoutingResult:
    layer = Layer(
        "layer-0",
        1767226500,
        canonical_utc(1767226500),
        False,
        (Contour("c-0", False, (Point(48.2, -123.7), Point(48.4, -123.5))),),
    )
    return RoutingResult(
        calculation_id=CALCULATION,
        generation=1,
        sequence=1,
        status="complete",
        outcome=Outcome(True, True, Validation("passed", True)),
        engine_identity=EngineIdentity(
            "rgleason/weather_routing_pi",
            "4df1b519e75e664dd72eed62dbb62b4a3e02b8d1",
            "1.2.1",
            "1.1.0",
            "a" * 64,
            None,
        ),
        provenance=Provenance("2026-01-01T00:00:00Z", "recorded"),
        legs=(
            Leg(
                "leg-0",
                Point(48.2, -123.7),
                Point(48.4, -123.5),
                1767225600,
                "2026-01-01T00:00:00Z",
                1767226500,
                "2026-01-01T00:15:00Z",
            ),
        ),
        layers=(layer,),
        capabilities=Capabilities("main", "absent", "sampled", 12, 1800),
    )


def test_retained_overlay_is_attached_to_the_scene():
    app = QGuiApplication.instance() or QGuiApplication([])
    view = QQuickView()
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.engine().addImportPath(str(QML_DIR))
    scene = ROOT / "sailworks" / "qt" / "shaders" / "ViewportChartScene.qml"
    view.setSource(QUrl.fromLocalFile(str(scene)))
    view.resize(320, 240)
    view.show()
    for _ in range(12):
        app.processEvents()
        time.sleep(0.02)

    host = SimpleNamespace(
        _weather_presenter=SimpleNamespace(widget=view),
        native_routing=RoutingOverlayModel(_result()),
        camera_state=SimpleNamespace(center_lat=48.3, center_lon=-123.6, zoom=6.0),
        _retained_import_added=False,
        _retained_qml_dir=lambda: str(QML_DIR),
    )

    assert ViewportMap._sync_retained_overlay(host) is True
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    item = view.rootObject().findChild(object, "retainedRoutingOverlay")
    assert item is not None
    assert item.property("segments")
    assert item.property("zoom") == pytest.approx(6.0)

    image = view.grabWindow()
    ink = [
        (x, y)
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).blue() > 100
        and image.pixelColor(x, y).blue() > image.pixelColor(x, y).red() + 30
    ]
    assert ink
    view.close()
