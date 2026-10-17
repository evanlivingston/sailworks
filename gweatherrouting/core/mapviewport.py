"""Toolkit-independent Mercator coordinates used by the map canvas."""

import math
from typing import Callable

import numpy as np


class MapPoint:
    """Replacement for OsmGpsMap.MapPoint - represents a geographic coordinate."""

    def __init__(self, lat, lon):
        self._lat = float(lat)
        self._lon = float(lon)

    @staticmethod
    def new_degrees(lat, lon):
        return MapPoint(lat, lon)

    def get_degrees(self):
        return (self._lat, self._lon)

    @property
    def rlat(self):
        return math.radians(self._lat)

    @property
    def rlon(self):
        return math.radians(self._lon)


class MercatorProjection:
    TILE_SIZE = 256
    MAX_LAT = 85.051129
    _center_lat: float
    _center_lon: float
    _zoom: float

    def get_allocated_width(self):
        raise NotImplementedError

    def get_allocated_height(self):
        raise NotImplementedError

    def _lat_to_y(self, lat):
        """Convert latitude to Mercator world-pixel Y at current zoom."""
        lat = max(-self.MAX_LAT, min(self.MAX_LAT, lat))
        n = 2.0**self._zoom
        lat_rad = math.radians(lat)
        return (
            (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi)
            / 2.0
            * n
            * self.TILE_SIZE
        )

    def _lon_to_x(self, lon):
        """Convert longitude to Mercator world-pixel X at current zoom."""
        n = 2.0**self._zoom
        return (lon + 180.0) / 360.0 * n * self.TILE_SIZE

    def _y_to_lat(self, y):
        """Convert Mercator world-pixel Y to latitude at current zoom."""
        n = 2.0**self._zoom
        lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * y / (n * self.TILE_SIZE))))
        return math.degrees(lat_rad)

    def _x_to_lon(self, x):
        """Convert Mercator world-pixel X to longitude at current zoom."""
        n = 2.0**self._zoom
        return x / (n * self.TILE_SIZE) * 360.0 - 180.0

    def get_bbox(self):
        """Return (top-left MapPoint, bottom-right MapPoint) of visible area."""
        w = self.get_allocated_width()
        h = self.get_allocated_height()

        cx = self._lon_to_x(self._center_lon)
        cy = self._lat_to_y(self._center_lat)

        top_lat = self._y_to_lat(cy - h / 2.0)
        left_lon = self._x_to_lon(cx - w / 2.0)
        bottom_lat = self._y_to_lat(cy + h / 2.0)
        right_lon = self._x_to_lon(cx + w / 2.0)

        return (MapPoint(top_lat, left_lon), MapPoint(bottom_lat, right_lon))

    def get_scale(self):
        """Return approximate meters per pixel at the map center.

        This matches OsmGpsMap's get_scale() behaviour used throughout
        the codebase for zoom-dependent rendering decisions.
        """
        lat_rad = math.radians(self._center_lat)
        # Circumference of Earth at this latitude / total pixels at zoom
        meters_per_pixel = (
            math.cos(lat_rad) * 2 * math.pi * 6378137 / (2**self._zoom * self.TILE_SIZE)
        )
        return meters_per_pixel

    def convert_geographic_to_screen(self, map_point):
        """Convert MapPoint to screen (x, y) pixel coordinates."""
        lat, lon = map_point.get_degrees()

        w = self.get_allocated_width()
        h = self.get_allocated_height()

        cx = self._lon_to_x(self._center_lon)
        cy = self._lat_to_y(self._center_lat)

        px = self._lon_to_x(lon)
        py = self._lat_to_y(lat)

        sx = px - cx + w / 2.0
        sy = py - cy + h / 2.0

        return (sx, sy)

    def convert_geographic_to_screen_batch(self, lats, lons):
        """Batch-convert numpy arrays of lat/lon to screen (x, y) arrays.

        Args:
            lats: numpy float64 array of latitudes
            lons: numpy float64 array of longitudes

        Returns:
            (xs, ys): tuple of numpy float64 arrays of screen coordinates
        """
        w = self.get_allocated_width()
        h = self.get_allocated_height()

        n = 2.0**self._zoom
        tile_n = n * self.TILE_SIZE

        # Center offsets (scalar)
        cx = (self._center_lon + 180.0) / 360.0 * tile_n
        clat_rad = math.radians(max(-self.MAX_LAT, min(self.MAX_LAT, self._center_lat)))
        cy = (
            (1.0 - math.log(math.tan(clat_rad) + 1.0 / math.cos(clat_rad)) / math.pi)
            / 2.0
            * tile_n
        )

        half_w = w / 2.0
        half_h = h / 2.0

        # Batch lon -> x
        px = (lons + 180.0) / 360.0 * tile_n

        # Batch lat -> y (Mercator)
        lat_clamped = np.clip(lats, -self.MAX_LAT, self.MAX_LAT)
        lat_rad = np.radians(lat_clamped)
        py = (
            (1.0 - np.log(np.tan(lat_rad) + 1.0 / np.cos(lat_rad)) / math.pi)
            / 2.0
            * tile_n
        )

        xs = px - cx + half_w
        ys = py - cy + half_h

        return xs, ys

    def convert_screen_to_geographic(self, sx, sy):
        """Convert screen (x, y) to MapPoint."""
        w = self.get_allocated_width()
        h = self.get_allocated_height()

        cx = self._lon_to_x(self._center_lon)
        cy = self._lat_to_y(self._center_lat)

        px = cx + sx - w / 2.0
        py = cy + sy - h / 2.0

        lat = self._y_to_lat(py)
        lon = self._x_to_lon(px)

        return MapPoint(lat, lon)


class MapViewport(MercatorProjection):
    """Frozen map projection for rendering a reusable chart image with a margin."""

    def __init__(self, source, margin=0, on_redraw=None):
        self._center_lat = source._center_lat
        self._center_lon = source._center_lon
        self._zoom = source._zoom
        self.width = source.get_allocated_width() + 2 * margin
        self.height = source.get_allocated_height() + 2 * margin
        self.queue_draw = on_redraw or source.queue_draw
        self.render_cancelled: Callable[[], bool] = lambda: False

    def get_allocated_width(self):
        return self.width

    def get_allocated_height(self):
        return self.height
