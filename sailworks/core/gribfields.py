"""Shared GRIB field aliases and narrow mean-sea-level pressure recognition."""

PRESSURE_ELEMENTS = ("PRMSL", "MSLMA", "PRES")
GUST_ELEMENTS = ("GUST",)
WAVE_HEIGHT_ELEMENTS = ("SWH", "HTSGW")
WAVE_PERIOD_ELEMENTS = ("MWP", "PERPW")
WAVE_DIRECTION_ELEMENTS = ("MWD", "DIRPW")


def is_mean_sea_level_pressure(metadata):
    element = metadata.get("GRIB_ELEMENT")
    if element in ("PRMSL", "MSLMA"):
        return True
    return (element == "PRES" and metadata.get("GRIB_SHORT_NAME") == "0-MSL") or (
        element == "MSL" and metadata.get("GRIB_SHORT_NAME") in ("0-SFC", "0-MSL")
    )
