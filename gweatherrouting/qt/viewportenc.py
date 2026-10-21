"""The viewport's ENC chart source: the application's ENC, in the viewport.

The viewport's chart layer is the application's own chart composition.  This
module supplies the source that composition was missing: the installed and
enabled ENC cells, drawn by the application's own ``ENCOverlayRenderer``.

Nothing here re-implements the portrayal.  The cells are ranked, filtered,
clipped and drawn by ``ENCOverlayRenderer._render`` -- the reference
implementation, unchanged -- from the chart descriptors the reference
expects: ``(path, native_scale, coverage_envelopes)``.  The compilation
scale ranking, the scale visibility filter, the coverage union, intersection
and difference, the even/odd clip, the downward-quantised simplification, the
S-57 labels, their anchors and their suppression, the area/line/point order
and the dash and alpha of the styles are the application's, because the
application's own renderer does them.

What the module adds is the viewport's own bookkeeping:

* the *manifest*: the installed cells and the user's own enabled state, read
  from the user's settings.  The worker opens the charts with its own
  handles: no chart object and no open handle is shared with the application;
* the *identity* of a composition: the cells that were selected and the
  coverage region each one ended up with, so a coverage miss is explicit and
  a selection can be compared with the reference's;
* the *states*: ``disabled``, ``loading``, ``ready``, ``coverage_miss`` and
  ``unavailable``, reported instead of an empty chart that would read as
  "there is no data here".
"""

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path

import cairo
from osgeo import ogr

from gweatherrouting.core.enccharts import chart_scale_rank, chart_scale_visible
from gweatherrouting.core.scheduling import schedule_idle
from gweatherrouting.qt.encgeometrycache import (
    PreparedFeatureCache,
    ViewportS57ChartDrawer,
)
from gweatherrouting.rendering.chartlayer import ChartLayer
from gweatherrouting.rendering.encrenderer import ENCOverlayRenderer
from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

#: The settings key the application's installed chart entries live under.
CHART_ENTRIES_KEY = "vectorCharts"

#: The display switch the application's own chart collection honours.
CLASS_KEY = "chartENCVisible"

#: The palette key in the user's settings.
PALETTE_KEY = "chartPalette"

#: The ENC source's own states.  They are the states the chart layer reports
#: for the ENC source, and they never stand in for one another.
STATE_DISABLED = "disabled"
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_COVERAGE_MISS = "coverage_miss"
STATE_UNAVAILABLE = "unavailable"

#: The reasons the source reports, in the user's own words.
REASON_DISABLED = "ENC charts are disabled"
REASON_NO_CELLS = "No ENC chart is enabled"
REASON_NO_COVERAGE = "No ENC coverage at this position"
REASON_UNREADABLE = "No ENC chart could be read"
REASON_CANCELLED = "The ENC preparation was cancelled"


def enc_cell_name(path):
    """The ENC cell's name, by the application's own rule."""
    return os.path.splitext(os.path.basename(path))[0].upper()


def is_enc_path(path):
    """Whether an installed chart path is an ENC chart.

    The application's own chart loader decides this from the file name: an
    S-57 exchange set is a ``.000`` file.  The same rule is used here, so
    the viewport's cells are the application's cells and no new policy is
    introduced.
    """
    return ".000" in path


def _version(path):
    """The file's identity, so a replaced chart invalidates the metadata."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def manifest_stamp(data_dir):
    """The user's settings file's own identity.

    The manifest is the user's file, and its identity is a cheap signal that
    the installed or the enabled cells may have changed.  It says nothing
    about the cells themselves: the worker reads the manifest itself.
    """
    if data_dir is None:
        return None
    try:
        stat = os.stat(Path(data_dir) / "settings.json")
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


@dataclass(frozen=True)
class Manifest:
    """The user's own chart manifest, read from the user's settings.

    The manifest is the application's: the entries are the user's charts and
    their enabled state.  Nothing is written, and no chart object is shared.
    A manifest that could not be read says so: ``error`` carries the reason
    and ``missing`` says that the user has no settings file at all.
    """

    entries: tuple = ()
    class_enabled: bool = True
    palette: str = ""
    generation: tuple = ()
    error: str = ""
    missing: bool = False

    @property
    def enc_entries(self):
        """The installed ENC entries, in the user's own order."""
        return tuple(entry for entry in self.entries if is_enc_path(entry[0]))

    @property
    def enabled(self):
        """The installed and enabled ENC entries."""
        return tuple(entry for entry in self.enc_entries if entry[1])

    @property
    def disabled(self):
        """The installed ENC entries the user switched off."""
        return tuple(entry for entry in self.enc_entries if not entry[1])


