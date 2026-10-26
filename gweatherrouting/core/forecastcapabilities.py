"""Stable model catalogue and native forecast coverage schedules."""

from dataclasses import dataclass
import re
from types import MappingProxyType


@dataclass(frozen=True)
class ModelCapabilities:
    model_id: str
    provider_name: str
    native_resolution: str
    default_hours: int = 120
    intervals: tuple[int, ...] = (0, 1, 3, 6, 12)
    global_download: bool = False
    attribution: str = ""
    license_url: str = ""
    data_url: str = ""
    grib_center: str | None = None
    grib_subcenter: str | None = None
    grib_product: tuple[int, int, int] | None = None
    grib_type: int | None = None
    grib_pdt: str = "0"
    cycles: tuple[int, ...] = (0, 6, 12, 18)

    def max_horizon(self, run_hour=None):
        if run_hour is not None and run_hour not in self.cycles:
            raise ValueError(f"Unsupported {self.model_id} cycle {run_hour}")
        if self.model_id == "GFS":
            return 384
        if self.model_id == "HRRR":
            return 48 if run_hour is None or run_hour in (0, 6, 12, 18) else 18
        if self.model_id == "HRDPS":
            return 48
        if self.model_id == "IFS":
            return 360 if run_hour is None or run_hour in (0, 12) else 144
        if self.model_id in ("AIFS", "AIGFS"):
            return 360 if self.model_id == "AIFS" else 384
        raise ValueError(f"No forecast horizon defined for {self.model_id}")

    def available_steps(self, run_hour, field):
        if field not in ("wind", "pressure") and not (
            field == "gust" and self.model_id in ("GFS", "HRRR")
        ):
            return ()
        maximum = self.max_horizon(run_hour)
        if self.model_id == "GFS":
            return tuple(range(121)) + tuple(range(123, 385, 3))
        if self.model_id == "IFS":
            maximum = self.max_horizon(run_hour)
            return tuple(range(0, 145, 3)) + (
                tuple(range(150, 361, 6)) if maximum == 360 else ()
            )
        if self.model_id == "AIFS":
            return tuple(range(0, 361, 6))
        if self.model_id == "AIGFS":
            return tuple(range(0, 385, 6))
        if self.model_id == "HRRR" and field == "gust":
            return tuple(range(maximum + 1))
        if self.model_id == "HRDPS" and field == "pressure":
            return tuple(range(0, maximum + 1, 3))
        return tuple(range(maximum + 1))


MODEL_CAPABILITIES = MappingProxyType(
    {
        "GFS": ModelCapabilities("GFS", "NOAA GFS", "0.25deg"),
        "HRRR": ModelCapabilities(
            "HRRR", "NOAA HRRR", "3km", default_hours=48, cycles=tuple(range(24))
        ),
        "HRDPS": ModelCapabilities(
            "HRDPS", "CMC HRDPS", "0.0225deg-rotated", default_hours=48
        ),
        "IFS": ModelCapabilities(
            "IFS",
            "ECMWF IFS",
            "0.25deg",
            default_hours=120,
            intervals=(0, 3, 6, 12),
            global_download=True,
            attribution="ECMWF IFS open data",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            data_url="https://data.ecmwf.int/forecasts/",
            grib_center="98",
            grib_subcenter="0",
            grib_product=(2, 255, 161),
            grib_type=1,
        ),
        "AIFS": ModelCapabilities(
            "AIFS",
            "ECMWF AIFS Single",
            "0.25deg",
            default_hours=120,
            intervals=(0, 6, 12),
            global_download=True,
            attribution="ECMWF AIFS Single open data",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            data_url="https://data.ecmwf.int/forecasts/",
            grib_center="98",
            grib_subcenter="0",
            grib_product=(2, 1, 5),
            grib_type=10,
        ),
        "AIGFS": ModelCapabilities(
            "AIGFS",
            "NOAA AI-GFS",
            "0.25deg",
            default_hours=120,
            intervals=(0, 6, 12),
            global_download=True,
            attribution="NOAA AI-GFS public data",
            data_url="https://nomads.ncep.noaa.gov/pub/data/nccf/com/aigfs/prod/",
            grib_center="7",
            grib_subcenter="0",
            grib_product=(2, 0, 137),
            grib_type=1,
        ),
    }
)


def model_from_grib_metadata(dataset, metadata):
    """Resolve supported model identity from decoded GRIB metadata only."""
    ids = metadata.get("GRIB_IDS", "")
    center_match = re.search(r"(?:^|\s)CENTER=(\d+)", ids)
    subcenter_match = re.search(r"(?:^|\s)SUBCENTER=(\d+)", ids)
    type_match = re.search(r"(?:^|\s)TYPE=(\d+)", ids)
    values = metadata.get("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES", "").split()
    center = center_match.group(1) if center_match else ""
    product = tuple(values[2:5]) if len(values) >= 5 else ()

    # Preserve the legacy names and HRDPS projection guard exactly.
    if center == "7" and len(values) >= 5:
        if values[4] in ("81", "96"):
            return "GFS"
        if values[4] == "83":
            return "HRRR"
    if center == "54" and len(values) >= 5 and values[4] == "50":
        if dataset is None:
            return "Unknown"
        transform = dataset.GetGeoTransform()
        if (
            abs(abs(transform[1]) - 0.0225) < 1e-6
            and "Pole rotation" in dataset.GetProjection()
        ):
            return "HRDPS"

    subcenter = subcenter_match.group(1) if subcenter_match else ""
    message_type = int(type_match.group(1)) if type_match else None
    pdt = metadata.get("GRIB_PDS_PDTN")
    for model_id, capability in MODEL_CAPABILITIES.items():
        if capability.grib_center is None:
            continue
        expected = tuple(str(value) for value in capability.grib_product or ())
        if (
            center == capability.grib_center
            and subcenter == capability.grib_subcenter
            and product == expected
            and pdt == capability.grib_pdt
            and message_type == capability.grib_type
        ):
            return model_id
    return "Unknown"
