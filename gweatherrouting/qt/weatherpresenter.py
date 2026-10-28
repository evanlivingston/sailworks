"""Qt Quick weather/chart child hosted by the desktop viewport map."""

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPainter
from PySide6.QtWidgets import QLabel, QWidget

from gweatherrouting.core.forecastcapabilities import MODEL_CAPABILITIES

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
    _nativeRendered = Signal(int)  # noqa: N815 - Qt signal naming
    _nativeFailed = Signal(int, str)  # noqa: N815 - Qt signal naming

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
        self._host_generation = 0
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
        self._nativeRendered.connect(self._native_frame_rendered)
        self._nativeFailed.connect(self._native_failed)
        canvas.destroyed.connect(self.close)

    @property
    def active(self):
        settings = self.canvas.controller.settings
        return (
            not self.closed
            and not self.reason
            and settings.gribWindStyle in ("barbs", "streamlines")
        )

    @property
    def enc_active(self):
        return self.active and self.widget is not None and self.widget._enc_chart_layer

    def _marine_state_changed(self, generation, _state):
        if generation != self._host_generation:
            return
        self.canvas.queue_draw()
        self._label()

    def request(self):
        if not self.closed and not self.timer.isActive():
            self.timer.start(0)

    @property
    def native_fields(self):
        return bool(getattr(self.canvas.controller, "native_weather_fields", False))

    def _native_frame_rendered(self, generation):
        if generation == self._host_generation:
            self._frame_rendered()

    def _native_failed(self, generation, reason):
        if generation == self._host_generation:
            self.fail(reason)

    def _retire_native_widget(self):
        self.deadline.stop()
        widget, self.widget = self.widget, None
        foreground, self.foreground = self.foreground, None
        self._host_generation += 1
        self._source = None
        self._rendered = False
        if widget is not None:
            widget.dispose()
            widget.hide()
            # A hidden QQuickWidget need not render another synchronization.
            # Destruction delivers actual invalidation and native retirement.
            widget.deleteLater()
        if foreground is not None:
            foreground.hide()
            foreground.deleteLater()
        self.canvas._invalidate_base()

    def _create(self, source):
        if QGuiApplication.platformName() in ("offscreen", "minimal"):
            raise RuntimeError("hardware weather unavailable on this Qt platform")
        widget = None
        desktop_map = getattr(self.canvas, "_desktop_viewport", False)
        model = self._model(source, grid=desktop_map)
        try:
            self._host_generation += 1
            generation = self._host_generation
            from gweatherrouting.core.storage import DATA_DIR

            kwargs = dict(
                chart_layer=True,
                # The desktop map uses the viewport's world chart composition.
                # Non-desktop map hosts retain their existing ENC-only host.
                enc_chart_layer=True,
                chart_without_weather=desktop_map,
                gpu_weather_fill=desktop_map,
                enc_only=not desktop_map,
                chart_data_dir=DATA_DIR,
                chart_palette=self.canvas.controller.settings.chartPalette,
                gpu_streamlines=self.canvas.controller.gpu_streamlines,
            )
            if self.native_fields:
                kwargs["native_weather_fields"] = True
                kwargs["native_weather_budget"] = (
                    self.canvas.controller.native_weather_budget
                )
            widget = _viewport_widget_class()(model, self.canvas, **kwargs)
            widget.marineStateChanged.connect(
                lambda state: self._marine_state_changed(generation, state)
            )
            widget.nativeError.connect(
                lambda reason: self._nativeFailed.emit(generation, reason)
            )
            # The viewport owns pan/zoom input. The transparent foreground
            # sibling keeps the application's navigation/route drawing above it.
            widget.setMouseTracking(True)
            if desktop_map:
                widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                widget.cameraChanged.connect(self._camera_changed)
                widget.chartStateChanged.connect(
                    lambda state: self._chart_state_changed(generation, state)
                )
            else:
                widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            widget.installEventFilter(self)
            widget._status.hide()
            if self.native_fields:
                widget.stateChanged.connect(
                    lambda state: (
                        self._state_changed(state)
                        if generation == self._host_generation
                        else None
                    )
                )
                widget.sceneGraphError.connect(
                    lambda _code, reason: self._nativeFailed.emit(generation, reason)
                )
                widget.statusChanged.connect(
                    lambda status: (
                        self._qml_status(status)
                        if generation == self._host_generation
                        else None
                    )
                )
                widget.quickWindow().afterRendering.connect(
                    lambda: self._nativeRendered.emit(generation)
                )
            else:
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
    def _model(source, grid=False):
        return GribViewportModel(
            () if source is None else (source.path,),
            display_identity=None if source is None else source.display_identity,
            resident_time_window=True,
            grid=grid,
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

    def _chart_state_changed(self, generation, state):
        if generation != self._host_generation or self.widget is None:
            return
        if state == "unavailable" and getattr(self.canvas, "_desktop_viewport", False):
            chart = self.widget._chart_model
            reason = (
                getattr(chart, "error", "")
                or getattr(chart, "get_basemap_status", lambda: "")()
                or "world basemap unavailable"
            )
            self.fail(reason)
            return
        self._label()

    def _label(self):  # noqa: C901 - status composition spans independent fallbacks
        if self.closed:
            return
        selection = self.canvas.controller.display_forecast
        if self.reason:
            mode = (
                "Viewport unavailable: " + self.reason
                if getattr(self.canvas, "_desktop_viewport", False)
                else "Weather renderer unavailable: " + self.reason
            )
        elif not self.canvas.wind_visible:
            mode = "Wind hidden"
        elif self.canvas.controller.settings.gribWindStyle == "streamlines":
            mode = "Viewport streamlines: " + self._state
        elif (
            getattr(self.canvas.controller.settings, "gribBarbDensity", "standard")
            == "all"
        ):
            mode = "Viewport wind (all forecast points, unthinned): " + self._state
        else:
            mode = "Viewport wind: " + self._state
        charts = getattr(self.canvas, "charts", None)
        basemap = charts.basemap_status(self.canvas) if charts is not None else ""
        if getattr(self.canvas, "_desktop_viewport", False) and self.widget:
            chart = self.widget._chart_model
            if chart is not None:
                basemap = chart.get_basemap_status()
                if chart.state == "unavailable":
                    basemap = chart.error or basemap or "World basemap unavailable"
                elif chart.state == "loading" and not basemap:
                    basemap = "World basemap loading…"
        marine_status = ""
        if self.widget is not None and self.canvas.marine.visible:
            marine_status = "Viewport currents: " + self.widget._marine_layer.state
        model_status = self._model_status(selection)
        self.status.setText(
            "\n".join(
                text for text in (model_status, mode, marine_status, basemap) if text
            )
        )
        self.status.setToolTip(selection.error)
        self.status.setFixedWidth(max(80, min(530, self.canvas.width() - 16)))
        self.status.adjustSize()
        self.status.move(8, max(8, self.canvas.height() - self.status.height() - 8))
        self.status.show()
        self.status.raise_()

    def _model_status(self, selection):
        model_id = ""
        analysis = getattr(self.canvas.controller, "analysis", None)
        active = getattr(analysis, "active", None)
        if active is not None:
            model_id = active.entry.model_id
        else:
            sources_method = getattr(selection, "sources", None)
            sources = sources_method() if callable(sources_method) else ()
            if sources:
                model_id = getattr(sources[0], "model_name", "")
        if not model_id:
            return selection.status
        capabilities = MODEL_CAPABILITIES.get(model_id)
        model_name = capabilities.provider_name if capabilities else model_id
        return f"Model: {model_name}"

    def _drop_widget(self):
        """Chart-enabled children own their source; replace them as a whole."""
        if self.native_fields and self.widget is not None:
            self._retire_native_widget()
            return
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

    def _sync_active(self, source):  # noqa: C901 - host configuration is explicit
        # Capture the host's restored/requested view before touching the child.
        # Resize, style and show operations can synchronously emit cameraChanged
        # with the child's previous camera, before it has adopted this request.
        camera_request = getattr(self.canvas, "requested_viewport_camera", None)
        camera = (
            camera_request()
            if camera_request is not None
            else ViewportCamera(
                self.canvas._center_lat,
                self.canvas._center_lon,
                self.canvas._zoom,
                self.canvas.width(),
                self.canvas.height(),
                self.canvas.time_control.time,
                self.canvas.controller.settings.gribBarbDensity,
                True,
            )
        )
        if self.widget is not None and (
            self.widget._chart_palette != self.canvas.controller.settings.chartPalette
        ):
            self._drop_widget()
        if self.widget is None:
            self._create(source)
            self._source = source
        elif source is not self._source:
            chart_model = self.widget._chart_model
            if chart_model is not None and not chart_model._enc_only:
                # A chart-enabled viewport binds chart/isobar composition to
                # its forecast model. Replace it atomically on a display-source
                # change rather than keeping a chart worker bound to old data.
                self._drop_widget()
                self._create(source)
            else:
                self.widget.replace_model(self._model(source))
            self._source = source
        widget, foreground = self.widget, self.foreground
        if widget is None or foreground is None:
            raise RuntimeError("ENC/weather viewport initialization failed")
        if widget._camera != camera:
            widget.set_camera(camera)
        widget.setGeometry(self.canvas.rect())
        foreground.setGeometry(self.canvas.rect())
        widget._smooth_zoom = getattr(self.canvas, "_smooth_zoom", True)
        widget._pan_inertia_enabled = getattr(self.canvas, "_pan_inertia_enabled", True)
        widget.set_wind_opacity(
            self.canvas.controller.settings.gribArrowOpacity
            if self.canvas.wind_visible
            else 0
        )
        widget.set_wind_style(self.canvas.controller.settings.gribWindStyle)
        from gweatherrouting.rendering.marinepresentation import chart_instant

        marine = self.canvas.marine
        marine.watch(self.canvas)
        widget.set_marine(
            marine.sources,
            chart_instant(marine.time_control.time),
            marine.visible,
        )
        widget.show()
        route_overlay = getattr(self.canvas, "viewport_route_overlay", None)
        if route_overlay is not None:
            widget.set_route_overlay(route_overlay())
        if not getattr(self.canvas, "_desktop_viewport", False):
            cache, target = self.canvas._resolve_reusable_layer(
                self.canvas._base_cache,
                self.canvas._base_key(),
                self.canvas._base_image,
            )
            self.canvas._base_cache = cache
            widget.set_base(cache[4], target)
        if widget._environment is not None:
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
        timeline = getattr(self.canvas, "timeline", None)
        if timeline is not None:
            timeline.raise_()
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
        elif self.widget is not None and self.native_fields:
            self._retire_native_widget()
            self._state = "unavailable" if source is None else "hidden"
            self.deadline.stop()
        elif self.widget is not None:
            self._drop_widget()
        self._label()
        # Existing interactive widgets must remain above both presentation children.
        self.canvas.coverage_notice.raise_()
        if hasattr(self.canvas, "coverage_fetch"):
            self.canvas.coverage_fetch.raise_()
        coverage_fetch_buttons = getattr(self.canvas, "coverage_fetch_buttons", None)
        if coverage_fetch_buttons:
            for button in coverage_fetch_buttons.values():
                button.raise_()
        timeline = getattr(self.canvas, "timeline", None)
        if timeline is not None:
            timeline.raise_()

    def source_changed(self):
        # Hide old weather synchronously, before the coalesced GUI refresh.
        if self.widget is not None and self.native_fields:
            self._retire_native_widget()
        elif self.widget is not None:
            self.widget.replace_model(self._model(None))
            self._source = None
            self.canvas._invalidate_base()
        self.request()

    def suspend(self):
        self.timer.stop()
        self.deadline.stop()
        if self.widget is not None and self.native_fields:
            self._retire_native_widget()
        elif self.widget is not None:
            self._drop_widget()

    def fail(self, reason):
        if self.closed or self.reason:
            return
        self.reason = reason
        self.suspend()
        self.canvas.controller.set_renderer_status(
            self.canvas, "unavailable", reason=reason
        )
        self.canvas.queue_draw()
        self._label()

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.widget and event.type() == QEvent.Type.MouseMove:
            self.canvas._hover(event.position())
        if watched is self.widget and event.type() == QEvent.Type.Leave:
            self.canvas._cursor_screen_position = None
            cursor_timer = getattr(self.canvas, "_cursor_refresh_timer", None)
            if cursor_timer is not None:
                cursor_timer.stop()
            self.canvas._cancel_cursor()
        if (
            watched is self.widget
            and event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
            and self.canvas.move_callback is not None
        ):
            position = self.canvas.convert_screen_to_geographic(
                event.position().x(), event.position().y()
            )
            callback = self.canvas.move_callback
            self.canvas.cancel_move()
            callback(position.get_degrees())
            return True
        if (
            watched is self.widget
            and getattr(self.canvas, "_desktop_viewport", False)
            and event.type() in (QEvent.Type.NativeGesture, QEvent.Type.Wheel)
        ):
            # The desktop viewport owns camera gestures. Legacy map input
            # remains the compatibility path for its navigation overlays only.
            return False
        if event.type() in (
            QEvent.Type.NativeGesture,
            QEvent.Type.ContextMenu,
            QEvent.Type.KeyPress,
        ):
            QCoreApplication.sendEvent(self.canvas, event)
            return True
        return super().eventFilter(watched, event)

    def _camera_changed(self, camera):
        """Mirror the viewport-owned camera for existing overlays and app APIs."""
        canvas = self.canvas
        if (
            self.closed
            or canvas.disposed
            or self.widget is None
            or self.widget._camera is None
        ):
            return
        adopt_camera = getattr(canvas, "adopt_viewport_camera", None)
        if adopt_camera is not None:
            adopt_camera(camera)
            return
        state = (
            camera.center_lat,
            camera.center_lon,
            camera.zoom,
        )
        previous = (canvas._center_lat, canvas._center_lon, canvas._zoom)
        if state == previous:
            return
        canvas._center_lat, canvas._center_lon, canvas._zoom = state
        canvas._cancel_cursor()
        canvas.invalidate_navigation()
        canvas.view_changed.emit()
        if self.foreground is not None:
            self.foreground.update()

    def close(self, *_):
        if self.closed:
            return
        self.closed = True
        self.timer.stop()
        self.deadline.stop()
        if self.widget is not None and self.native_fields:
            self._retire_native_widget()
        elif self.widget is not None:
            self.widget.dispose()
