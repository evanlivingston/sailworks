#!/usr/bin/env python3
"""Full-app GPU map smoke: the real window must present on a hardware backend.

This is the prerequisite gate for making the map renderer GPU-only.  It opens
the shipping ``PreviewWindow`` with ``map_renderer="gpu"`` and waits for the
viewport to publish a hardware Qt Quick backend.  An explicit CPU fallback or
unavailable state fails with the reported reason; it is never reported as a
pass, and a later readback is not presentation timing evidence.

Run it on a native display session (not offscreen): the point is the backend
the desktop actually selects.  Exit codes: 0 hardware GPU presented, 1 the
application reported a fallback or never presented, 2 cannot run here.
"""

import argparse
import json
import sys
import time


def status_kind(status):
    """Classify ``SessionController.renderer_status()`` for the smoke gate."""
    text = (status or "").strip()
    lowered = text.lower()
    if "waiting" in lowered or "requested" in lowered:
        # "GPU requested; waiting for a visible map" is not a presented frame.
        return "pending"
    if "fallback" in lowered:
        return "fallback"
    if lowered.startswith("gpu"):
        return "gpu"
    if lowered.startswith("cpu") or "unavailable" in lowered:
        return "unavailable"
    return "pending"


def _record(window, controller):
    presenter = getattr(getattr(window, "map", None), "_weather_presenter", None)
    widget = getattr(presenter, "widget", None)
    return {
        "renderer_requested": getattr(controller, "map_renderer", None),
        "renderer_status": controller.renderer_status(),
        "viewport_widget": type(widget).__name__ if widget is not None else None,
        "presented": bool(getattr(presenter, "_rendered", False)),
    }


def run(timeout, report_path=""):
    """Open the full window on the GPU renderer and report the real backend."""
    from PySide6.QtWidgets import QApplication

    from sailworks.core.marine.credentials import SystemCredentialStore
    from sailworks.qt.controller import SessionController
    from sailworks.qt.theme import apply_theme
    from sailworks.qt.window import PreviewWindow

    # The smoke does not need stored credentials; do not prompt for a keychain.
    SystemCredentialStore.load = lambda _self: None
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    controller = SessionController(map_renderer="gpu")
    window = PreviewWindow(controller)
    record = {}
    try:
        window.show()
        deadline = time.monotonic() + max(1.0, float(timeout))
        kind = "pending"
        while time.monotonic() < deadline:
            app.processEvents()
            record = _record(window, controller)
            kind = status_kind(record["renderer_status"])
            if kind in ("gpu", "fallback", "unavailable"):
                break
            time.sleep(0.02)
        record["kind"] = kind
        record["timed_out"] = kind == "pending"
    finally:
        try:
            window.close()
            window.deleteLater()
            app.processEvents()
        except Exception:
            pass
    if report_path:
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, default=str)
            handle.write("\n")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    try:
        record = run(args.timeout, args.report)
    except Exception as error:  # a smoke that cannot open the app is inconclusive
        print(json.dumps({"kind": "cannot-run", "error": str(error)}, default=str))
        return 2
    print(json.dumps(record, default=str))
    if record["kind"] == "gpu" and record.get("presented"):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
