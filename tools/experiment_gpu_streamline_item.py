"""Exercise the production GPU streamline item in a native Qt Quick Metal pass."""

from datetime import datetime
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory


def main():
    import numpy as np
    from PySide6.QtCore import QUrl, QTimer
    from PySide6.QtQuickWidgets import QQuickWidget
    from PySide6.QtWidgets import QApplication

    from gweatherrouting.qt.gpustreamlines import (
        GpuStreamlineItem,
        make_gpu_trace_input,
    )
    from gweatherrouting.qt.gribviewport import ViewportCamera
    from gweatherrouting.rendering.streamlinepaths import _world_geo

    width, height, grid_columns, grid_rows = 800, 600, 216, 166
    camera = ViewportCamera(
        center_lat=0,
        center_lon=0,
        zoom=3,
        width=width,
        height=height,
        time=datetime(2026, 1, 1),
        density="all",
        over_land=True,
    )
    view = camera.view()
    center_x = view._lon_to_x(view._center_lon)
    center_y = view._lat_to_y(view._center_lat)
    screen_xs = np.linspace(0.0, width, grid_columns)
    screen_ys = np.linspace(0.0, height, grid_rows)
    longitudes = [
        _world_geo(view, center_x + x - width / 2, center_y - height / 2)[1]
        for x in screen_xs
    ]
    latitudes = [
        _world_geo(view, center_x - width / 2, center_y + y - height / 2)[0]
        for y in reversed(screen_ys)
    ]
    columns = []
    for ix, lon in enumerate(longitudes):
        u = np.asarray(
            [2.5 + 0.008 * screen_xs[ix] + 0.004 * y for y in reversed(screen_ys)]
        )
        v = np.asarray(
            [0.8 + 0.003 * screen_xs[ix] - 0.002 * y for y in reversed(screen_ys)]
        )
        columns.append(
            (
                lon,
                latitudes,
                np.degrees(np.arctan2(-u, v)) % 360,
                np.hypot(u, v),
            )
        )
    trace_input = make_gpu_trace_input(view, columns, camera.density)
    seed_count = len(trace_input.seeds)

    errors = []
    app = QApplication.instance() or QApplication(sys.argv[:1])
    with TemporaryDirectory(prefix="gwr-gpu-item-") as temporary:
        qml = Path(temporary) / "root.qml"
        qml.write_text(
            'import QtQuick\nRectangle { width: 256; height: 160; color: "white" }'
        )
        widget = QQuickWidget()
        widget.setSource(QUrl.fromLocalFile(str(qml)))
        widget.resize(width, height)
        item = GpuStreamlineItem(widget.rootObject())
        item.setParentItem(widget.rootObject())
        item.attach_window(widget.quickWindow())
        item.gpuError.connect(errors.append)
        item.set_trace_input(trace_input)
        widget.show()
        started = time.perf_counter()
        timer = QTimer()

        def poll():
            if errors or item._last_dispatched == item._input_generation:
                app.quit()
            elif time.perf_counter() - started > 15:
                errors.append("GPU streamline item timed out")
                app.quit()

        timer.timeout.connect(poll)
        timer.start(10)
        app.exec()
        timer.stop()
        image = widget.grabFramebuffer()
        widget.close()

    if errors:
        raise RuntimeError("; ".join(str(error) for error in errors))
    image = image.convertToFormat(image.Format.Format_RGBA8888)
    pixels = np.frombuffer(image.bits(), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine()
    )[:, : image.width() * 4]
    colors = pixels.reshape(image.height(), image.width(), 4)
    changed = np.any(colors[:, :, :3] < 245, axis=2)
    pixel_count = int(np.count_nonzero(changed))
    if pixel_count < 10:
        raise AssertionError("GPU streamline item produced no visible lines")
    print(f"gpu-item=Metal seeds={seed_count} visible-pixels={pixel_count}")


if __name__ == "__main__":
    main()
