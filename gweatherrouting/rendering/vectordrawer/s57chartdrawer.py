"""Basic ENC portrayal using the app's existing nautical chart styles."""

import math
from collections import OrderedDict
from types import SimpleNamespace

import cairo
from osgeo import ogr

from gweatherrouting.rendering.style import Style

from .cm93chartdrawer import CM93ChartDrawer, SOUNDING_STYLE

CollectedFeatures = tuple[
    list[SimpleNamespace], list[SimpleNamespace], list[SimpleNamespace]
]


class S57ChartDrawer(CM93ChartDrawer):
    AREA_ORDER = {"DEPARE": 0, "DRGARE": 1, "LNDARE": 2}
    SKIP_LAYERS = {"DSID", "M_COVR", "M_QUAL", "M_NSYS", "M_CSCL", "M_SDAT", "M_VDAT"}
    AREA_LAYERS = {
        "LNDARE",
        "DEPARE",
        "DRGARE",
        "BUAARE",
        "BUISGL",
        "LAKARE",
        "RIVERS",
        "ACHARE",
        "ANCBRT",
        "RESARE",
        "CTNARE",
    }
    ATTRIBUTE_FIELDS = {"DEPARE": ("DRVAL1", "DRVAL2"), "BOYLAT": ("COLOUR",)}

    #: The drawer's own feature cache, in entries.  The cache belongs to the
    #: drawer: it lives and dies with it, and the entry count is what bounds
    #: it.
    FEATURE_CACHE_ENTRIES = 4096

    def draw(self, gpsmap, cr, vector_file, bounding):
        palette = Style.chart_palettes[self.palette]
        cancelled = getattr(gpsmap, "render_cancelled", lambda: False)
        min_lon, max_lon, min_lat, max_lat = bounding.GetEnvelope()
        tolerance = (
            min(
                (max_lon - min_lon) / gpsmap.get_allocated_width(),
                (max_lat - min_lat) / gpsmap.get_allocated_height(),
            )
            * 0.5
        )
        # Quantize downward so cached geometry never exceeds half-pixel error.
        tolerance = 2 ** math.floor(math.log2(tolerance)) if tolerance > 0 else 0
        self._ensure_feature_cache()
        collected = self._collect_layers(vector_file, bounding, tolerance, cancelled)
        if collected is None:
            return
        areas, lines, points = collected
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        for feature in sorted(areas, key=lambda f: self.AREA_ORDER.get(f.obj_name, 3)):
            if cancelled():
                return
            self._render_area(gpsmap, cr, feature, palette)
        for feature in lines:
            if cancelled():
                return
            self._render_line(gpsmap, cr, feature, palette)
        self._sounding_boxes = set()
        for feature in points:
            if cancelled():
                return
            self._render_point(gpsmap, cr, feature, palette, gpsmap.get_scale())

    def _collect_layers(self, vector_file, bounding, tolerance, cancelled):
        collected: CollectedFeatures = ([], [], [])
        for index in range(vector_file.GetLayerCount()):
            layer = vector_file.GetLayerByIndex(index)
            name = layer.GetName()
            if (
                name in self.SKIP_LAYERS
                or layer.GetLayerDefn().GetGeomFieldCount() == 0
            ):
                continue
            layer.SetSpatialFilter(bounding)
            layer.ResetReading()
            fields = self.ATTRIBUTE_FIELDS.get(name, ())
            field_indexes = [
                (key, layer.GetLayerDefn().GetFieldIndex(key)) for key in fields
            ]
            for feature in layer:
                if cancelled():
                    return None
                prepared = self._prepare_feature(
                    vector_file, name, feature, field_indexes, tolerance
                )
                for target, features in zip(collected, prepared):
                    target.extend(features)
        return collected

    def _ensure_feature_cache(self):
        """The drawer's own feature cache, created on the first draw.

        The cache is the drawer's own: it is keyed by the open handle, so it
        is only ever consulted for the handle it was filled from.
        """
        if not hasattr(self, "_feature_cache"):
            self._feature_cache = OrderedDict()

    def _prepare_feature(self, vector_file, name, feature, field_indexes, tolerance):
        cache_key = self._feature_cache_key(vector_file, tolerance, name, feature)
        cached = self._feature_cache_get(cache_key)
        if cached is not None:
            self._feature_cache_touch(cache_key)
            return cached
        collected = self._compute_feature(name, feature, field_indexes, tolerance)
        if collected is None:
            # A feature without a geometry is never prepared and never kept.
            return ([], [], [])
        self._feature_cache_put(cache_key, collected)
        return collected

    def _feature_cache_key(self, vector_file, tolerance, name, feature):
        """The key of one prepared feature.

        The key is the open handle, the tolerance of the view, the layer and
        the feature's own id: the four things the prepared geometry depends
        on.
        """
        return (id(vector_file), tolerance, name, feature.GetFID())

    def _feature_cache_get(self, cache_key):
        """The prepared feature, if the drawer's own cache holds it."""
        return self._feature_cache.get(cache_key)

    def _feature_cache_touch(self, cache_key):
        """The entry is the most recently used one."""
        self._feature_cache.move_to_end(cache_key)

    def _feature_cache_put(self, cache_key, collected):
        """Keep the prepared feature, and drop the oldest one when full."""
        self._feature_cache[cache_key] = collected
        if len(self._feature_cache) > self.FEATURE_CACHE_ENTRIES:
            self._feature_cache.popitem(last=False)

    def _compute_feature(self, name, feature, field_indexes, tolerance):
        """The feature's own geometry, prepared for drawing.

        ``None`` means the feature has no geometry: there is nothing to
        prepare and nothing to keep.
        """
        geom = feature.GetGeometryRef()
        if geom is None:
            return None
        collected: CollectedFeatures = ([], [], [])
        attributes = {
            key: feature.GetField(index) for key, index in field_indexes if index >= 0
        }
        kind = ogr.GT_Flatten(geom.GetGeometryType())
        if kind in (
            ogr.wkbPolygon,
            ogr.wkbMultiPolygon,
            ogr.wkbLineString,
            ogr.wkbMultiLineString,
        ):
            geom = geom.SimplifyPreserveTopology(tolerance)
        self._collect(name, attributes, geom, *collected)
        return collected

    def _collect(self, name, attributes, geom, areas, lines, points):
        kind = ogr.GT_Flatten(geom.GetGeometryType())
        feature = SimpleNamespace(obj_name=name, attributes=attributes, geometry=[])
        if name == "SOUNDG" and kind == ogr.wkbMultiPoint:
            # A sounding group can contain thousands of points. Project it once,
            # rather than allocate numpy arrays and run Mercator for each point.
            coordinates = [
                geom.GetGeometryRef(i).GetPoint()
                for i in range(geom.GetGeometryCount())
            ]
            feature.geometry = [(p[1], p[0], p[2]) for p in coordinates]
            points.append(feature)
        elif kind == ogr.wkbPolygon:
            if name not in self.AREA_LAYERS:
                return
            feature.geometry = [
                [(p[1], p[0]) for p in geom.GetGeometryRef(i).GetPoints()]
                for i in range(geom.GetGeometryCount())
            ]
            areas.append(feature)
        elif kind == ogr.wkbLineString:
            feature.geometry = [(p[1], p[0]) for p in geom.GetPoints()]
            lines.append(feature)
        elif kind == ogr.wkbPoint:
            p = geom.GetPoint()
            feature.geometry = [(p[1], p[0], p[2])]
            points.append(feature)
        else:
            for i in range(geom.GetGeometryCount()):
                self._collect(
                    name, attributes, geom.GetGeometryRef(i), areas, lines, points
                )

    def _render_area(self, gpsmap, cr, feature, palette):
        fill, stroke = self._get_area_styles(
            feature.obj_name, feature.attributes, palette
        )
        cr.new_path()
        # One path with even/odd fill preserves islands and polygon holes.
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        for ring in feature.geometry:
            if len(ring) < 3:
                continue
            xs, ys = self._geo_to_screen_batch(gpsmap, ring)
            cr.move_to(xs[0], ys[0])
            for x, y in zip(xs[1:], ys[1:]):
                cr.line_to(x, y)
            cr.close_path()
        fill.apply(cr)
        cr.fill_preserve()
        stroke.apply(cr)
        cr.stroke()
        cr.set_fill_rule(cairo.FILL_RULE_WINDING)

    def _render_soundings(self, gpsmap, cr, feature):
        if gpsmap.get_scale() > 100:
            return
        SOUNDING_STYLE.apply(cr)
        cr.set_font_size(11)
        xs, ys = self._geo_to_screen_batch(gpsmap, feature.geometry)
        for p, x, y in zip(feature.geometry, xs, ys):
            if not (
                0 <= x < gpsmap.get_allocated_width()
                and 0 <= y < gpsmap.get_allocated_height()
            ):
                continue
            if not math.isfinite(p[2]):
                continue
            box = (int(x // 32), int(y // 16))
            if box in self._sounding_boxes:
                continue
            self._sounding_boxes.add(box)
            cr.move_to(x + 2, y + 2)
            cr.show_text(f"{p[2]:.1f}")

    def _render_point(self, gpsmap, cr, feature, palette, scale):
        # ENC colours account for the regional buoyage system.
        if feature.obj_name == "BOYLAT":
            colours = feature.attributes.get("COLOUR") or []
            colour = str(colours[0]) if colours else ""
            if colour in ("3", "4"):
                feature.attributes["CATLAM"] = 1 if colour == "3" else 2
            else:
                feature.obj_name = "BOYSPP"
        super()._render_point(gpsmap, cr, feature, palette, scale)
