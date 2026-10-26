"""Custom GRIB selections, with provider limits visible before downloading."""

from datetime import datetime, timezone
from threading import Event, Thread

from PySide6.QtCore import QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QGridLayout,
    QVBoxLayout,
    QLabel,
    QCheckBox,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
    QPushButton,
    QGroupBox,
    QProgressBar,
)

from gweatherrouting.core.gribsources.options import DownloadOptions
from gweatherrouting.core.forecastcapabilities import MODEL_CAPABILITIES
from gweatherrouting.core.forecasttranche import ModelConfiguration

MODELS = (
    ("NOAA GFS", "GFS", "0.25°", "10 days", "Global", "GFS"),
    ("NOAA HRRR", "HRRR", "3 km", "18 / 48 h", "CONUS", "HRRR"),
    ("CMC HRDPS", "HRDPS", "2.5 km", "48 h", "Canada / northern US", "HRDPS"),
    (
        "ECMWF IFS",
        "IFS",
        "0.25° grid",
        "6 / 15 days",
        "Global field messages",
        "IFS",
    ),
    (
        "ECMWF AIFS Single",
        "AIFS",
        "0.25° grid",
        "15 days",
        "Global field messages",
        "AIFS",
    ),
    (
        "NOAA AI-GFS",
        "AI-GFS",
        "0.25° grid",
        "16 days",
        "Global field messages",
        "AIGFS",
    ),
)


