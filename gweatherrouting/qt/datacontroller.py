"""Qt data-library operations using the shared forecast and chart models."""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal
from osgeo import gdal

from gweatherrouting.core.enccharts import ENCCatalog
from gweatherrouting.core.storage import DATA_DIR
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


class DataController(QObject):
    changed = Signal()

    def __init__(self, session_controller):
        super().__init__(session_controller)
        self.owner = session_controller
        self.task = BackgroundTask(self)
        self.task.finished.connect(self._finish)
        self.task.changed.connect(self.changed.emit)
        self.catalog = ENCCatalog(Path(DATA_DIR) / "enc")
        self.remote_charts = []
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
                "chart-list": self._chart_listed,
                "chart": self._install_chart,
            }
            handlers[self.operation](result)
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            self.task.message = self.error

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
