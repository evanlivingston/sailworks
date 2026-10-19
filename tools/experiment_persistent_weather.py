"""E2c: persistent native field plus exact per-view selection indirection.

Disposable mechanism probe. Not an application renderer or performance benchmark.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "persistent_retained_support",
    Path(__file__).with_name("experiment_retained_weather.py"),
)
support = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(support)
startup = support.startup
LIMIT = 16 * 1024 * 1024


def frozen(values):
    result = np.array(values, dtype="<f4", copy=True)
    result.setflags(write=False)
    return result


@dataclass(frozen=True)
class Field:
    generation: str
    values: np.ndarray
    node_count: int

    @classmethod
    def create(cls, generation, first, second):
        a, b = np.asarray(first).reshape(-1, 2), np.asarray(second).reshape(-1, 2)
        if not generation or a.shape != b.shape or not 0 < len(a) <= 262144:
            raise ValueError("Invalid persistent field")
        if any(np.any(np.linalg.norm(v, axis=1) > 250) for v in (a, b)):
            raise ValueError("Unbounded wind vector")
        # Bounded width avoids assuming the GPU supports a very long 1D texture.
        values = np.full((math.ceil(len(a) / 64), 64, 4), np.nan, dtype="<f4")
        values.reshape(-1, 4)[: len(a)] = np.concatenate((a, b), axis=1)
        return cls(generation, frozen(values), len(a))


@dataclass(frozen=True)
class Selection:
    generation: str
    values: np.ndarray


def validate_binding(field, selection, byte_limit=LIMIT):
    if field.generation != selection.generation:
        raise ValueError("Stale field/selection generation")
    values = selection.values
    if values.ndim != 3 or values.shape[2] != 4 or not values.size:
        raise ValueError("Invalid selection texture")
    ids = values[..., 0]
    if not np.isfinite(values).all() or np.any(ids != np.floor(ids)):
        raise ValueError("Nonfinite or fractional selection")
    if np.any(ids < -1) or np.any(ids >= field.node_count):
        raise ValueError("Selection index outside field")
    if np.any((values[..., 3] < 0) | (values[..., 3] > 1)):
        raise ValueError("Invalid exact-time blend")
    # Two generations' field + selection + RGBA32F validation output overlap.
    reserved = 2 * (field.values.nbytes + 2 * values.nbytes)
    if reserved > byte_limit:
        raise ValueError("Persistent texture reservation exceeds limit")
    return reserved


def reconstruct(field, selection, endpoint=None):
    validate_binding(field, selection)
    ids = selection.values[..., 0].astype(int)
    vectors = field.values.reshape(-1, 4)[np.maximum(ids, 0)]
    a, b = vectors[..., :2], vectors[..., 2:]
    w = selection.values[..., 3:]
    if endpoint is not None:
        uv = (a if endpoint == 0 else b).copy()
    else:
        with np.errstate(invalid="ignore"):
            uv = np.where(w <= 0, a, np.where(w >= 1, b, a + (b - a) * w))
    uv[ids < 0] = np.nan
    return np.concatenate((uv, selection.values[..., 1:3]), axis=-1)


def select(field, retained, stamp, view, candidates, land, density_parameters, project):
    """Current portrayal only; no sampler/index, encoding or endpoint resampling."""
    if not retained.covers(view) or not retained.staged.contains(stamp):
        raise ValueError("Coverage or interval escape")
    step, cap = density_parameters(view.density)
    if step <= 0:
        raise ValueError("Unthinned selection is unsupported")
    cols = math.ceil((view.width + 64) / step) + 1
    rows = math.ceil((view.height + 64) / step) + 1
    if cols * rows > 8192:
        raise ValueError("Selection grid exceeds budget")
    points = np.asarray(candidates)
    node_ids = np.array(
        [retained.lookup[float(lat), float(lon)] for lat, lon in points]
    )
    a = retained.staged.first.reshape(-1, 2)[node_ids]
    b = retained.staged.second.reshape(-1, 2)[node_ids]
    w = retained.staged.weight(stamp)
    uv = a + (b - a) * w
    xx, yy = project(
        points[:, 0],
        points[:, 1],
        view._center_lat,
        view._center_lon,
        view._zoom,
        view.width,
        view.height,
    )
    visible = (
        (xx >= -32)
        & (xx <= view.width + 32)
        & (yy >= -32)
        & (yy <= view.height + 32)
        & np.isfinite(uv).all(axis=1)
        & np.isfinite(xx)
        & np.isfinite(yy)
    )
    ids = np.flatnonzero(visible)
    cells = np.floor((yy[ids] + 32) / step).astype(int) * cols + np.floor(
        (xx[ids] + 32) / step
    ).astype(int)
    _, first = np.unique(cells, return_index=True)
    chosen = np.sort(first)
    if cap is not None and len(chosen) > cap:
        chosen = chosen[np.linspace(0, len(chosen) - 1, cap, dtype=int)]
    if not view.over_land:
        land.refresh_source(view.get_scale())
        chosen = chosen[
            np.array([not land.contains(*points[ids[i]]) for i in chosen], dtype=bool)
        ]
    selected, slots = ids[chosen], cells[chosen]
    gy, gx = np.mgrid[:rows, :cols]
    values = np.empty((rows, cols, 4), dtype="<f4")
    values[..., 0] = -1
    values[..., 1] = gx * step - 32
    values[..., 2] = gy * step - 32
    values[..., 3] = w
    values.reshape(-1, 4)[slots, 0] = node_ids[selected]
    values.reshape(-1, 4)[slots, 1] = xx[selected]
    values.reshape(-1, 4)[slots, 2] = yy[selected]
    result = Selection(field.generation, frozen(values))
    validate_binding(field, result)
    return result


def prepare(config, output):
    """Offline private worker: reference oracle and numerical preparation only."""

    def blocked(*_args, **_kwargs):
        raise RuntimeError("Network disabled in persistent-field probe")

    socket.socket.connect = socket.socket.connect_ex = blocked
    from gweatherrouting.core.grib import Grib
    from gweatherrouting.core.gribsampling import WeatherSampler
    from gweatherrouting.rendering.windrenderer import WindLandMask

    app_root = Path(config["app_root"])
    modules = {}
    for name in (
        "qt/gpuweather",
        "rendering/weatherstaging",
        "rendering/weatherbarbs",
        "rendering/weathercache",
    ):
        modules[name] = support.support.load_file(
            "gweatherrouting." + name.replace("/", "."),
            app_root / "gweatherrouting" / (name + ".py"),
        )
    barbs = modules["rendering/weatherbarbs"]
    transport = modules["qt/gpuweather"]
    cache_module = modules["rendering/weathercache"]
    cache = cache_module.DecodedBandCache(128 * 1024 * 1024)
    sources, land = [], WindLandMask()
    try:
        data = Path(config["data"])
        opened = json.loads((data / "grib-manager.json").read_text())["opened"]
        if not opened or any(Path(name).name != name for name in opened):
            raise ValueError("Invalid source list")
        for name in opened:
            m = Grib.parse_metadata(str(data / "grib" / name))
            sources.append(
                cache_module.DisplayWeatherSource(
                    m.path,
                    m.name,
                    m.centre,
                    m.bounds,
                    m.start_time,
                    m.last_forecast,
                    m.model_name,
                    band_cache=cache,
                )
            )
        sampler = WeatherSampler(sources)
        if len(sampler.indices) != len(sources):
            raise ValueError("Selected source unavailable")
        saved = config["startup"]
        nav = saved["map"]
        width, height = config["viewport"]
        initial = barbs.BarbView(
            width,
            height,
            nav["latitude"],
            nav["longitude"],
            nav["zoom"],
            saved["barb_density"],
            saved["wind_over_land"],
        )
        from dataclasses import replace

        points, _ = support.support.native_points(
            sampler, replace(initial, width=width + 512, height=height + 512)
        )
        stamp = datetime.fromisoformat(config["forecast_time"]).timestamp() + 1
        staged = modules["rendering/weatherstaging"].stage_wind(
            sampler, stamp, points[:, 0][None, :], points[:, 1][None, :]
        )
        if staged is None:
            raise ValueError("Field preparation rejected")
        retained = support.RetainedWind(points, staged, initial, 256)
        generation = startup.digest(
            dict(
                payload=startup.array_content_hash(
                    (points, staged.first, staged.second)
                ),
                sources=staged.sources,
                before=staged.before,
                after=staged.after,
                snapshot=config["snapshot_sha256"],
            )
        )
        field = Field.create(generation, staged.first, staged.second)
        arrays = {"field": field.values}
        rows = []
        original_stage = barbs.stage_wind
        for i, dx in enumerate((0, 16, 32, 64, 128, 0, -16, -64, 0)):
            view = replace(
                initial,
                _center_lon=initial._x_to_lon(
                    initial._lon_to_x(initial._center_lon) + dx
                ),
            )
            captures = {}

            def remember(sampler, stamp, lats, lons, *args, **kwargs):
                captures["points"] = np.column_stack((lats.ravel(), lons.ravel()))
                return original_stage(sampler, stamp, lats, lons, *args, **kwargs)

            barbs.stage_wind = remember
            try:
                reference = barbs.stage_barbs(sampler, stamp, view, land)
            finally:
                barbs.stage_wind = original_stage
            if reference is None:
                raise ValueError("Reference atlas rejected")
            # Candidate membership must be metadata-only; use the unchanged node
            # enumerator and verify it agrees with the reference's sample queries.
            candidates, _ = support.support.native_points(sampler, view)
            if not np.array_equal(candidates, captures["points"]):
                raise ValueError("Native candidate membership changed")
            with support.retained_mode(barbs, sampler, retained):
                selection = select(
                    field,
                    retained,
                    stamp,
                    view,
                    candidates,
                    land,
                    barbs.barb_density_parameters,
                    barbs.mercator_project_flat,
                )
            decoded = [transport.decode_component(image) for image in reference.images]
            expected_first = np.stack(
                (decoded[0], decoded[1], decoded[4], decoded[5]), axis=-1
            )
            expected_second = np.stack(
                (decoded[2], decoded[3], decoded[4], decoded[5]), axis=-1
            )
            if not np.array_equal(
                reconstruct(field, selection, 0), expected_first, equal_nan=True
            ) or not np.array_equal(
                reconstruct(field, selection, 1), expected_second, equal_nan=True
            ):
                raise ValueError("Selection changed endpoint values/masks/anchors")
            ids = selection.values[..., 0].astype(int)
            if np.count_nonzero(ids >= 0) != reference.count:
                raise ValueError("Selected count changed")
            # Source provenance is inherited by immutable node identity, not
            # reselected in the candidate. Save a hash without private metadata.
            provenance = tuple(staged.sources[n] for n in ids[ids >= 0])
            arrays[f"selection_{i}"] = selection.values
            arrays[f"expected_{i}"] = reconstruct(field, selection)
            rows.append(
                dict(
                    view=i,
                    symbols=reference.count,
                    selection_bytes=selection.values.nbytes,
                    reference_atlas_bytes=reference.nbytes,
                    provenance_sha256=startup.digest(provenance),
                    reference_endpoint_match=True,
                )
            )
        np.savez(output, **arrays)
        return dict(
            generation=generation,
            selected_sources=len(sources),
            retained_nodes=len(points),
            field_bytes=field.values.nbytes,
            decoded_cache=cache.statistics(),
            views=rows,
            cpu_reference_exact=True,
            sampling_blocked_during_candidate_selection=True,
            reserved_texture_bytes=validate_binding(field, selection),
            texture_byte_limit=LIMIT,
        )
    finally:
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
    parser.add_argument("--app-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        result = prepare(
            json.loads(args.worker.read_text()), args.output.with_suffix(".npz")
        )
    else:
        if not all((args.snapshot, args.view_report, args.app_root)):
            parser.error("--snapshot, --view-report and --app-root required")
        manifest = startup.verify_snapshot(args.snapshot)
        report = json.loads(args.view_report.read_text())
        if (
            report["workload"]["snapshot_sha256"] != manifest["content_sha256"]
            or report["workload"]["forecast_time"] != manifest["forecast_time"]
        ):
            raise ValueError("Fixture/view identity mismatch")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists() or args.output.with_suffix(".npz").exists():
            raise ValueError("Refusing to overwrite evidence")
        with tempfile.TemporaryDirectory(prefix="gwr-persistent-field-") as temporary:
            root = Path(temporary)
            data = startup.prepare_sandbox(args.snapshot, root)
            config = dict(
                data=str(data),
                app_root=str(args.app_root.resolve()),
                startup=report["runs"][0]["startup"],
                viewport=report["runs"][0]["actual_viewport"],
                forecast_time=manifest["forecast_time"],
                snapshot_sha256=manifest["content_sha256"],
            )
            path = root / "config.json"
            path.write_text(json.dumps(config))
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
                    str(path),
                    "--output",
                    str(args.output.resolve()),
                ],
                cwd=ROOT,
                env=env,
                check=True,
                timeout=110,
            )
        result = json.loads(args.output.read_text())
        result.update(
            snapshot_sha256=manifest["content_sha256"],
            harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            payload_sha256=startup.protocol.file_hash(args.output.with_suffix(".npz")),
            app_commit=subprocess.check_output(
                ["git", "-C", str(args.app_root), "rev-parse", "HEAD"], text=True
            ).strip(),
        )
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
