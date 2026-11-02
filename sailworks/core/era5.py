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

DATASET = "reanalysis-era5-single-levels"
VARIABLES = (
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "mean_sea_level_pressure",
)
MAX_DAYS = 31
MAX_AREA_CELLS = 2_000_000
MAX_REQUEST_HOURS = 31 * 24
ERA5_EARLIEST = datetime(1940, 1, 1, tzinfo=timezone.utc)
LATENCY = timedelta(days=5)


class Era5Error(RuntimeError):
    """An actionable ERA5 acquisition or validation error."""


@dataclass(frozen=True)
class Era5Request:
    start: datetime
    end: datetime
    area: tuple[float, float, float, float]
    request: dict


def plan_request(start, end, area, *, now=None, max_days=MAX_DAYS):
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
    req = {
        "product_type": ["reanalysis"],
        "variable": list(VARIABLES),
        "year": sorted({f"{d.year:04d}" for d in _dates(start.date(), end.date())}),
        "month": sorted({f"{d.month:02d}" for d in _dates(start.date(), end.date())}),
        "day": sorted({f"{d.day:02d}" for d in _dates(start.date(), end.date())}),
        "time": [f"{h:02d}:00" for h in range(24)],
        "area": [north, west, south, east],
        "data_format": "grib",
        "download_format": "unarchived",
    }
    return Era5Request(start, end, (north, west, south, east), req)


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
        missing = [
            (field, moment)
            for field in VARIABLES
            for moment in required
            if (field, moment) not in found
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

    def submit(self, plan, *, retry=False):
        key = (plan.start, plan.end, plan.area)
        with self._lock:
            prior = self._jobs.get(key)
            if prior and prior[0].is_alive():
                return prior[0]
            cancel = threading.Event()
            thread = threading.Thread(
                target=self._run_guarded, args=(key, plan, cancel, retry), daemon=True
            )
            self._jobs[key] = (thread, cancel)
            thread.start()
            return thread

    def cancel(self, plan):
        with self._lock:
            job = self._jobs.get((plan.start, plan.end, plan.area))
            if job:
                job[1].set()

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

    def _run(self, plan, cancel, retry):
        root = Path(self.store.root)
        catalog = root / "era5-history.json"
        staged_chunks = []
        with tempfile.TemporaryDirectory(prefix="era5-") as staging:
            for n, piece in enumerate(split_request(plan)):
                if cancel.is_set():
                    raise Era5Error("ERA5 download canceled")
                staged_chunks.append(
                    self._stage_chunk(staging, n, piece, cancel, retry)
                )
            if cancel.is_set():
                raise Era5Error("ERA5 download canceled")
            self._publish_chunks(root, catalog, staged_chunks, cancel)

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
                    "variables": list(VARIABLES),
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

    def _stage_chunk(self, staging, n, piece, cancel, retry):
        path = Path(staging) / f"chunk-{n}.grib"
        attempts = 2 if retry else 1
        for attempt in range(attempts):
            try:
                self.client_factory().retrieve(DATASET, piece.request, str(path))
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
