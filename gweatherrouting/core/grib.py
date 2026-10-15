# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
# Copyright (C) 2012 Riccardo Apolloni
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

import datetime
import logging
import re
from functools import lru_cache
from typing import Optional, Tuple

from .gribsampling import GridIndex, WeatherSampler, number

import weatherrouting
from osgeo import gdal

logger = logging.getLogger("gweatherrouting")

# Mean-sea-level pressure elements (Pa) used for isobars. GFS downloads
# request PRMSL; other models may already carry one of these bands.
PRESSURE_ELEMENTS = ("PRMSL", "MSLMA")
PA_TO_HPA = 1.0 / 100.0


def forecast_model_name(dataset, metadata):
    """Display identity only; never change the sampler's model-selection keys."""
    center = re.search(r"(?:^|\s)CENTER=(\d+)", metadata.get("GRIB_IDS", ""))
    values = metadata.get("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES", "").split()
    if center is None or len(values) < 5:
        return "Unknown"
    origin, process = center.group(1), values[4]
    if origin == "7":
        return {"81": "GFS", "96": "GFS", "83": "HRRR"}.get(process, "Unknown")
    if origin == "54" and process == "50":
        # CMC process 50 alone is insufficient: confirm the continental HRDPS grid.
        transform = dataset.GetGeoTransform()
        if (
            abs(abs(transform[1]) - 0.0225) < 1e-6
            and "Pole rotation" in dataset.GetProjection()
        ):
            return "HRDPS"
    return "Unknown"


class MetaGrib:
    def __init__(
        self,
        path,
        name,
        centre,
        bounds,
        start_time,
        last_forecast,
        model_name="Unknown",
    ):
        self.name = name
        self.centre = centre.upper()
        self.bounds = bounds
        self.start_time = start_time
        self.last_forecast = last_forecast
        self.path = path
        self.model_name = model_name


