#!/usr/bin/env python3
"""Real-window fidelity check for the routing overlay.

Loads the application's QML scene in an offscreen QQuickView, feeds it the
routing overlay payload produced by the retained model, grabs the rendered
window and compares the painted line against an independent Mercator
projection computed here in Python.

This is window/fidelity evidence for the image-backed retained slice. It is not
a GPU retained-buffer measurement and makes no presentation-FPS claim.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "sailworks" / "qt" / "shaders" / "ViewportChartScene.qml"

WIDTH = 320
HEIGHT = 240
CENTER_LAT = 48.3
CENTER_LON = -123.6
ZOOM = 6.0
START = (48.2, -123.7)
END = (48.4, -123.5)
PAN = max(WIDTH, HEIGHT) * 0.5


def project(latitude, longitude, center_lat, center_lon, zoom, pan):
    """Independent Mercator projection matching the QML screenPoint rule."""
    world = 256 * 2**zoom
    latitude = max(-85.05112878, min(85.05112878, latitude))
    dx = (longitude - center_lon) * world / 360
    dx -= round(dx / world) * world
    y = world * (1 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2
    center_y = (
        world * (1 - math.asinh(math.tan(math.radians(center_lat))) / math.pi) / 2
    )
    # The canvas is offset by -panPadding in the scene, so a canvas point maps
    # to the window at (x - pan, y - pan).
    return (pan + WIDTH / 2 + dx - pan, pan + HEIGHT / 2 + y - center_y - pan)


def _is_frontier_ink(colour) -> bool:
    return colour.blue() > 110 and colour.blue() > colour.red() + 40


def _ink_near(image, x, y, radius=3):
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            px, py = int(x) + dx, int(y) + dy
            if 0 <= px < image.width() and 0 <= py < image.height():
                if _is_frontier_ink(image.pixelColor(px, py)):
                    return True
    return False


def measure() -> dict:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuick import QQuickView

    app = QGuiApplication.instance() or QGuiApplication([])
    view = QQuickView()
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.setSource(QUrl.fromLocalFile(str(SCENE)))
    view.resize(WIDTH, HEIGHT)
    view.show()
    for _ in range(10):
        app.processEvents()
        time.sleep(0.02)
    if view.status() != QQuickView.Status.Ready:
        raise RuntimeError(f"scene failed to load: {view.status()}")
    canvas = view.rootObject().findChild(object, "routeOverlay")
    if canvas is None:
        raise RuntimeError("routeOverlay canvas not found in the scene")

    canvas.setProperty("centerLatitude", CENTER_LAT)
    canvas.setProperty("centerLongitude", CENTER_LON)
    canvas.setProperty("zoom", ZOOM)
    canvas.setProperty(
        "isochrones",
        {
            "frontiers": [{"segments": [[list(START), list(END)]], "points": []}],
            "traces": [],
            "path": [],
        },
    )
    canvas.requestPaint()
    for _ in range(8):
        app.processEvents()
        time.sleep(0.02)
    image = view.grabWindow()

    midpoint = project(START[0], START[1], CENTER_LAT, CENTER_LON, ZOOM, PAN)
    midpoint = (
        (midpoint[0] + project(END[0], END[1], CENTER_LAT, CENTER_LON, ZOOM, PAN)[0])
        / 2,
        (midpoint[1] + project(END[0], END[1], CENTER_LAT, CENTER_LON, ZOOM, PAN)[1])
        / 2,
    )
    ink_at_midpoint = _ink_near(image, midpoint[0], midpoint[1])

    moved_center_lon = CENTER_LON + 0.5
    canvas.setProperty("centerLongitude", moved_center_lon)
    canvas.requestPaint()
    for _ in range(8):
        app.processEvents()
        time.sleep(0.02)
    moved = view.grabWindow()
    moved_midpoint = (
        (
            project(START[0], START[1], CENTER_LAT, moved_center_lon, ZOOM, PAN)[0]
            + project(END[0], END[1], CENTER_LAT, moved_center_lon, ZOOM, PAN)[0]
        )
        / 2,
        (
            project(START[0], START[1], CENTER_LAT, moved_center_lon, ZOOM, PAN)[1]
            + project(END[0], END[1], CENTER_LAT, moved_center_lon, ZOOM, PAN)[1]
        )
        / 2,
    )
    ink_after_camera = _ink_near(moved, moved_midpoint[0], moved_midpoint[1])
    stale_ink = _ink_near(moved, midpoint[0], midpoint[1])

    colour = image.pixelColor(int(midpoint[0]), int(midpoint[1]))
    return {
        "tool": "check_routing_overlay_window",
        "endpoint": (
            "Offscreen QQuickView render + independent Mercator oracle. "
            "Fidelity only; not GPU upload and not presentation FPS."
        ),
        "window": {"width": image.width(), "height": image.height()},
        "oracle_midpoint": {
            "x": round(midpoint[0], 2),
            "y": round(midpoint[1], 2),
        },
        "fidelity": {
            "ink_at_oracle_midpoint": ink_at_midpoint,
            "ink_after_camera_move": ink_after_camera,
            "stale_ink_at_old_midpoint": stale_ink,
            "sampled_blue_minus_red": colour.blue() - colour.red(),
            "sampled_rgba": [
                colour.red(),
                colour.green(),
                colour.blue(),
                colour.alpha(),
            ],
        },
        "payload": {"frontier_segments": 1, "traces": 0},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the measurement JSON")
    args = parser.parse_args()

    measured = measure()
    text = json.dumps(measured, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