class ForecastDownloadOptions(QWidget):
    changed = Signal()

    def __init__(self, sources, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        layout = QHBoxLayout()
        root.addLayout(layout)
        model_box = QGroupBox("Models")
        models = QGridLayout(model_box)
        models.setHorizontalSpacing(18)
        for column, heading in enumerate(("Model", "Resolution", "Forecast")):
            models.addWidget(QLabel(heading), 0, column)
        self.models = {}
        self.model_rows = {}
        model_row = 1
        for record in MODELS:
            name, label, resolution, forecast, region = record[:5]
            if name not in sources:
                continue
            check = QCheckBox(label)
            check.setChecked(name == "NOAA GFS")
            check.setToolTip(region)
            self.models[name] = check
            models.addWidget(check, model_row, 0)
            resolution_label, forecast_label = QLabel(resolution), QLabel(forecast)
            models.addWidget(resolution_label, model_row, 1)
            models.addWidget(forecast_label, model_row, 2)
            self.model_rows[name] = (check, resolution_label, forecast_label)
            check.toggled.connect(self.rebuild_hours)
            model_row += 1
        models.setRowStretch(model_row, 1)
        layout.addWidget(model_box, 3)
        parameter_box = QGroupBox("Parameters")
        parameters = QVBoxLayout(parameter_box)
        self.fields = {}
        for name, label in (
            ("wind", "Wind at 10 m"),
            ("pressure", "Sea-level pressure"),
        ):
            check = QCheckBox(label)
            check.setChecked(True)
            check.toggled.connect(self.rebuild_hours)
            self.fields[name] = check
            parameters.addWidget(check)
        parameters.addStretch()
        layout.addWidget(parameter_box, 2)
        time_box = QGroupBox("Forecast hours")
        times = QVBoxLayout(time_box)
        row = QHBoxLayout()
        row.addWidget(QLabel("Every"))
        self.interval = QComboBox()
        for label, value in (
            ("Model default", 0),
            ("1 hour", 1),
            ("3 hours", 3),
            ("6 hours", 6),
            ("12 hours", 12),
        ):
            self.interval.addItem(label, value)
        self.interval.setCurrentIndex(2)
        row.addWidget(self.interval)
        row.addWidget(QLabel("for"))
        self.duration = QComboBox()
        for label, value in (
            ("12 hours", 12),
            ("1 day", 24),
            ("2 days", 48),
            ("3 days", 72),
            ("5 days", 120),
            ("7 days", 168),
            ("10 days", 240),
            ("15 days", 360),
            ("16 days", 384),
        ):
            self.duration.addItem(label, value)
        self.duration.setCurrentIndex(4)
        row.addWidget(self.duration)
        times.addLayout(row)
        self.hours = QTableWidget(0, 3)
        self.hours.horizontalHeader().hide()
        self.hours.verticalHeader().hide()
        self.hours.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.hours.horizontalHeader().setStretchLastSection(True)
        for column in range(3):
            self.hours.setColumnWidth(column, 65)
        self.hours.setMinimumHeight(140)
        self.hours.setMaximumHeight(190)
        self.hours.setToolTip(
            "Hours after the model run. Only checked hours are requested; "
            "each model uses the supported subset."
        )
        times.addWidget(self.hours)
        row = QHBoxLayout()
        for label, callback in (
            ("Clear", lambda: self.check_hours(False)),
            ("All", lambda: self.check_hours(True)),
            ("Defaults", self.defaults),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        times.addLayout(row)
        layout.addWidget(time_box, 3)
        self.interval.currentIndexChanged.connect(self.rebuild_hours)
        self.duration.currentIndexChanged.connect(self.rebuild_hours)
        self.hours.itemChanged.connect(lambda *_args: self.changed.emit())
        self.rebuild_hours()

    def names(self):
        return tuple(name for name, check in self.models.items() if check.isChecked())

    def available(self):
        hours: set[int] = set()
        fields = tuple(name for name, check in self.fields.items() if check.isChecked())
        for name in self.names():
            model_id = next(
                (
                    model_id
                    for model_id, capability in MODEL_CAPABILITIES.items()
                    if capability.provider_name == name
                ),
                None,
            )
            if model_id is not None:
                capability = MODEL_CAPABILITIES[model_id]
                for field in fields:
                    hours.update(
                        hour
                        for hour in capability.available_steps(None, field)
                        if hour <= self.duration.currentData()
                    )
        return sorted(hours)

    def rebuild_hours(self, *_):
        options = DownloadOptions(
            self.duration.currentData(), self.interval.currentData()
        )
        values = options.available_steps(self.available())
        self.hours.blockSignals(True)
        self.hours.clearContents()
        self.hours.setRowCount((len(values) + 2) // 3)
        for index, hour in enumerate(values):
            item = QTableWidgetItem(str(hour))
            item.setData(Qt.ItemDataRole.UserRole, hour)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.hours.setItem(index // 3, index % 3, item)
        self.hours.blockSignals(False)
        self.changed.emit()

    def check_hours(self, checked):
        self.hours.blockSignals(True)
        for row in range(self.hours.rowCount()):
            for column in range(3):
                item = self.hours.item(row, column)
                if item is not None:
                    item.setCheckState(
                        Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                    )
        self.hours.blockSignals(False)
        self.changed.emit()

    def defaults(self):
        self.interval.setCurrentIndex(2)
        self.rebuild_hours()

    def options(self):
        if not self.names():
            raise ValueError("Select at least one model")
        steps = tuple(
            item.data(Qt.ItemDataRole.UserRole)
            for row in range(self.hours.rowCount())
            for column in range(3)
            if (item := self.hours.item(row, column)) is not None
            and item.checkState() == Qt.CheckState.Checked
        )
        if not steps:
            raise ValueError("Select at least one forecast hour")
        return DownloadOptions(
            self.duration.currentData(),
            self.interval.currentData(),
            tuple(name for name, check in self.fields.items() if check.isChecked()),
            steps,
        )


class AnalysisForecastDownloadOptions(ForecastDownloadOptions):
    """The existing Forecasts model/parameter/hour UI backed by tranche state."""

    _estimate_result = Signal(int, object)

    def __init__(self, controller, capture_region, parent=None):
        sources = [
            capability.provider_name for capability in MODEL_CAPABILITIES.values()
        ]
        super().__init__(sources, parent)
        self.analysis_session = controller
        self.capture_region = capture_region
        self.region = None
        self.analysis = None
        self._loading = False
        self._saved_signature = None
        self._estimate_generation = 0
        self._estimate_cancel = None
        self._estimate_timer = QTimer(self)
        self._estimate_timer.setSingleShot(True)
        self._estimate_timer.setInterval(600)
        self._estimate_timer.timeout.connect(self._start_size_estimate)
        self._estimate_result.connect(self._finish_size_estimate)
        self.region_button = QPushButton("Capture current map region")
        self.region_button.setToolTip(
            "Use the visible chart/log map bounds for this forecast request. "
            "A saved region is reused until you capture a new one."
        )
        self.region_status = QLabel("Region not set")
        self.region_status.setWordWrap(True)
        self.active_model = QComboBox()
        self.analysis_status = QLabel("Select models, then capture a shared region.")
        self.analysis_status.setWordWrap(True)
        self.model_selection_hint = QLabel()
        self.model_selection_hint.setWordWrap(True)
        self.refresh_hint = QLabel()
        self.refresh_hint.setWordWrap(True)
        self.size_estimate = QLabel("Download size will be estimated after selection.")
        self.size_estimate.setWordWrap(True)
        self.download_progress = QProgressBar()
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(0)
        self.download_progress.setFormat("%p%")
        self.download_progress.hide()
        self.global_notice = QLabel()
        self.global_notice.setWordWrap(True)
        self.global_notice.setOpenExternalLinks(True)
        self.refresh_button = QPushButton("Download selected models and use")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.hide()
        extra = QVBoxLayout()
        extra.addWidget(
            QLabel(
                "1. Choose models, parameters, hours. 2. Capture map region. "
                "3. Download and use."
            )
        )
        region_row = QHBoxLayout()
        region_row.addWidget(self.region_button)
        region_row.addWidget(self.region_status, 1)
        extra.addLayout(region_row)
        extra.addWidget(self.global_notice)
        extra.addWidget(QLabel("Currently published model"))
        extra.addWidget(self.active_model)
        extra.addWidget(self.model_selection_hint)
        extra.addWidget(self.analysis_status)
        extra.addWidget(self.download_progress)
        actions = QHBoxLayout()
        for button in (self.refresh_button, self.cancel_button):
            actions.addWidget(button)
        extra.addLayout(actions)
        extra.addWidget(self.refresh_hint)
        extra.addWidget(self.size_estimate)
        self.layout().addLayout(extra)

        self.region_button.clicked.connect(self._capture_region)
        self.refresh_button.clicked.connect(self.refresh_models)
        self.cancel_button.clicked.connect(self.cancel_refresh)
        self.active_model.currentIndexChanged.connect(self._select_model)
        self.changed.connect(self._save_intent)
        for check in self.models.values():
            check.toggled.connect(self._sync_interval)
        self._update_global_notice()
        self._update_active_status()
        self._update_refresh_availability()
        self._schedule_size_estimate()

    def on_shown(self):
        if self.analysis is None:
            self.analysis = self.analysis_session.prepare_model_analysis()
            self.analysis.changed.connect(self._analysis_changed)
            self._load_intent()
        self._update_active_status()
        self._update_refresh_availability()
        self._schedule_size_estimate()

    def _capture_region(self):
        self.on_shown()
        try:
            from gweatherrouting.core.forecasttranche import ModelConfiguration

            region = tuple(self.capture_region())
            ModelConfiguration("GFS", ("wind",), region, "native", 0, (), 0)
        except Exception as exc:
            self.region_status.setText(f"Region not captured: {exc}")
            return False
        self.region = region
        self.region_button.setText("Update region from current map")
        self.region_status.setText("Using map bounds")
        self._save_intent()
        return self._saved_signature is not None

    def _sync_interval(self, *_args):
        capabilities = [
            capability
            for capability in MODEL_CAPABILITIES.values()
            if capability.provider_name in self.names()
        ]
        if not capabilities:
            return
        supported = set(capabilities[0].intervals)
        for capability in capabilities[1:]:
            supported.intersection_update(capability.intervals)
        values = tuple(value for value in (0, 1, 3, 6, 12) if value in supported)
        if not values:
            return
        current = self.interval.currentData()
        existing = tuple(
            self.interval.itemData(index) for index in range(self.interval.count())
        )
        if current in values and existing == values:
            return
        self.interval.blockSignals(True)
        self.interval.clear()
        for value in values:
            label = (
                "Model default"
                if value == 0
                else f"{value} hour" + ("" if value == 1 else "s")
            )
            self.interval.addItem(label, value)
        chosen = current if current in values else (3 if 3 in values else values[0])
        self.interval.setCurrentIndex(self.interval.findData(chosen))
        self.interval.blockSignals(False)
        self.rebuild_hours()

    def _configurations(self):
        if self.region is None:
            raise ValueError("Capture a shared map region before refreshing")
        selected = self.names()
        if not selected:
            raise ValueError("Select at least one model")
        fields = tuple(name for name, check in self.fields.items() if check.isChecked())
        if not fields:
            raise ValueError("Select wind, pressure, or both")
        hours = self.duration.currentData()
        interval = self.interval.currentData()
        requested = tuple(
            item.data(Qt.ItemDataRole.UserRole)
            for row in range(self.hours.rowCount())
            for column in range(3)
            if (item := self.hours.item(row, column)) is not None
            and item.checkState() == Qt.CheckState.Checked
        )
        if not requested:
            raise ValueError("Select at least one forecast hour")
        result = []
        for provider_name in selected:
            model_id, capability = next(
                (model_id, capability)
                for model_id, capability in MODEL_CAPABILITIES.items()
                if capability.provider_name == provider_name
            )
            if interval not in capability.intervals:
                raise ValueError(
                    f"{capability.provider_name} does not support this interval"
                )
            model_hours = min(hours, capability.max_horizon())
            available = {
                step
                for field in fields
                for step in capability.available_steps(None, field)
                if step <= model_hours
            }
            steps = tuple(step for step in requested if step in available)
            if not steps:
                raise ValueError(
                    f"{capability.provider_name} has no selected forecast times"
                )
            result.append(
                ModelConfiguration(
                    model_id,
                    fields,
                    self.region,
                    "native",
                    model_hours,
                    steps,
                    interval,
                )
            )
        return tuple(result)

    def _save_intent(self, *_args):
        self._update_global_notice()
        if self._loading:
            return
        if self.region is None:
            self._update_refresh_availability()
            return
        try:
            configurations = self._configurations()
            current = self.analysis.configuration if self.analysis is not None else None
            ids = {item.model_id for item in configurations}
            refresh_model = (
                current.refresh_model_id
                if current is not None and current.refresh_model_id in ids
                else configurations[0].model_id
            )
            signature = (configurations, refresh_model)
            self.on_shown()
            if signature != self._saved_signature:
                self.analysis.configure(configurations, refresh_model)
                self._saved_signature = signature
            self.analysis_status.setText(
                "Model intent saved; published selection unchanged"
            )
            self._update_refresh_availability()
            self._schedule_size_estimate()
        except (TypeError, ValueError) as exc:
            self._update_refresh_availability(str(exc))
            self._schedule_size_estimate(str(exc))
        except Exception as exc:
            self._update_refresh_availability(f"Unable to save model intent: {exc}")
            self._schedule_size_estimate(str(exc))

    def _load_intent(self):
        saved = self.analysis.configuration
        if saved is None:
            self._saved_signature = None
            return
        regions = {tuple(item.region) for item in saved.configurations}
        if len(regions) != 1:
            self.region_status.setText(
                "Saved model regions differ; capture one shared region"
            )
            return
        self._loading = True
        try:
            self.region = next(iter(regions))
            self.region_button.setText("Update region from current map")
            self.region_status.setText(
                "Using saved region; moving the map does not change it"
            )
            by_model = {item.model_id: item for item in saved.configurations}
            for model_id, capability in MODEL_CAPABILITIES.items():
                row = self.models.get(capability.provider_name)
                if row is None:
                    continue
                item = by_model.get(model_id)
                row.setChecked(item is not None)
                if item is not None:
                    for field, check in self.fields.items():
                        check.setChecked(field in item.fields)
                    index = self.duration.findData(
                        item.hours if hasattr(item, "hours") else item.forecast_hours
                    )
                    if index >= 0:
                        self.duration.setCurrentIndex(index)
                    index = self.interval.findData(item.interval)
                    if index >= 0:
                        self.interval.setCurrentIndex(index)
            self.rebuild_hours()
            self._saved_signature = (saved.configurations, saved.refresh_model_id)
        finally:
            self._loading = False
        self._update_active_status()
        self._update_refresh_availability()
        if not self.analysis.busy:
            self._schedule_size_estimate()

    def _update_global_notice(self):
        capabilities = [
            capability
            for capability in MODEL_CAPABILITIES.values()
            if capability.provider_name in self.names() and capability.global_download
        ]
        if not capabilities:
            self.global_notice.clear()
            return
        sources = []
        for capability in capabilities:
            links = [f'<a href="{capability.data_url}">public source</a>']
            if capability.license_url:
                links.append(f'<a href="{capability.license_url}">CC BY 4.0</a>')
            sources.append(capability.attribution + " (" + ", ".join(links) + ")")
        self.global_notice.setText(
            "Global field messages are downloaded for "
            + ", ".join(sources)
            + ". The shared region is checked for coverage but does not filter network downloads."
        )

    def refresh_models(self):
        self._save_intent()
        if self.analysis is None or not self._saved_signature:
            return False
        return self.analysis.refresh()

    def cancel_refresh(self):
        return bool(self.analysis and self.analysis.cancel())

    def _select_model(self, index):
        if self.analysis is None or index < 0:
            return
        model_id = self.active_model.itemData(index)
        if model_id:
            self.analysis.select_model(model_id)

    def _analysis_changed(self):
        self._update_active_status()
        self._update_refresh_availability()
        self._schedule_size_estimate()

    def _schedule_size_estimate(self, error=""):
        self._estimate_generation += 1
        self._estimate_timer.stop()
        if self._estimate_cancel is not None:
            self._estimate_cancel.set()
            self._estimate_cancel = None
        if error:
            self.size_estimate.setText(f"Download size unavailable: {error}")
            return
        if self.analysis is None or self.analysis.busy:
            self.size_estimate.setText(
                "Download size unavailable while a forecast operation is running."
            )
            return
        configuration = self.analysis.configuration
        if configuration is None:
            self.size_estimate.setText(
                "Select models and capture a region to estimate download size."
            )
            return
        self.size_estimate.setText(
            "Checking latest runs and local data for download-size estimate…"
        )
        self._estimate_timer.start()

    def _start_size_estimate(self):
        if self.analysis is None or self.analysis.busy:
            return
        configuration = self.analysis.configuration
        state = getattr(self.analysis, "state", None)
        coordinator = getattr(state, "coordinator", None)
        if configuration is None or coordinator is None:
            self.size_estimate.setText("Download-size estimate unavailable.")
            return
        generation = self._estimate_generation
        cancel = Event()
        self._estimate_cancel = cancel
        configurations = configuration.configurations
        selected_model_id = configuration.refresh_model_id

        def estimate():
            try:
                result = coordinator.estimate_download(
                    configurations, selected_model_id, cancel=cancel
                )
                payload = (result, "")
            except Exception as exc:
                payload = (None, str(exc) or type(exc).__name__)
            try:
                self._estimate_result.emit(generation, payload)
            except RuntimeError:
                # The Forecasts dialog may close while catalogues are being read.
                pass

        Thread(target=estimate, name="forecast-size-estimate", daemon=True).start()

    @Slot(int, object)
    def _finish_size_estimate(self, generation, payload):
        if generation != self._estimate_generation:
            return
        result, error = payload
        if error:
            self.size_estimate.setText(f"Download-size estimate unavailable: {error}")
            return
        total_bytes, model_sizes, reusable = result
        if not model_sizes:
            reused = ", ".join(reusable)
            self.size_estimate.setText(
                "No download needed; reusing local data"
                + (f" for {reused}." if reused else ".")
            )
            return
        amount = total_bytes / (1024 * 1024)
        display_size = (
            f"{amount:.1f} MB" if amount >= 1 else f"{total_bytes / 1024:.0f} KB"
        )
        approximate = any(item[2] for item in model_sizes)
        text = f"Download: {'~' if approximate else ''}{display_size}"
        if reusable:
            text += f" · reusing {', '.join(reusable)}"
        self.size_estimate.setText(text)

    def _update_active_status(self):
        if self.analysis is None:
            self.on_shown()
            return
        active = self.analysis.active
        self.active_model.blockSignals(True)
        self.active_model.clear()
        if active is None:
            self.active_model.addItem("No published model selection", "")
        else:
            for entry in active.tranche.entries:
                self.active_model.addItem(entry.model_id, entry.model_id)
            self.active_model.setCurrentIndex(
                self.active_model.findData(active.entry.model_id)
            )
        self.active_model.blockSignals(False)
        busy = self.analysis.busy
        self.download_progress.setVisible(busy)
        self.download_progress.setValue(getattr(self.analysis, "progress", 0))
        if active is None:
            self.model_selection_hint.setText(
                "No published model yet. Choose models and click Download and use."
            )
            self.analysis_status.setText(self.analysis.error or self.analysis.status)
        else:
            model_ids = [entry.model_id for entry in active.tranche.entries]
            entry = active.entry
            if busy:
                self.model_selection_hint.setText(
                    f"{entry.model_id} active while new models download."
                )
                self.analysis_status.setText(
                    f"{self.analysis.status} · still showing {entry.model_id}"
                )
            elif self.analysis.error:
                self.model_selection_hint.setText(
                    f"Published: {entry.model_id}. Checked models not yet published."
                )
                detail = self.analysis.error.removeprefix(
                    "refresh preparation failed: "
                )
                for model_id in MODEL_CAPABILITIES:
                    prefix = f"{model_id} validation failed: "
                    if detail.startswith(prefix):
                        detail = f"{model_id}: {detail[len(prefix):]}"
                        break
                self.analysis_status.setText(
                    f"Download failed: {detail}. Still using {entry.model_id}."
                )
            elif len(model_ids) == 1:
                self.model_selection_hint.setText(
                    f"Published: {model_ids[0]}. Check another model above to publish it."
                )
            else:
                self.model_selection_hint.setText(
                    f"Published: {entry.model_id}. Select any above to switch."
                )
            age = max(
                0, (datetime.now(timezone.utc) - entry.run_time).total_seconds() / 3600
            )
            active_description = (
                f"{entry.model_id} run {entry.run_time:%Y-%m-%d %H:%M UTC} "
                f"({age:.1f} h old); downloaded "
                f"{entry.acquired_at:%Y-%m-%d %H:%M UTC}"
            )
            if not busy and not self.analysis.error:
                self.analysis_status.setText(active_description)

    def _update_refresh_availability(self, error=""):
        reason = error
        busy = self.analysis is not None and self.analysis.busy
        if not reason and busy:
            reason = "A forecast operation is in progress. Wait for it to finish or cancel it."
        if not reason and self.region is None:
            reason = "Capture a map region before downloading."
        if not reason:
            try:
                self._configurations()
            except (TypeError, ValueError) as exc:
                reason = str(exc)
        if not reason and self._saved_signature is None:
            reason = "Save a valid model selection before downloading."
        self.refresh_button.setEnabled(not reason)
        self.cancel_button.setVisible(busy)
        self.refresh_button.setToolTip(
            reason or "Download and publish the selected models."
        )
        if reason:
            self.refresh_hint.setText(reason)
        elif self.analysis is not None and self.analysis.error:
            self.refresh_hint.setText("Previous refresh failed; you can retry.")
        else:
            self.refresh_hint.setText("Ready to download the selected models.")

    def closeEvent(self, event):
        self._estimate_timer.stop()
        if self._estimate_cancel is not None:
            self._estimate_cancel.set()
        super().closeEvent(event)
