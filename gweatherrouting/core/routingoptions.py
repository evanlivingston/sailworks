"""Immutable routing options and scenario inputs over the existing engine metadata."""

import itertools
import math
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import timedelta

AXES = (
    ("time_offset_hours", "Departure offsets (hours)", "-6, 0, 6", 0),
    ("wind_speed_pct", "Wind intensity (%)", "-20, 0, 20", 0),
    ("wind_dir_offset", "Wind direction shift (°)", "-15, 0, 15", 0),
    ("polar_efficiency_pct", "Polar efficiency (%)", "85, 100", 100),
)


@dataclass(frozen=True)
class RouteScenario:
    time_offset_hours: float = 0
    wind_speed_pct: float = 0
    wind_dir_offset: float = 0
    polar_efficiency_pct: float = 100


def comparison_scenarios(axes, efficiency=100):
    """GTK's Cartesian product/order, with explicit validation before launching."""
    if not axes:
        return ()
    values = []
    for key, label, _, default in AXES:
        if key not in axes:
            values.append([efficiency if key == "polar_efficiency_pct" else default])
            continue
        try:
            entries = [
                float(value.strip()) for value in axes[key].split(",") if value.strip()
            ]
        except ValueError:
            raise ValueError(label + ": enter comma-separated numbers.") from None
        if not entries or not all(math.isfinite(value) for value in entries):
            raise ValueError(label + ": enter at least one finite number.")
        if key == "polar_efficiency_pct" and min(entries) <= 0:
            raise ValueError("Polar efficiency must be greater than zero.")
        if key == "wind_speed_pct" and min(entries) <= -100:
            raise ValueError("Wind intensity must be greater than -100%.")
        values.append(entries)
    if math.prod(map(len, values)) > 256:
        raise ValueError("Choose 256 or fewer comparison scenarios.")
    return tuple(RouteScenario(*parts) for parts in itertools.product(*values))


def scenario_request(request, scenario):
    parts = [request.name]
    for value, template, default in (
        (scenario.time_offset_hours, "t%+.0fh", 0),
        (scenario.wind_speed_pct, "ws%+.0f%%", 0),
        (scenario.wind_dir_offset, "wd%+.0fdeg", 0),
        (scenario.polar_efficiency_pct, "eff%.0f%%", 100),
    ):
        if value != default:
            parts.append(template % value)
    return replace(
        request,
        name="_".join(parts),
        departure=request.departure + timedelta(hours=scenario.time_offset_hours),
        wind_speed_pct=scenario.wind_speed_pct,
        wind_dir_offset=scenario.wind_dir_offset,
        polar_efficiency=scenario.polar_efficiency_pct / 100,
    )


def parameter_values(algorithm, values):
    result = {}
    for key, value in values.items():
        if key not in algorithm.PARAMS:
            raise ValueError("Unknown routing parameter: " + key)
        param = algorithm.PARAMS[key]
        if param.ttype == "bool":
            if type(value) is not bool:
                raise ValueError(param.name + ": choose on or off.")
        else:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(param.name + ": enter a finite number.")
            if not param.lower <= value <= param.upper:
                raise ValueError(param.name + ": value is outside the allowed range.")
            if param.ttype == "int":
                if value != int(value):
                    raise ValueError(param.name + ": enter a whole number.")
                value = int(value)
        result[key] = value
    return result


def configured_algorithm(algorithm, values):
    """Each calculation owns PARAMS; never mutate the engine's shared classes."""
    values = parameter_values(algorithm, dict(values))
    params = deepcopy(algorithm.PARAMS)
    for key, value in values.items():
        params[key].value = value
    return type(algorithm.__name__ + "Run", (algorithm,), {"PARAMS": params})
