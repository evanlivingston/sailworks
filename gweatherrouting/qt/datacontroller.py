"""Qt data-library operations using the shared forecast and chart models."""

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal
from osgeo import gdal

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.enccharts import ENCCatalog
from gweatherrouting.core.storage import DATA_DIR, GRIB_DIR
from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
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
        self.task = BackgroundTask(self)
        self.task.finished.connect(self._finish)
        self.task.changed.connect(self.changed.emit)
        self.catalog = ENCCatalog(Path(DATA_DIR) / "enc")
        self.remote_forecasts = []
        self.remote_charts = []
        self.local_forecasts = []
        self.operation = ""
        self.error = ""

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
        return self.task.start(message, operation)

    def forecast_directory(self):
        return Path(GRIB_DIR).resolve()

    def scan_forecasts(self):
        def scan(progress):
            files, errors = {}, []
            for path in sorted(Path(GRIB_DIR).glob("*")):
                if path.suffix.lower() not in (".grib", ".grb", ".grib2", ".grb2"):
                    continue
                try:
                    metadata = Grib.parse_metadata(str(path))
                    if metadata.start_time is None:
                        raise ValueError("No wind forecast")
                    files[str(path)] = metadata
                except Exception as exc:
                    errors.append(f"{path.name}: {exc}")
            return files, errors

        return self.run("scan", "Reading local forecasts…", scan)

    def import_forecast(self, path):
        return self.run(
            "forecast", "Importing forecast…", lambda _: import_forecast(path)
        )

    def set_forecast_enabled(self, metadata, enabled):
        if self.task.busy or self.owner.routing.busy or self.owner.polars.task.busy:
            return
        if enabled:
            self.run(
                "forecast", "Opening forecast…", lambda _: read_forecast(metadata.path)
            )
        else:
            old = list(self.manager.gribs)
            self.manager.gribs[:] = [g for g in old if g.path != metadata.path]
            try:
                self.manager.store_opened_gribs()
                self.manager.storage.save()
            except Exception:
                self.manager.gribs[:] = old
                raise
            self.redraw()
            self.changed.emit()

    def remove_forecast(self, metadata):
        if self.task.busy or self.owner.routing.busy or self.owner.polars.task.busy:
            return
        path = Path(metadata.path).resolve()
        if path.parent != Path(GRIB_DIR).resolve():
            raise ValueError("Only imported library copies can be removed")
        self.set_forecast_enabled(metadata, False)
        path.unlink()
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
                "chart-list": self._chart_listed,
                "chart": self._install_chart,
            }
            handlers[self.operation](result)
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            self.task.message = self.error

    def _scanned(self, result):
        files, errors = result
        for forecast in self.manager.gribs:
            files.setdefault(forecast.path, forecast)
        self.local_forecasts = list(files.values())
        if errors:
            self.task.message = "Skipped unreadable forecasts: " + "; ".join(errors)

    def _install_forecast(self, result):
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
        self.task.close()
