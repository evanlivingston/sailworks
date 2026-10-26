"""Strict validation of downloaded forecast files before tranche publication."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

from osgeo import gdal

from .grib import PRESSURE_ELEMENTS, forecast_model_name
from .gribsampling import model_key, number
from .gribvectors import vector_flags
from .forecastgrid import ForecastGridError, validate_grid
from .forecastcapabilities import MODEL_CAPABILITIES
from .forecastrefreshplan import RefreshCancelledError
from .gribfields import GUST_ELEMENTS


class ForecastValidationError(ValueError):
    """A downloaded file does not satisfy its immutable request."""


@dataclass(frozen=True)
class ValidatedModel:
    model_id: str
    run_time: datetime
    member: str
    fields: tuple[str, ...]
    levels: tuple[str, ...]
    region: tuple[float, float, float, float]
    resolution: str
    time_steps: tuple[datetime, ...]
    field_times: tuple[tuple[str, tuple[datetime, ...]], ...]


_LEVELS = {
    "UGRD": "10-HTGL",
    "VGRD": "10-HTGL",
    "PRMSL": "0-MSL",
    "MSLMA": "0-MSL",
    "PRES": "0-MSL",
    "GUST": "0-SFC",
}
_FIELD_BY_ELEMENT = {
    "UGRD": "wind",
    "VGRD": "wind",
    "PRMSL": "pressure",
    "MSLMA": "pressure",
    "PRES": "pressure",
    **{element: "gust" for element in GUST_ELEMENTS},
}


def _fail(message):
    raise ForecastValidationError(message)


def _cancel(cancel):
    if cancel is None:
        return
    if callable(cancel):
        if cancel():
            raise RefreshCancelledError("Forecast validation cancelled")
    elif getattr(cancel, "is_set", lambda: False)():
        raise RefreshCancelledError("Forecast validation cancelled")


def _planned_model(planned):
    config = planned.configuration
    model = config.model_id
    if model not in MODEL_CAPABILITIES:
        _fail(f"unsupported forecast model: {model}")
    capability = MODEL_CAPABILITIES.get(model)
    if capability is None or planned.provider_name != capability.provider_name:
        _fail("planned provider does not match model")
    if any(not capability.available_steps(None, field) for field in config.fields):
        _fail("model does not support a requested forecast field")
    native = capability.native_resolution
    if not isinstance(planned.identifier, str) or not planned.identifier:
        _fail("planned identifier is missing")
    if planned.native_resolution != native:
        _fail("planned native resolution does not match model")
    if config.resolution not in ("native", native):
        _fail("requested resolution is unsupported for model")
    return config


def _utc_epoch(value, label):
    try:
        seconds = number(value)
        if not math.isfinite(seconds) or not seconds.is_integer():
            _fail(f"invalid {label}")
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError, IndexError):
        _fail(f"invalid {label}")


def _seconds(value, label):
    try:
        seconds = number(value)
    except (TypeError, ValueError, IndexError):
        _fail(f"invalid {label}")
    if not math.isfinite(seconds) or not seconds.is_integer():
        _fail(f"invalid {label}")
    return int(seconds)


def _decode_band(band, dataset, index, cancel=None):
    """Decode in bounded blocks; nodata samples remain valid decoded data."""
    width = getattr(band, "XSize", dataset.RasterXSize)
    height = getattr(band, "YSize", dataset.RasterYSize)
    if width != dataset.RasterXSize or height != dataset.RasterYSize:
        _fail(f"band {index} dimensions differ from forecast grid")
    try:
        sample_bytes = (gdal.GetDataTypeSize(band.DataType) + 7) // 8
    except Exception as exc:
        raise ForecastValidationError(
            f"band {index} has unsupported data type"
        ) from exc
    if sample_bytes <= 0:
        _fail(f"band {index} has unsupported data type")
    try:
        for y in range(0, height, 256):
            _cancel(cancel)
            rows = min(256, height - y)
            for x in range(0, width, 256):
                _cancel(cancel)
                columns = min(256, width - x)
                data = band.ReadRaster(
                    x,
                    y,
                    columns,
                    rows,
                    buf_xsize=columns,
                    buf_ysize=rows,
                    buf_type=band.DataType,
                )
                if data is None or len(data) != rows * columns * sample_bytes:
                    _fail(f"band {index} contains truncated or undecodable raster data")
    except ForecastValidationError:
        raise
    except RefreshCancelledError:
        raise
    except Exception as exc:
        raise ForecastValidationError(f"band {index} raster decode failed") from exc


def inspect_download(path, planned, cancel=None, *, opener=None, vector_reader=None):
    """Inspect every supported band and reject incomplete or incoherent files.

    Region containment is checked on 64 samples per requested boundary edge;
    this validates geometric extent, not every interior cell's nodata mask.
    """
    config = _planned_model(planned)
    model = config.model_id
    fields = tuple(config.fields)
    if planned.member != "deterministic":
        _fail("only deterministic forecast members are supported")
    field_steps = tuple(planned.field_steps)
    requested_steps = dict(field_steps)
    if len(requested_steps) != len(field_steps) or set(requested_steps) != set(fields):
        _fail("planned per-field schedule does not match requested fields")
    for field, hours in requested_steps.items():
        if (
            not hours
            or any(
                type(hour) is not int or hour < 0 or hour > config.forecast_hours
                for hour in hours
            )
            or tuple(sorted(set(hours))) != tuple(hours)
        ):
            _fail(f"invalid planned schedule for {field}")
    expected = {field: set(requested_steps[field]) for field in fields}
    planned_run = planned.run_time
    if (
        not isinstance(planned_run, datetime)
        or planned_run.tzinfo is None
        or planned_run.utcoffset() is None
    ):
        _fail("planned run time must be timezone-aware UTC")
    run = planned_run.astimezone(timezone.utc)
    _cancel(cancel)
    dataset = (opener or gdal.Open)(str(path))
    if dataset is None:
        _fail("unable to open forecast download")
    try:
        try:
            resolution = validate_grid(dataset, model, config.region)
        except ForecastGridError as exc:
            raise ForecastValidationError(str(exc)) from exc
        actual_region = (
            (-90.0, -180.0, 90.0, 180.0)
            if MODEL_CAPABILITIES[model].global_download
            else config.region
        )
        actual_model = set()
        records = {field: {} for field in fields}
        try:
            vector = tuple((vector_reader or vector_flags)(str(path)))
        except Exception as exc:
            raise ForecastValidationError(
                "unable to validate wind vector metadata"
            ) from exc
        if len(vector) != dataset.RasterCount:
            _fail("wind vector metadata count does not match raster bands")
        source_identity = type(
            "_Identity", (), {"centre": "", "model": model, "path": str(path)}
        )()
        identities = set()
        for index in range(1, dataset.RasterCount + 1):
            _cancel(cancel)
            band = dataset.GetRasterBand(index)
            metadata = band.GetMetadata() or {}
            element = metadata.get("GRIB_ELEMENT")
            if element not in _LEVELS:
                continue
            actual_name = forecast_model_name(dataset, metadata)
            actual_model.add(actual_name)
            if actual_name != model:
                _fail("forecast band model identity does not match plan")
            if metadata.get("GRIB_PDS_PDTN") != "0":
                _fail("only deterministic point forecasts are supported")
            level = metadata.get("GRIB_SHORT_NAME", "")
            if level != _LEVELS[element]:
                _fail(f"unsupported level for {element}")
            field = _FIELD_BY_ELEMENT[element]
            if field not in expected:
                _fail("download contains an unrequested forecast field")
            unit = metadata.get("GRIB_UNIT", "")
            if field == "wind" and unit not in ("[m/s]", "m/s", "m s-1"):
                _fail("unsupported wind units")
            if field == "pressure" and unit not in (
                "[Pa]",
                "Pa",
                "[hPa]",
                "hPa",
                "[mb]",
                "mb",
            ):
                _fail("unsupported pressure units")
            if field == "gust" and unit not in ("[m/s]", "m/s", "m s-1"):
                _fail("unsupported gust units")
            reference = _utc_epoch(metadata.get("GRIB_REF_TIME"), "reference time")
            if reference != run:
                _fail("forecast bands contain mixed or unexpected run times")
            try:
                forecast_seconds = _seconds(
                    metadata["GRIB_FORECAST_SECONDS"], "forecast time"
                )
                valid_seconds = _seconds(metadata["GRIB_VALID_TIME"], "valid time")
            except KeyError:
                _fail("forecast band has incomplete valid-time metadata")
            if (
                forecast_seconds < 0
                or valid_seconds != int(reference.timestamp()) + forecast_seconds
                or forecast_seconds % 3600
            ):
                _fail("forecast band has inconsistent valid time")
            valid = reference + timedelta(seconds=forecast_seconds)
            hour = forecast_seconds // 3600
            if hour not in expected[field]:
                _fail("download contains an unplanned forecast time")
            if element in ("UGRD", "VGRD"):
                flag = vector[index - 1] if index <= len(vector) else None
                if type(flag) is not bool:
                    _fail("unknown or unsupported wind vector geometry")
            identities.add(model_key(metadata, source_identity))
            _decode_band(band, dataset, index, cancel)
            by_element = records[field].setdefault(hour, set())
            if element in by_element:
                _fail("duplicate field/time band")
            by_element.add(element)
        if not actual_model or actual_model != {model} or len(identities) != 1:
            _fail("forecast file model identity is unknown or mixed")
        field_times = []
        for field in fields:
            for hour in expected[field]:
                found = records[field].get(hour, set())
                if field == "wind":
                    if found != {"UGRD", "VGRD"}:
                        _fail(f"wind requires both components at hour {hour}")
                elif field == "pressure":
                    if len(found) != 1 or not found <= set(PRESSURE_ELEMENTS):
                        _fail(f"missing or duplicate pressure at hour {hour}")
                elif found != {"GUST"}:
                    _fail(f"missing or duplicate gust at hour {hour}")
            times = tuple(
                run + timedelta(hours=hour) for hour in sorted(expected[field])
            )
            field_times.append((field, times))
        all_times = tuple(sorted({time for _, times in field_times for time in times}))
        return ValidatedModel(
            model,
            run,
            planned.member,
            fields,
            tuple(
                sorted(
                    set(_LEVELS[e] for e in _LEVELS if _FIELD_BY_ELEMENT[e] in fields)
                )
            ),
            actual_region,
            resolution,
            all_times,
            tuple(field_times),
        )
    finally:
        band = None
        close = getattr(dataset, "Close", None)
        if close is not None:
            try:
                close()
            except Exception as exc:
                raise ForecastValidationError(
                    "failed to release forecast dataset"
                ) from exc
        dataset = None
