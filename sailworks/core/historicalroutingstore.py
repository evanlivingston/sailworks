"""Durable job records, publication ledger and artifact pins for historical jobs.

Every write is a temp file followed by ``os.replace`` plus an fsync of the file
and its containing directory, so a crash between the two leaves the previous
record intact. Reads never mutate: a malformed job file is reported through
``corrupt_jobs`` and skipped by ``list_jobs`` instead of aborting the listing.

Pins are shared across jobs, so ``pins.json`` is written before the job record
that references it: a crash can leak a pin, which is safe, but never leaves a
record pointing at a missing pin. An unreadable pin ledger raises instead of
being treated as empty, because rewriting it from a failed read would drop every
other job's pins.

Record types live in :mod:`sailworks.core.historicalroutingrecords` and are
re-exported here so callers need only one import.
"""

import hashlib
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None

from .historicalrouting import HistoricalMemberPlan
from .historicalroutingrecords import (
    SCHEMA_VERSION,
    AcquisitionRecord,
    JobRecord,
    MemberRecord,
    YearRecord,
    computation_key,
    str_tuple,
)

__all__ = [
    "SCHEMA_VERSION",
    "AcquisitionRecord",
    "MemberRecord",
    "YearRecord",
    "JobRecord",
    "HistoricalRoutingStore",
    "computation_key",
    "file_digest",
]


RESUMABLE_STATES = frozenset({"acquiring", "running", "recovering"})


