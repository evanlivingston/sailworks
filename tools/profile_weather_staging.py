"""Attribute weather staging cost on a verified application snapshot.

Diagnostic only: it stages the same saved view at the covered forecast instant
and reports cold, shifted and repeated window wall times, memo statistics,
tracemalloc peak and GC pauses.  It changes no rendering behaviour and asserts
no threshold.  The endpoint is CPU staging time, not UI latency or FPS.
"""

import argparse
import gc
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

CAMERA = (48.30887922260005, -123.09473088970692, 9.837590787619122)
WIDTH, HEIGHT = 900, 700


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def forecast_instant(paths, manifest):
    """A covered instant from the opened run names, else the manifest clock."""
    from datetime import datetime, timedelta, timezone

    runs = []
    for path in paths:
        match = re.search(r"_(\d{8})_(\d{2})z", Path(path).name)
        if match is None:
            continue
        runs.append(
            datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H").replace(
                tzinfo=timezone.utc
            )
        )
    if runs:
        return min(runs) + timedelta(hours=12)
    fallback = datetime.fromisoformat(manifest["forecast_time"])
    return (
        fallback
        if fallback.tzinfo is not None
        else fallback.replace(tzinfo=timezone.utc)
    )


def measure(snapshot, output, instant=None):
    fixture = _load("staging_fixture", "benchmark_startup_rendering.py")
    manifest = fixture.verify_snapshot(snapshot)
    root = Path(tempfile.mkdtemp(prefix="weather-staging-profile-"))
    data_dir = fixture.prepare_sandbox(snapshot, root)

    from sailworks.core.gribsampling import WeatherSampler
    from sailworks.qt import gribviewport
    from sailworks.qt.gpuweatherfields import FieldIdentity
    from sailworks.qt.gribviewport import ViewportCamera
    from sailworks.rendering.weatherbarbs import field_window, stage_field
    from sailworks.rendering.weathercache import ForecastSession
    from sailworks.rendering.weatherstaging import NodeSampleMemo, time_window
    from sailworks.rendering.windpainter import shoreline_quality
    from sailworks.rendering.windrenderer import WindLandMask

    paths, _settings = gribviewport.saved_selection(data_dir)
    instant = instant or forecast_instant(paths, manifest)
    lat, lon, zoom = CAMERA
    session = ForecastSession()
    sources = [session.source(path) for path in paths]
    sampler = WeatherSampler(sources)
    memo = NodeSampleMemo()
    land = WindLandMask(data_dir)
    source = tuple(gribviewport._source_key(item) for item in sources)

    def camera(shift=0.0):
        width = 256 * 2**zoom
        return ViewportCamera(
            lat,
            lon + shift * 360.0 / width,
            zoom,
            WIDTH,
            HEIGHT,
            instant,
            "very_dense",
            False,
        )

    def stage(cam):
        view = cam.view()
        window = field_window(
            gribviewport._expanded(cam, gribviewport.RESIDENT_WINDOW_FACTOR).view()
        )
        before, after = time_window(sampler, cam.stamp(), "wind")
        resolution = (
            tuple(sorted({index.nominal_step_degrees for index in sampler.indices})),
            None if cam.over_land else shoreline_quality(view.get_scale()),
            True,
        )
        identity = FieldIdentity(
            source=source,
            parameter="wind",
            before=before,
            after=after,
            window=window,
            resolution=resolution,
        )
        return stage_field(
            sampler,
            cam.stamp(),
            window,
            identity,
            lambda: False,
            view=view,
            land_mask=land,
            geometry=None,
            memo=memo,
            grid=True,
            parameter="wind",
            components=2,
        )

    report = {
        "snapshot": {
            "path": str(snapshot),
            "content_sha256": manifest["content_sha256"],
            "forecast_time": manifest["forecast_time"],
        },
        "camera": {"lat": lat, "lon": lon, "zoom": zoom},
        "instant": instant.isoformat(),
        "windows": [],
        "endpoint": "CPU stage_field wall time; not UI latency or FPS",
    }

    def window(label, shift):
        cam = camera(shift)
        gc_events = []

        def collect(phase, info):
            if phase == "start":
                gc_events.append([time.perf_counter(), None])
            elif gc_events and gc_events[-1][1] is None:
                gc_events[-1][1] = time.perf_counter()

        gc.callbacks.append(collect)
        tracemalloc.start()
        began = time.perf_counter()
        field = stage(cam)
        elapsed = (time.perf_counter() - began) * 1000.0
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        gc.callbacks.remove(collect)
        entry = {
            "label": label,
            "shift_pixels": shift,
            "stage_field_ms": round(elapsed, 3),
            "node_count": len(field.points),
            "tracemalloc_peak": peak,
            "gc_pauses_ms": round(
                sum((end or start) - start for start, end in gc_events) * 1000.0, 3
            ),
            "gc_collections": len(gc_events),
            "memo": memo.statistics(),
        }
        report["windows"].append(entry)
        print(json.dumps(entry))

    # Warm the sampler's own decode/geometry caches first; it is not a window.
    stage(camera(-600))
    window("cold", 0)
    window("shift_120", 120)
    window("shift_240", 240)
    window("repeat_240", 240)
    session.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--instant",
        help="ISO 8601 covered forecast instant; default: opened run + 12 h",
    )
    args = parser.parse_args(argv)
    from datetime import datetime, timezone

    instant = datetime.fromisoformat(args.instant) if args.instant else None
    if instant is not None and instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    report = measure(args.snapshot, args.output, instant)
    print(json.dumps({"windows": report["windows"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
