"""Bounded real-workload check for retained chart content (issue #108).

This is a measurement check, not the #108 acceptance gate.  It composes the
application's own base chart, ENC cells and pressure contours for one saved
view, then pans, reverses and changes the forecast instant on the same
layer.  It reports composition wall time, composed/reused pixel area, the
retained payload and the published composition/scheduling split.

The snapshot is verified before use and its identity is recorded with every
result.  Times are CPU composition/queue callbacks, never FPS or presentation
latency.  Exit code 1 means a pre-declared bound was violated; the formal
acceptance gate remains ``tools/gate_runner.py --gate enc-viewport``.
"""

import argparse
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

#: The recorded saved view used by the chart-reuse measurements.
SAVED_VIEW = (48.45003186114848, -124.28875762043288, 9.380182039882325)
#: Pan distances beyond the 128 px ``fits`` reuse margin.
PAN_PIXELS = (200.0, 450.0)
#: Pre-declared bounds for this bounded check.
MAX_PAN_COMPOSED_FRACTION = 0.60
MIN_PAN_SPEEDUP = 0.25
MAX_RETAINED_FRAMES = 3


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def panned_camera(camera, pixels):
    """The same camera after an eastward pan of *pixels* logical pixels."""
    from sailworks.qt.gribviewport import ViewportCamera

    width = 256 * 2**camera.zoom
    return ViewportCamera(
        camera.center_lat,
        camera.center_lon + pixels * 360.0 / width,
        camera.zoom,
        camera.width,
        camera.height,
        camera.time,
        camera.density,
        camera.over_land,
    )


def forecast_time(paths, fallback):
    """A covered forecast instant, from the opened files' own run names.

    The snapshot clock is the application's clock, not necessarily a covered
    forecast time.  The run name carries the model run; noon after the
    earliest run is inside the opened windows for the recorded selection.
    """
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
    if not runs:
        return fallback
    return min(runs) + timedelta(hours=12)


def verify(run):
    """The pre-declared pass/fail conditions for one bounded run."""
    full = next((r for r in run["runs"] if r["label"] == "warm_full"), None)
    pan = next((r for r in run["runs"] if r["label"] == "pan_200"), None)
    time_only = next((r for r in run["runs"] if r["label"] == "time_only"), None)
    reversal = next((r for r in run["runs"] if r["label"] == "reversal"), None)
    checks = {}
    if full and pan:
        target = full["composed_pixels"] or 1
        checks["pan_composed_fraction"] = (
            pan["composed_pixels"] / target <= MAX_PAN_COMPOSED_FRACTION
        )
        speedup = (full["ms"] - pan["ms"]) / full["ms"]
        checks["pan_speedup"] = speedup >= MIN_PAN_SPEEDUP
        run["pan_speedup"] = round(speedup, 4)
    checks["time_only_reuses_static"] = bool(time_only) and (
        time_only["composed_pixels"] == 0 and time_only["mode"] == "reused"
    )
    if run["workload"].get("isobars"):
        checks["time_only_draws_only_contours"] = bool(time_only) and (
            time_only["timings"].get("isobars_ms", 0.0) > 0.0
            and time_only["timings"].get("columns_ms", 0.0) > 0.0
            and time_only["timings"].get("copy_ms", 0.0) > 0.0
        )
        checks["contours_were_measured"] = run["counters"].get("isobars", 0) > 0
    checks["reversal_reuses_frame"] = bool(reversal) and (
        reversal["mode"] == "reused" and reversal["composed_pixels"] == 0
    )
    checks["identity_matches_reference"] = bool(run.get("identity", {}).get("match"))
    checks["retained_frames_bounded"] = run["static"]["frames"] <= MAX_RETAINED_FRAMES
    run["checks"] = checks
    run["passed"] = all(checks.values())
    return run


