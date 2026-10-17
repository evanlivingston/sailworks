"""Benchmark saved routing inputs with wind, currents and coastline enabled.

Example (run from the repository, with PYTHONPATH=.):
    .venv/bin/python tools/benchmark_frontier_routing.py \
        --data-dir "$HOME/Library/Application Support/gweatherrouting" \
        --grib /path/to/forecast.grib2 --polar /path/to/boat.pol \
        --start 2026-10-12T04:55:00+00:00 --steps 15

Uses selected offline fields; does not download data or save routes. Repeat
--grib to preserve a specific enabled-source order. Use --engine both to compare
with the scalar reference (which can take substantially longer).
"""

import argparse
import json
from datetime import datetime
from pathlib import Path
from statistics import median
from time import perf_counter
from types import SimpleNamespace

import numpy as np
import weatherrouting
from osgeo import ogr

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.marine.fields import FieldSelection, FieldStore
from gweatherrouting.core.marine.models import utc_time
from gweatherrouting.core.marine.routing import (
    CurrentIsoRouter,
    CurrentRoutingEnvironment,
    FrozenRoutingWind,
)
from gweatherrouting.rendering.gshhs import GSHHSVectorChart


def compare(actual, expected):
    """Validate outside the timed engine calls."""
    a, b = actual.isochrones[-1], expected.isochrones[-1]
    assert len(a) == len(b), "Different frontier sizes"
    np.testing.assert_allclose(
        [p.pos for p in a], [p.pos for p in b], atol=1e-9, rtol=0
    )
    for x, y in zip(a, b):
        assert x.prev_idx == y.prev_idx, "Different parent links"
        assert abs((x.time - y.time).total_seconds()) <= 1e-5, "Different clocks"
        np.testing.assert_allclose(
            (x.twd, x.tws, x.speed, x.brg, x.next_wp_dist),
            (y.twd, y.tws, y.speed, y.brg, y.next_wp_dist),
            atol=1e-8,
            rtol=1e-8,
        )


def benchmark(args):  # noqa: C901
    fields_path = args.data_dir / "marine/fields"
    if not fields_path.is_dir():
        raise ValueError("Saved marine fields directory does not exist")
    fields = FieldStore(fields_path).snapshot().fields
    if not fields:
        raise ValueError("No selected current fields")
    gribs = [Grib.parse(str(path)) for path in args.grib]
    tracks = json.loads((args.data_dir / "track.json").read_text())["data"]["elements"]
    track = next(t for t in tracks if t["name"] == args.track)
    points = [p[:2] for p in track["points"]]
    shoreline = args.shoreline or args.data_dir / "gshhs/GSHHS_shp/h/GSHHS_h_L1.shp"
    modes = ("scalar", "arrays") if args.engine == "both" else (args.engine,)
    timings = {mode: [] for mode in modes}
    print(
        f"Wind: {len(gribs)} GRIB(s); currents: {len(fields)} selected fields; "
        f"coastline: {shoreline}",
        flush=True,
    )
    for repeat in range(args.repeats):
        results = {}
        for mode in (modes if repeat % 2 == 0 else tuple(reversed(modes))):
            wind = FrozenRoutingWind(SimpleNamespace(gribs=gribs))
            env = CurrentRoutingEnvironment(wind, FieldSelection(fields))
            provider = GSHHSVectorChart.__new__(GSHHSVectorChart)
            provider.lpvFile = ogr.Open(str(shoreline), 0)
            if provider.lpvFile is None:
                raise ValueError("Unable to open the shoreline dataset")

            def factory(*values, **kwargs):
                router = CurrentIsoRouter(*values, environment=env, **kwargs)
                router.use_numpy = mode == "arrays"
                return router

            routing = weatherrouting.Routing(
                factory,
                weatherrouting.Polar(str(args.polar)),
                points,
                wind,
                args.start,
                points_validity=provider.points_validity,
                lines_validity=provider.lines_validity,
            )
            elapsed = 0.0
            for step in range(args.steps):
                begin = perf_counter()
                result = routing.step()
                elapsed += perf_counter() - begin
                if routing.end:
                    break
            timings[mode].append(elapsed)
            results[mode] = result
            print(
                f"{mode}: {elapsed:.3f}s, {step + 1} steps, complete={routing.end}",
                flush=True,
            )
        if len(modes) == 2:
            compare(results["arrays"], results["scalar"])
    for mode in modes:
        print(f"{mode} median: {median(timings[mode]):.3f}s")
    if len(modes) == 2:
        print(
            f"Speedup vs scalar: {median(timings['scalar']) / median(timings['arrays']):.2f}x"
        )
        print("Frontier positions, parent links, clocks and motion values match.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--grib", type=Path, action="append", required=True)
    parser.add_argument("--polar", type=Path, required=True)
    parser.add_argument("--shoreline", type=Path)
    parser.add_argument("--track", default="track")
    parser.add_argument(
        "--start", type=lambda s: utc_time(datetime.fromisoformat(s)), required=True
    )
    parser.add_argument("--steps", type=int, default=15)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--engine", choices=("arrays", "scalar", "both"), default="arrays"
    )
    options = parser.parse_args()
    if options.steps < 1 or options.repeats < 1:
        parser.error("steps and repeats must be positive")
    benchmark(options)
