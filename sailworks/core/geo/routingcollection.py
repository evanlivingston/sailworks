# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
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

from pathlib import Path
from typing import Generic, TypeVar

from sailworks.core.geo.collection import CollectionWithActiveElement
from sailworks.core.geo.routing import Routing, contour_frontiers

T = TypeVar("T", bound=Routing)


def _readout_value(readout, *keys):
    """Return the first recorded readout value, keeping unknown values absent."""
    for key in keys:
        value = readout.get(key)
        if value is not None:
            return value
    return None


def _leg_point(leg, latitude, longitude, time, readout):
    """Build one seven-field legacy route point from a schema-v1 leg."""
    readout = readout or {}
    return (
        latitude,
        longitude,
        time,
        readout.get("true_wind_angle_degrees"),
        readout.get("true_wind_speed_knots"),
        _readout_value(readout, "speed_over_ground_knots", "speed_through_water_knots"),
        _readout_value(readout, "heading_degrees", "course_over_ground_degrees"),
    )


def _result_points(result):
    """Map schema-v1 legs onto the legacy route point tuples.

    Legacy consumers expect ``(lat, lon, time, wind angle, wind speed, speed,
    heading)``. Each leg contributes its start point, and the last leg adds the
    final endpoint with its end time. Missing readouts stay ``None`` so they
    read as absent rather than as recorded zeroes.
    """
    points = [
        _leg_point(leg, leg.start.lat, leg.start.lon, leg.start_utc, leg.readout)
        for leg in result.legs
    ]
    if result.legs:
        final = result.legs[-1]
        points.append(
            _leg_point(
                final,
                final.end.lat,
                final.end.lon,
                final.end_utc,
                final.readout,
            )
        )
    return points


def result_summary(result):
    """Small metadata safe to keep in the collection JSON."""
    from sailworks.core.marine.routingresult import export_eligible

    validation = result.outcome.validation
    return {
        "schema_version": result.schema_version,
        "calculation_id": result.calculation_id,
        "generation": result.generation,
        "sequence": result.sequence,
        "status": result.status,
        "leg_count": len(result.legs),
        "layer_count": len(result.layers),
        "validation_state": validation.state,
        "export_eligible": export_eligible(result),
    }


class _RoutingCollection(CollectionWithActiveElement, Generic[T]):
    def __init__(self, result_store=None):
        # Constructed lazily so startup never scans or reads stored artifacts.
        self._result_store = result_store
        super().__init__(Routing, "routing")

    @property
    def result_store(self):
        if self._result_store is None:
            from sailworks.core import storage
            from sailworks.core.marine.routingresultstore import RoutingResultStore

            self._result_store = RoutingResultStore(
                Path(storage.DATA_DIR) / "routing-results"
            )
        return self._result_store

    def load_result(self, route):
        """Load one route's schema-v1 result on demand through the bounded store."""
        if not isinstance(route, Routing):
            raise TypeError("route must be a Routing")
        return route.load_result(self.result_store)

    def inspector(self, route):
        """Offline inspection view over the result referenced by ``route``."""
        from sailworks.core.marine.routinginspection import RoutingInspector

        return RoutingInspector(self.load_result(route))

    def save_result(self, name, result, points=None, inspection_context=None):
        """Persist a schema-v1 result once and return a lightweight route.

        The artifact payload is canonical result JSON. The route keeps the
        polyline needed by the map plus a small summary, and links the artifact
        by digest instead of embedding isochrones or traces.
        """
        from sailworks.core.marine.routingresult import RoutingResult

        if not isinstance(result, RoutingResult):
            raise TypeError("result must be a RoutingResult")
        payload = result.to_json().encode("utf-8")
        ref = self.result_store.put(payload)
        provenance = result.provenance.to_dict()
        return Routing(
            self.get_unique_name(name),
            list(points) if points is not None else _result_points(result),
            # Display frontiers are derived from the result we just saved, so
            # the live route renders immediately without a store round-trip.
            contour_frontiers(result),
            collection=self,
            provenance=provenance,
            result_ref=ref,
            result_summary=result_summary(result),
            result_verified=True,
            inspection_context=inspection_context,
        )

    def import_from_gpx(self, gpx):
        for track in gpx.routes:
            waypoints = []

            for segment in track.segments:
                for point in segment.points:
                    waypoints.append([point.latitude, point.longitude])

            self.append(Routing(track.name, waypoints, collection=self))


RoutingCollection = _RoutingCollection[Routing]
