#!/usr/bin/env python3
"""Exercise default ENC through the actual application ViewportMap/Weather panel."""

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from check_enc_viewport import _load_tool, _wait  # noqa: E402


def sandbox_settings(settings):
    """The sandbox settings a scenario runs with.

    The GPU ENC scene is the application's only ENC presenter, so there is no
    scene switch to write: this copy only enables the charts and the density
    the viewport serves.  It is written to the sandbox fixture, never the
    user's own settings file.
    """
    updated = dict(settings)
    updated["chartENCVisible"] = True
    updated["gribBarbDensity"] = "very_dense"
    # A failed renderer is surfaced by the application's own developer status
    # label (``WeatherPresenter._label`` hides it otherwise).  The sandbox copy
    # turns that mode on so the gate asserts the real display path instead of
    # claiming a deliberately hidden label is visible.
    updated["developerMode"] = True
    return updated


def scene_presenter_active(presenter, widget):
    """Scene-specific proof that the presenter owns the GPU ENC scene.

    ``presenter.enc_active`` names the *raster* ENC source: it requires
    ``widget._enc_chart_layer``.  ``resolve_enc_modes`` clears that flag
    whenever the scene layer is on (a cell is drawn once, never twice), so
    ``enc_active`` is always false on the scene path and can never prove the
    scene.  The scene's own layer flag, model and item are what prove it.
    """
    return bool(
        getattr(presenter, "active", False)
        and widget is not None
        and getattr(widget, "_enc_scene_layer", False)
        and getattr(widget, "_enc_scene_model", None) is not None
        and getattr(widget, "_enc_scene_item", None) is not None
    )


def scene_item_offset(item):
    """The container-local ``x``/``y`` the item actually draws at, or ``None``."""
    reader = getattr(item, "read_offset", None)
    if callable(reader):
        try:
            offset = reader()
        except (RuntimeError, TypeError, ValueError):
            return None
        if offset is None:
            return None
        try:
            return (float(offset[0]), float(offset[1]))
        except (IndexError, TypeError, ValueError):
            return None
    offset = []
    for name in ("x", "y"):
        value = getattr(item, name, None)
        if callable(value):
            try:
                value = value()
            except (RuntimeError, TypeError, ValueError):
                return None
        if value is None:
            return None
        try:
            offset.append(float(value))
        except (TypeError, ValueError):
            return None
    return tuple(offset)


def retained_scene_placement(widget, camera, metadata):
    """The transform the retained frame draws at beside the one it needs.

    A pan is carried by the shared ``mapContent`` translation: the item keeps
    its container-local offset and the parent moves it.  The published mesh's
    build camera therefore stops equalling the application camera on a
    retained pan even though the frame on screen is correct.  The applied
    truth is the item's own offset beside ``enc_scene_placement(reference,
    frame_camera)``: when they agree, the parent's translation lands the frame
    exactly on ``camera``.  A frame not shifted on a rebase, a frame whose
    scale cannot be translated, or a frame composed for another time all fail
    this comparison, so a stale retained frame still fails.
    """
    from sailworks.qt.viewporttransform import enc_scene_placement, same_frame

    frame = getattr(widget, "_enc_scene_frame_camera", None)
    if frame is None:
        frame = getattr(widget, "_enc_scene_camera", None)
    reference = getattr(widget, "_map_pan_reference", None)
    item = getattr(widget, "_enc_scene_item", None)
    dpr = float(getattr(widget, "_camera_dpr", 1.0) or 1.0)
    complete = bool(metadata is not None and not metadata.get("partial"))
    try:
        same_scale = bool(
            frame is not None and camera is not None and same_frame(frame, camera)
        )
    except (AttributeError, TypeError, ValueError):
        same_scale = False
    same_time = bool(
        frame is not None
        and camera is not None
        and getattr(frame, "time", None) == getattr(camera, "time", None)
    )
    expected = None
    applied = None
    if complete and same_scale and same_time and reference is not None:
        # Never let a failed translation read as a zero offset: (0, 0) is a
        # real placement, so a ValueError must leave the frame unproven.
        try:
            reference.translation_to(frame, dpr)
        except ValueError:
            pass
        else:
            expected = enc_scene_placement(reference, frame, dpr)
            applied = scene_item_offset(item)
    valid = bool(
        expected is not None
        and applied is not None
        and abs(applied[0] - expected[0]) <= 0.5
        and abs(applied[1] - expected[1]) <= 0.5
    )
    return {
        "frame_camera": None if frame is None else str(frame),
        "applied_offset": applied,
        "expected_offset": expected,
        "same_frame": same_scale,
        "same_time": same_time,
        "valid": valid,
    }


