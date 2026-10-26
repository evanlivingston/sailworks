"""Managed, byte-for-byte log originals with independently editable metadata."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .storage import DATA_DIR


LOG_SUFFIXES = {".log", ".nmea", ".txt", ".ebl"}
_EBL_SEGMENT = re.compile(r"^(\d+)_(\d+)\.ebl$", re.IGNORECASE)


def _ebl_series(paths):
    """Return groups for reliable vendor segment names, preserving other inputs."""
    paths = tuple(dict.fromkeys(Path(path).resolve() for path in paths))
    groups = {}
    for path in paths:
        source = Path(path)
        match = _EBL_SEGMENT.fullmatch(source.name)
        if match:
            groups.setdefault((source.parent.resolve(), match.group(1)), []).append(
                (int(match.group(2)), source)
            )
    by_path = {}
    for members in groups.values():
        if len(members) > 1:
            group = tuple(path for _, path in sorted(members))
            by_path.update((path, group) for path in group)
    result, handled = [], set()
    for path in paths:
        if path not in handled:
            group = by_path.get(path, (path,))
            result.append(group)
            handled.update(group)
    return result


class LogLibrary:
    def __init__(self, directory=None):
        self.directory = Path(directory or Path(DATA_DIR) / "log-library")
        self.originals = self.directory / "originals"
        self.index = self.directory / "index.json"
        self.lock = RLock()
        self._entries = {}
        if self.index.exists():
            data = json.loads(self.index.read_text(encoding="utf-8"))
            if data.get("version") != 1:
                raise ValueError("Unsupported log library index version")
            self._entries = {item["id"]: item for item in data["logs"]}

    def entries(self):
        with self.lock:
            return tuple(dict(item) for item in self._entries.values())

    def path(self, identifier):
        with self.lock:
            name = self._entries[identifier]["file"]
            if Path(name).name != name or name in (".", ".."):
                raise ValueError("Invalid managed log filename")
            return self.originals / name

    def _save(self, entries):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / f".{uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(
                    {"version": 1, "logs": list(entries.values())}, stream, indent=2
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.index)
        finally:
            temporary.unlink(missing_ok=True)
        self._entries = entries

    def import_path(self, path, progress=None):
        """Import one log, decoding EBL into a reusable .log when possible."""
        source = Path(path).resolve()
        if source.suffix.lower() not in LOG_SUFFIXES or not source.is_file():
            raise ValueError("Choose a local .log, .nmea, .txt or .ebl file")
        source_signature = self._source_signature((source,))
        with self.lock:
            for item in self._entries.values():
                if self.path(item["id"]).resolve() == source:
                    if progress is not None:
                        total = source.stat().st_size
                        progress(0, total)
                        progress(total, total)
                    return dict(item)
                if item.get("source_signature") == source_signature:
                    if progress is not None:
                        total = source.stat().st_size
                        progress(0, total)
                        progress(total, total)
                    return dict(item)
        if source.suffix.lower() == ".ebl":
            return self._import_ebl((source,), progress=progress)
        # Do not hold the metadata lock during a potentially large copy: the
        # GUI can still inspect the library while imports run in a worker.
        identifier = uuid4().hex
        item = dict(
            id=identifier,
            file=identifier + source.suffix.lower(),
            name=source.name,
            vessel="",
            notes="",
            imported=datetime.now(timezone.utc).isoformat(),
            status="Imported; not yet interpreted",
            diagnostics=[],
            source_signature=source_signature,
        )
        self.originals.mkdir(parents=True, exist_ok=True)
        target = self.originals / item["file"]
        try:
            total = source.stat().st_size
            copied = 0
            if progress is not None:
                progress(0, total)
            with source.open("rb") as incoming, target.open("xb") as outgoing:
                while chunk := incoming.read(64 * 1024):
                    outgoing.write(chunk)
                    copied += len(chunk)
                    if progress is not None:
                        progress(copied, total)
            if progress is not None and not copied:
                progress(0, total)
            with self.lock:
                self._save({**self._entries, identifier: item})
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return dict(item)

    def _import_original(self, source, progress=None):
        """Copy a single original file unchanged."""
        source = Path(source).resolve()
        source_signature = self._source_signature((source,))
        identifier = uuid4().hex
        item = dict(
            id=identifier,
            file=identifier + source.suffix.lower(),
            name=source.name,
            vessel="",
            notes="",
            imported=datetime.now(timezone.utc).isoformat(),
            status="Imported; not yet interpreted",
            diagnostics=[],
            source_signature=source_signature,
        )
        self.originals.mkdir(parents=True, exist_ok=True)
        target = self.originals / item["file"]
        try:
            total = source.stat().st_size
            copied = 0
            if progress is not None:
                progress(0, total)
            with source.open("rb") as incoming, target.open("xb") as outgoing:
                while chunk := incoming.read(64 * 1024):
                    outgoing.write(chunk)
                    copied += len(chunk)
                    if progress is not None:
                        progress(copied, total)
            if progress is not None and not copied:
                progress(0, total)
            with self.lock:
                self._save({**self._entries, identifier: item})
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return dict(item)

    def _import_ebl(self, group, progress=None, decode_progress=None):
        """Decode an EBL (single or stitched) and store the decoded .log."""
        from .ebllog import read_ebl_log, write_decoded_log
        from dataclasses import asdict, replace

        group = tuple(group)
        if not group:
            raise ValueError("EBL import requires at least one file")
        if any(path.suffix.lower() != ".ebl" or not path.is_file() for path in group):
            raise ValueError("EBL segments must be local files")
        # First check if any path in the group is already a managed file.
        with self.lock:
            for source in group:
                source_resolved = Path(source).resolve()
                for item in self._entries.values():
                    if self.path(item["id"]).resolve() == source_resolved:
                        if progress is not None:
                            total = sum(p.stat().st_size for p in group)
                            progress(0, total)
                            progress(total, total)
                        return dict(item)
        source_signature = self._source_signature(group)
        with self.lock:
            existing = next(
                (
                    item
                    for item in self._entries.values()
                    if item.get("source_signature") == source_signature
                ),
                None,
            )
        if existing is not None:
            if progress is not None:
                total = sum(path.stat().st_size for path in group)
                progress(0, total)
                progress(total, total)
            return dict(existing)
        identifier = uuid4().hex
        self.originals.mkdir(parents=True, exist_ok=True)
        callback_errors = []

        def report_decode(done, total):
            if decode_progress is None:
                return
            try:
                decode_progress(done, total)
            except Exception as exc:
                callback_errors.append(exc)
                raise

        try:
            log = read_ebl_log(group, progress=report_decode)
        except Exception:
            if callback_errors:
                raise callback_errors[0]
            log = None

        if log is None or not log.entries:
            # Preserve unsupported/malformed EBL for later inspection.
            if len(group) == 1:
                return self._import_original(group[0], progress=progress)
            target = self.originals / f"{identifier}.ebl"
            temporary = self.originals / f".{identifier}.tmp"
            total = sum(source.stat().st_size for source in group)
            copied = 0
            try:
                if progress is not None:
                    progress(0, total)
                with temporary.open("xb") as outgoing:
                    for source in group:
                        with source.open("rb") as incoming:
                            while chunk := incoming.read(64 * 1024):
                                outgoing.write(chunk)
                                copied += len(chunk)
                                if progress is not None:
                                    progress(copied, total)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                os.replace(temporary, target)
            except Exception:
                temporary.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                raise
            item = dict(
                id=identifier,
                file=target.name,
                name=f"{group[0].parent.name}/{group[0].stem.split('_')[0]}.ebl",
                vessel="",
                notes="",
                imported=datetime.now(timezone.utc).isoformat(),
                status="Imported; not yet interpreted",
                diagnostics=[],
                source_signature=source_signature,
            )
            try:
                with self.lock:
                    self._save({**self._entries, identifier: item})
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return dict(item)

        target = self.originals / f"{identifier}.log"
        temporary = self.originals / f".{identifier}.log.tmp"
        try:
            write_decoded_log(log, temporary)
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        try:
            item = dict(
                id=identifier,
                file=target.name,
                name=(
                    f"{group[0].parent.name}/{group[0].stem.split('_')[0]}.log"
                    if len(group) > 1
                    else group[0].stem + ".log"
                ),
                vessel="",
                notes="",
                imported=datetime.now(timezone.utc).isoformat(),
                status=(
                    (
                        f"{log.format}: {log.frame_count:,} raw CAN frames; "
                        f"{len(log.entries):,} replay entries"
                    )
                    if log.entries and log.frame_count is not None
                    else (
                        f"{log.format}: {len(log.entries):,} sentences"
                        if log.entries
                        else "Archive only — no supported replay data"
                    )
                ),
                diagnostics=list(log.diagnostics),
                source_signature=source_signature,
                file_info={
                    "path": str(target),
                    "format": "NMEA 0183 (decoded from EBL)",
                    "entries": len(log.entries),
                    "duration": log.duration,
                    "timestamped": log.timestamped,
                    "skipped": log.skipped,
                    "frame_count": log.frame_count,
                    "source_tracks": list(log.source_tracks),
                    "device_metadata_available": log.device_metadata_available,
                    "devices": [asdict(d) for d in log.devices],
                    "diagnostics": list(log.diagnostics),
                },
            )
            with self.lock:
                self._save({**self._entries, identifier: item})
            result = dict(item)
            result["_decoded_log"] = replace(log, path=str(target))
            return result
        except Exception:
            target.unlink(missing_ok=True)
            raise

    @staticmethod
    def group_paths(paths):
        """Group selected split EBL siblings in first-occurrence import order."""
        return _ebl_series(paths)

    def import_paths(self, paths, progress=None, decode_progress=None):
        """Import paths, concatenating selected sibling vendor segments losslessly."""
        groups = self.group_paths(paths)
        imported = []
        for group in groups:
            if len(group) == 1 and group[0].suffix.lower() != ".ebl":
                imported.append(self.import_path(group[0], progress=progress))
                continue
            if all(path.suffix.lower() == ".ebl" for path in group):
                imported.append(
                    self._import_ebl(
                        group, progress=progress, decode_progress=decode_progress
                    )
                )
                continue
            for path in group:
                imported.append(self.import_path(path, progress=progress))
        return imported

    @staticmethod
    def _source_signature(paths):
        """Identify unchanged external inputs without hashing large files."""
        signature = []
        for source in paths:
            source = Path(source).resolve()
            stat = source.stat()
            signature.append(
                {
                    "path": str(source),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        return signature

    def update(self, identifier, **values):
        if values.keys() - {
            "vessel",
            "notes",
            "status",
            "diagnostics",
            "file_info",
        }:
            raise ValueError("Only log metadata may be edited")
        with self.lock:
            item = {**self._entries[identifier], **values}
            self._save({**self._entries, identifier: item})

    def save_log(self, log):
        """Atomically save an edited log to its managed text-log copy."""
        from .ebllog import write_edited_log

        source = Path(log.path).resolve()
        with self.lock:
            identifier = next(
                (key for key in self._entries if self.path(key).resolve() == source),
                None,
            )
            if identifier is None:
                raise ValueError("The active log is not a managed library file")
            item = dict(self._entries[identifier])
            target = self.path(identifier)
            if target.suffix.lower() not in {".log", ".nmea", ".txt"}:
                raise ValueError("Only managed text logs can be saved after editing")
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            file_info = dict(item.get("file_info", {}))
            try:
                write_edited_log(log, temporary)
                os.replace(temporary, target)
                file_info.update(
                    {
                        "path": str(target),
                        "format": log.format,
                        "entries": len(log.entries),
                        "duration": log.duration,
                        "timestamped": log.timestamped,
                        "skipped": log.skipped,
                        "source_tracks": list(log.source_tracks),
                        "diagnostics": list(log.diagnostics),
                    }
                )
                item["file_info"] = file_info
                item["status"] = f"{log.format}: {len(log.entries):,} replay entries"
                item["diagnostics"] = list(log.diagnostics)
                self._save({**self._entries, identifier: item})
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        return dict(item)

    def remove(self, identifier):
        """Remove only our managed copy, never the user's source file."""
        with self.lock:
            path = self.path(identifier)
            entries = dict(self._entries)
            del entries[identifier]
            # A failed index write must not destroy the managed original.
            self._save(entries)
            path.unlink(missing_ok=True)
