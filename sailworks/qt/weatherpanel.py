"""Weather overlay controls for the active chart or log map."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .dockchrome import style_scrollarea
from .marinepanel import BusyButton
from .theme import section_label
from sailworks.rendering.windpainter import BARB_DENSITIES
from sailworks.core.forecastcapabilities import MODEL_CAPABILITIES, display_model_name
from .text import qt_text


class ForecastModelSelector(QComboBox):
    """Choose which published, routing-eligible model the session uses."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setAccessibleName("Routing forecast model")
        self.setToolTip("Choose which downloaded weather model routing uses.")
        self.currentIndexChanged.connect(self._select)
        analysis = self._analysis()
        if analysis is not None:
            analysis.changed.connect(self._refresh)
        self._refresh()

    def _analysis(self):
        return getattr(self.controller, "analysis", None)

    def _refresh(self, *_):
        analysis = self._analysis()
        active = getattr(analysis, "active", None) if analysis is not None else None
        self.blockSignals(True)
        try:
            self.clear()
            if analysis is None or active is None:
                self.addItem("No published model selection", "")
                return
            choices = self._choices(analysis)
            self._targets = {}
            for label, model_id, target in choices:
                self.addItem(label, model_id)
                self._targets[model_id] = target
            index = self.findData(active.entry.model_id)
            if index < 0:
                index = self.findText(display_model_name(active.entry.model_id))
            if index >= 0:
                self.setCurrentIndex(index)
            elif choices:
                self.setCurrentIndex(0)
            else:
                self.addItem("No wind model available", "")
        finally:
            self.blockSignals(False)

    @staticmethod
    def _choices(analysis):
        """One entry per model, using its newest published run.

        The dropdown chooses a model, not a run: switching runs is the job of
        a refresh, while selecting here decides which model the session uses.
        """
        store = analysis.state.store
        manifests = getattr(store, "manifests", None)
        if manifests is None:
            active = getattr(analysis, "active", None)
            published = (
                [
                    (
                        active.tranche,
                        (
                            getattr(active.tranche, "refreshed_at", None),
                            0,
                        ),
                    )
                ]
                if active is not None
                else []
            )
        else:
            published = []
            for path in manifests.glob("*.json"):
                try:
                    tranche = store.load(path.stem)
                    recency = (tranche.refreshed_at, path.stat().st_mtime_ns)
                except Exception:
                    continue
                published.append((tranche, recency))
        newest = {}
        for tranche, recency in published:
            for entry in tranche.entries:
                if "wind" not in entry.fields:
                    continue
                capability = MODEL_CAPABILITIES.get(entry.model_id)
                if capability is not None and not capability.routing_eligible:
                    continue
                prior = newest.get(entry.model_id)
                if prior is None or recency > prior[0]:
                    # Newest published tranche holding the model, not merely
                    # its newest run: a refresh may republish the same run.
                    newest[entry.model_id] = (
                        recency,
                        (tranche.tranche_id, entry.entry_id),
                        entry.run_time,
                    )
        ordered = sorted(newest.items(), key=lambda item: item[1][2], reverse=True)
        return [
            (display_model_name(model_id), model_id, data)
            for model_id, (_recency, data, _run) in ordered
        ]

    def _select(self, index):
        analysis = self._analysis()
        if analysis is None or index < 0:
            return
        model_id = self.itemData(index)
        target = getattr(self, "_targets", {}).get(model_id)
        if target is None:
            return
        tranche_id, entry_id = target
        active = getattr(analysis, "active", None)
        if active is not None and (
            active.tranche.tranche_id,
            active.entry.entry_id,
        ) == (tranche_id, entry_id):
            return
        activate = getattr(analysis, "activate", None)
        if callable(activate):
            activate(tranche_id, entry_id)
        else:
            analysis.select_model(model_id)


