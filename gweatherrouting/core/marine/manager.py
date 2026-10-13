"""Persistent dataset selection with immutable snapshots and explicit fallback."""

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from gweatherrouting.core.utils import EventDispatcher

from .models import (
    Capability,
    Dataset,
    QueryResult,
    Station,
    canonical_json,
    sample_station,
    utc_time,
)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, delete=False
        ) as handle:
            temporary = handle.name
            handle.write(canonical_json(value) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class Selection:
    datasets: Tuple[Dataset, ...]
    capability: Capability
    allow_fallback: bool = False

    def sample(self, station_id, instant):
        candidates = [d for d in self.datasets if d.station.id == station_id]
        if not candidates:
            return QueryResult(
                "no_selected_source", detail="No selected dataset for this station"
            )
        failures: List[str] = []
        for dataset in candidates:
            result = sample_station(dataset, self.capability, instant)
            if result.status == "available":
                return QueryResult(
                    result.status,
                    result.dataset_id,
                    result.values,
                    result.qc_flags,
                    "; ".join(failures),
                )
            failures.append(dataset.id + ": " + result.status)
            if not self.allow_fallback:
                return result
        return QueryResult("unavailable", detail="; ".join(failures))

    def to_json(self):
        return dict(
            capability=self.capability.value,
            allow_fallback=self.allow_fallback,
            datasets=[d.manifest() for d in self.datasets],
        )


class MarineManager(EventDispatcher):
    def __init__(self, directory, credential_store=None):
        super().__init__()
        self.directory = Path(directory)
        self._lock = threading.RLock()
        self.datasets = {}
        self.errors = []
        self._order = []
        self._enabled = set()
        self._fallback = False
        self.stations = ()
        self._station_download_lock = threading.Lock()
        self._pending_station_downloads: dict = {}
        self._load()
        from .coverage import CoveragePlanner
        from .fields import FieldStore
        from .spatialproviders import CopernicusProvider

        self.coverage_planner = CoveragePlanner(
            worldwide=CopernicusProvider(credential_store),
            cache_directory=self.directory / "source-cache",
        )
        self.fields = FieldStore(self.directory / "fields")

    def _load(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        for path in sorted((self.directory / "datasets").glob("*.json")):
            try:
                dataset = Dataset.from_json(json.loads(path.read_text()))
                if path.stem != dataset.id:
                    raise ValueError("Dataset checksum mismatch")
                self.datasets[dataset.id] = dataset
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.errors.append(path.name + ": " + str(error))
        self._order = list(self.datasets)
        try:
            path = self.directory / "selection.json"
            state = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(state, dict) or state.get("schema_version", 1) != 1:
                raise ValueError("Unsupported source-selection file")
            self._order = list(
                dict.fromkeys(i for i in state.get("order", []) if i in self.datasets)
            )
            self._order.extend(i for i in self.datasets if i not in self._order)
            self._enabled = set(state.get("enabled", [])) & set(self.datasets)
            self._fallback = bool(state.get("allow_fallback", False))
            path = self.directory / "stations.json"
            if path.exists():
                self.stations = tuple(
                    Station.from_json(s) for s in json.loads(path.read_text())
                )
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.errors.append("Saved marine selection: " + str(error))

    def _save(self):
        atomic_json(
            self.directory / "selection.json",
            dict(
                schema_version=1,
                order=self._order,
                enabled=sorted(self._enabled),
                allow_fallback=self._fallback,
            ),
        )
        self.dispatch("changed", None)

    def store_stations(self, stations):
        with self._lock:
            stations = tuple(stations)
            atomic_json(
                self.directory / "stations.json", [s.to_json() for s in stations]
            )
            self.stations = stations

    def add(self, dataset):
        with self._lock:
            identifier = dataset.id
            atomic_json(
                self.directory / "datasets" / (identifier + ".json"), dataset.to_json()
            )
            self.datasets[identifier] = dataset
            pending = self._pending_station_downloads.get(dataset.station.id)
            if pending is not None and pending.id == identifier:
                self._pending_station_downloads.pop(dataset.station.id)
            if identifier not in self._order:
                self._order.append(identifier)
            # Downloads are deliberately not enabled without explicit user selection.
            self._save()
            return identifier

    def ordered_datasets(self):
        with self._lock:
            return tuple(self.datasets[i] for i in self._order)

    def cached_station(self, station, start, end, capability=Capability.WATER_LEVEL):
        """Reuse predictions only when actual, usable samples cover the window."""
        start, end = utc_time(start), utc_time(end)
        with self._lock:
            candidates = (
                *self.datasets.values(),
                *self._pending_station_downloads.values(),
            )
        for dataset in reversed(candidates):
            if dataset.station.id != station.id or not (
                dataset.requested_start <= start < end <= dataset.requested_end
            ):
                continue
            codes = (
                ("water_level",)
                if capability == Capability.WATER_LEVEL
                else ("current_speed", "current_direction")
            )
            times = {start, end}
            for code in codes:
                series = dataset.get_series(code)
                if series is not None:
                    times.update(
                        s.time for s in series.samples if start <= s.time <= end
                    )
            if all(
                sample_station(dataset, capability, t).status == "available"
                for t in times
            ):
                # Checking the midpoints also detects long gaps between otherwise
                # valid exact samples. Requested bounds alone do not prove coverage.
                ordered = sorted(times)
                if all(
                    sample_station(dataset, capability, a + (b - a) / 2).status
                    == "available"
                    for a, b in zip(ordered, ordered[1:])
                ):
                    return dataset
        return None

    def download_station(
        self, provider, station, start, end, cancel, capability=Capability.WATER_LEVEL
    ):
        """Coalesce station requests across panels, including results awaiting UI save."""
        from .chs import DownloadCancelledError

        while not self._station_download_lock.acquire(timeout=0.1):
            if cancel.is_set():
                raise DownloadCancelledError()
        try:
            if cancel.is_set():
                raise DownloadCancelledError()
            cached = self.cached_station(station, start, end, capability)
            if cached is not None:
                return cached
            if capability == Capability.WATER_LEVEL:
                from .stationcache import download_missing

                with self._lock:
                    existing = (
                        *self.datasets.values(),
                        *self._pending_station_downloads.values(),
                    )
                dataset = download_missing(
                    provider, station, utc_time(start), utc_time(end), cancel, existing
                )
            else:
                dataset = provider.download(station, start, end, cancel)
            if cancel.is_set():
                raise DownloadCancelledError()
            with self._lock:
                # One pending record per station; persisted datasets are checked
                # independently, so this small handoff cache cannot grow by date.
                self._pending_station_downloads[station.id] = dataset
            return dataset
        finally:
            self._station_download_lock.release()

    def is_enabled(self, identifier):
        with self._lock:
            return identifier in self._enabled

    @property
    def allow_fallback(self):
        return self._fallback

    def set_fallback(self, enabled):
        with self._lock:
            self._fallback = bool(enabled)
            self._save()

    def enable(self, identifier, enabled=True):
        with self._lock:
            if identifier not in self.datasets:
                raise ValueError("Unknown marine dataset")
            if enabled:
                self._enabled.add(identifier)
            else:
                self._enabled.discard(identifier)
            self._save()

    def move(self, identifier, offset):
        with self._lock:
            index = self._order.index(identifier)
            target = max(0, min(len(self._order) - 1, index + offset))
            self._order.insert(target, self._order.pop(index))
            self._save()

    def remove(self, identifier):
        with self._lock:
            if identifier not in self.datasets:
                raise ValueError("Unknown marine dataset")
            (self.directory / "datasets" / (identifier + ".json")).unlink()
            del self.datasets[identifier]
            self._enabled.discard(identifier)
            self._order.remove(identifier)
            self._save()

    def snapshot(self, capability, identifiers=None, allow_fallback=None):
        with self._lock:
            capability = Capability(capability)
            order = (
                list(identifiers)
                if identifiers is not None
                else [i for i in self._order if i in self._enabled]
            )
            if len(set(order)) != len(order) or any(
                i not in self.datasets for i in order
            ):
                raise ValueError("Invalid source override")
            return Selection(
                tuple(
                    self.datasets[i]
                    for i in order
                    if capability in self.datasets[i].capabilities
                ),
                capability,
                self._fallback if allow_fallback is None else bool(allow_fallback),
            )
