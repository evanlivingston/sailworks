"""E2a: current per-view portrayal from an immutable wider numerical field.

Standalone worker mechanism experiment, not a production cache or FPS test.
"""

import argparse
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
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
    "retained_staging_support",
    Path(__file__).with_name("experiment_staging_residency.py"),
)
support = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(support)
startup = support.startup


class RetainedWind:
    def __init__(self, points, staged, view, padding):
        import numpy as np

        self.points = np.array(points, copy=True)
        self.points.setflags(write=False)
        first = np.array(staged.first, copy=True)
        second = (
            first
            if staged.first is staged.second
            else np.array(staged.second, copy=True)
        )
        first.setflags(write=False)
        second.setflags(write=False)
        self.staged = replace(
            staged, first=first, second=second, sources=tuple(staged.sources)
        )
        self.view, self.padding = view, padding
        self.lookup = {
            (float(lat), float(lon)): i for i, (lat, lon) in enumerate(self.points)
        }
        self.last_result = self.last_coordinates = None

    def covers(self, view):
        original = self.view
        if (view.width, view.height, view._zoom) != (
            original.width,
            original.height,
            original._zoom,
        ):
            return False
        dx = original._lon_to_x(view._center_lon) - original._lon_to_x(
            original._center_lon
        )
        dy = original._lat_to_y(view._center_lat) - original._lat_to_y(
            original._center_lat
        )
        return abs(dx) <= self.padding and abs(dy) <= self.padding

    def sample(
        self, _sampler, stamp, lats, lons, cancel=lambda: False, byte_limit=16 << 20
    ):
        import numpy as np

        if cancel():
            return None
        if not self.staged.contains(stamp):
            raise ValueError("Time is outside retained source-selection interval")
        lats, lons = np.broadcast_arrays(lats, lons)
        if lats.ndim != 2 or not lats.size or lats.size * 16 > byte_limit:
            raise ValueError("Invalid or oversized retained query grid")
        try:
            ids = np.array(
                [
                    self.lookup[float(lat), float(lon)]
                    for lat, lon in zip(lats.ravel(), lons.ravel())
                ]
            )
        except KeyError as exc:
            raise ValueError("Native coordinate is outside retained field") from exc
        shape = lats.shape + (2,)
        first = self.staged.first.reshape(-1, 2)[ids].reshape(shape)
        second = (
            first
            if self.staged.first is self.staged.second
            else self.staged.second.reshape(-1, 2)[ids].reshape(shape)
        )
        first.setflags(write=False)
        second.setflags(write=False)
        result = replace(
            self.staged,
            first=first,
            second=second,
            sources=tuple(self.staged.sources[i] for i in ids),
        )
        if cancel():
            return None
        self.last_result, self.last_coordinates = result, (lats, lons)
        return result


@contextmanager
def retained_mode(barbs, sampler, field):
    """Make any numerical preparation fail hard, not just run from a warm cache."""
    old_stage, old_side = barbs.stage_wind, sampler._side
    old_arrays = [(index, index.array) for index in sampler.indices]

    def blocked(*_args, **_kwargs):
        raise AssertionError("Numerical preparation accessed during retained selection")

    barbs.stage_wind = field.sample
    sampler._side = blocked
    for index, _ in old_arrays:
        index.array = blocked
    try:
        yield
    finally:
        barbs.stage_wind, sampler._side = old_stage, old_side
        for index, original in old_arrays:
            index.array = original


def snapshot_identity(staged, coordinates):
    return dict(
        interval=[staged.before, staged.after],
        coordinates=startup.array_content_hash(coordinates),
        values=startup.array_content_hash((staged.first, staged.second)),
        sources=startup.digest(staged.sources),
    )


def atlas_identity(atlas):
    import hashlib

    return dict(
        count=atlas.count,
        shape=atlas.pair.shape,
        hashes=[
            hashlib.sha256(bytes(image.constBits())).hexdigest()
            for image in atlas.images
        ],
        bytes=atlas.nbytes,
    )


