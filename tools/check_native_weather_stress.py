"""N3 stress: forced GPU eviction and reload on a real forecast, native Cocoa/Metal.

The budget holds exactly one forecast time's endpoint textures.  Alternating
A -> B -> A -> B must evict and reload, while pixels, budget and cleanup stay
exact.  Set GWR_NATIVE_WEATHER_GRIB to a local forecast; nothing is downloaded.
"""

import argparse
from dataclasses import replace
from datetime import timedelta
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
    "budget_held_one_time",
    "pressure_evicted",
    "reload_uploaded",
    "reload_pixels_identical",
    "budget_never_exceeded",
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
    from datetime import datetime

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

    def make(budget):
        model = GribViewportModel([path], land_mask=Land(), grid=True)
        widget = _viewport_widget_class()(
            model,
            gpu_weather_fill=True,
            native_weather_fields=True,
            native_weather_budget=budget,
        )
        widget.resize(640, 480)
        widget.show()
        return model, widget

    # Phase 0: measure one forecast time's textures with an unconstrained budget.
    probe_budget = NativeWeatherBudget()
    model, widget = make(probe_budget)
    widget.set_camera(camera(1))
    wait(lambda: widget._state == "ready", "probe", widget)
    one_time = probe_budget.statistics()["peak_bytes"]
    widget.dispose()
    widget.close()
    widget.deleteLater()
    model.close()
    pump()
    report["one_time_bytes"] = one_time

    # The stress budget holds one forecast time, not two.
    budget = NativeWeatherBudget(int(one_time * 1.05))
    report["budget_bytes"] = budget.byte_limit
    model, widget = make(budget)
    frames = {}
    phases = []
    try:
        for name, hours in (("A1", 1), ("B1", 3), ("A2", 1), ("B2", 3)):
            began = time.time()
            widget.set_camera(camera(hours))
            wait(lambda: widget._state == "ready", name, widget)
            for _ in range(3):
                pump()
                pixels = _grab(widget)
            frames.setdefault(name[0], []).append(pixels)
            phases.append(
                {
                    "name": name,
                    "began": began,
                    "ended": time.time(),
                    "native": widget.native_observations(),
                }
            )
        report["phases"] = phases
        stats = budget.statistics()
        report["budget_statistics"] = stats
        report["checks"]["budget_held_one_time"] = stats["limit_bytes"] < 2 * one_time
        report["checks"]["pressure_evicted"] = stats["evictions"] > 0
        report["checks"]["budget_never_exceeded"] = (
            stats["peak_bytes"] <= budget.byte_limit
        )
        report["checks"]["reload_pixels_identical"] = np.array_equal(
            frames["A"][0], frames["A"][1]
        ) and np.array_equal(frames["B"][0], frames["B"][1])
    finally:
        owners = tuple(widget._native_contexts.values())
        widget.dispose()
        widget.close()
        widget.deleteLater()
        pump()
        report["retired"] = [owner.snapshot() for owner in owners]
        report["final_budget"] = budget.statistics()
        report["checks"]["resources_released"] = (
            report["final_budget"]["current_bytes"] == 0
            and report["final_budget"]["contexts"] == 0
            and all(
                s.get("created", 0) > 0
                and s.get("created") == s.get("destroyed")
                and not s["errors"]
                for s in report["retired"]
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = Path(os.environ["GWR_NATIVE_WEATHER_GRIB"]).resolve()
    if not os.environ.get("GWR_NATIVE_STRESS_CHILD"):
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
                GWR_NATIVE_STRESS_CHILD="1",
                GWR_UPLOAD_TRACE_OUTPUT=str(trace),
            ),
            timeout=115,
            check=False,
        )
        report = json.loads(args.output.read_text())
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "check_native_weather_integration",
            ROOT / "tools/check_native_weather_integration.py",
        )
        integration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(integration)
        classify = integration.classify

        events = [json.loads(line) for line in trace.read_text().splitlines()]
        report["upload_counts"] = classify(events, report.get("phases", []))
        status = next((e for e in events if e["event"] == "process_status"), {})
        report["native_exit"] = status
        report["checks"]["reload_uploaded"] = report["upload_counts"].get("A2") not in (
            None,
            0,
        ) and report["upload_counts"].get("B2") not in (None, 0)
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
