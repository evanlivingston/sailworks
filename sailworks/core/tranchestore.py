"""Immutable tranche repository. Pointer rename is the activation commit point."""

import hashlib
import json
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import os
from dataclasses import dataclass
from pathlib import Path
import tempfile

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None

# Downloads are large and expensive, but the index is a hint: bound it so a
# long-lived installation cannot grow it without limit.
MAX_DOWNLOAD_RECORDS = 64

from .forecasttranche import (
    AnalysisSelection,
    Artifact,
    ForecastTranche,
    ModelEntry,
    _contains,
)


@dataclass(frozen=True)
class ResolvedForecast:
    selection: AnalysisSelection
    tranche: ForecastTranche
    entry: ModelEntry
    artifact_paths: tuple[Path, ...]


class TrancheStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.manifests = self.root / "manifests"
        self.artifacts = self.root / "artifacts"
        self.manifests.mkdir(parents=True, exist_ok=True)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.active_path = self.root / "active.json"
        self.downloads_path = self.root / "downloads.json"
        self.downloads_lock_path = self.root / "downloads.lock"

    @staticmethod
    def _fsync_dir(path):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _stage_json(self, payload):
        fd, name = tempfile.mkstemp(dir=self.manifests, prefix=".stage-")
        try:
            with os.fdopen(fd, "w", encoding="utf8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            return Path(name)
        except BaseException:
            if os.path.exists(name):
                os.unlink(name)
            raise

    def _atomic_active(self, payload, *, commit_guard=None):
        fd, name = tempfile.mkstemp(dir=self.root, prefix=".active-")
        committed = False
        try:
            with os.fdopen(fd, "w", encoding="utf8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            # Precommit preparation only: replace is the commit point; no
            # fallible operation follows it. This guarantees process-level
            # failure atomicity, not immunity to sudden power loss.
            self._fsync_dir(self.root)
            guard = commit_guard() if commit_guard is not None else nullcontext()
            with guard:
                os.replace(name, self.active_path)
                committed = True
        finally:
            if not committed and os.path.exists(name):
                os.unlink(name)

    @contextmanager
    def _downloads_lock(self):
        """Serialize read-modify-write of the download index across writers."""
        if fcntl is None:  # pragma: no cover - non-POSIX platforms
            yield
            return
        fd = os.open(self.downloads_lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def download_key(planned):
        """Stable identity for a planned download, independent of transport tags.

        The planned identifier already pins source, run date/cycle, fields and
        steps; model, region, member and native resolution complete the request.
        """
        from .forecastrefreshcontracts import PlannedModel

        if not isinstance(planned, PlannedModel):
            raise TypeError("expected PlannedModel")
        payload = {
            "model_id": planned.model_id,
            "identifier": json.loads(planned.identifier),
            "region": list(planned.configuration.region),
            "member": planned.member,
            "resolution": planned.native_resolution,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf8")).hexdigest()

    def _download_records(self):
        """Return the record mapping, ignoring a missing or malformed index."""
        path = self.downloads_path
        if path.is_symlink():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        records = payload.get("requests")
        return records if isinstance(records, dict) else {}

    def _write_downloads(self, records):
        """Atomically replace the index; call with the download lock held."""
        fd, name = tempfile.mkstemp(dir=self.root, prefix=".downloads-")
        try:
            with os.fdopen(fd, "w", encoding="utf8") as stream:
                json.dump(
                    {"version": 1, "requests": records},
                    stream,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o644)
            # Replace is the commit point so readers never observe a partial
            # index, even when two refreshes record downloads at once.
            os.replace(name, self.downloads_path)
            self._fsync_dir(self.root)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _bounded(records, limit):
        if len(records) <= limit:
            return records

        def created(item):
            record = item[1]
            return record.get("created", "") if isinstance(record, dict) else ""

        return dict(sorted(records.items(), key=created)[-limit:])

    def _coerce_artifact(self, artifact):
        if isinstance(artifact, Artifact):
            self._artifact_path(artifact)
            return artifact
        if isinstance(artifact, (str, os.PathLike)):
            path = Path(artifact)
            if not path.is_absolute():
                path = self.root / path
            if path.is_symlink() or not path.is_file():
                raise ValueError("adopted artifact is not a regular local file")
            if path.resolve().parent != self.artifacts:
                raise ValueError("adopted artifact must live in the artifact store")
            digest, size = self._hash(path)
            if path.name != digest:
                raise ValueError("artifact filename is not its content hash")
            return Artifact(f"artifacts/{digest}", digest, size)
        raise TypeError("expected Artifact or artifact path")

    def record_download(self, planned, artifact):
        """Register an existing artifact for a planned download.

        Callable by refresh itself and by an operator adopting bytes that are
        already in the artifact store, so a completed transfer is never repeated
        just because its manifest was not published.

        ``artifact`` may be an ``Artifact`` or a path inside ``artifacts/``.
        Returns the request key written.
        """
        key = self.download_key(planned)
        artifact = self._coerce_artifact(artifact)
        record = {
            "artifact": artifact.path,
            "sha256": artifact.sha256,
            "size": artifact.size,
            "model_id": planned.model_id,
            "run_time": planned.run_time.isoformat(),
            "region": list(planned.configuration.region),
            "fields": list(planned.configuration.fields),
            "steps": sorted(
                {hour for _field, hours in planned.field_steps for hour in hours}
            ),
            "member": planned.member,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        with self._downloads_lock():
            records = self._download_records()
            records[key] = record
            self._write_downloads(self._bounded(records, MAX_DOWNLOAD_RECORDS))
        return key

    def recorded_download(self, planned):
        """Return the verified artifact recorded for this request, or None."""
        key = self.download_key(planned)
        record = self._download_records().get(key)
        if not isinstance(record, dict):
            return None
        if (
            record.get("model_id") != planned.model_id
            or record.get("run_time") != planned.run_time.isoformat()
        ):
            return None
        sha256, size = record.get("sha256"), record.get("size")
        path = record.get("artifact")
        if not isinstance(sha256, str) or type(size) is not int:
            return None
        if not isinstance(path, str) or Path(path).name != sha256:
            return None
        try:
            artifact = Artifact(path, sha256, size)
            self._artifact_path(artifact)
        except (OSError, ValueError):
            return None
        return artifact

    def forget_download(self, planned):
        """Drop the recorded download for a request now covered by a manifest."""
        key = self.download_key(planned)
        with self._downloads_lock():
            records = self._download_records()
            if records.pop(key, None) is None:
                return False
            self._write_downloads(records)
        return True

    def register_file(self, source):
        """Register a local input by copying it into canonical immutable storage."""
        fd, stage_name = tempfile.mkstemp(dir=self.artifacts, prefix=".stage-")
        digest, size = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as output:
                with Path(source).open("rb") as source_file:
                    while chunk := source_file.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            hexdigest = digest.hexdigest()
            destination = self.artifacts / hexdigest
            os.chmod(stage_name, 0o444)
            try:
                os.link(stage_name, destination)
                self._fsync_dir(self.artifacts)
            except FileExistsError:
                if destination.is_symlink():
                    raise ValueError("artifact destination is a symlink")
                if self._hash(destination) != (hexdigest, size):
                    raise ValueError("content-address collision or corrupted artifact")
            return Artifact(f"artifacts/{hexdigest}", hexdigest, size)
        finally:
            if os.path.exists(stage_name):
                os.unlink(stage_name)

    @staticmethod
    def _hash(path):
        digest, size = hashlib.sha256(), 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def _artifact_path(self, artifact):
        if not isinstance(artifact, Artifact):
            raise ValueError("invalid artifact reference")
        path = self.root / artifact.path
        if path.is_symlink():
            raise ValueError("artifact path is a symlink")
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.resolve().parent != self.artifacts or self._hash(path) != (
            artifact.sha256,
            artifact.size,
        ):
            raise ValueError("artifact integrity check failed")
        return path

    def publish(self, tranche):
        if not isinstance(tranche, ForecastTranche):
            raise TypeError("expected ForecastTranche")
        for entry in tranche.entries:
            for artifact in entry.artifacts:
                self._artifact_path(artifact)
        destination = self.manifests / f"{tranche.tranche_id}.json"
        if destination.is_symlink():
            raise ValueError("manifest path is a symlink")
        payload = tranche.to_json()
        stage = self._stage_json(payload)
        try:
            os.chmod(stage, 0o444)
            try:
                os.link(stage, destination)
                self._fsync_dir(self.manifests)
            except FileExistsError:
                if json.loads(destination.read_text(encoding="utf8")) != payload:
                    raise FileExistsError(
                        "tranche ID already published with different content"
                    )
            return destination
        finally:
            if stage.exists():
                stage.unlink()

    def load(self, tranche_id):
        from .forecasttranche import _id

        _id(tranche_id, "tranche id")
        path = self.manifests / f"{tranche_id}.json"
        if path.is_symlink():
            raise ValueError("manifest path is a symlink")
        tranche = ForecastTranche.from_json(json.loads(path.read_text(encoding="utf8")))
        if tranche.tranche_id != tranche_id:
            raise ValueError("manifest ID does not match requested ID")
        return tranche

    def reusable_entry(self, planned):
        """Find the newest verified local artifact covering this exact run request."""
        configuration = planned.configuration
        required_fields = set(configuration.fields)
        requested_region = tuple(configuration.region)
        requested_times = {
            field: {planned.run_time + timedelta(hours=hour) for hour in hours}
            for field, hours in planned.field_steps
        }
        matches = []
        for path in self.manifests.glob("*.json"):
            try:
                tranche = self.load(path.stem)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            for entry in tranche.entries:
                if (
                    entry.model_id != planned.model_id
                    or entry.run_time != planned.run_time
                    or entry.member != planned.member
                    or entry.resolution != planned.native_resolution
                    or not _contains(entry.region, requested_region)
                    or not required_fields <= set(entry.fields)
                    or len(entry.artifacts) != 1
                ):
                    continue
                actual_times = dict(entry.field_times)
                if any(
                    not required_times <= set(actual_times.get(field, ()))
                    for field, required_times in requested_times.items()
                ):
                    continue
                try:
                    artifact_path = self._artifact_path(entry.artifacts[0])
                except (OSError, ValueError):
                    continue
                selected_field_times = tuple(
                    (
                        field,
                        tuple(
                            planned.run_time + timedelta(hours=hour) for hour in hours
                        ),
                    )
                    for field, hours in planned.field_steps
                )
                selected_times = tuple(
                    sorted(
                        {
                            time
                            for _field, times in selected_field_times
                            for time in times
                        }
                    )
                )
                selected_entry = ModelEntry(
                    entry.entry_id,
                    entry.model_id,
                    entry.run_time,
                    entry.member,
                    configuration.fields,
                    entry.levels,
                    entry.region,
                    entry.resolution,
                    selected_times,
                    entry.artifacts,
                    entry.acquired_at,
                    selected_field_times,
                )
                matches.append((entry.acquired_at, selected_entry, artifact_path))
        if not matches:
            return None
        _acquired_at, entry, path = max(matches, key=lambda item: item[0])
        return entry, path

    def resolve(self, selection):
        if not isinstance(selection, AnalysisSelection):
            raise TypeError("expected AnalysisSelection")
        tranche = self.load(selection.tranche_id)
        entry = next(
            (e for e in tranche.entries if e.entry_id == selection.entry_id), None
        )
        if entry is None:
            raise ValueError("selection entry not found")
        for model in tranche.entries:
            for artifact in model.artifacts:
                self._artifact_path(artifact)
        selected_paths = tuple(self._artifact_path(a) for a in entry.artifacts)
        return ResolvedForecast(selection, tranche, entry, selected_paths)

    def activate(self, selection, *, commit_guard=None):
        resolved = self.resolve(selection)
        if commit_guard is None:
            self._atomic_active(selection.to_json())
        else:
            self._atomic_active(selection.to_json(), commit_guard=commit_guard)
        return resolved

    def active_selection(self):
        if self.active_path.is_symlink():
            raise ValueError("active pointer is a symlink")
        if not self.active_path.exists():
            return None
        selection = AnalysisSelection.from_json(
            json.loads(self.active_path.read_text(encoding="utf8"))
        )
        tranche = self.load(selection.tranche_id)
        if not any(entry.entry_id == selection.entry_id for entry in tranche.entries):
            raise ValueError("active selection entry not found")
        return selection

    def active(self):
        selection = self.active_selection()
        return None if selection is None else self.resolve(selection)

    def clear_active(self, *, commit_guard=None):
        selection = self.active_selection()
        if selection is None:
            return None
        self._fsync_dir(self.root)
        guard = commit_guard() if commit_guard is not None else nullcontext()
        with guard:
            os.unlink(self.active_path)
        return None
