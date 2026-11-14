"""Results view for a Historical years ensemble.

No plotting dependency: :class:`_CurveCanvas` paints the elapsed-time
completion CDF and the year x offset strip directly with ``QPainter``.
"""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

TABLE_COLUMNS = ("Year", "Offset", "Departure", "Outcome", "Duration (h)", "Route")

# outcome key -> (symbol, label, colour). The symbol prefixes the cell text so
# the state never depends on colour alone.
OUTCOME_STYLES = {
    "finished": ("✓", "Finished", QColor("#5aa66a")),
    "no_finish": ("—", "No finish", QColor("#c09a52")),
    "failed": ("✕", "Failed", QColor("#c25b5b")),
    "cancelled": ("○", "Cancelled", QColor("#8a8a8a")),
    "blocked": ("■", "Blocked", QColor("#8a7ac0")),
}
UNKNOWN_OUTCOME = ("?", "Unknown", QColor("#8a8a8a"))


def _outcome_key(outcome):
    key = str(outcome or "").strip().lower()
    if not key:
        return ""
    for prefix, canonical in (
        ("no_finish", "no_finish"),
        ("failed", "failed"),
        ("blocked", "blocked"),
        ("interrupted", "blocked"),
        ("cancelled", "cancelled"),
        ("canceled", "cancelled"),
        ("finished", "finished"),
    ):
        if key.startswith(prefix):
            return canonical
    return key


def _outcome_style(outcome):
    return OUTCOME_STYLES.get(_outcome_key(outcome), UNKNOWN_OUTCOME)


