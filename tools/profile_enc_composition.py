#!/usr/bin/env python3
"""Measure ENC composition wall time with the application's own ENC path.

Reports per-iteration wall time for the viewport's ENC source at a given view.
It changes no rendering behaviour and asserts no speed threshold.
"""

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from check_enc_viewport import _load_tool  # noqa: E402

SAVED_VIEW = (48.45003186114848, -124.28875762043288, 9.380182039882325)
WIDTH = 1000
HEIGHT = 680
FACTOR = 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report = {"iterations": []}
    try:
        if args.snapshot is not None:
            fixture = _load_tool(
                "profile_fixture", ROOT / "tools/benchmark_startup_rendering.py"
            )
            import tempfile

            root = Path(tempfile.mkdtemp(prefix="enc-profile-"))
            data_dir = fixture.prepare_sandbox(args.snapshot, root)
            # The measurement needs the cells enabled; the sandbox copy is ours.
            settings_path = data_dir / "settings.json"
            settings = json.loads(settings_path.read_text())
            settings["chartENCVisible"] = True
            settings_path.write_text(json.dumps(settings))
            report["snapshot"] = str(args.snapshot)
            report["sandbox"] = str(root)
        else:
            from gweatherrouting.core.storage import DATA_DIR

            data_dir = Path(args.data_dir or DATA_DIR)
        report["data_dir"] = str(data_dir)
        report["_cleanup_root"] = report.get("sandbox")
        _measure(report, data_dir, args.iterations)
    except Exception as exc:
        import traceback

        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    report["limits"] = (
        "CPU wall time of the ENC composition only; not FPS, presentation latency "
        "or GPU time; no speed threshold asserted"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))


def _measure(report, data_dir, iterations):  # noqa: C901 - one bounded measurement
    import cairo
    from gweatherrouting.core.mapviewport import MapViewport
    from gweatherrouting.qt.gribviewport import ViewportCamera
    from gweatherrouting.qt.gribviewportchart import ChartCamera
    from gweatherrouting.qt.viewportenc import ViewportEncSource
    from gweatherrouting.rendering.overlayrenderer import MARGIN, RenderJob
    from threading import Event

    lat, lon, zoom = SAVED_VIEW
    camera = ViewportCamera(lat, lon, zoom, WIDTH, HEIGHT, None, "very_dense", False)
    canvas = ChartCamera(camera, FACTOR)
    viewport = MapViewport(canvas, MARGIN, lambda: None)
    cancel = Event()
    viewport.render_cancelled = cancel.is_set
    source = ViewportEncSource(data_dir, "dark")
    report["view"] = {"lat": lat, "lon": lon, "zoom": zoom}
    report["surface"] = [viewport.width * FACTOR, viewport.height * FACTOR]

    for index in range(max(1, iterations)):
        job = RenderJob(
            key=("dark", FACTOR, ()),
            viewport=viewport,
            charts=(),
            factor=FACTOR,
            palette="dark",
            cancel=cancel,
        )
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32, viewport.width * FACTOR, viewport.height * FACTOR
        )
        surface.set_device_scale(FACTOR, FACTOR)
        began = time.perf_counter()
        surface, metadata = source.compose(job, surface)
        elapsed = time.perf_counter() - began
        report["iterations"].append(
            {
                "index": index,
                "wall_s": round(elapsed, 4),
                "enc_state": metadata.get("state"),
                "selected": list(metadata.get("selected", ())),
                "bytes": metadata.get("bytes"),
            }
        )
    try:
        source.release()
    except Exception:
        pass


if __name__ == "__main__":
    main()
