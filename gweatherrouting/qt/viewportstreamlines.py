"""Viewport-owned cancellable streamline tracing and packed image payloads."""

import math
from threading import Condition, Event, Thread

import numpy as np
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

from gweatherrouting.rendering.streamlinepaths import trace_streamline_paths
from gweatherrouting.rendering.weatherbarbs import _view_bounds

MAX_TEXTURE_SIZE = 16384
MAX_BATCH_SIZE = 8192
MAX_GRID_AXIS = 512
GRID_SPACING_PX = 4


def encode_segments(segments):
    """Pack normalized xy endpoints as 8-bit RGB texels (no alpha payload)."""
    segments = np.asarray(segments, dtype=np.float64).reshape((-1, 4))
    if not len(segments):
        return QImage(), 0
    if len(segments) > MAX_BATCH_SIZE:
        raise ValueError("Streamline segment batch exceeds supported height")
    coordinates = np.rint(np.clip(segments, 0.0, 1.0) * 65535).astype(">u2")
    raw = np.zeros((len(segments), 9), dtype=np.uint8)
    raw[:, :8] = coordinates.view(np.uint8).reshape((-1, 8))
    image = QImage(
        raw.tobytes(), 3, len(segments), 9, QImage.Format.Format_RGB888
    ).copy()
    return image, len(segments)


class StreamlineImageProvider(QQuickImageProvider):
    """Retain published images until the owning QML generation is withdrawn."""

    def __init__(self):
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._images = {}
        self._generation = 0
        self._generations = {}
        self._active_generation = 0

    def publish(self, key, segments):
        image, count = encode_segments(segments)
        token = f"{self._active_generation}_{self._generation}_{key}"
        self._generation += 1
        self._images[token] = image
        self._generations.setdefault(self._active_generation, set()).add(token)
        return f"image://streamlines/{token}", count

    def replace_generation(self):
        """Start a new publication and release prior URLs after QML withdrawal."""
        self._images.clear()
        self._generations.clear()
        self._active_generation += 1

    def retain(self, urls):
        """Release old camera images once QML has loaded the current sources."""
        tokens = {url.rsplit("/", 1)[-1] for url in urls}
        for token in tuple(self._images):
            if token not in tokens:
                del self._images[token]
        for generation, owned in tuple(self._generations.items()):
            owned.intersection_update(tokens)
            if not owned:
                del self._generations[generation]

    def requestImage(self, image_id, size, requested_size):  # noqa: N802
        image = self._images.get(image_id, QImage())
        if size is not None:
            size.setWidth(image.width())
            size.setHeight(image.height())
        return image

    def clear(self):
        self._images.clear()
        self._generations.clear()


def clip_segments(segments, width, height):
    """Clip line segments to the viewport without changing their direction."""
    values = np.asarray(segments, dtype=float).reshape((-1, 4))
    result = []
    for x0, y0, x1, y1 in values:
        dx, dy = x1 - x0, y1 - y0
        low, high = 0.0, 1.0
        for p, q in ((-dx, x0), (dx, width - x0), (-dy, y0), (dy, height - y0)):
            if p == 0:
                if q < 0:
                    break
                continue
            t = q / p
            if p < 0:
                low = max(low, t)
            else:
                high = min(high, t)
            if low > high:
                break
        else:
            result.append(
                (x0 + low * dx, y0 + low * dy, x0 + high * dx, y0 + high * dy)
            )
    return np.asarray(result, dtype=float).reshape((-1, 4))


