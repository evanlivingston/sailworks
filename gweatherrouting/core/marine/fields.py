"""Immutable geographic current fields, mask-aware sampling and offline storage."""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path
from zipfile import BadZipFile

import numpy as np
from pyproj import Geod

from gweatherrouting.core.utils import EventDispatcher

from .manager import atomic_json
from .models import QueryResult, canonical_json, utc_time


@dataclass(frozen=True)
class Area:
    west: float
    south: float
    east: float
    north: float

    def __post_init__(self):
        if not (
            -180 <= self.west <= 180
            and -180 <= self.east <= 180
            and -90 <= self.south < self.north <= 90
        ):
            raise ValueError("Invalid geographic area")

    def parts(self):
        if self.west <= self.east:
            return (self,)
        return (
            Area(self.west, self.south, 180, self.north),
            Area(-180, self.south, self.east, self.north),
        )

    def intersects(self, other):
        return any(
            a.west <= b.east
            and b.west <= a.east
            and a.south <= b.north
            and b.south <= a.north
            for a in self.parts()
            for b in other.parts()
        )

    def to_json(self):
        return dict(self.__dict__)

    @classmethod
    def from_points(cls, points, padding=0.1):
        points = tuple(points)
        if not points:
            raise ValueError("Choose a route with at least one point")
        if any(not (-90 <= p[0] <= 90 and -180 <= p[1] <= 180) for p in points):
            raise ValueError("Invalid route coordinates")
        lons = sorted({(p[1] + 360) % 360 for p in points})
        gaps = [
            (lons[(i + 1) % len(lons)] + (360 if i == len(lons) - 1 else 0) - lon, i)
            for i, lon in enumerate(lons)
        ]
        _, index = max(gaps)
        west = lons[(index + 1) % len(lons)] - padding
        span = 360 - gaps[index][0] + 2 * padding
        if span >= 360:
            west, east = -180, 180
        else:
            east = (west + span + 180) % 360 - 180
            west = (west + 180) % 360 - 180
        return cls(
            west,
            max(-90, min(p[0] for p in points) - padding),
            east,
            min(90, max(p[0] for p in points) + padding),
        )


def frozen_array(value):
    array = np.asarray(value, dtype="<f8")
    # A bytes backing store prevents callers from switching writeability back on.
    return np.frombuffer(array.tobytes(), dtype="<f8").reshape(array.shape)


