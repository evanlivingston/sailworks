"""Measure the scenario pool's process overhead with a synthetic CPU job.

This is a mechanism benchmark, not a routing-performance measurement.  It
replaces the route calculation with a bounded CPU loop so the serial and
spawned-process paths are compared on identical work.  Real routing remains
unchanged and must be measured separately.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
import json
import multiprocessing
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import weatherrouting  # noqa: E402

from sailworks.core import scenariopool  # noqa: E402
from sailworks.core.forecaststate import ForecastAnalysisState  # noqa: E402
from sailworks.core.grib import Grib  # noqa: E402
from sailworks.core.routingjob import (  # noqa: E402
    CalculatedRoute,
    RouteRequest,
    RoutingCancelledError,
)
from sailworks.core.routingoptions import (  # noqa: E402
    RouteScenario,
    scenario_request,
)
from sailworks.core.scenariopool import ScenarioJob  # noqa: E402

WORK_SECONDS = 1.0
_GRIB_CACHE = {}


def set_work(seconds):
    global WORK_SECONDS
    WORK_SECONDS = max(0.0, float(seconds))


def synthetic_calculate(core, request, binding, providers, cancel, progress, **options):
    """A deterministic CPU-bound stand-in for one route."""
    started = time.perf_counter()
    value = 0
    while time.perf_counter() - started < WORK_SECONDS:
        value = (value * 1103515245 + 12345) & 0x7FFFFFFF
    progress(50, ())
    progress(100, ())
    seed = sum(ord(character) for character in request.name)
    return CalculatedRoute(
        ((seed % 90, seed % 180),),
        (),
        {"schema_version": 1, "scenario": request.name, "work": "synthetic"},
    )


def install_synthetic(seconds):
    set_work(seconds)
    scenariopool.calculate_bound_route = synthetic_calculate


def real_calculate(core, request, binding, providers, cancel, progress, **options):
    """A real route through the benchmark's frozen artifact, without Qt."""
    path = str(binding.artifact_paths[0])
    grib = _GRIB_CACHE.get(path)
    if grib is None:
        grib = Grib.parse(path)
        _GRIB_CACHE[path] = grib
    routing = weatherrouting.Routing(
        request.algorithm,
        weatherrouting.Polar(request.polar_file),
        list(request.waypoints) or [request.destination],
        grib,
        start_datetime=request.departure,
        start_position=request.start,
    )
    progress(0, ())
    steps = 0
    while not routing.end:
        if cancel.is_set():
            raise RoutingCancelledError()
        routing.step()
        steps += 1
        if steps > 1000:
            raise RuntimeError("route did not finish in 1000 steps")
    rows = tuple(
        (
            point.pos[0],
            point.pos[1],
            point.time.isoformat(),
            point.twd,
            point.tws,
            point.speed,
            point.brg,
        )
        for point in routing.path
    )
    return CalculatedRoute(rows, (), {"scenario": request.name})


def install_real(_seconds):
    scenariopool.calculate_bound_route = real_calculate


def make_jobs(count):
    algorithm = weatherrouting.list_routing_algorithms()[0]["class"]
    jobs = []
    for index in range(count):
        request = RouteRequest(
            "scenario-%d" % index,
            (48.0, -123.5),
            (48.6, -122.8),
            datetime(2026, 11, 2, 18, tzinfo=timezone.utc),
            "first317.pol",
            algorithm,
        )
        jobs.append(ScenarioJob(index, request, binding=None))
    return jobs


def make_real_jobs(snapshot, count, offsets):
    analysis = Path(snapshot) / "data" / "forecast-analysis"
    binding = ForecastAnalysisState(analysis).snapshot(required_fields=("wind",))
    algorithm = weatherrouting.list_routing_algorithms()[0]["class"]
    base = RouteRequest(
        "scenario",
        (48.0, -123.5),
        (48.6, -122.8),
        binding.entry.run_time,
        str(ROOT / "sailworks/data/polars/first317.pol"),
        algorithm,
    )
    jobs = []
    for index in range(count):
        offset = offsets[index % len(offsets)]
        request = scenario_request(base, RouteScenario(time_offset_hours=offset))
        jobs.append(ScenarioJob(index, request, binding))
    return jobs, binding


def run_serial(jobs, initializer, initargs):
    previous = scenariopool.calculate_bound_route
    initializer(*initargs)
    try:
        return [scenariopool.run_scenario(job) for job in jobs]
    finally:
        scenariopool.calculate_bound_route = previous


def run_parallel(jobs, workers, initializer, initargs):
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=min(workers, len(jobs)),
        mp_context=ctx,
        initializer=initializer,
        initargs=initargs,
    ) as pool:
        futures = [pool.submit(scenariopool.run_scenario, job) for job in jobs]
        return [future.result() for future in futures]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenarios", type=int, default=4, help="number of independent jobs"
    )
    parser.add_argument("--workers", type=int, default=4, help="process cap")
    parser.add_argument(
        "--work-seconds", type=float, default=1.0, help="CPU work per scenario"
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="real routing workload snapshot instead of synthetic",
    )
    parser.add_argument(
        "--offsets", default="0,3,6,9", help="real-mode departure offsets in hours"
    )
    parser.add_argument("--output", type=Path, help="write the result JSON here")
    args = parser.parse_args(argv)

    if args.snapshot:
        offsets = [
            float(value) for value in args.offsets.split(",") if value.strip()
        ] or [0.0]
        jobs, _binding = make_real_jobs(args.snapshot, max(1, args.scenarios), offsets)
        initializer = install_real
        initargs = (args.work_seconds,)
        workload = "real routing engine over a frozen forecast artifact"
    else:
        jobs = make_jobs(max(1, args.scenarios))
        initializer = install_synthetic
        initargs = (args.work_seconds,)
        workload = "synthetic CPU-bound scenario placeholder"

    started = time.perf_counter()
    serial = run_serial(jobs, initializer, initargs)
    serial_seconds = time.perf_counter() - started

    started = time.perf_counter()
    parallel = run_parallel(jobs, max(1, args.workers), initializer, initargs)
    parallel_seconds = time.perf_counter() - started

    serial_results = [outcome.result for outcome in serial]
    parallel_results = [outcome.result for outcome in parallel]
    report = {
        "workload": workload,
        "scenarios": len(jobs),
        "workers": min(max(1, args.workers), len(jobs)),
        "serial_seconds": serial_seconds,
        "parallel_seconds": parallel_seconds,
        "reduction_pct": 100.0
        * (serial_seconds - parallel_seconds)
        / max(serial_seconds, 1e-9),
        "results_identical": serial_results == parallel_results,
        "failures": [outcome.failure for outcome in parallel if outcome.failure],
    }
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    return 0 if report["results_identical"] and not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