def wind_columns(sampler, camera, cancelled):
    """Sample a bounded screen-space lattice, retaining NaN coverage holes.

    Native rotated/curvilinear grids have distinct coordinates per node; their
    unique latitude and longitude axes do not form a useful Cartesian grid.
    Four-pixel view spacing also bounds worker sampling independently of the
    source resolution while keeping the tracing grid finer than the seeds.
    """
    bounds = _view_bounds(camera.view())
    if not any(len(index.nodes(bounds)) for index in sampler.indices):
        raise ValueError("No wind nodes in view")
    south, north = sorted((bounds[0][0], bounds[1][0]))
    west, east = bounds[0][1], bounds[1][1]
    if east < west:
        east += 360
    if not (north > south and east > west):
        raise ValueError("Streamline viewport has no geographic area")
    nrows = min(
        MAX_GRID_AXIS, max(2, math.ceil((camera.height + 64) / GRID_SPACING_PX))
    )
    ncols = min(MAX_GRID_AXIS, max(2, math.ceil((camera.width + 64) / GRID_SPACING_PX)))
    lats = np.linspace(south, north, nrows)
    lons = np.linspace(west, east, ncols)
    latitude, longitude = np.meshgrid(lats, lons)
    uv = np.asarray(
        sampler.sample(
            "wind", camera.time, latitude.ravel(), longitude.ravel(), cancelled
        )
    ).reshape(ncols, nrows, 2)
    speed = np.hypot(uv[..., 0], uv[..., 1])
    direction = np.degrees(np.arctan2(-uv[..., 0], -uv[..., 1])) % 360
    return [(float(lon), lats, direction[i], speed[i]) for i, lon in enumerate(lons)]


class ViewportStreamlineModel:
    """Latest-camera-only worker; forecast handles never leave its thread."""

    def __init__(self, paths, display_identity=None):
        self.paths = tuple(paths)
        self.display_identity = display_identity
        self.on_change = None
        self.error = ""
        self._condition = Condition()
        self._thread = None
        self._closed = False
        self._pending = None
        self._cancel = Event()
        self._key = None
        self._state = "hidden"
        self._paths = None

    def update(self, camera, enabled=True):
        with self._condition:
            key = camera if enabled and not self._closed else None
            if key != self._key:
                self._cancel.set()
                self._cancel = Event()
                self._key, self._paths, self.error = key, None, ""
                self._state = "loading" if key is not None else "hidden"
                self._pending = (key, self._cancel) if key is not None else None
                if key is not None and self._thread is None:
                    self._thread = Thread(
                        target=self._run, daemon=True, name="viewport-streamlines"
                    )
                    self._thread.start()
                self._condition.notify_all()
            return self._state, self._paths

    def _complete(self, camera, cancel, paths, error):
        with self._condition:
            if self._closed or cancel.is_set() or camera != self._key:
                return
            self._paths, self.error = paths, error
            if not error and not paths:
                self.error = "No traceable wind cells in selected forecast"
            self._state = "unavailable" if self.error else "ready"
            if self.on_change is not None:
                self.on_change()

    def _run(self):
        from gweatherrouting.core.grib import Grib
        from gweatherrouting.core.gribsampling import WeatherSampler
        from gweatherrouting.rendering.weathercache import (
            DecodedBandCache,
            DisplayWeatherSource,
        )
        from gweatherrouting.rendering.windrenderer import WindLandMask

        sources = []
        sampler = None
        initialization_error = ""
        try:
            try:
                bands = DecodedBandCache()
                for path in self.paths:
                    meta = Grib.parse_metadata(path)
                    sources.append(
                        DisplayWeatherSource(
                            meta.path,
                            meta.name,
                            meta.centre,
                            meta.bounds,
                            meta.start_time,
                            meta.last_forecast,
                            meta.model_name,
                            band_cache=bands,
                            display_identity=self.display_identity,
                        )
                    )
                sampler = WeatherSampler(sources)
                if not sources or len(sampler.indices) != len(sources):
                    raise ValueError("Selected wind forecast unavailable")
                land = WindLandMask()
            except Exception as exc:
                initialization_error = str(exc)
            while True:
                with self._condition:
                    while self._pending is None and not self._closed:
                        self._condition.wait()
                    if self._closed:
                        return
                    camera, cancel = self._pending
                    self._pending = None
                paths, error = None, initialization_error
                if not error:
                    try:
                        columns = wind_columns(sampler, camera, cancel.is_set)
                        land.refresh_source(camera.view().get_scale())
                        paths = trace_streamline_paths(
                            camera.view(),
                            columns,
                            land.contains,
                            cancel.is_set,
                            density=camera.density,
                        )
                        if paths is None and not cancel.is_set():
                            error = "No traceable wind cells in selected forecast"
                    except Exception as exc:
                        error = str(exc)
                self._complete(camera, cancel, paths, error)
        finally:
            for source in sources:
                source.close()

    def close(self):
        with self._condition:
            self._closed = True
            self._cancel.set()
            self._pending = self._paths = self._key = None
            self._state = "hidden"
            self.on_change = None
            self._condition.notify_all()
