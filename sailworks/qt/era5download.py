"""User-directed, isolated ERA5 historical acquisition UI."""

from datetime import datetime, timezone
import json
import threading

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QGroupBox,
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
    build_history_display,
    plan_request,
    publish_history_tranche,
)
from sailworks.core.timecontrol import utc_to_wall
from .forecastdownload import ModelListRow

CDS_REQUESTS_URL = "https://cds.climate.copernicus.eu/requests"
CDS_LIVE_URL = "https://cds.climate.copernicus.eu/live"


class Era5DownloadTab(QWidget):
    """Download historical data without changing forecast or routing state."""

    displayReady = Signal(object, object)

    def __init__(self, controller, capture_region=None, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.capture_region = capture_region
        self.service = Era5DownloadService(controller.analysis.state.store)
        self.displayReady.connect(self._finish_history_display)

        self.plan = None
        self.job = None
        layout = QVBoxLayout(self)
        columns = QHBoxLayout()
        models_box = QGroupBox("Models")
        models_layout = QVBoxLayout(models_box)
        header = QHBoxLayout()
        include_heading = QLabel("Include")
        include_heading.setFixedWidth(58)
        header.addWidget(include_heading)
        header.addWidget(QLabel("Model"), 1)
        resolution_heading = QLabel("Resolution")
        resolution_heading.setFixedWidth(88)
        header.addWidget(resolution_heading)
        coverage_heading = QLabel("Coverage")
        coverage_heading.setFixedWidth(72)
        header.addWidget(coverage_heading)
        models_layout.addLayout(header)
        self.model_checks = {}
        for model_id, name, resolution, coverage in (
            ("ERA5", "ERA5 reanalysis", "0.25°", "hourly"),
            ("NBS", "Satellite-blended NBS", "0.25°", "6-hourly"),
        ):
            row = ModelListRow(model_id, name, resolution, coverage, True)
            row.download_check.toggled.connect(self._models_changed)
            models_layout.addWidget(row)
            self.model_checks[model_id] = row.download_check
        models_layout.addStretch(1)
        columns.addWidget(models_box, 1)
        self.models_box = models_box

        parameters_box = QGroupBox("Parameters")
        parameters_layout = QVBoxLayout(parameters_box)
        self.field_wind = QCheckBox("Wind at 10 m")
        self.field_wind.setChecked(True)
        self.field_pressure = QCheckBox("Sea-level pressure")
        self.field_pressure.setChecked(True)
        parameters_layout.addWidget(self.field_wind)
        parameters_layout.addWidget(self.field_pressure)
        self.field_wave_height = QCheckBox("Significant wave height")
        self.field_wave_period = QCheckBox("Wave period")
        self.field_wave_direction = QCheckBox("Wave direction")
        for check in (
            self.field_wave_height,
            self.field_wave_period,
            self.field_wave_direction,
        ):
            parameters_layout.addWidget(check)
        parameters_layout.addStretch(1)
        columns.addWidget(parameters_box, 1)
        self.parameters_box = parameters_box
        self._range = None
        columns.addStretch(1)
        layout.addLayout(columns)

        self.north, self.west = self._coord(90), self._coord(180)
        self.south, self.east = self._coord(90), self._coord(180)
        self.region_row = QHBoxLayout()
        self.region_button = QPushButton("Update region from current map")
        self.region_button.clicked.connect(self.use_visible_region)
        self.region_row.addWidget(self.region_button)
        self.region_status = QLabel("Using saved region")
        self.region_status.setProperty("secondary", True)
        self.region_row.addWidget(self.region_status, 1)
        layout.addLayout(self.region_row)

        self.request_button = QPushButton("Download selected history")
        self.request_button.clicked.connect(self.start_download)
        layout.addWidget(self.request_button)

        actions = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel")
        self.retry_button = QPushButton("Retry")
        for button in (self.cancel_button, self.retry_button):
            button.setMinimumWidth(120)
        self.cancel_button.clicked.connect(self.cancel)
        self.retry_button.clicked.connect(self.retry)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.retry_button)
        layout.addLayout(actions)
        self.actions_row = actions

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
        layout.addWidget(self.catalog)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.poll)
        self._poll_count = 0
        self.nbs_job = None
        self.nbs_cancel = None
        self.nbs_progress = None
        self.nbs_error = None
        self.nbs_tranche = None
        self._resume_was_active = False
        resume = getattr(self.service, "resume_pending", None)
        if callable(resume):
            self._resume_was_active = bool(resume())
        self.cancel_button.setEnabled(False)
        self.retry_button.setEnabled(False)
        self._models_changed()
        self.refresh_catalog()

    @staticmethod
    def _coord(limit):
        spin = QDoubleSpinBox()
        spin.setRange(-limit, limit)
        spin.setDecimals(2)
        return spin

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
            self.region_status.setText(f"Using visible region · {self._region_label()}")
        except Exception as exc:
            self.status.setText(f"Could not capture visible region: {exc}")

    def _region_label(self):
        return (
            f"N {self.north.value():.2f} / W {self.west.value():.2f} / "
            f"S {self.south.value():.2f} / E {self.east.value():.2f}"
        )

    def _area(self):
        return (
            self.north.value(),
            self.west.value(),
            self.south.value(),
            self.east.value(),
        )

    def _valid_area(self):
        north, west, south, east = self._area()
        return -90 <= south < north <= 90 and -180 <= west < east <= 180

    def _ensure_area(self):
        """Fill an unset region from the map before validating a download."""
        if self._valid_area():
            return True
        if self.capture_region is not None:
            self.use_visible_region()
            if self._valid_area():
                return True
        self.status.setText(
            "Set the download region first: click 'Update region from current map'."
        )
        return False

    def apply_race_request(self, region, start, end, *, auto_submit=True):
        """Use the selected race's bounds and UTC window."""
        if region is not None:
            south, west, north, east = region
            for control, value in zip(
                (self.north, self.west, self.south, self.east),
                (north, west, south, east),
            ):
                control.setValue(float(value))
            self.region_status.setText(f"Using race region · {self._region_label()}")
        self._range = (start, end) if start is not None and end is not None else None
        self.region_row.itemAt(0).widget().setVisible(False)
        self.status.setText("Using the selected race's region and UTC time range.")
        if auto_submit:
            self.start_download()

    def _make_plan(self, fields=None):
        if self._range is None:
            raise ValueError("Select a race to choose the download period.")
        start, end = self._range
        return plan_request(
            start,
            end,
            self._area(),
            fields=fields or self._selected_fields(),
        )

    def start_download(self):
        """Download every checked model with the checked parameters."""
        if self._range is None:
            self.status.setText("Select a race to choose the download period.")
            return
        models = self._selected_models()
        if not models:
            self.status.setText("Select at least one model to download.")
            return
        if "NBS" in models and not self.field_wind.isChecked():
            self.status.setText(
                "Wind at 10 m is required for a usable historical field."
            )
            return
        if "ERA5" in models:
            self.submit(fields=self._selected_fields())
        if "NBS" in models:
            self.submit_nbs()

    def _selected_models(self):
        return [
            model_id
            for model_id, check in self.model_checks.items()
            if check.isChecked()
        ]

    def _selected_fields(self):
        fields = []
        if self.field_wind.isChecked():
            fields.append("wind")
        if self.field_pressure.isChecked() and self.model_checks["ERA5"].isChecked():
            fields.append("pressure")
        if self.model_checks["ERA5"].isChecked():
            for field, check in (
                ("wave_height", self.field_wave_height),
                ("wave_period", self.field_wave_period),
                ("wave_direction", self.field_wave_direction),
            ):
                if check.isChecked():
                    fields.append(field)
        return tuple(fields) or ("wind",)

    def _models_changed(self, *_):
        era5 = self.model_checks["ERA5"].isChecked()
        nbs = self.model_checks["NBS"].isChecked()
        self.field_pressure.setEnabled(era5)
        for check in (
            self.field_wave_height,
            self.field_wave_period,
            self.field_wave_direction,
        ):
            check.setEnabled(era5)
        if not era5 and not nbs:
            self.status.setText("Select at least one model to download.")
        elif nbs and not era5:
            self.status.setText(
                "Satellite-blended NBS wind: NOAA CoastWatch, no CDS queue."
            )
        else:
            self.status.setText("Historical weather (UTC).")

    def submit(self, fields=None):
        if self.job is not None and self.job.is_alive():
            return
        if not self._ensure_area():
            return
        try:
            self.plan = self._make_plan(fields)
            self.service.clear_error(self.service.key_for(self.plan))
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
        if self.nbs_cancel is not None:
            self.nbs_cancel.set()
            self.status.setText("Cancelling the satellite wind download…")
            self.cancel_button.setEnabled(False)
            return
        if self.plan is not None:
            self.service.cancel(self.plan)
            self.status.setText(
                "Cancellation requested; current CDS transfer may finish before stopping."
            )
            self.cancel_button.setEnabled(False)

    def submit_nbs(self):
        from sailworks.core import nbs

        if self.nbs_job is not None and self.nbs_job.is_alive():
            return
        if not self._ensure_area():
            return
        if self._range is None:
            self.status.setText("Select a race to choose the download period.")
            return
        start, end = self._range
        try:
            plan = nbs.plan_request(
                start,
                end,
                (
                    self.north.value(),
                    self.west.value(),
                    self.south.value(),
                    self.east.value(),
                ),
            )
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        analysis = getattr(self.controller, "analysis", None)
        state = getattr(analysis, "state", None)
        if state is None:
            self.status.setText("Historical data is not available yet.")
            return
        self.nbs_progress = {"percent": 0, "stage": "Starting…"}
        self.nbs_error = None
        self.nbs_tranche = None
        self.nbs_cancel = threading.Event()
        self._nbs_status = {
            "start": plan.start.isoformat(),
            "end": plan.end.isoformat(),
            "area": list(plan.area),
            "status": "Downloading 0%",
        }
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress.show()
        self.status.setText("Downloading NOAA satellite-blended winds…")
        self.request_button.setEnabled(False)
        self.cancel_button.setEnabled(True)

        def report(percent, stage):
            self.nbs_progress = {"percent": percent, "stage": stage}
            if getattr(self, "_nbs_status", None) is not None:
                self._nbs_status["status"] = stage

        def work():
            try:
                self.nbs_tranche = nbs.build(
                    state, plan, progress=report, cancel=self.nbs_cancel
                )
            except Exception as exc:
                self.nbs_error = str(exc)

        self.nbs_job = threading.Thread(target=work, daemon=True)
        self.nbs_job.start()

    def _poll_nbs(self):
        if self.nbs_job.is_alive():
            info = self.nbs_progress or {}
            percent = int(info.get("percent", 0))
            self.progress.setValue(percent)
            stage = info.get("stage")
            if stage:
                self.status.setText(stage)
            if getattr(self, "_nbs_status", None) is not None:
                self._nbs_status["status"] = stage or f"Downloading {percent}%"
            self.refresh_catalog()
            return
        self.nbs_job = None
        self.cancel_button.setEnabled(False)
        self.request_button.setEnabled(True)
        if self.nbs_error:
            self.status.setText(f"Satellite wind download failed: {self.nbs_error}")
            self.progress.hide()
            if getattr(self, "_nbs_status", None) is not None:
                self._nbs_status["status"] = f"Failed: {self.nbs_error}"
            self.refresh_catalog()
            return
        if self.nbs_tranche is not None:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self.progress.setFormat("Complete")
            self.status.setText("NOAA satellite-blended winds published.")
            if getattr(self, "_nbs_status", None) is not None:
                self._nbs_status["status"] = "Published"
            self.refresh_catalog()
            self._show_tranche(self.nbs_tranche)

    def retry(self):
        if self.plan is None or (self.job is not None and self.job.is_alive()):
            return
        key = (
            self.job.era5_key
            if self.job is not None
            else self.service.key_for(self.plan)
        )
        self.service.clear_error(key)
        self.job = self.service.submit(self.plan, retry=True)
        self.progress.setRange(0, 0)
        self.progress.setFormat("Contacting CDS…")
        self.progress.show()
        self.status.setText("Retrying ERA5 request in background…")
        self.request_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.retry_button.setEnabled(False)

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()

    def poll(self):
        if getattr(self, "nbs_job", None) is not None:
            self._poll_nbs()
            return
        resume_active = getattr(self.service, "resume_active", None)
        running = (self.job is not None and self.job.is_alive()) or (
            self.job is None and callable(resume_active) and resume_active()
        )
        self._poll_count += 1
        # Show chunks the moment they are submitted to CDS, not only when the
        # whole plan finishes.
        if running and self._poll_count % 4 == 0:
            self.refresh_catalog()
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
                completed_keys = getattr(
                    self.service, "completed_resume_keys", lambda: []
                )()
                errors = [self.service.clear_error(key) for key in completed_keys]
                errors = [error for error in errors if error]
                if errors:
                    self.status.setText("ERA5 resume failed: " + "; ".join(errors))
                    self.refresh_catalog()
                    return
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
        error = self.service.clear_error(self.job.era5_key)
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
        if state is None:
            self.status.setText("Historical data is not available in this workspace.")
            return False
        if not entries:
            self.status.setText(
                "No downloaded ERA5 history to show yet; download it first."
            )
            return False
        self.status.setText("Building ERA5 history display…")

        def build():
            try:
                tranche = build_history_display(state, entries)
                error = None
            except Exception as exc:
                tranche, error = None, exc
            self.displayReady.emit(tranche, error)

        threading.Thread(target=build, daemon=True, name="era5-display").start()
        return True

    def _finish_history_display(self, tranche, error):
        if tranche is None:
            self.status.setText(f"Could not display ERA5 history: {error}")
            return
        analysis = getattr(self.controller, "analysis", None)
        state = getattr(analysis, "state", None)
        if state is None:
            self.status.setText("Historical data is not available in this workspace.")
            return
        try:
            publish_history_tranche(state, tranche)
        except Exception as exc:
            self.status.setText(f"Could not display ERA5 history: {exc}")
            return
        self._show_tranche(tranche)
        self.status.setText("Published ERA5 history as a display forecast.")

    def _show_tranche(self, tranche):
        """Jump to a freshly published tranche and refresh the map."""
        analysis = getattr(self.controller, "analysis", None)
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

    def _set_status_link(self, index, status, row):
        link = QLabel(
            f'<a href="{CDS_REQUESTS_URL}" style="text-decoration:none;">'
            f"{status} ↗</a>"
        )
        link.setOpenExternalLinks(True)
        link.setToolTip(
            "Open your requests on the CDS website "
            f"(request {row.get('request_id', '')})"
        )
        link.setContentsMargins(6, 0, 6, 0)
        link.setStyleSheet("QLabel { background: transparent; }")
        self.catalog.setCellWidget(index, 3, link)

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
        nbs_status = getattr(self, "_nbs_status", None)
        if nbs_status:
            rows.append(
                {
                    "kind": "nbs",
                    "entry": None,
                    "start": nbs_status.get("start"),
                    "end": nbs_status.get("end"),
                    "area": nbs_status.get("area", []),
                    "variables": ["u10", "v10"],
                    "artifact": "",
                    "status": nbs_status.get("status", ""),
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
            elif row["kind"] == "nbs":
                status = str(row.get("status", "Satellite winds"))
                artifact = "—"
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
                if row["kind"] == "queued" and column == 3:
                    # The status is a link to the CDS request list.
                    continue
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.catalog.setItem(index, column, item)
            if row["kind"] == "queued":
                self._set_status_link(index, status, row)
            else:
                self.catalog.item(index, 4).setToolTip(str(row["artifact"]))
        self.catalog.blockSignals(False)