@dataclass(frozen=True, eq=False)
class CurrentField:
    metadata_json: str
    times: np.ndarray
    latitudes: np.ndarray
    longitudes: np.ndarray
    east: np.ndarray
    north: np.ndarray

    def __post_init__(self):
        metadata = json.loads(self.metadata_json)
        if (
            metadata.get("schema_version") != 1
            or metadata.get("vector_reference") != "true_east_north"
        ):
            raise ValueError("Unsupported current field reference or schema")
        for key in (
            "provider",
            "model",
            "composition",
            "sources",
            "resolution_m",
            "run",
        ):
            if key not in metadata:
                raise ValueError("Missing current metadata: " + key)
        for name in ("times", "latitudes", "longitudes", "east", "north"):
            object.__setattr__(self, name, frozen_array(getattr(self, name)))
        for name in ("times", "latitudes", "longitudes"):
            array = getattr(self, name)
            if (
                array.ndim != 1
                or not len(array)
                or not np.isfinite(array).all()
                or np.any(np.diff(array) <= 0)
            ):
                raise ValueError("Current coordinates must be finite and increasing")
        if (
            self.east.shape
            != (len(self.times), len(self.latitudes), len(self.longitudes))
            or self.north.shape != self.east.shape
        ):
            raise ValueError("Current grid dimensions do not match")
        if (
            self.latitudes[0] < -90
            or self.latitudes[-1] > 90
            or self.longitudes[0] < -180
            or self.longitudes[-1] > 180
        ):
            raise ValueError("Invalid geographic grid")

    @cached_property
    def id(self):
        digest = hashlib.sha256(self.metadata_json.encode())
        for array in (
            self.times,
            self.latitudes,
            self.longitudes,
            self.east,
            self.north,
        ):
            digest.update(str(array.shape).encode())
            digest.update(array.tobytes())
        return digest.hexdigest()

    def manifest(self):
        return dict(
            json.loads(self.metadata_json),
            dataset_id=self.id,
            valid_start=datetime.fromtimestamp(self.times[0], timezone.utc).isoformat(),
            valid_end=datetime.fromtimestamp(self.times[-1], timezone.utc).isoformat(),
            bounds=[
                float(self.longitudes[0]),
                float(self.latitudes[0]),
                float(self.longitudes[-1]),
                float(self.latitudes[-1]),
            ],
        )

    @cached_property
    def resolution_m(self):
        return json.loads(self.metadata_json)["resolution_m"]

    @cached_property
    def content_key(self):
        """Identify the same forecast without changing saved provenance IDs."""
        metadata = json.loads(self.metadata_json)
        metadata.pop("retrieved_at", None)
        for source in metadata["sources"]:
            source.pop("retrieved_at", None)
        digest = hashlib.sha256(canonical_json(metadata).encode())
        for array in (
            self.times,
            self.latitudes,
            self.longitudes,
            self.east,
            self.north,
        ):
            digest.update(str(array.shape).encode())
            digest.update(array.tobytes())
        return digest.hexdigest()

    def sample(self, latitude, longitude, instant):
        instant = utc_time(instant).timestamp()
        longitude = (longitude + 180) % 360 - 180
        if not self.times[0] <= instant <= self.times[-1]:
            return QueryResult("outside_time", self.id)
        if not (
            self.latitudes[0] <= latitude <= self.latitudes[-1]
            and self.longitudes[0] <= longitude <= self.longitudes[-1]
        ):
            return QueryResult("outside_grid", self.id)
        axes = (self.times, self.latitudes, self.longitudes)
        locations = (instant, latitude, longitude)
        brackets = []
        for axis, location in zip(axes, locations):
            right = int(np.searchsorted(axis, location))
            left = right if axis[right] == location else right - 1
            weight = (
                0
                if left == right
                else (location - axis[left]) / (axis[right] - axis[left])
            )
            brackets.append(
                ((left, 1 - weight),)
                if left == right
                else ((left, 1 - weight), (right, weight))
            )
        left, right = brackets[0][0][0], brackets[0][-1][0]
        if self.times[right] - self.times[left] > 3600:
            return QueryResult("time_gap", self.id)
        east, north = 0.0, 0.0
        for t, wt in brackets[0]:
            for y, wy in brackets[1]:
                for x, wx in brackets[2]:
                    ce, cn = float(self.east[t, y, x]), float(self.north[t, y, x])
                    if not (math.isfinite(ce) and math.isfinite(cn)):
                        return QueryResult(
                            "masked_or_missing",
                            self.id,
                            detail="Interpolation touches masked water/land; no extrapolation",
                        )
                    east += ce * wt * wy * wx
                    north += cn * wt * wy * wx
        return QueryResult("available", self.id, (east, north))

    def sample_nearest(self, latitude, longitude, instant, valid):
        """Use a finite corner of this cell, within one nominal grid spacing.

        A routing shoreline validator is required; never search across cells or
        bridge missing forecast times. Use the same corner at both time slices.
        """
        timestamp = utc_time(instant).timestamp()
        longitude = (longitude + 180) % 360 - 180
        axes = (self.times, self.latitudes, self.longitudes)
        locations = (timestamp, latitude, longitude)
        if any(
            not axis[0] <= value <= axis[-1] for axis, value in zip(axes, locations)
        ):
            return None
        indices = []
        for axis, value in zip(axes, locations):
            right = int(np.searchsorted(axis, value))
            left = right if axis[right] == value else right - 1
            indices.append((left, right))
        t0, t1 = indices[0]
        if self.times[t1] - self.times[t0] > 3600:
            return None
        candidates = []
        origin = (latitude, longitude)
        geod = Geod(ellps="WGS84")
        for y in sorted(set(indices[1])):
            for x in sorted(set(indices[2])):
                values = np.array(
                    [
                        [self.east[t0, y, x], self.north[t0, y, x]],
                        [self.east[t1, y, x], self.north[t1, y, x]],
                    ]
                )
                if not np.isfinite(values).all():
                    continue
                target = (float(self.latitudes[y]), float(self.longitudes[x]))
                distance = geod.inv(longitude, latitude, target[1], target[0])[2]
                if distance <= self.resolution_m:
                    candidates.append((distance, target, values))
        for distance, target, values in sorted(candidates, key=lambda c: c[0]):
            if not valid(origin, target):
                continue
            weight = (
                0
                if t0 == t1
                else (timestamp - self.times[t0]) / (self.times[t1] - self.times[t0])
            )
            vector = values[0] + weight * (values[1] - values[0])
            return QueryResult(
                "available",
                self.id,
                tuple(vector),
                qc_flags=("nearest_wet_cell",),
                detail=canonical_json(
                    dict(
                        latitude=target[0],
                        longitude=target[1],
                        distance_m=distance,
                    )
                ),
            )
        return None

    def sample_many(self, positions, instants):
        """Vectorized trilinear interpolation with the scalar sampler's masks."""
        if not len(positions):
            return []
        positions = np.asarray(positions, dtype=float)
        times = np.array([utc_time(t).timestamp() for t in instants])
        return self._sample_many_arrays(positions, times)

    def _sample_many_arrays(self, positions, times):
        statuses, values = self.sample_arrays(positions, times)
        return [
            field_query(status, self.id, values[i]) for i, status in enumerate(statuses)
        ]

    def sample_arrays(self, positions, times):
        """Interpolate epoch seconds without creating a QueryResult per sample."""
        lats = positions[:, 0]
        lons = (positions[:, 1] + 180) % 360 - 180
        statuses = np.full(len(positions), "available", dtype=object)
        statuses[(times < self.times[0]) | (times > self.times[-1])] = "outside_time"
        outside = ~(
            (lats >= self.latitudes[0])
            & (lats <= self.latitudes[-1])
            & (lons >= self.longitudes[0])
            & (lons <= self.longitudes[-1])
        )
        statuses[outside & (statuses == "available")] = "outside_grid"
        active = np.flatnonzero(statuses == "available")
        values = np.zeros((len(positions), 2))
        if len(active):
            brackets = []
            for axis, locations in zip(
                (self.times, self.latitudes, self.longitudes), (times, lats, lons)
            ):
                locations = locations[active]
                right = np.searchsorted(axis, locations)
                left = np.where(axis[right] == locations, right, right - 1)
                width = axis[right] - axis[left]
                weight = np.divide(
                    locations - axis[left],
                    width,
                    out=np.zeros(len(active)),
                    where=width != 0,
                )
                brackets.append(((left, 1 - weight), (right, weight)))
            left, right = brackets[0][0][0], brackets[0][1][0]
            gaps = self.times[right] - self.times[left] > 3600
            masked = np.zeros(len(active), dtype=bool)
            for t, wt in brackets[0]:
                for y, wy in brackets[1]:
                    for x, wx in brackets[2]:
                        ce, cn = self.east[t, y, x], self.north[t, y, x]
                        masked |= ~(np.isfinite(ce) & np.isfinite(cn))
                        # Match scalar multiplication order and corner order.
                        values[active, 0] += ce * wt * wy * wx
                        values[active, 1] += cn * wt * wy * wx
            statuses[active[masked]] = "masked_or_missing"
            statuses[active[gaps]] = "time_gap"
        return statuses, values


