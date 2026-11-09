"""Cursor weather and depth readouts for the Qt map canvas."""

from sailworks.rendering.isochrones import hit_test_isochrone_frame


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
            self.wave_visible,
            self.marine.sources,
            self.marine.currents_visible,
        )

    def _cancel_cursor(self):
        self._wind_cursor.cancel()
        self._cursor_context = None
        self._cursor_position = None
        self._cursor_parts = {
            "wind": "",
            "gust": "",
            "current": "",
            "wave": "",
            "depth": "",
            "isochrone": "",
        }
        self._isochrone_hit = None
        self.cursor_readout.emit("")

    def _emit_cursor_readout(self):
        parts = [
            part
            for part in (
                self._cursor_parts.get("wind", ""),
                self._cursor_parts.get("gust", ""),
                self._cursor_parts.get("current", ""),
                self._cursor_parts.get("wave", ""),
                self._cursor_parts.get("depth", ""),
                self._cursor_parts.get("isochrone", ""),
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
        self._cursor_screen_position = position
        self._cursor_parts = {
            "wind": "Wind …",
            "gust": "",
            "current": "",
            "wave": "",
            "depth": "",
            "isochrone": "",
        }
        point = (lat, lon)
        frame = getattr(self, "isochrone_frame", None)
        hit = hit_test_isochrone_frame(frame, lat, lon) if frame is not None else None
        if hit is not None:
            self._cursor_parts["isochrone"] = (
                "Isochrone " + hit.valid_time if hit.valid_time else "Isochrone"
            )
        self._isochrone_hit = hit
        set_selected = getattr(self, "set_selected_isochrone", None)
        if callable(set_selected):
            set_selected(hit)
        self._cursor_coords = "Lat %.6f°  Lon %.6f°" % (lat, lon)
        if not self.is_moving() and self.move_callback is None:
            marine = self.marine.cursor_text(lat, lon)
            if marine:
                self._cursor_parts["current"] = marine
        self._sample_depth()
        self._emit_cursor_readout()
        if self.is_moving() or self.move_callback is not None or not self.isVisible():
            self._wind_cursor.cancel()
            self._cursor_parts["wind"] = "Wind unavailable"
            self._cursor_parts["wave"] = "Waves unavailable"
            self._emit_cursor_readout()
            return
        sources = context[1]
        if not sources:
            self._wind_cursor.cancel()
            self._cursor_parts["wind"] = "Wind unavailable"
            self._cursor_parts["wave"] = "Waves unavailable"
            self._emit_cursor_readout()
            return
        if not self.wave_visible:
            self._cursor_parts["wave"] = "Waves unavailable"
            self._emit_cursor_readout()

        def show(weather):
            if (
                self.disposed
                or not self.isVisible()
                or self._cursor_context != context
                or self._cursor_position != point
                or context != self._weather_context()
            ):
                return
            wind, gust, waves = weather
            self._cursor_parts["wind"] = (
                "Wind %.1f°, %.1f kts" % (wind[0], wind[1] * 1.94384)
                if wind is not None
                else "Wind unavailable"
            )
            self._cursor_parts["gust"] = (
                "Gust %.1f kts" % (gust * 1.94384) if gust is not None else ""
            )
            self._cursor_parts["wave"] = (
                "Wave %.1f m · %.0f s · %.0f°" % waves
                if waves is not None
                else "Waves unavailable"
            )
            self._emit_cursor_readout()

        self._wind_cursor.request(
            sources, context[0], lat, lon, show, wave_visible=self.wave_visible
        )
