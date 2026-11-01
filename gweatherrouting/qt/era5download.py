"""User-directed, isolated ERA5 historical acquisition UI."""

from datetime import datetime, timedelta, timezone
import json
import math

from PySide6.QtCore import QDate, QDateTime, QTime, QTimer, QTimeZone, Qt
from PySide6.QtWidgets import (
    QDateTimeEdit,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gweatherrouting.core.era5 import Era5DownloadService, Era5Error, plan_request


class Era5DownloadTab(QWidget):
    """Download historical data without changing forecast or routing state."""

    def __init__(self, controller, capture_region=None, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.capture_region = capture_region
        self.service = Era5DownloadService(controller.analysis.state.store)
        self.plan = None
        self.job = None
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "ERA5 reanalysis history (UTC). Requests are limited to 31 days and "
                "a bounded area; historical imports never activate a forecast."
            )
        )
        instructions = QLabel(
            "CDS setup: create ~/.cdsapirc with "
            "url: https://cds.climate.copernicus.eu/api and "
            "key: <personal-access-token>, then accept the ERA5 dataset terms "
            "at https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        self.north, self.west = self._coord(90), self._coord(180)
        self.south, self.east = self._coord(-90), self._coord(-180)
        row = QHBoxLayout()
        for title, control in (
            ("North", self.north),
            ("West", self.west),
            ("South", self.south),
            ("East", self.east),
        ):
            row.addWidget(QLabel(title))
            row.addWidget(control)
        self.region_button = QPushButton("Use visible map region")
        self.region_button.clicked.connect(self.use_visible_region)
        row.addWidget(self.region_button)
        layout.addLayout(row)
        now = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        ) - timedelta(days=6)
        self.start = self._date(now - timedelta(days=1))
        self.end = self._date(now)
        row = QHBoxLayout()
        row.addWidget(QLabel("Start UTC"))
        row.addWidget(self.start)
        row.addWidget(QLabel("End UTC"))
        row.addWidget(self.end)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.request_button = QPushButton("Start download")
        self.cancel_button = QPushButton("Cancel")
        self.retry_button = QPushButton("Retry")
        self.request_button.clicked.connect(self.submit)
        self.cancel_button.clicked.connect(self.cancel)
        self.retry_button.clicked.connect(self.retry)
        row.addWidget(self.request_button)
        row.addWidget(self.cancel_button)
        row.addWidget(self.retry_button)
        layout.addLayout(row)
        self.status = QLabel("Select a region and UTC hour range.")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.status)
        self.catalog = QTableWidget(0, 4)
        self._catalog_entries = []
        self.catalog.setHorizontalHeaderLabels(
            ["UTC coverage", "Area N/W/S/E", "Variables", "Artifact"]
        )
        self.catalog.horizontalHeader().setStretchLastSection(True)
        self.catalog.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.catalog.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.catalog.itemSelectionChanged.connect(self._history_selected)
        layout.addWidget(self.catalog)
        samples = QHBoxLayout()
        samples.addWidget(QLabel("Selected history sample UTC"))
        self.sample_start = self._date(now)
        self.sample_end = self._date(now)
        self.sample_start.setObjectName("historySampleStart")
        self.sample_end.setObjectName("historySampleEnd")
        samples.addWidget(self.sample_start)
        samples.addWidget(QLabel("through"))
        samples.addWidget(self.sample_end)
        self.sample_lat = self._coord(90)
        self.sample_lon = self._coord(180)
        samples.addWidget(QLabel("Lat"))
        samples.addWidget(self.sample_lat)
        samples.addWidget(QLabel("Lon"))
        samples.addWidget(self.sample_lon)
        self.sample_button = QPushButton("Sample selected hour")
        self.sample_button.clicked.connect(self.sample_selected)
        samples.addWidget(self.sample_button)
        self.history_status = QLabel(
            "Select an imported history row to enable sampling."
        )
        samples.addWidget(self.history_status)
        layout.addLayout(samples)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.cancel_button.setEnabled(False)
        self.retry_button.setEnabled(False)
        self.refresh_catalog()

    @staticmethod
    def _coord(limit):
        spin = QDoubleSpinBox()
        spin.setRange(-limit, limit)
        spin.setDecimals(2)
        return spin

    @staticmethod
    def _date(value):
        edit = QDateTimeEdit()
        edit.setTimeZone(QTimeZone.utc())
        edit.setDateTime(
            QDateTime(
                QDate(value.year, value.month, value.day),
                QTime(value.hour, value.minute, value.second),
                Qt.TimeSpec.UTC,
            )
        )
        edit.setDisplayFormat("yyyy-MM-dd HH:00 'UTC'")
        edit.setCalendarPopup(True)
        return edit

    def use_visible_region(self):
        if self.capture_region is None:
            self.status.setText("Visible-region capture is unavailable.")
            return
        try:
            area = self.capture_region()
            north, west, south, east = area
            for control, value in zip(
                (self.north, self.west, self.south, self.east), area
            ):
                control.setValue(value)
            self.status.setText(
                f"Using visible bounds N/W/S/E: {north:.2f}, {west:.2f}, {south:.2f}, {east:.2f}"
            )
        except Exception as exc:
            self.status.setText(f"Could not capture visible region: {exc}")

    def _make_plan(self):
        def as_utc(edit):
            return edit.dateTime().toPython().replace(tzinfo=timezone.utc)

        return plan_request(
            as_utc(self.start),
            as_utc(self.end),
            (
                self.north.value(),
                self.west.value(),
                self.south.value(),
                self.east.value(),
            ),
        )

    def submit(self):
        if self.job is not None and self.job.is_alive():
            return
        try:
            self.plan = self._make_plan()
            self.service.errors.pop(
                (self.plan.start, self.plan.end, self.plan.area), None
            )
            self.job = self.service.submit(self.plan)
            self.status.setText("ERA5 request running in background…")
            self.request_button.setEnabled(False)
            self.cancel_button.setEnabled(True)
            self.retry_button.setEnabled(False)
        except (ValueError, Era5Error, AttributeError) as exc:
            self.status.setText(str(exc))

    def cancel(self):
        if self.plan is not None:
            self.service.cancel(self.plan)
            self.status.setText(
                "Cancellation requested; current CDS transfer may finish before stopping."
            )
            self.cancel_button.setEnabled(False)

    def retry(self):
        if self.plan is None or (self.job is not None and self.job.is_alive()):
            return
        self.service.errors.pop((self.plan.start, self.plan.end, self.plan.area), None)
        self.job = self.service.submit(self.plan, retry=True)
        self.status.setText("Retrying ERA5 request in background…")
        self.request_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.retry_button.setEnabled(False)

    def poll(self):
        if self.job is not None and not self.job.is_alive():
            key = (self.plan.start, self.plan.end, self.plan.area)
            error = self.service.errors.pop(key, None)
            self.status.setText(error or "ERA5 historical request complete.")
            self.job = None
            self.request_button.setEnabled(True)
            self.cancel_button.setEnabled(False)
            self.retry_button.setEnabled(bool(error))
            self.refresh_catalog()

    def refresh_catalog(self):
        path = self.service.store.root / "era5-history.json"
        try:
            entries = (
                json.loads(path.read_text(encoding="utf8")) if path.exists() else []
            )
        except (OSError, ValueError):
            entries = []
        self.catalog.setRowCount(len(entries))
        self._catalog_entries = entries
        for row, entry in enumerate(entries):
            provenance = entry.get("provenance", {})
            coverage = f"{entry.get('start', '')} — {entry.get('end', '')} UTC"
            source = entry.get("source", "CDS ERA5 hourly single levels reanalysis")
            self.catalog.setItem(
                row,
                0,
                QTableWidgetItem(
                    f"{source}; {coverage}; {provenance.get('reader', 'unverified')}"
                ),
            )
            self.catalog.setItem(row, 1, QTableWidgetItem(str(entry.get("area", ""))))
            self.catalog.setItem(
                row, 2, QTableWidgetItem(", ".join(entry.get("variables", [])))
            )
            self.catalog.setItem(
                row, 3, QTableWidgetItem(str(entry.get("artifact", "")))
            )

    def _history_selected(self):
        row = self.catalog.currentRow()
        if row < 0:
            self.history_status.setText(
                "Select an imported history row to enable sampling."
            )
            return
        artifact = self.catalog.item(row, 3)
        self.history_status.setText(
            f"Selected history artifact {artifact.text() if artifact else ''}; choose any inclusive UTC sample-hour range."
        )

    def sample_selected(self):
        row = self.catalog.currentRow()
        if row < 0:
            self.history_status.setText(
                "Select an imported history row before sampling."
            )
            return
        artifact = self.catalog.item(row, 3)
        if artifact is None:
            self.history_status.setText("Selected history row has no artifact.")
            return
        from gweatherrouting.core.grib import Grib

        grib = None
        try:
            path = self.service.store.root / artifact.text()
            grib = Grib.parse(str(path))
            start = self.sample_start.dateTime().toPython().replace(tzinfo=timezone.utc)
            end = self.sample_end.dateTime().toPython().replace(tzinfo=timezone.utc)
            if start != end:
                raise ValueError("Sample one explicitly selected UTC hour at a time.")
            entry = self._catalog_entries[row]
            coverage_start = datetime.fromisoformat(
                entry["start"].replace("Z", "+00:00")
            )
            coverage_end = datetime.fromisoformat(entry["end"].replace("Z", "+00:00"))
            if coverage_start.tzinfo is None:
                coverage_start = coverage_start.replace(tzinfo=timezone.utc)
            if coverage_end.tzinfo is None:
                coverage_end = coverage_end.replace(tzinfo=timezone.utc)
            if not coverage_start <= start <= coverage_end:
                raise ValueError(
                    "Selected hour is outside this history artifact's UTC coverage."
                )
            lat, lon = self.sample_lat.value(), self.sample_lon.value()
            sampler = grib.sampler()
            wind = sampler.sample("wind", start, lat, lon)
            pressure = sampler.sample("pressure", start, lat, lon)
            if wind is None or pressure is None:
                raise ValueError("No wind and pressure data at this hour/location.")
            if wind.shape != (1, 2):
                raise ValueError("Wind sample did not contain u and v components.")
            if pressure.shape != (1, 1):
                raise ValueError("Pressure sample did not contain one value.")
            u, v = float(wind[0, 0]), float(wind[0, 1])
            pressure = float(pressure[0, 0])
            if not all(map(math.isfinite, (u, v, pressure))):
                raise ValueError("Sample returned a non-finite wind or pressure value.")
            self.history_status.setText(
                f"{start:%Y-%m-%d %H:00 UTC} at {lat:.2f}, {lon:.2f}: "
                f"wind u={u:.2f}, v={v:.2f} m/s; pressure={pressure:.1f} hPa"
            )
        except Exception as exc:
            self.history_status.setText(f"Sampling failed: {exc}")
        finally:
            if grib is not None:
                grib.close()