def failure_surface(presenter, controller, reason, developer_mode):
    """The application's real failure contract, field by field.

    ``fail`` records the reason, suspends the presenter, retires the widget
    and reports the viewport unavailable to the controller.  The on-canvas
    status label is developer-only by design: ``_label`` hides it unless
    ``developerMode`` is set.  The check therefore asserts the label is shown
    and carries the reason when that mode is on, and stays hidden when it is
    off; either way the renderer status must name the failure.
    """
    status = getattr(presenter, "status", None)
    is_visible = getattr(status, "isVisible", None)
    try:
        visible = bool(is_visible()) if callable(is_visible) else False
    except RuntimeError:
        visible = False
    text_of = getattr(status, "text", None)
    try:
        text = str(text_of()) if callable(text_of) else ""
    except RuntimeError:
        text = ""
    recorded = str(getattr(presenter, "reason", "") or "")
    renderer = str(controller.renderer_status())
    widget = getattr(presenter, "widget", None)
    snapshot = {
        "presenter_active": bool(getattr(presenter, "active", False)),
        "enc_active": bool(getattr(presenter, "enc_active", False)),
        "scene_active": scene_presenter_active(presenter, widget),
        "widget_retired": widget is None,
        "reason": recorded,
        "reason_recorded": reason in recorded,
        "renderer_status": renderer,
        "renderer_unavailable": (
            "unavailable" in renderer.lower() and reason in renderer
        ),
        "status_visible": visible,
        "status_text": text,
        "developer_mode": bool(developer_mode),
    }
    snapshot["status_contract"] = (
        (visible and reason in text) if snapshot["developer_mode"] else not visible
    )
    snapshot["ok"] = bool(
        not snapshot["presenter_active"]
        and snapshot["widget_retired"]
        and snapshot["reason_recorded"]
        and snapshot["renderer_unavailable"]
        and snapshot["status_contract"]
    )
    return snapshot


def scene_presentation(widget, camera=None):
    """Published, adopted and drawn truth for the viewport's GPU ENC scene.

    Read from the layer's own publication, the widget's adoption bookkeeping
    and the item that is actually on screen.  A setting, a constructed layer,
    a ready weather backend or a ready raster chart is never a substitute for
    those three: the scene gate fails when the frame was not published, not
    adopted, or draws no vertices into the viewport.
    """
    from sailworks.qt.gribviewport import scene_presents_chart

    layer = getattr(widget, "_enc_scene_model", None)
    item = getattr(widget, "_enc_scene_item", None)
    if camera is None:
        camera = getattr(widget, "_camera", None)
    published = getattr(layer, "published", None)
    generation = None if published is None else published[0]
    metadata = None if published is None else published[2]
    ready_generation = getattr(widget, "_enc_scene_ready_generation", None)
    shown = 0
    covers = False
    visible = False
    item_generation = None
    uploads = None
    unsupported = None
    if item is not None:
        count = getattr(item, "shown_vertex_count", None)
        shown = int(count()) if callable(count) else int(count or 0)
        reaches = getattr(item, "scene_covers_view", None)
        covers = bool(reaches()) if callable(reaches) else bool(reaches)
        is_visible = getattr(item, "is_visible", None)
        visible = bool(is_visible()) if callable(is_visible) else False
        item_generation = getattr(item, "generation", None)
        uploads = getattr(item, "upload_count", None)
        unsupported = getattr(item, "unsupported_count", None)
    complete = bool(metadata is not None and not metadata.get("partial"))
    drawn = bool(visible and shown > 0 and covers)
    adopted = bool(
        complete
        and ready_generation is not None
        and ready_generation == generation
        and item_generation is not None
    )
    retained = retained_scene_placement(widget, camera, metadata)
    # A frame composed for exactly this camera presents directly.  A frame
    # retained across a pan or zoom presents too when the transform it draws
    # at still lands on this camera; the mesh's build camera alone cannot
    # decide that, because the shared container carries the pan.
    presented = bool(
        complete
        and camera is not None
        and (
            scene_presents_chart(metadata, camera, item)
            or (retained["valid"] and drawn)
        )
    )
    return {
        "constructed": layer is not None and item is not None,
        "state": getattr(layer, "state", None),
        "error": getattr(layer, "error", None),
        "incomplete": getattr(layer, "incomplete", None),
        "published_generation": generation,
        "ready_generation": ready_generation,
        "item_generation": item_generation,
        "published_camera": None if metadata is None else metadata.get("camera"),
        "retained": retained,
        "partial": None if metadata is None else bool(metadata.get("partial")),
        "cells_order": (
            () if metadata is None else tuple(metadata.get("cells_order", ()))
        ),
        "cell_states": (
            () if metadata is None else tuple(metadata.get("cell_states", ()))
        ),
        "shown_vertices": shown,
        "covers_view": covers,
        "visible": visible,
        "uploads": uploads,
        "unsupported": unsupported,
        "text_labels": len(getattr(item, "text_labels", ()) or ()),
        "drawn": drawn,
        "adopted": adopted,
        "presented": presented,
    }


