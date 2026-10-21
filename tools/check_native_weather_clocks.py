"""N3 clocks: two views on independent forecast times share one GPU budget.

Each view must keep its own time's picture; a source swap on one view must not
disturb the other, and returning must reproduce the first picture exactly.
Set GWR_NATIVE_WEATHER_GRIB to a local forecast; nothing is downloaded.
"""

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHECKS = (
    "views_independent",
    "other_view_undisturbed",
    "source_swap_reproduces",
    "shared_budget_used",
    "resources_released",
    "clean_native_exit",
)


def run(path, report):
    import numpy as np
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from gweatherrouting.qt.gpuweathertextures import NativeWeatherBudget
    from gweatherrouting.qt.gribviewport import (
        GribViewportModel,
        ViewportCamera,
        _viewport_widget_class,
    )
    from test.qt_gribviewport_checks import _grab
    from test.test_weather_barbs import Land

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)

    def pump():
        loop = QEventLoop()
        QTimer.singleShot(25, loop.quit)
        loop.exec()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait(predicate, label, widget):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            pump()
            widget.grabFramebuffer()
            if predicate():
                return
        raise RuntimeError("Timed out: " + label)

    base = datetime(2026, 10, 20, 19, 0)

    def camera(hours):
        return ViewportCamera(
            48.5, -123.25, 9, 640, 480, base + timedelta(hours=hours), geographic=True
        )

    def settle(widget, hours):
        widget.set_camera(camera(hours))
        wait(lambda: widget._state == "ready", f"{hours}h", widget)
        for _ in range(3):
            pump()
            pixels = _grab(widget)
        return pixels

    budget = NativeWeatherBudget()
    views = []
    try:
        for _ in range(2):
            model = GribViewportModel([path], land_mask=Land(), grid=True)
            widget = _viewport_widget_class()(
                model,
                gpu_weather_fill=True,
                native_weather_fields=True,
                native_weather_budget=budget,
            )
            widget.resize(640, 480)
            widget.show()
            views.append((model, widget))
        a_first = settle(views[0][1], 1)
        b_first = settle(views[1][1], 3)
        a_again = settle(views[0][1], 1)
        b_again = settle(views[1][1], 3)
        report["checks"]["views_independent"] = (
            not np.array_equal(a_first, b_first)
            and np.array_equal(a_first, a_again)
            and np.array_equal(b_first, b_again)
        )
        # Source swap on view 0: retire it, reload it, then compare with the
        # original picture; view 1 must still show its own time.
        model0, widget0 = views[0]
        widget0.replace_model(GribViewportModel([path], land_mask=Land(), grid=True))
        swapped = settle(widget0, 1)
        report["checks"]["source_swap_reproduces"] = np.array_equal(swapped, a_first)
        report["checks"]["other_view_undisturbed"] = np.array_equal(
            settle(views[1][1], 3), b_first
        )
        stats = budget.statistics()
        report["budget_statistics"] = stats
        report["checks"]["shared_budget_used"] = (
            stats["contexts"] == 4 and stats["peak_bytes"] > 0
        )
        report["phases"] = [{"name": "views", "state": [w._state for _, w in views]}]
    finally:
        owners = tuple(
            owner for _, widget in views for owner in widget._native_contexts.values()
        )
        for model, widget in views:
            widget.dispose()
            widget.close()
            widget.deleteLater()
            model.close()
        pump()
        report["retired"] = [owner.snapshot() for owner in owners]
        report["final_budget"] = budget.statistics()
        report["checks"]["resources_released"] = (
            report["final_budget"]["current_bytes"] == 0
            and report["final_budget"]["contexts"] == 0
            and all(
                s.get("created") == s.get("destroyed") and not s["errors"]
                for s in report["retired"]
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = Path(os.environ["GWR_NATIVE_WEATHER_GRIB"]).resolve()
    if not os.environ.get("GWR_NATIVE_CLOCKS_CHILD"):
        args.output.parent.mkdir(parents=True, exist_ok=True)
        trace = args.output.with_suffix(".uploads.jsonl")
        trace.write_text("")
        subprocess.run(
            [
                "xcrun",
                "lldb",
                "--no-lldbinit",
                "--batch",
                "-o",
                "settings set target.disable-aslr false",
                "-o",
                "settings set target.process.stop-on-exec false",
                "-o",
                f"command script import {ROOT / 'tools/qrhi_upload_trace.py'}",
                "-o",
                "run",
                "-o",
                "script qrhi_upload_trace.write_process_status(lldb.debugger)",
                "--",
                sys.executable,
                str(Path(__file__).resolve()),
                "--output",
                str(args.output),
            ],
            env=dict(
                os.environ,
                GWR_NATIVE_CLOCKS_CHILD="1",
                GWR_UPLOAD_TRACE_OUTPUT=str(trace),
            ),
            timeout=115,
            check=False,
        )
        report = json.loads(args.output.read_text())
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        status = next((e for e in events if e["event"] == "process_status"), {})
        report["native_exit"] = status
        report["checks"]["clean_native_exit"] = (
            status.get("exited") is True and status.get("exit_code") == 0
        )
    else:
        report = {"checks": dict.fromkeys(CHECKS, False), "phases": []}
        try:
            run(path, report)
        except Exception:
            report["error"] = traceback.format_exc()
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")


if __name__ == "__main__":
    main()
