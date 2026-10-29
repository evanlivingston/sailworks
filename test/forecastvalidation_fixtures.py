from datetime import datetime, timezone
import json

from osgeo import osr
from pyproj import CRS

from gweatherrouting.core.forecasttranche import ModelConfiguration
from gweatherrouting.core.forecastrefreshplan import (
    MODEL_CAPABILITIES,
    PlannedModel,
)
from gweatherrouting.core.forecastvalidation import inspect_download

RUN = datetime(2025, 1, 1, tzinfo=timezone.utc)


class Band:
    XSize = 16
    YSize = 16
    DataType = 1

    def __init__(self, metadata, fail_decode=False, truncated=False):
        self.metadata = metadata
        self.fail_decode = fail_decode
        self.truncated = truncated

    def GetMetadata(self):
        return self.metadata

    def ReadRaster(self, x, y, width, height, **kwargs):
        if self.fail_decode:
            raise RuntimeError("synthetic decode failure")
        size = width * height
        return bytes(size - 1 if self.truncated else size)


class Dataset:
    RasterXSize = 16
    RasterYSize = 16

    def __init__(self, bands):
        self.bands = [item if isinstance(item, Band) else Band(item) for item in bands]
        self.closed = False
        self.srs = osr.SpatialReference()
        self.srs.ImportFromEPSG(4326)
        self.srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    @property
    def RasterCount(self):
        return len(self.bands)

    def GetGeoTransform(self):
        return (-2.0, 0.25, 0.0, 2.0, 0.0, -0.25)

    def GetProjection(self):
        return self.srs.ExportToWkt()

    def GetRasterBand(self, index):
        result = self.bands[index - 1]
        result.XSize = self.RasterXSize
        result.YSize = self.RasterYSize
        return result

    def Close(self):
        self.closed = True


class ProjectedDataset(Dataset):
    RasterXSize = 100
    RasterYSize = 100

    def __init__(self, bands):
        super().__init__(bands)
        self.srs.ImportFromEPSG(3857)

    def GetGeoTransform(self):
        return (-150_000, 3_000, 0.0, 150_000, 0.0, -3_000)


class GlobalDataset(Dataset):
    RasterXSize = 1440
    RasterYSize = 720

    def GetGeoTransform(self):
        return (0.0, 0.25, 0.0, 90.0, 0.0, -0.25)


class RotatedDataset(Dataset):
    def __init__(self, bands):
        self.bands = [Band(item) for item in bands]
        self.closed = False
        wkt = CRS.from_cf(
            {
                "grid_mapping_name": "rotated_latitude_longitude",
                "grid_north_pole_latitude": 39.25,
                "grid_north_pole_longitude": -170.0,
                "north_pole_grid_longitude": 0.0,
            }
        ).to_wkt()
        self.srs = osr.SpatialReference()
        assert self.srs.ImportFromWkt(wkt) == 0
        self.srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        self.wkt = wkt.replace('["', '["Pole rotation ', 1)
        target = osr.SpatialReference()
        target.ImportFromEPSG(4326)
        target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        transform = osr.CoordinateTransformation(target, self.srs)
        points = [
            transform.TransformPoint(lon, lat)[:2]
            for lon in (-1.0, 0.0, 1.0)
            for lat in (-1.0, 0.0, 1.0)
        ]
        step = 0.0225
        left = min(x for x, _ in points) - 0.5
        top = max(y for _, y in points) + 0.5
        self.RasterXSize = int((max(x for x, _ in points) - left + 0.5) / step) + 1
        self.RasterYSize = int((top - min(y for _, y in points) + 0.5) / step) + 1
        self.rotated_gt = (left, step, 0.0, top, 0.0, -step)

    def GetGeoTransform(self):
        return self.rotated_gt

    def GetProjection(self):
        return self.wkt


def band(element, hour, *, model="GFS", unit=None, level=None):
    process = {
        "GFS": "81",
        "HRRR": "83",
        "HRDPS": "50",
        "GFSWAVE": "11",
    }[model]
    center = "54" if model == "HRDPS" else "7"
    default_unit = {
        "UGRD": "[m/s]",
        "VGRD": "[m/s]",
        "GUST": "[m/s]",
        "HTSGW": "[m]",
        "PERPW": "[s]",
        "DIRPW": "[Degree true]",
    }.get(element, "[Pa]")
    return {
        "GRIB_ELEMENT": element,
        "GRIB_SHORT_NAME": level
        or (
            "10-HTGL"
            if element in ("UGRD", "VGRD")
            else (
                "0-SFC"
                if element == "GUST"
                else "1-SFC" if element in ("HTSGW", "PERPW", "DIRPW") else "0-MSL"
            )
        ),
        "GRIB_UNIT": unit or default_unit,
        "GRIB_IDS": (
            f"CENTER={center} SUBCENTER=0 TYPE=1"
            if model == "GFSWAVE"
            else f"CENTER={center}"
        ),
        "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": (
            f"0 0 2 0 {process}" if model == "GFSWAVE" else f"0 0 0 0 {process}"
        ),
        "GRIB_PDS_PDTN": "0",
        "GRIB_REF_TIME": str(int(RUN.timestamp())),
        "GRIB_FORECAST_SECONDS": str(hour * 3600),
        "GRIB_VALID_TIME": str(int(RUN.timestamp()) + hour * 3600),
    }


def planned(fields=("wind",), steps=None, *, model="GFS"):
    steps = steps or {field: (0, 3) for field in fields}
    capability = MODEL_CAPABILITIES[model]
    union_steps = tuple(
        sorted({hour for schedule in steps.values() for hour in schedule})
    )
    horizon = max(union_steps)
    configuration = ModelConfiguration(
        model,
        fields,
        (-1, -1, 1, 1),
        "native",
        horizon,
        union_steps,
        0,
    )
    identifier = json.dumps(
        {
            "source": capability.provider_name,
            "date": "20250101",
            "cycle": "00",
            "hours": horizon,
            "steps": list(union_steps),
            "fields": list(fields),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return PlannedModel(
        configuration,
        RUN,
        capability.provider_name,
        identifier,
        capability.native_resolution,
        tuple((field, tuple(hours)) for field, hours in steps.items()),
    )


def inspect_fake(path, request, dataset, cancel=None, flags=None):
    if flags is None:
        flags = [False] * dataset.RasterCount
    return inspect_download(
        path,
        request,
        cancel,
        opener=lambda _: dataset,
        vector_reader=lambda _: flags,
    )


def dataset_for(fields=("wind",), hours=(0, 3), **kwargs):
    bands = []
    for field in fields:
        for hour in hours:
            if field == "wind":
                bands.extend(
                    (band("UGRD", hour, **kwargs), band("VGRD", hour, **kwargs))
                )
            elif field == "gust":
                bands.append(band("GUST", hour, **kwargs))
            elif field == "wave_height":
                bands.append(band("HTSGW", hour, **kwargs))
            elif field == "wave_period":
                bands.append(band("PERPW", hour, **kwargs))
            elif field == "wave_direction":
                bands.append(band("DIRPW", hour, **kwargs))
            else:
                bands.append(band("PRMSL", hour, **kwargs))
    return Dataset(bands)


class GFSWaveGlobalDataset(Dataset):
    RasterXSize = 1440
    RasterYSize = 721

    def GetGeoTransform(self):
        return (-180.125, 0.25, 0.0, 90.125, 0.0, -0.25)
