"""Qt data-library operations using the shared forecast and chart models."""

import os
import shutil
import tempfile
from datetime import datetime
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal, QTimer
from osgeo import gdal

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.forecastselection import (
    ForecastSelection,
    forecast_source,
)
from gweatherrouting.core.modeldownloads import newest_entries, install_download
from gweatherrouting.core.enccharts import ENCCatalog
from gweatherrouting.core.storage import DATA_DIR, GRIB_DIR
from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
from gweatherrouting.rendering.mbtilesrenderer import MBTilesChart
from gweatherrouting.rendering.gdalrasterchart import (
    GDALRasterChart,
    GDALSingleRasterChart,
)
from .backgroundtask import BackgroundTask
from gweatherrouting.rendering.chartlayer import ChartLayer


class ChartSettings(SimpleNamespace):
    """Worker-local palette callbacks; attach to live settings only on delivery."""

    def __init__(self, palette):
        super().__init__(chartPalette=palette, handlers=[])

    def register_on_change(self, key, callback):
        self.handlers.append((key, callback))
        callback(getattr(self, key))

    def attach(self, settings):
        for key, callback in self.handlers:
            settings.register_on_change(key, callback)


def read_forecast(path):
    path = Path(path).resolve()
    metadata = Grib.parse_metadata(str(path))
    if metadata.start_time is None or metadata.last_forecast is None:
        raise ValueError("This file contains no supported wind forecast")
    return Grib(
        str(path),
        metadata.name,
        metadata.centre,
        metadata.bounds,
        metadata.start_time,
        metadata.last_forecast,
        metadata.model_name,
    )