def file_digest(path) -> str:
    """SHA-256 hex digest of a file, read in bounded chunks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class HistoricalRoutingStore:
    """Crash-safe job store rooted at ``root``; directories appear on first write."""

    def __init__(self, root):
        self.root = Path(root)
        self.jobs = self.root / "jobs"
        self.quarantine_dir = self.jobs / "quarantine"
        self.pins_path = self.root / "pins.json"
        self.lock_path = self.root / "pins.lock"
        self._corrupt: list = []
        self._corrupt_pins = None

    @staticmethod
    def _fsync_dir(path) -> None:
        handle = os.open(path, os.O_RDONLY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)

    def _ensure(self) -> None:
        self.jobs.mkdir(parents=True, exist_ok=True)

    def _write_text(self, path: Path, text: str) -> None:
        """Temp write, fsync, replace: the previous file survives a failed write."""
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
        committed = False
        try:
            try:
                stream = os.fdopen(handle, "w", encoding="utf8")
            except BaseException:
                os.close(handle)
                raise
            with stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            committed = True
            self._fsync_dir(path.parent)
        finally:
            if not committed and os.path.exists(name):
                os.unlink(name)

    def _write_json(self, path: Path, payload) -> None:
        self._write_text(path, json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def _read_json(self, path: Path):
        return json.loads(path.read_text(encoding="utf8"))

    def _job_path(self, job_id: str) -> Path:
        text = str(job_id)
        if not text or Path(text).name != text or text.startswith("."):
            raise ValueError(f"invalid job id: {job_id!r}")
        return self.jobs / f"{text}.json"

    def create(self, spec: dict, members: Iterable[HistoricalMemberPlan]) -> JobRecord:
        """Write a new planned job and return its record."""
        planned = list(members)
        job_id = str(uuid.uuid4())
        year_values = sorted({member.year for member in planned})
        record = JobRecord(
            schema_version=SCHEMA_VERSION,
            job_id=job_id,
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            spec=dict(spec),
            state="planned",
            requested_years=tuple(year_values),
            effective_years=tuple(year_values),
            excluded_years=(),
            members={
                member.member_id: MemberRecord(
                    member_id=member.member_id,
                    year=member.year,
                    offset_days=member.offset_days,
                    state="planned",
                    departure_utc=member.resolved_departure_utc.isoformat(),
                    coverage_start_utc=member.coverage_start_utc.isoformat(),
                    coverage_end_utc=member.coverage_end_utc.isoformat(),
                )
                for member in planned
            },
            years={year: YearRecord(year=year, state="eligible") for year in year_values},
        )
        self.save(record)
        return record

    def load(self, job_id: str) -> JobRecord:
        path = self._job_path(job_id)
        return JobRecord.from_dict(self._read_json(path))

    def list_jobs(self) -> list:
        """Every readable job, oldest first; corrupt files are only reported."""
        self._corrupt = []
        found: list = []
        if not self.jobs.is_dir():
            return found
        for path in sorted(self.jobs.glob("*.json")):
            try:
                record = JobRecord.from_dict(self._read_json(path))
                created = datetime.fromisoformat(record.created_at_utc.replace("Z", "+00:00"))
                if created.utcoffset() != timedelta(0):
                    raise ValueError("created_at_utc is not UTC")
            except (OSError, ValueError, KeyError, TypeError) as error:
                self._corrupt.append((path, str(error)))
                continue
            found.append((created, record))
        found.sort(key=lambda item: (item[0], item[1].job_id))
        return [record for _created, record in found]

    def corrupt_jobs(self) -> tuple:
        """Paths skipped by the most recent ``list_jobs`` call."""
        return tuple(path for path, _error in self._corrupt)

    def save(self, record: JobRecord) -> None:
        """Replace the record on disk; a foreign schema version is refused."""
        if record.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"cannot save schema version {record.schema_version}, expected {SCHEMA_VERSION}"
            )
        self._ensure()
        self._write_json(self._job_path(record.job_id), record.to_dict())

    def update_member(
        self,
        record: JobRecord,
        member_id: str,
        *,
        state=None,
        route_uid=None,
        outcome=None,
        error=None,
        binding_digest=None,
        attempts=None,
    ) -> JobRecord:
        member = record.members.get(member_id)
        if member is None:
            raise KeyError(member_id)
        if state is not None:
            member.state = str(state)
        if route_uid is not None:
            member.route_uid = str(route_uid)
        if outcome is not None:
            member.outcome = str(outcome)
        if error is not None:
            member.error = str(error)
        if binding_digest is not None:
            member.binding_digest = str(binding_digest)
        if attempts is not None:
            member.attempts = int(attempts)
        self.save(record)
        return record

    def update_acquisition(
        self,
        record: JobRecord,
        unit_id: str,
        *,
        state=None,
        artifact_hashes=None,
        error=None,
    ) -> JobRecord:
        unit = record.acquisitions.get(unit_id)
        if unit is None:
            raise KeyError(unit_id)
        if state is not None:
            unit.state = str(state)
        if artifact_hashes is not None:
            unit.artifact_hashes = str_tuple(artifact_hashes)
        if error is not None:
            unit.error = str(error)
        self.save(record)
        return record

    def set_job_state(self, record: JobRecord, state: str) -> JobRecord:
        record.state = str(state)
        self.save(record)
        return record

    def begin_publication(self, record: JobRecord, key: str) -> JobRecord:
        """Record publication intent without overwriting an existing one."""
        text = str(key)
        if text in record.published or text in record.pending_publications:
            return record
        record.pending_publications[text] = datetime.now(timezone.utc).isoformat()
        self.save(record)
        return record

    def complete_publication(self, record: JobRecord, key: str, route_uid: str) -> JobRecord:
        text = str(key)
        uid = str(route_uid)
        existing = record.published.get(text)
        if existing is not None:
            if existing == uid:
                return record
            raise ValueError(f"{text} is already published as {existing}, not {uid}")
        if text not in record.pending_publications:
            raise KeyError(text)
        del record.pending_publications[text]
        record.published[text] = uid
        self.save(record)
        return record

    def published_route(self, record: JobRecord, key: str):
        return record.published.get(str(key))

    @contextmanager
    def _pins_lock(self):
        """Serialize the ``pins.json`` read-modify-write across processes."""
        self.root.mkdir(parents=True, exist_ok=True)
        handle = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:  # pragma: no branch - POSIX path is the norm
                fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                os.close(handle)

    def corrupt_pins(self) -> Path | None:
        """``pins.json`` when the most recent pin read failed, else ``None``."""
        return self._corrupt_pins

    def _load_pins(self) -> dict:
        """Read the pin ledger; an absent file is empty, an unreadable one raises."""
        self._corrupt_pins = None
        if not self.pins_path.exists():
            return {}
        try:
            payload = self._read_json(self.pins_path)
            if not isinstance(payload, dict):
                raise ValueError("pin ledger must map digests to job ids")
        except (OSError, ValueError) as error:
            self._corrupt_pins = self.pins_path
            raise ValueError(f"pin ledger is unreadable: {error}") from error
        pins: dict = {}
        for digest, job_ids in payload.items():
            if isinstance(job_ids, list):
                pins[str(digest)] = sorted({str(value) for value in job_ids})
        return pins

    def _write_pins(self, pins: dict) -> None:
        self._write_json(self.pins_path, pins)

    def pin(self, record: JobRecord, hashes: Iterable[str]) -> JobRecord:
        """Pin artifact digests for this job; pins are shared across jobs."""
        self._ensure()
        digests = sorted({str(value) for value in hashes if str(value)})
        with self._pins_lock():
            pins = self._load_pins()
            for digest in digests:
                owners = set(pins.get(digest, ()))
                owners.add(record.job_id)
                pins[digest] = sorted(owners)
            self._write_pins(pins)
        if digests:
            record.artifact_pins = tuple(sorted(set(record.artifact_pins) | set(digests)))
        self.save(record)
        return record

    def release(self, record: JobRecord) -> JobRecord:
        """Drop this job from every pin entry, deleting entries left empty."""
        with self._pins_lock():
            pins = self._load_pins()
            for digest in list(pins):
                owners = [value for value in pins[digest] if value != record.job_id]
                if owners:
                    pins[digest] = owners
                else:
                    del pins[digest]
            self._write_pins(pins)
        record.artifact_pins = ()
        self.save(record)
        return record

    def is_pinned(self, digest: str) -> bool:
        return str(digest) in self._load_pins()

    def pinned_hashes(self) -> set:
        return set(self._load_pins())

    def recover(self) -> list:
        """What a restarted process should do; never writes.

        Readable jobs come first, ordered by job id; unreadable job files follow
        as ``(path, "attention")`` because only an operator can decide them.
        """
        pending: list = []
        for record in self.list_jobs():
            if any(key in record.published for key in record.pending_publications):
                disposition = "published"
            elif record.state in RESUMABLE_STATES:
                disposition = "resume"
            else:
                disposition = "attention"
            pending.append((record, disposition))
        pending.sort(key=lambda item: item[0].job_id)
        pending.extend((path, "attention") for path in self.corrupt_jobs())
        return pending

    def quarantine(self, job_id: str, error: str) -> Path | None:
        """Move an unreadable job file aside so listings stop failing on it."""
        source = self._job_path(job_id)
        if not source.is_file():
            return None
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        destination = self.quarantine_dir / source.name
        counter = 1
        while destination.exists():
            destination = self.quarantine_dir / f"{source.stem}.{counter}{source.suffix}"
            counter += 1
        os.replace(source, destination)
        self._write_text(self.quarantine_dir / f"{destination.stem}.error.txt", f"{error}\n")
        self._fsync_dir(self.jobs)
        return destination