class _CurveCanvas(QWidget):
    """Completion CDF above, year x offset outcome strip below."""

    LEFT_MARGIN = 46
    RIGHT_MARGIN = 12
    TOP_MARGIN = 12
    BOTTOM_MARGIN = 28
    BACKGROUND = QColor("#1e1e1e")
    GRID = QColor("#3a3a3a")
    AXIS = QColor("#6a6a6a")
    TEXT = QColor("#c8c8c8")
    MUTED = QColor("#8a8a8a")
    LINE = QColor("#6fb1d8")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._curve = []
        self._strip = []
        self.setMinimumHeight(190)
        self.setAccessibleName("Completion curve and year strip")

    def sizeHint(self):
        return QSize(520, 240)

    def set_curve(self, points):
        self._curve = sorted((float(x), float(y)) for x, y in (points or []))
        self.update()

    def set_strip(self, points):
        self._strip = [dict(point) for point in (points or [])]
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt virtual method name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), self.BACKGROUND)
        area = QRectF(self.rect()).adjusted(
            self.LEFT_MARGIN, self.TOP_MARGIN, -self.RIGHT_MARGIN, -self.BOTTOM_MARGIN
        )
        if area.width() <= 1 or area.height() <= 1:
            return
        strip_height = min(90.0, area.height() * 0.34) if self._strip else 0.0
        curve_height = area.height() - (strip_height + (14.0 if strip_height else 0.0))
        curve_rect = QRectF(
            area.left(), area.top(), area.width(), max(20.0, curve_height)
        )
        self._paint_curve(painter, curve_rect)
        if strip_height:
            strip_rect = QRectF(
                area.left(),
                curve_rect.bottom() + 14.0,
                area.width(),
                strip_height,
            )
            self._paint_strip(painter, strip_rect)

    def _paint_curve(self, painter, rect):
        if not self._curve:
            painter.setPen(self.MUTED)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No completion data")
            return
        points = [(0.0, 0.0)] + self._curve
        max_x = max(x for x, _ in points) or 1.0
        max_y = max(y for _, y in points)
        max_y = 1.0 if max_y <= 1.0 else max_y
        top = rect.top()
        bottom = rect.bottom()
        left = rect.left()
        right = rect.right()

        painter.setPen(QPen(self.GRID, 1))
        for step in range(1, 4):
            y = bottom - (bottom - top) * step / 4.0
            painter.drawLine(QPointF(left, y), QPointF(right, y))
        painter.setPen(self.AXIS)
        painter.drawLine(QPointF(left, top), QPointF(left, bottom))
        painter.drawLine(QPointF(left, bottom), QPointF(right, bottom))

        painter.setPen(self.TEXT)
        for step in range(0, 5):
            y = bottom - (bottom - top) * step / 4.0
            painter.drawText(
                QRectF(left - 44, y - 8, 38, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{step * 25}%",
            )
        painter.setPen(self.MUTED)
        painter.drawText(
            QRectF(right - 90, bottom + 11, 90, 14),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "Elapsed (h)",
        )
        for step in range(0, 5):
            x = left + (right - left) * step / 4.0
            painter.drawText(
                QRectF(x - 20, bottom + 11, 40, 14),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                f"{max_x * step / 4:.0f}",
            )

        def project(x, y):
            return QPointF(
                left + (right - left) * (x / max_x),
                bottom - (bottom - top) * (y / max_y),
            )

        painter.setPen(QPen(self.LINE, 2))
        previous = project(*points[0])
        for x, y in points[1:]:
            current = project(x, y)
            corner = QPointF(current.x(), previous.y())
            painter.drawLine(previous, corner)
            painter.drawLine(corner, current)
            previous = current

    def _paint_strip(self, painter, rect):
        if not self._strip:
            return
        offsets = sorted({int(point.get("offset_days", 0)) for point in self._strip})
        years = sorted({int(point.get("year", 0)) for point in self._strip})
        if not offsets or not years:
            return
        cell_w = rect.width() / max(1, len(years))
        cell_h = rect.height() / max(1, len(offsets))
        lane = {offset: index for index, offset in enumerate(offsets)}
        year_index = {year: index for index, year in enumerate(years)}
        painter.setPen(Qt.PenStyle.NoPen)
        for point in self._strip:
            year = year_index[int(point.get("year", 0))]
            offset = lane[int(point.get("offset_days", 0))]
            colour = _outcome_style(point.get("outcome"))[2]
            painter.setBrush(colour)
            painter.drawRect(
                QRectF(
                    rect.left() + year * cell_w + 1.0,
                    rect.top() + offset * cell_h + 1.0,
                    max(1.0, cell_w - 2.0),
                    max(1.0, cell_h - 2.0),
                )
            )
        painter.setPen(self.MUTED)
        for year, index in year_index.items():
            if cell_w >= 26.0:
                painter.drawText(
                    QRectF(rect.left() + index * cell_w, rect.bottom() + 2, cell_w, 14),
                    Qt.AlignmentFlag.AlignCenter,
                    str(year),
                )


class HistoricalRoutingResults(QWidget):
    """Per-member table, aggregate curve and job-level actions."""

    member_selected = Signal(str)
    representatives_requested = Signal()
    show_all_requested = Signal()
    hide_all_requested = Signal()
    retry_failed_requested = Signal()
    recheck_requested = Signal()
    delete_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._median_member_id = None
        self._build()
        self.set_actions_enabled(False, False, False)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("Historical results summary")
        layout.addWidget(self.summary)

        self.canvas = _CurveCanvas()
        layout.addWidget(self.canvas, 1)

        self.table = QTableWidget(0, len(TABLE_COLUMNS))
        self.table.setHorizontalHeaderLabels(TABLE_COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setAccessibleName("Historical members")
        self.table.cellClicked.connect(self._row_clicked)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.median_button = self._button(buttons, "Median route", self._median_clicked)
        self.representatives_button = self._button(
            buttons, "Representatives", self.representatives_requested.emit
        )
        self.show_all_button = self._button(
            buttons, "Show all", self.show_all_requested.emit
        )
        self.hide_all_button = self._button(
            buttons, "Hide all", self.hide_all_requested.emit
        )
        self.retry_button = self._button(
            buttons, "Retry failed", self.retry_failed_requested.emit
        )
        self.recheck_button = self._button(
            buttons, "Recheck excluded years", self.recheck_requested.emit
        )
        self.delete_button = self._button(
            buttons, "Delete job and routes", self.delete_requested.emit
        )
        buttons.addStretch(1)
        layout.addLayout(buttons)

    def _button(self, layout, text, slot):
        button = QPushButton(text)
        button.setAccessibleName(text)
        button.clicked.connect(lambda *_: slot())
        layout.addWidget(button)
        return button

    # -- data ------------------------------------------------------------
    def set_summary(self, text):
        self.summary.setText(text or "")

    def set_rows(self, rows):
        rows = list(rows or [])
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            symbol, label, colour = _outcome_style(row.get("outcome"))
            member_id = str(row.get("member_id", ""))
            cells = (
                str(row.get("year", "")),
                f"{int(row.get('offset_days', 0)):+d}",
                str(row.get("departure", "") or "—"),
                f"{symbol} {label}",
                self._duration(row.get("duration_hours")),
                str(row.get("route_uid", "") or "—"),
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 4:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if column == 3:
                    item.setForeground(colour)
                    item.setToolTip(label)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, member_id)
                self.table.setItem(index, column, item)
        self.table.setSortingEnabled(True)
        self._rows = rows
        self._median_member_id = self._find_median()

    @staticmethod
    def _duration(value):
        if value is None or value == "":
            return "—"
        try:
            return f"{float(value):.1f}"
        except (TypeError, ValueError):
            return "—"

    def set_curve(self, points):
        self.canvas.set_curve(points)

    def set_strip(self, points):
        self.canvas.set_strip(points)

    def set_actions_enabled(self, has_failures, has_excluded, has_job):
        self.retry_button.setEnabled(bool(has_failures))
        self.recheck_button.setEnabled(bool(has_excluded) and bool(has_job))
        for button in (
            self.delete_button,
            self.median_button,
            self.representatives_button,
            self.show_all_button,
            self.hide_all_button,
        ):
            button.setEnabled(bool(has_job))
        self.median_button.setEnabled(
            bool(has_job) and self._median_member_id is not None
        )

    def clear(self):
        self.summary.setText("")
        self.canvas.set_curve([])
        self.canvas.set_strip([])
        self.table.setRowCount(0)
        self._rows = []
        self._median_member_id = None
        self.set_actions_enabled(False, False, False)

    # -- internals -------------------------------------------------------
    def _find_median(self):
        finished = [
            (float(row["duration_hours"]), str(row.get("member_id", "")))
            for row in self._rows
            if _outcome_key(row.get("outcome")) == "finished"
            and row.get("duration_hours") not in (None, "")
        ]
        if not finished:
            return None
        finished.sort(key=lambda entry: entry[0])
        return finished[len(finished) // 2][1]

    def _row_clicked(self, row, _column):
        item = self.table.item(row, 0)
        if item is None:
            return
        member_id = item.data(Qt.ItemDataRole.UserRole)
        if member_id:
            self.member_selected.emit(str(member_id))

    def _median_clicked(self):
        if self._median_member_id:
            self.member_selected.emit(self._median_member_id)