def application_camera_mismatch(canvas, camera):
    """Field-level differences between a scene camera and the application view.

    A single boolean cannot say *why* the viewport's camera is not the
    application's, which is exactly the evidence a failing gate needs.  Fields
    are named individually; an empty mapping means the camera is this view.
    """
    if camera is None:
        return {"camera": "missing"}
    expected = {
        "center_lat": canvas._center_lat,
        "center_lon": canvas._center_lon,
        "zoom": canvas._zoom,
        "width": canvas.width(),
        "height": canvas.height(),
        "time": canvas.time_control.time,
    }
    return {
        field: {"camera": str(getattr(camera, field, None)), "application": str(want)}
        for field, want in expected.items()
        if getattr(camera, field, None) != want
    }


def scene_gate(widget, presenter, canvas):
    """One snapshot of every input to the scene gate.

    ``ready`` and the recorded stage both derive from this dict, so the report
    cannot show an adopted, presented and drawn scene beside a false top-level
    check without naming the input that failed.
    """
    view = None if widget is None else scene_presentation(widget)
    camera = None if widget is None else getattr(widget, "_camera", None)
    mismatch = (
        {"camera": "missing"}
        if widget is None
        else application_camera_mismatch(canvas, camera)
    )
    is_visible = getattr(widget, "isVisible", None)
    try:
        visible = bool(is_visible()) if callable(is_visible) else False
    except RuntimeError:
        # A deleted C++ object is not a visible widget.
        visible = False
    return {
        "presenter_active": bool(getattr(presenter, "active", False)),
        "presenter_enc_active": bool(getattr(presenter, "enc_active", False)),
        "presenter_scene_active": scene_presenter_active(presenter, widget),
        "widget_visible": visible,
        "camera_is_application_view": not mismatch,
        "camera_mismatch": mismatch,
        "scene": view,
    }


def scene_gate_presents(gate):
    """Whether a gate snapshot is the application's own ENC chart on screen."""
    view = gate.get("scene") or {}
    return bool(
        gate.get("presenter_active")
        and gate.get("presenter_scene_active")
        and gate.get("widget_visible")
        and gate.get("camera_is_application_view")
        and view.get("state") == "ready"
        and view.get("presented")
        and view.get("adopted")
        and view.get("drawn")
    )


def scene_checks(gate):
    """The published/adopted/drawn checks, from the snapshot that records them.

    Each check states one fact about the live scene, so a false aggregate names
    the fact that failed instead of collapsing every cause into "not ready".
    """
    view = gate.get("scene") or {}
    live = bool(
        gate.get("presenter_active")
        and gate.get("presenter_scene_active")
        and gate.get("widget_visible")
    )
    return {
        "scene_layer_constructed": bool(view.get("constructed")),
        "scene_published": bool(
            live
            and view.get("state") == "ready"
            and view.get("published_generation") is not None
            and not view.get("partial")
            and view.get("cells_order")
        ),
        "scene_adopted": bool(live and view.get("adopted")),
        "scene_drawn": bool(live and view.get("drawn")),
        "scene_matches_application_camera": bool(
            live and gate.get("camera_is_application_view") and view.get("presented")
        ),
    }


def moved_scene_presented(stage_record, before_generation):
    """Whether a pan or zoom left the application's current view presented.

    A fresh publication is one way to satisfy this, but reusing the retained
    scene is equally valid while that retained scene still describes the
    current camera: the layer re-publishes a retained frame unchanged, so its
    generation alone cannot decide.  Whether the generation advanced is
    recorded for review; a stale retained frame (adopted and drawn, wrong
    camera) still fails because ``presented`` is false.
    """
    record = stage_record or {}
    view = record.get("scene") or {}
    if stage_record is not None:
        stage_record["publication_advanced"] = bool(
            view.get("published_generation") != before_generation
        )
    return bool(scene_gate_presents(record.get("gate") or {}))