class WeatherPanel(QWidget):
    def __init__(self, controller, active_map, manage_forecasts):
        super().__init__()
        self.controller = controller
        self.active_map = active_map
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = style_scrollarea(QScrollArea())
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        controls = QVBoxLayout(content)
        controls.setContentsMargins(12, 8, 12, 8)
        controls.addWidget(self._section_divider())
        model_form = QFormLayout()
        model_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.forecast_model = ForecastModelSelector(controller)
        model_form.addRow("Model", self.forecast_model)
        controls.addLayout(model_form)
        controls.addWidget(section_label("Wind"))
        self.wind = QCheckBox("Show wind")
        self.wind.setChecked(active_map().wind_visible)
        self.wind.toggled.connect(
            lambda enabled: active_map().set_wind_visible(enabled)
        )
        controls.addWidget(self.wind)
        self.wind_style = QComboBox()
        self.wind_style.addItem("Barbs", "barbs")
        self.wind_style.addItem("Streamlines", "streamlines")
        self.wind_style.setToolTip(
            "Static forecast-flow curves with direction chevrons,"
            " or native-point wind barbs. No particle animation."
        )
        self.wind_style.setCurrentIndex(
            max(0, self.wind_style.findData(controller.settings.gribWindStyle))
        )
        self.wind_style.currentIndexChanged.connect(
            lambda _index: self.set_preference(
                "gribWindStyle", self.wind_style.currentData()
            )
        )
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.addRow("Style", self.wind_style)
        self.wind_gradient = self.checkbox("Gradient", "gribWindGradient")
        form.addRow(self.wind_gradient)
        self.wind_gradient_opacity = self.percent("gribWindGradientOpacity")
        form.addRow("Gradient opacity", self.wind_gradient_opacity)
        self.wind_gradient_style = QComboBox()
        self.wind_gradient_style.addItem("Smooth", True)
        self.wind_gradient_style.addItem("Hard transitions", False)
        self.wind_gradient_style.currentIndexChanged.connect(
            lambda _index: self.set_preference(
                "gribWindGradientSmooth", self.wind_gradient_style.currentData()
            )
        )
        form.addRow("Gradient style", self.wind_gradient_style)
        self.wind_gradient_levels = self.integer_slider("gribWindGradientLevels", 2, 15)
        form.addRow("Gradient levels", self.wind_gradient_levels)
        self.wind_opacity = self.percent("gribArrowOpacity")
        form.addRow("Wind opacity", self.wind_opacity)
        self.barb_density = QComboBox()
        for key, label, _spacing in BARB_DENSITIES:
            self.barb_density.addItem(label, key)
        self.barb_density.setCurrentIndex(self.density_index())
        self.refresh_density_labels()
        self.barb_density.currentIndexChanged.connect(
            lambda _index: self.set_preference(
                "gribBarbDensity", self.barb_density.currentData()
            )
        )
        form.addRow("Line density", self.barb_density)
        controls.addLayout(form)
        controls.addWidget(self._section_divider())
        self.waves_section = QWidget()
        waves = QVBoxLayout(self.waves_section)
        waves.setContentsMargins(0, 0, 0, 0)
        waves.addWidget(section_label("Waves"))
        self.wave = QCheckBox("Show wave height")
        self.wave.setChecked(active_map().wave_visible)
        self.wave.toggled.connect(self._toggle_wave)
        waves.addWidget(self.wave)
        # The wave display source belongs with the wave layers it switches,
        # not with the routing model selector: it never affects routing.
        self.wave_forecast_row = QWidget()
        wave_forecast_row = QHBoxLayout(self.wave_forecast_row)
        wave_forecast_row.setContentsMargins(0, 0, 0, 0)
        self.wave_label = QLabel("Wave forecast")
        self.wave_forecast = QComboBox()
        self.wave_forecast.setAccessibleName("Wave forecast display")
        self.wave_forecast.setToolTip(
            "Show a wave-only published forecast. Routing keeps its own model."
        )
        self.wave_forecast.currentIndexChanged.connect(self._select_wave)
        wave_forecast_row.addWidget(self.wave_label)
        wave_forecast_row.addWidget(self.wave_forecast, 1)
        waves.addWidget(self.wave_forecast_row)
        display = getattr(self.controller, "display_forecast", None)
        wave = getattr(display, "wave", None)
        if wave is not None:
            wave.changed.connect(self._refresh_wave)
        self._refresh_wave()
        wave_form = QFormLayout()
        wave_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        # Wave direction arrows reuse the wind barbs' thinning modes through
        # their own setting, so the two glyph layers thin independently and
        # toggling waves never restages the wind barbs.
        self.wave_density = QComboBox()
        for key, label, _spacing in BARB_DENSITIES:
            self.wave_density.addItem(label, key)
        initial_density = getattr(
            getattr(getattr(self, "controller", None), "settings", None),
            "gribWaveDensity",
            "standard",
        )
        self.wave_density.setCurrentIndex(
            max(0, self.wave_density.findData(initial_density))
        )
        self.wave_density.currentIndexChanged.connect(self._select_wave_density)
        wave_form.addRow("Arrow density", self.wave_density)
        self.wave_opacity = self.percent("gribWaveOpacity")
        wave_form.addRow("Wave opacity", self.wave_opacity)
        self.wave_style = QComboBox()
        self.wave_style.addItem("Smooth", True)
        self.wave_style.addItem("Hard transitions", False)
        self.wave_style.currentIndexChanged.connect(
            lambda _index: self.set_preference(
                "gribWaveSmooth", self.wave_style.currentData()
            )
        )
        wave_form.addRow("Gradient style", self.wave_style)
        self.wave_levels = self.integer_slider("gribWaveLevels", 2, 9)
        wave_form.addRow("Gradient levels", self.wave_levels)
        waves.addLayout(wave_form)
        controls.addWidget(self.waves_section)
        controls.addWidget(self._section_divider())
        controls.addWidget(section_label("Ship weather observations"))
        self.ship_observations = self.active_map().ship_observations
        self.ship_toggle = QCheckBox("Recent ship observations (NOAA ICOADS)")
        self.ship_toggle.setToolTip(
            "Show delayed ship weather reports. Report positions are not live vessel positions."
        )
        self.ship_toggle.setChecked(self.ship_observations.visible)
        self.ship_toggle.toggled.connect(self.ship_observations.set_visible)
        controls.addWidget(self.ship_toggle)
        self.ship_refresh = BusyButton("Refresh reports")
        self.ship_refresh.clicked.connect(self.ship_observations.refresh)
        controls.addWidget(self.ship_refresh)
        self.ship_observations.changed.connect(self._ship_observations_changed)
        controls.addWidget(self._section_divider())
        controls.addWidget(section_label("Isobars"))
        self.isobars = self.checkbox("Show isobars", "gribIsobarsVisible")
        controls.addWidget(self.isobars)
        self.isobar_options = QWidget()
        form = QFormLayout(self.isobar_options)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(1, 20)
        self.interval.setDecimals(1)
        self.interval.setSuffix(" hPa")
        self.interval.setValue(controller.settings.gribIsobarInterval)
        self.interval.valueChanged.connect(
            lambda value: self.set_preference("gribIsobarInterval", value)
        )
        form.addRow("Spacing", self.interval)
        self.isobar_opacity = self.percent("gribIsobarOpacity")
        form.addRow("Opacity", self.isobar_opacity)
        self.labels = self.checkbox("Pressure labels", "gribIsobarLabels")
        form.addRow(self.labels)
        controls.addWidget(self.isobar_options)
        self.isobar_options.setEnabled(self.isobars.isChecked())
        self.manage_forecasts = QPushButton("Manage / download forecasts…")
        self.manage_forecasts.clicked.connect(manage_forecasts)
        controls.addWidget(self.manage_forecasts)
        controls.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)
        self.refresh()

    def _stored_wave_source(self):
        settings = getattr(getattr(self, "controller", None), "settings", None)
        if settings is None:
            return ""
        try:
            return str(settings["gribWaveSource"] or "")
        except (KeyError, TypeError, AttributeError):
            return str(getattr(settings, "gribWaveSource", "") or "")

    def _store_wave_source(self, key):
        """Remember the display-only wave choice; never a routing selection."""
        settings = getattr(getattr(self, "controller", None), "settings", None)
        if settings is None:
            return
        try:
            settings["gribWaveSource"] = key
        except TypeError:
            settings.gribWaveSource = key
        except OSError:
            # A failed save must not break the panel refresh; the choice stays
            # active for this session.
            pass

    def _restore_wave_source(self, wave, key):
        choose = getattr(wave, "choose", None)
        if not callable(choose):
            return False
        if not choose(key):
            return False
        presenter = getattr(self.active_map(), "_weather_presenter", None)
        if presenter is not None:
            presenter.source_changed()
        return True

    def _refresh_wave(self):
        if getattr(self, "_choosing_wave", False):
            # Choosing only changes the selection, not the offered choices;
            # the combo already shows the chosen row. Rebuilding it from
            # inside its own currentIndexChanged signal confuses Qt's index.
            return
        display = getattr(self.controller, "display_forecast", None)
        wave = getattr(display, "wave", None)
        choices = tuple(getattr(wave, "choices", ()) or ())
        selected = getattr(wave, "selected_key", "")
        loading = bool(getattr(wave, "loading", False)) or bool(
            getattr(display, "loading", False)
        )
        if not selected and choices and not loading:
            stored = self._stored_wave_source()
            if stored:
                if self._restore_wave_source(wave, stored):
                    # The restoral emits changed, which re-enters this method
                    # with the selection already in place.
                    selected = stored
                else:
                    # The stored source is gone: fall back and forget it.
                    self._store_wave_source("")
        if not choices and not loading and getattr(wave, "scan_complete", False):
            # A completed scan that yields no wave choice means the stored
            # preference no longer exists. A rescan publishes an empty list
            # before its choices arrive, so it must not erase a valid choice.
            if self._stored_wave_source():
                self._store_wave_source("")
        self.wave_forecast.blockSignals(True)
        try:
            self.wave_forecast.clear()
            self.wave_forecast.addItem("Routing forecast", "")
            for choice in choices:
                label = getattr(choice, "short_label", None) or choice.label
                self.wave_forecast.addItem(label, choice.key)
            self.wave_forecast.setCurrentIndex(
                max(0, self.wave_forecast.findData(selected))
            )
        finally:
            self.wave_forecast.blockSignals(False)
        visible = bool(choices)
        self.wave_forecast_row.setVisible(visible)
        self.wave_label.setVisible(visible)
        self.wave_forecast.setVisible(visible)

    def _select_wave_density(self, _index):
        """Store the wave arrow density; the wind barb density is untouched."""
        settings = getattr(getattr(self, "controller", None), "settings", None)
        value = self.wave_density.currentData()
        if settings is None or value is None:
            return
        try:
            settings["gribWaveDensity"] = value
        except TypeError:
            settings.gribWaveDensity = value

    def _select_wave(self, index):
        if index < 0 or getattr(self, "_choosing_wave", False):
            return
        display = getattr(self.controller, "display_forecast", None)
        wave = getattr(display, "wave", None)
        choose = getattr(wave, "choose", None)
        key = self.wave_forecast.itemData(index) or ""
        self._choosing_wave = True
        try:
            chosen = callable(choose) and choose(key)
        finally:
            self._choosing_wave = False
        if not chosen:
            # Leave the combo on the source that is actually in effect.
            selected = getattr(wave, "selected_key", "")
            self.wave_forecast.blockSignals(True)
            try:
                self.wave_forecast.setCurrentIndex(
                    max(0, self.wave_forecast.findData(selected))
                )
            finally:
                self.wave_forecast.blockSignals(False)
            return
        self._store_wave_source(key)
        presenter = getattr(self.active_map(), "_weather_presenter", None)
        if presenter is not None:
            presenter.source_changed()

    def checkbox(self, label, key):
        widget = QCheckBox(qt_text(label))
        widget.setChecked(bool(self.controller.settings[key]))
        widget.toggled.connect(lambda value: self.set_preference(key, value))
        return widget

    def percent(self, key):
        control = QWidget()
        layout = QHBoxLayout(control)
        layout.setContentsMargins(0, 0, 0, 0)
        slider = QSlider()
        slider.setOrientation(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        accessible_name = {
            "gribArrowOpacity": "Wind opacity",
            "gribWindGradientOpacity": "Gradient opacity",
            "gribIsobarOpacity": "Isobar opacity",
        }.get(key, key)
        slider.setAccessibleName(accessible_name)
        slider.setToolTip("Set opacity from 0% to 100%")
        value_label = QLabel()
        value_label.setAccessibleName(f"{accessible_name} value")
        value_label.setMinimumWidth(42)
        value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        slider.setValue(round(self.controller.settings[key] * 100))
        value_label.setText(f"{slider.value()} %")
        slider.valueChanged.connect(lambda value: value_label.setText(f"{value} %"))
        slider.valueChanged.connect(lambda value: self.set_preference(key, value / 100))
        layout.addWidget(slider, 1)
        layout.addWidget(value_label)
        control.slider = slider
        return control

    def integer_slider(self, key, minimum, maximum):
        control = QWidget()
        layout = QHBoxLayout(control)
        layout.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setAccessibleName("Gradient levels")
        slider.setToolTip(f"Choose {minimum} to {maximum} gradient color levels")
        value_label = QLabel()
        value_label.setAccessibleName("Gradient levels value")
        value_label.setMinimumWidth(24)
        value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        slider.setValue(self.controller.settings[key])
        value_label.setText(str(slider.value()))
        slider.valueChanged.connect(lambda value: value_label.setText(str(value)))
        slider.valueChanged.connect(lambda value: self.set_preference(key, value))
        layout.addWidget(slider, 1)
        layout.addWidget(value_label)
        control.slider = slider
        return control

    def _toggle_wave(self, enabled):
        """Switch the wave layer and remember it across restarts."""
        self.active_map().set_wave_visible(enabled)
        self.set_preference("gribWaveVisible", bool(enabled))

    def set_preference(self, key, value):
        settings = self.controller.settings
        previous = settings[key]
        try:
            settings[key] = value
        except OSError as exc:
            settings.__dict__[key] = previous
            dict.__setitem__(settings, key, previous)
            QMessageBox.warning(self, "Unable to save setting", str(exc))
        self.refresh()
        presenter = getattr(self.active_map(), "_weather_presenter", None)
        if presenter is not None:
            presenter.request()
        self.controller.data.redraw()

    def density_index(self):
        index = self.barb_density.findData(self.controller.settings.gribBarbDensity)
        return index if index >= 0 else self.barb_density.findData("standard")

    def refresh_density_labels(self):
        streamlines = self.controller.settings.gribWindStyle == "streamlines"
        for index, (key, label, _spacing) in enumerate(BARB_DENSITIES):
            self.barb_density.setItemText(
                index, "Maximum" if streamlines and key == "all" else label
            )
        self.barb_density.setToolTip(
            "Choose streamline spacing. Denser settings add paths;"
            " Maximum remains capped for responsiveness."
            if streamlines
            else "Choose how many wind barbs to display."
            " All forecast points shows every visible sample without a"
            " symbol-count cap."
        )

    @staticmethod
    def _section_divider():
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _ship_observations_changed(self):
        self.ship_refresh.set_busy(self.ship_observations.loading)
        status = self.ship_observations.status
        if status:
            main_window = self.window()
            status_bar = getattr(main_window, "statusBar", None)
            if callable(status_bar):
                timeout = 0 if self.ship_observations.loading else 12_000
                status_bar().showMessage(status, timeout)
        self.active_map().queue_draw()

    def dispose(self):
        try:
            self.ship_observations.changed.disconnect(self._ship_observations_changed)
        except (RuntimeError, TypeError):
            pass

    def refresh(self):
        self.wind_style.blockSignals(True)
        self.wind_style.setCurrentIndex(
            max(0, self.wind_style.findData(self.controller.settings.gribWindStyle))
        )
        self.wind_style.blockSignals(False)
        self.wind_gradient_style.blockSignals(True)
        self.wind_gradient_style.setCurrentIndex(
            max(
                0,
                self.wind_gradient_style.findData(
                    self.controller.settings.gribWindGradientSmooth
                ),
            )
        )
        self.wind_gradient_style.blockSignals(False)
        self.wave_style.blockSignals(True)
        self.wave_style.setCurrentIndex(
            max(
                0,
                self.wave_style.findData(self.controller.settings.gribWaveSmooth),
            )
        )
        self.wave_style.blockSignals(False)
        self.barb_density.blockSignals(True)
        self.barb_density.setCurrentIndex(self.density_index())
        self.barb_density.blockSignals(False)
        self.refresh_density_labels()
        for widget, value in (
            (self.wind, self.active_map().wind_visible),
            (self.wind_gradient, self.controller.settings.gribWindGradient),
            (
                self.wind_gradient_opacity,
                round(self.controller.settings.gribWindGradientOpacity * 100),
            ),
            (
                self.wind_gradient_levels,
                self.controller.settings.gribWindGradientLevels,
            ),
            (self.wind_opacity, round(self.controller.settings.gribArrowOpacity * 100)),
            (self.wave, self.active_map().wave_visible),
            (self.wave_opacity, round(self.controller.settings.gribWaveOpacity * 100)),
            (self.wave_levels, self.controller.settings.gribWaveLevels),
            (self.isobars, self.controller.settings.gribIsobarsVisible),
            (self.interval, self.controller.settings.gribIsobarInterval),
            (
                self.isobar_opacity,
                round(self.controller.settings.gribIsobarOpacity * 100),
            ),
            (self.labels, self.controller.settings.gribIsobarLabels),
        ):
            widget.blockSignals(True)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif hasattr(widget, "slider"):
                widget.slider.setValue(value)
            else:
                widget.setValue(value)
            widget.blockSignals(False)
        self.wind_gradient_opacity.setEnabled(self.wind_gradient.isChecked())
        self.wind_gradient_style.setEnabled(self.wind_gradient.isChecked())
        self.wind_gradient_levels.setEnabled(
            self.wind_gradient.isChecked()
            and not self.controller.settings.gribWindGradientSmooth
        )
        self.isobar_options.setEnabled(self.isobars.isChecked())
