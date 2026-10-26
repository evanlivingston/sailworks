from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from gweatherrouting.core.forecaststate import NoActiveForecastError
from gweatherrouting.core.forecasttranche import (
    AnalysisSelection,
    Artifact,
    ForecastTranche,
    ModelConfiguration,
    ModelEntry,
)
from gweatherrouting.core.tranchestore import ResolvedForecast, TrancheStore


@dataclass(frozen=True)
class FakeSnapshot:
    selection: object
    tranche: object
    entry: object
    artifact_paths: tuple


class FakeAnalysisState:
    def __init__(self, root, active=None):
        self.store = SimpleNamespace(root=Path(root))
        self.configuration = None
        self.active = active
        self.next_refresh = active
        self.invalidations = 0
        self.action_generation = 0

    def snapshot(self, required_fields=()):
        if self.active is None:
            raise NoActiveForecastError("No active forecast selection")
        if not set(required_fields) <= set(self.active.entry.fields):
            raise ValueError("missing required fields")
        return self.active

    def configure(self, configurations, refresh_model_id):
        self.action_generation += 1
        self.configuration = SimpleNamespace(
            configurations=configurations, refresh_model_id=refresh_model_id
        )

    def begin_action(self):
        self.action_generation += 1
        return self.action_generation

    def _check_action(self, action_token, cancel):
        if cancel is not None and cancel.is_set():
            raise RuntimeError("cancelled")
        if action_token != self.action_generation:
            raise RuntimeError("stale action")

    def refresh(self, cancel=None, progress=None, action_token=None):
        self._check_action(action_token, cancel)
        if self.next_refresh is None:
            raise ValueError("refresh failed")
        self.active = self.next_refresh
        return self.active

    def select_model(self, model_id, *, action_token=None, cancel=None):
        self._check_action(action_token, cancel)
        entry = next(e for e in self.active.tranche.entries if e.model_id == model_id)
        artifact = entry.artifacts[0]
        self.active = FakeSnapshot(
            SimpleNamespace(
                tranche_id=self.active.tranche.tranche_id, entry_id=entry.entry_id
            ),
            self.active.tranche,
            entry,
            (self.store.root / artifact.path,),
        )
        return self.active

    def clear_selection(self, *, action_token=None, cancel=None):
        self._check_action(action_token, cancel)
        self.active = None
        return None

    def invalidate(self):
        self.invalidations += 1
        self.action_generation += 1


def make_snapshot(root, tranche_id="tranche-a", selected="GFS", *, fields=("wind",)):
    run = datetime(2025, 1, 1, tzinfo=timezone.utc)
    entries = []
    for model_id in ("GFS", "HRRR"):
        entry_id = model_id.lower()
        artifact = SimpleNamespace(
            path=f"artifacts/{model_id.lower()}", sha256=model_id.lower() * 32, size=1
        )
        entries.append(
            SimpleNamespace(
                entry_id=entry_id,
                model_id=model_id,
                run_time=run,
                member="deterministic",
                fields=tuple(fields),
                field_times=tuple((f, (run,)) for f in fields),
                time_steps=(run,),
                artifacts=(artifact,),
            )
        )
    tranche = SimpleNamespace(tranche_id=tranche_id, entries=tuple(entries))
    entry = next(e for e in entries if e.model_id == selected)
    selection = SimpleNamespace(tranche_id=tranche_id, entry_id=entry.entry_id)
    return FakeSnapshot(
        selection,
        tranche,
        entry,
        (Path(root) / entry.artifacts[0].path,),
    )


def make_published_snapshot(
    root, tranche_id="published", selected="GFS", *, fields=("wind",)
):
    root = Path(root)
    store = TrancheStore(root)
    run = datetime(2025, 1, 1, tzinfo=timezone.utc)
    region = (0, -1, 1, 1)
    entries, configurations = [], []
    for model_id, resolution in (("GFS", "0.25deg"), ("HRRR", "3km")):
        configuration = ModelConfiguration(
            model_id, fields, region, "native", 1, (0,), 0
        )
        configurations.append(configuration)
        artifact_source = root / f"{model_id.lower()}-source.grib"
        artifact_source.write_bytes(f"fixture-{model_id}".encode())
        artifact = store.register_file(artifact_source)
        levels = tuple(
            sorted("10-HTGL" if field == "wind" else "0-MSL" for field in fields)
        )
        field_times = tuple((field, (run,)) for field in fields)
        entries.append(
            ModelEntry(
                model_id.lower(),
                model_id,
                run,
                "deterministic",
                fields,
                levels,
                region,
                resolution,
                (run,),
                (artifact,),
                run + timedelta(hours=1),
                field_times,
            )
        )
    tranche = ForecastTranche(
        tranche_id, run + timedelta(hours=2), tuple(configurations), tuple(entries)
    )
    entry = next(item for item in entries if item.model_id == selected)
    selection = AnalysisSelection(tranche_id, entry.entry_id)
    paths = (store.root / entry.artifacts[0].path,)
    return ResolvedForecast(selection, tranche, entry, paths)
