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
