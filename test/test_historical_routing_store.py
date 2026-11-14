"""Durable historical job store: atomicity, publication ledger, pins, recovery."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

import sailworks.core.historicalroutingrecords as records_module
import sailworks.core.historicalroutingstore as store_module
from sailworks.core.historicalrouting import HistoricalMemberPlan
from sailworks.core.historicalroutingrecords import (
    SCHEMA_VERSION,
    AcquisitionRecord,
    JobRecord,
    MemberRecord,
    YearRecord,
    computation_key,
    str_tuple,
)
from sailworks.core.historicalroutingstore import HistoricalRoutingStore, file_digest

UTC = timezone.utc
DEPARTURE = datetime(2019, 6, 1, 12, 0, tzinfo=UTC)


def make_member(year=2019, offset=0, start=None, hours=240):
    start = start or datetime(year, 6, 1, 12, 0, tzinfo=UTC) + timedelta(days=offset)
    return HistoricalMemberPlan(
        member_id=f"{year:04d}-{offset:+03d}",
        year=year,
        offset_days=offset,
        intended_wall_datetime=start.replace(tzinfo=None),
        resolved_departure_utc=start,
        calendar_adjustments=(),
        coverage_start_utc=start.replace(minute=0, second=0, microsecond=0),
        coverage_end_utc=start + timedelta(hours=hours),
    )


def make_store(tmp_path) -> HistoricalRoutingStore:
    return HistoricalRoutingStore(tmp_path / "store")


def test_store_module_reexports_the_record_names():
    assert store_module.AcquisitionRecord is AcquisitionRecord
    assert store_module.MemberRecord is MemberRecord
    assert store_module.YearRecord is YearRecord
    assert store_module.JobRecord is JobRecord
    assert store_module.computation_key is computation_key
    assert store_module.SCHEMA_VERSION == SCHEMA_VERSION == 1
    assert store_module.str_tuple is str_tuple
    assert "str_tuple" in records_module.__all__
    assert "AcquisitionRecord" in records_module.__all__
    assert "HistoricalRoutingStore" in store_module.__all__


def test_create_writes_lazily_and_round_trips(tmp_path):
    store = make_store(tmp_path)
    assert not (tmp_path / "store").exists()

    record = store.create(
        {"years": 2, "region": [-10.0, 40.0, 5.0, 50.0]},
        [make_member(2018), make_member(2019, 0), make_member(2019, 1)],
    )

    assert (tmp_path / "store" / "jobs" / f"{record.job_id}.json").is_file()
    assert record.state == "planned"
    assert record.requested_years == (2018, 2019)
    assert set(record.members) == {"2018-+00", "2019-+00", "2019-+01"}
    assert record.members["2018-+00"].departure_utc.startswith("2018-")

    loaded = store.load(record.job_id)
    assert loaded.to_dict() == record.to_dict()
    assert isinstance(loaded.members["2019-+00"], MemberRecord)
    assert loaded.years[2019] == YearRecord(year=2019, state="eligible")


def test_record_round_trip_through_plain_json():
    acquisition = AcquisitionRecord(
        unit_id="u" * 64,
        state="ready",
        start_utc="2020-01-01T00:00:00+00:00",
        end_utc="2020-01-02T00:00:00+00:00",
        artifact_hashes=("a" * 64, "b" * 64),
        error="",
        attempts=2,
    )
    member = MemberRecord(
        member_id="2019-+00",
        year=2019,
        offset_days=0,
        state="finished",
        departure_utc="2019-06-01T12:00:00+00:00",
        coverage_start_utc="2019-06-01T12:00:00+00:00",
        coverage_end_utc="2019-06-11T12:00:00+00:00",
        acquisition_unit_ids=("u" * 64,),
        binding_digest="c" * 64,
        route_uid="route-1",
        outcome="finished",
        attempts=1,
        computation_generation=2,
    )
    year = YearRecord(year=2019, state="excluded", reason="late", previous_reasons=("ok",))
    job = JobRecord(
        schema_version=SCHEMA_VERSION,
        job_id="job-1",
        created_at_utc="2026-01-01T00:00:00+00:00",
        spec={"years": 3},
        state="running",
        requested_years=(2018, 2019),
        effective_years=(2018,),
        excluded_years=(2019,),
        members={member.member_id: member},
        acquisitions={acquisition.unit_id: acquisition},
        years={2019: year},
        published={"k": "route-1"},
        pending_publications={"j": "2026-01-01T00:00:01+00:00"},
        artifact_pins=("d" * 64,),
        attempt_history=({"attempt": 1, "error": "boom"},),
        last_checkpoint_utc="2026-01-01T00:00:02+00:00",
        supersedes_job_id="job-0",
        scene_group_id="group-1",
        scene_detached=True,
    )

    for original, factory in (
        (acquisition, AcquisitionRecord),
        (member, MemberRecord),
        (year, YearRecord),
        (job, JobRecord),
    ):
        decoded = json.loads(json.dumps(original.to_dict()))
        assert factory.from_dict(decoded) == original
        # Plain JSON only: every leaf is a str, int, bool, float, list or dict.
        assert json.loads(json.dumps(decoded)) == decoded


def test_save_is_atomic_when_the_write_fails(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    record = store.create({"years": 1}, [make_member()])
    record.state = "running"
    record.attempt_history = ({"attempt": 1},)

    def boom(*_args, **_kwargs):
        raise OSError("simulated crash")

    monkeypatch.setattr(store_module.os, "replace", boom)
    with pytest.raises(OSError):
        store.save(record)
    monkeypatch.undo()

    assert store.load(record.job_id).state == "planned"
    assert store.load(record.job_id).attempt_history == ()
    assert [path.name for path in (tmp_path / "store" / "jobs").iterdir()] == [
        f"{record.job_id}.json"
    ]


def test_list_jobs_sorted_and_corrupt_files_are_reported(tmp_path):
    store = make_store(tmp_path)
    first = store.create({"years": 1}, [make_member(2018)])
    second = store.create({"years": 1}, [make_member(2019)])
    first.created_at_utc = "2020-01-01T00:00:00+00:00"
    second.created_at_utc = "2021-01-01T00:00:00+00:00"
    store.save(first)
    store.save(second)

    (tmp_path / "store" / "jobs" / "notes.txt").write_text("ignore me", encoding="utf8")
    corrupt = tmp_path / "store" / "jobs" / "broken.json"
    corrupt.write_text("{not json", encoding="utf8")

    jobs = store.list_jobs()
    assert [job.job_id for job in jobs] == [first.job_id, second.job_id]
    assert store.corrupt_jobs() == (corrupt,)
    assert store.list_jobs() == jobs


def test_quarantine_moves_the_unreadable_file(tmp_path):
    store = make_store(tmp_path)
    store.create({"years": 1}, [make_member()])
    corrupt = tmp_path / "store" / "jobs" / "broken.json"
    corrupt.write_text("{not json", encoding="utf8")
    assert store.corrupt_jobs() == ()

    store.list_jobs()
    assert store.corrupt_jobs() == (corrupt,)

    moved = store.quarantine("broken", "invalid JSON")
    assert moved == tmp_path / "store" / "jobs" / "quarantine" / "broken.json"
    assert moved.read_text(encoding="utf8") == "{not json"
    assert not corrupt.exists()
    assert (moved.parent / "broken.error.txt").read_text(encoding="utf8") == "invalid JSON\n"
    assert store.list_jobs()
    assert store.corrupt_jobs() == ()
    assert store.quarantine("missing", "nope") is None


def test_publication_intent_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    record = store.create({"years": 1}, [make_member()])
    key = computation_key(record.job_id, "2019-+00", 1)

    store.begin_publication(record, key)
    stamp = record.pending_publications[key]
    store.begin_publication(record, key)
    assert record.pending_publications == {key: stamp}
    assert store.load(record.job_id).pending_publications == {key: stamp}


def test_publication_requires_intent_and_detects_conflicts(tmp_path):
    store = make_store(tmp_path)
    record = store.create({"years": 1}, [make_member()])
    key = computation_key(record.job_id, "2019-+00", 1)

    with pytest.raises(KeyError):
        store.complete_publication(record, key, "route-1")

    store.begin_publication(record, key)
    store.complete_publication(record, key, "route-1")
    assert store.published_route(record, key) == "route-1"
    assert record.pending_publications == {}
    assert store.load(record.job_id).published == {key: "route-1"}

    # Same uid again is a no-op; a different uid is a real conflict.
    store.complete_publication(record, key, "route-1")
    assert store.published_route(record, key) == "route-1"
    with pytest.raises(ValueError):
        store.complete_publication(record, key, "route-2")
    assert store.load(record.job_id).published == {key: "route-1"}


def test_pins_are_shared_between_jobs_and_release_cleanly(tmp_path):
    store = make_store(tmp_path)
    first = store.create({"years": 1}, [make_member(2018)])
    second = store.create({"years": 1}, [make_member(2019)])
    digest = "a" * 64
    other = "b" * 64

    store.pin(first, [digest, other])
    store.pin(second, [digest])

    pins = json.loads((tmp_path / "store" / "pins.json").read_text(encoding="utf8"))
    assert pins == {digest: sorted([first.job_id, second.job_id]), other: [first.job_id]}
    assert store.is_pinned(digest)
    assert store.pinned_hashes() == {digest, other}
    assert store.load(first.job_id).artifact_pins == tuple(sorted([digest, other]))

    store.release(first)
    pins = json.loads((tmp_path / "store" / "pins.json").read_text(encoding="utf8"))
    assert pins == {digest: [second.job_id]}
    assert store.load(first.job_id).artifact_pins == ()
    assert not store.is_pinned(other)

    store.release(second)
    assert store.pinned_hashes() == set()
    assert store.is_pinned(digest) is False


def test_update_member_and_acquisition_persist(tmp_path):
    store = make_store(tmp_path)
    record = store.create({"years": 1}, [make_member()])
    record.acquisitions["unit-1"] = AcquisitionRecord(
        unit_id="unit-1",
        state="planned",
        start_utc="2019-06-01T12:00:00+00:00",
        end_utc="2019-06-02T00:00:00+00:00",
    )
    store.save(record)

    store.update_member(
        record,
        "2019-+00",
        state="finished",
        route_uid="route-1",
        outcome="finished",
        binding_digest="c" * 64,
        attempts=1,
    )
    store.update_acquisition(record, "unit-1", state="ready", artifact_hashes=["d" * 64])

    reloaded = store.load(record.job_id)
    assert reloaded.members["2019-+00"].state == "finished"
    assert reloaded.members["2019-+00"].route_uid == "route-1"
    assert reloaded.members["2019-+00"].attempts == 1
    assert reloaded.acquisitions["unit-1"].state == "ready"
    assert reloaded.acquisitions["unit-1"].artifact_hashes == ("d" * 64,)

    with pytest.raises(KeyError):
        store.update_member(record, "2099-+00", state="failed")
    with pytest.raises(KeyError):
        store.update_acquisition(record, "unit-9", state="failed")


def test_recovery_dispositions_without_mutation(tmp_path):
    store = make_store(tmp_path)
    published = store.create({"years": 1}, [make_member(2017)])
    resumable = store.create({"years": 1}, [make_member(2018)])
    waiting = store.create({"years": 1}, [make_member(2019)])

    # A crash after the route was recorded but before the ledger settled.
    record = store.load(published.job_id)
    record.state = "running"
    record.published["k1"] = "route-1"
    record.pending_publications["k1"] = "2026-01-01T00:00:00+00:00"
    store.save(record)
    store.set_job_state(resumable, "recovering")
    store.set_job_state(waiting, "planned")

    before = {path.name: path.read_bytes() for path in (tmp_path / "store" / "jobs").glob("*.json")}

    recovered = store.recover()

    assert [(item[0].job_id, item[1]) for item in recovered] == sorted(
        [
            (published.job_id, "published"),
            (resumable.job_id, "resume"),
            (waiting.job_id, "attention"),
        ]
    )
    after = {path.name: path.read_bytes() for path in (tmp_path / "store" / "jobs").glob("*.json")}
    assert before == after


def test_computation_key_and_file_digest(tmp_path):
    assert computation_key("job", "2019-+00", 3) == "job:2019-+00:3"
    path = tmp_path / "artifact.bin"
    payload = b"wind" * 500_000
    path.write_bytes(payload)
    assert file_digest(path) == hashlib.sha256(payload).hexdigest()
    assert file_digest(path) == file_digest(path)


def test_corrupt_pins_raise_and_are_never_rewritten(tmp_path):
    store = make_store(tmp_path)
    record = store.create({"years": 1}, [make_member()])
    store.pin(record, ["a" * 64])
    pins = tmp_path / "store" / "pins.json"
    pins.write_text("{not json", encoding="utf8")
    before = pins.read_bytes()

    with pytest.raises(ValueError):
        store.pin(record, ["b" * 64])
    assert pins.read_bytes() == before
    assert store.corrupt_pins() == pins

    with pytest.raises(ValueError):
        store.pinned_hashes()
    with pytest.raises(ValueError):
        store.release(record)
    assert pins.read_bytes() == before
    # The job record is untouched too: nothing was saved from the failed write.
    assert store.load(record.job_id).artifact_pins == ("a" * 64,)


def test_quarantine_does_not_overwrite_a_previous_capture(tmp_path):
    store = make_store(tmp_path)
    store.create({"years": 1}, [make_member()])
    jobs = tmp_path / "store" / "jobs"
    (jobs / "broken.json").write_text("{", encoding="utf8")

    first = store.quarantine("broken", "first failure")
    (jobs / "broken.json").write_text("}", encoding="utf8")
    second = store.quarantine("broken", "second failure")

    assert first == jobs / "quarantine" / "broken.json"
    assert second == jobs / "quarantine" / "broken.1.json"
    assert first.read_text(encoding="utf8") == "{"
    assert second.read_text(encoding="utf8") == "}"
    assert (first.parent / "broken.error.txt").read_text(encoding="utf8") == "first failure\n"
    assert (first.parent / "broken.1.error.txt").read_text(encoding="utf8") == "second failure\n"


def test_recovery_reports_corrupt_job_files(tmp_path):
    store = make_store(tmp_path)
    good = store.create({"years": 1}, [make_member()])
    corrupt = tmp_path / "store" / "jobs" / "broken.json"
    corrupt.write_text("{not json", encoding="utf8")

    recovered = store.recover()

    assert (good, "attention") in recovered
    assert recovered[-1] == (corrupt, "attention")


def test_job_record_current_binding_round_trips_and_defaults_empty(tmp_path):
    store = make_store(tmp_path)
    record = store.create({"years": 1}, [make_member(2019)])
    record.current_binding = {
        "field_ids": ["wind", "currents"],
        "field_hashes": ["a" * 64],
        "coverage_end_utc": "2019-06-11T12:00:00+00:00",
    }
    store.save(record)

    loaded = store.load(record.job_id)
    assert loaded.current_binding == record.current_binding
    assert JobRecord.from_dict(loaded.to_dict()).current_binding == record.current_binding

    path = tmp_path / "store" / "jobs" / f"{record.job_id}.json"
    payload = json.loads(path.read_text(encoding="utf8"))
    del payload["current_binding"]
    path.write_text(json.dumps(payload), encoding="utf8")
    assert store.load(record.job_id).current_binding == {}
