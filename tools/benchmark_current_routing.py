"""Compare scalar and NumPy routing locally; no downloads or credentials needed.

Run from the repository: PYTHONPATH=. .venv/bin/python tools/benchmark_current_routing.py
Optionally pass --grib /path/to/forecast.grib2 covering 49.3N, 123.8W.
The current field and six-knot boat are synthetic; coastline checks are excluded.
"""

import argparse
from datetime import datetime, timedelta, timezone
from statistics import median
from time import perf_counter
from types import SimpleNamespace

import numpy as np

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.marine.fields import CurrentField, FieldSelection
from gweatherrouting.core.marine.models import canonical_json
from gweatherrouting.core.marine.routing import (
    CurrentIsoRouter,
    CurrentRoutingEnvironment,
    FrozenRoutingWind,
)


def benchmark(grib_path=None, repeats=3, steps=2):
    grib = Grib.parse(grib_path) if grib_path else None
    start = (
        grib.start_time.astimezone(timezone.utc)
        if grib
        else datetime(2026, 10, 11, tzinfo=timezone.utc)
    )
    hours = max(2, steps // 6 + 2)
    field = CurrentField(
        canonical_json(
            dict(
                schema_version=1,
                provider="benchmark",
                model="synthetic",
                composition="total",
                vector_reference="true_east_north",
                sources=[],
                resolution_m=500,
                run=start.isoformat(),
            )
        ),
        np.array([(start + timedelta(hours=i)).timestamp() for i in range(hours)]),
        np.array([48.0, 50.0]),
        np.array([-125.0, -122.0]),
        np.full((hours, 2, 2), 0.2),
        np.zeros((hours, 2, 2)),
    )
    timings = {False: [], True: []}
    results = {}
    for repeat in range(repeats):
        # Alternate order to reduce cache/order bias.
        for use_numpy in ((False, True) if repeat % 2 == 0 else (True, False)):
            wind = (
                FrozenRoutingWind(SimpleNamespace(gribs=[grib]))
                if grib
                else SimpleNamespace(
                    get_wind_at=lambda *_: (0, 10), last_source="synthetic"
                )
            )
            router = CurrentIsoRouter(
                SimpleNamespace(get_speed=lambda *_: 6),
                wind,
                environment=CurrentRoutingEnvironment(wind, FieldSelection([field])),
            )
            router.use_numpy = use_numpy
            result = None
            begin = perf_counter()
            for _ in range(steps):
                result = router.route(result, start, 1, (49.3, -123.8), (49.5, -123.2))
                if result.path:
                    break
            timings[use_numpy].append(perf_counter() - begin)
            results[use_numpy] = result
    scalar, batch = (results[k].isochrones[-1] for k in (False, True))
    np.testing.assert_allclose(
        [p.pos for p in batch], [p.pos for p in scalar], atol=1e-10, rtol=0
    )
    a, b = median(timings[False]), median(timings[True])
    print(f"Scalar median: {a:.3f}s; NumPy median: {b:.3f}s; speedup: {a / b:.2f}×")
    print(f"Frontier positions match within 1e-10 degrees ({len(batch)} candidates).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grib")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--steps", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1 or args.steps < 1:
        parser.error("repeats and steps must be positive")
    benchmark(args.grib, args.repeats, args.steps)
