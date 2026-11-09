"""Observe the installed wind-only router's candidates before pruning.

The weatherrouting package owns the wind-only isochrone loop.  It creates all
heading candidates and then keeps the best one per bearing bin, so the
rejected candidates exist only inside that call.  This adapter subclasses the
configured algorithm, records the candidates as they are created, and lets the
dependency's own pruning run unchanged.  No dependency file is edited and the
returned frontiers are exactly the ones the engine would have produced.
"""

from __future__ import annotations

import logging
import math

from weatherrouting import utils

from sailworks.core.isochrones import (
    CandidateSample,
    append_contour_slice,
    build_contour_slice,
    contours_enabled,
)

logger = logging.getLogger("sailworks")


def _key(point):
    return round(float(point[0]), 12), round(float(point[1]), 12)


def contour_observing_router(algorithm):
    """Return a subclass that publishes contour slices beside its frontiers."""

    class ContourObservingRouter(algorithm):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.contour_slices = []
            self.capture_contours = contours_enabled()
            self._contour_leg = -1
            self._contour_capture = []

        def route(self, lastlog, t, timedelta, start, end):
            if lastlog is None:
                self._contour_leg += 1
            return super().route(lastlog, t, timedelta, start, end)

        def _calculate_isochrones(
            self, t, dt, isocrone, nextwp, point_f, subdiv
        ):  # noqa: C901
            if not getattr(self, "capture_contours", True):
                return super()._calculate_isochrones(
                    t, dt, isocrone, nextwp, point_f, subdiv
                )
            from sailworks.core.safetypolicy import policy_of

            frontier = list(isocrone[-1]) if isocrone else []
            parents = {_key(point.pos): index for index, point in enumerate(frontier)}
            self._contour_capture = []
            policy = policy_of(self.polar)

            def recording_point_f(p, tws, twa, dt, brg):
                result = point_f(p, tws, twa, dt, brg)
                try:
                    destination = result[0]
                    speed = float(result[1])
                    slot = int(round(math.degrees(float(twa))) + 180) // 5
                    parent = parents.get(_key(p), -1)
                    next_distance = utils.point_distance(
                        destination[0],
                        destination[1],
                        nextwp[0],
                        nextwp[1],
                    )
                    accepted = bool(
                        parent >= 0
                        and next_distance <= frontier[parent].next_wp_dist + 1e-12
                        and not policy.is_nogo(float(tws))
                    )
                    self._contour_capture.append(
                        dict(
                            latitude=float(destination[0]),
                            longitude=float(destination[1]),
                            parent=parent,
                            heading_index=slot,
                            heading=float(brg),
                            speed=speed,
                            next_wp_dist=next_distance,
                            accepted=accepted,
                        )
                    )
                except Exception:
                    # Observation must never affect the dependency's result.
                    pass
                return result

            return super()._calculate_isochrones(
                t, dt, isocrone, nextwp, recording_point_f, subdiv
            )

        def calculate_isochrones(self, t, dt, isocrone, nextwp):
            frontier = list(isocrone[-1]) if isocrone else []
            step = len(isocrone)
            self._contour_capture = []
            result = super().calculate_isochrones(t, dt, isocrone, nextwp)
            if not getattr(self, "capture_contours", True):
                return result
            try:
                final = list(result[-1]) if result else []
                final_keys = {_key(getattr(point, "pos", point)) for point in final}
                candidates = [
                    CandidateSample(
                        item["latitude"],
                        item["longitude"],
                        item["parent"],
                        item["heading_index"],
                        item["heading"],
                        bool(item["accepted"])
                        or _key((item["latitude"], item["longitude"])) in final_keys,
                    )
                    for item in self._contour_capture
                ]
                append_contour_slice(
                    self,
                    build_contour_slice(
                        [getattr(point, "pos", point) for point in frontier],
                        candidates,
                        leg_id=max(self._contour_leg, 0),
                        step=step,
                        valid_time=t,
                        frontier=(
                            [getattr(point, "pos", point) for point in result[-1]]
                            if result
                            else ()
                        ),
                        lines_validity=self.lines_validity,
                        line_validity=self.line_validity,
                    ),
                )
            except Exception:
                logger.debug("Isochrone contour capture failed", exc_info=True)
            return result

    ContourObservingRouter.__name__ = algorithm.__name__
    ContourObservingRouter.__qualname__ = algorithm.__name__
    return ContourObservingRouter
