"""Bounded, handle-local repairs and routing validity for shoreline polygons."""

import math
from collections import OrderedDict

import numpy as np
from osgeo import gdal, ogr, osr

try:
    import shapely
except ImportError:  # pragma: no cover - optional acceleration only
    shapely = None


class ShorelineValidity:
    """The GSHHS point/line validity predicate, independent of the Qt drawer.

    ``GSHHSVectorChart`` delegates to one of these, and a scenario worker can
    build the same predicate directly from the installed path without opening
    the chart drawer or any Qt object.
    """

    def __init__(self, path):
        self.path = path
        self.use_native_geometry = True
        driver = ogr.GetDriverByName("ESRI Shapefile")
        self.lpvFile = driver.Open(str(path) + "/GSHHS_shp/h/GSHHS_h_L1.shp")
        if self.lpvFile is None:
            raise ValueError("Missing GSHHS high-resolution shoreline layer")
        self._routing_clips: OrderedDict[tuple, tuple] = OrderedDict()
        self._routing_sources: OrderedDict[tuple, ogr.Geometry] = OrderedDict()
        self._routing_native_clips: OrderedDict = OrderedDict()

    def _routing_source(self, layer_index, feature):
        key = (layer_index, feature.GetFID())
        if key not in self._routing_sources:
            geometry = feature.GetGeometryRef().Clone()
            if not geometry.IsValid():
                geometry = geometry.MakeValid()
            if geometry is None:
                raise ValueError("Unable to repair routing shoreline geometry")
            self._routing_sources[key] = geometry
            if len(self._routing_sources) > 64:
                self._routing_sources.popitem(last=False)
        self._routing_sources.move_to_end(key)
        return self._routing_sources[key]

    def _routing_land(self, west, south, east, north):
        """Cache exact local shoreline geometry, repairing invalid sources."""
        bounds = (
            math.floor(west * 4) / 4 - 0.001,
            math.floor(south * 4) / 4 - 0.001,
            math.ceil(east * 4) / 4 + 0.001,
            math.ceil(north * 4) / 4 + 0.001,
        )
        if bounds in self._routing_clips:
            self._routing_clips.move_to_end(bounds)
            return self._routing_clips[bounds]
        left, bottom, right, top = bounds
        clip = ogr.CreateGeometryFromWkt(
            f"POLYGON (({left} {bottom}, {right} {bottom}, {right} {top}, "
            f"{left} {top}, {left} {bottom}))"
        )
        geometries = []
        for i in range(self.lpvFile.GetLayerCount()):
            layer = self.lpvFile.GetLayerByIndex(i)
            layer.SetSpatialFilterRect(*bounds)
            layer.ResetReading()
            for feature in layer:
                geometry = self._routing_source(i, feature).Intersection(clip)
                if geometry is None:
                    raise ValueError("Unable to clip routing shoreline geometry")
                if not geometry.IsEmpty():
                    geometries.append((geometry, geometry.CreatePreparedGeometry()))
        self._routing_clips[bounds] = tuple(geometries)
        if len(self._routing_clips) > 64:
            self._routing_clips.popitem(last=False)
        return self._routing_clips[bounds]

    def _native_routing_land(self, land):
        """Convert each immutable clipped polygon once for batch predicates."""
        key = id(land)
        if key not in self._routing_native_clips:
            polygons = []
            for geometry, _ in land:
                native = shapely.from_wkb(bytes(geometry.ExportToWkb()))
                shapely.prepare(native)
                polygons.append((*geometry.GetEnvelope(), native))
            # Retain land itself so its identity cannot be reused while cached.
            self._routing_native_clips[key] = (land, tuple(polygons))
            if len(self._routing_native_clips) > 64:
                self._routing_native_clips.popitem(last=False)
        self._routing_native_clips.move_to_end(key)
        return self._routing_native_clips[key][1]

    def points_validity(self, latlons):
        """Screen polygon envelopes in arrays, then use exact containment."""
        if not len(latlons):
            return []
        points = np.asarray(latlons, dtype=float)
        latitudes, longitudes = points[:, 0], points[:, 1]
        land = self._routing_land(
            longitudes.min(), latitudes.min(), longitudes.max(), latitudes.max()
        )
        valid = np.ones(len(points), dtype=bool)
        if shapely is not None and getattr(self, "use_native_geometry", True):
            for west, east, south, north, native in self._native_routing_land(land):
                possible = np.flatnonzero(
                    valid
                    & (longitudes >= west)
                    & (longitudes <= east)
                    & (latitudes >= south)
                    & (latitudes <= north)
                )
                valid[possible] &= ~shapely.contains_xy(
                    native, longitudes[possible], latitudes[possible]
                )
            return valid.tolist()
        for geometry, prepared in land:
            west, east, south, north = geometry.GetEnvelope()
            possible = np.flatnonzero(
                valid
                & (longitudes >= west)
                & (longitudes <= east)
                & (latitudes >= south)
                & (latitudes <= north)
            )
            for i in possible:
                point = ogr.Geometry(ogr.wkbPoint)
                point.AddPoint(float(longitudes[i]), float(latitudes[i]))
                if (prepared or geometry).Contains(point):
                    valid[i] = False
        return valid.tolist()

    def lines_validity(self, latlons, clearance_distance_m=None):
        """Reject land intersections and, when configured, near-shore segments."""
        if not len(latlons):
            return []
        lines = np.asarray(latlons, dtype=float)
        left = np.minimum(lines[:, 1], lines[:, 3])
        right = np.maximum(lines[:, 1], lines[:, 3])
        bottom = np.minimum(lines[:, 0], lines[:, 2])
        top = np.maximum(lines[:, 0], lines[:, 2])
        if clearance_distance_m is None:
            clearance_distance_m = getattr(self, "coastline_clearance_distance_m", 0)
        distance = float(clearance_distance_m or 0)
        lat_margin = distance / 110_000.0
        max_latitude = min(89.999, max(abs(bottom.min()), abs(top.max())) + lat_margin)
        lon_margin = min(
            180.0, lat_margin / max(math.cos(math.radians(max_latitude)), 1e-6)
        )
        land = self._routing_land(
            left.min() - lon_margin,
            bottom.min() - lat_margin,
            right.max() + lon_margin,
            top.max() + lat_margin,
        )
        valid = np.ones(len(lines), dtype=bool)
        if (
            not distance
            and shapely is not None
            and getattr(self, "use_native_geometry", True)
        ):
            segments = None
            for west, east, south, north, native in self._native_routing_land(land):
                possible = np.flatnonzero(
                    valid
                    & (right >= west)
                    & (left <= east)
                    & (top >= south)
                    & (bottom <= north)
                )
                if not len(possible):
                    continue
                if segments is None:
                    segments = shapely.linestrings(lines.reshape(-1, 2, 2)[:, :, ::-1])
                valid[possible] &= ~shapely.intersects(native, segments[possible])
            return valid.tolist()
        for geometry, prepared in land:
            west, east, south, north = geometry.GetEnvelope()
            possible = np.flatnonzero(
                valid
                & (right + lon_margin >= west)
                & (left - lon_margin <= east)
                & (top + lat_margin >= south)
                & (bottom - lat_margin <= north)
            )
            for i in possible:
                lat1, lon1, lat2, lon2 = lines[i]
                line = ogr.Geometry(ogr.wkbLineString)
                line.AddPoint(float(lon1), float(lat1))
                line.AddPoint(float(lon2), float(lat2))
                if not distance and (prepared or geometry).Intersects(line):
                    valid[i] = False
                elif distance:
                    lat0 = (lat1 + lat2) / 2
                    lon0 = (lon1 + lon2) / 2
                    source = osr.SpatialReference()
                    source.ImportFromEPSG(4326)
                    source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    projected = osr.SpatialReference()
                    projected.SetFromUserInput(
                        f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} "
                        "+datum=WGS84 +units=m +no_defs"
                    )
                    projected.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    transform = osr.CoordinateTransformation(source, projected)
                    local_land = geometry.Clone()
                    local_line = line.Clone()
                    if (
                        local_land.Transform(transform) != 0
                        or local_line.Transform(transform) != 0
                    ):
                        raise ValueError(
                            "Unable to project coastline for clearance check"
                        )
                    if local_land.Distance(local_line) <= distance:
                        valid[i] = False
        return valid.tolist()


