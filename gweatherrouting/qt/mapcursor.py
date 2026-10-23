"""Cursor weather and depth readouts for the Qt map canvas."""


class MapCursorMixin:
    def _weather_context(self):
        return (
            self.time_control.time,
            self.controller.display_forecast.sources(),
            self._center_lat,
            self._center_lon,
            self._zoom,
            self.width(),
            self.height(),
            self.wind_visible,
            self.marine.sources,
            self.marine.currents_visible,
        )

    def _cancel_cursor(self):
        self._wind_cursor.cancel()
        self._cursor_context = None
        self._cursor_position = None
        self._cursor_parts = {"wind": "", "current": "", "depth": ""}
        self.cursor_readout.emit("")

    def _emit_cursor_readout(self):
        parts = [
            part
            for part in (
                self._cursor_parts["wind"],
                self._cursor_parts["current"],
                self._cursor_parts["depth"],
            )
            if part
        ]
        if self._cursor_coords:
            parts.append(self._cursor_coords)
        self.cursor_readout.emit("  ·  ".join(parts))

    def _sample_depth(self):
        if (
            self.disposed
            or not self.isVisible()
            or self._cursor_position is None
            or self._cursor_context != self._weather_context()
        ):
            return
        lat, lon = self._cursor_position
        try:
            depth = self.charts.depth_at(lat, lon, self.get_scale())
        except Exception:
            depth = None
        text = ""
        if depth:
            if "sounding" in depth:
                text = "Sounding %.1f m (%.0f m away)" % (
                    depth["sounding"],
                    depth["distance_m"],
                )
            elif depth.get("area") and all(
                value is not None for value in depth["area"]
            ):
                low, high = sorted(depth["area"])
                text = "Depth %g–%g m" % (low, high)
        self._cursor_parts["depth"] = text

    def _hover(self, position):
        lat, lon = self.convert_screen_to_geographic(
            position.x(), position.y()
        ).get_degrees()
        context = self._weather_context()
        self._cursor_context = context
        self._cursor_position = (lat, lon)
        self._cursor_parts = {"wind": "", "current": "", "depth": ""}
        self._cursor_coords = "Lat %.6f°  Lon %.6f°" % (lat, lon)
        if not self.is_moving() and self.move_callback is None:
            marine = self.marine.cursor_text(lat, lon)
            if marine:
                self._cursor_parts["current"] = marine
        self._sample_depth()
        self._emit_cursor_readout()
        if self.is_moving() or self.move_callback is not None or not self.isVisible():
            self._wind_cursor.cancel()
            return
        if not self.wind_visible:
            self._wind_cursor.cancel()
            return

        def show(wind):
            if (
                self.disposed
                or not self.isVisible()
                or self._cursor_context != context
                or context != self._weather_context()
            ):
                return
            self._cursor_parts["wind"] = (
                "Wind %.1f°, %.1f kts" % (wind[0], wind[1] * 1.94384)
                if wind is not None
                else ""
            )
            self._emit_cursor_readout()

        self._wind_cursor.request(context[1], context[0], lat, lon, show)
