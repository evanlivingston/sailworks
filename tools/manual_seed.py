"""Deterministic sailing data for the manual screenshots.

The capture tool runs the real application against a temporary profile, so the
screens are empty unless something is seeded. The fixtures come from the
Pacific NW Offshore 2026 race file (Yellowbrick tracking) reduced to a course,
two boats and the course marks: ``tools/samples/pnwos2026_seed.json``.

Everything is fixed per capture, so the images stay comparable between runs,
and the area has satellite coverage without any local chart pack.
"""

from __future__ import annotations

import json
from pathlib import Path

WIDGET = object

SOURCE = Path(__file__).resolve().parent / "samples" / "pnwos2026_seed.json"
ZOOM = 7


def _data() -> dict:
    return json.loads(SOURCE.read_text(encoding="utf-8"))


def _centre(bounds):
    (south, west), (north, east) = bounds
    return ((south + north) / 2, (west + east) / 2)


def seed(window: WIDGET) -> None:
    """Load the fixtures into ``window``'s collections and refresh the panels."""

    from sailworks.core.geo.routing import Routing

    data = _data()
    core = window.controller.core

    for track in data["tracks"]:
        element = core.trackManager.new_element(points=track["points"])
        element.name = track["name"]
    core.trackManager.set_active(core.trackManager[0])

    for poi in data["pois"]:
        if poi["name"] == "Course":
            continue
        element = core.poiManager.create(tuple(poi["position"]))
        element.name = poi["name"]

    core.routingManager.append(
        Routing(
            data["course"]["name"],
            data["course"]["points"],
            collection=core.routingManager,
        )
    )

    window.navigation_panel.refresh()


def center_chart(window: WIDGET) -> None:
    """Aim the chart at the seeded course so the map shows the race area."""

    viewport = getattr(window, "map", None)
    camera = getattr(viewport, "camera_state", None)
    if camera is None:
        return
    latitude, longitude = _centre(_data()["bounds"])
    try:
        camera.center_lat = latitude
        camera.center_lon = longitude
        camera.zoom = ZOOM
    except AttributeError:
        pass
