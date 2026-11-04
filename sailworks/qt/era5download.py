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
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sailworks.core.era5 import (
    Era5DownloadService,
    Era5Error,
    plan_request,
    publish_history_display,
)
from sailworks.core.timecontrol import utc_to_wall


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
        self.intro = QLabel(
            "ERA5 reanalysis history (UTC). Requests are limited to 31 days and "
            "a bounded area; historical imports never activate a forecast."
        )
        layout.addWidget(self.intro)
        self.instructions = QLabel(
            "CDS setup: create ~/.cdsapirc with "
            "url: https://cds.climate.copernicus.eu/api and "
            "key: <personal-access-token>, then accept the ERA5 dataset terms "
            "at https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels."
        )
        self.instructions.setWordWrap(True)
        layout.addWidget(self.instructions)
        self.north, self.west = self._coord(90), self._coord(180)
        self.south, self.east = self._coord(90), self._coord(180)
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
        self.region_row = row
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
        self.date_row = row
        row = QHBoxLayout()
        self.request_button = QPushButton("Start download")
        self.cancel_button = QPushButton("Cancel")
        self.retry_button = QPushButton("Retry")
        self.request_button.clicked.connect(self.submit)
        self.cancel_button.clicked.connect(self.cancel)
        self.retry_button.clicked.connect(self.retry)
        self.show_button = QPushButton("Show on map")
        self.show_button.clicked.connect(self._publish_history_display)
        row.addWidget(self.request_button)
        row.addWidget(self.show_button)
        row.addWidget(self.cancel_button)
        row.addWidget(self.retry_button)
        layout.addLayout(row)
        self.status = QLabel("Select a region and UTC hour range.")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress.setTextVisible(True)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.catalog = QTableWidget(0, 5)
        self._rows = []
        self.catalog.setHorizontalHeaderLabels(
            ["UTC coverage", "Area N/W/S/E", "Variables", "Status", "Artifact"]
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
        self.samples_row = samples
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self._resume_was_active = False
        resume = getattr(self.service, "resume_pending", None)
        if callable(resume):
            resume()
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

    def apply_race_request(self, region, start, end, *, auto_submit=True):
        """Use the selected race's bounds and UTC window, hiding manual inputs."""
        if region is not None:
            south, west, north, east = region
            for control, value in zip(
                (self.north, self.west, self.south, self.east),
                (north, west, south, east),
            ):
                control.setValue(float(value))
        for edit, value, round_up in (
            (self.start, start, False),
            (self.end, end, True),
        ):
            if value is None:
                continue
            whole = value.replace(minute=0, second=0, microsecond=0)
            if round_up and whole != value:
                whole += timedelta(hours=1)
            edit.setDateTime(
                QDateTime(
                    QDate(whole.year, whole.month, whole.day),
                    QTime(whole.hour, whole.minute, whole.second),
                    Qt.TimeSpec.UTC,
                )
            )

        def hide_layout(layout):
            for index in range(layout.count()):
                widget = layout.itemAt(index).widget()
                if widget is not None:
                    widget.setVisible(False)

        self.intro.setVisible(False)
        self.instructions.setVisible(False)
        hide_layout(self.region_row)
        hide_layout(self.date_row)
        hide_layout(self.samples_row)
        self.status.setText("Using the selected race's region and UTC time range.")
        if auto_submit:
            self.submit()

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
            self.progress.setRange(0, 0)
            self.progress.setFormat("Contacting CDS…")
            self.progress.show()
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
        self.progress.setRange(0, 0)
        self.progress.setFormat("Contacting CDS…")
        self.progress.show()
        self.status.setText("Retrying ERA5 request in background…")
        self.request_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.retry_button.setEnabled(False)

    def poll(self):
        resume_active = getattr(self.service, "resume_active", None)
        if self.job is None:
            if callable(resume_active) and resume_active():
                self._resume_was_active = True
                self.progress.setRange(0, 0)
                self.progress.setFormat("Resuming ERA5 request…")
                self.progress.show()
                self.status.setText("Resuming queued ERA5 download…")
            elif getattr(self, "_resume_was_active", False):
                self._resume_was_active = False
                self.progress.hide()
                self.status.setText("ERA5 history updated.")
                self.refresh_catalog()
                self._publish_history_display()
            return
        plan = self.plan
        progress_for = getattr(self.service, "progress_for", None)
        if self.job.is_alive():
            info = progress_for(plan) if plan is not None and progress_for else None
            if info is not None:
                total = int(info.get("total") or 0)
                done = int(info.get("done") or 0)
                if total:
                    self.progress.setRange(0, total)
                    self.progress.setValue(min(done, total))
                    self.progress.setFormat(f"{done}/{total} chunks (%p%)")
                else:
                    self.progress.setRange(0, 0)
                stage = info.get("stage")
                if stage:
                    self.status.setText(stage)
                self.progress.show()
            return
        if plan is None:
            self.job = None
            return
        key = (plan.start, plan.end, plan.area)
        error = self.service.errors.pop(key, None)
        info = progress_for(plan) if progress_for else None
        stage = (info or {}).get("stage")
        if error:
            self.status.setText(error)
        elif stage == "Already available":
            self.status.setText("ERA5 history already available.")
        else:
            self.status.setText("ERA5 historical request complete.")
        self.job = None
        if error:
            self.progress.hide()
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self.progress.setFormat("Complete")
            self.progress.show()
        self.request_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.retry_button.setEnabled(bool(error))
        self.refresh_catalog()
        if not error:
            self._publish_history_display()

    def _publish_history_display(self):
        path = self.service.store.root / "era5-history.json"
        try:
            entries = (
                json.loads(path.read_text(encoding="utf8")) if path.exists() else []
            )
        except (OSError, ValueError):
            entries = []
        analysis = getattr(self.controller, "analysis", None)
        state = getattr(analysis, "state", None)
        if state is None or not entries:
            return False
        try:
            tranche = publish_history_display(state, entries)
            # Jump the clock into the freshly published coverage so the wind
            # layer is visible without scrubbing.
            first_valid = tranche.entries[0].run_time
            set_time = getattr(self.controller, "time_control", None)
            if set_time is not None:
                set_time.set_time(utc_to_wall(first_valid))
            owner = self.parent()
            while owner is not None:
                fit_race = getattr(owner, "fit_race", None)
                if callable(fit_race):
                    fit_race()
                    break
                owner = owner.parent()
            refresh = getattr(analysis, "refresh", None)
            if callable(refresh):
                refresh()
            self.history_status.setText("Published ERA5 history as a display forecast.")
            return True
        except Exception as exc:
            self.history_status.setText(f"Could not display ERA5 history: {exc}")
            return False

    _VARIABLE_LABELS = {
        "10m_u_component_of_wind": "u10",
        "10m_v_component_of_wind": "v10",
        "mean_sea_level_pressure": "msl",
    }

    @classmethod
    def _variables_label(cls, values):
        return ", ".join(
            cls._VARIABLE_LABELS.get(str(value), str(value)) for value in values
        )

    @staticmethod
    def _coverage_label(start, end):
        try:
            start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            return f"{start_dt:%Y-%m-%d %H:%M} → {end_dt:%Y-%m-%d %H:%M} UTC"
        except ValueError:
            return f"{start} → {end} UTC"

    @staticmethod
    def _area_label(area):
        try:
            north, west, south, east = (float(value) for value in area)
            return f"N {north:.2f} / W {west:.2f} / S {south:.2f} / E {east:.2f}"
        except (TypeError, ValueError):
            return str(area)

    @staticmethod
    def _queued_age(created):
        if not created:
            return ""
        try:
            then = datetime.fromisoformat(str(created))
        except ValueError:
            return ""
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        minutes = int((datetime.now(timezone.utc) - then).total_seconds() // 60)
        if minutes < 1:
            return "just now"
        if minutes < 60:
            return f"{minutes} min"
        hours, remaining = divmod(minutes, 60)
        return f"{hours} h {remaining} min" if remaining else f"{hours} h"

    def refresh_catalog(self):
        path = self.service.store.root / "era5-history.json"
        try:
            catalog = (
                json.loads(path.read_text(encoding="utf8")) if path.exists() else []
            )
        except (OSError, ValueError):
            catalog = []
        pending_for = getattr(self.service, "pending_requests", None)
        pending = pending_for() if callable(pending_for) else []
        rows = []
        catalog_keys = set()
        for entry in catalog:
            catalog_keys.add((str(entry.get("start")), str(entry.get("end"))))
            rows.append(
                {
                    "kind": "complete",
                    "entry": entry,
                    "start": entry.get("start"),
                    "end": entry.get("end"),
                    "area": entry.get("area", []),
                    "variables": entry.get("variables", []),
                    "artifact": str(entry.get("artifact", "")),
                }
            )
        for entry in pending:
            key = (str(entry.get("start")), str(entry.get("end")))
            if key in catalog_keys:
                continue
            rows.append(
                {
                    "kind": "queued",
                    "entry": None,
                    "start": entry.get("start"),
                    "end": entry.get("end"),
                    "area": entry.get("area", []),
                    "variables": list(self._VARIABLE_LABELS),
                    "artifact": "",
                    "request_id": str(entry.get("request_id", "")),
                    "created": str(entry.get("created", "")),
                }
            )
        rows.sort(key=lambda row: str(row.get("start") or ""))
        self._rows = rows
        self.catalog.blockSignals(True)
        self.catalog.setRowCount(len(rows))
        for index, row in enumerate(rows):
            if row["kind"] == "complete":
                status = "Downloaded"
                artifact = str(row["artifact"]).rsplit("/", 1)[-1][:12]
            else:
                status = "Queued at CDS"
                age = self._queued_age(row.get("created"))
                if age:
                    status = f"Queued at CDS · {age}"
                artifact = "—"
            values = (
                self._coverage_label(row["start"], row["end"]),
                self._area_label(row["area"]),
                self._variables_label(row["variables"]),
                status,
                artifact,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.catalog.setItem(index, column, item)
            if row["kind"] == "queued":
                self.catalog.item(index, 3).setToolTip(
                    f"CDS request {row.get('request_id', '')}"
                )
            else:
                self.catalog.item(index, 4).setToolTip(str(row["artifact"]))
        self.catalog.blockSignals(False)
        downloaded = sum(1 for row in rows if row["kind"] == "complete")
        self.history_status.setText(
            f"{downloaded} downloaded, {len(rows) - downloaded} queued."
        )

    def _history_selected(self):
        row = self.catalog.currentRow()
        if row < 0 or row >= len(self._rows):
            self.history_status.setText(
                "Select an imported history row to enable sampling."
            )
            return
        data = self._rows[row]
        if data["kind"] == "queued":
            self.history_status.setText(
                f"Queued at CDS (request {data.get('request_id', '')}). "
                "Sampling is available after the download completes."
            )
            return
        self.history_status.setText(
            f"Selected {self._coverage_label(data['start'], data['end'])}; "
            "choose an inclusive UTC sample hour."
        )

    def sample_selected(self):
        row = self.catalog.currentRow()
        if row < 0 or row >= len(self._rows):
            self.history_status.setText(
                "Select an imported history row before sampling."
            )
            return
        data = self._rows[row]
        if data["kind"] != "complete":
            self.history_status.setText(
                "This chunk is still queued at CDS; sampling is not available yet."
            )
            return
        artifact = str(data["artifact"])
        from sailworks.core.grib import Grib

        grib = None
        try:
            path = self.service.store.root / artifact
            grib = Grib.parse(str(path))
            start = self._sample_hour(row)
            lat, lon = self.sample_lat.value(), self.sample_lon.value()
            sampler = grib.sampler()
            wind = sampler.sample("wind", start, lat, lon)
            pressure = sampler.sample("pressure", start, lat, lon)
            u, v, pressure = self._sample_values(wind, pressure)
            self.history_status.setText(
                f"{start:%Y-%m-%d %H:00 UTC} at {lat:.2f}, {lon:.2f}: "
                f"wind u={u:.2f}, v={v:.2f} m/s; pressure={pressure:.1f} hPa"
            )
        except Exception as exc:
            self.history_status.setText(f"Sampling failed: {exc}")
        finally:
            if grib is not None:
                grib.close()

    def _sample_hour(self, row):
        start = self.sample_start.dateTime().toPython().replace(tzinfo=timezone.utc)
        end = self.sample_end.dateTime().toPython().replace(tzinfo=timezone.utc)
        if start != end:
            raise ValueError("Sample one explicitly selected UTC hour at a time.")
        entry = self._rows[row].get("entry")
        if entry is None:
            raise ValueError("This chunk has not been downloaded yet")
        coverage_start = datetime.fromisoformat(entry["start"].replace("Z", "+00:00"))
        coverage_end = datetime.fromisoformat(entry["end"].replace("Z", "+00:00"))
        if coverage_start.tzinfo is None:
            coverage_start = coverage_start.replace(tzinfo=timezone.utc)
        if coverage_end.tzinfo is None:
            coverage_end = coverage_end.replace(tzinfo=timezone.utc)
        if not coverage_start <= start <= coverage_end:
            raise ValueError(
                "Selected hour is outside this history artifact's UTC coverage."
            )
        return start

    @staticmethod
    def _sample_values(wind, pressure):
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
        return u, v, pressure
