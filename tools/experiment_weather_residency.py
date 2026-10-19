"""E3a: private-snapshot, display-only decoded-band residency experiment.

No shared sampler implementation is changed. A retained NumPy-array limit is
not a bound on GDAL, decode temporaries, live caller references, or process RSS.
"""

import argparse
from collections import OrderedDict
from collections.abc import MutableMapping
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "startup_benchmark", Path(__file__).with_name("benchmark_startup_rendering.py")
)
startup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(startup)


class ResidencyLimitExceeded(Exception):
    """Not a recoverable sampler error: never turn budget failure into fallback."""


class BandBudget:
    """Shared LRU retaining full decoded bands; used by the probe only."""

    def __init__(self, limit, evict=True):
        self.limit, self.evict = limit, evict
        self.entries = OrderedDict()
        self.bytes = self.peak = self.hits = self.loads = self.evictions = 0

    def reserve(self, key, size):
        if key in self.entries:
            self.hits += 1
            return
        if size > self.limit:
            raise ResidencyLimitExceeded("A decoded band exceeds the experiment budget")
        while self.bytes + size > self.limit:
            if not self.evict:
                raise ResidencyLimitExceeded(
                    "Reference retention reached its safety stop"
                )
            _, array = self.entries.popitem(last=False)
            self.bytes -= array.nbytes
            self.evictions += 1
        self.loads += 1

    def put(self, key, value):
        if key in self.entries:
            self.bytes -= self.entries.pop(key).nbytes
        if self.bytes + value.nbytes > self.limit:
            raise ResidencyLimitExceeded(
                "Decoded array size exceeded its reserved budget"
            )
        self.entries[key] = value
        self.bytes += value.nbytes
        self.peak = max(self.peak, self.bytes)


class SourceCache(MutableMapping):
    """OrderedDict-compatible per-source view of the shared residency ledger."""

    def __init__(self, budget, source):
        self.budget, self.source = budget, source

    def __getitem__(self, band):
        return self.budget.entries[self.source, band]

    def __setitem__(self, band, value):
        self.budget.put((self.source, band), value)

    def __delitem__(self, band):
        self.budget.bytes -= self.budget.entries.pop((self.source, band)).nbytes

    def __iter__(self):
        return iter(
            [band for source, band in self.budget.entries if source == self.source]
        )

    def __len__(self):
        return sum(source == self.source for source, _ in self.budget.entries)

    def move_to_end(self, band):
        self.budget.entries.move_to_end((self.source, band))

    def popitem(self, last=True):
        keys = list(self)
        if not keys:
            raise KeyError("empty source cache")
        band = keys[-1] if last else keys[0]
        value = self[band]
        del self[band]
        return band, value


def install_budget(index, budget, source_id):
    if index.arrays:
        raise ValueError("Residency probe requires initially cold band caches")
    original_array, original_cache = index.array, index.arrays
    index.arrays = SourceCache(budget, source_id)

    def array(record):
        budget.reserve((source_id, record.band), index.width * index.height * 8)
        return original_array(record)

    index.array = array
    return original_array, original_cache


def value_identity(values, provenance):
    import numpy as np

    values = np.asarray(values, dtype="<f8")
    finite = np.isfinite(values)
    # Canonicalize non-finite payload bits; compare their class separately.
    canonical = np.where(finite, values, 0).astype("<f8")
    classes = np.where(
        np.isnan(values),
        1,
        np.where(np.isposinf(values), 2, np.where(np.isneginf(values), 3, 0)),
    ).astype("u1")
    return dict(
        values_sha256=hashlib.sha256(canonical.tobytes()).hexdigest(),
        missing_sha256=hashlib.sha256(classes.tobytes()).hexdigest(),
        provenance_sha256=startup.digest(list(provenance)),
        usable_points=int(finite.all(axis=1).sum()),
        points=len(values),
    )


def query_points(view, width, height):
    import numpy as np
    from gweatherrouting.core.mapviewport import MercatorProjection

    class View(MercatorProjection):
        _center_lat = view["latitude"]
        _center_lon = view["longitude"]
        _zoom = view["zoom"]

        def get_allocated_width(self):
            return width

        def get_allocated_height(self):
            return height

    projection = View()
    grid = np.linspace(0, 1, 33)
    points = [
        projection.convert_screen_to_geographic(x * width, y * height).get_degrees()
        for y in grid
        for x in grid
    ]
    return np.asarray(points)


