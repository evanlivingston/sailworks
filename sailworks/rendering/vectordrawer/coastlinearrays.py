"""Process-wide, byte-bounded cache of unmodified shoreline coordinates."""

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
import time

import numpy as np
from osgeo import ogr


@dataclass
class CoastlineArrays:
    # Each entry is (layer index, FID, polygons), or polygons=None if unsupported.
    features: list
    bounds: np.ndarray
    nbytes: int


class CoastlineArrayCache:
    def __init__(self, budget=192 * 1024 * 1024):
        self.budget = budget
        self.datasets = OrderedDict()
        self.too_large = set()
        self.nbytes = 0
        self._lock = RLock()

    def key(self, dataset):
        layers = [dataset.GetLayerByIndex(i) for i in range(dataset.GetLayerCount())]
        # Feature counts must not depend on a previous viewport filter.
        for layer in layers:
            layer.SetSpatialFilter(None)
        return (
            dataset.GetDescription(),
            tuple(layer.GetFeatureCount() for layer in layers),
        )

    def peek(self, dataset):
        """Return a cached dataset without extracting anything."""
        with self._lock:
            key = self.key(dataset)
            if key in self.datasets:
                self.datasets.move_to_end(key)
                return self.datasets[key]
            return None

    def get(self, dataset):
        with self._lock:
            key = self.key(dataset)
            layers = [
                dataset.GetLayerByIndex(i) for i in range(dataset.GetLayerCount())
            ]
            if key in self.too_large:
                return None
            if key in self.datasets:
                self.datasets.move_to_end(key)
                return self.datasets[key]
            features, bounds = [], []
            size = 0

            def polygons(geometry):
                name = geometry.GetGeometryName()
                if name == "POLYGON":
                    rings = []
                    for i in range(geometry.GetGeometryCount()):
                        points = np.asarray(
                            geometry.GetGeometryRef(i).GetPoints(), dtype=np.float64
                        )
                        if (
                            len(points) < 4
                            or points.ndim != 2
                            or not np.array_equal(points[0, :2], points[-1, :2])
                            or not np.isfinite(points[:, :2]).all()
                        ):
                            return None
                        rings.append((points[:, 0].copy(), points[:, 1].copy()))
                    return [rings] if rings else None
                if name in ("MULTIPOLYGON", "GEOMETRYCOLLECTION"):
                    result = []
                    for i in range(geometry.GetGeometryCount()):
                        part = polygons(geometry.GetGeometryRef(i))
                        if part is None:
                            return None
                        result.extend(part)
                    return result or None
                return None

            for index, layer in enumerate(layers):
                layer.ResetReading()
                for feature in layer:
                    if len(features) % 2000 == 0:
                        # Extraction runs on a warm thread while the UI draws;
                        # yielding keeps the GIL from starving the worker.
                        time.sleep(0.001)
                    geometry = feature.GetGeometryRef()
                    envelope = (
                        geometry.GetEnvelope()
                        if geometry and not geometry.IsEmpty()
                        else None
                    )
                    parts = None
                    if envelope and envelope[1] - envelope[0] <= 180:
                        # Validity repair is rare (1 of 179833 polygons in the
                        # full tier) and IsValid costs as much as extraction
                        # itself, so odd geometry is left to the clip and the
                        # per-feature fallback instead.
                        parts = polygons(geometry)
                    if parts:
                        size += sum(
                            lon.nbytes + lat.nbytes
                            for polygon in parts
                            for lon, lat in polygon
                        )
                    # Include bounding boxes and a conservative metadata allowance.
                    size += 32 + 128
                    if size > self.budget:
                        self.too_large.add(key)
                        return None
                    features.append((index, feature.GetFID(), parts))
                    bounds.append(envelope if envelope else (-180, 180, -90, 90))
            result = CoastlineArrays(
                features, np.asarray(bounds, dtype=np.float64).reshape(-1, 4), size
            )
            while self.datasets and self.nbytes + size > self.budget:
                _, evicted = self.datasets.popitem(last=False)
                self.nbytes -= evicted.nbytes
            self.datasets[key] = result
            self.nbytes += size
            return result

    def get_by_path(self, path):
        """Extract from an independent handle so a warm thread never shares one."""
        dataset = ogr.Open(path)
        if dataset is None:
            return None
        return self.get(dataset)


COASTLINE_ARRAYS = CoastlineArrayCache()


def _clip_half(points, axis, value, keep_greater):
    """Clip a closed ring against one axis-aligned half plane, vectorised."""
    coordinate = points[:, axis]
    inside = coordinate >= value if keep_greater else coordinate <= value
    if inside.all():
        return points
    if not inside.any():
        return np.empty((0, 2))

    following = np.roll(points, -1, axis=0)
    following_inside = np.roll(inside, -1)
    crossing = inside != following_inside

    delta = following - points
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = (value - coordinate) / delta[:, axis]
    ratio = np.where(np.isfinite(ratio), ratio, 0.0)
    intersection = points + delta * ratio[:, None]

    slots = np.empty((len(points), 2, 2))
    slots[:, 0] = points
    slots[:, 1] = intersection
    keep = np.empty((len(points), 2), dtype=bool)
    keep[:, 0] = inside
    keep[:, 1] = crossing
    return slots[keep]


def clip_ring(xs, ys, xmin, ymin, xmax, ymax):
    """Clip a closed ring to an axis-aligned rect; None when nothing remains."""
    points = np.column_stack((xs, ys))
    points = _clip_half(points, 0, float(xmin), True)
    if len(points) < 3:
        return None
    points = _clip_half(points, 0, float(xmax), False)
    if len(points) < 3:
        return None
    points = _clip_half(points, 1, float(ymin), True)
    if len(points) < 3:
        return None
    points = _clip_half(points, 1, float(ymax), False)
    if len(points) < 3:
        return None
    return points