def read_manifest(data_dir):
    """The installed chart entries, read from the user's own settings.

    The settings file is read, never written.  A missing file is reported as
    missing and a broken one as unreadable: neither is ever read as a chart,
    and neither is a silent failure.
    """
    if data_dir is None:
        return Manifest(error="no application data directory")
    path = Path(data_dir) / "settings.json"
    error = ""
    missing = False
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raw, error, missing = {}, "no settings file at %s" % path, True
    except (OSError, ValueError) as exc:
        raw, error = {}, "unreadable settings at %s: %s" % (path, exc)
    if not isinstance(raw, dict):
        raw, error = {}, "unexpected settings content at %s" % path
    entries = []
    for entry in raw.get(CHART_ENTRIES_KEY) or []:
        if not isinstance(entry, dict):
            continue
        chart_path = entry.get("path")
        if not isinstance(chart_path, str) or not chart_path:
            continue
        entries.append((chart_path, bool(entry.get("enabled", True))))
    generation = tuple(
        (chart_path, enabled, _version(chart_path))
        for chart_path, enabled in entries
        if is_enc_path(chart_path)
    )
    return Manifest(
        entries=tuple(entries),
        class_enabled=bool(raw.get(CLASS_KEY, True)),
        palette=str(raw.get(PALETTE_KEY, "") or ""),
        generation=generation,
        error=error,
        missing=missing,
    )


@dataclass(frozen=True, eq=False)
class EncSource:
    """One installed ENC chart, described for the worker.

    The descriptor is the one the reference renderer expects: the path, the
    compilation scale and the coverage envelopes.  A chart that could not be
    read keeps its place in the manifest and reports why.
    """

    path: str
    native_scale: int = 0
    envelopes: tuple = ()
    error: str = ""

    @property
    def name(self):
        return enc_cell_name(self.path)

    @property
    def descriptor(self):
        """The chart descriptor the reference renderer expects."""
        return (self.path, self.native_scale, self.envelopes)


class EncCatalog:
    """The installed, enabled ENC charts, read with the worker's own handles.

    The manifest is re-read on every refresh; the charts' metadata is read
    only when the manifest changed, so an installed, replaced or switched
    off chart invalidates the metadata that described it.
    """

    def __init__(self, data_dir):
        self._data_dir = data_dir
        self._manifest = None
        self._sources = ()
        self._disabled = ()

    @property
    def manifest(self):
        return Manifest() if self._manifest is None else self._manifest

    @property
    def sources(self):
        """The installed and enabled ENC charts, in the user's order."""
        return self._sources

    @property
    def disabled(self):
        """The names of the ENC charts the user switched off."""
        return self._disabled

    def refresh(self):
        """Re-read the manifest and, when the cells changed, their metadata.

        The manifest itself is always the current one: the user's own switch
        and the reason a manifest could not be read are never stale.  Only
        the cells' metadata is kept, and it is read again when the cells
        changed, so an installed, replaced or switched off cell invalidates
        the metadata that described it.
        """
        manifest = read_manifest(self._data_dir)
        changed = (
            self._manifest is None or manifest.generation != self._manifest.generation
        )
        self._manifest = manifest
        if not changed:
            return self
        self._sources = tuple(self._read(path) for path, _ in manifest.enabled)
        self._disabled = tuple(enc_cell_name(path) for path, _ in manifest.disabled)
        return self

    @staticmethod
    def _read(path):
        """One chart's metadata, read with the worker's own handle.

        The metadata is the application's own: the compilation scale comes
        from the chart's identification layer and the coverage envelopes
        from its coverage layer, read exactly as the application's chart
        loader reads them.
        """
        vector_file = None
        chart = None
        try:
            vector_file = ogr.Open(path)
            if vector_file is None:
                raise ValueError("cannot open %s" % path)
            chart = GDALVectorChart.__new__(GDALVectorChart)
            chart.vector_file = vector_file
            chart._coverage = []
            chart._native_scale = 0
            chart._load_enc_metadata()
            envelopes = tuple(geometry.GetEnvelope() for geometry in chart._coverage)
            return EncSource(path, chart._native_scale, envelopes)
        except Exception as exc:
            return EncSource(path, error=str(exc))
        finally:
            # The worker's own metadata handle is released here: the renderer
            # opens its own handle for the drawing, and no handle outlives
            # the read that described it.
            del vector_file, chart


