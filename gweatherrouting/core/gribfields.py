"""Shared GRIB field aliases and narrow mean-sea-level pressure recognition."""

PRESSURE_ELEMENTS = ("PRMSL", "MSLMA", "PRES")
GUST_ELEMENTS = ("GUST",)


def is_mean_sea_level_pressure(metadata):
    element = metadata.get("GRIB_ELEMENT")
    if element in ("PRMSL", "MSLMA"):
        return True
    return element == "PRES" and metadata.get("GRIB_SHORT_NAME") == "0-MSL"