def field_query(status, identifier, values):
    return QueryResult(
        status,
        identifier,
        tuple(values) if status == "available" else (),
        detail=(
            "Interpolation touches masked water/land; no extrapolation"
            if status == "masked_or_missing"
            else ""
        ),
    )


@dataclass
class CurrentSamples:
    """Numeric source selection, with exceptional wet-corner records kept intact."""

    values: np.ndarray
    dataset: np.ndarray
    status: np.ndarray
    fields: tuple
    special: dict

    def identifier(self, index):
        if index in self.special:
            return self.special[index].dataset_id
        field = self.dataset[index]
        return self.fields[field].id if field >= 0 else None

    def query(self, index):
        if index in self.special:
            return self.special[index]
        if self.status[index] == "no_coverage":
            return QueryResult(
                "no_coverage",
                detail="No selected spatial field at this position and time",
            )
        return field_query(
            self.status[index], self.identifier(index), self.values[index]
        )


class FieldSelection:
    def __init__(self, fields, neighbor_valid=None, neighbor_valid_many=None):
        self.fields = tuple(fields)
        self.neighbor_valid = neighbor_valid
        self.neighbor_valid_many = neighbor_valid_many
        self.used = set()
        self.missing = {}

    def _nearest(self, latitude, longitude, instant):
        if self.neighbor_valid is None:
            return None
        for field in self.fields:
            result = field.sample_nearest(
                latitude, longitude, instant, self.neighbor_valid
            )
            if result is not None:
                self.used.add(field.id)
                return result
        return None

    def sample(self, latitude, longitude, instant):
        missing = None
        for field in self.fields:
            result = field.sample(latitude, longitude, instant)
            if result.status in ("outside_time", "outside_grid"):
                continue
            if result.status == "available":
                self.used.add(field.id)
                return result
            if missing is None:
                missing = result
        return self._resolve_missing(missing, latitude, longitude, instant)

    def _resolve_missing(self, result, latitude, longitude, instant):
        if result is not None and result.status == "masked_or_missing":
            nearest = self._nearest(latitude, longitude, instant)
            if nearest is not None:
                return nearest
        if result is None or result.status == "no_coverage":
            result = QueryResult(
                "no_coverage",
                detail="No selected spatial field at this position and time",
            )
        self.missing[result.status] = self.missing.get(result.status, 0) + 1
        return result

    def sample_many(self, positions, instants):
        if not len(positions):
            return []
        positions = np.asarray(positions, dtype=float)
        times = np.array([utc_time(t).timestamp() for t in instants])
        samples = self.sample_arrays(positions, times)
        return [samples.query(i) for i in range(len(positions))]

    def sample_arrays(self, positions, times, check_cancel=None):
        """Apply the same precedence, masks and bounded recovery to numeric queries."""
        count = len(positions)
        values = np.zeros((count, 2))
        dataset = np.full(count, -1, dtype=np.int32)
        status = np.full(count, "no_coverage", dtype=object)
        special: dict = {}
        samples = CurrentSamples(values, dataset, status, self.fields, special)
        if not count:
            return samples
        lats = positions[:, 0]
        lons = (positions[:, 1] + 180) % 360 - 180
        pending = np.arange(len(positions))
        for field_index, field in enumerate(self.fields):
            if check_cancel is not None:
                check_cancel()
            if not len(pending):
                break
            covered = (
                (times[pending] >= field.times[0])
                & (times[pending] <= field.times[-1])
                & (lats[pending] >= field.latitudes[0])
                & (lats[pending] <= field.latitudes[-1])
                & (lons[pending] >= field.longitudes[0])
                & (lons[pending] <= field.longitudes[-1])
            )
            active = pending[covered]
            if not len(active):
                continue
            local_status, local_values = field.sample_arrays(
                positions[active], times[active]
            )
            available = local_status == "available"
            resolved = np.zeros(len(pending), dtype=bool)
            resolved[covered] = available
            chosen = active[available]
            values[chosen] = local_values[available]
            dataset[chosen] = field_index
            status[chosen] = "available"
            if len(chosen):
                self.used.add(field.id)
            first = (status[active] == "no_coverage") & ~available
            status[active[first]] = local_status[first]
            dataset[active[first]] = field_index
            pending = pending[~resolved]
        return self._finish_samples(samples, positions, times, pending, check_cancel)

    def _finish_samples(self, samples, positions, times, pending, check_cancel):
        values, status, special = samples.values, samples.status, samples.special
        if self.neighbor_valid_many is not None:
            from .nearestbatch import resolve_nearest

            masked = pending[status[pending] == "masked_or_missing"]
            resolved = resolve_nearest(self, positions, times, masked, check_cancel)
            for index, answer in resolved.items():
                status[index] = "available"
                values[index] = answer.values
                special[int(index)] = answer
            pending = pending[status[pending] != "available"]
        for index in pending:
            if check_cancel is not None:
                check_cancel()
            answer = samples.query(index)
            if self.neighbor_valid_many is None:
                answer = self._resolve_missing(
                    answer,
                    positions[index, 0],
                    positions[index, 1],
                    datetime.fromtimestamp(float(times[index]), timezone.utc),
                )
            else:
                self.missing[answer.status] = self.missing.get(answer.status, 0) + 1
            status[index] = answer.status
            if answer.status == "available":
                values[index] = answer.values
                special[int(index)] = answer
        return samples

    def to_json(self):
        return dict(
            schema_version=1,
            selection_policy=(
                "finest resolution, newest run; "
                "fallback to valid selected fields; no blending"
                + (
                    "; bounded nearest wet corner with shoreline checks"
                    if self.neighbor_valid is not None
                    else ""
                )
            ),
            selected_sources=[f.manifest() for f in self.fields],
            actual_sources=sorted(self.used),
            missing=dict(self.missing),
        )