class Grib(weatherrouting.Grib):
    def __init__(
        self,
        path: str,
        name: str,
        centre,
        bounds,
        start_time: datetime.datetime,
        last_forecast,
        model_name="Unknown",
    ):
        self.name = name
        self.model_name = model_name
        self.centre = centre.upper()
        self.bounds = bounds
        self.start_time = start_time
        self.last_forecast = last_forecast
        self.end_time = self.start_time + datetime.timedelta(hours=self.last_forecast)
        self.path = path
        self.dataset = gdal.Open(path)

    def _find_bands_for_time(self, t):
        """Find bands for U and V wind components at a given time."""
        u_band = None
        v_band = None

        for bidx in range(1, self.dataset.RasterCount + 1):
            band = self.dataset.GetRasterBand(bidx)
            metadata = band.GetMetadata()

            if (
                "GRIB_ELEMENT" not in metadata
                or "GRIB_FORECAST_SECONDS" not in metadata
            ):
                continue

            element = metadata["GRIB_ELEMENT"]
            forecast_seconds = number(metadata["GRIB_FORECAST_SECONDS"])
            forecast_hours = forecast_seconds / 3600

            if forecast_hours == t:
                if element == "UGRD":
                    u_band = band
                elif element == "VGRD":
                    v_band = band

        return u_band, v_band

    def _find_pressure_band_for_time(self, t):
        """Find the mean-sea-level pressure band at a given forecast hour."""
        for bidx in range(1, self.dataset.RasterCount + 1):
            band = self.dataset.GetRasterBand(bidx)
            metadata = band.GetMetadata()

            if (
                "GRIB_ELEMENT" not in metadata
                or "GRIB_FORECAST_SECONDS" not in metadata
            ):
                continue

            if (
                metadata["GRIB_ELEMENT"] in PRESSURE_ELEMENTS
                and number(metadata["GRIB_FORECAST_SECONDS"]) / 3600 == t
            ):
                return band

        return None

    @lru_cache(maxsize=2048)
    def get_rindex_data(self, t):
        u_band, v_band = self._find_bands_for_time(t)
        if u_band is None or v_band is None:
            raise ValueError(f"Wind data not found for forecast hour {t}")

        u = u_band.ReadAsArray()
        v = v_band.ReadAsArray()

        gt = self.dataset.GetGeoTransform()
        uv_data = []

        for y in range(u.shape[0]):
            for x in range(u.shape[1]):
                lon = gt[0] + (x + 0.5) * gt[1] + (y + 0.5) * gt[2]
                lat = gt[3] + (x + 0.5) * gt[4] + (y + 0.5) * gt[5]
                uv_data.append((lat, lon, u[y, x], v[y, x]))

        return uv_data

    def _get_wind_data(self, t, bounds):
        try:
            uv = self.get_rindex_data(t)
        except Exception as e:
            logger.debug(e)
            return None

        return list(
            filter(
                lambda x: bounds[0][0] <= x[0] <= bounds[1][0]
                and bounds[0][1] <= x[1] <= bounds[1][1],
                uv,
            )
        )

    @lru_cache(maxsize=512)
    def get_pressure_rindex_data(self, t):
        band = self._find_pressure_band_for_time(t)
        if band is None:
            raise ValueError(f"Pressure data not found for forecast hour {t}")

        pressure = band.ReadAsArray()
        gt = self.dataset.GetGeoTransform()
        data = []

        for y in range(pressure.shape[0]):
            for x in range(pressure.shape[1]):
                lon = gt[0] + (x + 0.5) * gt[1] + (y + 0.5) * gt[2]
                lat = gt[3] + (x + 0.5) * gt[4] + (y + 0.5) * gt[5]
                data.append((lat, lon, pressure[y, x]))

        return data

    def _get_pressure_data(self, t, bounds):
        try:
            pressure = self.get_pressure_rindex_data(t)
        except Exception as e:
            logger.debug(e)
            return None

        return list(
            filter(
                lambda x: bounds[0][0] <= x[0] <= bounds[1][0]
                and bounds[0][1] <= x[1] <= bounds[1][1],
                pressure,
            )
        )

    @property
    def sampling(self):
        if "_sampling" not in self.__dict__:
            self._sampling = GridIndex(self)
        return self._sampling

    def sampler(self):
        if "_sampler" not in self.__dict__:
            self._sampler = WeatherSampler((self,))
        return self._sampler

    def get_pressure(self, tt, bounds):
        return self.sampler().grid("pressure", tt, bounds) or None

    def get_wind(self, tt, bounds):
        return self.sampler().grid("wind", tt, bounds)

    def close(self):
        if "_sampling" in self.__dict__:
            self._sampling.close()
        self.__dict__.pop("_sampler", None)
        self.dataset = None

    @lru_cache(maxsize=1)
    def _get_available_pressure_hours(self):
        """Scan GRIB bands to find all forecast hours with pressure data."""
        hours = set()
        for bidx in range(1, self.dataset.RasterCount + 1):
            band = self.dataset.GetRasterBand(bidx)
            metadata = band.GetMetadata()
            if (
                "GRIB_FORECAST_SECONDS" in metadata
                and metadata.get("GRIB_ELEMENT") in PRESSURE_ELEMENTS
            ):
                hours.add(number(metadata["GRIB_FORECAST_SECONDS"]) / 3600)
        return sorted(hours)

    @lru_cache(maxsize=1)
    def _get_available_forecast_hours(self):
        """Scan GRIB bands to find all available forecast hours."""
        hours = set()
        for bidx in range(1, self.dataset.RasterCount + 1):
            band = self.dataset.GetRasterBand(bidx)
            metadata = band.GetMetadata()
            if "GRIB_FORECAST_SECONDS" in metadata and metadata.get("GRIB_ELEMENT") in (
                "UGRD",
                "VGRD",
            ):
                hours.add(number(metadata["GRIB_FORECAST_SECONDS"]) / 3600)
        return sorted(hours)

    def _find_bracketing_hours(self, t, available_hours):
        if not available_hours:
            return None, None
        before = [hour for hour in available_hours if hour <= t]
        after = [hour for hour in available_hours if hour >= t]
        return (
            max(before) if before else min(after),
            min(after) if after else max(before),
        )

    def _transform_time(self, t) -> Optional[float]:
        elapsed = (t.timestamp() - self.start_time.timestamp()) / 3600
        if elapsed < 0 or t.timestamp() > self.end_time.timestamp():
            return None
        return elapsed

    def get_wind_at(self, t, lat: float, lon: float) -> Optional[Tuple[float, float]]:
        return self.sampler().wind_at(t, lat, lon)

    @staticmethod
    def parse_metadata(path):
        dataset = gdal.Open(path)
        if dataset is None:
            raise ValueError("Unable to open forecast: " + path)
        centre = ""
        # TODO: get bounds and timeframe
        bounds = [0, 0, 0, 0]
        start_time = None
        hours_forecasted = None
        models = set()

        for bidx in range(1, dataset.RasterCount + 1):
            band = dataset.GetRasterBand(bidx)
            metadata = band.GetMetadata()

            if metadata.get("GRIB_ELEMENT") in ("UGRD", "VGRD", *PRESSURE_ELEMENTS):
                models.add(forecast_model_name(dataset, metadata))
                centre = metadata.get("GRIB_CENTER", "")
                forecast_hours = number(metadata.get("GRIB_FORECAST_SECONDS", 0)) / 3600
                time = datetime.datetime.fromtimestamp(
                    number(metadata.get("GRIB_REF_TIME", 0))
                )

                if start_time is None or time < start_time:
                    start_time = time

                if hours_forecasted is None or forecast_hours > hours_forecasted:
                    hours_forecasted = forecast_hours

        if start_time is None or hours_forecasted is None:
            raise ValueError(
                "Forecast contains no supported wind or pressure records: " + path
            )
        return MetaGrib(
            path,
            path.split("/")[-1],
            centre,
            bounds,
            start_time,
            hours_forecasted,
            " / ".join(sorted(models)),
        )

    @staticmethod
    def parse(path):
        meta = Grib.parse_metadata(path)
        return Grib(
            path,
            meta.name,
            meta.centre,
            meta.bounds,
            meta.start_time,
            meta.last_forecast,
            meta.model_name,
        )