def probe(config):
    import resource

    def blocked(*_args, **_kwargs):
        raise RuntimeError("Network disabled in residency experiment")

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    import numpy as np
    from osgeo import gdal
    from gweatherrouting.core.grib import Grib
    from gweatherrouting.core.gribsampling import WeatherSampler

    # Avoid reading account/instrument configuration or constructing a GUI.
    data = Path(config["data"])
    opened = json.loads((data / "grib-manager.json").read_text())["opened"]
    if not opened or any(Path(name).name != name for name in opened):
        raise ValueError("Expected saved managed forecast filenames")
    sources = []
    installed = []
    budget = BandBudget(config["limit_bytes"], evict=config["mode"] == "bounded")
    began = time.perf_counter()
    try:
        for name in opened:
            sources.append(Grib.parse(str(data / "grib" / name)))
        sampler = WeatherSampler(sources)
        if len(sampler.indices) != len(sources):
            raise ValueError("A selected source is unavailable")
        potential = sum(
            index.width * index.height * 8 * min(6, index.dataset.RasterCount)
            for index in sampler.indices
        )
        for number, index in enumerate(sampler.indices):
            installed.append((index, *install_budget(index, budget, number)))
        initialization_ms = (time.perf_counter() - began) * 1000
        points = query_points(config["view"], *config["viewport"])
        # Exact instants, including an interior second; never round forecast time.
        stamp = datetime.fromisoformat(config["forecast_time"]).timestamp()
        queries = []
        for seconds in (0, 1, 900):
            for shift in (0.0, 0.1, 0.0):
                lats, lons = points[:, 0], points[:, 1] + shift
                started = time.perf_counter()
                values = sampler.sample_timestamps("wind", stamp + seconds, lats, lons)
                queries.append(
                    dict(
                        seconds=seconds,
                        longitude_shift=shift,
                        elapsed_ms=(time.perf_counter() - started) * 1000,
                        retained_bytes=budget.bytes,
                        **value_identity(values, sampler.point_sources),
                    )
                )
        return dict(
            protocol="weather-residency-e3a-v1",
            mode=config["mode"],
            selected_sources=len(sources),
            potential_band_cache_bytes=potential,
            would_fail_pr50_guard=potential > 128 * 1024 * 1024,
            limit_bytes=budget.limit,
            peak_retained_bytes=budget.peak,
            loads=budget.loads,
            hits=budget.hits,
            global_evictions=budget.evictions,
            initialization_ms=initialization_ms,
            queries=queries,
            process_high_water_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            gdal_cache_used_bytes=gdal.GetCacheUsed(),
            total_memory_bound_proven=False,
        )
    finally:
        for index, original_array, original_cache in installed:
            index.array = original_array
            index.arrays = original_cache
        budget.entries.clear()
        for source in sources:
            source.close()


def compare(reference, candidate):
    if reference.get("mode") != "reference" or candidate.get("mode") != "bounded":
        raise ValueError("Expected reference and bounded reports, in that order")
    if not reference["queries"] or not any(
        q["usable_points"] for q in reference["queries"]
    ):
        raise ValueError("No usable reference weather")
    if candidate["peak_retained_bytes"] > candidate["limit_bytes"]:
        raise ValueError("Candidate exceeded its retention budget")
    for key in (
        "snapshot_sha256",
        "forecast_time",
        "view",
        "viewport",
        "harness_sha256",
    ):
        if reference[key] != candidate[key]:
            raise ValueError(f"Incompatible {key}")
    if reference["selected_sources"] != candidate["selected_sources"]:
        raise ValueError("Selected source count changed")
    if len(reference["queries"]) != len(candidate["queries"]):
        raise ValueError("Mismatched queries")
    for a, b in zip(reference["queries"], candidate["queries"]):
        for key in (
            "seconds",
            "longitude_shift",
            "values_sha256",
            "missing_sha256",
            "provenance_sha256",
            "usable_points",
            "points",
        ):
            if a[key] != b[key]:
                raise ValueError(f"Query mismatch: {key}")
    return dict(status="passed", matched_queries=len(reference["queries"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--view-report", type=Path)
    parser.add_argument("--mode", choices=("reference", "bounded"), default="bounded")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", nargs=2, type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        result = probe(json.loads(args.worker.read_text()))
    elif args.compare:
        result = compare(*(json.loads(p.read_text()) for p in args.compare))
    else:
        if not args.snapshot or not args.view_report:
            parser.error("--snapshot and --view-report are required")
        manifest = startup.verify_snapshot(args.snapshot)
        view_report = json.loads(args.view_report.read_text())
        if (
            view_report["workload"]["snapshot_sha256"] != manifest["content_sha256"]
            or view_report["workload"]["forecast_time"] != manifest["forecast_time"]
        ):
            raise ValueError("View report and snapshot identity/clock differ")
        run = view_report["runs"][0]
        with tempfile.TemporaryDirectory(prefix="gwr-residency-") as temporary:
            root = Path(temporary)
            data = startup.prepare_sandbox(args.snapshot, root)
            config = dict(
                data=str(data),
                mode=args.mode,
                limit_bytes=(128 if args.mode == "bounded" else 512) * 1024 * 1024,
                forecast_time=manifest["forecast_time"],
                view=run["startup"]["map"],
                viewport=run["actual_viewport"],
            )
            config_path, output = root / "config.json", root / "result.json"
            config_path.write_text(json.dumps(config))
            env = dict(
                os.environ,
                HOME=str(root / "home"),
                XDG_DATA_HOME=str(root / "xdg-data"),
                XDG_CONFIG_HOME=str(root / "xdg-config"),
                XDG_CACHE_HOME=str(root / "xdg-cache"),
                PYTHONPATH=str(ROOT),
            )
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(config_path),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                env=env,
                check=True,
                timeout=110,
            )
            result = json.loads(output.read_text())
        result.update(
            snapshot_sha256=manifest["content_sha256"],
            forecast_time=manifest["forecast_time"],
            view=config["view"],
            viewport=config["viewport"],
            harness_sha256=startup.protocol.file_hash(__file__),
            revision=startup.protocol.revision(),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
