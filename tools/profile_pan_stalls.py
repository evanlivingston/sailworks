"""Diagnostic pan trace: wall/thread CPU spans and GC, using private snapshots.

This supplements benchmark_startup_rendering.py, not its acceptance timings.
Raw output contains private viewport information and must not be committed.
"""

import argparse
import functools
import gc
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "startup_protocol", Path(__file__).with_name("benchmark_startup_rendering.py")
)
protocol = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(protocol)


class Tracer:
    """Record wall-time, thread CPU and GC spans around instrumented calls."""

    def __init__(self):
        self.origin = time.perf_counter()
        self.spans = []
        self.collections = []
        self.restorations = []
        self.gc_started = {}
        self.main_thread = threading.get_ident()

    def stamp(self):
        return (time.perf_counter() - self.origin) * 1000

    def wrap(self, owner, method, label, static=False):
        original = getattr(owner, method)

        @functools.wraps(original)
        def measured(*args, **kwargs):
            started, cpu = self.stamp(), time.thread_time()
            try:
                return original(*args, **kwargs)
            finally:
                cpu_ms = (time.thread_time() - cpu) * 1000
                ended = self.stamp()
                self.spans.append(
                    dict(
                        stage=label,
                        thread=threading.get_ident(),
                        main=threading.get_ident() == self.main_thread,
                        start_ms=started,
                        end_ms=ended,
                        wall_ms=ended - started,
                        cpu_ms=cpu_ms,
                    )
                )

        setattr(owner, method, staticmethod(measured) if static else measured)
        self.restorations.append((owner, method, original, static))

    def observe_gc(self, event, info):
        key = (threading.get_ident(), info["generation"])
        if event == "start":
            self.gc_started[key] = (self.stamp(), time.thread_time())
        elif key in self.gc_started:
            started, cpu = self.gc_started.pop(key)
            ended = self.stamp()
            self.collections.append(
                dict(
                    thread=key[0],
                    generation=key[1],
                    start_ms=started,
                    end_ms=ended,
                    wall_ms=ended - started,
                    cpu_ms=(time.thread_time() - cpu) * 1000,
                    collected=info["collected"],
                )
            )

    def restore(self):
        for owner, method, original, static in reversed(self.restorations):
            setattr(owner, method, staticmethod(original) if static else original)
        self.restorations = []


def instrument(tracer):
    """Wrap the foreground, worker and geometry stages with the tracer."""
    # LEGACY-ONLY (TODO #86): TrackMap CPU painting/profiling probe; not retargeted.
    from gweatherrouting.qt.mapcanvas import TrackMap  # LEGACY-ONLY (TODO #86)
    from gweatherrouting.qt.basecharts import BaseChartRenderer
    from gweatherrouting.rendering.encrenderer import ENCOverlayRenderer
    from gweatherrouting.rendering.marinemaplayer import MarineRenderer
    from gweatherrouting.rendering.windrenderer import WindOverlayRenderer
    from gweatherrouting.rendering import windpainter

    # LEGACY-ONLY (TODO #86): wrap legacy TrackMap painting methods only.
    for method in ("paintEvent", "_base_image", "_paint_wind_barbs"):
        tracer.wrap(TrackMap, method, "ui." + method)  # LEGACY-ONLY (TODO #86)
    tracer.wrap(TrackMap, "_paint_wind_geometry", "ui.paint_geometry", static=True)  # LEGACY-ONLY (TODO #86)
    tracer.wrap(
        windpainter.WindPainter, "live_geometry_for_view", "ui.select", static=True
    )
    tracer.wrap(windpainter.WindPainter, "build_barb_geometry", "ui.build", static=True)
    tracer.wrap(windpainter, "mercator_project_flat", "ui.project")
    for owner in (
        BaseChartRenderer,
        ENCOverlayRenderer,
        MarineRenderer,
        WindOverlayRenderer,
    ):
        tracer.wrap(owner, "_render", owner.__name__ + "._render")
    tracer.wrap(WindOverlayRenderer, "_columns", "worker.columns", static=True)


def trace_worker(config):
    def no_network(*_args, **_kwargs):
        raise RuntimeError("Network is disabled in pan diagnostics")

    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network

    tracer = Tracer()
    instrument(tracer)
    gc.callbacks.append(tracer.observe_gc)
    try:
        result = protocol.worker(config)
        result["diagnostic"] = {
            key: config[key]
            for key in ("diagnostic_version", "snapshot_sha256", "revision")
        }
        result["diagnostic_spans"] = tracer.spans
        result["gc_spans"] = tracer.collections
        return result
    finally:
        gc.callbacks.remove(tracer.observe_gc)
        tracer.restore()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", type=Path)
    source.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.worker:
        result = trace_worker(json.loads(args.worker.read_text()))
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        return

    snapshot = args.snapshot.resolve(strict=True)
    manifest = protocol.verify_snapshot(snapshot)
    config = dict(
        diagnostic_version=1,
        snapshot_sha256=manifest["content_sha256"],
        revision=protocol.protocol.revision(),
        scenario="pan",
        frames=60,
        warmup_frames=3,
        interval_ms=16.667,
        settle_timeout_s=180,
        forecast_time=manifest["forecast_time"],
        map_renderer="cpu",
    )
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pan-stalls-") as temporary:
        root = Path(temporary)
        protocol.prepare_sandbox(snapshot, root)
        config_path = root / "config.json"
        config_path.write_text(json.dumps(config))
        env = dict(
            os.environ,
            HOME=str(root / "home"),
            XDG_DATA_HOME=str(root / "xdg-data"),
            XDG_CONFIG_HOME=str(root / "xdg-config"),
            XDG_CACHE_HOME=str(root / "xdg-cache"),
            QT_QPA_PLATFORM="offscreen",
            QT_SCALE_FACTOR="1",
            PYTHONHASHSEED="0",
            PYTHONPATH=str(ROOT),
        )
        subprocess.run(
            [
                sys.executable,
                __file__,
                "--worker",
                str(config_path),
                "--output",
                str(args.output),
            ],
            env=env,
            cwd=ROOT,
            check=True,
            timeout=600,
        )


if __name__ == "__main__":
    main()
