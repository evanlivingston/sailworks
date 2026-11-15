"""Progress display for a historical-years ensemble run.

The engine reports two real tiers of progress: the whole batch (completed
routes out of the requested total) and the route currently being calculated
(an effort percentage, or an indeterminate busy state).  There is no leg-level
progress in the engine, so this widget deliberately shows none.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QWidget,
)

#: How often the widget samples the panel's progress probe, in milliseconds.
REFRESH_MS = 250


def format_eta(seconds):
    """A compact remaining-time string, or ``""`` when nothing is known."""
    if seconds is None:
        return ""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    minutes = max(1, int(round(seconds / 60.0)))
    if minutes < 60:
        return f"~{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"~{hours} h {minutes:02d} min"


def _caption(text):
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


def _compact_count(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return ""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m".replace(".0m", "m")
    if value >= 1_000:
        return f"{value / 1_000:.1f}k".replace(".0k", "k")
    return f"{value:.0f}"


class HistoricalProgress(QWidget):
    """Two-tier progress for one historical ensemble job.

    Call :meth:`set_probe` with a callable returning a snapshot dict and
    :meth:`start` when the run begins.  :meth:`stop` hides the widget, which is
    the idle state: nothing is shown until a run starts.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._probe = None
        self._weather_phase = False
        self._finished = False
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh_from_probe)
        self._build()
        self.reset()

    # -- construction ----------------------------------------------------
    def _build(self):
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(4)
        layout.setColumnStretch(1, 1)

        self.overall_caption = _caption("Overall")
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 100)
        self.overall_bar.setValue(0)
        self.overall_bar.setTextVisible(False)
        self.overall_bar.setFixedHeight(10)
        self.overall_bar.setMinimumWidth(60)
        self.overall_bar.setAccessibleName("Overall historical progress")

        self.overall_label = QLabel()
        self.overall_label.setAccessibleName("Completed routes")
        self.overall_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.eta_label = QLabel()
        self.eta_label.setAccessibleName("Estimated time remaining")
        self.eta_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        layout.addWidget(self.overall_caption, 0, 0)
        layout.addWidget(self.overall_bar, 0, 1)
        layout.addWidget(self.overall_label, 0, 2)
        layout.addWidget(self.eta_label, 0, 3)

        self.route_caption = _caption("Current route")
        self.route_bar = QProgressBar()
        self.route_bar.setRange(0, 100)
        self.route_bar.setValue(0)
        self.route_bar.setTextVisible(False)
        self.route_bar.setFixedHeight(10)
        self.route_bar.setMinimumWidth(60)
        self.route_bar.setAccessibleName("Current route progress")

        self.route_label = QLabel()
        self.route_label.setAccessibleName("Current route stage")
        self.route_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        layout.addWidget(self.route_caption, 1, 0)
        layout.addWidget(self.route_bar, 1, 1)
        layout.addWidget(self.route_label, 1, 2, 1, 2)

        self.counters_label = QLabel()
        self.counters_label.setWordWrap(True)
        self.counters_label.setAccessibleName("Routing counters")
        font = self.counters_label.font()
        font.setPointSize(max(7, font.pointSize() - 1))
        self.counters_label.setFont(font)
        layout.addWidget(self.counters_label, 2, 1, 1, 3)

    # -- probe plumbing --------------------------------------------------
    def set_probe(self, probe):
        """Install a callable returning the current progress snapshot."""
        self._probe = probe

    def start(self):
        """Show the widget and begin sampling the probe."""
        if self._probe is None:
            return
        self.reset()
        self.setVisible(True)
        self.refresh_from_probe()
        self._timer.start()

    def stop(self):
        """Stop sampling and hide the widget."""
        self._timer.stop()
        self.setVisible(False)

    # -- refresh ---------------------------------------------------------
    def refresh_from_probe(self):
        """Pull one snapshot from the probe and update every display element."""
        probe = self._probe
        if probe is None or self._finished:
            return
        try:
            snapshot = probe()
        except Exception:
            return
        if not isinstance(snapshot, dict):
            return

        completed = max(0, int(snapshot.get("completed") or 0))
        total = max(0, int(snapshot.get("total") or 0))
        route_percent = snapshot.get("route_percent")
        if route_percent is not None:
            try:
                route_percent = float(route_percent)
            except (TypeError, ValueError):
                route_percent = None
        indeterminate = bool(snapshot.get("route_indeterminate", route_percent is None))
        if indeterminate:
            route_percent = None
        stage = str(snapshot.get("stage") or "")
        counters = str(snapshot.get("counters") or "")
        try:
            elapsed = float(snapshot.get("elapsed_seconds") or 0.0)
        except (TypeError, ValueError):
            elapsed = 0.0

        if self._weather_phase:
            # Acquisition reports unit progress only.  Leave the weather
            # display in place until the route tier actually starts; the
            # snapshot's ``routing`` flag marks that handover, so a snapshot
            # that merely lacks a route percent cannot clear the display.
            routing = bool(snapshot.get("routing", route_percent is not None))
            if not routing and completed == 0:
                return
            self._weather_phase = False

        if total > 0 and completed >= total:
            fraction = 1.0
        elif total > 0:
            in_flight = (route_percent / 100.0) if route_percent is not None else 0.0
            fraction = (completed + in_flight) / total
            fraction = min(max(fraction, 0.0), 0.999)
        else:
            fraction = 0.0

        self.overall_bar.setValue(int(round(fraction * 100)))
        self.overall_label.setText(f"{completed} / {total} routes")
        self.overall_label.setVisible(True)

        eta = ""
        if total > 0 and completed < total:
            if fraction >= 0.1 and elapsed >= 30 and (1.0 - fraction) > 0.02:
                eta = format_eta(elapsed * (1.0 - fraction) / fraction)
            if not eta:
                eta = "estimating…"
        self.eta_label.setText(eta)
        self.eta_label.setVisible(bool(eta))

        self._set_route_visible(True)
        if indeterminate:
            self.route_bar.setRange(0, 0)
            self.route_label.setText(f"working · {stage}" if stage else "working")
        else:
            self.route_bar.setRange(0, 100)
            percent = route_percent if route_percent is not None else 0.0
            self.route_bar.setValue(int(round(min(max(percent, 0.0), 100.0))))
            self.route_label.setText(
                f"{percent:.0f}% · {stage}" if stage else f"{percent:.0f}%"
            )

        self.counters_label.setText(counters)
        self.counters_label.setVisible(bool(counters))

        if not snapshot.get("running", True):
            self._timer.stop()

    # -- special phases --------------------------------------------------
    def set_weather_phase(self, completed, total, message=""):
        """Show CDS acquisition as whole units; there is no route percent yet."""
        self._weather_phase = True
        self._finished = False
        completed = max(0, int(completed or 0))
        total = max(0, int(total or 0))
        fraction = (completed / total) if total > 0 else 0.0
        fraction = min(max(fraction, 0.0), 1.0)
        self.setVisible(True)
        self.overall_bar.setRange(0, 100)
        self.overall_bar.setValue(int(round(fraction * 100)))
        self.overall_label.setText(f"{completed} / {total} weather units")
        self.overall_label.setVisible(True)
        self.eta_label.setText("estimating…")
        self.eta_label.setVisible(True)
        self._set_route_visible(False)
        self.counters_label.setText(str(message or ""))
        self.counters_label.setVisible(bool(message))

    def set_finished(self, ok, message=""):
        """Stop sampling and leave the final state on screen."""
        self._timer.stop()
        self._weather_phase = False
        self._finished = True
        self.setVisible(True)
        if ok:
            self.overall_bar.setRange(0, 100)
            self.overall_bar.setValue(100)
        self.overall_label.setText(str(message or ("done" if ok else "stopped")))
        self.overall_label.setVisible(True)
        self.eta_label.setText("")
        self.eta_label.setVisible(False)
        self.counters_label.setVisible(False)
        self._set_route_visible(False)

    def reset(self):
        """Return to the idle state: zeroed bars, no labels, hidden."""
        self._timer.stop()
        self._weather_phase = False
        self._finished = False
        self.overall_bar.setRange(0, 100)
        self.overall_bar.setValue(0)
        self.route_bar.setRange(0, 100)
        self.route_bar.setValue(0)
        for label in (
            self.overall_label,
            self.eta_label,
            self.route_label,
            self.counters_label,
        ):
            label.setText("")
            label.setVisible(False)
        self._set_route_visible(True)
        self.setVisible(False)

    # -- internals -------------------------------------------------------
    def _set_route_visible(self, visible):
        visible = bool(visible)
        self.route_caption.setVisible(visible)
        self.route_bar.setVisible(visible)
        self.route_label.setVisible(visible)
