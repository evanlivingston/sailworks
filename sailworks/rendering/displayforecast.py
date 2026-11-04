"""Immutable display identities and private-reader filtering (never routing policy)."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json


def identity_key(path, reference, model):
    return json.dumps((str(path), reference, model), separators=(",", ":"))


def display_selection(source):
    """Run selection for a displayed source, or None for display-only models.

    Models outside the forecast capability catalogue (for example imported
    ERA5 history) have no model-run identity, so their GRIB messages must not
    be filtered by one.
    """
    from sailworks.core.forecastcapabilities import MODEL_CAPABILITIES

    if getattr(source, "model_name", "") not in MODEL_CAPABILITIES:
        return None
    return getattr(source, "display_identity", None)


def restrict_index(index, selection):
    """Filter the private index, preserving original band numbers/vector flags."""
    if selection is not None:
        index.records = [
            record
            for record in index.records
            if (record.reference, record.identity[1]) == selection
        ]
        index.references = {record.reference for record in index.records}
    return index


@dataclass(frozen=True, eq=False)
class DisplayForecast:
    path: str
    name: str
    centre: str
    bounds: object
    start_time: datetime
    last_forecast: float
    model_name: str
    display_identity: tuple
    first_valid: float
    last_valid: float
    version: tuple
    valid_times: tuple = ()

    @property
    def key(self):
        return identity_key(self.path, *self.display_identity)

    @property
    def label(self):
        reference, model = self.display_identity
        run = datetime.fromtimestamp(reference, timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        member = "/".join(model[2]) if model[2] else "deterministic"
        return f"{self.name} · run {run} · member {member}"


def inspect_forecasts(entries):
    """Worker-only metadata scan; no decoded arrays or shared GDAL handles."""
    from sailworks.core.grib import Grib

    forecasts, errors = [], []
    for path, version in entries:
        source = None
        try:
            metadata = Grib.parse_metadata(path)
            source = Grib(
                metadata.path,
                metadata.name,
                metadata.centre,
                metadata.bounds,
                metadata.start_time,
                metadata.last_forecast,
                metadata.model_name,
            )
            if metadata.model_name == "ERA5":
                times = tuple(
                    sorted({record.valid for record in source.sampling.records})
                )
                if times:
                    reference = min(times)
                    forecasts.append(
                        DisplayForecast(
                            str(path),
                            metadata.name,
                            metadata.centre,
                            metadata.bounds,
                            datetime.fromtimestamp(reference),
                            (max(times) - reference) / 3600,
                            "ERA5",
                            (reference, ("ecmf", "ERA5", ())),
                            reference,
                            max(times),
                            version,
                            valid_times=times,
                        )
                    )
                else:
                    errors.append(f"{metadata.name}: no supported point forecasts")
                continue
            groups = {}
            for record in source.sampling.records:
                groups.setdefault((record.reference, record.identity[1]), []).append(
                    record.valid
                )
            for identity, times in groups.items():
                forecasts.append(
                    DisplayForecast(
                        str(path),
                        metadata.name,
                        metadata.centre,
                        metadata.bounds,
                        datetime.fromtimestamp(identity[0]),
                        (max(times) - identity[0]) / 3600,
                        metadata.model_name,
                        identity,
                        min(times),
                        max(times),
                        version,
                        valid_times=tuple(sorted(set(times))),
                    )
                )
            if not groups:
                errors.append(f"{metadata.name}: no supported point forecasts")
        except Exception as exc:
            errors.append(f"{path}: {exc}")
        finally:
            if source is not None:
                source.close()
    return tuple(forecasts), "; ".join(errors)
