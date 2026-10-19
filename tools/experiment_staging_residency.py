"""E3b: run PR #50 staging read-only under the experiment-only band budget."""

import argparse
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


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


residency = load_file(
    "staging_residency_support",
    Path(__file__).with_name("experiment_weather_residency.py"),
)
startup = residency.startup


def check_values(staged, sampler, stamp, points):
    import numpy as np

    reference = sampler.sample_timestamps("wind", stamp, points[:, 0], points[:, 1])
    weight = np.float32(staged.weight(stamp))
    actual = staged.first[0] + (staged.second[0] - staged.first[0]) * weight
    if not np.array_equal(np.isfinite(actual), np.isfinite(reference)):
        raise ValueError("Staged/reference finite masks differ")
    if tuple(staged.sources) != tuple(sampler.point_sources):
        raise ValueError("Staged/reference per-point provenance differs")
    finite = np.isfinite(reference)
    maximum = float(np.max(np.abs(actual[finite] - reference[finite]), initial=0))
    # Display-only float32 transport is already the PR's declared approximation.
    if maximum > 2e-5:
        raise ValueError(f"Staged float32 error exceeds 2e-5 m/s: {maximum}")
    return dict(
        max_component_error_mps=maximum,
        usable_points=int(np.isfinite(reference).all(axis=1).sum()),
        points=len(points),
        masks_match=True,
        provenance_match=True,
    )


def native_points(sampler, view):
    import numpy as np

    cx, cy = view._lon_to_x(view._center_lon), view._lat_to_y(view._center_lat)
    bounds = (
        (
            view._y_to_lat(cy + view.height / 2 + 32),
            view._x_to_lon(cx - view.width / 2 - 32),
        ),
        (
            view._y_to_lat(cy - view.height / 2 - 32),
            view._x_to_lon(cx + view.width / 2 + 32),
        ),
    )
    nodes, count = [], 0
    for index in sampler.indices:
        if not any(record.element in ("UGRD", "VGRD") for record in index.records):
            continue
        candidates = index.nodes(bounds)
        count += len(candidates)
        if count > 262144:
            raise ValueError("Native-node union exceeds PR #50 candidate budget")
        nodes.append(candidates)
    if not nodes:
        raise ValueError("No native wind grids")
    points = np.unique(np.concatenate(nodes), axis=0)
    if not len(points):
        raise ValueError("No native wind nodes in view")
    points = points[np.lexsort((points[:, 0], points[:, 1]))]
    return points, count


def probe(config):
    import resource

    def blocked(*_args, **_kwargs):
        raise RuntimeError("Network disabled in staging experiment")

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    import PySide6
    from osgeo import gdal
    from gweatherrouting.core.grib import Grib
    from gweatherrouting.core.gribsampling import WeatherSampler
    from gweatherrouting.rendering.windrenderer import WindLandMask

    root = Path(config["staging_root"])
    modules = {}
    hashes = {}
    for suffix in (
        "qt/gpuweather",
        "rendering/weatherstaging",
        "rendering/weatherbarbs",
    ):
        path = root / "gweatherrouting" / (suffix + ".py")
        hashes[suffix] = startup.protocol.file_hash(path)
        modules[suffix] = load_file("gweatherrouting." + suffix.replace("/", "."), path)
    staging = modules["rendering/weatherstaging"]
    barbs = modules["rendering/weatherbarbs"]
    data = Path(config["data"])
    opened = json.loads((data / "grib-manager.json").read_text())["opened"]
    if not opened or any(Path(name).name != name for name in opened):
        raise ValueError("Expected managed selected forecast filenames")
    sources, installed = [], []
    budget = residency.BandBudget(128 * 1024 * 1024)
    land = WindLandMask()
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
        for slot, index in enumerate(sampler.indices):
            installed.append((index, *residency.install_budget(index, budget, slot)))
        settings = config["startup"]
        saved = settings["map"]
        view = barbs.BarbView(
            *config["viewport"],
            saved["latitude"],
            saved["longitude"],
            saved["zoom"],
            settings["barb_density"],
            settings["wind_over_land"],
        )
        points, count = native_points(sampler, view)
        initialization_ms = (time.perf_counter() - began) * 1000
        stamp = datetime.fromisoformat(config["forecast_time"]).timestamp()
        rows = []
        for seconds in (0, 1, 900):
            started = time.perf_counter()
            staged = staging.stage_wind(
                sampler, stamp + seconds, points[:, 0][None, :], points[:, 1][None, :]
            )
            if staged is None:
                raise ValueError("Staging returned no field")
            differential = check_values(staged, sampler, stamp + seconds, points)
            atlas = barbs.stage_barbs(sampler, stamp + seconds, view, land)
            if atlas is None or atlas.count <= 0:
                raise ValueError("No staged visible barbs")
            if atlas.nbytes > 32 * 1024 * 1024:
                raise ValueError("Atlas exceeds PR #50 image budget")
            rows.append(
                dict(
                    seconds=seconds,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    symbols=atlas.count,
                    atlas_image_bytes=atlas.nbytes,
                    staged_uv_bytes=staged.first.nbytes
                    + (0 if staged.first is staged.second else staged.second.nbytes),
                    atlas_uv_bytes=atlas.staged.first.nbytes
                    + (
                        0
                        if atlas.staged.first is atlas.staged.second
                        else atlas.staged.second.nbytes
                    ),
                    retained_decoded_bytes=budget.bytes,
                    **differential,
                )
            )
        return dict(
            protocol="pr50-staging-residency-e3b-v1",
            selected_sources=len(sources),
            native_candidates=count,
            unique_native_nodes=len(points),
            potential_band_cache_bytes=potential,
            limit_bytes=budget.limit,
            peak_retained_bytes=budget.peak,
            loads=budget.loads,
            hits=budget.hits,
            global_evictions=budget.evictions,
            initialization_ms=initialization_ms,
            staging_module_hashes=hashes,
            queries=rows,
            qt=PySide6.__version__,
            gdal=gdal.VersionInfo("RELEASE_NAME"),
            process_high_water_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            total_memory_bound_proven=False,
            gpu_rendering_tested=False,
        )
    finally:
        for index, original_array, original_cache in installed:
            index.array, index.arrays = original_array, original_cache
        budget.entries.clear()
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
        view_report = json.loads(args.view_report.read_text())
        if (
            view_report["workload"]["snapshot_sha256"] != manifest["content_sha256"]
            or view_report["workload"]["forecast_time"] != manifest["forecast_time"]
        ):
            raise ValueError("View report and snapshot identity/clock differ")
        run = view_report["runs"][0]
        with tempfile.TemporaryDirectory(prefix="gwr-staging-residency-") as temporary:
            root = Path(temporary)
            data = startup.prepare_sandbox(args.snapshot, root)
            config = dict(
                data=str(data),
                staging_root=str(args.staging_root.resolve()),
                forecast_time=manifest["forecast_time"],
                startup=run["startup"],
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
            harness_sha256=startup.protocol.file_hash(__file__),
            residency_helper_sha256=startup.protocol.file_hash(
                Path(__file__).with_name("experiment_weather_residency.py")
            ),
            revision=startup.protocol.revision(),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
