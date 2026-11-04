"""Bounded, handle-local repairs for installed shoreline polygons."""

from collections import OrderedDict

from osgeo import gdal


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
