"""Tides & currents downloads and saved sources for the Data Library.

The side panels (see marinepanel) are display-only: visibility toggles and
coverage status. All acquisition and saved-source management lives here.
"""

from datetime import timedelta

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gweatherrouting.core.mapviewport import MapViewport
from gweatherrouting.core.marine.chs import CHSProvider
from gweatherrouting.core.marine.models import Capability, sample_station
from gweatherrouting.core.marine.presentation import download_tides
from gweatherrouting.rendering.marinemaplayer import chart_instant
from .marinejobs import MarineJobs
from .marinepanel import CoverageTarget, CurrentSources, DownloadStart, text_label
from .theme import section_label


class TideSources(QTableWidget):
    """Saved station predictions with enable checkboxes."""

    def __init__(self, manager, clock):
        super().__init__(0, 3)
        self.manager = manager
        self.clock = clock
        self.setHorizontalHeaderLabels(["Use", "Station", "Height"])
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.setMinimumHeight(130)
        self.itemChanged.connect(self.toggle)
        self.status = text_label()
        self.reload()

    def reload(self):
        self.blockSignals(True)
        instant = chart_instant(self.clock.time)
        datasets = [
            d
            for d in self.manager.ordered_datasets()
            if Capability.WATER_LEVEL in d.capabilities
        ]
        self.setRowCount(len(datasets))
        for row, dataset in enumerate(datasets):
            use = QTableWidgetItem()
            use.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsSelectable
            )
            use.setData(Qt.ItemDataRole.UserRole, dataset.id)
            use.setCheckState(
                Qt.CheckState.Checked
                if self.manager.is_enabled(dataset.id)
                else Qt.CheckState.Unchecked
            )
            self.setItem(row, 0, use)
            result = sample_station(dataset, Capability.WATER_LEVEL, instant)
            height = (
                "%.2f m" % result.values[0] if result.status == "available" else "—"
            )
            if result.fell_back:
                height += " · fallback"
            for column, text in enumerate((dataset.station.name, height), 1):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                item.setToolTip(
                    dataset.provider
                    + " · "
                    + dataset.requested_start.isoformat()
                    + " → "
                    + dataset.requested_end.isoformat()
                )
                self.setItem(row, column, item)
        self.resizeColumnToContents(0)
        self.resizeColumnToContents(2)
        self.blockSignals(False)

    def toggle(self, item):
        if item.column() != 0:
            return
        try:
            self.manager.enable(
                item.data(Qt.ItemDataRole.UserRole),
                item.checkState() == Qt.CheckState.Checked,
            )
            self.status.clear()
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))


