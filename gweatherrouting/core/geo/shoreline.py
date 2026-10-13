"""Bounded, handle-local repairs for installed shoreline polygons."""

from collections import OrderedDict

from osgeo import gdal


class ShorelineGeometryCache:
    def __init__(self):
        self.sources = OrderedDict()

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
        clipped = self.sources[key].Intersection(bounds)
        if clipped is None:
            raise ValueError("Unable to clip repaired shoreline geometry")
        return clipped
