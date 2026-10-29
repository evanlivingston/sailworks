"""Native-grid identity and requested-extent checks for forecast validation."""

import math

from osgeo import osr

from .forecastcapabilities import MODEL_CAPABILITIES


class ForecastGridError(ValueError):
    """The raster CRS, affine grid, or requested extent is unsupported."""


def _transform(projection):
    source = osr.SpatialReference()
    if not projection or source.ImportFromWkt(projection) != 0:
        raise ForecastGridError("forecast grid has no valid CRS")
    target = osr.SpatialReference()
    target.ImportFromEPSG(4326)
    if hasattr(source, "SetAxisMappingStrategy"):
        source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    try:
        forward = osr.CoordinateTransformation(target, source)
    except Exception as exc:
        raise ForecastGridError("unsupported forecast CRS") from exc
    return source, forward


def _inside(gt, forward, lon, lat, width, height, wrap_longitude):
    try:
        x, y, *_ = forward.TransformPoint(lon, lat)
    except Exception as exc:
        raise ForecastGridError(
            "requested region cannot be transformed to grid"
        ) from exc
    a, b, c, d, e, f = gt
    det = b * f - c * e
    shifts = (0.0, 360.0, -360.0) if wrap_longitude else (0.0,)
    for shift in shifts:
        px = (f * (x + shift - a) - c * (y - d)) / det
        py = (-e * (x + shift - a) + b * (y - d)) / det
        if -1e-6 <= px <= width + 1e-6 and -1e-6 <= py <= height + 1e-6:
            return
    raise ForecastGridError("forecast grid does not contain requested region")


def validate_grid(dataset, model, requested):
    """Validate supported native resolution and sample requested boundary.

    The 64-segment boundary test checks geometric extent only; it cannot prove
    that every interior cell is present or non-nodata.
    """
    gt = dataset.GetGeoTransform()
    if gt is None or len(gt) != 6 or not all(math.isfinite(v) for v in gt):
        raise ForecastGridError("forecast grid has an invalid geotransform")
    determinant = gt[1] * gt[5] - gt[2] * gt[4]
    if abs(determinant) < 1e-15:
        raise ForecastGridError("forecast grid has a singular geotransform")
    width, height = dataset.RasterXSize, dataset.RasterYSize
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ForecastGridError("forecast grid has invalid dimensions")
    projection = dataset.GetProjection()
    source, forward = _transform(projection)
    geographic = bool(source.IsGeographic())
    derived = (
        bool(source.IsDerivedGeographic())
        if hasattr(source, "IsDerivedGeographic")
        else False
    )
    x_axis, y_axis = math.hypot(gt[1], gt[4]), math.hypot(gt[2], gt[5])
    if model == "GFS":
        if (
            not geographic
            or derived
            or abs(x_axis - 0.25) > 1e-6
            or abs(y_axis - 0.25) > 1e-6
            or gt[2]
            or gt[4]
        ):
            raise ForecastGridError(
                "GFS grid is not the supported 0.25 degree geographic grid"
            )
        resolution = "0.25deg"
    elif model in ("IFS", "AIFS", "AIGFS", "GFSWAVE"):
        if (
            not MODEL_CAPABILITIES[model].global_download
            or not geographic
            or derived
            or width != 1440
            or height != 721
            or abs(gt[1] - 0.25) > 1e-6
            or abs(gt[5] + 0.25) > 1e-6
            or gt[2]
            or gt[4]
            or abs((gt[0] + gt[1] * 0.5) + 180.0) > 1e-6
            or abs((gt[0] + gt[1] * (width - 0.5)) - 179.75) > 1e-6
            or abs((gt[3] + gt[5] * 0.5) - 90.0) > 1e-6
            or abs((gt[3] + gt[5] * (height - 0.5)) + 90.0) > 1e-6
        ):
            raise ForecastGridError(
                f"{model} grid is not the verified global 0.25 degree grid"
            )
        resolution = "0.25deg"
    elif model == "HRRR":
        if (
            geographic
            or derived
            or abs(source.GetLinearUnits() - 1.0) > 1e-9
            or max(abs(x_axis - 3000), abs(y_axis - 3000)) > 1
        ):
            raise ForecastGridError(
                "HRRR grid is not the supported 3 km projected grid"
            )
        resolution = "3km"
    elif model == "HRDPS":
        if (
            not geographic
            or not derived
            or abs(x_axis - 0.0225) > 1e-6
            or abs(y_axis - 0.0225) > 1e-6
            or "Pole rotation" not in projection
        ):
            raise ForecastGridError(
                "HRDPS grid is not the supported rotated 0.0225 degree grid"
            )
        resolution = "0.0225deg-rotated"
    else:
        raise ForecastGridError(f"Unsupported forecast grid model: {model}")

    south, west, north, east = requested
    intervals = ((west, east),) if west <= east else ((west, 180.0), (-180.0, east))
    for left, right in intervals:
        for index in range(65):
            fraction = index / 64
            lon = left + fraction * (right - left)
            for lat in (south, north):
                _inside(
                    gt, forward, lon, lat, width, height, geographic and not derived
                )
            lat = south + fraction * (north - south)
            for lon in (left, right):
                _inside(
                    gt, forward, lon, lat, width, height, geographic and not derived
                )
    return resolution
