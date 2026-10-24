"""Deterministic correctness check for Qt Quick marine annotations."""

import argparse
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from gweatherrouting.qt.gribviewport import ViewportCamera
from gweatherrouting.rendering.marinepresentation import MarineSources
from gweatherrouting.rendering.marinesnapshot import (
    current_labels,
    current_triangles,
    sample_currents,
)


def run():
    instant = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
    station = SimpleNamespace(
        id="fixture-station", latitude=49.5, longitude=-125.5, name="Fixture Harbor"
    )

    def selection(values):
        return SimpleNamespace(
            datasets=(SimpleNamespace(station=station),),
            sample=lambda _station_id, _instant: SimpleNamespace(
                status="available", values=values
            ),
        )

    sources = MarineSources((), selection((2.5,)), selection((1.0, 0.0)))
    camera = ViewportCamera(49.5, -125.5, 10, 256, 192, instant)
    snapshot = sample_currents(sources, instant, camera, lambda: False)
    currents = [arrow for arrow in snapshot.arrows if not arrow.tide]
    tides = [arrow for arrow in snapshot.arrows if arrow.tide]
    labels = {text for _x, _y, text in current_labels(snapshot, camera)}
    vertices = current_triangles(snapshot, camera)
    return {
        "station_current_count": len(currents),
        "tide_count": len(tides),
        "current_label_present": any("Fixture Harbor" in text for text in labels)
        and any("kn (station)" in text for text in labels),
        "tide_label_present": any("2.50 m (source datum)" in text for text in labels),
        "geometry_vertices": len(vertices),
        "cairo_marine_modules_absent": all(
            importlib.util.find_spec(name) is None
            for name in (
                "gweatherrouting.rendering.marinemaplayer",
                "gweatherrouting.rendering.marinelandmask",
                "gweatherrouting.qt.marineoverlay",
            )
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    metrics = run()
    Path(args.output).write_text(json.dumps({"marine_current": metrics}, indent=2))
    checks = (
        metrics["station_current_count"] == 1,
        metrics["tide_count"] == 1,
        metrics["current_label_present"],
        metrics["tide_label_present"],
        metrics["geometry_vertices"] > 0,
        metrics["cairo_marine_modules_absent"],
    )
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
