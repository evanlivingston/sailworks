"""Bounded real-entrypoint validation; private fixture helpers supplied explicitly."""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time

import numpy as np


def arrays_hash(arrays):
    digest = hashlib.sha256()
    for value in arrays:
        array = np.ascontiguousarray(value)
        digest.update(repr((array.dtype.str, array.shape)).encode())
        digest.update(array.tobytes())
    return digest.hexdigest()


def sample_weather(window):
    """Describe the prepared weather frame of a window, or None."""
    canvas = window.map
    state = canvas.wind._states.get(canvas)
    if (
        not state
        or not state.frame
        or not canvas.wind.prepared_view_status(canvas)["current"]
    ):
        return None
    job, surface = state.frame
    flat = getattr(job, "barb_flat", None)
    if flat is None:
        return None
    return dict(
        view=[
            canvas.width(),
            canvas.height(),
            canvas._center_lat,
            canvas._center_lon,
            canvas._zoom,
        ],
        clock=canvas.time_control.time.isoformat(),
        flat_hash=arrays_hash(flat),
        land_hash=(
            arrays_hash((job.barb_land,))
            if getattr(job, "barb_land", None) is not None
            else None
        ),
        weather_surface_hash=hashlib.sha256(surface.get_data()).hexdigest(),
        job_id=id(job),
        sample_count=len(flat[0]),
        sources=[g.name for g in window.controller.core.grib_manager.gribs],
    )


def close_pending(window, controller, startup, report):
    """Close while charts are still pending and report what was rejected."""
    if startup is None or startup.state != "waiting":
        raise ValueError("Close-pending did not reach waiting state")
    window.close()
    controller.close()
    startup.advance()
    report["closed_state"] = startup.state
    report["pending_callback_rejected"] = (
        not controller.charts.charts and startup.state == "closed"
    )


def record_chart_ready(controller, startup, report, now, began):
    """Record the first moment the staged charts were published."""
    report["charts_initialized_seconds"] = now - began
    report["chart_count"] = len(controller.charts.charts)
    report["chart_errors"] = controller.chart_errors
    if startup:
        report["chart_stages"] = startup.stages
        report["weather_current_before_charts"] = startup.weather_current_before_charts


def check_weather_preserved(initial, frame, startup):
    """Fail if chart publication replaced the weather or the camera."""
    for key in (
        "view",
        "clock",
        "flat_hash",
        "land_hash",
        "weather_surface_hash",
        "sources",
    ):
        if frame[key] != initial[key]:
            raise ValueError(
                f"Chart publication changed weather/camera identity: {key}"
            )
    if startup and frame["job_id"] != initial["job_id"]:
        raise ValueError("Chart publication replaced the prepared weather job")


def open_logs(window, app, report, began):
    """Open the Logs workspace and confirm the deferred plots materialize."""
    window.views.setCurrentWidget(window.logs_view)
    for _ in range(60):
        app.processEvents()
        time.sleep(0.01)
    report["logs_matplotlib_imported"] = "matplotlib" in sys.modules
    report["logs_plot_materialized"] = getattr(window.plots, "plot", None) is not None
    report["logs_axes"] = len(getattr(window.plots, "axes", []))
    if not report["logs_plot_materialized"] or not report["logs_axes"]:
        raise ValueError("Logs plots did not materialize when opened")
    report["logs_first_frame_seconds"] = time.monotonic() - began


