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


# Per-route visual modes saved with the route. "speed" is the historical
# default; the categorical modes read recorded leg metrics instead.
ROUTE_COLOUR_MODES = ("speed", "wind_angle", "tack")
DEFAULT_ROUTE_COLOUR_MODE = "speed"
ROUTE_COLOUR_MODE_LABELS = {
    "speed": "Speed",
    "wind_angle": "Upwind / Downwind",
    "tack": "Port / Starboard",
}

# Independent of the route-line colour mode: how the calculated contour layers
# themselves are drawn. "uniform" is the historical single blue/orange look;
# "time" colours each layer from its recorded epoch, oldest to newest.
ISOCHRONE_COLOUR_MODES = ("uniform", "time")
DEFAULT_ISOCHRONE_COLOUR_MODE = "uniform"
ISOCHRONE_COLOUR_MODE_LABELS = {
    "uniform": "Uniform",
    "time": "Time",
}


def normalize_route_colour_mode(value):
    """Return a supported mode, falling back to speed for missing/legacy data."""
    if isinstance(value, str) and value in ROUTE_COLOUR_MODES:
        return value
    return DEFAULT_ROUTE_COLOUR_MODE


def normalize_isochrone_colour_by(value):
    """Return a supported isochrone mode, defaulting to uniform."""
    if isinstance(value, str) and value in ISOCHRONE_COLOUR_MODES:
        return value
    return DEFAULT_ISOCHRONE_COLOUR_MODE


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
        inspection_context=None,
        isochrones_visible=True,
        colour_by=DEFAULT_ROUTE_COLOUR_MODE,
        isochrone_colour_by=DEFAULT_ISOCHRONE_COLOUR_MODE,
        traces_visible=False,
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
        # Layer, trace and leg selection saved with the referenced result so an
        # offline reload restores the same inspection context.
        self.inspection_context = deepcopy(inspection_context)
        # Eligibility is only trusted after this session verified the artifact.
        self._result_verified = bool(result_verified) and result_ref is not None
        # Set when a referenced artifact could not be read for display.
        self.contour_error = None
        # Independent display flag for the calculated contours. Scene
        # visibility keeps the route line itself; this only gates isochrones.
        self.isochrones_visible = bool(isochrones_visible)
        # Independent display flag for the route's trace web, separate from the
        # isochrones and from the route line. Off by default in every saved
        # route: the dense trace web makes the map unreadable, so it is only
        # drawn after the user asks for it.
        self._traces_visible = bool(traces_visible)
        # Visual mode for the drawn route line. Changing it bumps ``revision``
        # so overlay caches rebuild without touching the saved result.
        self._colour_by = normalize_route_colour_mode(colour_by)
        # Independent portrayal for the contour layers themselves. Changing it
        # bumps ``revision`` so overlay caches rebuild.
        self._isochrone_colour_by = normalize_isochrone_colour_by(isochrone_colour_by)
        # Lazily loaded per-leg readouts for the "tack" and sail-change
        # overlays; None means the saved result has not been read yet.
        self._leg_readouts = None
        self._leg_readouts_ref = None
        # A failed read is remembered so frames never retry artifact I/O; an
        # explicit mode change calls refresh_leg_tacks() to try again.
        self._leg_readouts_failed = False
        # Contour hydration gets the same treatment: a missing or corrupt
        # artifact is read once, then remembered until an explicit refresh or a
        # mode change asks for it again.
        self._contour_read_failed = False
        self._contour_read_ref = None

    @property
    def colour_by(self):
        return self._colour_by

    @colour_by.setter
    def colour_by(self, value):
        mode = normalize_route_colour_mode(value)
        if mode == self._colour_by:
            return
        self._colour_by = mode
        if self._leg_readouts_failed:
            self.refresh_leg_tacks()
        self.touch()

    @property
    def isochrone_colour_by(self):
        return self._isochrone_colour_by

    @isochrone_colour_by.setter
    def isochrone_colour_by(self, value):
        mode = normalize_isochrone_colour_by(value)
        if mode == self._isochrone_colour_by:
            return
        self._isochrone_colour_by = mode
        if self._contour_read_failed:
            # An explicit mode change is the retry point for contour colours,
            # the same way a route colour change retries the leg readouts.
            self.refresh_isochrones()
        self.touch()

    @property
    def traces_visible(self):
        return self._traces_visible

    @traces_visible.setter
    def traces_visible(self, value):
        visible = bool(value)
        if visible == self._traces_visible:
            return
        self._traces_visible = visible
        # Overlay and viewport caches key on the flag, so bump the revision to
        # invalidate them and repaint without re-reading the result artifact.
        self.touch()

    def refresh_leg_tacks(self):
        """Drop cached leg readouts so the next read retries once."""
        self._leg_readouts = None
        self._leg_readouts_ref = None
        self._leg_readouts_failed = False

    def _leg_readout_values(self):
        """Recorded readouts for each saved leg, in leg order.

        Artifact I/O happens lazily and is shared by every overlay that needs
        per-leg readouts (tack colour and sail-change markers). Legacy routes
        and unreadable artifacts yield no entries, which callers must render
        as unknown. A failed read stays failed until an explicit mode change
        refreshes it, so repeated frames do not hammer the artifact store.
        """
        # The cache records the artifact reference it was built from, so a
        # replaced reference rebuilds both tack colours and sail markers.
        if self._leg_readouts is not None and self._leg_readouts_ref != self.result_ref:
            self.refresh_leg_tacks()
        if self._leg_readouts_failed and self._leg_readouts_ref != self.result_ref:
            self.refresh_leg_tacks()
        if self._leg_readouts is not None:
            return self._leg_readouts
        if self._leg_readouts_failed:
            return ()
        if self.result_ref is None:
            self._leg_readouts = ()
            self._leg_readouts_ref = self.result_ref
            return self._leg_readouts
        store = self._attached_store()
        if store is None:
            # Stay unset so attaching a collection later can still load them.
            return ()
        try:
            result = self.load_result(store)
        except (OSError, KeyError, ValueError, LookupError) as exc:
            self._leg_readouts_failed = True
            self._leg_readouts_ref = self.result_ref
            self.contour_error = f"{type(exc).__name__}: {exc}"
            return ()
        self.contour_error = None
        self._leg_readouts = tuple(
            getattr(leg, "readout", None) or {} for leg in getattr(result, "legs", ())
        )
        self._leg_readouts_ref = self.result_ref
        return self._leg_readouts

    def leg_tacks(self):
        """Recorded per-leg tacks from the saved result, in leg order.

        Only the tack mode needs these values, so the artifact is read lazily
        and cached. Legacy routes and unreadable artifacts yield no entries,
        which callers must render as unknown rather than as port/starboard.
        """
        return tuple(readout.get("tack") for readout in self._leg_readout_values())

    def leg_profile_identities(self):
        """Recorded per-leg sail-plan identities, in leg order.

        The identity is the engine's ``profile_identity`` readout. When that
        text is absent the recorded ``sail_plan`` selector is used instead, so
        a plan change still marks at a plan boundary. A leg with neither value
        stays ``None`` so callers cannot mistake an unknown plan for a recorded
        one, and therefore never invent a sail change at an unknown boundary.
        """
        identities = []
        for readout in self._leg_readout_values():
            value = readout.get("profile_identity")
            identity = value if isinstance(value, str) and value else None
            if identity is None:
                plan = readout.get("sail_plan")
                if type(plan) in (int, float):
                    identity = str(int(plan)) if float(plan).is_integer() else str(plan)
            identities.append(identity)
        return tuple(identities)

    def refresh_isochrones(self):
        """Drop a recorded contour-read failure so the next access retries once.

        Explicit user actions (a colour-mode change or a caller-provided
        refresh) use this; repeated display frames must not.
        """
        self._contour_read_failed = False
        self._contour_read_ref = None
        self.contour_error = None

    @property
    def isochrones(self):
        """Display frontiers, hydrated from the artifact only on first access.

        Legacy routes and freshly calculated schema routes already carry their
        frontiers. A route reloaded from the collection starts with ``None`` so
        opening the collection never reads the artifact store or its traces.
        A failed read stays failed until an explicit refresh clears it, so
        repeated frames do not re-read the same missing artifact.
        """
        if self._isochrones is not None:
            return self._isochrones
        if self.result_ref is None:
            self._isochrones = []
            return self._isochrones
        if self._contour_read_failed and self._contour_read_ref != self.result_ref:
            # A replaced reference is a new artifact worth one attempt.
            self.refresh_isochrones()
        if self._contour_read_failed:
            return ()
        store = self._attached_store()
        if store is None:
            # No collection yet: leave the cache unset so a later attach still
            # hydrates the reference instead of pinning an empty result.
            return ()
        try:
            result = self.load_result(store)
        except (OSError, KeyError, ValueError) as exc:
            # Missing, pruned or corrupt artifact: show no contours rather than
            # raising from a display slot, and keep the route unverified. The
            # failure is remembered so every later frame does not retry it.
            self.contour_error = f"{type(exc).__name__}: {exc}"
            self._contour_read_failed = True
            self._contour_read_ref = self.result_ref
            return ()
        self.contour_error = None
        self._isochrones = contour_frontiers(result)
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
        for name, claimed_value, actual in (
            ("generation", summary.get("generation"), result.generation),
            ("sequence", summary.get("sequence"), result.sequence),
        ):
            if claimed_value is not None and claimed_value != actual:
                raise SchemaError(f"result artifact does not match the saved {name}")
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
            c["isochrones_visible"] = bool(self.isochrones_visible)
            if self.inspection_context is not None:
                c["inspection_context"] = _json_value(self.inspection_context)
            if self.colour_by != DEFAULT_ROUTE_COLOUR_MODE:
                c["colour_by"] = self.colour_by
            if self.isochrone_colour_by != DEFAULT_ISOCHRONE_COLOUR_MODE:
                c["isochrone_colour_by"] = self.isochrone_colour_by
            # Only serialise the non-default value so existing payloads keep
            # their shape and absent means off.
            if self._traces_visible:
                c["traces_visible"] = True
            # Isochrones are deliberately omitted: the referenced artifact is
            # authoritative and duplicating them would grow the collection file
            # without adding information.
            return c
        c["isochrones"] = list(
            map(lambda x: list(map(lambda y: y.to_list(), x)), self.isochrones)
        )
        c["isochrones_visible"] = bool(self.isochrones_visible)
        c["provenance"] = deepcopy(self.provenance)
        if self.colour_by != DEFAULT_ROUTE_COLOUR_MODE:
            c["colour_by"] = self.colour_by
        if self.isochrone_colour_by != DEFAULT_ISOCHRONE_COLOUR_MODE:
            c["isochrone_colour_by"] = self.isochrone_colour_by
        if self._traces_visible:
            c["traces_visible"] = True
        return c

    @staticmethod
    def from_json(j):
        d = Track.from_json(j)
        traces_visible = j.get("traces_visible", False)
        if type(traces_visible) is not bool:
            from sailworks.core.marine.routingrecord import SchemaError

            raise SchemaError("traces_visible must be a boolean")
        isochrones_visible = j.get("isochrones_visible", True)
        if type(isochrones_visible) is not bool:
            from sailworks.core.marine.routingrecord import SchemaError

            raise SchemaError("isochrones_visible must be a boolean")
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
                inspection_context=j.get("inspection_context"),
                isochrones_visible=isochrones_visible,
                colour_by=normalize_route_colour_mode(j.get("colour_by")),
                isochrone_colour_by=normalize_isochrone_colour_by(
                    j.get("isochrone_colour_by")
                ),
                traces_visible=traces_visible,
            )
        ic = list(map(lambda x: list(map(IsoPoint.from_list, x)), j["isochrones"]))
        return Routing(
            d.name,
            d.points,
            ic,
            d.visible,
            provenance=j.get("provenance"),
            isochrones_visible=isochrones_visible,
            colour_by=normalize_route_colour_mode(j.get("colour_by")),
            isochrone_colour_by=normalize_isochrone_colour_by(
                j.get("isochrone_colour_by")
            ),
            traces_visible=traces_visible,
        )
