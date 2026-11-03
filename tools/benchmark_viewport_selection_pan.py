"""Model-side pan baseline for the resident weather selection.

This measures ``GribViewportModel.update()`` and the selection work it runs
against a captured app snapshot. It is deliberately narrower than the app
replay: no window, no GPU readback and no presentation clock. The committed
report is the reference for the pan/zoom optimisation tickets.

The snapshot is private and must not be committed. The report contains the
snapshot content hash, the fixed camera, per-frame timings and counters only.
"""

import argparse
import json
import math
import os
import platform
import statistics
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA = 1
CAMERA = {
    "center_lat": 48.30887922260005,
    "center_lon": -123.09473088970692,
    "zoom": 9.837590787619122,
    "width": 900,
    "height": 700,
}


def summary(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}

    def percentile(fraction):
        index = (len(ordered) - 1) * fraction
        lower = math.floor(index)
        upper = math.ceil(index)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)

    return {
        "count": len(values),
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": ordered[-1],
        "mean_ms": statistics.fmean(ordered),
    }


def environment():
    import cairo
    import numpy
    import PySide6

    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "host": platform.node(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "qt": PySide6.__version__,
        "numpy": numpy.__version__,
        "cairo": cairo.cairo_version_string(),
    }


def run(snapshot, frames, pixels_per_frame, timeout_s):
    from sailworks.qt.gribviewport import (
        GribViewportModel,
        ViewportCamera,
        saved_selection,
    )
    from sailworks.rendering import weatherbarbs
    from sailworks.rendering.windrenderer import WindLandMask

    data = snapshot / "data"
    manifest = json.loads((snapshot / "manifest.json").read_text())
    paths, settings = saved_selection(data)
    model = GribViewportModel(
        paths,
        land_mask=WindLandMask(data),
        resident_time_window=True,
        grid=True,
    )
    camera = ViewportCamera(
        center_lat=CAMERA["center_lat"],
        center_lon=CAMERA["center_lon"],
        zoom=CAMERA["zoom"],
        width=CAMERA["width"],
        height=CAMERA["height"],
        time=datetime.fromisoformat(manifest["forecast_time"]).replace(
            tzinfo=timezone.utc
        ),
        density=settings.get("gribBarbDensity", "very_dense"),
        over_land=settings.get("gribArrowOnGround", False),
    )
    counters = {"stage_selection": 0, "node_anchor_atlas": 0}
    spans = {"stage_selection_ms": [], "node_anchor_atlas_ms": []}
    real_selection = weatherbarbs.stage_selection
    real_atlas = weatherbarbs.node_anchor_atlas

    def measured_selection(*args, **kwargs):
        started = time.perf_counter()
        try:
            return real_selection(*args, **kwargs)
        finally:
            counters["stage_selection"] += 1
            spans["stage_selection_ms"].append((time.perf_counter() - started) * 1000)

    def measured_atlas(*args, **kwargs):
        started = time.perf_counter()
        try:
            return real_atlas(*args, **kwargs)
        finally:
            counters["node_anchor_atlas"] += 1
            spans["node_anchor_atlas_ms"].append((time.perf_counter() - started) * 1000)

    weatherbarbs.stage_selection = measured_selection
    weatherbarbs.node_anchor_atlas = measured_atlas
    try:
        # Cold load: the first camera owns the worker preparation.
        deadline = time.perf_counter() + timeout_s
        state = model.update(camera)[0]
        while state != "ready" and time.perf_counter() < deadline:
            time.sleep(0.005)
            state = model.update(camera)[0]
        if state != "ready":
            raise TimeoutError(f"Cold load did not settle: {model.error}")
        cold_counters = dict(model.counters)
        reference = camera.view()
        step = pixels_per_frame * 360.0 / (256.0 * 2.0**camera.zoom)
        update_ms = []
        states = []
        for index in range(frames):
            camera = replace(camera, center_lon=camera.center_lon - step)
            started = time.perf_counter()
            state = model.update(camera, reference=reference)[0]
            update_ms.append((time.perf_counter() - started) * 1000)
            states.append(state)
        return {
            "schema_version": SCHEMA,
            "kind": "viewport-selection-pan-v1",
            "snapshot_sha256": manifest["content_sha256"],
            "forecast_time": manifest["forecast_time"],
            "camera": dict(CAMERA, time=manifest["forecast_time"]),
            "settings": {
                "density": camera.density,
                "over_land": camera.over_land,
                "sources": len(paths),
            },
            "workload": {
                "frames": frames,
                "pixels_per_frame": pixels_per_frame,
                "longitude_step_degrees": step,
                "reference": "PanReference at the cold camera",
            },
            "environment": environment(),
            "cold": {
                "state": state,
                "counters": cold_counters,
                "retained_field": model.field_statistics(),
            },
            "samples": {
                "update_ms": update_ms,
                **spans,
            },
            "summary": {
                "update_ms": summary(update_ms),
                "stage_selection_ms": summary(spans["stage_selection_ms"]),
                "node_anchor_atlas_ms": summary(spans["node_anchor_atlas_ms"]),
            },
            "states": {name: states.count(name) for name in sorted(set(states))},
            "counters": dict(model.counters),
            "selection_calls": counters,
            "retained_field": model.field_statistics(),
        }
    finally:
        weatherbarbs.stage_selection = real_selection
        weatherbarbs.node_anchor_atlas = real_atlas
        model.close()
        thread = getattr(model, "_thread", None)
        if thread is not None:
            thread.join(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--pixels-per-frame", type=float, default=8.0)
    parser.add_argument("--timeout-s", type=float, default=120.0)
    args = parser.parse_args()
    snapshot = args.snapshot.resolve(strict=True)
    report = run(snapshot, args.frames, args.pixels_per_frame, args.timeout_s)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
