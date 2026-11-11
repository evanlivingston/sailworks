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

from copy import deepcopy

import gpxpy
from weatherrouting import IsoPoint

from sailworks.core.geo.track import Track

_ARTIFACT_FIELDS = (
    "artifact_version",
    "artifact_id",
    "sha256",
    "byte_length",
    "media_type",
    "uri",
)

_RESULT_SUMMARY_FIELDS = (
    "schema_version",
    "calculation_id",
    "generation",
    "sequence",
    "status",
    "leg_count",
    "layer_count",
    "validation_state",
    "export_eligible",
)

_SUMMARY_INT_FIELDS = ("generation", "sequence", "leg_count", "layer_count")


def contour_frontiers(result):
    """Return retained result contours in the legacy display shape.

    Rendering consumes ``(lat, lon)`` pairs, so the contours can be shown
    without reconstructing legacy ``IsoPoint`` records or loading traces.
    """
    return [
        [(point.lat, point.lon) for point in contour.points]
        for layer in result.layers
        for contour in layer.contours
    ]


def _json_value(value):
    """Return a JSON-serialisable copy of record-like metadata."""
    if value is None:
        return None
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return deepcopy(value)


def _artifact_ref_from_json(value):
    from sailworks.core.marine.routingrecord import ArtifactRef, SchemaError

    if not isinstance(value, dict):
        raise SchemaError("result_ref must be an object")
    missing = [field for field in _ARTIFACT_FIELDS if field not in value]
    if missing:
        raise SchemaError("result_ref is missing fields: " + ", ".join(sorted(missing)))

    return ArtifactRef(**{field: value[field] for field in _ARTIFACT_FIELDS})


def _summary_from_json(value):
    """Validate the small metadata duplicated in the collection JSON."""
    from sailworks.core.marine.routingrecord import SchemaError
    from sailworks.core.marine.routingresult import VALIDATION_STATES

    if not isinstance(value, dict):
        raise SchemaError("result_summary must be an object")
    missing = [field for field in _RESULT_SUMMARY_FIELDS if field not in value]
    if missing:
        raise SchemaError(
            "result_summary is missing fields: " + ", ".join(sorted(missing))
        )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise SchemaError("result_summary schema_version must be 1")
    for field in _SUMMARY_INT_FIELDS:
        item = value[field]
        if type(item) is not int or item < 0:
            raise SchemaError(f"result_summary {field} must be a non-negative integer")
    for field in ("calculation_id", "status"):
        item = value[field]
        if not isinstance(item, str) or not item:
            raise SchemaError(f"result_summary {field} must be a non-empty string")
    if value["validation_state"] not in VALIDATION_STATES:
        raise SchemaError("result_summary validation_state is not recognised")
    if type(value["export_eligible"]) is not bool:
        raise SchemaError("result_summary export_eligible must be a boolean")
    return deepcopy(value)