class MarineLibraryTab(QWidget):
    """Download currents/tides and manage saved sources in the Data Library."""

    def __init__(self, controller, canvas, settings_callback=None, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.canvas = canvas
        self.manager = controller.core.marine_manager
        self.clock = controller.time_control
        self.provider = CHSProvider()
        self.settings_callback = settings_callback
        self.closed = False
        self.jobs = MarineJobs(self)
        self.jobs.changed.connect(self.job_changed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.pages = QTabWidget()
        layout.addWidget(self.pages, 1)
        self.targets, self.starts, self.hours = {}, {}, {}
        self.downloads = {}
        self.currents = CurrentSources(self.manager.fields)
        self.tides = TideSources(self.manager, self.clock)
        self.pages.addTab(
            self._download_page(
                "currents",
                "Choose a route or map area. Current models are selected "
                "automatically. Regional forecasts take priority over "
                "worldwide coverage.",
                self.currents,
            ),
            "Currents",
        )
        self.pages.addTab(
            self._download_page(
                "tides",
                "Water level at stations · metres above source datum. "
                "Station predictions do not describe currents along a route.",
                self.tides,
            ),
            "Tides",
        )
        self.status = text_label()
        layout.addWidget(self.status)
        self.activity = QWidget()
        activity = QHBoxLayout(self.activity)
        activity.setContentsMargins(0, 0, 0, 0)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(self.jobs.cancel_download)
        activity.addWidget(self.progress, 1)
        activity.addWidget(self.cancel)
        layout.addWidget(self.activity)
        self.activity.hide()
        self.clock.connect("time_change", self.invalidate)
        self.manager.connect("changed", self.sources_changed)
        self.manager.fields.connect("changed", self.sources_changed)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def _download_page(self, kind, note, sources):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.addWidget(section_label("Download"))
        info = text_label(note)
        layout.addWidget(info)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        target = CoverageTarget(self.controller.core, self.canvas)
        self.targets[kind] = target
        form.addRow("Coverage for", target)
        start = DownloadStart(self.clock)
        self.starts[kind] = start
        form.addRow("Start (UTC)", start)
        hours = QSpinBox()
        hours.setRange(1, 168)
        hours.setValue(24)
        self.hours[kind] = hours
        form.addRow("Hours", hours)
        layout.addLayout(form)
        row = QHBoxLayout()
        download = QPushButton("Download data")
        download.clicked.connect(lambda _=False, kind=kind: self.download_data(kind))
        self.downloads[kind] = download
        row.addWidget(download)
        if self.settings_callback is not None:
            advanced = QPushButton("Advanced…")
            advanced.setToolTip("Saved source priority and account settings")
            advanced.clicked.connect(
                lambda _=False, kind=kind: self.settings_callback(kind)
            )
            row.addWidget(advanced)
        layout.addLayout(row)
        layout.addWidget(section_label("Saved sources"))
        layout.addWidget(sources, 1)
        if isinstance(sources, TideSources):
            layout.addWidget(sources.status)
        else:
            layout.addWidget(sources.status)
        return page

    @property
    def busy(self):
        return self.jobs.busy

    def set_kind(self, kind):
        self.pages.setCurrentIndex(1 if kind == "tides" else 0)

    def sources_changed(self, *_):
        self.controller.scheduler.submit(self._sources_changed)

    def _sources_changed(self):
        if self.closed:
            return
        self.currents.reload()
        self.tides.reload()
        self.refresh()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.refresh()

    def invalidate(self, *_):
        if not self.closed:
            for start in self.starts.values():
                start.refresh()
            self.refresh()

    def refresh(self):
        if self.closed or not self.isVisible():
            return
        for target in self.targets.values():
            target.refresh()
        self.tides.reload()

    def refresh_buttons(self, blocked):
        for download in self.downloads.values():
            download.setEnabled(not blocked and not self.jobs.busy)
        self.currents.tree.setEnabled(not blocked)
        self.currents.remove.setEnabled(not blocked)
        self.tides.setEnabled(not blocked)

    def download_data(self, kind):
        if self.jobs.busy or self.closed:
            return
        try:
            area = self.targets[kind].area()
            start = self.starts[kind].instant()
            end = start + timedelta(hours=self.hours[kind].value())
            view = MapViewport(self.canvas)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        if kind == "currents":
            planner = self.manager.coverage_planner

            def operation(cancel, report):
                return planner.download(area, start, end, cancel, report)

            complete = self.complete_currents
            message = "Downloading current coverage…"
        else:
            cached = tuple(self.manager.stations)
            route_area = area if self.targets[kind].currentIndex() > 0 else None

            def operation(cancel, report):
                return download_tides(
                    self.manager,
                    self.provider,
                    view,
                    route_area,
                    cached,
                    start,
                    end,
                    cancel,
                    report,
                )

            complete = self.complete_tides
            message = "Finding tide stations in the selected area…"
        self.jobs.start(message, operation, complete)

    def complete_currents(self, result):
        fields, issues = result
        for field in fields:
            self.manager.fields.add(field)
        self._sources_changed()
        return "Saved %d spatial datasets for offline use.\n%s" % (
            len(fields),
            "\n".join(issues),
        )

    def complete_tides(self, result):
        stations, datasets, issues, reused = result
        self.manager.store_stations(stations)
        for dataset in datasets:
            if dataset.id not in self.manager.datasets:
                self.manager.add(dataset)
            self.manager.enable(dataset.id)
        self._sources_changed()
        return (
            "Saved %d tide station predictions; reused %d already downloaded.\n%s"
            % (len(datasets) - reused, reused, "\n".join(issues))
        )

    def job_changed(self):
        if self.closed:
            return
        for download in self.downloads.values():
            download.setEnabled(not self.jobs.busy)
        self.activity.setVisible(self.jobs.busy)
        self.cancel.setEnabled(self.jobs.busy and not self.jobs.cancel.is_set())
        self.status.setText(self.jobs.message)

    def dispose(self):
        if self.closed:
            return
        self.closed = True
        self.timer.stop()
        self.jobs.close()
        self.clock.disconnect("time_change", self.invalidate)
        self.manager.disconnect("changed", self.sources_changed)
        self.manager.fields.disconnect("changed", self.sources_changed)
