#!/usr/bin/env python3
"""Retained routing imagery fidelity check.

Verifies that retained primitives survive camera moves and style changes without
rebuilding geometry, that the rendered line lands on the independently computed
projection of the segment midpoint, and that device-pixel-ratio scaling is
applied to both the image and the coordinates.

This is CPU-image fidelity for the retained layer used by the Qt viewport; it is
not a GPU timing measurement and makes no presentation-FPS claim.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QColor  # noqa: E402

from sailworks.core.marine.routingrecord import (  # noqa: E402
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
from sailworks.core.marine.routingresult import (  # noqa: E402
    RoutingResult,
    canonical_utc,
)
from sailworks.rendering.routingoverlay import RoutingOverlayModel  # noqa: E402
from sailworks.rendering.routingretained import RetainedRoutingLayer  # noqa: E402


class FakeCamera:
    """Linear projection for the headless fidelity check."""

    def __init__(self, offset_x=0.0):
        self.offset_x = offset_x

    def convert_geographic_to_screen(self, latitude, longitude):
        return (self.offset_x + longitude * 100.0, latitude * 100.0)


def build_result() -> RoutingResult:
    return RoutingResult(
        calculation_id="6f1c1a1e-0000-4000-8000-000000000014",
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
                Point(0.5, 0.0),
                Point(0.5, 1.0),
                1767225600,
                "2026-01-01T00:00:00Z",
                1767226500,
                "2026-01-01T00:15:00Z",
            ),
        ),
        layers=(
            Layer(
                "layer-0",
                1767226500,
                canonical_utc(1767226500),
                False,
                (Contour("c-0", False, (Point(0.5, 0.0), Point(0.5, 1.0))),),
            ),
        ),
        capabilities=Capabilities("main", "absent", "sampled", 12, 1800),
    )


WIDTH = 120
HEIGHT = 90


def _midpoint_ink(image, camera, first, second, dpr):
    start = camera.convert_geographic_to_screen(*first)
    end = camera.convert_geographic_to_screen(*second)
    x = int(round((start[0] + end[0]) / 2.0 * dpr))
    y = int(round((start[1] + end[1]) / 2.0 * dpr))
    if not (0 <= x < image.width() and 0 <= y < image.height()):
        return 0
    best = 0
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            px, py = x + dx, y + dy
            if 0 <= px < image.width() and 0 <= py < image.height():
                best = max(best, image.pixelColor(px, py).alpha())
    return best


def measure() -> dict:
    model = RoutingOverlayModel(build_result())
    layer = RetainedRoutingLayer(colour=QColor(0, 0, 255, 255))
    layer.set_model(model)
    builds_after_model = layer.counters.geometry_builds

    camera = FakeCamera()
    layer.render(camera, WIDTH, HEIGHT)
    first_segment = (0.5, 0.0), (0.5, 1.0)
    fidelity_alpha = _midpoint_ink(layer._image, camera, *first_segment, 1.0)

    for offset in range(5):
        moved = FakeCamera(offset_x=offset)
        layer.render(moved, WIDTH, HEIGHT)
    builds_after_camera = layer.counters.geometry_builds
    panned_alpha = _midpoint_ink(
        layer._image, FakeCamera(offset_x=4), *first_segment, 1.0
    )

    layer.set_style(colour=QColor(255, 0, 0, 255), width=2.0)
    layer.render(FakeCamera(), WIDTH, HEIGHT)
    builds_after_style = layer.counters.geometry_builds
    styled = layer._image.pixelColor(int(round(0.5 * 100)), int(round(0.5 * 100)))

    dpr_image = layer.render(FakeCamera(), WIDTH, HEIGHT, dpr=2.0)
    dpr_alpha = _midpoint_ink(dpr_image, FakeCamera(), *first_segment, 2.0)

    return {
        "tool": "check_routing_overlay_retained",
        "endpoint": (
            "CPU retained-image fidelity for routing geometry; NOT a GPU or "
            "presentation timing measurement"
        ),
        "retained": {
            "geometry_builds_after_model": builds_after_model,
            "geometry_rebuilds_during_camera": builds_after_camera - builds_after_model,
            "geometry_rebuilds_during_style": builds_after_style - builds_after_camera,
            "renders": layer.counters.renders,
            "style_changes": layer.counters.style_changes,
        },
        "fidelity": {
            "line_alpha_at_projected_midpoint": fidelity_alpha,
            "line_alpha_after_camera_moves": panned_alpha,
            "style_red_channel": styled.red(),
            "dpr_image_width": dpr_image.width(),
            "dpr_image_height": dpr_image.height(),
            "dpr_line_alpha": dpr_alpha,
        },
        "expected": {
            "dpr_image_width": WIDTH * 2,
            "dpr_image_height": HEIGHT * 2,
        },
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
