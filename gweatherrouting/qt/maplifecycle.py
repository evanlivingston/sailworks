"""Terminal and visibility lifecycle shared by Qt map hosts."""


class MapLifecycleMixin:
    def showEvent(self, event):  # noqa: N802 - Qt virtual method name
        super().showEvent(event)
        if self._weather_presenter is not None:
            self._weather_presenter.request()
        if self._gpu is not None and self._gpu.active:
            self._gpu.request()

    def resizeEvent(self, event):  # noqa: N802 - Qt virtual method name
        self._cancel_cursor()
        self._cancel_inertia(settle=False)
        self._finish_zoom_silent()
        self.drag = None
        if self.coverage_notice.isVisible():
            self.coverage_notice.setFixedWidth(max(80, min(360, self.width() - 24)))
            self.coverage_notice.adjustSize()
            self._layout_coverage()
        super().resizeEvent(event)
        if self._weather_presenter is not None:
            self._weather_presenter.request()
        if self._gpu is not None and self._gpu.active:
            self._gpu.request()

    def dispose(self):
        if self.disposed:
            return
        self.disposed = True
        if self._weather_presenter is not None:
            self.controller.display_forecast.changed.disconnect(
                self._display_forecast_changed
            )
            self._weather_presenter.close()
        if self._gpu is not None:
            self._gpu.close()
        if hasattr(self.controller, "forget_renderer"):
            self.controller.forget_renderer(self)
        self._wind_cursor.close()
        self.controller.data.changed.disconnect(self._cancel_cursor)
        self.weather_coverage.close()
        self._disconnect_optional(
            self.controller.data.task.changed, self._download_progress
        )
        self._disconnect_optional(
            self.controller.data.forecast_loaded, self._note_installed_forecast
        )
        self._disconnect_optional(
            self.controller.data.task.finished, self._fetch_finished
        )
        self.cursor_readout.emit("")
        self._cancel_inertia(settle=False)
        self._finish_zoom_silent()
        self.controller.boat_changed.disconnect(self._boat_changed)
        self.charts.forget_map(self)
        # Terminal teardown closes renderers even if queued completion callbacks
        # never run. Hide/show retains the reusable forget path.
        self._base_chart_renderer.close()
        if self.wind is not None:
            self.wind.close()
        self._overlay_services.close(self)
        for token in list(self._ticks):
            self.remove_tick_callback(token)
        self.time_control.disconnect("time_change", self._time_changed)

    def _disconnect_optional(self, signal, slot):
        """Disconnect a signal, tolerating a slot that was never connected."""
        try:
            signal.disconnect(slot)
        except (RuntimeError, TypeError):
            pass

    def hideEvent(self, event):  # noqa: N802 - Qt virtual method name
        if self._weather_presenter is not None:
            self._weather_presenter.suspend()
        if self._gpu is not None:
            self._gpu.suspend()
        self._gpu_geometry = None
        self._gpu_batches = ()
        self._gpu_wind_raster = None
        self._gpu_raster_key = None
        self._cancel_cursor()
        self._cancel_inertia(settle=False)
        self._finish_zoom_silent()
        self.drag = None
        self.cancel_move()
        self.charts._chs_renderer.forget(self)
        self._base_chart_renderer.forget(self)
        self._overlay_services.forget(self)
        # Drop cached pixels whose forgotten overlay state may never refresh.
        self._invalidate_base()
        self.invalidate_navigation()
        super().hideEvent(event)

    def closeEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.dispose()
        super().closeEvent(event)
