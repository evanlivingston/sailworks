#!/usr/bin/env python3
"""Diagnose whether the application base map (world coastlines) renders.

Read-only: it renders the application's own base image at the saved view and
reports what it contains. It changes no settings and saves nothing.
"""

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def application(report, output):  # noqa: C901 - one bounded diagnostic
    import numpy as np
    from PySide6.QtWidgets import QApplication
    from gweatherrouting.core.marine.credentials import SystemCredentialStore
    from gweatherrouting.qt.controller import SessionController

    # LEGACY-ONLY (TODO #86): diagnose_base_map probes TrackMap base-map internals
    # (_base_image, _sea_color, _invalidate_base, _base_chart_renderer); not retargeted.
    from gweatherrouting.qt.mapcanvas import TrackMap  # LEGACY-ONLY (TODO #86)

    SystemCredentialStore.load = lambda self: None
    app = QApplication.instance() or QApplication([])
    controller = SessionController()
    canvas = TrackMap(controller)  # LEGACY-ONLY (TODO #86)
    canvas.resize(710, 743)

    workspace = (
        Path.home() / "Library/Application Support/gweatherrouting/workspace-qt.json"
    )
    view = {
        "lat": 48.45003186114848,
        "lon": -124.28875762043288,
        "zoom": 9.380182039882325,
    }
    if workspace.is_file():
        state = json.loads(workspace.read_text()).get("state", {}).get("map", {})
        view = {
            "lat": state.get("latitude", view["lat"]),
            "lon": state.get("longitude", view["lon"]),
            "zoom": state.get("zoom", view["zoom"]),
        }
    canvas._center_lat, canvas._center_lon, canvas._zoom = (
        view["lat"],
        view["lon"],
        view["zoom"],
    )
    report["view"] = view
    report["settings"] = {
        key: controller.settings.get(key)
        for key in (
            "chartWorldVisible",
            "chartENCVisible",
            "chartRasterVisible",
            "chartMBTilesVisible",
            "chartCM93Visible",
            "chartSeamarksVisible",
            "chartVectorVisible",
            "canadianCharts",
            "chartPalette",
        )
    }
    gshhs = controller.charts.gshhsLayer
    report["gshhs_loaded"] = gshhs is not None
    report["gshhs_vector_files"] = (
        sorted(gshhs.vector_files) if gshhs is not None else []
    )
    report["gshhs_enabled"] = getattr(gshhs, "enabled", None)
    report["gshhs_force_downscale"] = getattr(gshhs, "force_downscale", None)
    report["chart_count"] = len(controller.charts.charts)
    report["map_scale"] = canvas.get_scale()
    if gshhs is not None:
        scale = canvas.get_scale()
        if scale > 5000:
            level = "c"
        elif scale > 500:
            level = "l"
        elif scale > 200 or gshhs.force_downscale:
            level = "i"
        elif scale >= 15:
            level = "h"
        else:
            level = "f"
        report["gshhs_level"] = level
        report["gshhs_level_file"] = str(gshhs.vector_files.get(level + "1"))

    began = time.perf_counter()
    image = canvas._base_image()
    report["base_image_wall_s"] = round(time.perf_counter() - began, 3)
    if image is None:
        report["base_image"] = "none"
        return
    data = (
        np.frombuffer(image.constBits(), np.uint8)
        .reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4]
        .reshape(image.height(), image.width(), 4)
    )
    report["base_image_size"] = [image.width(), image.height()]
    report["base_image_opaque_pixels"] = int((data[:, :, 3] > 0).sum())
    sea = canvas._sea_color()
    report["sea_colour"] = list(sea.getRgb()[:3])
    report["base_image_non_sea_pixels"] = int(
        (
            (data[:, :, 0].astype(int) != sea.red())
            | (data[:, :, 1].astype(int) != sea.green())
            | (data[:, :, 2].astype(int) != sea.blue())
        ).sum()
    )
    colours, counts = np.unique(data.reshape(-1, 4), axis=0, return_counts=True)
    order = np.argsort(-counts)[:8]
    report["base_image_colour_histogram"] = [
        {"rgba": [int(v) for v in colours[index]], "pixels": int(counts[index])}
        for index in order
    ]

    # The base chart is drawn asynchronously on a worker.  Give it time to
    # deliver, then rebuild the image and measure again.
    from PySide6.QtCore import QEventLoop, QTimer

    def pump(milliseconds):
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    report["async_before"] = {
        "non_sea_pixels": report["base_image_non_sea_pixels"],
        "distinct_colours": int(len(colours)),
    }
    for _ in range(6):
        pump(500)
    renderer = getattr(canvas, "_base_chart_renderer", None)
    state = renderer._states.get(canvas) if renderer is not None else None
    report["async_worker_state"] = {
        "has_state": state is not None,
        "has_frame": bool(state is not None and state.frame is not None),
        "running": bool(getattr(renderer, "_running", False)),
        "failed_key": bool(getattr(renderer, "_failed_key", None)),
        "diagnostics": dict(getattr(renderer, "diagnostics", {}) or {}),
    }
    canvas._invalidate_base()
    again = canvas._base_image()
    if again is not None:
        raw = (
            np.frombuffer(again.constBits(), np.uint8)
            .reshape(again.height(), again.bytesPerLine())[:, : again.width() * 4]
            .reshape(again.height(), again.width(), 4)
        )
        report["async_after"] = {
            "non_sea_pixels": int(
                (
                    (raw[:, :, 0].astype(int) != sea.red())
                    | (raw[:, :, 1].astype(int) != sea.green())
                    | (raw[:, :, 2].astype(int) != sea.blue())
                ).sum()
            ),
            "distinct_colours": int(len(np.unique(raw.reshape(-1, 4), axis=0))),
        }

    colours, counts = np.unique(data.reshape(-1, 4), axis=0, return_counts=True)
    order = np.argsort(-counts)[:8]
    report["base_image_colour_histogram"] = [
        {"rgba": [int(v) for v in colours[index]], "pixels": int(counts[index])}
        for index in order
    ]

    # Does the coastline layer draw any geometry at all, on a clear surface?
    import cairo

    if gshhs is not None:
        scratch = cairo.ImageSurface(cairo.FORMAT_ARGB32, 710, 743)
        context = cairo.Context(scratch)
        began = time.perf_counter()
        try:
            gshhs.do_draw(canvas, context)
            report["gshhs_direct_draw_error"] = ""
        except Exception as exc:
            report["gshhs_direct_draw_error"] = f"{type(exc).__name__}: {exc}"
        report["gshhs_direct_draw_wall_s"] = round(time.perf_counter() - began, 3)
        scratch.flush()
        raw = (
            np.frombuffer(bytes(scratch.get_data()), np.uint8)
            .reshape(743, scratch.get_stride())[:, : 710 * 4]
            .reshape(743, 710, 4)
        )
        report["gshhs_direct_opaque_pixels"] = int((raw[:, :, 3] > 0).sum())
        # Draw with the same object the worker uses: a MapViewport over the
        # canvas, not the canvas itself.
        from gweatherrouting.core.mapviewport import MapViewport
        from gweatherrouting.rendering.overlayrenderer import MARGIN

        viewport = MapViewport(canvas, MARGIN, lambda: None)
        report["viewport_scale"] = viewport.get_scale()
        report["canvas_scale"] = canvas.get_scale()
        try:
            geometry = gshhs.get_bounding_geometry(viewport)
            report["viewport_bounding_area"] = float(geometry.GetArea())
            report["viewport_bounding_empty"] = bool(geometry.IsEmpty())
        except Exception as exc:
            report["viewport_bounding_error"] = f"{type(exc).__name__}: {exc}"
        scratch2 = cairo.ImageSurface(cairo.FORMAT_ARGB32, 710, 743)
        context2 = cairo.Context(scratch2)
        began = time.perf_counter()
        try:
            gshhs.do_draw(viewport, context2)
            report["gshhs_viewport_draw_error"] = ""
        except Exception as exc:
            report["gshhs_viewport_draw_error"] = f"{type(exc).__name__}: {exc}"
        report["gshhs_viewport_draw_wall_s"] = round(time.perf_counter() - began, 3)
        scratch2.flush()
        raw2 = (
            np.frombuffer(bytes(scratch2.get_data()), np.uint8)
            .reshape(743, scratch2.get_stride())[:, : 710 * 4]
            .reshape(743, 710, 4)
        )
        report["gshhs_viewport_opaque_pixels"] = int((raw2[:, :, 3] > 0).sum())
        if state is not None and state.frame is not None:
            job, surface = state.frame
            report["worker_frame"] = {
                "surface_size": [surface.get_width(), surface.get_height()],
                "viewport_scale": job.viewport.get_scale(),
                "viewport_size": [job.viewport.width, job.viewport.height],
                "canvas_size": [
                    canvas.get_allocated_width(),
                    canvas.get_allocated_height(),
                ],
                "factor": job.factor,
            }
            # Paint the finished frame by hand into a base-image-like surface.
            from gweatherrouting.rendering.overlayrenderer import paint_viewport
            from gweatherrouting.rendering.style import Style

            raw_frame = (
                np.frombuffer(bytes(surface.get_data()), np.uint8)
                .reshape(surface.get_height(), surface.get_stride())[
                    :, : surface.get_width() * 4
                ]
                .reshape(surface.get_height(), surface.get_width(), 4)
            )
            frame_colours, frame_counts = np.unique(
                raw_frame.reshape(-1, 4), axis=0, return_counts=True
            )
            frame_order = np.argsort(-frame_counts)[:6]
            report["worker_frame_colour_histogram"] = [
                {
                    "rgba": [int(v) for v in frame_colours[index]],
                    "pixels": int(frame_counts[index]),
                }
                for index in frame_order
            ]
            report["worker_frame_opaque_pixels"] = int((raw_frame[:, :, 3] > 0).sum())
            report["worker_frame_device_scale"] = [
                surface.get_device_scale()[0],
                surface.get_device_scale()[1],
            ]
            ys, xs = np.nonzero(raw_frame[:, :, 3] > 0)
            if len(xs):
                report["worker_frame_content_bbox_device_px"] = [
                    int(xs.min()),
                    int(ys.min()),
                    int(xs.max()),
                    int(ys.max()),
                ]
            report["expected_canvas_subrect_device_px"] = [
                256 * job.factor,
                256 * job.factor,
                (256 + 710) * job.factor,
                (256 + 743) * job.factor,
            ]
            report["canvas_center"] = [canvas._center_lon, canvas._center_lat]
            report["viewport_center"] = [
                job.viewport._center_lon,
                job.viewport._center_lat,
            ]
            report["canvas_zoom"] = canvas._zoom
            report["viewport_zoom"] = job.viewport._zoom
            factor = canvas.get_scale_factor()
            scratch3 = cairo.ImageSurface(
                cairo.FORMAT_ARGB32,
                canvas.get_allocated_width() * factor,
                canvas.get_allocated_height() * factor,
            )
            scratch3.set_device_scale(factor, factor)
            context3 = cairo.Context(scratch3)
            Style.chart_palettes[controller.settings.chartPalette].sea.apply(context3)
            context3.paint()
            try:
                paint_viewport(canvas, context3, job.viewport, surface)
                report["manual_paint_error"] = ""
            except Exception as exc:
                report["manual_paint_error"] = f"{type(exc).__name__}: {exc}"
            scratch3.flush()
            raw3 = (
                np.frombuffer(bytes(scratch3.get_data()), np.uint8)
                .reshape(scratch3.get_height(), scratch3.get_stride())[
                    :, : scratch3.get_width() * 4
                ]
                .reshape(scratch3.get_height(), scratch3.get_width(), 4)
            )
            report["manual_paint_non_sea_pixels"] = int(
                (
                    (raw3[:, :, 0].astype(int) != sea.red())
                    | (raw3[:, :, 1].astype(int) != sea.green())
                    | (raw3[:, :, 2].astype(int) != sea.blue())
                ).sum()
            )
            # Does the finished frame still match the key the canvas asks for?
            from gweatherrouting.qt.basecharts import describe_sources

            current = describe_sources([gshhs])
            current_key = (
                controller.settings.chartPalette,
                factor,
                tuple((source,) for source in current),
            )
            report["frame_key_matches"] = bool(job.key == current_key)
            report["frame_key"] = repr(job.key)[:400]
            report["current_key"] = repr(current_key)[:400]
        report["gshhs_drawer_palette"] = getattr(gshhs.drawer, "palette", None)
        report["gshhs_palette_attribute"] = getattr(gshhs, "palette", None)
        from gweatherrouting.rendering.chartclasses import chart_class_enabled

        report["gshhs_class_switch_enabled"] = bool(
            chart_class_enabled(controller.settings, gshhs)
        )
        report["gshhs_drawer_type"] = type(gshhs.drawer).__name__
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(output.with_suffix(".base.png")))
    canvas.close()
    controller.close()
    app.processEvents()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = {}
    try:
        application(report, args.output)
    except Exception as exc:
        import traceback

        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    report["limits"] = (
        "Read-only render check; no settings written, no user data changed"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
