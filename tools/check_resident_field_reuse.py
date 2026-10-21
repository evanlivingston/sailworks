"""Bounded #64 covered-demand mechanism check, using a real HRRR download.

This is not a hardware-upload or presentation measurement. The old chart-inclusive
scrub gate remains unchanged. GWR_RESIDENCY_FORECAST can reuse a local GRIB;
otherwise GWR_NETWORK_TESTS=1 explicitly permits a small NOAA download.
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
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def forecast(directory):
    from gweatherrouting.core.grib import Grib
    from gweatherrouting.core.gribsources.noaahrrr import NOAAHRRRSource
    from gweatherrouting.core.modeldownloads import install_download

    supplied = os.environ.get("GWR_RESIDENCY_FORECAST")
    if supplied:
        path = Path(supplied).resolve()
        return Grib.parse_metadata(str(path))
    if os.environ.get("GWR_NETWORK_TESTS") != "1":
        raise RuntimeError("set GWR_NETWORK_TESTS=1 or GWR_RESIDENCY_FORECAST")
    source = NOAAHRRRSource()
    source.bounds = (47.5, -124.5, 49.5, -122.0)
    rows = source.get_download_list()
    if not rows:
        raise RuntimeError("NOAA has no available HRRR forecast")
    return install_download(source, rows[0], directory, lambda _percent: None)


def measure(source):
    warm_began = time.time()
    from PySide6.QtCore import qVersion
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication
    from viewport_texture_probe import TextureProbe

    from gweatherrouting.rendering.weathercache import DisplayGridIndex
    from gweatherrouting.qt.gribviewport import (
        GribViewportModel,
        ViewportCamera,
        _covers,
        _viewport_widget_class,
    )

    app = QApplication.instance() or QApplication([])
    model = GribViewportModel([source.path])
    models = [model]
    widget = _viewport_widget_class()(model)
    textures = TextureProbe(widget)

    def pump(seconds=0.1):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)

    try:
        camera = ViewportCamera(
            center_lat=48.5,
            center_lon=-123.25,
            zoom=9,
            # Exact forecast instants have singleton identities. Warm inside
            # the bracket so all later inputs really are covered by this field.
            time=source.start_time + timedelta(hours=1, minutes=15),
            density="standard",
            over_land=True,
        )
        widget.resize(camera.width, camera.height)
        widget.show()
        widget.set_camera(camera)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            pump()
            if widget._state == "unavailable":
                raise RuntimeError(model.error)
            if widget._state == "ready" and model._active is None:
                break
        if widget._state != "ready":
            raise RuntimeError("weather did not become ready")

        # Isolate the covered-demand mechanism, not speculative scheduling.
        # No publication/readback timing is interpreted as performance evidence.
        with model._condition:
            model._recentre_if_needed = lambda *_args: None
            model._queue_window_fill = lambda *_args: None
            model._pending = None
        deadline = time.monotonic() + 10
        while model._active is not None and time.monotonic() < deadline:
            pump()
        if model._active is not None:
            raise RuntimeError("preparation did not quiesce")
        pump(0.3)
        resident = model._field
        before = widget.publication_counters()
        textures_before = textures.snapshot()
        preparations = model.counters["field_preparations"]
        misses = model.counters["misses"]
        array_calls = []
        original = DisplayGridIndex.array

        def forbidden_array(self, record):
            array_calls.append(True)
            raise RuntimeError("covered demand attempted decoded-band access")

        DisplayGridIndex.array = forbidden_array
        inputs = []
        input_began = time.time()
        try:
            # Stay in the same temporal bracket: blend changes, field does not.
            for offset, minutes in ((0.001, 0), (-0.001, 0), (0, 15), (0, 30)):
                demand = replace(
                    camera,
                    center_lon=camera.center_lon + offset,
                    time=camera.time + timedelta(minutes=minutes),
                )
                covered = _covers(resident, demand)
                state = widget.set_camera(demand)
                pump()
                inputs.append(
                    {
                        "covered_before_input": covered,
                        "ready": state == "ready" and widget._state == "ready",
                        "same_field": model._field is resident,
                    }
                )
        finally:
            DisplayGridIndex.array = original
        after = widget.publication_counters()
        input_ended = time.time()
        textures_after = textures.snapshot()
        report = {
            "host": {
                "platform": QGuiApplication.platformName(),
                "graphics_api": widget.quickWindow()
                .rendererInterface()
                .graphicsApi()
                .name,
                "qt_version": qVersion(),
                "device_pixel_ratio": widget.devicePixelRatioF(),
            },
            "initialization_window": [warm_began, input_began],
            "trace_clock": "Unix wall time for cross-process phase attribution, not latency",
            "atlas_size_limit": os.environ.get("QSG_ATLAS_SIZE_LIMIT"),
            "input_window": [input_began, input_ended],
            "textures_before": textures_before,
            "textures_after": textures_after,
            "new_field_textures": textures_after["observed_field_textures"]
            - textures_before["observed_field_textures"],
            "texture_probe_ok": (
                textures_before["observed_field_textures"] >= 4
                and textures_after["samples"] > textures_before["samples"]
                and not textures_before["errors"]
                and not textures_after["errors"]
            ),
            "inputs": inputs,
            "field_preparations": model.counters["field_preparations"] - preparations,
            "misses": model.counters["misses"] - misses,
            "decoded_band_accesses": len(array_calls),
            "field_publications": after["field_publications"]
            - before["field_publications"],
            "selection_publications": after["selection_publications"]
            - before["selection_publications"],
            "retained_bytes": model.field_statistics()["current_bytes"],
            "field_byte_limit": model._store.byte_limit,
            "hardware_uploads_measured": False,
            "presentation_latency_measured": False,
            "limitations": [
                "Speculative recentre/lookahead disabled after warmup",
                "Same-field temporal bracket only; not cross-field resident-window switching",
                "Publications are not GPU uploads; GPU residency acceptance remains open",
                "No charts, shoreline filtering, eviction stress or original-sampler fidelity claim",
            ],
        }
        empty = GribViewportModel([])
        models.append(empty)
        widget.replace_model(empty)
        pump(0.5)
        source_cleared = textures.snapshot()
        report["source_retirement"] = {
            "old_weather_hidden": not widget._weather.isVisible(),
            "provider_bytes": widget._provider.field_bytes(),
            "live_textures": source_cleared["live_textures"],
            "destroyed_textures": source_cleared["destroyed_textures"],
        }
        replacement = GribViewportModel([source.path])
        models.append(replacement)
        widget.replace_model(replacement)
        widget.set_camera(camera)
        deadline = time.monotonic() + 20
        while widget._state != "ready" and time.monotonic() < deadline:
            pump()
        report["source_reload_ready"] = widget._state == "ready"
        return report
    finally:
        from PySide6.QtCore import QCoreApplication, QEvent

        textures.detach()
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for owner in models:
            owner.close()
            if owner._thread is not None:
                owner._thread.join(5)
        if "report" in locals():
            report["teardown"] = {
                "textures": textures.snapshot(),
                "workers_stopped": all(
                    owner._thread is None or not owner._thread.is_alive()
                    for owner in models
                ),
                "retained_field_bytes": sum(
                    owner.field_statistics()["current_bytes"] for owner in models
                ),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GWR_QRHI_TRACE") == "1" and not os.environ.get("GWR_QRHI_CHILD"):
        trace_path = args.output.with_suffix(".uploads.jsonl")
        trace_path.write_text("")
        env = dict(
            os.environ, GWR_QRHI_CHILD="1", GWR_UPLOAD_TRACE_OUTPUT=str(trace_path)
        )
        # The debugger instruments the unchanged Qt Image-provider path, rather
        # than replacing it with a diagnostic texture factory.
        result = subprocess.run(
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
                "--",
                sys.executable,
                str(Path(__file__).resolve()),
                "--output",
                str(args.output),
            ],
            env=env,
            timeout=100,
            check=False,
        )
        if result.returncode != 0 or not args.output.is_file():
            raise RuntimeError("QRhi debugger run did not produce a successful report")
        report = json.loads(args.output.read_text())
        events = [json.loads(line) for line in trace_path.read_text().splitlines()]
        report["upload_trace"] = classify_uploads(events, report)
        report["hardware_uploads_measured"] = False
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        return
    with tempfile.TemporaryDirectory(prefix="gwr-residency-") as directory:
        source = forecast(directory)
        digest = hashlib.sha256(Path(source.path).read_bytes()).hexdigest()
        report = measure(source)
        report["forecast_sha256"] = digest
        report["forecast_name"] = Path(source.path).name
        args.output.write_text(json.dumps(report, indent=2) + "\n")


def classify_uploads(events, report):
    uploads = [event for event in events if event["event"] == "uploadTexture"]
    fields = set(report["textures_before"]["rhi_field_pointers"])
    warm_began, _warm_ended = report["initialization_window"]
    began, ended = report["input_window"]
    return {
        "endpoint": "QRhiResourceUpdateBatch::uploadTexture submission, not GPU completion",
        "configured": any(event["event"] == "configured" for event in events),
        "warm_field_submissions": sum(
            e["texture"] in fields and warm_began <= e["time"] < began for e in uploads
        ),
        "input_field_submissions": sum(
            e["texture"] in fields and began <= e["time"] <= ended for e in uploads
        ),
        "input_all_submissions": sum(began <= e["time"] <= ended for e in uploads),
        "field_resources_identifiable": len(fields) == 4
        and not report["textures_before"]["field_atlas_observed"],
    }


if __name__ == "__main__":
    main()