def region_identity(path, region):
    """The identity of one cell's coverage region.

    The region is compared as geometry, not as pixels: its envelope, its
    area and the digest of its exact shape.
    """
    wkt = region.ExportToWkt()
    return {
        "name": enc_cell_name(path),
        "envelope": tuple(region.GetEnvelope()),
        "area": region.GetArea(),
        "wkt_sha256": hashlib.sha256(wkt.encode("utf-8")).hexdigest(),
    }


class ViewportEncRenderer(ENCOverlayRenderer):
    """The application's ENC renderer, on the chart worker's own handles.

    ``_render`` is the reference implementation, unchanged: the cells are
    ranked, filtered, clipped and drawn exactly as the application draws
    them.  The subclass records the *identity* of the composition -- the
    cells that were selected and the coverage region each one ended up with
    -- which the reference does not report, and it keeps the prepared
    geometry of the cells in a cache of its own, so that the eviction of a
    chart handle does not make the cells be prepared again.
    """

    def __init__(self, schedule=schedule_idle, feature_cache=None):
        super().__init__(schedule)
        self._coverages = {}
        self._regions = ()
        # The prepared geometry is the renderer's own: it outlives the chart
        # handles, which are evicted, and it is keyed by the cell's own path,
        # so the same cell reopened finds the geometry already prepared.
        self._feature_cache = (
            feature_cache if feature_cache is not None else PreparedFeatureCache()
        )

    @property
    def feature_cache(self):
        """The renderer's own cache of prepared features."""
        return self._feature_cache

    @property
    def cache_bytes(self):
        """The bytes the prepared geometry cache retains."""
        return self._feature_cache.bytes

    def _worker_chart(self, path, native_scale, palette):
        chart = super()._worker_chart(path, native_scale, palette)
        # The reference's own coverage, recorded for the identity report.
        self._coverages.setdefault(path, tuple(chart._coverage))
        self._install_drawer(chart, path, palette)
        return chart

    def _install_drawer(self, chart, path, palette):
        """The viewport's own drawer, on the renderer's own cache.

        The drawer is the reference's own drawer: the same features, the same
        tolerance, the same order, the same styles and the same clipping.
        Only the place the prepared geometry is kept changes, and the cell's
        path becomes the cell's identity, so the same cell reopened hits the
        entries that were prepared for it.

        A chart that is not drawn by the ENC drawer is left exactly as it
        is: it has no prepared geometry to keep.
        """
        drawer = getattr(chart, "drawer", None)
        if not isinstance(drawer, S57ChartDrawer):
            return
        if not isinstance(drawer, ViewportS57ChartDrawer):
            drawer = ViewportS57ChartDrawer()
            chart.drawer = drawer
        drawer.bind(path, self._feature_cache, palette)

    def _render(self, job):
        self._coverages = {}
        surface = super()._render(job)
        self._regions = () if surface is None else self._regions_of(job)
        return surface

    @property
    def regions(self):
        """The selected cells and the coverage region of each one."""
        return self._regions

    def release(self):
        """Release the worker's own chart handles and the prepared geometry.

        The prepared geometry describes the cells as they were read: a cell
        that was replaced or switched off must never be drawn from what was
        prepared for the cell that was there before it.
        """
        self.release_handles()
        self._feature_cache.clear()

    def release_handles(self):
        """Release the worker's own chart handles, and keep the geometry.

        The prepared geometry outlives the handle it was prepared for: the
        handles are the renderer's own and are released when the worker is
        done with them, while the geometry is kept for the next draw of the
        same cells.
        """
        self._charts.clear()
        self._coverages = {}
        self._regions = ()

    def _regions_of(self, job):
        """The per-cell coverage regions, assembled as the reference does.

        The reference assembles the coverage while it draws: every cell's
        coverage is unioned, intersected with the view and differenced
        against the region the higher priority cells already cover.  The
        same assembly is repeated here over the reference's own coverage,
        so the reported regions are the regions the reference drew into.
        """
        bounding = self._bounding(job)
        covered = None
        regions = []
        for path, region, known in self._candidates(job, bounding):
            if covered is not None:
                region = region.Difference(covered)
            if region.IsEmpty():
                continue
            regions.append((path, region))
            if known:
                covered = region if covered is None else covered.Union(region)
        return tuple(regions)

    @staticmethod
    def _bounding(job):
        """The view's own bounding polygon, as the reference builds it."""
        p1, p2 = job.viewport.get_bbox()
        return ogr.CreateGeometryFromWkt(
            ChartLayer.get_bounding_wkt_of_coords(*p1.get_degrees(), *p2.get_degrees())
        )

    def _candidates(self, job, bounding):
        """The cells the reference would draw, with their clipped coverage.

        The scale visibility rule, the coverage envelope test and the
        cancellation are the application's own: a cell the reference would
        not draw is never reported as selected.
        """
        viewport = job.viewport
        min_lon, max_lon, min_lat, max_lat = bounding.GetEnvelope()
        for path, native_scale, envelopes in self._ranked(job):
            if job.cancel.is_set():
                return
            if not chart_scale_visible(native_scale, viewport.get_scale()):
                continue
            if envelopes and not self._envelope_reaches(
                envelopes, min_lon, max_lon, min_lat, max_lat
            ):
                continue
            region, known = self._cell_region(path, bounding)
            if region is None:
                continue
            yield path, region, known

    @staticmethod
    def _ranked(job):
        """The cells in the reference's own order: the closest scale first."""
        viewport = job.viewport
        return sorted(
            job.charts,
            key=lambda chart: (
                chart_scale_rank(chart[1], viewport.get_scale()),
                chart[0],
            ),
        )

    @staticmethod
    def _envelope_reaches(envelopes, min_lon, max_lon, min_lat, max_lat):
        """Whether any coverage envelope reaches the view."""
        return any(
            a <= max_lon and b >= min_lon and c <= max_lat and d >= min_lat
            for a, b, c, d in envelopes
        )

    def _cell_region(self, path, bounding):
        """One cell's coverage, clipped to the view.

        The second value says whether the cell described its own coverage:
        a cell without one covers the whole view, exactly as in the
        reference, and does not narrow the cells that follow it.
        """
        region = None
        for geometry in self._coverages.get(path, ()):
            region = geometry.Clone() if region is None else region.Union(geometry)
        known = region is not None
        if region is None:
            return bounding.Clone(), known
        return region.Intersection(bounding), known


