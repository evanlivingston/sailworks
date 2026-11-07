"""Rendering measurement protocol shared by the startup benchmarks and tests."""

import hashlib
import json
import math
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1
DEFAULT_SCENE = {
    "version": 1,
    "center": [48.4, -123.4],
    "zoom": 9.0,
    "time": "2026-10-15T12:00:00",
    "navigation_points": 10000,
    "log_points": 100000,
    "wind_side": 316,
    "datasets": [],
}


def summary(values):
    """Linear-interpolated percentiles, with budget counts and raw samples elsewhere."""
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
        "over_16_7_ms": sum(v > 16.7 for v in values),
        "over_33_3_ms": sum(v > 33.3 for v in values),
        "over_50_ms": sum(v > 50 for v in values),
    }


def read_scene(path=None):
    scene = dict(DEFAULT_SCENE)
    if path:
        supplied = json.loads(Path(path).read_text())
        unknown = set(supplied) - set(scene)
        if unknown:
            raise ValueError(f"Unknown scene keys: {sorted(unknown)}")
        scene.update(supplied)
    if scene["version"] != 1:
        raise ValueError("Unsupported scene version")
    for key in ("navigation_points", "log_points", "wind_side"):
        if type(scene[key]) is not int or scene[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    lat, lon = scene["center"]
    if not (-85 <= lat <= 85 and -180 <= lon <= 180 and 1 <= scene["zoom"] <= 18):
        raise ValueError("Invalid scene center or zoom")
    from datetime import datetime

    instant = datetime.fromisoformat(scene["time"])
    if instant.tzinfo is not None:
        raise ValueError("Scene time must be naive; replay uses TZ=UTC")
    datasets = []
    for entry in scene["datasets"]:
        if entry["kind"] not in ("vector", "grib", "auxiliary"):
            raise ValueError("Dataset kind must be vector, grib, or auxiliary")
        file = Path(entry["path"]).expanduser()
        if not file.is_absolute():
            file = Path(path).resolve().parent / file
        file = file.resolve(strict=True)
        digest = file_hash(file)
        if digest != entry["sha256"]:
            raise ValueError(f"Dataset checksum mismatch: {file}")
        datasets.append(dict(kind=entry["kind"], path=str(file), sha256=digest))
    scene["datasets"] = datasets
    return scene


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scene_identity(scene):
    # Locations may differ across checkouts; content and order must not.
    identity = dict(scene)
    identity["datasets"] = [
        {k: entry[k] for k in ("kind", "sha256")} for entry in scene["datasets"]
    ]
    return identity


def revision():
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "tracked_diff_sha256": hashlib.sha256(
            git("diff", "HEAD", "--binary").encode()
        ).hexdigest(),
        "harness_sha256": file_hash(__file__),
    }


def compare_reports(before, after):
    if before["schema_version"] != SCHEMA or after["schema_version"] != SCHEMA:
        raise ValueError("Incompatible schema_version")
    for key in ("schema_version", "workload", "environment"):
        if before[key] != after[key]:
            raise ValueError(f"Incompatible {key}; refusing a misleading comparison")
    if before["revision"]["harness_sha256"] != after["revision"]["harness_sha256"]:
        raise ValueError("Incompatible measurement harness")
    rows = []
    for scenario in before["workload"]["scenarios"]:
        a = [r for r in before["runs"] if r["scenario"] == scenario]
        b = [r for r in after["runs"] if r["scenario"] == scenario]
        repeats = before["workload"]["repeats"]
        if len(a) != repeats or len(b) != repeats:
            raise ValueError(f"Missing or mismatched repetitions: {scenario}")
        if any(
            sorted(r["repeat"] for r in runs) != list(range(repeats)) for runs in (a, b)
        ):
            raise ValueError(f"Duplicate or missing repetition IDs: {scenario}")
        for run in a + b:
            if len(run["samples"]["grab_ms"]) != before["workload"]["frames"]:
                raise ValueError(f"Incomplete frame samples: {scenario}")
        old = summary([v for run in a for v in run["samples"]["grab_ms"]])
        new = summary([v for run in b for v in run["samples"]["grab_ms"]])
        rows.append(
            {
                "scenario": scenario,
                "before": old,
                "after": new,
                "queued_callback_before": summary(
                    [v for run in a for v in run["samples"]["queued_callback_ms"]]
                ),
                "queued_callback_after": summary(
                    [v for run in b for v in run["samples"]["queued_callback_ms"]]
                ),
                "resources_before": resource_summary(a),
                "resources_after": resource_summary(b),
                "p99_change_percent": (
                    100 * (new["p99_ms"] / old["p99_ms"] - 1) if old["p99_ms"] else None
                ),
            }
        )
    return {"schema_version": SCHEMA, "comparisons": rows}


def resource_summary(runs):
    return {
        "peak_rss_bytes": max(r["process_peak_rss_bytes"] for r in runs),
        "peak_tracked_surface_bytes": max(
            max(r["tracked_surface_bytes_per_frame"]) for r in runs
        ),
        "base_rebuilds": sum(
            frame["_base_image"] for run in runs for frame in run["rebuilds_per_frame"]
        ),
        "navigation_rebuilds": sum(
            frame["_navigation_image"]
            for run in runs
            for frame in run["rebuilds_per_frame"]
        ),
        "post_gesture_worker_idle": summary(
            [r["post_gesture_worker_idle_ms"] for r in runs]
        ),
    }


def tracked_surface_bytes(canvas):
    """Known retained image buffers only, not total memory or temporary copies."""
    seen = set()
    total = 0

    def add(image):
        nonlocal total
        if image is None:
            return
        if hasattr(image, "cacheKey"):
            key, size = ("qt", image.cacheKey()), image.sizeInBytes()
        else:
            key, size = ("cairo", id(image)), image.get_stride() * image.get_height()
        if key not in seen:
            seen.add(key)
            total += size

    for cache in (canvas._base_cache, canvas._nav_cache, canvas._dash_cache):
        if cache:
            add(cache[-1])
    cached = canvas.charts._chart_surfaces.get(canvas)
    if cached:
        add(cached[-1])
    for renderer in (
        canvas.wind,
        canvas.isochrones,
        canvas.charts._enc_renderer,
    ):
        state = renderer._states.get(canvas)
        if state and state.frame:
            add(state.frame[1])
    for surface, _modified in canvas.charts._chs_renderer._tiles.values():
        add(surface)
    return total