class ShorelineGeometryCache:
    def __init__(self):
        self.sources = OrderedDict()
        self.clips = OrderedDict()

    def clip(self, key, geometry, bounds):
        if key not in self.sources:
            if geometry is None:
                raise ValueError("Missing shoreline geometry")
            source = geometry.Clone()
            # Invalid global polygons can fail even when the viewport is far
            # from their self-intersection. Repair before any intersection.
            gdal.PushErrorHandler("CPLQuietErrorHandler")
            try:
                if not source.IsValid():
                    source = source.MakeValid()
            finally:
                gdal.PopErrorHandler()
            if source is None:
                raise ValueError("Unable to repair shoreline geometry")
            self.sources[key] = source
            if len(self.sources) > 64:
                self.sources.popitem(last=False)
        self.sources.move_to_end(key)
        # Keep the exact clip shape in the key: rounding bounds can reuse a
        # clip that excludes newly visible coastline at a viewport edge.
        clip_key = (key, bytes(bounds.ExportToWkb()))
        if clip_key in self.clips:
            self.clips.move_to_end(clip_key)
            return self.clips[clip_key]
        source = self.sources[key]
        west, east, south, north = source.GetEnvelope()
        bwest, beast, bsouth, bnorth = bounds.GetEnvelope()
        if (
            bwest <= west <= east <= beast
            and bsouth <= south <= north <= bnorth
            and bounds.Contains(source)
        ):
            clipped = source
        else:
            clipped = source.Intersection(bounds)
        if clipped is None:
            raise ValueError("Unable to clip repaired shoreline geometry")
        self.clips[clip_key] = clipped
        if len(self.clips) > 256:
            self.clips.popitem(last=False)
        return clipped
