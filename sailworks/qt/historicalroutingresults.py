"""Job-level actions for a Historical years ensemble.

The per-member detail lives in the scene tree: each published route is a child
of the job's collection. The panel keeps only the maintenance actions and the
median finished route the representatives action needs.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class HistoricalRoutingResults(QWidget):
    """Job-level actions and the median member behind representatives."""

    retry_failed_requested = Signal()
    recheck_requested = Signal()
    delete_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        # Kept for the "representatives" action even though its button is gone:
        # visibility now lives in the scene tree.
        self._median_member_id = None
        self._build()
        self.set_actions_enabled(False, False, False)
        # No results clutter before the first run: the whole widget stays
        # hidden until a record is displayed.
        self.setVisible(False)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.empty_label = QLabel(
            "No historical run yet — configure the years above and press Calculate route."
        )
        self.empty_label.setWordWrap(True)
        self.empty_label.setAccessibleName("No historical run")
        layout.addWidget(self.empty_label)
        layout.addStretch(1)

        # Visibility lives in the scene tree; only job maintenance remains.
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
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
        # Labels may clip rather than force a horizontal scrollbar on the
        # narrow Compare page.
        button.setMinimumWidth(0)
        button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        button.clicked.connect(lambda *_: slot())
        layout.addWidget(button)
        return button

    # -- run history -----------------------------------------------------
    def set_has_run(self, has_run):
        self.empty_label.setVisible(not has_run)
        self.setVisible(bool(has_run))

    # -- data ------------------------------------------------------------
    def set_rows(self, rows):
        """Record the member rows and pick their median finished member.

        No widget shows the rows any more, but the representatives action still
        reads ``_median_member_id``, so the median stays computed here.
        """
        self._rows = list(rows or [])
        self._median_member_id = self._find_median()

    def set_actions_enabled(self, has_failures, has_excluded, has_job):
        self.retry_button.setEnabled(bool(has_failures))
        self.recheck_button.setEnabled(bool(has_excluded) and bool(has_job))
        self.delete_button.setEnabled(bool(has_job))

    def clear(self):
        self._rows = []
        self._median_member_id = None
        self.set_actions_enabled(False, False, False)
        self.set_has_run(False)

    # -- internals -------------------------------------------------------
    @staticmethod
    def _finished(outcome):
        return str(outcome or "").strip().lower().startswith("finished")

    def _find_median(self):
        finished = [
            (float(row["duration_hours"]), str(row.get("member_id", "")))
            for row in self._rows
            if self._finished(row.get("outcome"))
            and row.get("duration_hours") not in (None, "")
        ]
        if not finished:
            return None
        finished.sort(key=lambda entry: entry[0])
        return finished[len(finished) // 2][1]