def measure(snapshot, output, dpr=2, with_isobars=True):
    fixture = _load("chart_reuse_fixture", "benchmark_startup_rendering.py")
    manifest = fixture.verify_snapshot(snapshot)
    import tempfile

    sandbox = Path(tempfile.mkdtemp(prefix="chart-reuse-"))
    data_dir = fixture.prepare_sandbox(snapshot, sandbox)
    settings_path = data_dir / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["chartENCVisible"] = True
    settings_path.write_text(json.dumps(settings))

    from threading import Event

    from sailworks.qt import gribviewportchart
    from sailworks.qt.gribviewport import ViewportCamera, saved_selection

    paths, _settings = saved_selection(data_dir)
    app_time = datetime.fromisoformat(manifest["forecast_time"]).replace(
        tzinfo=timezone.utc
    )
    instant = forecast_time(paths, app_time)
    lat, lon, zoom = SAVED_VIEW
    camera = ViewportCamera(
        lat,
        lon,
        zoom,
        1000,
        680,
        instant,
        "very_dense",
        False,
    )
    layer = gribviewportchart.ViewportChartLayer(
        paths,
        data_dir,
        enc_enabled=True,
        enc_only=not with_isobars,
        dpr=dpr,
    )
    runs = []

    def measure_run(label, cam, preview=False):
        cancel = Event()
        began = time.perf_counter()
        image, metadata = layer._compose(cam, cancel, preview=preview)
        elapsed = (time.perf_counter() - began) * 1000.0
        if image is None:
            raise RuntimeError(f"{label} did not compose")
        static = metadata["static"]
        timings = metadata.get("timings") or {}
        runs.append(
            {
                "label": label,
                "ms": round(elapsed, 1),
                "mode": static["mode"],
                "regions": static["regions"],
                "composed_pixels": static["composed_pixels"],
                "reused_pixels": static["reused_pixels"],
                "retained_bytes": static["retained_bytes"],
                "timings": timings,
                "enc_state": metadata.get("enc", {}).get("state"),
                "selected": list(metadata.get("enc", {}).get("selected") or ()),
            }
        )
        return metadata

    def clear_static():
        layer._static_frames.clear()
        with layer._condition:
            layer._static_bytes = 0
            layer._static_count = 0

    metadata = measure_run("cold", camera)
    clear_static()
    measure_run("warm_full", camera)
    pan_200 = panned_camera(camera, PAN_PIXELS[0])
    metadata_200 = measure_run("pan_200", pan_200)
    measure_run("pan_450", panned_camera(camera, PAN_PIXELS[1]))
    measure_run("reversal", camera)
    later = datetime.fromtimestamp(camera.time.timestamp() + 3600, tz=timezone.utc)
    measure_run(
        "time_only",
        ViewportCamera(
            camera.center_lat,
            camera.center_lon,
            camera.zoom,
            camera.width,
            camera.height,
            later,
            camera.density,
            camera.over_land,
        ),
    )
    measure_run("preview_1", camera, preview=True)
    measure_run("preview_2", camera, preview=True)

    identity = {"match": False}
    try:
        identity = _identity_check(
            data_dir, pan_200, dpr, metadata_200.get("enc", {}), gribviewportchart
        )
    except Exception as exc:  # a failed identity probe never hides the timing
        identity = {"match": False, "error": str(exc)}

    observations = layer.observations()
    run = {
        "snapshot": {
            "path": str(snapshot),
            "content_sha256": manifest["content_sha256"],
            "files": len(manifest["files"]),
            "bytes": sum(entry["bytes"] for entry in manifest["files"]),
            "forecast_time": manifest["forecast_time"],
        },
        "workload": {
            "view": {"lat": lat, "lon": lon, "zoom": zoom},
            "width": 1000,
            "height": 680,
            "dpr": dpr,
            "pan_pixels": list(PAN_PIXELS),
            "isobars": with_isobars,
            "forecast_instant": instant.isoformat(),
        },
        "runs": runs,
        "counters": observations["counters"],
        "static": observations["static"],
        "identity": identity,
        "endpoint": (
            "CPU composition wall time and request/worker callbacks; not FPS, "
            "presentation latency, input-to-photon or GPU completion"
        ),
        "limits": (
            "One saved view; no zoom reuse; fractional-pan reuse resamples the "
            "retained area bilinearly; RSS is not measured here"
        ),
    }
    verify(run)
    layer.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(run, indent=2))
    return run


def _identity_check(data_dir, camera, dpr, published, gribviewportchart):
    """Compare a band composition's ENC identity with a full reference."""
    import cairo

    from sailworks.core.mapviewport import MapViewport
    from sailworks.qt.viewportenc import ViewportEncSource
    from sailworks.rendering.overlayrenderer import MARGIN, RenderJob

    viewport = MapViewport(
        gribviewportchart.ChartCamera(camera, dpr), MARGIN, lambda: None
    )
    cancel = __import__("threading").Event()
    viewport.render_cancelled = cancel.is_set
    job = RenderJob(("cm93", dpr, ()), viewport, (), dpr, "cm93", cancel)
    surface = cairo.ImageSurface(
        cairo.FORMAT_ARGB32, viewport.width * dpr, viewport.height * dpr
    )
    surface.set_device_scale(dpr, dpr)
    source = ViewportEncSource(data_dir, "cm93")
    try:
        _surface, reference = source.compose(job, surface)
    finally:
        source.release()
    return {
        "match": list(published.get("selected") or ())
        == list(reference.get("selected") or ())
        and list(published.get("coverage") or ())
        == list(reference.get("coverage") or ()),
        "selected": list(reference.get("selected") or ()),
        "reference_coverage": len(reference.get("coverage") or ()),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dpr", type=int, default=2)
    parser.add_argument(
        "--no-isobars",
        action="store_true",
        help="Compose ENC only; the default exercises the contour path too.",
    )
    args = parser.parse_args(argv)
    run = measure(
        args.snapshot, args.output, dpr=args.dpr, with_isobars=not args.no_isobars
    )
    print(
        f"{'PASS' if run['passed'] else 'FAIL'} chart-reuse "
        f"pan200={_run_ms(run, 'pan_200')}ms "
        f"warm_full={_run_ms(run, 'warm_full')}ms "
        f"speedup={run.get('pan_speedup')} "
        f"time_only={_run_mode(run, 'time_only')} "
        f"reversal={_run_mode(run, 'reversal')} "
        f"identity={run['identity'].get('match')} "
        f"frames={run['static']['frames']} "
        f"({args.output})"
    )
    for name, passed in run["checks"].items():
        if not passed:
            print(f"  failed: {name}")
    return 0 if run["passed"] else 1


def _run_ms(run, label):
    item = next((r for r in run["runs"] if r["label"] == label), None)
    return None if item is None else item["ms"]


def _run_mode(run, label):
    item = next((r for r in run["runs"] if r["label"] == label), None)
    return None if item is None else item["mode"]


if __name__ == "__main__":
    raise SystemExit(main())
