"""Coverage notice and forecast download controls for Qt map hosts."""

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton


class MapCoverageMixin:
    _FETCH_BUTTON_STYLE = (
        "background: rgba(30, 33, 36, 220); color: #9fc3e0; "
        "padding: 6px 10px; border-radius: 4px; border: 1px solid #5295d2;"
    )
    # Suppress a repeat Fetch for a view/time/selection whose gaps survived
    # the last fetch (domain-edge points no re-download can fill). Newer
    # model cycles arrive within hours, so the offer returns after this.
    _FETCH_SUPPRESS_S = 6 * 3600

    def initialize_coverage_ui(self, controller):
        """Create coverage controls and connect their controller callbacks."""
        self.coverage_notice = QLabel(self)
        self.coverage_notice.setWordWrap(True)
        self.coverage_notice.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.coverage_notice.setStyleSheet(
            "background: rgba(30, 33, 36, 220); color: #e5c17c; "
            "padding: 8px; border-radius: 4px;"
        )
        self.coverage_notice.hide()
        self.coverage_fetch = QPushButton("Fetch", self)
        self.coverage_fetch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.coverage_fetch.setStyleSheet(self._FETCH_BUTTON_STYLE)
        self.coverage_fetch.hide()
        self.coverage_fetch.clicked.connect(
            lambda _checked=False: self._fetch_missing_coverage()
        )
        self.coverage_fetch_buttons = {}
        self._coverage_gaps = None
        self._coverage_text = ""
        self._fetch_awaiting = {}
        self._fetch_done = {}
        self._fetch_initiator = None
        controller.data.task.changed.connect(self._download_progress)
        controller.data.forecast_loaded.connect(self._note_installed_forecast)
        controller.data.task.finished.connect(self._fetch_finished)
        self._weather_services.initialize_weather_coverage(self, self._show_coverage)

    def _show_coverage(self, text, gaps=None):
        if self.disposed:
            return
        if gaps is not None:
            self._coverage_gaps = gaps
        elif text != self.coverage_notice.text():
            self._coverage_gaps = None
        for model in list(self._fetch_done):
            if model not in (self._coverage_gaps or {}):
                del self._fetch_done[model]
        self._coverage_text = text
        suppressed = self._fetch_suppressed()
        if text and suppressed and self._coverage_gaps:
            lines = []
            for model, missing in self._coverage_gaps.items():
                short = model.removeprefix("NOAA ").removeprefix("CMC ")
                status = ", ".join(missing)
                if model in suppressed:
                    status += " (downloaded; gaps remain)"
                lines.append(f"{short}: {status}")
            text = "\n".join(lines)
        self.coverage_notice.setText(text)
        visible = bool(text) and self.isVisible()
        self.coverage_notice.setFixedWidth(max(80, min(360, self.width() - 24)))
        self.coverage_notice.adjustSize()
        self.coverage_notice.setVisible(visible)
        self._refresh_fetch_buttons()
        self.coverage_notice.raise_()
        for button in self._visible_fetch_buttons():
            button.raise_()

    def _layout_coverage(self):
        self.coverage_notice.move(
            self.width() - self.coverage_notice.width() - 12,
            self.height() - self.coverage_notice.height() - 12,
        )
        buttons = self._visible_fetch_buttons()
        if not buttons:
            return
        for button in buttons:
            button.adjustSize()
        width = max(button.width() for button in buttons)
        spacing = 6
        total = sum(button.height() for button in buttons) + spacing * (
            len(buttons) - 1
        )
        x = self.coverage_notice.x() - width - 8
        if x < 8:
            x = self.width() - width - 12
            y = self.coverage_notice.y() - total - 6
        else:
            y = self.coverage_notice.y() + self.coverage_notice.height() - total
        y = max(8, y)
        for button in buttons:
            button.move(x + width - button.width(), y)
            y += button.height() + spacing

    def _visible_fetch_buttons(self):
        """Visible fetch buttons: per-model in notice order, Fetch all last."""
        gaps = self._coverage_gaps or {}
        ordered = [
            self.coverage_fetch_buttons[model]
            for model in gaps
            if model in self.coverage_fetch_buttons
            and self.coverage_fetch_buttons[model].isVisible()
        ]
        if self.coverage_fetch.isVisible():
            ordered.append(self.coverage_fetch)
        return ordered

    def _ensure_fetch_button(self, model):
        button = self.coverage_fetch_buttons.get(model)
        if button is None:
            button = QPushButton(model.removeprefix("NOAA ").removeprefix("CMC "), self)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(self._FETCH_BUTTON_STYLE)
            button.hide()
            button.clicked.connect(
                lambda _checked=False, m=model: self._fetch_missing_coverage((m,))
            )
            self.coverage_fetch_buttons[model] = button
        return button

    def _refresh_fetch_buttons(self, estimate=True):
        """Rebuild Fetch-all plus per-model button visibility, labels, layout."""
        fresh = self._fetch_models()
        downloading = self._forecast_downloading()
        sizes = (
            self._estimate_models_coverage(fresh)
            if estimate and not downloading
            else {}
        )
        bounds_ok = self._view_download_bounds() is not None
        base = (
            bool(self.coverage_notice.text())
            and self.coverage_notice.isVisible()
            and bounds_ok
            and (downloading or not self._download_busy())
        )
        if downloading:
            percent = self.controller.data.task.percent
            progress = f"Fetching… {percent}%" if percent >= 0 else "Fetching…"
        else:
            progress = ""
        initiator = self._fetch_initiator if downloading else None
        for model in fresh:
            button = self._ensure_fetch_button(model)
            short = model.removeprefix("NOAA ").removeprefix("CMC ")
            if downloading:
                show = bool(base and model == initiator)
                button.setText(f"{short} · {progress}" if show else short)
            else:
                show = bool(base)
                size = sizes.get(model)
                button.setText(f"{short} ({size})" if size else short)
            button.setVisible(show)
            button.setEnabled(bool(show and not downloading))
        for model, button in self.coverage_fetch_buttons.items():
            if model not in fresh:
                button.hide()
        show_all = bool(
            base
            and fresh
            and (not downloading or initiator == "all" or not self._fetch_awaiting)
        )
        if downloading:
            self.coverage_fetch.setText(progress)
        elif len(fresh) == 1:
            size = sizes.get(fresh[0])
            self.coverage_fetch.setText(f"Fetch ({size})" if size else "Fetch")
        else:
            self.coverage_fetch.setText("Fetch all")
        self.coverage_fetch.setVisible(show_all)
        self.coverage_fetch.setEnabled(bool(show_all and not downloading))
        self._layout_coverage()
        for button in self._visible_fetch_buttons():
            button.raise_()

    def _fetch_models(self):
        suppressed = set(self._fetch_suppressed())
        from gweatherrouting.core.modeldownloads import PROVIDERS

        gaps = self._coverage_gaps or {}
        selected = tuple(self.controller.data.selection.models)
        return tuple(
            m for m in gaps if m in PROVIDERS and m in selected and m not in suppressed
        )

    def _fetch_suppressed(self):
        """Gap models already fetched for this view/time/selection.

        A repeat download would return the same run, so it cannot fill the
        remaining (domain-edge) gaps. Offered again once the view, time, or
        selection moves, a newer run lands, or the window lapses.
        """
        from gweatherrouting.core.modeldownloads import PROVIDERS

        gaps = self._coverage_gaps or {}
        selected = tuple(self.controller.data.selection.models)
        models = tuple(m for m in gaps if m in PROVIDERS and m in selected)
        context = self._coverage_context()
        now = time.monotonic()
        return tuple(
            model
            for model in models
            if (record := self._fetch_done.get(model)) is not None
            and context is not None
            and record[0] == context
            and record[1] is not None
            and self._model_max_start(model) == record[1]
            and now - record[2] < self._FETCH_SUPPRESS_S
        )

    def _coverage_context(self):
        """Coverage request inputs minus the installed-sources snapshot."""
        key = self.weather_coverage.key
        return key[:4] if key else None

    def _model_max_start(self, model):
        from gweatherrouting.core.forecastselection import forecast_source

        try:
            starts = [
                grib.start_time
                for grib in self.controller.core.grib_manager.gribs
                if forecast_source(grib) == model
                and getattr(grib, "start_time", None) is not None
            ]
            return max(starts) if starts else None
        except TypeError:
            return None

    def _note_installed_forecast(self, forecast):
        from gweatherrouting.core.forecastselection import forecast_source

        try:
            model = forecast_source(forecast)
        except Exception:
            return
        context = self._fetch_awaiting.pop(model, None)
        if context is None:
            return
        self._fetch_done[model] = (
            context,
            self._model_max_start(model),
            time.monotonic(),
        )

    def _fetch_finished(self, _result, _error):
        if getattr(self.controller.data, "operation", "") == "forecasts":
            self._fetch_awaiting.clear()
            self._fetch_initiator = None

    def _download_busy(self):
        data = self.controller.data
        task = getattr(data, "task", None)
        if task is not None and (task.busy or task.closed):
            return True
        owner = getattr(data, "owner", None) or self.controller
        if getattr(getattr(owner, "routing", None), "busy", False):
            return True
        for name in ("polars", "map_data"):
            subtask = getattr(getattr(owner, name, None), "task", None)
            if getattr(subtask, "busy", False):
                return True
        return False

    def _forecast_downloading(self):
        """A Fetch-style model download started by download_selected is running."""
        data = self.controller.data
        task = getattr(data, "task", None)
        return bool(task is not None and task.busy and data.operation == "forecasts")

    def _download_progress(self):
        """Mirror the running model download on the Fetch buttons, text only."""
        if self.disposed:
            return
        if not self._forecast_downloading():
            if any(
                "Fetching" in button.text()
                for button in (
                    self.coverage_fetch,
                    *self.coverage_fetch_buttons.values(),
                )
                if button.isVisible()
            ):
                self._show_coverage(self._coverage_text, self._coverage_gaps)
            return
        if self.is_moving():
            return
        self._refresh_fetch_buttons(estimate=False)

    def _view_download_bounds(self):
        try:
            top_left, bottom_right = self.get_bbox()
        except Exception:
            return None
        north, west = top_left.get_degrees()
        south, east = bottom_right.get_degrees()
        if east - west >= 360:
            west, east = -180, 180
        else:
            west, east = (west + 180) % 360 - 180, (east + 180) % 360 - 180
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            return None
        south, north = max(-90, south - 0.5), min(90, north + 0.5)
        west, east = max(-180, west - 0.5), min(180, east + 0.5)
        return south, west, north, east

    def _estimate_models_coverage(self, models):
        from gweatherrouting.core.modeldownloads import estimate_missing_download

        gaps = self._coverage_gaps or {}
        if not models:
            return {}
        bounds = self._view_download_bounds()
        if bounds is None:
            return {}
        selection = self.controller.data.selection
        sizes = {}
        for model in models:
            try:
                sizes[model] = estimate_missing_download(
                    {model: gaps[model]}, bounds, selection
                )
            except Exception:
                sizes[model] = None
        return sizes

    def _fetch_missing_coverage(self, models=None):
        fresh = self._fetch_models()
        requested = tuple(models) if models is not None else fresh
        requested = tuple(model for model in requested if model in fresh)
        if not requested or self._download_busy():
            return
        bounds = self._view_download_bounds()
        if bounds is None:
            return
        self._record_fetch_context(requested)
        self._fetch_initiator = "all" if models is None else requested[0]
        widgets = [self.coverage_fetch] + [
            self._ensure_fetch_button(model) for model in requested
        ]
        self._set_fetch_widgets_enabled(widgets, False)
        try:
            started = self.controller.data.download_selected(bounds, requested)
        except Exception:
            self._fetch_initiator = None
            self._set_fetch_widgets_enabled(widgets, True)
            raise
        if started:
            self._set_fetch_widgets_enabled(
                [self.coverage_fetch, *self.coverage_fetch_buttons.values()], False
            )
        else:
            self._fetch_initiator = None
            self._set_fetch_widgets_enabled(widgets, True)

    def _record_fetch_context(self, requested):
        """Remember the view context each requested model is fetched for."""
        context = self._coverage_context()
        if context is not None:
            for model in requested:
                self._fetch_awaiting[model] = context

    def _set_fetch_widgets_enabled(self, widgets, enabled):
        """Enable or disable the given fetch buttons in order."""
        for widget in widgets:
            widget.setEnabled(enabled)

    def _check_weather_coverage(self):
        from gweatherrouting.core.forecastselection import forecast_source

        if self.disposed or self.is_moving():
            self.coverage_notice.hide()
            self.coverage_fetch.hide()
            for button in self.coverage_fetch_buttons.values():
                button.hide()
            return
        fields = tuple(
            name
            for name, enabled in (
                ("wind", self.wind_visible),
                ("pressure", self.controller.settings.gribIsobarsVisible),
            )
            if enabled
        )
        points = tuple(
            self.convert_screen_to_geographic(
                self.width() * x, self.height() * y
            ).get_degrees()
            for y in (0.02, 0.5, 0.98)
            for x in (0.02, 0.5, 0.98)
        )
        points = tuple((lat, (lon + 180) % 360 - 180) for lat, lon in points)
        self.weather_coverage.request(
            tuple(
                dict.fromkeys(
                    forecast_source(source)
                    for source in self.controller.display_forecast.sources()
                )
            ),
            fields,
            self.time_control.time,
            points,
            self.controller.display_forecast.sources(),
        )
        if (
            not self.weather_coverage.running
            and self.weather_coverage.pending is None
            and self.coverage_notice.text()
            and not self.coverage_notice.isVisible()
        ):
            self._show_coverage(self._coverage_text, self._coverage_gaps)
