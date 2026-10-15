"""Shared model selection for cached forecasts, downloads and routing."""

from dataclasses import dataclass

from .gribsources.options import DownloadOptions

MODEL_SOURCES = {"GFS": "NOAA GFS", "HRRR": "NOAA HRRR", "HRDPS": "CMC HRDPS"}
IMPORTED = "Imported forecasts"


def forecast_source(grib):
    return MODEL_SOURCES.get(getattr(grib, "model_name", "Unknown"), IMPORTED)


@dataclass(frozen=True)
class ForecastSelection:
    models: tuple = ("NOAA GFS",)
    hours: int = 120
    interval: int = 3
    fields: tuple = ("wind", "pressure")
    steps: tuple = ()

    def __post_init__(self):
        for name in ("models", "fields", "steps"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not set(self.models) <= {*MODEL_SOURCES.values(), IMPORTED}:
            raise ValueError("Unsupported weather model selection")
        object.__setattr__(
            self,
            "models",
            tuple(
                name
                for name in (*MODEL_SOURCES.values(), IMPORTED)
                if name in self.models
            ),
        )
        DownloadOptions(self.hours, self.interval, self.fields or ("wind",), self.steps)

    def options(self, routing=False):
        if not self.models:
            raise ValueError("Select at least one weather model")
        fields = (
            tuple(dict.fromkeys((*self.fields, "wind"))) if routing else self.fields
        )
        return DownloadOptions(self.hours, self.interval, fields, self.steps)

    def to_json(self):
        return dict(
            models=list(self.models),
            hours=self.hours,
            interval=self.interval,
            fields=list(self.fields),
            steps=list(self.steps),
        )

    @classmethod
    def from_json(cls, config):
        return cls(
            tuple(config["models"]),
            config["hours"],
            config["interval"],
            tuple(config["fields"]),
            tuple(config["steps"]),
        )