def disabled_metadata():
    """The ENC metadata of a layer that does not compose ENC at all."""
    return {
        "enabled": False,
        "state": STATE_DISABLED,
        "reason": REASON_DISABLED,
        "error": "",
        "source_ids": (),
        "disabled_ids": (),
        "selected": (),
        "coverage": (),
        "failed": (),
        "factor": 1,
        "generation": "",
        "bytes": 0,
    }


class ViewportEncSource:
    """The viewport's ENC source, on the chart worker's own handles.

    The source owns the manifest it reads, the charts it opens and the
    reference renderer it draws with.  Nothing is shared with the
    application: the application's chart objects are never borrowed, and
    every handle the source opens is released when it is released.
    """

    def __init__(self, data_dir, palette):
        self._data_dir = data_dir
        self._palette = palette
        self._catalog = EncCatalog(data_dir)
        self._renderer = None
        self._generation = None
        self._state = STATE_DISABLED
        self._reason = ""
        self._error = ""

    @property
    def state(self):
        return self._state

    @property
    def cache_bytes(self):
        """The bytes the worker's own prepared geometry cache retains."""
        renderer = self._renderer
        return 0 if renderer is None else renderer.cache_bytes

    def compose(self, job, surface):
        """Compose the ENC cells onto the chart surface.

        The chart surface is returned with the ENC painted onto it, in the
        application's order: the base chart first, the ENC second.  A
        failure never raises: the surface is returned without the ENC and
        the state says why, so an ENC failure is never presented as a chart.
        """
        metadata = self._empty_metadata(job)
        try:
            self._catalog.refresh()
        except Exception as exc:  # the manifest is never a silent failure
            return surface, self._unavailable(metadata, exc)
        manifest = self._catalog.manifest
        metadata["enabled"] = manifest.class_enabled
        if manifest.error and not manifest.missing:
            # The user's own manifest could not be read: the cells are
            # unknown, and an unknown cell is never presented as a chart.
            metadata["error"] = manifest.error
            return surface, self._unavailable(metadata, RuntimeError(manifest.error))
        self._adopt_generation()
        if not manifest.class_enabled:
            metadata["state"] = STATE_DISABLED
            metadata["reason"] = REASON_DISABLED
            self._state = STATE_DISABLED
            return surface, self._finish(metadata)
        return surface, self._compose_cells(job, surface, metadata)

    def _compose_cells(self, job, surface, metadata):
        """Compose the installed and enabled cells onto the surface."""
        sources = self._catalog.sources
        metadata["source_ids"] = tuple(source.name for source in sources)
        metadata["disabled_ids"] = tuple(self._catalog.disabled)
        failed = tuple(
            (source.name, source.error) for source in sources if source.error
        )
        metadata["failed"] = failed
        if failed:
            metadata["error"] = "; ".join(
                "%s: %s" % (name, error) for name, error in failed
            )
        usable = tuple(source for source in sources if not source.error)
        if not usable:
            if failed:
                metadata["state"] = STATE_UNAVAILABLE
                metadata["reason"] = REASON_UNREADABLE
            else:
                # Nothing is enabled: the source is disabled, not a coverage
                # miss.  A coverage miss says that cells exist and none of
                # them covers the view.
                metadata["enabled"] = False
                metadata["state"] = STATE_DISABLED
                metadata["reason"] = REASON_NO_CELLS
            self._state = metadata["state"]
            self._error = metadata["error"]
            return self._finish(metadata)
        renderer = self._renderer_instance()
        enc_job = replace(job, charts=tuple(source.descriptor for source in usable))
        try:
            enc_surface = renderer._render(enc_job)
        except Exception as exc:
            # An ENC failure is never a chart failure: the chart is composed
            # without the ENC and the state says why, so a broken cell is
            # never presented as the previous cell's content.
            return self._unavailable(metadata, exc)
        if enc_surface is None:
            metadata["state"] = STATE_LOADING
            metadata["reason"] = REASON_CANCELLED
            self._state = STATE_LOADING
            return self._finish(metadata)
        regions = renderer.regions
        metadata["selected"] = tuple(enc_cell_name(path) for path, _r in regions)
        metadata["coverage"] = tuple(
            region_identity(path, region) for path, region in regions
        )
        if not regions:
            # No enabled cell covers this view: an explicit coverage miss,
            # never an empty chart that reads as "no data here".
            metadata["state"] = STATE_COVERAGE_MISS
            metadata["reason"] = REASON_NO_COVERAGE
            self._state = STATE_COVERAGE_MISS
            return self._finish(metadata)
        cr = cairo.Context(surface)
        cr.set_source_surface(enc_surface, 0, 0)
        cr.paint()
        surface.flush()
        metadata["state"] = STATE_READY
        self._state = STATE_READY
        self._error = ""
        return self._finish(metadata)

    def _adopt_generation(self):
        """Release the worker's cached charts when the sources changed.

        The manifest's generation covers the installed cells, their enabled
        state and their files' identity, so a chart that was installed,
        replaced or switched off releases the metadata that described it --
        and the geometry that was prepared for it.
        """
        generation = self._catalog.manifest.generation
        if generation == self._generation:
            return
        self._generation = generation
        if self._renderer is not None:
            self._renderer.release()

    def release(self):
        """Release the worker's own chart handles.

        The prepared geometry is kept: it describes the cells as they were
        read, and the cells themselves decide when it must be dropped -- a
        cell that changed releases it, through the generation.
        """
        if self._renderer is not None:
            self._renderer.release_handles()

    def _renderer_instance(self):
        if self._renderer is None:
            self._renderer = ViewportEncRenderer(schedule_idle)
        return self._renderer

    def _empty_metadata(self, job):
        """The report of a composition that has not looked at any cell yet."""
        return {
            "enabled": True,
            "state": STATE_COVERAGE_MISS,
            "reason": "",
            "error": "",
            "source_ids": (),
            "disabled_ids": (),
            "selected": (),
            "coverage": (),
            "failed": (),
            "factor": job.factor,
            "generation": "",
            "bytes": 0,
        }

    def _unavailable(self, metadata, exc):
        """The report of a composition that could not read or draw a cell."""
        metadata["state"] = STATE_UNAVAILABLE
        metadata["reason"] = REASON_UNREADABLE
        metadata["error"] = str(exc)
        self._state = STATE_UNAVAILABLE
        self._error = str(exc)
        return self._finish(metadata)

    def _finish(self, metadata):
        """Complete the metadata with its own size."""
        metadata["generation"] = hashlib.sha256(
            repr(self._catalog.manifest.generation).encode("utf-8")
        ).hexdigest()
        metadata["bytes"] = len(
            json.dumps(metadata, sort_keys=True, default=str).encode("utf-8")
        )
        return metadata