class FieldStore(EventDispatcher):
    def __init__(self, directory):
        super().__init__()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.fields = {}
        self.excluded = set()
        self.errors = []
        for path in self.directory.glob("*/manifest.json"):
            try:
                value = json.loads(path.read_text())
                with np.load(path.parent / "arrays.npz", allow_pickle=False) as arrays:
                    field = CurrentField(value["metadata_json"], **dict(arrays))
                if path.parent.name != field.id:
                    raise ValueError("Spatial dataset checksum mismatch")
                self.fields[field.id] = field
            except (OSError, ValueError, KeyError, TypeError, BadZipFile) as error:
                self.errors.append(str(error))
        path = self.directory / "selection.json"
        try:
            if path.exists():
                self.excluded = set(json.loads(path.read_text())["excluded"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.errors.append(str(error))
        self._content: dict[str, str] = {}
        for field in self.fields.values():
            self._content.setdefault(field.content_key, field.id)

    def equivalent(self, field):
        identifier = self._content.get(field.content_key)
        return self.fields[identifier] if identifier is not None else field

    def add(self, field):
        existing = self.equivalent(field)
        if existing.id in self.fields:
            return existing.id
        directory = self.directory / field.id
        directory.mkdir(exist_ok=True)
        temporary = directory / "arrays.tmp.npz"
        np.savez_compressed(
            temporary,
            times=field.times,
            latitudes=field.latitudes,
            longitudes=field.longitudes,
            east=field.east,
            north=field.north,
        )
        temporary.replace(directory / "arrays.npz")
        atomic_json(
            directory / "manifest.json", dict(metadata_json=field.metadata_json)
        )
        self.fields[field.id] = field
        self._content[field.content_key] = field.id
        self.dispatch("changed", None)
        return field.id

    def enable(self, identifier, enabled):
        if identifier not in self.fields:
            raise ValueError("Unknown spatial dataset")
        self.excluded.discard(identifier) if enabled else self.excluded.add(identifier)
        atomic_json(
            self.directory / "selection.json", dict(excluded=sorted(self.excluded))
        )
        self.dispatch("changed", None)

    def remove(self, identifier):
        if identifier not in self.fields:
            raise ValueError("Unknown spatial dataset")
        directory = self.directory / identifier
        (directory / "manifest.json").unlink()
        (directory / "arrays.npz").unlink()
        directory.rmdir()
        del self.fields[identifier]
        self._content = {}
        for field in self.fields.values():
            self._content.setdefault(field.content_key, field.id)
        self.excluded.discard(identifier)
        atomic_json(
            self.directory / "selection.json", dict(excluded=sorted(self.excluded))
        )
        self.dispatch("changed", None)

    def snapshot(self):
        return FieldSelection(
            sorted(
                (f for f in self.fields.values() if f.id not in self.excluded),
                key=lambda f: (
                    f.manifest()["resolution_m"],
                    -utc_time(
                        f.manifest()["run"] or f.manifest()["retrieved_at"]
                    ).timestamp(),
                ),
            )
        )
