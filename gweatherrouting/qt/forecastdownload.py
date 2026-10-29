"""Custom GRIB selections, with provider limits visible before downloading."""

from datetime import datetime, timezone
from threading import Event, Thread

from PySide6.QtCore import QTimer, Qt, Signal, Slot
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
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
    (
        "NOAA GFS-Wave",
        "GFS-Wave",
        "0.25°",
        "16 days",
        "Global wave fields",
        "GFSWAVE",
    ),
)


class ModelListRow(QWidget):
    selected = Signal(str)

    def __init__(self, model_id, name, resolution, forecast, checked, parent=None):
        super().__init__(parent)
        self.model_id = model_id
        self.setObjectName("forecastModelRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(8)

        include = QWidget(self)
        include.setFixedWidth(58)
        include_layout = QHBoxLayout(include)
        include_layout.setContentsMargins(0, 0, 0, 0)
        include_layout.addStretch(1)
        self.download_check = QCheckBox(include)
        self.download_check.setChecked(checked)
        self.download_check.setAccessibleName(f"Include {name} in download")
        include_layout.addWidget(self.download_check)
        include_layout.addStretch(1)

        self.model_label = QLabel(name, self)
        self.resolution_label = QLabel(resolution, self)
        self.forecast_label = QLabel(forecast, self)
        self.resolution_label.setFixedWidth(88)
        self.forecast_label.setFixedWidth(72)
        for label in (self.model_label, self.resolution_label, self.forecast_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout.addWidget(include)
        layout.addWidget(self.model_label, 1)
        layout.addWidget(self.resolution_label)
        layout.addWidget(self.forecast_label)
        self.set_selected(False)

    def set_selected(self, selected):
        self.setProperty("selected", bool(selected))
        background = (
            self.palette().color(QPalette.ColorRole.Highlight).name()
            if selected
            else "transparent"
        )
        foreground = (
            self.palette().color(QPalette.ColorRole.HighlightedText).name()
            if selected
            else self.palette().color(QPalette.ColorRole.WindowText).name()
        )
        self.setStyleSheet(
            "QWidget#forecastModelRow {"
            f"background-color: {background}; border-radius: 3px;"
            "}"
        )
        for label in (self.model_label, self.resolution_label, self.forecast_label):
            label.setStyleSheet(f"background: transparent; color: {foreground};")

    def mousePressEvent(self, event):  # noqa: N802 - Qt virtual method name
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.model_id)
            event.accept()
            return
        super().mousePressEvent(event)


class ForecastDownloadOptions(QWidget):
    changed = Signal()

    def __init__(self, sources, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        layout = QHBoxLayout()
        root.addLayout(layout)
        self._model_id_by_name = {
            capability.provider_name: model_id
            for model_id, capability in MODEL_CAPABILITIES.items()
            if capability.provider_name in sources
        }
        self._fields_by_model = {
            model_id: {
                field: bool(MODEL_CAPABILITIES[model_id].available_steps(None, field))
                and field
                in ("wind", "pressure", "wave_height", "wave_period", "wave_direction")
                for field in (
                    "wind",
                    "pressure",
                    "gust",
                    "wave_height",
                    "wave_period",
                    "wave_direction",
                )
            }
            for model_id in self._model_id_by_name.values()
        }
        model_box = QGroupBox("Models")
        models = QVBoxLayout(model_box)
        models.setSpacing(2)
        header = QHBoxLayout()
        include_heading = QLabel("Include")
        include_heading.setFixedWidth(58)
        resolution_heading = QLabel("Resolution")
        resolution_heading.setFixedWidth(88)
        forecast_heading = QLabel("Forecast")
        forecast_heading.setFixedWidth(72)
        header.addWidget(include_heading)
        header.addWidget(QLabel("Model"), 1)
        header.addWidget(resolution_heading)
        header.addWidget(forecast_heading)
        models.addLayout(header)
        self.models = {}
        self.model_rows = {}
        self.model_rows_by_id = {}
        self._parameter_model_id = None
        for record in MODELS:
            name, label, resolution, forecast, region = record[:5]
            if name not in sources:
                continue
            model_id = self._model_id_by_name[name]
            row = ModelListRow(
                model_id,
                label,
                resolution,
                forecast,
                checked=name == "NOAA GFS",
            )
            row.download_check.setToolTip(region)
            self.models[name] = row.download_check
            self.model_rows[name] = row
            self.model_rows_by_id[model_id] = row
            if self._parameter_model_id is None:
                self._parameter_model_id = model_id
            row.selected.connect(self._parameter_model_changed)
            row.download_check.toggled.connect(self.rebuild_hours)
            models.addWidget(row)
        models.addStretch(1)
        layout.addWidget(model_box, 3)
        parameter_box = QGroupBox("Parameters")
        parameters = QVBoxLayout(parameter_box)
        self.fields = {}
        self._field_labels = (
            ("wind", "Wind at 10 m"),
            ("pressure", "Sea-level pressure"),
            ("gust", "Wind gust"),
            ("wave_height", "Significant wave height"),
            ("wave_period", "Primary wave period"),
            ("wave_direction", "Primary wave direction"),
        )
        for name, label in self._field_labels:
            check = QCheckBox(label)
            check.setChecked(name != "gust")
            if name == "gust":
                check.setToolTip(
                    "Surface GUST forecast field. Available only for models "
                    "that provide it."
                )
            check.toggled.connect(self._parameter_fields_changed)
            self.fields[name] = check
            parameters.addWidget(check)
        self._parameter_model_changed(self._parameter_model_id)
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

    def _fields_for_model(self, model_id):
        selected = self._fields_by_model.get(model_id, {})
        return tuple(field for field in self.fields if selected.get(field, False))

    def _parameter_model_changed(self, model_id):
        if model_id not in self._fields_by_model:
            self.fields["gust"].hide()
            return
        self._parameter_model_id = model_id
        for selected_model, row in self.model_rows_by_id.items():
            row.set_selected(selected_model == model_id)
        capability = MODEL_CAPABILITIES[model_id]
        for field, check in self.fields.items():
            check.setVisible(bool(capability.available_steps(None, field)))
        values = self._fields_by_model[model_id]
        for field, check in self.fields.items():
            previous = check.blockSignals(True)
            check.setChecked(values.get(field, False))
            check.blockSignals(previous)

    def _parameter_fields_changed(self, *_args):
        model_id = self._parameter_model_id
        if model_id not in self._fields_by_model:
            return
        self._fields_by_model[model_id] = {
            field: check.isChecked() for field, check in self.fields.items()
        }
        self.rebuild_hours()

    def available(self):
        hours: set[int] = set()
        for name in self.names():
            model_id = self._model_id_by_name.get(name)
            if model_id is not None:
                capability = MODEL_CAPABILITIES[model_id]
                for field in self._fields_for_model(model_id):
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
        self.active_model = QComboBox()
        self.active_model.setAccessibleName("Routing forecast model")
        self.active_model.setToolTip(
            "Choose which downloaded weather model routing uses."
        )
        self.size_estimate = QLabel("Download estimate pending.")
        self.size_estimate.setWordWrap(True)
        self.download_progress = QProgressBar()
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(0)
        self.download_progress.setFormat("%p%")
        self.download_progress.hide()
        self.refresh_button = QPushButton("Download and use selected")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.hide()
        extra = QVBoxLayout()
        region_row = QHBoxLayout()
        region_row.addWidget(self.region_button)
        region_row.addWidget(self.region_status, 1)
        extra.addLayout(region_row)
        extra.addWidget(self.active_model)
        extra.addWidget(self.download_progress)
        actions = QHBoxLayout()
        for button in (self.refresh_button, self.cancel_button):
            actions.addWidget(button)
        extra.addLayout(actions)
        extra.addWidget(self.size_estimate)
        self.layout().addLayout(extra)

        self.region_button.clicked.connect(self._capture_region)
        self.refresh_button.clicked.connect(self.refresh_models)
        self.cancel_button.clicked.connect(self.cancel_refresh)
        self.active_model.currentIndexChanged.connect(self._select_model)
        self.changed.connect(self._save_intent)
        for check in self.models.values():
            check.toggled.connect(self._sync_interval)
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
            fields = self._fields_for_model(model_id)
            if not fields:
                raise ValueError(
                    f"Select at least one parameter for {capability.provider_name}"
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
        if not any(
            MODEL_CAPABILITIES[item.model_id].routing_eligible for item in result
        ):
            raise ValueError(
                "Include a wind forecast model; GFS-Wave supplies wave fields only."
            )
        return tuple(result)

    def _save_intent(self, *_args):
        if self._loading:
            return
        if self.region is None:
            self._update_refresh_availability()
            return
        try:
            configurations = self._configurations()
            current = self.analysis.configuration if self.analysis is not None else None
            routing_models = tuple(
                item.model_id
                for item in configurations
                if MODEL_CAPABILITIES[item.model_id].routing_eligible
            )
            if not routing_models:
                raise ValueError(
                    "Include a wind forecast model; GFS-Wave supplies wave fields only."
                )
            refresh_model = (
                current.refresh_model_id
                if current is not None and current.refresh_model_id in routing_models
                else routing_models[0]
            )
            signature = (configurations, refresh_model)
            self.on_shown()
            if signature != self._saved_signature:
                self.analysis.configure(configurations, refresh_model)
                self._saved_signature = signature
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
            self.region_status.setText("Using saved region")
            by_model = {item.model_id: item for item in saved.configurations}
            for model_id, capability in MODEL_CAPABILITIES.items():
                row = self.models.get(capability.provider_name)
                if row is None:
                    continue
                item = by_model.get(model_id)
                if item is not None:
                    self._fields_by_model[model_id] = {
                        field: field in item.fields for field in self.fields
                    }
                row.setChecked(item is not None)
                if item is not None:
                    index = self.duration.findData(
                        item.hours if hasattr(item, "hours") else item.forecast_hours
                    )
                    if index >= 0:
                        self.duration.setCurrentIndex(index)
                    index = self.interval.findData(item.interval)
                    if index >= 0:
                        self.interval.setCurrentIndex(index)
            parameter_model_id = (
                saved.refresh_model_id
                if saved.refresh_model_id in by_model
                else next(iter(by_model), None)
            )
            if parameter_model_id in self.model_rows_by_id:
                self._parameter_model_changed(parameter_model_id)
            self.rebuild_hours()
            self._saved_signature = (saved.configurations, saved.refresh_model_id)
        finally:
            self._loading = False
        self._update_active_status()
        self._update_refresh_availability()
        if not self.analysis.busy:
            self._schedule_size_estimate()

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
            self._set_size_estimate_error(error)
            return
        if self.analysis is None or self.analysis.busy:
            self.size_estimate.setText("Download estimate paused while busy.")
            return
        if self.analysis.error:
            return
        configuration = self.analysis.configuration
        if configuration is None:
            self.size_estimate.setText("Select models and a region for an estimate.")
            return
        self.size_estimate.setText("Checking run availability and download size…")
        self._estimate_timer.start()

    def _set_size_estimate_error(self, error):
        detail = str(error) or type(error).__name__
        if "Expected HTTP " in detail and ", got " in detail:
            status = detail.split(", got ", 1)[1].split(" ", 1)[0]
            summary = f"provider returned HTTP {status}"
        else:
            summary = detail.split(" for ", 1)[0]
            if len(summary) > 96:
                summary = summary[:93] + "…"
        self.size_estimate.setText(f"Estimate unavailable: {summary}.")
        self.size_estimate.setToolTip(detail)

    def _start_size_estimate(self):
        if self.analysis is None or self.analysis.busy:
            return
        configuration = self.analysis.configuration
        state = getattr(self.analysis, "state", None)
        coordinator = getattr(state, "coordinator", None)
        if configuration is None or coordinator is None:
            self.size_estimate.setText("Download estimate unavailable.")
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
            self._set_size_estimate_error(error)
            return
        total_bytes, model_sizes, reusable = result
        if not model_sizes:
            self.size_estimate.setText("Local forecasts already cover this request.")
            return
        amount = total_bytes / (1024 * 1024)
        display_size = (
            f"{amount:.1f} MB" if amount >= 1 else f"{total_bytes / 1024:.0f} KB"
        )
        approximate = any(item[2] for item in model_sizes)
        text = f"Download: {'~' if approximate else ''}{display_size}"
        if any(
            MODEL_CAPABILITIES[model_id].global_download
            for model_id, _, _ in model_sizes
        ):
            text += " · includes full global files"
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
            routable_entries = tuple(
                entry
                for entry in active.tranche.entries
                if MODEL_CAPABILITIES.get(entry.model_id)
                and MODEL_CAPABILITIES[entry.model_id].routing_eligible
            )
            for entry in routable_entries:
                self.active_model.addItem(entry.model_id, entry.model_id)
            selected_index = self.active_model.findData(active.entry.model_id)
            if selected_index >= 0:
                self.active_model.setCurrentIndex(selected_index)
            elif routable_entries:
                self.active_model.setCurrentIndex(0)
            else:
                self.active_model.addItem("No wind model available", "")
        self.active_model.blockSignals(False)
        busy = self.analysis.busy
        self.download_progress.setVisible(busy)
        self.download_progress.setValue(getattr(self.analysis, "progress", 0))
        if self.analysis.error:
            self.size_estimate.setText("Download estimate unavailable.")
            self.size_estimate.setToolTip(self.analysis.error)

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

    def closeEvent(self, event):
        self._estimate_timer.stop()
        if self._estimate_cancel is not None:
            self._estimate_cancel.set()
        super().closeEvent(event)
