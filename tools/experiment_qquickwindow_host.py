"""Compare QQuickWidget and QQuickWindow render-loop overhead on one scene.

This is an architectural probe, not presentation latency: it measures the
time from ``update()`` to the Qt Quick ``afterRendering`` signal.
"""

import argparse
import json
import math
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "sailworks/qt/shaders/ViewportChartScene.qml"


def summary(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}

    def percentile(fraction):
        index = (len(ordered) - 1) * fraction
        lower = math.floor(index)
        upper = math.ceil(index)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)

    return {
        "count": len(ordered),
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": ordered[-1],
    }


def run(mode, frames, interval_ms, warmup, grab=False):
    from PySide6.QtCore import QEventLoop, QTimer, QUrl, Qt
    from PySide6.QtQuick import QQuickView
    from PySide6.QtQuickWidgets import QQuickWidget
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

    app = QApplication([])
    source = QUrl.fromLocalFile(str(SCENE))
    container = None
    if mode == "qquickwidget":
        container = QWidget()
        widget = QQuickWidget()
        widget.setResizeMode(QQuickWidget.SizeRootObjectToView)
        widget.setSource(source)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget)
        container.resize(1000, 700)
        container.show()
        widget.show()
        quick = widget.quickWindow()
        update = widget.update
    else:
        view = QQuickView()
        view.setResizeMode(QQuickView.SizeRootObjectToView)
        view.setSource(source)
        view.resize(1000, 700)
        view.show()
        quick = view
        update = view.update

    def pump(milliseconds=0):
        app.processEvents()
        if milliseconds > 0:
            loop = QEventLoop()
            QTimer.singleShot(milliseconds, loop.quit)
            loop.exec()

    for _ in range(30):
        pump(20)
    state = {"revision": -1, "synchronizing": None}
    dispatched, rendered = {}, {}
    quick.beforeSynchronizing.connect(
        lambda: state.__setitem__("synchronizing", state["revision"]),
        Qt.ConnectionType.DirectConnection,
    )

    def rendered_callback():
        revision = state["synchronizing"]
        if revision is not None and revision not in rendered:
            rendered[revision] = time.perf_counter()

    quick.afterRendering.connect(rendered_callback, Qt.ConnectionType.DirectConnection)
    for _ in range(warmup):
        update()
        pump(20)
    interval = interval_ms / 1000.0
    if grab:
        samples = []
        for _ in range(frames):
            started = time.perf_counter()
            image = (
                widget.grabFramebuffer()
                if mode == "qquickwidget"
                else view.grabWindow()
            )
            samples.append((time.perf_counter() - started) * 1000)
            if image.isNull():
                raise RuntimeError("framebuffer readback failed")
            pump(int(interval * 1000))
        result = {
            "mode": mode,
            "endpoint": "grabFramebuffer" if mode == "qquickwidget" else "grabWindow",
            "frames": frames,
            "observed": len(samples),
            "summary": summary(samples),
            "environment": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": platform.version(),
                "qt_platform": app.platformName(),
                "dpr": quick.devicePixelRatio(),
                "width": quick.width(),
                "height": quick.height(),
            },
        }
        if container is not None:
            container.close()
        else:
            view.close()
        return result
    for index in range(frames):
        state["revision"] = index
        dispatched[index] = time.perf_counter()
        update()
        pump(int(interval * 1000))
    pump(300)
    latency = [
        (rendered[index] - dispatched[index]) * 1000
        for index in dispatched
        if index in rendered
    ]
    result = {
        "mode": mode,
        "scene": str(SCENE.relative_to(ROOT)),
        "frames": frames,
        "observed": len(latency),
        "summary": summary(latency),
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.version(),
            "qt_platform": app.platformName(),
            "dpr": quick.devicePixelRatio(),
            "width": quick.width(),
            "height": quick.height(),
        },
    }
    if container is not None:
        container.close()
    else:
        view.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("qquickwidget", "qquickwindow"), required=True
    )
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--interval-ms", type=float, default=16.667)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--grab", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.mode, args.frames, args.interval_ms, args.warmup, args.grab)
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
