"""Benchmark the Python and native routing geometry backends.

Route a frozen forecast artifact with `weatherrouting.Routing` and the bundled
first317 polar, alternating backends to reduce cache/order bias, then verify the
paths agree. Also reports the four geometry calls as a microbenchmark.

Example:
    .venv/bin/python tools/benchmark_routing_geometry.py \
        --snapshot "$HOME/Library/Caches/sailworks-bench/snapshot-mini" \
        --route 48.0,-123.5,48.6,-122.8 --repeats 3
"""

import argparse
import json
import math
import random
import statistics
import time
from datetime import timedelta
from pathlib import Path

import weatherrouting

from sailworks import geometry
from sailworks.core.grib import Grib

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLAR = ROOT / "sailworks/data/polars/first317.pol"
DEFAULT_ROUTE = "48.0,-123.5,48.6,-122.8"


def active_artifact(snapshot):
    """Resolve the active tranche's first artifact from a snapshot directory."""
    analysis = Path(snapshot) / "data" / "forecast-analysis"
    active = json.loads((analysis / "active.json").read_text())
    manifest = json.loads(
        (analysis / "manifests" / f"{active['tranche_id']}.json").read_text()
    )
    entry = next(e for e in manifest["entries"] if e["entry_id"] == active["entry_id"])
    return analysis / entry["artifacts"][0]["path"]


def parse_route(value):
    values = [float(part) for part in value.split(",")]
    if len(values) != 4:
        raise argparse.ArgumentTypeError("route must be lat,lon,lat,lon")
    return (values[0], values[1]), (values[2], values[3])


def path_rows(route):
    return [
        (p.pos[0], p.pos[1], p.time, p.twd, p.tws, p.speed, p.brg) for p in route.path
    ]


def compare_rows(native, python):
    assert len(native) == len(python), "different path lengths"
    for a, b in zip(native, python):
        assert a[2] == b[2], "different timestamps"
        for index in (0, 1, 3, 4, 5, 6):
            assert math.isclose(
                a[index], b[index], abs_tol=1e-9
            ), f"path field {index} differs: {a[index]!r} vs {b[index]!r}"


def run_route(grib, polar, start, end, offset_hours, max_steps):
    router = weatherrouting.Routing(
        weatherrouting.list_routing_algorithms()[0]["class"],
        polar,
        [end],
        grib,
        grib.start_time + timedelta(hours=offset_hours),
        start_position=start,
    )
    begin = time.perf_counter()
    steps = 0
    while not router.end:
        router.step()
        steps += 1
        if steps > max_steps:
            raise RuntimeError(f"route did not finish in {max_steps} steps")
    return time.perf_counter() - begin, path_rows(router)


def microbenchmark(iterations):
    rng = random.Random(112)
    rows = []
    for _ in range(iterations):
        a = (rng.uniform(45.0, 49.0), rng.uniform(-126.0, -121.0))
        b = (a[0] + rng.uniform(-0.5, 0.5), a[1] + rng.uniform(-0.5, 0.5))
        rows.append((a, b, rng.uniform(0.1, 50.0), rng.uniform(-math.pi, math.pi)))

    timings = {}
    for backend in ("python", "native"):
        geometry.set_backend(backend)
        utils = weatherrouting.utils
        begin = time.perf_counter()
        for a, b, distance, heading in rows:
            utils.point_distance(*a, *b)
            utils.ortodromic(*a, *b)
            utils.lossodromic(*a, *b)
            utils.routage_point_distance(*a, distance, heading)
        timings[backend] = time.perf_counter() - begin
    return timings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--polar", type=Path, default=DEFAULT_POLAR)
    parser.add_argument("--route", type=parse_route, default=DEFAULT_ROUTE)
    parser.add_argument("--offsets", default="0", help="comma-separated start hours")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--micro-iterations", type=int, default=100_000)
    parser.add_argument("--max-steps", type=int, default=500)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")

    artifact = args.artifact or active_artifact(args.snapshot)
    grib = Grib.parse(str(artifact))
    polar = weatherrouting.Polar(str(args.polar))
    start, end = args.route
    offsets = [int(value) for value in args.offsets.split(",") if value.strip()]

    # Warm the forecast bands and both implementations before timing.
    for backend in ("python", "native"):
        geometry.set_backend(backend)
        run_route(grib, polar, start, end, offsets[0], args.max_steps)

    results = {backend: [] for backend in ("python", "native")}
    for repeat in range(args.repeats):
        order = ("python", "native") if repeat % 2 == 0 else ("native", "python")
        for backend in order:
            geometry.set_backend(backend)
            elapsed, rows = run_route(
                grib, polar, start, end, offsets[repeat % len(offsets)], args.max_steps
            )
            results[backend].append((elapsed, rows))
    compare_rows(results["native"][0][1], results["python"][0][1])

    python_median = statistics.median(row[0] for row in results["python"])
    native_median = statistics.median(row[0] for row in results["native"])
    print(f"artifact: {artifact}")
    print(f"route: {start} -> {end}; offsets: {offsets}")
    print(f"python median: {python_median:.3f}s")
    print(f"native median: {native_median:.3f}s")
    print(f"speedup: {python_median / native_median:.2f}x")

    micro = microbenchmark(args.micro_iterations)
    print(
        "microbenchmark "
        f"({args.micro_iterations} x 4 calls): "
        f"python {micro['python']:.3f}s, native {micro['native']:.3f}s, "
        f"{micro['python'] / micro['native']:.2f}x"
    )


if __name__ == "__main__":
    main()
