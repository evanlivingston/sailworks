"""Persistence tests for legacy routes and referenced schema-v1 results."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import sailworks.core.storage as storage
from sailworks.core.geo.routing import Routing
from sailworks.core.geo.routingcollection import RoutingCollection
from sailworks.core.marine.routingrecord import (
    Capabilities,
    Contour,
    EngineIdentity,
    Layer,
    Leg,
    Observation,
    Outcome,
    Point,
    Provenance,
    SchemaError,
    Trace,
    Validation,
)
from sailworks.core.marine.routingresult import RoutingResult, canonical_utc
from sailworks.core.marine.routingresultstore import RoutingResultStore

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000148"
OTHER = "6f1c1a1e-0000-4000-8000-000000000149"
EPOCH = 1767225600
LATER = 1767229200
TREE = "a" * 64


def _result(calculation_id=CALCULATION) -> RoutingResult:
    legs = (
        Leg(
            "leg-1",
            Point(48.0, -124.0),
            Point(48.5, -123.5),
            EPOCH,
            canonical_utc(EPOCH),
            LATER,
            canonical_utc(LATER),
            {"wind_speed_ms": Observation(6.0, "m/s", "reported")},
            {"heading_degrees": 90.0, "tack": "starboard"},
        ),
        Leg(
            "leg-2",
            Point(48.5, -123.5),
            Point(48.8, -123.0),
            LATER,
            canonical_utc(LATER),
            LATER + 1200,
            canonical_utc(LATER + 1200),
            None,
        ),
    )
    layers = (
        Layer(
            "layer-1",
            EPOCH,
            canonical_utc(EPOCH),
            False,
            (
                Contour(
                    "contour-1",
                    True,
                    (
                        Point(48.0, -124.0),
                        Point(48.0, -123.0),
                        Point(48.5, -123.0),
                    ),
                ),
                Contour("contour-2", False, (Point(48.1, -124.1),)),
            ),
        ),
        Layer(
            "layer-2",
            LATER,
            canonical_utc(LATER),
            True,
            (Contour("contour-3", False, (Point(48.2, -123.9),)),),
        ),
    )
    traces = (
        Trace(
            "trace-1",
            Point(48.5, -123.5),
            (Point(48.0, -124.0), Point(48.5, -123.5)),
            (EPOCH, LATER),
        ),
    )
    return RoutingResult(
        calculation_id=calculation_id,
        generation=1,
        sequence=4,
        status="complete",
        outcome=Outcome(True, True, Validation("passed", True)),
        engine_identity=EngineIdentity(
            "https://example.invalid/sailworks-native",
            "deadbeef",
            "1.2.3",
            "1.2",
            TREE,
            None,
        ),
        provenance=Provenance(canonical_utc(EPOCH), "recorded"),
        legs=legs,
        layers=layers,
        capabilities=Capabilities(
            producer="synthetic",
            trace_times="per_position",
            validation_mode="sampled",
            sample_count=12,
            cadence=3,
        ),
        traces=traces,
    )


def _collection(tmp_path, monkeypatch, *, max_entries=8):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    store = RoutingResultStore(tmp_path / "artifacts", max_entries=max_entries)
    collection = RoutingCollection(result_store=store)
    # Keep assertions focused on artifact persistence, not the collection file.
    collection.save = lambda: None
    return collection, store


def test_legacy_routing_json_roundtrips_unchanged(tmp_path, monkeypatch):
    payload = {
        "uid": "legacy-route",
        "name": "legacy",
        "visible": True,
        "points": [[48.0, -124.0], [48.5, -123.5]],
        "isochrones": [
            [
                [
                    48.0,
                    -124.0,
                    -1,
                    EPOCH,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    [0.0, 0.0],
                ]
            ]
        ],
        "provenance": {"source": "legacy"},
    }

    collection, _ = _collection(tmp_path, monkeypatch)
    collection.load_json({"elements": [payload]})
    route = collection[0]

    assert route.is_legacy
    assert route.provenance == {"source": "legacy"}
    assert route.points == payload["points"]
    # A legacy payload keeps its validation state: absent metadata stays unknown.
    assert route.validation_state == "unknown"
    assert route.export_eligible is False
    assert route.to_json() == payload
    assert collection.to_json()["elements"] == [payload]


def test_legacy_explicit_validation_is_not_upgraded_by_loading():
    route = Routing.from_json(
        {
            "name": "legacy",
            "visible": True,
            "points": [[48.0, -124.0]],
            "isochrones": [],
            "provenance": {"validation": {"state": "passed"}},
        }
    )

    assert route.validation_state == "passed"
    # Independent validation is never implied by a legacy summary.
    assert route.export_eligible is False


def test_schema_payload_requires_reference_and_valid_summary(tmp_path, monkeypatch):
    collection, _ = _collection(tmp_path, monkeypatch)
    payload = collection.save_result("alpha", _result()).to_json()

    with pytest.raises(SchemaError):
        Routing.from_json(
            {key: value for key, value in payload.items() if key != "result_schema"}
        )
    with pytest.raises(SchemaError):
        Routing.from_json({**payload, "result_schema": 2})
    with pytest.raises(SchemaError):
        Routing.from_json({**payload, "result_summary": {"schema_version": 1}})
    with pytest.raises(SchemaError):
        Routing.from_json(
            {**payload, "result_ref": {**payload["result_ref"], "sha256": "bad"}}
        )

    without_ref = {key: value for key, value in payload.items() if key != "result_ref"}
    with pytest.raises(SchemaError):
        Routing.from_json(without_ref)


def test_reloaded_schema_eligibility_requires_verified_artifact(tmp_path, monkeypatch):
    collection, store = _collection(tmp_path, monkeypatch)
    ineligible = replace(
        _result(),
        status="failed",
        outcome=Outcome(False, False, Validation("unknown", False)),
    )
    forged_route = collection.save_result("forged", ineligible)
    assert forged_route.export_eligible is False
    forged = forged_route.to_json()
    forged["result_summary"]["export_eligible"] = True

    reader = RoutingCollection(result_store=store)
    reader.load_json({"elements": [forged], "activeElement": None})
    reloaded = reader.elements[0]

    # A mutable summary alone cannot promote an ineligible artifact.
    assert reloaded.export_eligible is False
    assert reader.load_result(reloaded).status == "failed"
    assert reloaded.export_eligible is False
    assert reloaded.result_summary["export_eligible"] is False


def test_eligible_schema_route_unlocks_export_after_verification(tmp_path, monkeypatch):
    collection, store = _collection(tmp_path, monkeypatch)
    payload = collection.save_result("alpha", _result()).to_json()
    payload["result_summary"]["export_eligible"] = False

    reader = RoutingCollection(result_store=store)
    reader.load_json({"elements": [payload], "activeElement": None})
    reloaded = reader.elements[0]

    # Pessimistic until the artifact is verified, even if the summary claims no.
    assert reloaded.export_eligible is False
    reader.load_result(reloaded)
    # Verification refreshes the summary from the artifact itself.
    assert reloaded.export_eligible is True


def test_schema_result_persists_reference_without_duplicate_isochrones(
    tmp_path, monkeypatch
):
    collection, store = _collection(tmp_path, monkeypatch)
    result = _result()

    route = collection.save_result("alpha", result)
    payload = route.to_json()

    assert payload["result_schema"] == 1
    assert "isochrones" not in payload
    assert payload["result_ref"]["sha256"] == route.result_ref.sha256
    assert route.result_summary["calculation_id"] == CALCULATION
    assert route.result_summary["leg_count"] == 2
    assert route.validation_state == "passed"
    assert route.export_eligible is True
    assert [point for point in route.points] == [
        (48.0, -124.0),
        (48.5, -123.5),
        (48.8, -123.0),
    ]
    # The just-saved route renders its retained contours without a store read.
    assert route.isochrones == [
        [(48.0, -124.0), (48.0, -123.0), (48.5, -123.0)],
        [(48.1, -124.1)],
        [(48.2, -123.9)],
    ]

    reloaded_route = Routing.from_json(payload)
    assert reloaded_route.result_ref == route.result_ref
    # No collection is attached, so display cannot hydrate yet; the empty
    # result must not be cached, or attaching later would never load.
    assert reloaded_route.isochrones == ()
    reloaded_route.collection = collection
    assert reloaded_route.isochrones == [
        [(48.0, -124.0), (48.0, -123.0), (48.5, -123.0)],
        [(48.1, -124.1)],
        [(48.2, -123.9)],
    ]
    assert RoutingResult.from_dict(store.load_json(route.result_ref)) == result


def test_collection_reload_defers_artifact_load_and_supports_offline_inspection(
    tmp_path, monkeypatch
):
    writer, store = _collection(tmp_path, monkeypatch, max_entries=1)
    first = writer.save_result("one", _result())
    second = writer.save_result("two", _result(OTHER))

    reader = RoutingCollection(result_store=store)
    reader.load_json(
        {
            "elements": [first.to_json(), second.to_json()],
            "activeElement": None,
        }
    )

    # Starting up neither reads the artifact nor duplicates its isochrones.
    assert store.cache_entries == 0
    assert reader.elements[0].load_result  # method exists without eager state
    # Serialising the collection must also stay artifact-free.
    assert "isochrones" not in reader.to_json()["elements"][0]
    assert store.cache_entries == 0

    # Explicit display access hydrates the retained contours exactly once.
    assert reader.elements[0].isochrones == [
        [(48.0, -124.0), (48.0, -123.0), (48.5, -123.0)],
        [(48.1, -124.1)],
        [(48.2, -123.9)],
    ]
    assert store.cache_entries == 1
    assert reader.elements[0].isochrones[0][0] == (48.0, -124.0)
    assert store.cache_entries == 1
    assert store.cached_ids == (first.result_ref.artifact_id,)

    result = reader.load_result(reader.elements[0])
    assert [layer.layer_id for layer in result.layers] == ["layer-1", "layer-2"]
    assert [contour.contour_id for contour in result.layers[0].contours] == [
        "contour-1",
        "contour-2",
    ]
    assert result.traces[0].times == (EPOCH, LATER)
    assert result.provenance.to_dict() == reader.elements[0].provenance

    inspector = reader.inspector(reader.elements[0])
    readout = inspector.leg_readout("leg-1")
    assert readout["values"]["heading_degrees"] == 90.0
    assert "estimated_fuel_litres" in readout["missing"]
    assert inspector.nearest_layer(LATER).layer_id == "layer-2"
    assert inspector.trace_at(48.5, -123.5).times == (EPOCH, LATER)

    # The store keeps at most one artifact; each fresh route evicts the other.
    assert store.cached_ids == (first.result_ref.artifact_id,)
    reader.load_result(reader.elements[1])
    assert store.cached_ids == (second.result_ref.artifact_id,)
    reader.load_result(reader.elements[0])
    assert store.cached_ids == (first.result_ref.artifact_id,)
    assert store.cache_entries == 1


def test_controller_saves_schema_result_by_reference(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from sailworks.qt.routingcontroller import RoutingController

    collection, store = _collection(tmp_path, monkeypatch)
    host = SimpleNamespace(
        core=SimpleNamespace(routingManager=collection),
        error="",
        saved_count=0,
        completed=SimpleNamespace(emit=lambda route: None),
    )

    RoutingController._save_result(host, SimpleNamespace(name="alpha"), _result())

    route = collection.get_by_name("alpha")
    assert host.saved_count == 1
    assert route.result_ref is not None
    assert route.export_eligible is True
    assert "isochrones" not in route.to_json()
    assert store.load_json(route.result_ref)["calculation_id"] == CALCULATION


def test_controller_reports_routing_store_errors(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from sailworks.qt.routingcontroller import RoutingController

    collection, _ = _collection(tmp_path, monkeypatch)

    def fail(*args, **kwargs):
        raise SchemaError("artifact store unavailable")

    monkeypatch.setattr(collection, "save_result", fail)
    host = SimpleNamespace(
        core=SimpleNamespace(routingManager=collection),
        error="",
        saved_count=0,
        completed=SimpleNamespace(emit=lambda route: None),
    )

    with pytest.raises(ValueError, match="could not be saved"):
        RoutingController._save_result(host, SimpleNamespace(name="alpha"), _result())

    assert "artifact store unavailable" in host.error
    assert host.saved_count == 0


def test_missing_or_corrupt_artifact_shows_no_contours_without_raising(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    writer, store = _collection(tmp_path, monkeypatch)
    route = writer.save_result("alpha", _result())
    reloaded = Routing.from_json(route.to_json())
    reloaded.collection = writer

    # Remove the artifact: a restored collection without the store file must
    # degrade to "no contours" rather than raising from a display path.
    store._path(reloaded.result_ref.artifact_id).unlink()

    assert reloaded.isochrones == ()
    assert reloaded.contour_error is not None
    assert reloaded.export_eligible is False


def test_artifact_mismatched_generation_is_rejected(tmp_path, monkeypatch):
    writer, store = _collection(tmp_path, monkeypatch)
    route = writer.save_result("alpha", _result())
    reloaded = Routing.from_json(route.to_json())
    reloaded.collection = writer
    reloaded.result_summary = dict(reloaded.result_summary, generation=99)

    assert reloaded.isochrones == ()
    assert "generation" in (reloaded.contour_error or "")
