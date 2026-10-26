"""Coverage notice for the currently published model forecast."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel


class MapCoverageMixin:
    def initialize_coverage_ui(self, controller):
        """Create a non-interactive notice for uncovered forecast samples."""
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
        self._coverage_gaps = None
        self._coverage_text = ""
        self._weather_services.initialize_weather_coverage(self, self._show_coverage)

    def _show_coverage(self, text, gaps=None):
        if self.disposed:
            return
        if gaps is not None:
            self._coverage_gaps = gaps
        elif text != self.coverage_notice.text():
            self._coverage_gaps = None
        self._coverage_text = text
        self.coverage_notice.setText(text)
        self.coverage_notice.setFixedWidth(max(80, min(360, self.width() - 24)))
        self.coverage_notice.adjustSize()
        self.coverage_notice.setVisible(bool(text) and self.isVisible())
        self._layout_coverage()
        self.coverage_notice.raise_()

    def _layout_coverage(self):
        self.coverage_notice.move(
            self.width() - self.coverage_notice.width() - 12,
            self.height() - self.coverage_notice.height() - 12,
        )

    def _check_weather_coverage(self):
        if self.disposed or self.is_moving():
            self.coverage_notice.hide()
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
        sources = self.controller.display_forecast.sources()
        self.weather_coverage.request(
            tuple(
                dict.fromkeys(
                    getattr(source, "entry_id", getattr(source, "model_name", "Unknown"))
                    for source in sources
                )
            ),
            fields,
            self.time_control.time,
            points,
            sources,
        )
        if (
            not self.weather_coverage.running
            and self.weather_coverage.pending is None
            and self.coverage_notice.text()
            and not self.coverage_notice.isVisible()
        ):
            self._show_coverage(self._coverage_text, self._coverage_gaps)
