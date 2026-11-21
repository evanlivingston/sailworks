"""Qt data-library operations using the shared forecast and chart models."""

import json
import logging
import math
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal
from osgeo import gdal

from sailworks.core.enccharts import ENCCatalog, missing_chart_for_view
from sailworks.core.storage import DATA_DIR
from sailworks.rendering.gdalvectorchart import GDALVectorChart
from sailworks.rendering.mbtilesrenderer import MBTilesChart
from sailworks.rendering.gdalrasterchart import (
    GDALRasterChart,
    GDALSingleRasterChart,
)
from .backgroundtask import BackgroundTask
from sailworks.rendering.chartlayer import ChartLayer

AUTO_CHART_COOLDOWN = 600.0
CELL_PATH = re.compile(r"(?:^|[/\\])(US[A-Z0-9]{6})(?=\.|[/\\]|$)")
logger = logging.getLogger("sailworks")


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
    # An automatically downloaded chart is installed and drawing-ready.  The
    # viewport listens for this and requests fresh frames: the camera has not
    # moved, so nothing else would ask again.
    autoChartReady = Signal()

    def __init__(self, session_controller):
        super().__init__(session_controller)
        self.owner = session_controller
        self.task = BackgroundTask(self)
        self.task.finished.connect(self._finish)
        self.task.changed.connect(self.changed.emit)
        # Automatic chart fetches own a task of their own: a manual chart
        # operation that is busy or that fails must neither block them nor be
        # mistaken for them.  One auto operation runs at a time.
        self.auto_task = BackgroundTask(self)
        self.auto_task.finished.connect(self._auto_finish)
        self.auto_task.changed.connect(self.changed.emit)
        self.catalog = ENCCatalog(Path(DATA_DIR) / "enc")
        self.remote_charts = []
        self.operation = ""
        self.error = ""
        self.auto_downloads = 0
        self._auto_attempts = set()
        self._auto_cooldown_until = 0.0
        self._auto_cell = None
        self._auto_operation = ""
        self._auto_existing = False
        self._auto_view = None

    @property
    def chart_downloading(self):
        """True only while a NOAA ENC exchange set is actually being fetched.

        Catalog selection and adopting an exchange set already on disk are
        work too, but neither is a download; the status bar indicator hides
        for them.
        """
        if self.task.busy and self.operation == "chart-download":
            return True
        return (
            self.auto_task.busy
            and self._auto_operation == "chart-auto"
            and not self._auto_existing
        )

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

        return self.run("chart", "Indexing chart", prepare)

    def list_charts(self, latitude, longitude):
        def listing(_):
            self.catalog.load()
            return [c for c in self.catalog.cells if c.contains(latitude, longitude)]

        return self.run("chart-list", "Finding NOAA charts at this location", listing)

    def download_chart(self, cell):
        palette = self.owner.settings.chartPalette

        def download(_):
            path = self.catalog.download(cell)
            settings = ChartSettings(palette)
            return GDALVectorChart(path, settings), settings

        # A distinct operation, not plain chart indexing: the manual import
        # shares the "chart" operation, and the status bar's download
        # indicator keys off this one because an import never fetches.
        return self.run("chart-download", "Downloading NOAA chart", download)

    def download_missing_chart(self, bounds, meters_per_pixel):
        """Fetch one missing NOAA ENC for a (south, west, north, east) view.

        On by default.  Both the selection and the download run on the
        dedicated serial auto task, so the catalog and the network never touch
        the GUI thread and a manual chart operation is neither blocked nor
        able to fail this one.  One operation is in flight at a time, a settled
        view selects once per install, a cell is requested at most once per
        session, and a failure starts a cooldown: those bound the work for one
        settled view.  There is deliberately no per-session success limit, so
        charts installed in an earlier view never stop a new view from being
        covered.
        """
        settings = self.owner.settings
        if not settings.get("chartAutoDownload", True):
            return False
        if not settings.get("chartENCVisible", True):
            return False
        if time.monotonic() < self._auto_cooldown_until:
            return False
        if self.auto_task.busy or self.auto_task.closed:
            return False
        view = self._auto_view_key(bounds, meters_per_pixel)
        if view is None or view == self._auto_view:
            # A settled view is checked once per install generation: repeated
            # demands from the same stationary camera do not re-select, while
            # ``_auto_installed`` clears the key so the next missing cell can
            # still be offered.
            return False
        installed, enabled = self._chart_sources()
        self._auto_operation = "chart-auto-select"
        # Record the view before the work starts: a finished install clears it
        # again, and that must not be undone by a later return from ``start``.
        self._auto_view = view
        if not self.auto_task.start(
            "Checking NOAA chart coverage",
            lambda _: self._auto_select(bounds, meters_per_pixel, installed, enabled),
        ):
            self._auto_operation = ""
            self._auto_view = None
            return False
        return True

    @staticmethod
    def _auto_view_key(bounds, meters_per_pixel):
        """A settled camera's identity, coarse enough to absorb jitter."""
        try:
            south, west, north, east = (float(value) for value in bounds)
            meters_per_pixel = float(meters_per_pixel)
        except (TypeError, ValueError):
            return None
        if not all(
            math.isfinite(value)
            for value in (south, west, north, east, meters_per_pixel)
        ):
            return None
        if meters_per_pixel <= 0 or north <= south:
            return None
        span = east - west
        if span < 0:
            span += 360
        # The centre wrapped, not the arithmetic mean: a view crossing the
        # antimeridian (west 179, east -179) is centred on 180/-180, not on 0
        # at the Gulf of Guinea, so the two views cannot share a key.
        centre = (west + span / 2 + 180) % 360 - 180
        return (
            round((north + south) / 2, 2),
            round(centre, 2),
            round(meters_per_pixel, 1),
        )

    def _chart_sources(self):
        """Catalog cell names already configured, and those currently enabled."""
        settings = self.owner.settings
        installed, enabled = set(), set()
        enc_enabled = bool(settings.get("chartENCVisible", True))
        for key in ("vectorCharts", "encCharts"):
            for entry in settings.get(key, []) or []:
                match = CELL_PATH.search(str(entry.get("path", "")))
                if match is None:
                    continue
                name = match.group(1)
                installed.add(name)
                if enc_enabled and entry.get("enabled", True):
                    enabled.add(name)
        return frozenset(installed), frozenset(enabled)

    def _auto_select(self, bounds, meters_per_pixel, installed, enabled):
        # The automatic path adopts the cached catalog even when it is stale:
        # paying the catalog request timeout again for every new view is what
        # made an offline session appear hung.  With no cache at all it falls
        # back to the one fetch, and a failure starts the cooldown.
        self.catalog.load(fetch=False)
        cell = missing_chart_for_view(
            self.catalog.cells,
            bounds,
            meters_per_pixel,
            installed,
            enabled,
            attempted=self._auto_attempts,
        )
        if cell is None:
            return None
        # The download itself is a worker operation: report an exchange set
        # left on disk by an earlier session so it is installed, not fetched
        # again and not skipped forever.
        return cell, self.catalog.downloaded_path(cell)

    def _auto_selected(self, result):
        if result is None:
            self.auto_task.message = "NOAA charts cover this view"
            return
        cell, existing = result
        if cell.name in self._auto_attempts:
            self.auto_task.message = "NOAA chart already requested"
            return
        self._auto_attempts.add(cell.name)
        logger.info(
            "auto chart selected %s (%s) for view %s",
            cell.name,
            "on disk" if existing else "download",
            self._auto_view,
        )
        palette = self.owner.settings.chartPalette
        if existing:
            message = f"Adding NOAA chart {cell.name}"
        else:
            message = f"Downloading NOAA chart {cell.name}"

        def prepare(_):
            # An exchange set already on disk is adopted: it is configured
            # instead of fetched again, and never skipped forever.
            path = existing or self.catalog.download(cell)
            return self._auto_chart(path, palette)

        self._auto_cell = cell.name
        self._auto_existing = bool(existing)
        self._auto_operation = "chart-auto"
        if not self.auto_task.start(message, prepare):
            self._auto_cell = None
            self._auto_operation = ""

    def _auto_chart(self, path, palette):
        settings = ChartSettings(palette)
        return GDALVectorChart(path, settings), settings

    def _auto_installed(self, result):
        name, self._auto_cell = self._auto_cell, None
        # Count and announce only a chart that is actually installed and able
        # to draw: an install the user's disabled entries or the chart class
        # suppress is saved, but it must not ask the viewport for frames of
        # nothing.
        drawable = self._install_chart(result, enabled=None, task=self.auto_task)
        if not drawable:
            self.auto_task.message = "NOAA chart saved, disabled"
            logger.info("auto chart saved but not drawable %s", name)
            return
        # ``auto_downloads`` counts installs for reporting only; it never
        # gates a fetch, so earlier coverage cannot stop a later view.
        self.auto_downloads += 1
        logger.info("auto chart installed %s", name)
        self.auto_task.message = "NOAA chart installed for this view"
        # A successful install changed what covers this stationary view, so it
        # is offered again: the next settled demand may find another missing
        # cell.  Reset only after the chart is registered, never before, and
        # let ``autoChartReady`` drive the follow-up selection.  The per-cell
        # attempt set and the serial task keep that loop bounded, and the
        # cooldown bounds a failing one.
        self._auto_view = None
        self.autoChartReady.emit()

    def _auto_finish(self, result, error):
        operation, self._auto_operation = self._auto_operation, ""
        self._auto_existing = False
        try:
            if error is not None:
                raise error
            if operation == "chart-auto-select":
                self._auto_selected(result)
            elif operation == "chart-auto":
                self._auto_installed(result)
        except Exception as exc:
            self._auto_failed(exc)

    def _auto_failed(self, exc):
        """Bound a failed selection or download: report it and back off."""
        self.auto_task.message = str(exc) or type(exc).__name__
        cell, self._auto_cell = self._auto_cell, None
        logger.warning("auto chart failed for %s: %s", cell or "selection", exc)
        # The view may be offered again once the cooldown has passed.
        self._auto_view = None
        self._auto_cooldown_until = time.monotonic() + AUTO_CHART_COOLDOWN

    def _chart_entries_on_disk(self, key):
        """The chart entries the settings file holds, as the file holds them.

        The in-memory list is not always the whole story.  An automatic
        install loads the entries it knows about, and writing only those back
        would shorten the user's own file: the charts a predecessor app left
        behind would be installed and enabled on disk but absent from the
        list the install saves, so the next reader of the file (the viewport's
        ENC scene catalog, which reads the file rather than this process's
        memory) would assemble only the cells this install happened to see.
        The file is the user's own list, so an entry it holds and memory does
        not is kept.
        """
        try:
            raw = json.loads((Path(DATA_DIR) / "settings.json").read_text())
        except (OSError, ValueError):
            return []
        entries = raw.get(key) if isinstance(raw, dict) else None
        return [
            dict(entry)
            for entry in entries or []
            if isinstance(entry, dict) and entry.get("path")
        ]

    def _save_chart_entries(self, chart, enabled=None, remove=False):
        key = chart.ctype + "Charts"
        entries = [dict(e) for e in self.owner.settings.get(key, []) or []]
        known = {entry["path"] for entry in entries if entry.get("path")}
        for entry in self._chart_entries_on_disk(key):
            # An entry the file holds is never dropped by a write that only
            # knew part of the list; the installed chart is added below and a
            # removal below is explicit about the one path it removes.
            if entry["path"] not in known:
                entries.append(entry)
                known.add(entry["path"])
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
                "chart": lambda value: self._install_chart(value, enabled=True),
                "chart-download": lambda value: self._install_chart(
                    value, enabled=True
                ),
            }
            handlers[self.operation](result)
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            self.task.message = self.error

    def _chart_listed(self, result):
        self.remote_charts = result
        self.task.message = f"{len(result)} NOAA charts cover this location"

    def _install_chart(self, result, enabled=None, task=None):
        from sailworks.rendering.chartclasses import chart_class_enabled

        chart, settings = result
        task = self.task if task is None else task
        if enabled is None:
            # An automatic install must not re-enable a chart the user turned
            # off: the configured entry's own flag wins.
            entries = self.owner.settings.get(chart.ctype + "Charts", []) or []
            existing = next((e for e in entries if e.get("path") == chart.path), None)
            enabled = bool(existing.get("enabled", True)) if existing else True
        self._save_chart_entries(chart, enabled)
        chart.enabled = enabled
        settings.attach(self.owner.settings)
        chart.settings_manager = self.owner.settings
        charts = self.owner.charts.charts
        index = next((i for i, c in enumerate(charts) if c.path == chart.path), None)
        drawable = chart_class_enabled(self.owner.settings, chart)
        drawable = drawable and bool(chart.enabled)
        if not drawable:
            if index is not None:
                del charts[index]
        elif index is None:
            charts.append(chart)
        else:
            charts[index] = chart
        self.redraw()
        task.message = "Chart enabled and saved" if enabled else "Chart saved, disabled"
        return drawable

    def close(self):
        self.task.close()
        self.auto_task.close()