class StartupWatch:
    """Observe the real entry point until the startup gate closes."""

    def __init__(self, args, manifest, windows, app, timer):
        self.args = args
        self.manifest = manifest
        self.windows = windows
        self.app = app
        self.timer = timer
        self.report = dict(
            snapshot_sha256=manifest["content_sha256"],
            baseline=args.baseline,
            close_pending=args.close_pending,
            errors=[],
            presentation_measured=False,
        )
        self.began = time.monotonic()
        self.ticks = []
        self.initial = None
        self.ready_at = None

    def window(self):
        return self.windows[0] if self.windows else None

    def tick(self):
        now = time.monotonic()
        self.ticks.append(now)
        try:
            if now - self.began > 85:
                raise TimeoutError("Actual startup validation exceeded 85 s")
            window = self.window()
            if window is None:
                return
            controller = window.controller
            startup = controller.chart_startup
            self.record_first_window(controller, now)
            if self.args.close_pending:
                close_pending(window, controller, startup, self.report)
                self.stop()
                return
            frame = sample_weather(window)
            if self.initial is None and frame is not None:
                self.record_first_weather(window, controller, frame, now)
            if self.charts_ready(startup):
                self.observe_charts(window, controller, startup, frame, now)
        except Exception as exc:
            self.report["errors"].append(str(exc))
            self.stop()

    def record_first_window(self, controller, now):
        if "first_window_seconds" in self.report:
            return
        self.report["first_window_seconds"] = now - self.began
        self.report["matplotlib_imported_at_first_window"] = "matplotlib" in sys.modules
        self.report["initial_chart_count"] = len(controller.charts.charts)

    def record_first_weather(self, window, controller, frame, now):
        self.initial = frame
        self.report["first_weather_seconds"] = now - self.began
        self.report["first_weather"] = frame
        self.report["chart_count_at_first_weather"] = len(controller.charts.charts)
        window.map.grab().save(str(self.args.directory / "first-weather.png"))

    def charts_ready(self, startup):
        if self.initial is None:
            return False
        return startup is None or startup.state == "ready"

    def observe_charts(self, window, controller, startup, frame, now):
        if self.ready_at is None:
            self.ready_at = now
            record_chart_ready(controller, startup, self.report, now, self.began)
        if now - self.ready_at <= 1 or frame is None:
            return
        self.report["final_weather"] = frame
        check_weather_preserved(self.initial, frame, startup)
        window.map.grab().save(str(self.args.directory / "charts-and-weather.png"))
        self.report["camera_weather_preserved"] = True
        if self.args.logs:
            open_logs(window, self.app, self.report, self.began)
        self.stop()

    def stop(self):
        self.timer.stop()
        window = self.window()
        if window is not None:
            window.close()
        self.app.quit()

    def heartbeat(self):
        if len(self.ticks) < 2:
            return
        gaps = np.diff(self.ticks) * 1000
        self.report["heartbeat_ms_including_readbacks"] = dict(
            p95=float(np.percentile(gaps, 95)), maximum=float(gaps.max())
        )

    def passed(self):
        return (
            not self.report["errors"]
            and self.report["entry_exit"] == 0
            and self.report["controller_closed"]
            and (
                self.report.get("pending_callback_rejected")
                or self.report.get("camera_weather_preserved")
            )
        )


def prepare_sandbox(args, fixture):
    """Verify the snapshot and prepare an isolated writable copy."""
    args.directory.mkdir(parents=True, exist_ok=False)
    manifest = fixture.verify_snapshot(args.snapshot)
    sandbox = args.directory / "sandbox"
    fixture.prepare_sandbox(args.snapshot, sandbox)
    os.environ.update(
        HOME=str(sandbox / "home"),
        XDG_DATA_HOME=str(sandbox / "xdg-data"),
        XDG_CONFIG_HOME=str(sandbox / "xdg-config"),
        XDG_CACHE_HOME=str(sandbox / "xdg-cache"),
    )
    return manifest


def block_network():
    def blocked(*_args, **_kwargs):
        raise RuntimeError("Network disabled in real startup validation")

    socket.socket.connect = socket.socket.connect_ex = blocked


def freeze_clock(manifest):
    """Freeze the application clock at the fixture's forecast time."""
    from gweatherrouting.core.timecontrol import TimeControl

    original_time = TimeControl.__init__

    def frozen_time(self):
        original_time(self)
        self.set_time(datetime.fromisoformat(manifest["forecast_time"]))

    TimeControl.__init__ = frozen_time


def track_windows():
    """Record every window the application builds."""
    from gweatherrouting.qt.window import PreviewWindow

    windows = []
    original_window = PreviewWindow.__init__

    def construct(self, *args, **kwargs):
        original_window(self, *args, **kwargs)
        windows.append(self)

    PreviewWindow.__init__ = construct
    return windows


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-tools", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--close-pending", action="store_true")
    parser.add_argument("--logs", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    sys.path.append(str(args.fixture_tools))
    import benchmark_startup_rendering as fixture

    manifest = prepare_sandbox(args, fixture)
    block_network()
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from gweatherrouting.core.marine.credentials import SystemCredentialStore
    from gweatherrouting.qt import __main__ as entry

    SystemCredentialStore.load = lambda _self: None
    freeze_clock(manifest)
    windows = track_windows()
    app = QApplication([])
    timer = QTimer()
    watch = StartupWatch(args, manifest, windows, app, timer)
    timer.setInterval(25)
    timer.timeout.connect(watch.tick)
    timer.start()
    argv = ["--offline"] + ([] if args.baseline else ["--early-weather"])
    watch.report["entry_exit"] = entry.main(argv)
    watch.report["controller_closed"] = bool(windows and windows[0].controller.closed)
    watch.heartbeat()
    watch.report["passed"] = watch.passed()
    (args.directory / "result.json").write_text(
        json.dumps(watch.report, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                key: value
                for key, value in watch.report.items()
                if key not in ("first_weather", "final_weather", "chart_stages")
            },
            indent=2,
        ),
        flush=True,
    )
    if not watch.report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
