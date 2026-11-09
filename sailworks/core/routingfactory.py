"""Shared construction of one routing calculation.

The application ``Core`` and a scenario worker must build the same routing
object from the same inputs.  Keeping the construction here avoids a second,
divergent copy in the process worker.
"""

from typing import Callable, Optional

import weatherrouting

from sailworks.core import utils
from sailworks.core.geo.track import Track
from sailworks.core.polarwrapper import PolarWrapper


def build_routing(
    algorithm,
    polar_file,
    track_or_poi,
    start_datetime,
    start_position,
    validity_providers,
    grib,
    wind_override=None,
    disable_coastline_checks=False,
    polar_efficiency=None,
    use_currents=False,
    wind_sources=None,
    current_selection=None,
    safety_policy=None,
):
    """Build the existing engine routing from resolved inputs.

    ``grib`` is the bound wind override (or the application's manager for a
    non-bound calculation).  ``current_selection`` is the frozen field
    selection when currents are used.  No source-selection policy lives here.
    """
    from sailworks.core.marine.routing import (
        CurrentIsoRouter,
        CurrentRoutingEnvironment,
        FrozenRoutingWind,
        routing_utc,
    )

    polar = weatherrouting.Polar(polar_file)

    if polar_efficiency is not None and polar_efficiency != 1.0:
        polar = PolarWrapper(polar, polar_efficiency)

    if safety_policy is not None and safety_policy.active:
        from sailworks.core.safetypolicy import SafetyPolar, wind_limit_router

        polar = SafetyPolar(polar, safety_policy)
        if not use_currents and safety_policy.tws_max_kn is not None:
            algorithm = wind_limit_router(algorithm)

    if not use_currents:
        # Observe the dependency's pre-pruning candidates without editing it.
        from sailworks.core.marine.windcontours import contour_observing_router

        algorithm = contour_observing_router(algorithm)

    pval: Optional[Callable] = utils.points_validity
    lval: Optional[Callable] = None

    if len(validity_providers) > 0:
        # The existing engine uses the first provider's callables; keep that
        # selection rule unchanged for the worker.
        pval = validity_providers[0].points_validity
        lval = validity_providers[0].lines_validity

    if disable_coastline_checks and not validity_providers:
        lval = None
        pval = None

    environment = None
    if use_currents:
        from functools import partial

        grib = FrozenRoutingWind(
            wind_sources if wind_sources is not None else grib,
            wind_override,
        )
        start_datetime = routing_utc(start_datetime)
        environment = CurrentRoutingEnvironment(grib, current_selection)
        algorithm = partial(CurrentIsoRouter, environment=environment)

    routing = weatherrouting.Routing(
        algorithm,
        polar,
        (
            track_or_poi.to_list()
            if isinstance(track_or_poi, Track)
            else [track_or_poi.position]
        ),
        grib,
        start_datetime=start_datetime,
        start_position=start_position,
        points_validity=pval,
        lines_validity=lval,
    )
    routing.current_environment = environment
    if environment is not None:
        environment.factors = {
            "currents": True,
            "land_avoidance": not disable_coastline_checks or bool(validity_providers),
            "wave_correction": (
                getattr(safety_policy, "wave_correct", False)
                if safety_policy is not None
                else False
            ),
        }
    return routing