def current_application_view(canvas, widget):
    """A previous ready frame must not satisfy a new application view request."""
    camera = widget._camera
    if camera is None:
        return False
    expected = (
        canvas._center_lat,
        canvas._center_lon,
        canvas._zoom,
        canvas.width(),
        canvas.height(),
        canvas.time_control.time,
    )
    actual = (
        camera.center_lat,
        camera.center_lon,
        camera.zoom,
        camera.width,
        camera.height,
        camera.time,
    )
    if expected != actual:
        return False
    layer = widget._chart_model
    with layer._condition:
        if layer._published is None or not layer._reusable(camera):
            return False
    cache = canvas._environment_cache
    return cache is not None and widget._environment_key == cache[4].cacheKey()


def run(snapshot, output, application=None):
    began = time.perf_counter()
    report = {"checks": {}, "stages": {}, "complete": False}
    fixture = _load_tool(
        "app_enc_fixture", ROOT / "tools/benchmark_startup_rendering.py"
    )
    manifest = fixture.verify_snapshot(snapshot)
    report["snapshot_sha256"] = manifest["content_sha256"]
    root = Path(tempfile.mkdtemp(prefix="enc-app-rollout-"))
    try:
        data = fixture.prepare_sandbox(snapshot, root)
        # The application gate exercises the viewport path: the cells are
        # enabled and the density is one the viewport serves.  The "All
        # forecast points" path has its own gate.
        settings_path = data / "settings.json"
        sandbox = sandbox_settings(json.loads(settings_path.read_text()))
        settings_path.write_text(json.dumps(sandbox))
        report["notes"] = [
            "Sandbox copy: chartENCVisible and gribBarbDensity set for the "
            "viewport path; the user's own settings are untouched."
        ]
        os.environ["HOME"] = str(root / "home")
        os.environ["XDG_DATA_HOME"] = str(root / "xdg-data")
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")

        def no_network(*args, **kwargs):
            raise RuntimeError("Network disabled during ENC rollout acceptance")

        socket.socket.connect = no_network
        socket.socket.connect_ex = no_network
        if application is None:
            application = _scene_application
        application(report, data, output, began + 95)
    except Exception as exc:
        import traceback

        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    report["wall_s"] = round(time.perf_counter() - began, 3)
    report["timing_note"] = "Process wall time, not FPS or presentation latency"
    return report


