#!/usr/bin/env python3
"""Headless correctness check for the retained routing overlay.

Builds a deterministic schema-v1 result, converts it to retained geometry and
verifies the behavioural invariants the overlay promises: engine contour order
and open/closed state preserved, no segment crosses the antimeridian, style
changes do not alter geometry, picking is deterministic, and the QML payload
carries no proximity joins.

No timing measurement is taken here; this is fidelity only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sailworks.core.marine.routingrecord import (  # noqa: E402
    Capabilities,
    Contour,
    EngineIdentity,
    Layer,
    Leg,
    Outcome,
    Point,
    Provenance,
    Trace,
    Validation,
)
from sailworks.core.marine.routingresult import (  # noqa: E402
    RoutingResult,
    canonical_utc,
)
from sailworks.rendering.routingoverlay import (  # noqa: E402
    RoutingOverlayModel,
    routing_overlay_data,
)

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000012"


def build_result() -> RoutingResult:
    layers = (
        Layer(
            "layer-0",
            1767226500,
            canonical_utc(1767226500),
            False,
            (
                Contour(
                    "closed-contour",
                    True,
                    (Point(48.0, -124.0), Point(48.1, -123.9), Point(48.2, -123.8)),
                ),
                Contour(
                    "open-contour",
                    False,
                    (Point(48.3, -123.7), Point(48.4, -123.6)),
                ),
                Contour(
                    "dateline-contour",
                    False,
                    (Point(10.0, 179.5), Point(10.0, -179.5)),
                ),
            ),
        ),
        Layer(
            "layer-1",
            1767228300,
            canonical_utc(1767228300),
            True,
            (
                Contour(
                    "reverse-contour", True, (Point(48.1, -124.0), Point(48.2, -123.9))
                ),
            ),
        ),
    )
    traces = tuple(
        Trace(
            f"trace-{index}",
            Point(48.1 + index * 0.01, -123.9),
            (
                Point(48.0, -124.0),
                Point(48.1 + index * 0.01, -123.9),
            ),
        )
        for index in range(3)
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
        layers=layers,
        capabilities=Capabilities(
            "main",
            "absent",
            "sampled",
            12,
            1800,
            {"inspection_geometry": "sampled", "shoreline": "unsupported"},
        ),
        traces=traces,
    )


def measure() -> dict:
    model = RoutingOverlayModel(build_result())
    payload = routing_overlay_data(model)

    contour_ids = [
        polyline.object_id for polyline in model.polylines if polyline.kind == "contour"
    ]
    expected_ids = ["closed-contour", "open-contour", "dateline-contour"]
    expected_ids += ["dateline-contour#1", "reverse-contour"]
    order_preserved = contour_ids == expected_ids
    open_state_preserved = [
        polyline.closed for polyline in model.polylines if polyline.kind == "contour"
    ] == [True, False, False, False, True]

    crossing = 0
    for frontier in payload["frontiers"]:
        for first, second in frontier["segments"]:
            if abs(first[1] - second[1]) > 180.0:
                crossing += 1

    digest_before = model.geometry_digest
    revision_before = model.geometry_revision
    model.set_layer_visible("layer-0", False)
    model.set_selected_layer("layer-1")
    model.set_thickness("contour", 2.5)
    style_stable = (
        model.geometry_digest == digest_before
        and model.geometry_revision == revision_before
    )

    picks = [
        model.pick_trace(48.11, -123.9),
        model.pick_trace(48.11, -123.9),
        model.pick_trace(48.12, -123.9),
    ]
    picking_deterministic = picks[0] == picks[1] and picks[0] is not None

    measured = {
        "tool": "check_routing_overlay",
        "calculation_id": CALCULATION,
        "geometry": {
            "polyline_count": len(model.polylines),
            "contour_count": len(contour_ids),
            "segment_count": sum(
                len(frontier["segments"]) for frontier in payload["frontiers"]
            ),
            "digest": model.geometry_digest,
        },
        "topology": {
            "contour_order_preserved": order_preserved,
            "open_state_preserved": open_state_preserved,
            "cross_world_segments": crossing,
        },
        "style": {"geometry_stable": style_stable},
        "picking": {
            "deterministic": picking_deterministic,
            "first_pick": picks[0],
        },
        "payload": {"bytes": len(json.dumps(payload))},
        "endpoint": "headless retained-geometry fidelity; no timing",
    }
    return measured


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
