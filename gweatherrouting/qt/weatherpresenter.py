"""ENC/weather Quick child; TrackMap retains source policy and all input."""

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPainter
from PySide6.QtWidgets import QLabel, QWidget

from .gribviewport import GribViewportModel, ViewportCamera, _viewport_widget_class


class WeatherForeground(QWidget):
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
        self.canvas._paint_foreground(painter)
        painter.end()


class WeatherPresenter(QObject):
    failed = Signal(str)
    rendered = Signal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.widget = None
        self.foreground = None
        self.closed = False
        self.reason = ""
        self._source = None
        self._state = "unavailable"
        self._rendered = False
        self.status = QLabel(canvas)
        self.status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.status.setStyleSheet(
            "background: rgba(0,0,0,190); color: white; padding: 4px"
        )
        self.status.setWordWrap(True)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.sync)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.setInterval(3000)
        self.deadline.timeout.connect(
            lambda: self.fail("weather GPU initialization timed out")
        )
        self.failed.connect(self.fail)
        self.rendered.connect(self._frame_rendered)
        canvas.destroyed.connect(self.close)

    @property
    def active(self):
        settings = self.canvas.controller.settings
        return (
            not self.closed
            and not self.reason
            and settings.get("weatherRenderer", "viewport") == "viewport"
            and settings.gribWindStyle == "barbs"
            and getattr(settings, "gribBarbDensity", "standard") != "all"
        )

    @property
    def enc_active(self):
        return self.active and self.widget is not None and self.widget._enc_chart_layer

    def request(self):
        if not self.closed and not self.timer.isActive():
            self.timer.start(0)

    def _create(self, source):
        if QGuiApplication.platformName() in ("offscreen", "minimal"):
            raise RuntimeError("hardware weather unavailable on this Qt platform")
        widget = None
        model = self._model(source)
        try:
            from gweatherrouting.core.storage import DATA_DIR

            widget = _viewport_widget_class()(
                model,
                self.canvas,
                chart_layer=True,
                enc_chart_layer=True,
                enc_only=True,
                chart_data_dir=DATA_DIR,
                chart_palette=self.canvas.controller.settings.chartPalette,
            )
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            widget.installEventFilter(self)
            widget._status.hide()
            widget.stateChanged.connect(self._state_changed)
            widget.sceneGraphError.connect(
                lambda _code, reason: self.failed.emit(reason)
            )
            widget.statusChanged.connect(self._qml_status)
            widget.quickWindow().afterRendering.connect(self.rendered.emit)
            self.widget = widget
            self.foreground = WeatherForeground(self.canvas)
            self.foreground.installEventFilter(self)
            # Parent teardown closes the current (possibly replaced) model.
            self.canvas.destroyed.connect(widget.dispose)
            self.canvas._invalidate_base()
        except Exception:
            model.close()
            if widget is not None:
                widget.deleteLater()
            raise

    @staticmethod
    def _model(source):
        return GribViewportModel(
            () if source is None else (source.path,),
            display_identity=None if source is None else source.display_identity,
            resident_time_window=True,
        )

    def _qml_status(self, status):
        from PySide6.QtQuickWidgets import QQuickWidget

        if status == QQuickWidget.Status.Error and self.widget is not None:
            self.fail("; ".join(error.toString() for error in self.widget.errors()))

    def _frame_rendered(self):
        if self.closed or self.widget is None:
            return
        from PySide6.QtQuick import QSGRendererInterface

        api = self.widget.quickWindow().rendererInterface().graphicsApi()
        if api in (
            QSGRendererInterface.GraphicsApi.Software,
            QSGRendererInterface.GraphicsApi.Null,
        ):
            self.fail("weather shader requires a hardware Qt Quick backend")
            return
        self._rendered = True
        self.deadline.stop()
        self.canvas.controller.set_renderer_status(
            self.canvas, "gpu", api="weather " + api.name
        )

    def _state_changed(self, state):
        self._state = state
        if self.widget is not None:
            self.widget._status.hide()
        self._label()

    def _label(self):
        if self.closed:
            return
        selection = self.canvas.controller.display_forecast
        if self.reason:
            mode = "Legacy weather fallback: " + self.reason
        elif self.canvas.controller.settings.gribWindStyle == "streamlines":
            mode = "Streamlines — legacy renderer"
        elif (
            self.canvas.controller.settings.get("weatherRenderer", "viewport")
            == "legacy"
        ):
            mode = "Legacy weather recovery"
        elif not self.canvas.wind_visible:
            mode = "Wind hidden"
        elif (
            getattr(self.canvas.controller.settings, "gribBarbDensity", "standard")
            == "all"
        ):
            mode = "All forecast points — legacy chart/weather renderer (unthinned)"
        else:
            mode = "Viewport wind: " + self._state
        self.status.setText(selection.status + "\n" + mode)
        self.status.setToolTip(selection.error)
        self.status.setFixedWidth(max(80, min(530, self.canvas.width() - 16)))
        self.status.adjustSize()
        self.status.move(8, max(8, self.canvas.height() - self.status.height() - 8))
        self.status.show()
        self.status.raise_()

    def _drop_widget(self):
        """Chart-enabled children own their source; replace them as a whole."""
        widget, self.widget = self.widget, None
        foreground, self.foreground = self.foreground, None
        self.deadline.stop()
        self._rendered = False
        self._source = None
        if widget is not None:
            widget.hide()
            widget.dispose()
            widget.deleteLater()
        if foreground is not None:
            foreground.hide()
            foreground.deleteLater()
        self.canvas._invalidate_base()

    def _sync_active(self, source):
        if self.widget is not None and (
            self.widget._chart_palette != self.canvas.controller.settings.chartPalette
        ):
            self._drop_widget()
        if self.widget is None:
            self._create(source)
            self._source = source
        elif source is not self._source:
            self.widget.replace_model(self._model(source))
            self._source = source
        widget, foreground = self.widget, self.foreground
        if widget is None or foreground is None:
            raise RuntimeError("ENC/weather viewport initialization failed")
        widget.setGeometry(self.canvas.rect())
        foreground.setGeometry(self.canvas.rect())
        widget.set_wind_opacity(
            self.canvas.controller.settings.gribArrowOpacity
            if self.canvas.wind_visible
            else 0
        )
        widget.show()
        widget.set_camera(
            ViewportCamera(
                self.canvas._center_lat,
                self.canvas._center_lon,
                self.canvas._zoom,
                self.canvas.width(),
                self.canvas.height(),
                self.canvas.time_control.time,
                self.canvas.controller.settings.gribBarbDensity,
                self.canvas.controller.settings.gribArrowOnGround,
            )
        )
        cache, target = self.canvas._resolve_reusable_layer(
            self.canvas._environment_cache,
            self.canvas._base_key(),
            self.canvas._environment_image,
        )
        self.canvas._environment_cache = cache
        widget.set_environment(cache[4], target)
        if not self._rendered and not self.deadline.isActive():
            self.deadline.start()
        foreground.show()
        foreground.raise_()
        foreground.update()

    def sync(self):
        if self.closed or self.canvas.disposed or not self.canvas.isVisible():
            return
        sources = self.canvas.controller.display_forecast.sources()
        source = sources[0] if sources else None
        if self.active:
            try:
                self._sync_active(source)
            except (ImportError, RuntimeError, OSError, ValueError) as exc:
                self.fail(str(exc))
        elif self.widget is not None:
            self._drop_widget()
        if (
            getattr(self.canvas.controller.settings, "gribBarbDensity", "standard")
            == "all"
        ):
            self.canvas.controller.set_renderer_status(
                self.canvas,
                "cpu",
                reason="All forecast points: unthinned legacy renderer",
            )
        self._label()
        # Existing interactive widgets must remain above both presentation children.
        self.canvas.coverage_notice.raise_()
        self.canvas.coverage_fetch.raise_()
        for button in self.canvas.coverage_fetch_buttons.values():
            button.raise_()

    def source_changed(self):
        # Hide old weather synchronously, before the coalesced GUI refresh.
        if self.widget is not None:
            self.widget.replace_model(self._model(None))
            self._source = None
            self.canvas._invalidate_base()
        self.request()

    def suspend(self):
        self.timer.stop()
        self.deadline.stop()
        if self.widget is not None:
            self._drop_widget()

    def fail(self, reason):
        if self.closed or self.reason:
            return
        self.reason = reason
        self.suspend()
        self.canvas.controller.set_renderer_status(self.canvas, "cpu", reason=reason)
        self.canvas.queue_draw()
        self._label()

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in (
            QEvent.Type.NativeGesture,
            QEvent.Type.Wheel,
            QEvent.Type.ContextMenu,
            QEvent.Type.KeyPress,
            QEvent.Type.KeyRelease,
        ):
            QCoreApplication.sendEvent(self.canvas, event)
            return True
        return super().eventFilter(watched, event)

    def close(self, *_):
        if self.closed:
            return
        self.closed = True
        self.timer.stop()
        self.deadline.stop()
        if self.widget is not None:
            self.widget.dispose()
