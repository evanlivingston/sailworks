"""Opt-in QQuickWidget map host; existing TrackMap owns all input and map state."""

import logging
import math

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QPainter, QSurfaceFormat
from PySide6.QtWidgets import QWidget

from .mapframe import resource_estimate

logger = logging.getLogger("gweatherrouting")


class DynamicOverlay(QWidget):
    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, _):  # noqa: N802
        if self.canvas.disposed:
            return
        painter = QPainter(self)
        painter.setClipRect(self.rect())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.canvas._paint_route_overlay(painter)
        self.canvas._paint_track_overlay(painter)
        self.canvas._paint_dashboard(painter)
        self.canvas._paint_caption(painter)
        painter.end()


class GpuPresenter(QObject):
    # Trial explicit-resource limits for the experimental option, not measured
    # total VRAM or a promise about Qt/driver allocations outside this ledger.
    GPU_LIMIT = 512 * 1024 * 1024
    CPU_SCENE_LIMIT = 128 * 1024 * 1024
    UPLOAD_LIMIT = 128 * 1024 * 1024

    def __init__(self, canvas):
        super().__init__(canvas)
        from PySide6.QtQuickWidgets import QQuickWidget
        from .gpuscene import MapScene

        if QGuiApplication.platformName() in ("offscreen", "minimal"):
            raise RuntimeError("Hardware map rendering is unavailable on this platform")
        self.canvas = canvas
        self.active, self.closed, self.initialized = True, False, False
        self.estimate = {}
        self._quick_window = None
        self.scene = None
        self.widget = QQuickWidget(canvas)
        self.widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        format_ = QSurfaceFormat()
        format_.setSamples(4)
        format_.setAlphaBufferSize(8)
        self.widget.setFormat(format_)
        self.widget.hide()
        self.overlay = DynamicOverlay(canvas)
        self.overlay.hide()
        self.widget.installEventFilter(self)
        self.overlay.installEventFilter(self)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._present)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.setInterval(3000)
        self.deadline.timeout.connect(lambda: self.fail("GPU initialization timed out"))
        self.widget.sceneGraphError.connect(lambda _error, reason: self.fail(reason))
        self.widget.statusChanged.connect(self._status_changed)
        try:
            self.scene = MapScene(self.widget, self.fail)
            self.scene.root.windowChanged.connect(lambda _window: self.request())
        except Exception:
            self.closed = True
            self.active = False
            self.timer.stop()
            self.deadline.stop()
            self.widget.deleteLater()
            self.overlay.deleteLater()
            raise

    def request(self):
        if self.active and not self.closed and not self.timer.isActive():
            self.timer.start(0)

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in (
            QEvent.Type.Show,
            QEvent.Type.ParentChange,
            QEvent.Type.WinIdChange,
        ):
            self.request()
        if (
            self.active
            and not self.closed
            and event.type()
            in (
                QEvent.Type.NativeGesture,
                QEvent.Type.Wheel,
                QEvent.Type.ContextMenu,
                QEvent.Type.KeyPress,
                QEvent.Type.KeyRelease,
            )
        ):
            # Both presentation children have the canvas's origin and extent.
            # Keep all gestures/picking in the existing TrackMap implementation.
            QCoreApplication.sendEvent(self.canvas, event)
            return True
        return super().eventFilter(watched, event)

    def _bind_window(self):
        import shiboken6

        window = self.widget.quickWindow()
        if self._quick_window is window:
            return
        if self._quick_window is not None and shiboken6.isValid(self._quick_window):
            self._quick_window.afterRendering.disconnect(self._rendered)
            self._quick_window.sceneGraphInvalidated.disconnect(self._invalidated)
        self._quick_window = window
        self.initialized = False
        window.afterRendering.connect(self._rendered)
        window.sceneGraphInvalidated.connect(self._invalidated)

    def _invalidated(self):
        self.initialized = False
        self.request()

    def _status_changed(self, status):
        from PySide6.QtQuickWidgets import QQuickWidget

        if status == QQuickWidget.Status.Error:
            self.fail("; ".join(error.toString() for error in self.widget.errors()))

    def _rendered(self):
        if not self.active or self.closed:
            return
        from PySide6.QtQuick import QSGRendererInterface

        api = self.widget.quickWindow().rendererInterface().graphicsApi()
        if api in (
            QSGRendererInterface.GraphicsApi.Software,
            QSGRendererInterface.GraphicsApi.Null,
            QSGRendererInterface.GraphicsApi.Unknown,
        ):
            self.fail("No hardware scene graph")
            return
        self.initialized = True
        self.deadline.stop()
        self.canvas.controller.set_renderer_status(self.canvas, "gpu", api=api.name)

    def _present(self):
        if (
            not self.active
            or self.closed
            or self.canvas.disposed
            or not self.canvas.isVisible()
        ):
            return
        try:
            self._bind_window()
            frame = self.canvas._gpu_frame()
            window = self.canvas.window()
            ratio = window.devicePixelRatioF()
            pixels = math.ceil(window.width() * ratio) * math.ceil(
                window.height() * ratio
            )
            self.estimate = resource_estimate(frame, self.scene.layers, pixels)
            if (
                self.estimate["gpu_estimate_bytes"] > self.GPU_LIMIT
                or self.estimate["cpu_scene_bytes"] > self.CPU_SCENE_LIMIT
                or self.estimate["upload_bytes"] > self.UPLOAD_LIMIT
            ):
                raise RuntimeError("Map exceeds experimental GPU resource limits")
            self.scene.present(frame)
            self.widget.setGeometry(self.canvas.rect())
            self.overlay.setGeometry(self.canvas.rect())
            self.widget.show()
            self.widget.lower()
            self.overlay.show()
            self.overlay.raise_()
            self.canvas.coverage_notice.raise_()
            for button in (
                self.canvas.coverage_fetch,
                *self.canvas.coverage_fetch_buttons.values(),
            ):
                button.raise_()
            self.widget.update()
            self.overlay.update()
            if not self.initialized and not self.deadline.isActive():
                self.deadline.start()
        except Exception as exc:
            self.fail(str(exc))

    def suspend(self):
        if not self.active or self.closed:
            return
        self.timer.stop()
        self.deadline.stop()
        self.widget.hide()
        self.overlay.hide()
        self.scene.clear()
        self.widget.quickWindow().releaseResources()
        self.canvas.controller.set_renderer_status(self.canvas, "pending")

    def fail(self, reason):
        if not self.active or self.closed:
            return
        self.active = False
        self.timer.stop()
        self.deadline.stop()
        self._failure_reason = reason
        logger.warning("GPU map unavailable; using CPU: %s", reason)
        # Never destroy scene nodes inside an updatePaintNode/render callback.
        QTimer.singleShot(0, self._fallback)

    def _fallback(self):
        if self.closed:
            return
        self.canvas.controller.set_renderer_status(
            self.canvas, "cpu", reason=self._failure_reason
        )
        self.widget.hide()
        self.overlay.hide()
        self.scene.clear()
        self.widget.deleteLater()
        self.overlay.deleteLater()
        self.widget = None
        self.overlay = None
        self.scene = None
        self.canvas._gpu_geometry = None
        self.canvas._gpu_batches = ()
        self.canvas._gpu_wind_raster = None
        self.canvas._gpu_raster_key = None
        self.canvas._invalidate_base()
        self.canvas.update()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.active = False
        self.timer.stop()
        self.deadline.stop()
        if self.widget is not None:
            self.widget.hide()
            self.overlay.hide()
            self.scene.clear()
            self.widget.deleteLater()
            self.overlay.deleteLater()
