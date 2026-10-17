"""Compare cold/warm shoreline preparation and moving-view barb geometry.

Run with PYTHONPATH=. python tools/benchmark_wind_land_mask.py. Uses installed
GSHHS data when available; no network, chart edits, or forecast calculations.
"""

import json
from statistics import median
from time import perf_counter

import numpy as np

from gweatherrouting.rendering.windpainter import WindPainter
from gweatherrouting.rendering.windrenderer import WindLandMask


def timed(call, repeats):
    durations = []
    for _ in range(repeats):
        start = perf_counter()
        result = call()
        durations.append((perf_counter() - start) * 1000)
    return result, median(durations)


def main():
    lons, lats = np.meshgrid(np.linspace(-124, -122, 80), np.linspace(47.5, 49.5, 80))
    flat = (np.zeros(lats.size), np.full(lats.size, 8.0), lats.ravel(), lons.ravel())
    masks = {}
    results = {}
    for name in ("country_baseline", "detailed_shoreline"):
        mask = WindLandMask()
        if name == "detailed_shoreline":
            # Same tier as the 48.5 N, zoom-9 viewport below (~203 m/px).
            mask.refresh_source(scale=203)
        classify = lambda: np.array(
            [mask.contains(lat, lon) for lat, lon in zip(flat[2], flat[3])]
        )
        flags, cold = timed(classify, 1)
        _, warm = timed(classify, 10)
        masks[name] = flags
        results[name] = dict(
            cold_worker_ms=cold, warm_worker_ms=warm, land_points=int(flags.sum())
        )
    filtered = tuple(values[~masks["country_baseline"]] for values in flat)
    # Reproject each call (stationary UI repaints reuse cached geometry).
    for name, samples, flags in (
        ("old_moving_geometry", filtered, None),
        ("new_moving_geometry", flat, masks["detailed_shoreline"]),
    ):
        (_, count), elapsed = timed(
            lambda: WindPainter.live_geometry_for_view(
                samples, 48.5, -123, 9, 1600, 1000, land_mask=flags
            ),
            100,
        )
        results[name] = dict(median_ms=elapsed, barbs=count)
    results["sample_count"] = lats.size
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
