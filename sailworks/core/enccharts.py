"""NOAA ENC coverage discovery and on-demand, offline chart downloads."""

import math
import re
import tempfile
import threading
import time
import xml.etree.ElementTree as ElementTree
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

CATALOG_URL = "https://www.charts.noaa.gov/ENCs/ENCProdCat.xml"
CHART_HOSTS = frozenset({"charts.noaa.gov", "www.charts.noaa.gov"})
MAX_REDIRECTS = 5
CACHE_SECONDS = 7 * 24 * 60 * 60
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


def _checked_url(url):
    """Reject anything that is not an HTTPS NOAA chart URL before it is fetched.

    Redirects are followed by hand, so every hop is checked here *before* a
    request is made; the default opener would otherwise follow an arbitrary
    host first and only let the caller inspect the destination afterwards.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in CHART_HOSTS:
        raise ValueError("Unexpected chart host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Unexpected chart host")
    return url


def _get(url, **kwargs):
    """A bounded GET that validates the URL and each redirect hop up front."""
    for _ in range(MAX_REDIRECTS + 1):
        _checked_url(url)
        response = requests.get(url, allow_redirects=False, **kwargs)
        if getattr(response, "status_code", None) not in _REDIRECT_STATUS:
            return response
        location = response.headers.get("location")
        response.close()
        if not location:
            raise ValueError("Chart redirect without a destination")
        url = urljoin(url, location)
    raise ValueError("Too many chart redirects")


def display_scale_denominator(meters_per_pixel):
    """The nominal 1:N display scale for a view's metres per pixel.

    The single conversion the ENC scale gates share: a raster or vector chart's
    compilation scale and a feature's SCAMIN are both denominators of this
    form, so this is the only place the 96 dpi assumption is applied.
    """
    return float(meters_per_pixel) * 3779.527559


def chart_scale_rank(native_scale, meters_per_pixel):
    """Prefer compilation scales closest to the current nominal display scale.

    Logarithmic distance treats each doubling equally. Prefer the more detailed
    source when two scales are equally close; unknown scales are fallbacks.
    """
    if not native_scale or meters_per_pixel <= 0:
        return (math.inf, math.inf)
    display_scale = display_scale_denominator(meters_per_pixel)
    return (abs(math.log2(native_scale / display_scale)), native_scale)


def chart_scale_visible(native_scale, meters_per_pixel):
    """Limit harbor clutter when zoomed out and overview geometry when zoomed in."""
    if not native_scale:
        return True
    display_scale = display_scale_denominator(meters_per_pixel)
    if display_scale > native_scale * 16:
        return False
    return native_scale < 1000000 or native_scale <= display_scale * 8


def polygon_contains(vertices, lat, lon):
    """Ray casting, unwrapping longitudes across the antimeridian."""
    points: list[tuple[float, float]] = []
    for y, x in vertices:
        if points:
            x += 360 * round((points[-1][1] - x) / 360)
        points.append((y, x))
    if len(points) < 3:
        return False
    lon += 360 * round((points[0][1] - lon) / 360)
    inside = False
    y1, x1 = points[-1]
    for y2, x2 in points:
        cross = (lon - x1) * (y2 - y1) - (lat - y1) * (x2 - x1)
        if abs(cross) < 1e-10 and min(x1, x2) <= lon <= max(x1, x2):
            if min(y1, y2) <= lat <= max(y1, y2):
                return True
        if (y1 > lat) != (y2 > lat):
            if lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                inside = not inside
        y1, x1 = y2, x2
    return inside


@dataclass
class ENCCell:
    name: str
    title: str
    scale: int
    size: int
    panels: list

    def __post_init__(self):
        self._bounds = []
        for kind, vertices in self.panels:
            if kind != "E" or len(vertices) < 3:
                continue
            longitudes: list[float] = []
            for _, lon in vertices:
                if longitudes:
                    lon += 360 * round((longitudes[-1] - lon) / 360)
                longitudes.append(lon)
            self._bounds.append(
                (
                    min(y for y, _ in vertices),
                    max(y for y, _ in vertices),
                    min(longitudes),
                    max(longitudes),
                )
            )

    def contains(self, lat, lon):
        # Most catalog cells are far away. Reject them without walking polygons
        # on the GUI thread; exact coverage still decides the remaining matches.
        if not any(
            south <= lat <= north
            and west <= lon + 360 * round(((west + east) / 2 - lon) / 360) <= east
            for south, north, west, east in self._bounds
        ):
            return False
        exterior = any(
            polygon_contains(v, lat, lon) for t, v in self.panels if t == "E"
        )
        excluded = any(
            polygon_contains(v, lat, lon) for t, v in self.panels if t == "I"
        )
        return exterior and not excluded

    @property
    def url(self):
        return f"https://www.charts.noaa.gov/ENCs/{self.name}.zip"


def parse_catalog(data):
    cells = []
    for node in ElementTree.fromstring(data).findall("cell"):
        name = node.findtext("name", "")
        if node.findtext("status") != "Active" or not re.fullmatch(
            r"US[A-Z0-9]{6}", name
        ):
            continue
        panels = [
            (
                p.findtext("type"),
                [
                    (float(v.findtext("lat") or ""), float(v.findtext("long") or ""))
                    for v in p.findall("vertex")
                ],
            )
            for p in node.findall("cov/panel")
        ]
        scale = int(node.findtext("cscale", "0"))
        if panels and scale > 0:
            cells.append(
                ENCCell(
                    name,
                    node.findtext("lname", name),
                    scale,
                    int(node.findtext("zipfile_size", "0")),
                    panels,
                )
            )
    if not cells:
        raise ValueError("The NOAA chart catalog contains no active chart coverage.")
    return sorted(cells, key=lambda c: (c.scale, c.name))


COVERAGE_SAMPLE_GRID = 4


def coverage_sample_points(bounds, grid=COVERAGE_SAMPLE_GRID):
    """Bounded interior sample points for a (south, west, north, east) view.

    Sampling the whole visible box keeps a chart that only covers part of the
    view discoverable; the centre alone can fall in a coarser neighbouring cell.
    """
    south, west, north, east = (float(value) for value in bounds)
    grid = max(1, min(8, int(grid)))
    span = east - west
    if span < 0:
        span += 360
    points = []
    for row in range(grid):
        latitude = south + (north - south) * (row + 0.5) / grid
        for column in range(grid):
            longitude = west + span * (column + 0.5) / grid
            longitude = (longitude + 180) % 360 - 180
            points.append((latitude, longitude))
    return points


def missing_chart_for_view(
    cells,
    bounds,
    meters_per_pixel,
    installed=frozenset(),
    enabled=None,
    grid=COVERAGE_SAMPLE_GRID,
    attempted=frozenset(),
):
    """The catalog cell worth fetching automatically for the visible view.

    ``installed`` names every catalog cell already configured, enabled or not,
    so a user-disabled chart is never fetched again. ``enabled`` names the
    cells that currently draw for comparison; the missing cell must outrank the
    best enabled cell covering the same sample point. ``attempted`` names cells
    this session already tried and failed, so a failed fetch cannot shadow the
    next candidate forever. Returns ``None`` when no missing cell improves on
    what is installed.
    """
    if not cells or not meters_per_pixel or meters_per_pixel <= 0:
        return None
    installed = frozenset(installed)
    enabled = installed if enabled is None else frozenset(enabled)
    attempted = frozenset(attempted)
    present = [c for c in cells if c.name in installed and c.name in enabled]
    available = [
        c for c in cells if c.name not in installed and c.name not in attempted
    ]
    if not available:
        return None

    def rank(cell):
        return chart_scale_rank(cell.scale, meters_per_pixel)

    wins = {}
    for latitude, longitude in coverage_sample_points(bounds, grid):
        visible = (
            c
            for c in present
            if c.contains(latitude, longitude)
            and chart_scale_visible(c.scale, meters_per_pixel)
        )
        installed_best = min(visible, key=lambda c: (rank(c), c.name), default=None)
        missing = (
            c
            for c in available
            if c.contains(latitude, longitude)
            and chart_scale_visible(c.scale, meters_per_pixel)
        )
        missing_best = min(missing, key=lambda c: (rank(c), c.name), default=None)
        if missing_best is None:
            continue
        if installed_best is not None and rank(missing_best) >= rank(installed_best):
            continue
        wins[missing_best.name] = wins.get(missing_best.name, 0) + 1
    if not wins:
        return None
    return min(
        (c for c in available if c.name in wins),
        key=lambda c: (-wins[c.name], rank(c), c.name),
    )


class ENCCatalog:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.cells = []
        # The catalog cache file is shared by the manual and the automatic
        # chart operations, which run on separate workers.  This lock only
        # guards reading and writing ``catalog.xml``; it is never held across
        # a network request or a chart download, so one worker's slow fetch
        # cannot block the other.  Downloads serialise per cell instead.
        self._lock = threading.RLock()
        self._cell_locks_guard = threading.Lock()
        self._cell_locks = {}

    def _cell_lock(self, name):
        with self._cell_locks_guard:
            lock = self._cell_locks.get(name)
            if lock is None:
                lock = self._cell_locks[name] = threading.Lock()
            return lock

    def load(self, fetch=True):
        """Called in a worker; keep a usable catalog when the network is unavailable.

        ``fetch=False`` is the automatic path: a cached catalog is used even
        when it is stale, so a stationary view does not pay the network
        timeout again.  The cache is only fetched when there is none at all,
        and a failure then backs the caller off.
        """
        with self._lock:
            self._load(fetch)

    def _load(self, fetch=True):
        self.directory.mkdir(parents=True, exist_ok=True)
        cache = self.directory / "catalog.xml"
        if cache.exists():
            try:
                fresh = time.time() - cache.stat().st_mtime < CACHE_SECONDS
                if self.cells and fresh:
                    return
                self.cells = parse_catalog(cache.read_bytes())
                if fresh or not fetch:
                    return
            except (ValueError, ElementTree.ParseError):
                # An unparsable cache is only refetched on the manual path;
                # the automatic path may still use cells from an earlier load.
                if not fetch:
                    if self.cells:
                        return
                    raise
        elif not fetch:
            # No cache at all: allow the one fetch that creates it.  A failure
            # raises, and the caller's cooldown bounds the retries.
            fetch = True
        try:
            with _get(CATALOG_URL, timeout=(10, 45)) as response:
                response.raise_for_status()
                data = response.content
            cells = parse_catalog(data)
            temporary = cache.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(cache)
            self.cells = cells
        except (requests.RequestException, ValueError, ElementTree.ParseError, OSError):
            if not self.cells:
                raise

    def chart_at(self, lat, lon, meters_per_pixel=None):
        candidates = (c for c in self.cells if c.contains(lat, lon))
        if meters_per_pixel is None:
            return min(candidates, key=lambda c: (c.scale, c.name), default=None)
        candidates = (
            c for c in candidates if chart_scale_visible(c.scale, meters_per_pixel)
        )
        return min(
            candidates,
            key=lambda c: (chart_scale_rank(c.scale, meters_per_pixel), c.name),
            default=None,
        )

    def downloaded_path(self, cell):
        """Find a cached exchange set even if it hasn't been added to Settings."""
        return next(
            (str(p) for p in (self.directory / cell.name).rglob(cell.name + ".000")),
            None,
        )

    def download(self, cell):
        """Stage a complete exchange set (including updates), then install atomically.

        One lock per cell serialises manual and automatic installs of the same
        cell, so neither can write the staged exchange set twice; different
        cells download concurrently.
        """
        if not re.fullmatch(r"US[A-Z0-9]{6}", cell.name):
            raise ValueError("Invalid NOAA chart identifier")
        with self._cell_lock(cell.name):
            return self._download(cell)

    def _download(self, cell):
        destination = self.directory / cell.name
        existing = self.downloaded_path(cell)
        if existing:
            return existing
        self.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.directory) as temporary:
            staging = Path(temporary)
            archive = staging / "chart.zip"
            with _get(cell.url, stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                if urlparse(response.url).hostname not in CHART_HOSTS:
                    raise ValueError("Unexpected chart download host")
                with archive.open("wb") as output:
                    total = 0
                    for chunk in response.iter_content(65536):
                        total += len(chunk)
                        if total > 100 * 1024 * 1024:
                            raise ValueError("Chart download exceeds 100 MB")
                        output.write(chunk)
            unpacked = staging / "unpacked"
            with zipfile.ZipFile(archive) as package:
                if sum(m.file_size for m in package.infolist()) > 300 * 1024 * 1024:
                    raise ValueError("Chart archive is too large")
                for member in package.infolist():
                    target = (unpacked / member.filename).resolve()
                    if not target.is_relative_to(unpacked.resolve()):
                        raise ValueError("Invalid chart archive path")
                package.extractall(unpacked)
            charts = list(unpacked.rglob(cell.name + ".000"))
            if len(charts) != 1:
                raise ValueError("The download does not contain the expected chart")
            relative = charts[0].relative_to(unpacked)
            unpacked.replace(destination)
        return str(destination / relative)
