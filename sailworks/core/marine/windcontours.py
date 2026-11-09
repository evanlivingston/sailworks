"""Observe the installed wind-only router and build ordered contours.

The weatherrouting package owns the wind-only isochrone loop and prunes its
frontier.  This adapter subclasses the configured algorithm, records the
candidate endpoints while they are still available, and passes them to the
convex/concave boundary builder.  The dependency's pruning and path selection
are unchanged.
"""

from __future__ import annotations

import logging

from sailworks.core.isochrones import (
    append_contour_slice,
    build_contour_slice,
    contours_enabled,
)

logger = logging.getLogger("sailworks")


def contour_observing_router(algorithm):
    """Return a subclass that publishes boundary slices beside its frontiers."""

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

        def _calculate_isochrones(self, t, dt, isocrone, nextwp, point_f, subdiv):
            if not getattr(self, "capture_contours", True):
                return super()._calculate_isochrones(
                    t, dt, isocrone, nextwp, point_f, subdiv
                )
            self._contour_capture = []
            self._contour_counter = 0

            def recording_point_f(p, tws, twa, dt, brg):
                result = point_f(p, tws, twa, dt, brg)
                self._contour_counter += 1
                if self._contour_counter % 3:
                    return result
                try:
                    destination = result[0]
                    self._contour_capture.append(
                        (float(destination[0]), float(destination[1]))
                    )
                except Exception:
                    # Observation must never affect the dependency's result.
                    pass
                return result

            return super()._calculate_isochrones(
                t, dt, isocrone, nextwp, recording_point_f, subdiv
            )

        def calculate_isochrones(self, t, dt, isocrone, nextwp):
            step = len(isocrone)
            result = super().calculate_isochrones(t, dt, isocrone, nextwp)
            if not getattr(self, "capture_contours", True):
                return result
            try:
                final = list(result[-1]) if result else []
                append_contour_slice(
                    self,
                    build_contour_slice(
                        [],
                        list(self._contour_capture),
                        leg_id=max(self._contour_leg, 0),
                        step=step,
                        valid_time=t,
                        frontier=[getattr(point, "pos", point) for point in final],
                        frontier_bearings=[
                            getattr(point, "brg", 0.0) for point in final
                        ],
                        gap_m=getattr(self, "contour_gap_m", 2200.0),
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
