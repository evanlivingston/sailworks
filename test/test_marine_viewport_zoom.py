"""Opt-in native Quick readback of a real production wheel interaction.

This is synthetic interaction evidence, not full-application visual acceptance
or a presentation-FPS measurement.
"""

import os
from threading import Lock
import time

import numpy as np
import pytest

from test.test_gribviewport import _camera, _paths, _wait_ready
from test.test_weather_staging import gribs  # noqa: F401


@pytest.mark.skipif(
    os.environ.get("GWR_MARINE_QUICK_SNAPSHOT") != "1",
    reason="opt-in native Qt Quick framebuffer readback",
)
def test_wheel_reprojects_marine_and_renders_before_settling(gribs, monkeypatch):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    from gweatherrouting.qt import gribviewport, marinequick
    from gweatherrouting.rendering.marinesnapshot import (
        CurrentArrow,
        MarineSnapshot,
        current_triangles,
    )

    app = QApplication.instance() or QApplication([])
    paths, _ = _paths(gribs)
    model = gribviewport.GribViewportModel(paths)
    camera = _camera()
    _wait_ready(model, camera=camera)
    # Inject at the marine worker boundary, not at the item/camera boundary.
    # Both geographic grid arrows and the station ring must survive the zoom.
    from types import SimpleNamespace

    sources = SimpleNamespace(fields=(), currents=SimpleNamespace(datasets=()))
    snapshot = MarineSnapshot(
        sources,
        camera.time,
        (
            CurrentArrow(0.5, 0.40, 1, 0),
            CurrentArrow(0.55, 0.45, 0, 1),
            CurrentArrow(0.5, 0.60, 0, 1, True),
        ),
        "available",
    )
    monkeypatch.setattr(marinequick, "sample_currents", lambda *args: snapshot)
    lock = Lock()
    publications, nodes, frames = [], [], []
    present = marinequick.MarineCurrentItem.present
    paint_node = marinequick.MarineCurrentItem.updatePaintNode

    def observed_present(item, sampled, current):
        present(item, sampled, current)
        with lock:
            publications.append((current.zoom, item.vertices.tobytes()))

    def observed_node(item, old, data):
        node = paint_node(item, old, data)
        with lock:
            if node is not None and publications:
                nodes.append(publications[-1])
        return node

    monkeypatch.setattr(marinequick.MarineCurrentItem, "present", observed_present)
    monkeypatch.setattr(marinequick.MarineCurrentItem, "updatePaintNode", observed_node)
    widget = gribviewport._viewport_widget_class()(
        model,
        gpu_weather_fill=True,
        chart_without_weather=True,
        native_weather_fields=False,
    )
    # Use the production layered scene, but isolate marine pixels so changing
    # wind/fill cannot satisfy the animated-frame assertion accidentally.
    widget._barbs.setOpacity(0)
    widget._fill.setVisible(False)
    widget.resize(camera.width, camera.height)

    def rendered():
        # This may run on the scene-graph thread: no QWidget/camera access.
        with lock:
            if nodes:
                frames.append(nodes[-1])

    window = widget.quickWindow()
    window.afterRendering.connect(rendered, Qt.ConnectionType.DirectConnection)

    def frame_records():
        with lock:
            return tuple(frames)

    def wait_for(predicate, message, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            time.sleep(0.002)
        with lock:
            counts = (len(publications), len(nodes), len(frames))
        pytest.fail(
            f"{message}; publications/nodes/frames={counts}; "
            f"marine={widget._marine_layer.state}"
        )

    try:
        widget.set_camera(camera)
        widget.set_marine(sources, camera.time, True)
        widget.show()
        wait_for(
            lambda: bool(frame_records()) and widget._marine_layer.snapshot is not None,
            "Initial marine geometry never reached a Quick frame",
        )
        baseline = widget.grabFramebuffer()
        assert not baseline.isNull(), "Initial Quick framebuffer readback stalled"
        initial_vertices = widget._marine_layer.item.vertices.tobytes()
        assert initial_vertices, "Initial grid/station geometry is empty"
        assert np.any(
            np.all(
                widget._marine_layer.item.vertices["color"] == (255, 178, 46, 255),
                axis=1,
            )
        ), "Initial station-ring geometry is missing"
        with lock:
            publications.clear()
            nodes.clear()
            frames.clear()

        position = QPointF(camera.width / 2, camera.height / 2)
        event = QWheelEvent(
            position,
            QPointF(widget.mapToGlobal(position.toPoint())),
            QPoint(),
            QPoint(0, 480),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(widget, event)
        assert event.isAccepted(), "Production viewport rejected the wheel gesture"
        target = widget._wheel_zoom_target
        assert target is not None and target > camera.zoom, "Wheel zoom never started"

        def intermediate(records):
            with lock:
                return any(
                    camera.zoom < zoom < target and data != initial_vertices
                    for zoom, data in records
                )

        wait_for(
            lambda: widget._camera.zoom > camera.zoom,
            "Camera stalled during wheel animation",
        )
        wait_for(
            lambda: intermediate(publications),
            "Camera moved but marine projected geometry stalled",
        )
        wait_for(
            lambda: intermediate(nodes),
            "Marine geometry changed but updatePaintNode stalled",
        )
        wait_for(
            lambda: intermediate(frames),
            "Marine node changed but Quick rendering stalled",
        )
        assert (
            widget._wheel_zoom_tick.isActive()
        ), "No marine frame observed until wheel animation had already settled"
        moving = widget.grabFramebuffer()
        assert not moving.isNull(), "Animated Quick framebuffer readback stalled"
        assert moving != baseline, "Quick framebuffer did not change during zoom"
        assert (
            widget._wheel_zoom_tick.isActive()
        ), "Framebuffer readback completed only after wheel animation settled"
        # Let the real timer finish; never call finish_motion to create the result.
        wait_for(
            lambda: widget._wheel_zoom_target is None, "Wheel animation never settled"
        )
        assert widget._camera.zoom == pytest.approx(target)
        rows = np.asarray(current_triangles(snapshot, widget._camera))
        np.testing.assert_allclose(
            widget._marine_layer.item.vertices["position"],
            rows[:, :2],
            rtol=0,
            atol=3e-5,
            err_msg="Final marine geometry is stale relative to the final camera",
        )
        final_vertices = widget._marine_layer.item.vertices.tobytes()
        wait_for(
            lambda: any(data == final_vertices for _, data in frame_records()),
            "Final marine geometry never reached a rendered Quick frame",
        )
        assert not widget._marine_layer.error, widget._marine_layer.error
    finally:
        window.afterRendering.disconnect(rendered)
        widget.dispose()
        widget.close()
        model.close()
