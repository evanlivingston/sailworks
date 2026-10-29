"""Validated selections shared by custom forecast downloads."""

import json
from dataclasses import dataclass

DEFAULT_FIELDS = ("wind", "pressure")
FIELDS = (
    *DEFAULT_FIELDS,
    "gust",
    "wave_height",
    "wave_period",
    "wave_direction",
)


@dataclass(frozen=True)
class DownloadOptions:
    hours: int = 120
    interval: int = 3
    fields: tuple = DEFAULT_FIELDS
    steps: tuple = ()

    def __post_init__(self):
        if type(self.hours) is not int or not 0 <= self.hours <= 384:
            raise ValueError("Choose a forecast length between 0 and 384 hours")
        if self.interval not in (0, 1, 3, 6, 12):
            raise ValueError("Choose model default, 1, 3, 6 or 12 hour spacing")
        if not self.fields or not set(self.fields) <= set(FIELDS):
            raise ValueError("Select at least one supported forecast field")
        if any(type(h) is not int or not 0 <= h <= self.hours for h in self.steps):
            raise ValueError("Forecast hours must lie within the chosen length")

    def available_steps(self, available):
        return tuple(
            h
            for h in available
            if h <= self.hours and (not self.interval or h % self.interval == 0)
        )

    def select(self, available):
        steps = self.available_steps(available)
        return tuple(h for h in steps if not self.steps or h in self.steps)


def selection(config, available, defaults):
    """Validate serialized custom requests; preserve legacy schedules."""
    if "steps" not in config:
        return list(defaults), DEFAULT_FIELDS
    options = DownloadOptions(
        config["hours"], 0, tuple(config["fields"]), tuple(config["steps"])
    )
    steps = sorted(set(options.steps))
    if not steps or any(h not in available for h in steps):
        raise ValueError("No supported forecast hours in this request")
    return steps, options.fields


def custom_rows(source, options, runs, available, estimate):
    """One row per run, reporting the actual selected hours for that model."""
    rows = []
    supported = getattr(source, "download_fields", DEFAULT_FIELDS)
    fields = tuple(field for field in options.fields if field in supported)
    if not fields:
        return rows
    for date, cycle, run_label, hours in runs:
        steps = options.select(available(cycle, fields, hours))
        if not steps:
            continue
        config = {
            "source": source.name,
            "date": date,
            "cycle": cycle,
            "hours": max(steps),
            "steps": steps,
            "fields": fields,
        }
        coverage = f"{min(steps)}–{max(steps)} h · {len(steps)} times"
        rows.append(
            [
                f"{source.name.removeprefix('NOAA ').removeprefix('CMC ')} · {coverage}",
                source.name,
                estimate(steps, fields),
                run_label,
                json.dumps(config),
            ]
        )
    return rows


def filtered_parameters(params, fields, pressure):
    """NOMADS uses separate switches for each field and vertical level."""
    if "wind" not in fields:
        for key in ("lev_10_m_above_ground", "var_UGRD", "var_VGRD"):
            params.pop(key, None)
    if "pressure" not in fields:
        for key in ("lev_mean_sea_level", "var_" + pressure):
            params.pop(key, None)
    if "gust" not in fields:
        for key in ("lev_surface", "var_GUST"):
            params.pop(key, None)
    return params
