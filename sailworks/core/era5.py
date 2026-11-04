"""Historical ERA5 request planning, retrieval, and independent cataloguing."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import fcntl
from dataclasses import dataclass
from datetime import datetime, time as daytime, timedelta, timezone
from pathlib import Path

from .forecasttranche import (
    AnalysisSelection,
    Artifact,
    ForecastTranche,
    ModelConfiguration,
    ModelEntry,
)

DATASET = "reanalysis-era5-single-levels"
FIELD_VARIABLES = {
    "wind": (
        "10m_u_component_of_wind",
        "10m_v_component_of_wind",
    ),
    "pressure": ("mean_sea_level_pressure",),
    "wave_height": ("significant_height_of_combined_wind_waves_and_swell",),
    "wave_period": ("mean_wave_period",),
    "wave_direction": ("mean_wave_direction",),
}
DEFAULT_FIELDS = ("wind", "pressure")
VARIABLES = FIELD_VARIABLES["wind"] + FIELD_VARIABLES["pressure"]
MAX_DAYS = 31
MAX_AREA_CELLS = 2_000_000
MAX_REQUEST_HOURS = 31 * 24
ERA5_EARLIEST = datetime(1940, 1, 1, tzinfo=timezone.utc)
LATENCY = timedelta(days=5)
# ERA5 is served on a 0.25 degree grid, so bounds that differ by less than
# this select exactly the same cells. Matching them keeps a request from being
# resubmitted just because the map region was rounded differently.
AREA_TOLERANCE = 0.01
# CDS leaves a request in "accepted" while it waits in the dataset queue, and
# that queue can run for hours (thousands of requests are often waiting). Only
# a request that has sat untouched for this long is treated as dead, so a real
# queue position is never thrown away.
STALE_ACCEPTED = timedelta(hours=6)
QUEUE_RETRY_SECONDS = 30
QUEUE_RETRY_ATTEMPTS = 20


class Era5Error(RuntimeError):
    """An actionable ERA5 acquisition or validation error."""


def _areas_close(left, right):
    return len(left) == len(right) and all(
        abs(float(a) - float(b)) <= AREA_TOLERANCE for a, b in zip(left, right)
    )


def _canonical_area(area):
    return tuple(round(float(value), 2) for value in area)


def _is_queue_limit_error(exc):
    text = str(exc).lower()
    return "maximum number queued" in text or "queued requests for this dataset" in text


@dataclass(frozen=True)
class Era5Request:
    start: datetime
    end: datetime
    area: tuple[float, float, float, float]
    request: dict


def job_key(plan):
    """Return the complete identity of an ERA5 job."""
    return (
        plan.start,
        plan.end,
        _canonical_area(plan.area),
        DATASET,
        tuple(sorted(plan.request.get("variable") or VARIABLES)),
    )


def plan_request(
    start, end, area, *, now=None, max_days=MAX_DAYS, fields=DEFAULT_FIELDS
):
    """Plan inclusive hourly UTC coverage for a bounded, non-wrapping area."""

    def utc(value):
        if not isinstance(value, datetime):
            raise ValueError("start and end must be datetimes")
        if value.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        return value.astimezone(timezone.utc)

    start, end = utc(start), utc(end)
    if (
        start.minute
        or start.second
        or start.microsecond
        or end.minute
        or end.second
        or end.microsecond
    ):
        raise ValueError("ERA5 historical coverage must use whole UTC hours")
    if start > end:
        raise ValueError("start must not be after end")
    if start < ERA5_EARLIEST:
        raise ValueError("ERA5 coverage starts on 1940-01-01")
    clock = utc(now) if now is not None else datetime.now(timezone.utc)
    if end > clock - LATENCY:
        raise ValueError(
            "ERA5 data is not considered available until five days after valid time"
        )
    north, west, south, east = _validate_area_and_limits(start, end, area, max_days)
    variables = _field_variables(fields)
    return Era5Request(
        start,
        end,
        (north, west, south, east),
        _request_payload(start, end, area, variables=variables),
    )


def _field_variables(fields):
    selected = tuple(fields)
    if not selected or any(field not in FIELD_VARIABLES for field in selected):
        raise ValueError("fields must be supported ERA5 field names")
    if len(set(selected)) != len(selected):
        raise ValueError("duplicate ERA5 field")
    return [name for field in selected for name in FIELD_VARIABLES[field]]


def _request_payload(start, end, area, *, hours=None, variables=None):
    """Build the CDS payload for a UTC range, optionally filtered to hours."""
    if hours is None:
        time = [f"{h:02d}:00" for h in range(24)]
    else:
        time = [f"{h:02d}:00" for h in hours]
        if not time:
            time = [f"{h:02d}:00" for h in range(24)]
    return {
        "product_type": ["reanalysis"],
        "variable": list(variables if variables is not None else VARIABLES),
        "year": sorted({f"{d.year:04d}" for d in _dates(start.date(), end.date())}),
        "month": sorted({f"{d.month:02d}" for d in _dates(start.date(), end.date())}),
        "day": sorted({f"{d.day:02d}" for d in _dates(start.date(), end.date())}),
        "time": time,
        "area": [float(value) for value in area],
        "data_format": "grib",
        "download_format": "unarchived",
    }


def _chunk_payload(start, end, area, *, variables=None):
    """CDS payload for one chunk: only the hours the chunk covers."""
    return _request_payload(
        start,
        end,
        area,
        hours=range(start.hour, end.hour + 1),
        variables=variables,
    )


def _validate_area_and_limits(start, end, area, max_days):
    try:
        north, west, south, east = (float(v) for v in area)
    except (TypeError, ValueError):
        raise ValueError("area must be north, west, south, east") from None
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise ValueError(
            "area must be valid north/west/south/east bounds; dateline wrapping is unsupported"
        )
    hours = int((end - start).total_seconds() // 3600) + 1
    grid_cells = (int((north - south) * 4) + 1) * (int((east - west) * 4) + 1)
    if grid_cells * hours > MAX_AREA_CELLS:
        raise ValueError("requested ERA5 area exceeds the bounded grid-size limit")
    days = (end.date() - start.date()).days + 1
    if days > max_days:
        raise ValueError(f"request spans {days} UTC days; maximum is {max_days}")
    if hours > MAX_REQUEST_HOURS:
        raise ValueError(f"request spans {hours} hours; maximum is {MAX_REQUEST_HOURS}")
    return north, west, south, east


def _dates(first, last):
    while first <= last:
        yield first
        first += timedelta(days=1)


def split_request(plan):
    """Return day-and-month bounded requests, filtering hours to coverage."""
    pieces = []
    cursor = plan.start
    while cursor <= plan.end:
        next_day = datetime.combine(
            cursor.date() + timedelta(days=1), daytime.min, timezone.utc
        )
        finish = min(plan.end, next_day - timedelta(hours=1))
        req = dict(plan.request)
        req.update(
            year=[f"{cursor.year:04d}"],
            month=[f"{cursor.month:02d}"],
            day=[f"{cursor.day:02d}"],
            time=[f"{h:02d}:00" for h in range(cursor.hour, finish.hour + 1)],
        )
        pieces.append(Era5Request(cursor, finish, plan.area, req))
        cursor = finish + timedelta(hours=1)
    return tuple(pieces)


def _client():
    config = Path.home() / ".cdsapirc"
    if not config.is_file():
        raise Era5Error(
            "Configure ~/.cdsapirc with your CDS personal access token ("
            "url: https://cds.climate.copernicus.eu/api and "
            "key: <personal-access-token>), then accept the ERA5 dataset terms."
        )
    try:
        import cdsapi
    except ImportError as exc:
        raise Era5Error(
            "ERA5 downloads require cdsapi; install the project ERA5/CDS API dependency"
        ) from exc
    return cdsapi.Client()


def validate_grib(path, piece, *, dataset_opener=None):
    """Check the GDAL GRIB inventory against requested variables and valid times."""
    if dataset_opener is None:
        try:
            from osgeo import gdal
        except ImportError as exc:
            raise Era5Error("ERA5 import requires GDAL with GRIB support") from exc
        dataset_opener = gdal.Open
    dataset = dataset_opener(str(path))
    if dataset is None:
        raise Era5Error("Downloaded file is not a readable GDAL GRIB dataset")
    required = {
        piece.start + timedelta(hours=n)
        for n in range(int((piece.end - piece.start).total_seconds() // 3600) + 1)
    }
    found = set()
    fields = set()
    try:
        for index in range(1, dataset.RasterCount + 1):
            meta = dataset.GetRasterBand(index).GetMetadata()
            element = meta.get("GRIB_ELEMENT", "").upper()
            level = meta.get("GRIB_SHORT_NAME", "")
            if element in ("10U", "UGRD") and level in ("0-SFC", "10-HTGL"):
                field = VARIABLES[0]
            elif element in ("10V", "VGRD") and level in ("0-SFC", "10-HTGL"):
                field = VARIABLES[1]
            elif (element in ("MSL", "PRMSL", "MSLMA") and level == "0-MSL") or (
                element == "MSL" and level == "0-SFC"
            ):
                field = VARIABLES[2]
            elif element in (
                "SWH",
                "HTSGW",
                "MWP",
                "PERPW",
                "MWD",
                "DIRPW",
            ) and level in ("0-SFC", "1-SFC"):
                name = {
                    "SWH": "wave_height",
                    "HTSGW": "wave_height",
                    "MWP": "wave_period",
                    "PERPW": "wave_period",
                    "MWD": "wave_direction",
                    "DIRPW": "wave_direction",
                }[element]
                field = FIELD_VARIABLES[name][0]
            else:
                raise Era5Error(
                    f"Unsupported GRIB element/level band: {element or '<unknown>'}/"
                    f"{level or '<unknown>'}; expected 10U/0-SFC or UGRD/10-HTGL, "
                    "10V/0-SFC or VGRD/10-HTGL, or MSL/0-MSL"
                )
            valid = meta.get("GRIB_VALID_TIME")
            if valid is None:
                continue
            try:
                timestamp = datetime.fromtimestamp(int(valid.split()[0]), timezone.utc)
            except (ValueError, OverflowError):
                continue
            if timestamp in required:
                fields.add(field)
                found.add((field, timestamp))
        requested = piece.request.get("variable") or list(VARIABLES)
        required_fields = [
            name
            for name, variables in FIELD_VARIABLES.items()
            if all(variable in requested for variable in variables)
        ]
        missing = [
            (field, moment)
            for field in required_fields
            for moment in required
            if any(
                (variable, moment) not in found for variable in FIELD_VARIABLES[field]
            )
        ]
        if missing:
            raise Era5Error(
                "GRIB is missing required ERA5 fields/UTC times (first missing: "
                f"{missing[0][0]} at {missing[0][1].isoformat()})"
            )
        return {
            "fields": sorted(fields),
            "validated_hours": len(required),
            "reader": "GDAL GRIB metadata",
        }
    finally:
        dataset = None


class Era5DownloadService:
    """Background, cancellable retrieval with atomic publication and history metadata."""

    def __init__(self, store, *, client_factory=_client, validator=validate_grib):
        self.store = store
        self.client_factory = client_factory
        self.validator = validator
        self._lock = threading.Lock()
        self._jobs = {}
        self.errors = {}
        self.progress = {}
        self._resume_threads = []
        self._resuming = set()
        self._completed_resumes = []

    _job_key = staticmethod(job_key)

    def submit(self, plan, *, retry=False):
        key = self._job_key(plan)
        with self._lock:
            prior = self._jobs.get(key)
            if prior and prior[0].is_alive():
                return prior[0]
            cancel = threading.Event()
            self.errors.pop(key, None)
            self.progress[key] = {
                "done": 0,
                "total": 0,
                "stage": "Queued for CDS…",
            }
            thread = threading.Thread(
                target=self._run_guarded, args=(key, plan, cancel, retry), daemon=True
            )
            self._jobs[key] = (thread, cancel)
            thread.start()
            return thread

    def cancel(self, plan):
        with self._lock:
            job = self._jobs.get(self._job_key(plan))
            if job:
                job[1].set()

    def progress_for(self, plan):
        key = self._job_key(plan)
        with self._lock:
            entry = self.progress.get(key)
            return dict(entry) if entry is not None else None

    def error_for(self, key):
        with self._lock:
            return self.errors.get(key)

    def clear_error(self, key):
        with self._lock:
            return self.errors.pop(key, None)

    def _trim_finished(self):
        # Retain the most recent 128 completed results, excluding running jobs.
        for registry in (self.progress, self.errors):
            finished = [key for key in registry if key not in self._jobs]
            for key in finished[:-128]:
                del registry[key]

    def _set_progress(self, key, *, done=None, total=None, stage=None):
        with self._lock:
            entry = self.progress.setdefault(key, {"done": 0, "total": 0, "stage": ""})
            if done is not None:
                entry["done"] = done
            if total is not None:
                entry["total"] = total
            if stage is not None:
                entry["stage"] = stage

    def _run_guarded(self, key, plan, cancel, retry):
        try:
            self._run(plan, cancel, retry)
        except Exception as exc:
            # CDS exceptions can contain request headers and credential material.
            raw_message = str(exc)
            if any(
                word in raw_message.lower()
                for word in ("401", "403", "unauthor", "credential", "token")
            ):
                message = (
                    "CDS API authentication failed. Check ~/.cdsapirc and accept "
                    "the ERA5 dataset terms at cds.climate.copernicus.eu."
                )
            elif isinstance(exc, Era5Error):
                message = raw_message
            else:
                # Third-party errors may embed CDS request headers or tokens.
                message = (
                    f"ERA5 download failed ({type(exc).__name__}). Check CDS API "
                    "credentials and dataset terms, then retry."
                )
            with self._lock:
                self.errors[key] = message[:1000]
            self._set_progress(key, stage="Failed")
        finally:
            with self._lock:
                job = self._jobs.get(key)
                if job and job[0] is threading.current_thread():
                    del self._jobs[key]
                self._trim_finished()

    def _run(self, plan, cancel, retry):
        key = self._job_key(plan)
        root = Path(self.store.root)
        catalog = root / "era5-history.json"
        pieces = list(split_request(plan))
        total = len(pieces)
        self._set_progress(
            key, done=0, total=total, stage=f"Planning {total} CDS chunk(s)…"
        )
        entries = self._catalog_entries(catalog)
        staged_chunks = []
        queued = []
        deferred = []
        done = 0
        with tempfile.TemporaryDirectory(prefix="era5-") as staging:
            # Submit every missing chunk first so they queue at CDS in parallel
            # instead of waiting for one chunk to finish before submitting the next.
            for n, piece in enumerate(pieces):
                if cancel.is_set():
                    raise Era5Error("ERA5 download canceled")
                if self._catalog_covers(entries, piece):
                    pending = self._pending_entry(self._load_pending(), piece)
                    if pending:
                        pending_piece = Era5Request(
                            datetime.fromisoformat(pending["start"]),
                            datetime.fromisoformat(pending["end"]),
                            tuple(pending["area"]),
                            {"variable": pending.get("variables")},
                        )
                        if self._catalog_covers(entries, pending_piece):
                            self._forget_pending(pending.get("request_id"))
                    done += 1
                    self._set_progress(
                        key,
                        done=done,
                        stage=f"Chunk {n + 1}/{total}: already downloaded",
                    )
                    continue
                if self._pending_entry(self._load_pending(), piece) is not None:
                    stage = f"Chunk {n + 1}/{total}: resuming queued CDS request…"
                else:
                    stage = f"Chunk {n + 1}/{total}: submitting to CDS…"
                self._set_progress(key, stage=stage)
                try:
                    self._ensure_chunk_request(piece)
                except Exception as exc:
                    if not _is_queue_limit_error(exc):
                        raise
                    # CDS refuses more queued requests than its dataset limit
                    # allows; retry once the other chunks have freed their slot.
                    deferred.append((n, piece))
                    continue
                queued.append((n, piece))
            for n, piece in queued:
                if cancel.is_set():
                    raise Era5Error("ERA5 download canceled")
                self._set_progress(
                    key, stage=f"Chunk {n + 1}/{total}: waiting for CDS results…"
                )
                staged_chunks.append(
                    self._stage_chunk(staging, n, piece, cancel, retry)
                )
                done += 1
                self._set_progress(
                    key,
                    done=done,
                    stage=f"Chunk {n + 1}/{total}: downloaded and validated",
                )
            for n, piece in deferred:
                if cancel.is_set():
                    raise Era5Error("ERA5 download canceled")
                self._set_progress(
                    key,
                    stage=f"Chunk {n + 1}/{total}: waiting for a CDS queue slot…",
                )
                self._submit_with_backoff(piece, cancel)
                staged_chunks.append(
                    self._stage_chunk(staging, n, piece, cancel, retry)
                )
                done += 1
                self._set_progress(
                    key,
                    done=done,
                    stage=f"Chunk {n + 1}/{total}: downloaded and validated",
                )
            if cancel.is_set():
                raise Era5Error("ERA5 download canceled")
            if not staged_chunks:
                self._set_progress(key, done=total, stage="Already available")
                return
            self._set_progress(key, stage="Publishing ERA5 history…")
            self._publish_chunks(root, catalog, staged_chunks, cancel)
            for _path, piece, _provenance in staged_chunks:
                self._forget_pending(getattr(piece, "_pending_request_id", None))
        self._set_progress(key, done=total, stage="Complete")

    def _catalog_entries(self, catalog):
        try:
            if not catalog.exists():
                return []
            entries = json.loads(catalog.read_text())
        except (OSError, ValueError):
            return []
        return entries if isinstance(entries, list) else []

    def _catalog_covers(self, entries, piece):
        """True when an existing artifact already covers this chunk."""
        root = Path(self.store.root)
        for entry in entries:
            try:
                start = datetime.fromisoformat(str(entry["start"]))
                end = datetime.fromisoformat(str(entry["end"]))
                area = tuple(float(value) for value in entry["area"])
                artifact = root / str(entry["artifact"])
            except (KeyError, TypeError, ValueError):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if start > piece.start or end < piece.end:
                continue
            if not _areas_close(area, piece.area):
                continue
            requested = set(piece.request.get("variable") or VARIABLES)
            covered = set(entry.get("variables") or VARIABLES)
            if not requested <= covered:
                continue
            if artifact.is_file():
                return True
        return False

    def _pending_path(self):
        return Path(self.store.root) / "era5-pending.json"

    def _load_pending(self):
        try:
            data = json.loads(self._pending_path().read_text())
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save_pending(self, entries):
        path = self._pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(prefix=".era5-pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf8") as stream:
                json.dump(entries, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, path)
        finally:
            if os.path.exists(staged):
                os.unlink(staged)

    def _pending_entry(self, entries, piece):
        for entry in entries:
            try:
                start = datetime.fromisoformat(entry["start"])
                end = datetime.fromisoformat(entry["end"])
                area = tuple(float(value) for value in entry["area"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                start == piece.start
                and end == piece.end
                and _areas_close(area, piece.area)
                # A queued request only contains its submitted variables.
                and entry.get("dataset", DATASET) == DATASET
                and set(piece.request.get("variable") or VARIABLES)
                <= set(entry.get("variables") or VARIABLES)
            ):
                return entry
        return None

    def _remember_pending(self, piece, request_id):
        path = self._pending_path()
        with _catalog_lock(path):
            entries = self._load_pending()
            if self._pending_entry(entries, piece) is not None:
                return
            entries.append(
                {
                    "dataset": DATASET,
                    "start": piece.start.isoformat(),
                    "end": piece.end.isoformat(),
                    "area": list(_canonical_area(piece.area)),
                    "variables": list(piece.request.get("variable") or VARIABLES),
                    "request_id": str(request_id),
                    "created": datetime.now(timezone.utc).isoformat(),
                }
            )
            self._save_pending(entries)

    def _forget_pending(self, request_id):
        if request_id is None:
            return
        path = self._pending_path()
        with _catalog_lock(path):
            entries = self._load_pending()
            kept = [
                entry
                for entry in entries
                if str(entry.get("request_id")) != str(request_id)
            ]
            if len(kept) != len(entries):
                self._save_pending(kept)

    def pending_requests(self):
        entries = self._load_pending()
        catalog = self._catalog_entries(Path(self.store.root) / "era5-history.json")
        kept = self._prune_pending(entries, catalog)
        if len(kept) != len(entries):
            path = self._pending_path()
            with _catalog_lock(path):
                entries = self._load_pending()
                kept = self._prune_pending(entries, catalog)
                if len(kept) != len(entries):
                    self._save_pending(kept)
        return kept

    def _prune_pending(self, entries, catalog):
        """Keep only outstanding, unique requests.

        Entries whose chunk is already in the catalog are stale bookkeeping
        from a run that failed before it could forget them, and entries that
        describe the same chunk (same times, same ERA5 grid cells) are
        duplicates from a restart. Keeping the newest duplicate lets one
        superseding request replace a stuck one.
        """
        newest = {}
        for entry in entries:
            try:
                piece = Era5Request(
                    datetime.fromisoformat(entry["start"]),
                    datetime.fromisoformat(entry["end"]),
                    tuple(float(value) for value in entry["area"]),
                    {"variable": entry.get("variables") or VARIABLES},
                )
            except (KeyError, TypeError, ValueError):
                continue
            if entry.get("dataset", DATASET) != DATASET:
                continue
            if self._catalog_covers(catalog, piece):
                continue
            key = job_key(piece)
            prior = newest.get(key)
            if prior is None or str(entry.get("created", "")) > str(
                prior.get("created", "")
            ):
                newest[key] = entry
        return list(newest.values())

    def pending_count(self):
        return len(self.pending_requests())

    def resume_active(self):
        with self._lock:
            return len(self._resume_threads)

    def completed_resume_keys(self):
        with self._lock:
            keys = self._completed_resumes
            self._completed_resumes = []
            return keys

    def resume_pending(self):
        """Resume persisted CDS requests that outlived a previous app run."""
        started = 0
        root = Path(self.store.root)
        catalog = self._catalog_entries(root / "era5-history.json")
        for entry in self.pending_requests():
            try:
                start = datetime.fromisoformat(entry["start"])
                end = datetime.fromisoformat(entry["end"])
                area = tuple(float(value) for value in entry["area"])
                key = job_key(
                    Era5Request(start, end, area, {"variable": entry.get("variables")})
                )
            except (KeyError, TypeError, ValueError):
                continue
            piece = Era5Request(
                start,
                end,
                area,
                _chunk_payload(start, end, area, variables=entry.get("variables")),
            )
            if self._catalog_covers(catalog, piece):
                self._forget_pending(entry.get("request_id"))
                continue
            with self._lock:
                if key in self._resuming:
                    continue
                self._resuming.add(key)
                self.errors.pop(key, None)
                thread = threading.Thread(
                    target=self._resume_entry, args=(entry, key), daemon=True
                )
                self._resume_threads.append(thread)
                thread.start()
            started += 1
        return started

    def _resume_entry(self, entry, key):
        try:
            piece = Era5Request(
                datetime.fromisoformat(entry["start"]),
                datetime.fromisoformat(entry["end"]),
                tuple(float(value) for value in entry["area"]),
                _chunk_payload(
                    datetime.fromisoformat(entry["start"]),
                    datetime.fromisoformat(entry["end"]),
                    tuple(float(value) for value in entry["area"]),
                    variables=entry.get("variables"),
                ),
            )
            inner = getattr(self.client_factory(), "client", None)
            if inner is None or not hasattr(inner, "get_remote"):
                return
            self._submit_with_backoff(piece, threading.Event())
            with tempfile.TemporaryDirectory(prefix="era5-") as staging:
                path = Path(staging) / "resume.grib"
                self._retrieve_chunk(piece, path, retry=True)
                if self.validator is None:
                    raise Era5Error(
                        "No GRIB validator configured; refusing to import "
                        "unverified ERA5 data"
                    )
                provenance = self.validator(path, piece)
                if not provenance:
                    raise Era5Error(
                        "Downloaded GRIB is missing required ERA5 fields, "
                        "levels, or UTC times"
                    )
                root = Path(self.store.root)
                self._publish_chunks(
                    root,
                    root / "era5-history.json",
                    [(path, piece, provenance)],
                    threading.Event(),
                )
            self._forget_pending(getattr(piece, "_pending_request_id", None))
        except Exception as exc:
            with self._lock:
                self.errors[key] = str(exc)[:1000]
                self._trim_finished()
        finally:
            with self._lock:
                self._resuming.discard(key)
                self._resume_threads.remove(threading.current_thread())
                self._completed_resumes.append(key)

    def _publish_chunks(self, root, catalog, staged_chunks, cancel):
        registered = []
        try:
            # Publish only after every chunk passed validation.
            records = []
            for path, piece, provenance in staged_chunks:
                # register_file is idempotent; only artifacts absent before this
                # invocation belong to this transaction and may be rolled back.
                digest = _sha256(path)
                destination = root / "artifacts" / digest
                existed = destination.exists()
                artifact = self.store.register_file(path)
                if not existed:
                    registered.append(artifact)
                record = {
                    "dataset": DATASET,
                    "source": "CDS ERA5 hourly single levels reanalysis",
                    "artifact": artifact.path,
                    "sha256": artifact.sha256,
                    "size": artifact.size,
                    "start": piece.start.isoformat(),
                    "end": piece.end.isoformat(),
                    "area": list(piece.area),
                    "variables": list(piece.request.get("variable") or VARIABLES),
                    "provenance": provenance,
                }
                records.append(record)
            if cancel.is_set():
                raise Era5Error("ERA5 import canceled before catalog publication")
            with _catalog_lock(catalog):
                entries = json.loads(catalog.read_text()) if catalog.exists() else []
                known = {e.get("sha256") for e in entries}
                entries.extend(
                    record for record in records if record["sha256"] not in known
                )
                fd2, staged = tempfile.mkstemp(prefix=".era5-catalog-", dir=root)
                try:
                    with os.fdopen(fd2, "w", encoding="utf8") as stream:
                        json.dump(entries, stream, sort_keys=True)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(staged, catalog)
                finally:
                    if os.path.exists(staged):
                        os.unlink(staged)
        except BaseException:
            # Remove newly published but unreferenced artifacts on any failed import.
            for artifact in registered:
                target = root / artifact.path
                target.unlink(missing_ok=True)
            raise

    def _ensure_chunk_request(self, piece):
        """Submit a chunk (or reuse its pending request) without waiting."""
        client = self.client_factory()
        inner = getattr(client, "client", None)
        if (
            inner is None
            or not hasattr(inner, "submit")
            or not hasattr(inner, "get_remote")
        ):
            return None
        request_id = self._live_request_id(inner, piece)
        if request_id is not None:
            return request_id
        remote = inner.submit(DATASET, piece.request)
        self._remember_pending(piece, remote.request_id)
        return str(remote.request_id)

    def _live_request_id(self, inner, piece):
        """Return the queued request for this chunk, discarding dead ones.

        A request CDS accepted and then never touched again is stuck in the
        dataset queue; deleting it frees the queue slot so the chunk can be
        resubmitted instead of polling it forever.
        """
        entry = self._pending_entry(self._load_pending(), piece)
        if entry is None or not entry.get("request_id"):
            return None
        request_id = str(entry["request_id"])
        try:
            remote = inner.get_remote(request_id)
            status = remote.status
        except Exception:
            self._forget_pending(request_id)
            return None
        if self._remote_is_stale(remote):
            self._delete_request(inner, request_id)
            self._forget_pending(request_id)
            return None
        if status in ("failed", "rejected", "dismissed", "deleted"):
            self._forget_pending(request_id)
            return None
        piece.request["variable"] = list(entry.get("variables") or VARIABLES)
        object.__setattr__(piece, "_pending_request_id", request_id)
        return request_id

    @staticmethod
    def _remote_is_stale(remote):
        if getattr(remote, "status", None) != "accepted":
            return False
        created = getattr(remote, "created_at", None)
        updated = getattr(remote, "updated_at", None)
        if not isinstance(created, datetime):
            return False
        if isinstance(updated, datetime) and updated != created:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - created > STALE_ACCEPTED

    @staticmethod
    def _delete_request(inner, request_id):
        delete = getattr(inner, "delete", None)
        if not callable(delete):
            return
        try:
            delete(request_id)
        except Exception:
            pass

    def _submit_with_backoff(self, piece, cancel):
        """Wait for a CDS queue slot instead of failing the whole download."""
        for attempt in range(QUEUE_RETRY_ATTEMPTS):
            if cancel.is_set():
                raise Era5Error("ERA5 download canceled")
            try:
                self._ensure_chunk_request(piece)
                return
            except Exception as exc:
                if (
                    not _is_queue_limit_error(exc)
                    or attempt + 1 == QUEUE_RETRY_ATTEMPTS
                ):
                    raise
            if cancel.wait(QUEUE_RETRY_SECONDS):
                raise Era5Error("ERA5 download canceled")

    def _retrieve_chunk(self, piece, path, retry):
        client = self.client_factory()
        inner = getattr(client, "client", None)
        if (
            inner is None
            or not hasattr(inner, "submit")
            or not hasattr(inner, "get_remote")
        ):
            # Legacy / test clients expose retrieve() directly.
            client.retrieve(DATASET, piece.request, str(path))
            return
        remote = None
        request_id = self._live_request_id(inner, piece)
        if request_id is not None:
            remote = inner.get_remote(request_id)
        if remote is None:
            remote = inner.submit(DATASET, piece.request)
            self._remember_pending(piece, remote.request_id)
        object.__setattr__(piece, "_pending_request_id", str(remote.request_id))
        status = remote.status
        if status in ("failed", "rejected", "dismissed", "deleted"):
            self._forget_pending(str(remote.request_id))
            raise Era5Error(f"ERA5 CDS request {remote.request_id} was {status}")
        remote.download(str(path))

    def _stage_chunk(self, staging, n, piece, cancel, retry):
        path = Path(staging) / f"chunk-{n}.grib"
        attempts = 2 if retry else 1
        for attempt in range(attempts):
            try:
                self._retrieve_chunk(piece, path, retry)
                break
            except Exception:
                if attempt + 1 == attempts:
                    raise
                if cancel.wait(min(2**attempt, 8)):
                    raise Era5Error("ERA5 download canceled")
        if cancel.is_set():
            raise Era5Error("ERA5 download canceled")
        if self.validator is None:
            raise Era5Error(
                "No GRIB validator configured; refusing to import unverified ERA5 data"
            )
        provenance = self.validator(path, piece)
        if not provenance:
            raise Era5Error(
                "Downloaded GRIB is missing required ERA5 fields, levels, or UTC times"
            )
        return path, piece, provenance


_catalog_locks: dict[str, threading.Lock] = {}
_catalog_locks_guard = threading.Lock()


class _catalog_lock:
    def __init__(self, catalog):
        self.path = Path(str(catalog) + ".lock")

    def __enter__(self):
        with _catalog_locks_guard:
            lock = _catalog_locks.setdefault(str(self.path.resolve()), threading.Lock())
        lock.acquire()
        self.lock = lock
        self.file = self.path.open("a+")
        fcntl.flock(self.file.fileno(), fcntl.LOCK_EX)

    def __exit__(self, *_):
        fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()
        self.lock.release()


def _sha256(path):
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish_history_display(state, entries, *, model_id="ERA5"):
    """Publish downloaded ERA5 chunks as one hourly display forecast."""
    usable = [
        entry
        for entry in entries
        if entry.get("artifact") and entry.get("start") and entry.get("end")
    ]
    if not usable:
        raise Era5Error("No downloaded ERA5 history to display")
    usable.sort(key=lambda entry: str(entry["start"]))
    root = Path(state.store.root)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="era5-display-", suffix=".grib", delete=False, dir=root
        ) as stream:
            temp_path = Path(stream.name)
            for entry in usable:
                stream.write((root / str(entry["artifact"])).read_bytes())
        artifact = state.store.register_file(temp_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    variables_by_time = {}
    for entry in usable:
        variables = set(entry.get("variables") or VARIABLES)
        start = datetime.fromisoformat(str(entry["start"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(entry["end"]).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        cursor = start.astimezone(timezone.utc)
        finish = end.astimezone(timezone.utc)
        while cursor <= finish:
            variables_by_time.setdefault(cursor, []).append((entry["area"], variables))
            cursor += timedelta(hours=1)
    field_time_sets = {
        field: {
            stamp
            for stamp, groups in variables_by_time.items()
            if any(
                set(names)
                <= set().union(
                    *(
                        variables
                        for other_area, variables in groups
                        if _areas_close(area, other_area)
                    )
                )
                for area, _ in groups
            )
        }
        for field, names in FIELD_VARIABLES.items()
    }
    fields = tuple(field for field, values in field_time_sets.items() if values)
    times = tuple(sorted(set().union(*(field_time_sets[field] for field in fields))))
    if len(times) < 2:
        raise Era5Error("ERA5 history needs at least two hours to display")

    north, west, south, east = (float(value) for value in usable[0]["area"])
    region = (south, west, north, east)
    run_time = times[0]
    hours = int((times[-1] - run_time).total_seconds() // 3600)
    if not fields:
        raise Era5Error("ERA5 history has no supported fields to display")
    levels = tuple(
        dict.fromkeys(
            level
            for field, level in (
                ("wind", "10-HTGL"),
                ("pressure", "0-MSL"),
                ("wave_height", "0-SFC"),
                ("wave_period", "0-SFC"),
                ("wave_direction", "0-SFC"),
            )
            if field in fields
        )
    )
    config = ModelConfiguration(
        model_id,
        fields,
        region,
        "0.25",
        hours,
        tuple(int((instant - run_time).total_seconds() // 3600) for instant in times),
        1,
    )
    now = datetime.now(timezone.utc)
    entry = ModelEntry(
        model_id,
        model_id,
        run_time,
        "deterministic",
        fields,
        levels,
        region,
        "0.25",
        times,
        (Artifact(artifact.path, artifact.sha256, artifact.size),),
        now,
        field_times=tuple(
            (field, tuple(sorted(field_time_sets[field]))) for field in fields
        ),
    )
    tranche = ForecastTranche(
        f"era5-{artifact.sha256[:16]}-v2", now, (config,), (entry,)
    )
    state.store.publish(tranche)
    state.store.activate(AnalysisSelection(tranche.tranche_id, entry.entry_id))
    return tranche