def _scene_application(
    report, data, output, deadline
):  # noqa: C901 - one native session
    """Exercise the opt-in GPU ENC scene through the real application window.

    Nothing here compares pixels or claims parity.  It asks the native scene
    graph whether the ENC frame was published, whether the viewport adopted
    that generation and whether the item on screen draws vertices into the
    current view, then repeats the question after a pan and a zoom.  A pan or
    zoom passes when the current application camera is presented - a fresh
    publication or a retained scene that still describes that camera; the
    generation advance is recorded, not required.  Each stage records the
    snapshot its own ``ready`` flag and the top-level checks are computed
    from, so a false check always names the input that failed.  The layer's
    explicit incompleteness marker travels in the report unchanged.
    """
    from PySide6.QtWidgets import QApplication

    from sailworks.core.marine.credentials import SystemCredentialStore
    from sailworks.core.storage import DATA_DIR
    from sailworks.qt.controller import SessionController
    from sailworks.qt.viewportmap import ViewportMap
    from sailworks.qt.weatherpanel import WeatherPanel

    if Path(DATA_DIR) != data:
        raise RuntimeError("Application storage escaped the sandbox")
    SystemCredentialStore.load = lambda _self: None
    app = QApplication.instance() or QApplication([])
    controller = SessionController()
    canvas = ViewportMap(controller)
    canvas.resize(710, 743)
    panel = WeatherPanel(controller, lambda: canvas, lambda: None)
    report.setdefault("scene", {})

    def center():
        canvas._center_lat, canvas._center_lon, canvas._zoom = (
            48.1125,
            -122.7375,
            14.236,
        )
        canvas._invalidate_base()
        canvas.update()

    def wait(predicate, timeout=12):
        return _wait(predicate, max(0, min(timeout, deadline - time.perf_counter())))

    def ready():
        presenter = canvas._weather_presenter
        widget = presenter.widget
        if widget is None:
            return False
        return scene_gate_presents(scene_gate(widget, presenter, canvas))

    def capabilities():
        from PySide6.QtQuick import QSGRendererInterface

        widget = canvas._weather_presenter.widget
        quick = widget.quickWindow() if widget is not None else None
        backend = ""
        try:
            interface = quick.rendererInterface() if quick is not None else None
            api = interface.graphicsApi() if interface is not None else None
            backend = (
                QSGRendererInterface.GraphicsApi(api).name if api is not None else ""
            )
        except Exception:
            backend = ""
        return {
            "scene_graph_backend": backend,
            "requested_backend": os.environ.get("QSG_RHI_BACKEND", "default"),
            "device_pixel_ratio": (
                float(quick.devicePixelRatio()) if quick is not None else None
            ),
        }

    def stage(name):
        started = time.perf_counter()
        wait(ready)
        waited = time.perf_counter() - started
        presenter = canvas._weather_presenter
        widget = presenter.widget
        gate = scene_gate(widget, presenter, canvas)
        report["stages"][name] = {
            "ready": scene_gate_presents(gate),
            "wait_s": round(waited, 3),
            "fallback_reason": presenter.reason,
            "presenter": {
                "active": presenter.active,
                "scene_active": scene_presenter_active(presenter, widget),
                "closed": presenter.closed,
                "reason": presenter.reason,
                "widget_is_none": widget is None,
                "canvas_visible": canvas.isVisible(),
                "canvas_disposed": canvas.disposed,
            },
            "gate": gate,
            "scene": gate["scene"],
            "environment_ready": (
                None if widget is None else bool(widget._environment.property("ready"))
            ),
            "chart_state": None if widget is None else widget._chart_model.state,
            "renderer_status": controller.renderer_status(),
        }
        return report["stages"][name]["ready"]

    try:
        # The GPU scene is the application's only ENC presentation; the
        # weather renderer is still the viewport so this is the application's
        # own map host.
        controller.settings["weatherRenderer"] = "viewport"
        controller.settings["gribWindStyle"] = "barbs"
        controller.display_forecast.choose("")
        center()
        canvas.show()
        report["scene"]["mandatory"] = True
        stage("scene")
        scene_stage = report["stages"]["scene"]
        view = scene_stage["scene"] or {}
        report["scene"]["capabilities"] = capabilities()
        report["scene"]["published"] = {
            "generation": view.get("published_generation"),
            "cells": [
                record.get("name") if hasattr(record, "get") else str(record)
                for record in view.get("cells_order", ())
            ],
            "cell_states": list(view.get("cell_states", ())),
            "incomplete": view.get("incomplete"),
        }
        # The checks are read from the same snapshot the stage records, so a
        # presented and adopted scene can never sit beside a false check.
        report["checks"].update(scene_checks(scene_stage["gate"]))

        before = view.get("published_generation")
        canvas._center_lon += 0.01
        canvas._invalidate_base()
        canvas.update()
        stage("pan")
        report["checks"]["scene_pan_republishes"] = moved_scene_presented(
            report["stages"]["pan"], before
        )
        before = (report["stages"]["pan"]["scene"] or {}).get("published_generation")
        canvas._zoom += 0.25
        canvas._invalidate_base()
        canvas.update()
        stage("zoom")
        report["checks"]["scene_zoom_republishes"] = moved_scene_presented(
            report["stages"]["zoom"], before
        )

        output.parent.mkdir(parents=True, exist_ok=True)
        canvas.grab().save(str(output.with_suffix(".png")))
        failure_reason = "synthetic GPU scene failure for acceptance"
        controller.settings["developerMode"] = True
        canvas._weather_presenter.fail(failure_reason)
        report["checks"]["scene_failure_visible"] = wait(
            lambda: failure_surface(
                canvas._weather_presenter,
                controller,
                failure_reason,
                controller.settings.get("developerMode", False),
            )["ok"]
        )
        report["scene"]["failure"] = failure_surface(
            canvas._weather_presenter,
            controller,
            failure_reason,
            controller.settings.get("developerMode", False),
        )
        report["complete"] = all(report["checks"].values())
    finally:
        panel.close()
        canvas.close()
        controller.close()
        app.processEvents()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = run(args.snapshot, args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