def probe(config):
    import resource

    def blocked(*_args, **_kwargs):
        raise RuntimeError("Network disabled in retained-field experiment")

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    import numpy as np
    from gweatherrouting.core.gribsampling import WeatherSampler
    from gweatherrouting.rendering.windrenderer import WindLandMask

    stage_root = Path(config["staging_root"])
    modules, hashes = {}, {}
    for suffix in (
        "qt/gpuweather",
        "rendering/weatherstaging",
        "rendering/weatherbarbs",
        "rendering/weathercache",
    ):
        path = stage_root / "gweatherrouting" / (suffix + ".py")
        hashes[suffix] = startup.protocol.file_hash(path)
        modules[suffix] = support.load_file(
            "gweatherrouting." + suffix.replace("/", "."), path
        )
    staging, barbs, cache_module = (
        modules[name]
        for name in (
            "rendering/weatherstaging",
            "rendering/weatherbarbs",
            "rendering/weathercache",
        )
    )
    data = Path(config["data"])
    opened = json.loads((data / "grib-manager.json").read_text())["opened"]
    if not opened or any(Path(name).name != name for name in opened):
        raise ValueError("Expected managed selected forecast filenames")
    from gweatherrouting.core.grib import Grib

    sources = []
    cache = cache_module.DecodedBandCache(128 * 1024 * 1024)
    land = WindLandMask()
    original_stage = barbs.stage_wind
    try:
        for name in opened:
            metadata = Grib.parse_metadata(str(data / "grib" / name))
            sources.append(
                cache_module.DisplayWeatherSource(
                    metadata.path,
                    metadata.name,
                    metadata.centre,
                    metadata.bounds,
                    metadata.start_time,
                    metadata.last_forecast,
                    metadata.model_name,
                    band_cache=cache,
                )
            )
        sampler = WeatherSampler(sources)
        if len(sampler.indices) != len(sources):
            raise ValueError("A selected source is unavailable")
        settings, padding = config["startup"], 256
        saved = settings["map"]

        def view(width, height, longitude):
            return barbs.BarbView(
                width,
                height,
                saved["latitude"],
                longitude,
                saved["zoom"],
                settings["barb_density"],
                settings["wind_over_land"],
            )

        width, height = config["viewport"]
        initial = view(width, height, saved["longitude"])
        wide = view(width + 2 * padding, height + 2 * padding, saved["longitude"])
        began = time.perf_counter()
        points, candidate_count = support.native_points(sampler, wide)
        stamp = datetime.fromisoformat(config["forecast_time"]).timestamp() + 1
        staged = staging.stage_wind(
            sampler, stamp, points[:, 0][None, :], points[:, 1][None, :]
        )
        if staged is None:
            raise ValueError("Wider numerical preparation returned no field")
        retained = RetainedWind(points, staged, initial, padding)
        del staged
        field_build_ms = (time.perf_counter() - began) * 1000
        captures = {}

        def remember(sampler, stamp, lats, lons, *args, **kwargs):
            result = original_stage(sampler, stamp, lats, lons, *args, **kwargs)
            captures["staged"], captures["coordinates"] = result, (lats, lons)
            return result

        rows = []
        offsets = (0, 16, 32, 64, 128, 0, -16, -64, 0)
        for repeat in range(3):
            for number, offset in enumerate(offsets):
                current = view(
                    width,
                    height,
                    initial._x_to_lon(initial._lon_to_x(initial._center_lon) + offset),
                )
                if not retained.covers(current):
                    raise ValueError("Pan escaped declared retained coverage")
                results, elapsed = {}, {}
                for mode in (
                    ("reference", "retained")
                    if (number + repeat) % 2 == 0
                    else ("retained", "reference")
                ):
                    started = time.perf_counter()
                    if mode == "reference":
                        barbs.stage_wind = remember
                        atlas = barbs.stage_barbs(sampler, stamp, current, land)
                        elapsed[mode] = (time.perf_counter() - started) * 1000
                        results[mode] = (
                            atlas_identity(atlas),
                            snapshot_identity(
                                captures["staged"], captures["coordinates"]
                            ),
                        )
                    else:
                        before = cache.statistics()
                        with retained_mode(barbs, sampler, retained):
                            atlas = barbs.stage_barbs(sampler, stamp, current, land)
                        elapsed[mode] = (time.perf_counter() - started) * 1000
                        after = cache.statistics()
                        if (
                            after["hits"] != before["hits"]
                            or after["misses"] != before["misses"]
                        ):
                            raise ValueError("Retained selection touched decoded bands")
                        results[mode] = (
                            atlas_identity(atlas),
                            snapshot_identity(
                                retained.last_result, retained.last_coordinates
                            ),
                        )
                if results["reference"] != results["retained"]:
                    raise ValueError(
                        "Retained field changed atlas, coordinates, vectors or provenance"
                    )
                rows.append(
                    dict(
                        repeat=repeat,
                        offset_px=offset,
                        symbols=atlas.count,
                        reference_ms=elapsed["reference"],
                        retained_ms=elapsed["retained"],
                        exact_atlas_and_staging_match=True,
                        atlas_identity=results["reference"][0],
                        staging_identity=results["reference"][1],
                    )
                )
        outside = view(
            width,
            height,
            initial._x_to_lon(initial._lon_to_x(initial._center_lon) + padding + 1),
        )
        if retained.covers(outside):
            raise ValueError("Coverage escape incorrectly accepted")
        return dict(
            protocol="retained-native-field-e2a-v1",
            field_build_ms=field_build_ms,
            native_candidates=candidate_count,
            retained_nodes=len(points),
            numerical_field_bytes=retained.points.nbytes
            + retained.staged.first.nbytes
            + retained.staged.second.nbytes,
            queries=rows,
            reference_summary=startup.protocol.summary(
                [r["reference_ms"] for r in rows]
            ),
            retained_summary=startup.protocol.summary([r["retained_ms"] for r in rows]),
            decoded_cache=cache.statistics(),
            module_hashes=hashes,
            outside_coverage_rejected=True,
            preparation_blocked_for_all_retained_queries=True,
            selected_sources=len(sources),
            process_high_water_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            gpu_rendering_tested=False,
            end_to_end_speedup_measured=False,
        )
    finally:
        barbs.stage_wind = original_stage
        for source in sources:
            source.close()
        land.contains.cache_clear()
        land._prepared.clear()
        land._tiles.clear()
        land._dataset = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--view-report", type=Path)
    parser.add_argument("--staging-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        result = probe(json.loads(args.worker.read_text()))
    else:
        if not args.snapshot or not args.view_report or not args.staging_root:
            parser.error("--snapshot, --view-report and --staging-root are required")
        manifest = startup.verify_snapshot(args.snapshot)
        report = json.loads(args.view_report.read_text())
        if (
            report["workload"]["snapshot_sha256"] != manifest["content_sha256"]
            or report["workload"]["forecast_time"] != manifest["forecast_time"]
        ):
            raise ValueError("View report and snapshot identity/clock differ")
        with tempfile.TemporaryDirectory(prefix="gwr-retained-weather-") as temporary:
            root = Path(temporary)
            data = startup.prepare_sandbox(args.snapshot, root)
            config = dict(
                data=str(data),
                staging_root=str(args.staging_root.resolve()),
                startup=report["runs"][0]["startup"],
                viewport=report["runs"][0]["actual_viewport"],
                forecast_time=manifest["forecast_time"],
            )
            configuration, output = root / "config.json", root / "result.json"
            configuration.write_text(json.dumps(config))
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
                    str(configuration),
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
            harness_sha256=startup.protocol.file_hash(__file__),
            revision=startup.protocol.revision(),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
