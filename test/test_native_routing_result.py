"""Focused schema-v1 routing result records, conversion and store tests."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from sailworks.core.marine.routingrecord import (
    ArtifactIntegrityError,
    ArtifactRef,
    Capabilities,
    Contour,
    EngineIdentity,
    Layer,
    Leg,
    Observation,
    Outcome,
    Point,
    Preview,
    Provenance,
    SchemaError,
    StaleResultError,
    Trace,
    Validation,
    canonical_utc,
)
from sailworks.core.marine.routingresult import (
    RoutingResult,
    export_blockers,
    export_eligible,
    legacy_validation_state,
    navigation_eligible,
)
from sailworks.core.marine.routingresultstore import RoutingResultStore

CALC = str(uuid.UUID(int=7))
EPOCH = 1767225600
LATER = 1767229200
CREATED = canonical_utc(EPOCH)
TREE = "a" * 64


def _engine():
    return EngineIdentity(
        "https://example.invalid/sailworks-native",
        "deadbeef",
        "1.2.3",
        "1.2",
        TREE,
        "b6258343592e104d754297d504c381ebae78947d",
    )


def _capabilities(**overrides):
    values = {
        "producer": "synthetic",
        "trace_times": "per_position",
        "validation_mode": "sampled",
        "sample_count": 12,
        "cadence": 3,
    }
    values.update(overrides)
    return Capabilities(**values)


def _outcome(
    state="passed", independent=True, accepted=True, delivered=True, diagnostics=None
):
    return Outcome(
        accepted,
        delivered,
        Validation(state, independent),
        {} if diagnostics is None else diagnostics,
    )


def _legs():
    return (
        Leg(
            "leg-1",
            Point(48.0, -124.0),
            Point(48.5, -123.5),
            EPOCH,
            canonical_utc(EPOCH),
            LATER,
            canonical_utc(LATER),
            {
                "wind_speed_ms": Observation(6.0, "m/s", "reported"),
                "current_speed_ms": Observation(None, "m/s", "unknown"),
            },
        ),
    )


def _layers():
    return (
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
                Contour(
                    "contour-2",
                    False,
                    (Point(48.1, -124.1), Point(48.2, -124.0)),
                ),
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


def _traces():
    return (
        Trace(
            "trace-1",
            Point(48.5, -123.5),
            (Point(48.0, -124.0), Point(48.5, -123.5)),
            (EPOCH, LATER),
        ),
    )


def _preview():
    return Preview(
        "preview-1",
        2,
        1,
        (Point(48.0, -124.0), Point(48.1, -124.0)),
    )


def _record(**overrides):
    values = {
        "calculation_id": CALC,
        "generation": 1,
        "sequence": 4,
        "status": "complete",
        "outcome": _outcome(),
        "engine_identity": _engine(),
        "provenance": Provenance(CREATED, "recorded"),
        "legs": _legs(),
        "layers": _layers(),
        "capabilities": _capabilities(),
        "traces": _traces(),
        "previews": (),
        "artifact_ref": None,
    }
    values.update(overrides)
    return RoutingResult(**values)


def test_unsatisfiable_rounding_status_and_provenance_roundings_round_trip():
    rounding = dict(
        mark={"latitude": 48.0, "longitude": -124.0},
        side="port",
        radius_nm=0.5,
        exit_bearing_min_deg=None,
        exit_bearing_max_deg=None,
        achieved_closest_distance_nm=None,
        achieved_terminal_bearing_deg=None,
        achieved_terminal_course_deg=None,
        satisfied=False,
        failure_code="side_unreachable",
    )
    record = _record(
        status="unsatisfiable_rounding",
        outcome=Outcome(False, False, Validation("unknown", False), {}),
        provenance=Provenance(CREATED, "recorded", (rounding,)),
        legs=(),
        layers=(),
        traces=(),
    )
    assert "status:unsatisfiable_rounding" in export_blockers(record)
    assert not export_eligible(record)
    encoded = record.to_dict()
    assert encoded["provenance"]["roundings"][0]["failure_code"] == "side_unreachable"
    decoded = RoutingResult.from_dict(encoded)
    assert decoded.provenance.roundings[0]["side"] == "port"
    assert decoded.status == "unsatisfiable_rounding"


def test_schema_roundtrip_preserves_contours_times_previews_and_order():
    record = _record(previews=(_preview(),))
    encoded = record.to_json()
    assert encoded == record.to_json()
    assert RoutingResult.from_json(encoded) == record
    native = record.to_native()
    assert RoutingResult.from_native(native) == record
    assert RoutingResult.from_native(native).to_native() == native

    contour = record.to_dict()["layers"][0]["contours"][0]
    assert contour["closed"] is True
    assert contour["points"] == [
        {"lat": 48.0, "lon": -124.0},
        {"lat": 48.0, "lon": -123.0},
        {"lat": 48.5, "lon": -123.0},
    ]
    assert record.to_dict()["layers"][0]["contours"][0]["points"][0] != (
        record.to_dict()["layers"][0]["contours"][0]["points"][-1]
    )
    assert record.traces[0].times == (EPOCH, LATER)
    preview = record.to_dict()["previews"][0]
    assert preview["provisional"] is True
    assert (preview["generation"], preview["sequence"]) == (2, 1)


def test_native_epoch_only_conversion_builds_explicit_canonical_utc():
    native = _record().to_native()
    assert "start_utc" not in native["legs"][0]
    assert "time_utc" not in native["layers"][0]
    assert "created_utc" not in native["provenance"]
    converted = RoutingResult.from_native(native)
    assert converted.legs[0].start_utc == "2026-01-01T00:00:00Z"
    assert converted.layers[0].time_utc == "2026-01-01T00:00:00Z"


def test_epoch_and_canonical_utc_must_agree():
    contour = _layers()[0].contours[0]
    with pytest.raises(SchemaError):
        Layer("layer-1", EPOCH, canonical_utc(LATER), False, (contour,))
    with pytest.raises(SchemaError):
        Leg(
            "leg-1",
            Point(48.0, -124.0),
            Point(48.5, -123.5),
            EPOCH,
            canonical_utc(LATER),
            LATER,
            canonical_utc(LATER),
        )
    with pytest.raises(SchemaError):
        Layer("layer-1", EPOCH, "2026-01-01T00:00:00.000Z", False, (contour,))
    data = _record().to_dict()
    data["layers"][0]["time_utc"] = canonical_utc(LATER)
    with pytest.raises(SchemaError):
        RoutingResult.from_dict(data)


@pytest.mark.parametrize(
    "lat,lon",
    [
        (float("nan"), 0.0),
        (0.0, float("inf")),
        (91.0, 0.0),
        (0.0, 181.0),
        (None, 0.0),
    ],
)
def test_coordinates_are_finite_in_range_or_rejected(lat, lon):
    with pytest.raises(SchemaError):
        Point(lat, lon)


def test_trace_times_align_and_missing_times_are_never_inferred():
    route = (Point(48.0, -124.0), Point(48.1, -124.1))
    with pytest.raises(SchemaError):
        Trace("trace-1", route[0], route, (EPOCH,))
    with pytest.raises(SchemaError):
        Trace("trace-1", route[0], route, (LATER, EPOCH))
    with pytest.raises(SchemaError):
        Trace("trace-1", route[0], route, (EPOCH, 1.5))

    absent = Trace("trace-1", route[0], route, None)
    record = _record(traces=(absent,))
    assert record.traces[0].times is None
    assert record.to_dict()["traces"][0]["times"] is None
    assert record.to_native()["traces"][0]["times"] is None
    assert RoutingResult.from_native(record.to_native()).traces[0].times is None


def test_standard_producer_declares_absent_trace_times():
    with pytest.raises(SchemaError):
        _capabilities(producer="standard", trace_times="per_position")
    absent = _record(
        traces=(Trace("trace-1", Point(48.5, -123.5), (Point(48.0, -124.0),)),),
        capabilities=_capabilities(producer="standard", trace_times="absent"),
    )
    assert absent.to_dict()["capabilities"]["backend"]["trace_times"] == "absent"
    assert absent.to_dict()["traces"][0]["times"] is None
    with pytest.raises(SchemaError):
        _record(capabilities=_capabilities(producer="standard", trace_times="absent"))


def test_identifiers_are_unique_and_contours_belong_to_one_layer():
    duplicate = Layer(
        "layer-2",
        LATER,
        canonical_utc(LATER),
        False,
        (Contour("contour-1", False, (Point(48.0, -124.0),)),),
    )
    with pytest.raises(SchemaError):
        _record(layers=_layers() + (duplicate,))
    crossed = Trace("leg-1", Point(48.5, -123.5), (Point(48.0, -124.0),))
    with pytest.raises(SchemaError):
        _record(traces=(crossed,))


def test_capabilities_and_unknown_fields_are_strict():
    data = _record().to_dict()
    data["capabilities"]["schema"]["contour_order"] = "sorted"
    with pytest.raises(SchemaError):
        RoutingResult.from_dict(data)
    data = _record().to_dict()
    data["surprise"] = 1
    with pytest.raises(SchemaError):
        RoutingResult.from_dict(data)
    data = _record().to_dict()
    data["schema_version"] = 2
    with pytest.raises(SchemaError):
        RoutingResult.from_dict(data)


def test_eligibility_requires_complete_accepted_delivered_independent_pass():
    assert export_eligible(_record())
    assert export_blockers(_record()) == ()
    cases = [
        {
            "status": "cancelled",
            "outcome": _outcome("unknown", False, False, False),
        },
        {
            "status": "failed",
            "outcome": _outcome("failed", True, False, False),
        },
        {
            "status": "incomplete",
            "outcome": _outcome("unknown", False, False, False),
        },
        {
            "status": "preview",
            "outcome": _outcome("unknown", False, False, False),
        },
        {"outcome": _outcome(accepted=False, delivered=False)},
        {"outcome": _outcome(delivered=False)},
        {"outcome": _outcome(independent=False)},
        {"outcome": _outcome("unknown", False)},
        {"outcome": _outcome("failed", True)},
        {"legs": ()},
    ]
    for overrides in cases:
        result = _record(**overrides)
        assert not export_eligible(result)
        assert not navigation_eligible(result)
        assert export_blockers(result)
    unproven = _record(outcome=_outcome(independent=False))
    assert "validation:not-independent" in export_blockers(unproven)


def test_export_eligibility_does_not_require_inspection_layers():
    result = _record(layers=())
    assert export_eligible(result)
    assert export_blockers(result) == ()
    assert result.to_dict()["layers"] == []
    assert result.to_dict()["capabilities"]["schema"]["contour_order"] == "emitted"


def test_independent_validation_without_backend_sampling_is_valid():
    result = _record(
        outcome=_outcome("passed", True),
        capabilities=_capabilities(
            validation_mode="none", sample_count=None, cadence=None
        ),
    )
    assert export_eligible(result)
    backend = result.to_dict()["capabilities"]["backend"]["validation"]
    assert backend == {"mode": "none", "sample_count": None, "cadence": None}


def test_empty_environment_is_allowed_with_absent_route_environment():
    empty = Leg(
        "leg-1",
        Point(48.0, -124.0),
        Point(48.5, -123.5),
        EPOCH,
        canonical_utc(EPOCH),
        LATER,
        canonical_utc(LATER),
        {},
    )
    result = _record(provenance=Provenance(CREATED, "absent"), legs=(empty,))
    assert not result.legs[0].environment
    with pytest.raises(SchemaError):
        _record(provenance=Provenance(CREATED, "absent"), legs=_legs())


def test_unknown_availability_cannot_carry_a_native_value():
    native = _record().to_native()
    native["legs"][0]["environment"]["depth_m"] = {
        "value": 12.5,
        "unit": "m",
        "availability": "unknown",
    }
    with pytest.raises(SchemaError):
        RoutingResult.from_native(native)


def test_standard_producer_match_is_case_insensitive():
    with pytest.raises(SchemaError):
        _capabilities(producer="Standard", trace_times="per_position")
    declared = _capabilities(producer="STANDARD", trace_times="absent")
    assert declared.producer == "STANDARD"


def test_absent_capability_counts_are_null_not_zero():
    absent = _capabilities(validation_mode="none", sample_count=None, cadence=None)
    record = _record(capabilities=absent)
    backend = record.to_dict()["capabilities"]["backend"]["validation"]
    assert backend == {"mode": "none", "sample_count": None, "cadence": None}
    assert RoutingResult.from_dict(record.to_dict()) == record
    for zero in (0, 1):
        with pytest.raises(SchemaError):
            _capabilities(validation_mode="none", sample_count=zero, cadence=None)
    with pytest.raises(SchemaError):
        _capabilities(validation_mode="sampled", sample_count=None, cadence=3)
    exhaustive = _capabilities(
        validation_mode="exhaustive", sample_count=None, cadence=None
    )
    assert exhaustive.to_dict()["backend"]["validation"]["sample_count"] is None


def test_artifact_uri_traversal_rejects_authority_and_path_segments():
    digest = "a" * 64
    allowed = ArtifactRef(
        1, digest, digest, 3, "text/plain", "https://example.invalid/a..b?next=.."
    )
    assert allowed.uri.endswith("?next=..")
    query_only = ArtifactRef(
        1, digest, digest, 3, "text/plain", "artifact://host/x?redirect=../y"
    )
    assert query_only.uri.endswith("?redirect=../y")
    ordinary = ArtifactRef(1, digest, digest, 3, "text/plain", "artifact://sha256/a")
    assert ordinary.uri == "artifact://sha256/a"
    for uri in (
        "artifact://../x",
        "artifact://..",
        "artifact://./x",
        "https://example.invalid/a/../b",
        "artifact://host/a/../b",
        "artifacts\\..\\b",
    ):
        with pytest.raises(SchemaError):
            ArtifactRef(1, digest, digest, 3, "text/plain", uri)


def test_status_and_outcome_flags_must_agree():
    for status in ("cancelled", "failed", "incomplete", "preview"):
        with pytest.raises(SchemaError):
            _record(status=status)
    with pytest.raises(SchemaError):
        _record(outcome=_outcome(accepted=False, delivered=True))
    with pytest.raises(SchemaError):
        _record(outcome=Outcome(False, False, Validation("unknown", True), {}))


def test_passed_flags_outside_validation_do_not_authorize_export():
    result = _record(
        outcome=_outcome(
            "unknown",
            False,
            diagnostics={"passed": True, "validation": {"passed": True}},
        )
    )
    assert not export_eligible(result)
    assert "validation:unknown" in export_blockers(result)
    assert legacy_validation_state({"passed": True}).state == "unknown"
    legacy = legacy_validation_state({"validation": {"passed": True}})
    assert (legacy.state, legacy.independent) == ("unknown", False)
    explicit = legacy_validation_state({"validation": {"state": "passed"}})
    assert (explicit.state, explicit.independent) == ("passed", False)


def test_previews_are_provisional_and_do_not_block_export():
    record = _record(previews=(_preview(),))
    assert export_eligible(record)
    assert export_blockers(record) == ()
    assert record.to_dict()["previews"][0]["provisional"] is True
    with pytest.raises(SchemaError):
        Preview("preview-1", 0, 0, (Point(48.0, -124.0),), provisional=False)


def test_previews_do_not_substitute_for_route_geometry():
    result = _record(legs=(), previews=(_preview(),))
    assert not export_eligible(result)
    assert not navigation_eligible(result)
    assert "route:no-legs" in export_blockers(result)


def test_preview_only_provisional_results_are_never_export_eligible():
    only_preview = _record(
        status="preview",
        outcome=_outcome("unknown", False, False, False),
        legs=(),
        layers=(),
        traces=(),
        previews=(_preview(),),
    )
    assert not export_eligible(only_preview)
    assert not navigation_eligible(only_preview)
    assert only_preview.to_dict()["previews"][0]["provisional"] is True


def test_supersession_requires_newer_generation_or_sequence():
    previous = _record(generation=3, sequence=5)
    _record(generation=3, sequence=6).ensure_supersedes(previous)
    _record(generation=4, sequence=0).ensure_supersedes(previous)
    with pytest.raises(StaleResultError):
        _record(generation=3, sequence=5).ensure_supersedes(previous)
    with pytest.raises(StaleResultError):
        _record(generation=3, sequence=4).ensure_supersedes(previous)
    with pytest.raises(StaleResultError):
        _record(generation=2, sequence=99).ensure_supersedes(previous)
    other = _record(calculation_id=str(uuid.UUID(int=8)))
    with pytest.raises(StaleResultError):
        other.ensure_supersedes(previous)


def test_nonfinite_or_missing_environment_is_explicit_unknown_not_zero():
    native = _record().to_native()
    native["legs"][0]["environment"] = {
        "wind_speed_ms": float("inf"),
        "wave_height_m": {
            "value": float("nan"),
            "unit": "m",
            "availability": "reported",
        },
        "current_speed_ms": None,
        "depth_m": 12.5,
    }
    result = RoutingResult.from_native(native)
    environment = result.legs[0].environment
    for key in ("wind_speed_ms", "wave_height_m", "current_speed_ms"):
        assert environment[key].value is None
        assert environment[key].availability == "unknown"
    assert environment["wave_height_m"].unit == "m"
    assert environment["depth_m"].value == 12.5
    assert environment["depth_m"].availability == "reported"
    data = result.to_dict()["legs"][0]["environment"]
    assert data["wind_speed_ms"]["value"] is None
    assert data["depth_m"]["value"] == 12.5
    assert "Infinity" not in result.to_json()
    assert "NaN" not in result.to_json()
    with pytest.raises(SchemaError):
        _record(provenance=Provenance(CREATED, "absent"))


def test_store_put_load_verifies_digest_length_and_detects_tampering(tmp_path):
    store = RoutingResultStore(tmp_path)
    assert store.cache_entries == 0
    payload = b'{"schema_version":1}'
    ref = store.put(payload)
    assert ref.artifact_id == hashlib.sha256(payload).hexdigest()
    assert ref.byte_length == len(payload)
    assert ref.artifact_version == 1
    assert store.cache_entries == 0
    assert store.load(ref) == payload
    assert store.load(ref.artifact_id) == payload
    assert store.load_json(ref) == {"schema_version": 1}
    record = _record(artifact_ref=ref)
    assert RoutingResult.from_json(record.to_json()) == record

    path = tmp_path / ref.artifact_id
    path.write_bytes(b'{"schema_version":2}')
    with pytest.raises(ArtifactIntegrityError):
        RoutingResultStore(tmp_path).load(ref)
    path.write_bytes(payload[:-1])
    with pytest.raises(ArtifactIntegrityError):
        RoutingResultStore(tmp_path).load(ref)


def test_store_cache_is_lazy_bounded_and_evicts_lru(tmp_path):
    store = RoutingResultStore(tmp_path, max_entries=2, max_bytes=100)
    refs = [store.put(bytes([index]) * 10) for index in range(3)]
    assert store.cached_ids == ()
    for index, ref in enumerate(refs):
        assert store.load(ref) == bytes([index]) * 10
    assert store.cache_entries == 2
    assert store.cached_ids == (refs[1].artifact_id, refs[2].artifact_id)
    store.load(refs[1])
    assert store.cached_ids == (refs[2].artifact_id, refs[1].artifact_id)

    byte_bounded = RoutingResultStore(tmp_path, max_entries=10, max_bytes=15)
    byte_bounded.load(refs[0])
    assert byte_bounded.cache_entries == 1
    byte_bounded.load(refs[1])
    assert byte_bounded.cache_entries == 1
    assert byte_bounded.cache_bytes == 10


def test_store_rejects_traversal_and_malformed_artifact_refs(tmp_path):
    store = RoutingResultStore(tmp_path)
    with pytest.raises(SchemaError):
        store.load("../escape")
    with pytest.raises(SchemaError):
        store.load("A" * 64)
    with pytest.raises(SchemaError):
        ArtifactRef(1, "../escape", "../escape", 0, "application/json", "artifact://x")
    with pytest.raises(SchemaError):
        ArtifactRef(1, "a" * 64, "b" * 64, 0, "application/json", "artifact://x")
    with pytest.raises(SchemaError):
        ArtifactRef(1, "a" * 64, "a" * 64, 0, "application/json", "artifact://../x")