class Routing(Track):
    def __init__(
        self,
        name,
        points=[],
        isochrones=[],
        visible=True,
        collection=None,
        provenance=None,
        result_ref=None,
        result_summary=None,
        result_verified=False,
    ):
        super().__init__(name, points, visible, collection)
        # None means schema-v1 contours have not been hydrated for display yet.
        self._isochrones = isochrones
        # None explicitly means provenance is unknown, including legacy routes.
        self.provenance = deepcopy(provenance)
        # Schema-v1 routes keep only a content-addressed reference and a small
        # summary in the collection; the full result stays in the artifact store
        # until display or inspection explicitly asks for it.
        self.result_ref = result_ref
        self.result_summary = deepcopy(result_summary)
        # Eligibility is only trusted after this session verified the artifact.
        self._result_verified = bool(result_verified) and result_ref is not None

    @property
    def isochrones(self):
        """Display frontiers, hydrated from the artifact only on first access.

        Legacy routes and freshly calculated schema routes already carry their
        frontiers. A route reloaded from the collection starts with ``None`` so
        opening the collection never reads the artifact store or its traces.
        """
        if self._isochrones is not None:
            return self._isochrones
        store = self._attached_store()
        if self.result_ref is None or store is None:
            self._isochrones = []
            return self._isochrones
        self._isochrones = contour_frontiers(self.load_result(store))
        return self._isochrones

    @isochrones.setter
    def isochrones(self, value):
        self._isochrones = value

    def _attached_store(self):
        collection = getattr(self, "collection", None)
        return getattr(collection, "result_store", None)

    @property
    def is_legacy(self):
        return self.result_ref is None

    @property
    def validation_state(self):
        if self.result_ref is None:
            from sailworks.core.marine.routingresult import legacy_validation_state

            return legacy_validation_state(self.provenance).state
        summary = self.result_summary if isinstance(self.result_summary, dict) else {}
        return str(summary.get("validation_state", "unknown"))

    @property
    def export_eligible(self):
        """Whether the referenced result passed the schema-v1 export gate.

        Legacy routes have no schema-v1 result, so they remain unknown and can
        never become export eligible merely by being loaded. Schema-v1 routes
        stay pessimistic until this session has verified the artifact, so
        editing the mutable summary in the collection cannot forge eligibility.
        """
        if self.result_ref is None or not self._result_verified:
            return False
        summary = self.result_summary if isinstance(self.result_summary, dict) else {}
        return summary.get("export_eligible") is True

    def load_result(self, store):
        """Load and verify the referenced schema-v1 result through ``store``.

        The store verifies the artifact digest and length, and the schema
        reference is checked against the summary before the result is trusted.
        A successful load refreshes the summary from the artifact, which also
        unlocks the export gate for exactly what the artifact says.
        """
        if self.result_ref is None:
            raise LookupError("route has no persisted routing result")
        from sailworks.core.geo.routingcollection import result_summary
        from sailworks.core.marine.routingrecord import SchemaError
        from sailworks.core.marine.routingresult import RoutingResult

        result = RoutingResult.from_dict(store.load_json(self.result_ref))
        summary = self.result_summary if isinstance(self.result_summary, dict) else {}
        claimed = summary.get("calculation_id")
        if claimed is not None and claimed != result.calculation_id:
            raise SchemaError("result artifact does not match the saved summary")
        self.result_summary = result_summary(result)
        self._result_verified = True
        return result

    def to_gpx_object(self):
        gpx_route = gpxpy.gpx.GPXRoute()

        for x in self.points:
            gpx_route.points.append(gpxpy.gpx.GPXRoutePoint(x[0], x[1]))

        return gpx_route

    def to_json(self):
        c = super().to_json()
        if self.result_ref is not None:
            c["result_schema"] = 1
            c["result_ref"] = self.result_ref.to_dict()
            c["result_summary"] = _json_value(self.result_summary or {})
            c["provenance"] = _json_value(self.provenance)
            # Isochrones are deliberately omitted: the referenced artifact is
            # authoritative and duplicating them would grow the collection file
            # without adding information.
            return c
        c["isochrones"] = list(
            map(lambda x: list(map(lambda y: y.to_list(), x)), self.isochrones)
        )
        c["provenance"] = deepcopy(self.provenance)
        return c

    @staticmethod
    def from_json(j):
        d = Track.from_json(j)
        result_ref = j.get("result_ref")
        result_schema = j.get("result_schema")
        if result_ref is not None or result_schema is not None:
            from sailworks.core.marine.routingrecord import SchemaError

            if type(result_schema) is not int or result_schema != 1:
                raise SchemaError("unsupported result_schema")
            if result_ref is None:
                raise SchemaError("result_schema requires result_ref")
            return Routing(
                d.name,
                d.points,
                None,
                d.visible,
                provenance=j.get("provenance"),
                result_ref=_artifact_ref_from_json(result_ref),
                result_summary=_summary_from_json(j.get("result_summary")),
            )
        ic = list(map(lambda x: list(map(IsoPoint.from_list, x)), j["isochrones"]))
        return Routing(d.name, d.points, ic, d.visible, provenance=j.get("provenance"))
