"""N2: actual presenter lifecycle and shipped barb/fill shaders, saved real GRIB.

Set GWR_NATIVE_WEATHER_GRIB to a local forecast. No downloads or user-data writes.
QRhi submission tracing is macOS-only; framebuffer reads are not presentation FPS.
"""

import argparse
from dataclasses import replace
from datetime import timedelta
import hashlib
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
    "presenter_real_barbs",
    "session_budget_shared",
    "presenter_suspend_released",
    "presenter_resume",
    "source_clear_released",
    "stale_callback_rejected",
    "native_layers_draw",
    "finite_pixels_identical",
    "all_missing_zero_alpha",
    "covered_reuse",
    "budget_respected",
    "resources_released",
    "warm_upload_positive",
    "covered_zero_upload",
    "resident_return_reused",
    "new_interval_upload_positive",
    "resident_return_zero_upload",
    "clean_native_exit",
)


def run(path, report):
    import numpy as np
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, qVersion
    from PySide6.QtWidgets import QApplication

    from gweatherrouting.qt.gpuweather import encode_component
    from gweatherrouting.qt.gpuweathertextures import NativeWeatherBudget
    from gweatherrouting.qt.gribviewport import (
        GribViewportModel,
        ViewportCamera,
        _viewport_widget_class,
    )
    from test.qt_gribviewport_checks import _grab
    from test.qt_preview_checks import QtPreviewChecks
    from test.test_weather_barbs import Land

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    report["qt"] = qVersion()
    report["fixture"] = {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }

    def pump():
        loop = QEventLoop()
        QTimer.singleShot(25, loop.quit)
        loop.exec()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait(predicate, label, widget=None):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            pump()
            if widget is not None:
                widget.grabFramebuffer()
            if predicate():
                return
        raise RuntimeError("Timed out: " + label)

    # Reuse the normal application's sandbox fixture, not its GPU experiment.
    fixture = QtPreviewChecks()
    fixture.app = app
    fixture.setUp()
    try:
        controller = fixture.controller
        assert controller.native_weather_fields
        controller.settings.gribArrowOnGround = True
        controller.settings.gribBarbDensity = "standard"
        from types import SimpleNamespace

        controller.core.grib_manager.local_gribs = [SimpleNamespace(path=str(path))]
        selection = controller.display_forecast
        selection.refresh()
        wait(lambda: bool(selection.choices), "forecast metadata")
        choice = selection.choices[0]
        selection.choose(choice.key)
        canvas = fixture.window.map
        camera_state = canvas.camera_state
        camera_state.center_lat, camera_state.center_lon, camera_state.zoom = (
            48.5,
            -123.25,
            9,
        )
        canvas.time_control.set_time(choice.start_time + timedelta(hours=1))
        presenter = canvas._weather_presenter
        presenter.sync()
        wait(
            lambda: presenter.widget is not None and presenter.widget._state == "ready",
            "presenter real forecast",
        )
        widget = presenter.widget
        pixels = _grab(widget)
        report["presenter"] = widget.native_observations()
        report["checks"]["presenter_real_barbs"] = (
            bool((pixels[..., 3] > 25).any()) and not presenter.reason
        )
        from gweatherrouting.qt.viewportmap import ViewportMap

        sibling = ViewportMap(controller)
        fixture.addCleanup(sibling.close)
        sibling.resize(640, 480)
        sibling_camera = sibling.camera_state
        sibling_camera.center_lat, sibling_camera.center_lon, sibling_camera.zoom = (
            camera_state.center_lat,
            camera_state.center_lon,
            camera_state.zoom,
        )
        sibling.show()
        sibling_presenter = sibling._weather_presenter
        sibling_presenter.sync()
        wait(
            lambda: sibling_presenter.widget is not None
            and sibling_presenter.widget._state == "ready",
            "second application map",
        )
        sibling_widget = sibling_presenter.widget
        report["shared_maps_budget"] = controller.native_weather_budget.statistics()
        report["checks"]["session_budget_shared"] = (
            widget._native_budget
            is sibling_widget._native_budget
            is controller.native_weather_budget
            and report["shared_maps_budget"]["contexts"] == 8
            and report["shared_maps_budget"]["live_bytes"] > 0
        )
        sibling.hide()
        wait(
            lambda: controller.native_weather_budget.statistics()["contexts"] == 4,
            "sibling map retirement",
        )
        owners = tuple(widget._native_contexts.values())
        old_generation = presenter._host_generation
        canvas.hide()
        wait(
            lambda: controller.native_weather_budget.statistics()["current_bytes"] == 0,
            "presenter suspend retirement",
        )
        report["checks"]["presenter_suspend_released"] = presenter.widget is None
        presenter._native_frame_rendered(old_generation)
        report["checks"]["stale_callback_rejected"] = not presenter._rendered
        canvas.show()
        presenter.sync()
        wait(
            lambda: presenter.widget is not None and presenter.widget._state == "ready",
            "presenter resume",
        )
        report["checks"]["presenter_resume"] = presenter.widget is not widget
        owners += tuple(presenter.widget._native_contexts.values())
        selection.choose("")
        wait(
            lambda: controller.native_weather_budget.statistics()["current_bytes"] == 0,
            "source clear retirement",
        )
        report["checks"]["source_clear_released"] = presenter.widget is None
        report["presenter_retired"] = [owner.snapshot() for owner in owners]
    finally:
        fixture.doCleanups()
        pump()

    # Both shipped layers: byte-identical output against the unchanged Image
    # path on the same real forecast, then covered time/pan and missing controls.
    camera = ViewportCamera(
        48.5, -123.25, 9, 640, 480, choice.start_time + timedelta(hours=1)
    )
    budget = NativeWeatherBudget()
    widgets = []
    try:
        frames = []
        for native in (False, True):
            model = GribViewportModel([path], land_mask=Land(), grid=True)
            widget = _viewport_widget_class()(
                model,
                gpu_weather_fill=True,
                native_weather_fields=native,
                native_weather_budget=budget if native else None,
            )
            widgets.append(widget)
            widget.resize(640, 480)
            widget.show()
            began = time.time()
            widget.set_camera(camera)
            wait(
                lambda: model.state == "ready"
                and (
                    widget._state == "ready"
                    if native
                    else widget._barbs.property("imagesReady")
                    and widget._fill.property("imagesReady")
                ),
                "real barb/fill layers",
                widget,
            )
            for _ in range(3):
                pump()
                pixels = _grab(widget)
            frames.append(pixels)
            if native:
                report["phases"].append(
                    {
                        "name": "cold",
                        "began": began,
                        "ended": time.time(),
                        "native": widget.native_observations(),
                    }
                )
            else:
                widget.hide()
        report["checks"]["native_layers_draw"] = bool((frames[1][..., 3] > 25).any())
        report["pixel_difference_bytes"] = int(np.count_nonzero(frames[0] != frames[1]))
        report["checks"]["finite_pixels_identical"] = np.array_equal(*frames)
        native_widget = widgets[1]
        before = native_widget.native_observations()
        prepared_before = native_widget._model.band_statistics()
        began = time.time()
        native_widget.set_camera(
            replace(
                camera,
                center_lon=camera.center_lon + 0.001,
                time=camera.time + timedelta(minutes=1),
            )
        )
        wait(lambda: native_widget._state == "ready", "covered time/pan", native_widget)
        for _ in range(3):
            pump()
            _grab(native_widget)
        after = native_widget.native_observations()
        report["phases"].append(
            {"name": "covered", "began": began, "ended": time.time(), "native": after}
        )
        report["model_before"] = prepared_before
        report["model_after"] = native_widget._model.band_statistics()
        report["checks"]["covered_reuse"] = (
            all(
                before["layers"][role]["created"] == after["layers"][role]["created"]
                for role in ("barbs", "fill")
            )
            and report["model_before"] == report["model_after"]
        )
        # Exercise two distinct real forecast brackets, then return to A while
        # it is still admitted. Returning to A must reuse actual QRhi resources,
        # not merely the CPU image payloads.
        for name, target in (
            ("interval_B", replace(camera, time=camera.time + timedelta(hours=3))),
            ("return_A", camera),
        ):
            began = time.time()
            native_widget.set_camera(target)
            wait(lambda: native_widget._state == "ready", name, native_widget)
            for _ in range(3):
                pump()
                pixels = _grab(native_widget)
            report["phases"].append(
                {
                    "name": name,
                    "began": began,
                    "ended": time.time(),
                    "native": native_widget.native_observations(),
                    "bands": native_widget._model.band_statistics(),
                }
            )
        returned = report["phases"][-1]
        middle = report["phases"][-2]
        report["checks"]["resident_return_reused"] = (
            np.array_equal(pixels, frames[1])
            and all(
                returned["native"]["layers"][role]["created"]
                == middle["native"]["layers"][role]["created"]
                and returned["native"]["layers"][role]["current"]
                == before["layers"][role]["current"]
                for role in ("barbs", "fill")
            )
            and returned["bands"] == middle["bands"]
        )
        field = native_widget._model.update(native_widget._camera)[1]

        def missing(images):
            return tuple(
                encode_component(
                    np.full((image.height(), image.width() // 2), np.nan, np.float32)
                )
                for image in images
            )

        native_widget._bind_native_layer("barbs", "all-missing", missing(field.images))
        native_widget.publish_fill(missing(field.grid.images), "all-missing")
        wait(
            lambda: native_widget._barbs.property("ready")
            and native_widget._fill.property("ready"),
            "missing native endpoints",
            native_widget,
        )
        pixels = _grab(native_widget)
        report["missing_alpha_pixels"] = int(np.count_nonzero(pixels[..., 3]))
        report["checks"]["all_missing_zero_alpha"] = report["missing_alpha_pixels"] == 0
        report["checks"]["budget_respected"] = (
            budget.statistics()["peak_bytes"] <= budget.byte_limit
        )
    finally:
        owners = tuple(
            owner for widget in widgets for owner in widget._native_contexts.values()
        )
        for widget in widgets:
            widget.dispose()
            widget.close()
            widget.deleteLater()
        pump()
        report["retired"] = [owner.snapshot() for owner in owners]
        report["final_budget"] = budget.statistics()
        report["checks"]["resources_released"] = (
            len(report["retired"]) == 4
            and report["final_budget"]["current_bytes"] == 0
            and report["final_budget"]["contexts"] == 0
            and all(
                s.get("created", 0) > 0
                and s.get("created") == s.get("destroyed")
                and not s["errors"]
                for s in report["retired"]
            )
        )


def classify(events, phases):
    counts = {}
    for phase in phases:
        layers = phase["native"]["layers"]
        groups = [
            group
            for layer in layers.values()
            for group in layer.get("groups", [])
            if group["identity"] == layer.get("current")
        ]
        handles = {handle for group in groups for handle in group["handles"] if handle}
        # Missing attribution is unavailable, never measured zero.
        counts[phase["name"]] = (
            None
            if len(groups) != len(layers) or len(handles) != 4 * len(layers)
            else sum(
                e["event"] == "uploadTexture"
                and e.get("texture") in handles
                and phase["began"] <= e["time"] <= phase["ended"]
                for e in events
            )
        )
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = Path(os.environ["GWR_NATIVE_WEATHER_GRIB"]).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if not os.environ.get("GWR_NATIVE_INTEGRATION_CHILD"):
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
                GWR_NATIVE_INTEGRATION_CHILD="1",
                GWR_NATIVE_WEATHER_FIELDS="1",
                GWR_UPLOAD_TRACE_OUTPUT=str(trace),
            ),
            timeout=115,
            check=False,
        )
        report = json.loads(args.output.read_text())
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        report["upload_counts"] = classify(events, report["phases"])
        status = next((e for e in events if e["event"] == "process_status"), {})
        report["native_exit"] = status
        report["checks"].update(
            warm_upload_positive=report["upload_counts"].get("cold") == 8,
            covered_zero_upload=report["upload_counts"].get("covered") == 0,
            new_interval_upload_positive=report["upload_counts"].get("interval_B") == 8,
            resident_return_zero_upload=report["upload_counts"].get("return_A") == 0,
            clean_native_exit=status.get("exited") is True
            and status.get("exit_code") == 0,
        )
    else:
        report = {
            "checks": dict.fromkeys(CHECKS, False),
            "phases": [],
            "presentation_latency_measured": False,
            "physical_vram_measured": False,
        }
        try:
            run(path, report)
        except Exception:
            report["error"] = traceback.format_exc()
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")


if __name__ == "__main__":
    main()