def import_forecast(path):
    """Validate before installing, retaining existing files with the same name."""
    path = Path(path).resolve()
    root = Path(GRIB_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if path.parent == root:
        return read_forecast(path)
    metadata = read_forecast(path)
    metadata.dataset = None
    destination = root / path.name
    if destination.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = root / f"{path.stem}-{stamp}{path.suffix}"
    # A partial import never appears in the local forecast library.
    with tempfile.NamedTemporaryFile(dir=root, suffix=".part", delete=False) as out:
        temporary = Path(out.name)
    try:
        shutil.copyfile(path, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return read_forecast(destination)


class DataController(QObject):
    changed = Signal()
    forecast_loaded = Signal(object)

    def __init__(self, session_controller):
        super().__init__(session_controller)
        self.owner = session_controller
        self.manager = session_controller.core.grib_manager
        for forecast in self.manager.gribs:
            forecast.path = str(Path(forecast.path).resolve())
        self.selection_scan_timer = QTimer(self)
        self.selection_scan_timer.setSingleShot(True)
        self.selection_scan_timer.timeout.connect(self._scan_selection)
        self.task = BackgroundTask(self)
        self.task.finished.connect(self._finish)
        self.task.changed.connect(self.changed.emit)
        self.catalog = ENCCatalog(Path(DATA_DIR) / "enc")
        self.remote_forecasts = []
        self.remote_charts = []
        self.local_forecasts = []
        self.operation = ""
        self.error = ""
        self.forecast_report = ""
        config = self.manager.storage.forecast_selection
        try:
            self.selection = (
                ForecastSelection.from_json(config)
                if config
                else ForecastSelection(
                    tuple(dict.fromkeys(forecast_source(g) for g in self.manager.gribs))
                    or ("NOAA GFS",)
                )
            )
        except (KeyError, TypeError, ValueError):
            self.selection = ForecastSelection()
        self.manager.gribs[:] = [
            g for g in self.manager.gribs if forecast_source(g) in self.selection.models
        ]

    def run(self, kind, message, operation):
        if (
            self.task.busy
            or self.task.closed
            or self.owner.routing.busy
            or self.owner.polars.task.busy
            or self.owner.map_data.task.busy
        ):
            return False
        self.operation, self.error = kind, ""
        if kind != "scan":
            self.forecast_report = ""
        return self.task.start(message, operation)

    def forecast_directory(self):
        return Path(GRIB_DIR).resolve()

    def scan_forecasts(self):
        selection = self.selection

        def scan(progress):
            files, errors, opened = {}, [], []
            for path in sorted(Path(GRIB_DIR).glob("*")):
                path = path.resolve()
                if path.suffix.lower() not in (".grib", ".grb", ".grib2", ".grb2"):
                    continue
                try:
                    if str(path) in files:
                        continue
                    metadata = Grib.parse_metadata(str(path))
                    if metadata.start_time is None:
                        raise ValueError("No wind forecast")
                    files[str(path)] = metadata
                    if forecast_source(metadata) in selection.models:
                        opened.append(read_forecast(path))
                except Exception as exc:
                    errors.append(f"{path.name}: {exc}")
            return files, errors, opened

        return self.run("scan", "Reading local forecasts…", scan)

    def import_forecast(self, path):
        return self.run(
            "forecast", "Importing forecast…", lambda _: import_forecast(path)
        )

    def set_selection(self, selection):
        if self.task.busy or self.owner.routing.busy or self.owner.polars.task.busy:
            return False
        previous = self.selection
        self._persist_selection(selection)
        self.manager.gribs[:] = [
            g for g in self.manager.gribs if forecast_source(g) in selection.models
        ]
        self.manager.store_opened_gribs()
        self.manager.storage.save()
        self.redraw()
        self.changed.emit()
        if previous.models != selection.models:
            self.selection_scan_timer.start(150)
        return True

    def _persist_selection(self, selection):
        previous = self.selection
        try:
            self.manager.storage.forecast_selection = selection.to_json()
        except Exception:
            dict.__setitem__(
                self.manager.storage, "forecast_selection", previous.to_json()
            )
            self.manager.storage.__dict__["forecast_selection"] = previous.to_json()
            raise
        self.selection = selection

    def _scan_selection(self):
        if not self.scan_forecasts() and not self.task.closed:
            self.selection_scan_timer.start(500)

    def set_forecast_enabled(self, metadata, enabled):
        # Legacy callers now select the model, never an individual file.
        name = forecast_source(metadata)
        models = tuple(m for m in self.selection.models if m != name)
        if enabled:
            models += (name,)
        return self.set_selection(replace(self.selection, models=models))

    def download_selected(self, bounds, models=None):
        selection = self.selection
        if models is not None:
            selection = replace(selection, models=tuple(models))
        selection.options()

        def download(progress):
            entries, issues = newest_entries(selection, bounds)
            forecasts = []
            if not entries and not issues:
                issues.append(
                    "The selected imported forecasts have no download provider"
                )
            for index, (source, row) in enumerate(entries):
                try:
                    forecasts.append(
                        install_download(
                            source,
                            row,
                            GRIB_DIR,
                            lambda percent: progress(
                                int((index * 100 + percent) / len(entries))
                            ),
                        )
                    )
                except Exception as exc:
                    issues.append(f"{source.name}: {exc}")
            return forecasts, issues

        return self.run("forecasts", "Downloading selected weather models…", download)

    def remove_forecast(self, metadata):
        if self.task.busy or self.owner.routing.busy or self.owner.polars.task.busy:
            return
        path = Path(metadata.path).resolve()
        if path.parent != Path(GRIB_DIR).resolve():
            raise ValueError("Only imported library copies can be removed")
        path.unlink()
        self.manager.gribs[:] = [g for g in self.manager.gribs if g.path != str(path)]
        self.manager.store_opened_gribs()
        self.manager.storage.save()
        self.redraw()
        self.local_forecasts = [g for g in self.local_forecasts if g.path != str(path)]
        self.changed.emit()

    def list_forecasts(self, source_name, bounds):
        registered = self.manager.get_source(source_name)
        if registered is None:
            raise ValueError("Unknown forecast source: " + source_name)
        source = type(registered)()
        if hasattr(source, "bounds"):
            source.bounds = bounds

        def listing(_):
            return [(source, row) for row in source.get_download_list()]

        return self.run("forecast-list", "Fetching available forecasts…", listing)

    def list_model_forecasts(self, names, bounds, options):
        sources = []
        for name in names:
            registered = self.manager.get_source(name)
            if registered is None:
                raise ValueError("Unknown forecast source: " + name)
            source = type(registered)()
            if hasattr(source, "bounds"):
                source.bounds = bounds
            sources.append(source)

        def listing(_):
            entries: list = []
            issues = []
            for source in sources:
                try:
                    rows = source.get_download_list(options)
                    entries.extend((source, row) for row in rows)
                    if not rows:
                        issues.append(
                            source.name + ": no runs support these forecast hours"
                        )
                except Exception as exc:
                    issues.append(f"{source.name}: {exc}")
            return entries, issues

        return self.run("forecast-model-list", "Finding model runs…", listing)

    def download_forecasts(self, entries):
        entries = tuple(entries)
        if not entries:
            return False

        def download(progress):
            forecasts, issues = [], []
            Path(GRIB_DIR).mkdir(parents=True, exist_ok=True)
            for index, (source, row) in enumerate(entries):
                try:
                    with tempfile.TemporaryDirectory(dir=GRIB_DIR) as directory:

                        def report(percent):
                            progress(int((index * 100 + percent) / len(entries)))

                        path = source.download(row[4], directory, report)
                        forecasts.append(import_forecast(path))
                except Exception as exc:
                    issues.append(f"{row[0]} ({row[3]}): {exc}")
                progress(int((index + 1) * 100 / len(entries)))
            return forecasts, issues

        return self.run(
            "forecasts", f"Downloading {len(entries)} model runs…", download
        )

    def download_forecast(self, entry):
        source, row = entry

        def download(progress):
            # Providers write into staging, so failures preserve installed data.
            with tempfile.TemporaryDirectory(dir=GRIB_DIR) as directory:
                path = source.download(row[4], directory, progress)
                return import_forecast(path)

        return self.run("forecast", "Downloading forecast…", download)

    def import_chart(self, path, kind):
        palette = self.owner.settings.chartPalette

        def prepare(progress):
            settings = ChartSettings(palette)
            chart: ChartLayer
            if kind == "vector":
                chart = GDALVectorChart(path, settings)
            elif kind == "mbtiles":
                chart = MBTilesChart(path, settings)
                progress(100)
            else:
                directory = str(Path(path).resolve()) + os.sep
                files = [
                    p
                    for p in Path(directory).iterdir()
                    if p.is_file()
                    and p.suffix.lower() in (".kap", ".bsb", ".tif", ".tiff", ".vrt")
                ]
                if not files:
                    raise ValueError("No supported raster charts in this folder")
                metadata = []
                for index, file in enumerate(files):
                    dataset = gdal.Open(str(file))
                    if (
                        dataset is None
                        or dataset.GetRasterBand(1).GetRasterColorTable() is None
                    ):
                        raise ValueError(
                            f"{file.name}: the raster renderer requires a palette chart"
                        )
                    dataset = None
                    metadata.append(GDALSingleRasterChart.get_file_info(str(file)))
                    progress(100 * (index + 1) / len(files))
                chart = GDALRasterChart(
                    directory,
                    settings,
                    metadata=metadata,
                    schedule=self.owner.scheduler.submit,
                )
            return chart, settings

        return self.run("chart", "Indexing chart…", prepare)

    def list_charts(self, latitude, longitude):
        def listing(_):
            self.catalog.load()
            return [c for c in self.catalog.cells if c.contains(latitude, longitude)]

        return self.run("chart-list", "Finding NOAA charts at this location…", listing)

    def download_chart(self, cell):
        palette = self.owner.settings.chartPalette

        def download(_):
            path = self.catalog.download(cell)
            settings = ChartSettings(palette)
            return GDALVectorChart(path, settings), settings

        return self.run("chart", "Downloading NOAA chart…", download)

    def _save_chart_entries(self, chart, enabled=None, remove=False):
        key = chart.ctype + "Charts"
        entries = [dict(e) for e in self.owner.settings[key]]
        if remove:
            entries = [e for e in entries if e["path"] != chart.path]
        else:
            value = {
                "path": chart.path,
                "metadata": chart.metadata,
                "enabled": chart.enabled if enabled is None else enabled,
            }
            index = next(
                (i for i, e in enumerate(entries) if e["path"] == chart.path), None
            )
            if index is None:
                entries.append(value)
            else:
                entries[index] = value
        self.owner.settings[key] = entries

    def set_chart_enabled(self, chart, enabled):
        self._save_chart_entries(chart, enabled)
        chart.enabled = enabled
        self.redraw()
        self.changed.emit()

    def remove_chart(self, chart):
        self._save_chart_entries(chart, remove=True)
        self.owner.charts.charts.remove(chart)
        self.redraw()
        self.changed.emit()

    def redraw(self):
        self.owner.charts._chart_surfaces.clear()
        for canvas in self.owner.charts.maps:
            canvas.queue_draw()

    def _finish(self, result, error):
        try:
            if error is not None:
                raise error
            handlers = {
                "scan": self._scanned,
                "forecast": self._install_forecast,
                "forecast-list": self._forecast_listed,
                "forecast-model-list": self._models_listed,
                "forecasts": self._install_forecasts,
                "chart-list": self._chart_listed,
                "chart": self._install_chart,
            }
            handlers[self.operation](result)
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            self.task.message = self.error

    def _scanned(self, result):
        files, errors, opened = result
        for forecast in self.manager.gribs:
            files.setdefault(forecast.path, forecast)
        self.local_forecasts = list(files.values())
        external = [
            g
            for g in self.manager.gribs
            if Path(g.path).parent != Path(GRIB_DIR).resolve() and g.path not in files
        ]
        order = {g.path: index for index, g in enumerate(self.manager.gribs)}
        opened.sort(key=lambda g: order.get(g.path, len(order)))
        self.manager.gribs[:] = [*external, *opened]
        self.manager.store_opened_gribs()
        self.manager.storage.save()
        if errors:
            self.task.message = "Skipped unreadable forecasts: " + "; ".join(errors)
        elif self.forecast_report:
            self.task.message = self.forecast_report

    def _install_forecast(self, result):
        name = forecast_source(result)
        if self.operation == "forecast" and name not in self.selection.models:
            self._persist_selection(
                replace(self.selection, models=(*self.selection.models, name))
            )
        if name not in self.selection.models:
            self.local_forecasts.append(result)
            self.task.message = "Forecast cached; its model is not selected"
            return
        old = list(self.manager.gribs)
        if not any(g.path == result.path for g in old):
            self.manager.gribs.append(result)
        try:
            self.manager.store_opened_gribs()
            self.manager.storage.save()
        except Exception:
            self.manager.gribs[:] = old
            raise
        self.local_forecasts = [
            g for g in self.local_forecasts if g.path != result.path
        ]
        self.local_forecasts.append(result)
        self.redraw()
        self.forecast_loaded.emit(result)
        self.task.message = "Forecast enabled and saved"

    def _models_listed(self, result):
        entries, issues = result
        self._forecast_listed(entries)
        if issues:
            self.task.message += ". " + "; ".join(issues)

    def _install_forecasts(self, result):
        forecasts, issues = result
        installed = 0
        for forecast in forecasts:
            try:
                self._install_forecast(forecast)
                installed += 1
            except Exception as exc:
                issues.append(f"{forecast.name}: {exc}")
        self.task.message = f"{installed} forecasts enabled and saved"
        if issues:
            self.task.message += ". Failed: " + "; ".join(issues)
        self.forecast_report = self.task.message
        if not installed and issues:
            self.error = "; ".join(issues)

    def _forecast_listed(self, result):
        self.remote_forecasts = result
        self.task.message = f"{len(result)} forecasts available"

    def _chart_listed(self, result):
        self.remote_charts = result
        self.task.message = f"{len(result)} NOAA charts cover this location"

    def _install_chart(self, result):
        chart, settings = result
        self._save_chart_entries(chart, True)
        settings.attach(self.owner.settings)
        chart.settings_manager = self.owner.settings
        charts = self.owner.charts.charts
        index = next((i for i, c in enumerate(charts) if c.path == chart.path), None)
        if index is None:
            charts.append(chart)
        else:
            charts[index] = chart
        self.redraw()
        self.task.message = "Chart enabled and saved"

    def close(self):
        self.selection_scan_timer.stop()
        self.task.close()
