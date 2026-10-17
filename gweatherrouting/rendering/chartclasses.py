"""Display-only chart class switches, independent of per-file enablement."""

from gweatherrouting.common import resource_path

from .gdalrasterchart import GDALRasterChart
from .gdalvectorchart import GDALVectorChart
from .gshhs import GSHHSVectorChart
from .mbtilesrenderer import MBTilesChart
from .vectordrawer.cm93chartdrawer import CM93ChartDrawer
from .vectordrawer.osmchartdrawer import OSMChartDrawer
from .vectordrawer.s57chartdrawer import S57ChartDrawer

CHART_CLASSES = (
    ("world", "chartWorldVisible", "World map / coastlines"),
    ("enc", "chartENCVisible", "Local ENC charts"),
    ("raster", "chartRasterVisible", "Raster charts (KAP / BSB / GeoTIFF)"),
    ("mbtiles", "chartMBTilesVisible", "Offline MBTiles charts"),
    ("cm93", "chartCM93Visible", "CM93 charts"),
    ("seamarks", "chartSeamarksVisible", "OpenStreetMap seamarks"),
    ("vector", "chartVectorVisible", "Other vector charts"),
)
CLASS_SETTINGS = {kind: setting for kind, setting, _ in CHART_CLASSES}


def chart_class(chart):
    if isinstance(chart, GSHHSVectorChart) or getattr(
        chart, "path", None
    ) == resource_path("gweatherrouting", "data/countries.geojson"):
        return "world"
    if isinstance(chart, GDALRasterChart):
        return "raster"
    if isinstance(chart, MBTilesChart):
        return "mbtiles"
    if isinstance(chart, GDALVectorChart):
        drawer = getattr(chart, "drawer", None)
        if isinstance(drawer, S57ChartDrawer):
            return "enc"
        if isinstance(drawer, CM93ChartDrawer):
            return "cm93"
        if isinstance(drawer, OSMChartDrawer):
            return "seamarks"
    return "vector"


def chart_class_enabled(settings, chart):
    return bool(settings.get(CLASS_SETTINGS[chart_class(chart)], True))
