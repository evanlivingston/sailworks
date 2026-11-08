"""Managed, byte-for-byte log originals with independently editable metadata."""

import json
import multiprocessing
import os
import re
import shutil
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
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


class LogImportCancelled(Exception):
    """Raised when a caller's stop predicate aborts an import."""


def _decode_group_to_temp(
    originals, group, identifier, progress_queue=None, include_log=False
):
    """Decode one EBL group into temporary decoded logs.

    This runs in a spawned worker process during parallel imports, so it must
    not touch the library index or hold any lock.
    """
    from .ebllog import iter_ebl_sessions, write_decoded_log

    group = tuple(Path(path) for path in group)
    originals = Path(originals)
    originals.mkdir(parents=True, exist_ok=True)
    staged = []
    first_log = None

    def report(done, total):
        if progress_queue is not None:
            progress_queue.put((identifier, done, total))

    for session_log in iter_ebl_sessions(group, progress=report):
        if not session_log.entries:
            continue
        temporary = originals / f".{identifier}_{len(staged)}.log.tmp"
        write_decoded_log(session_log, temporary)
        metadata = LogLibrary._ebl_metadata(session_log)
        metadata["file_info"]["ebl_session_key"] = LogLibrary._ebl_session_key(
            group, session_log
        )
        metadata["file_info"]["ebl_source"] = str(group[0].parent.resolve())
        staged.append((temporary, metadata))
        if first_log is None:
            first_log = session_log
    return staged, (first_log if include_log else None)


class LogLibrary:
    def __init__(self, directory=None):
        self.directory = Path(directory or Path(DATA_DIR) / "log-library")
        self.originals = self.directory / "originals"
        self.index = self.directory / "index.json"
        self.lock = RLock()
        self.import_lock = RLock()
        self.keep_ebl_originals = True
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
        with self.import_lock:
            return self._import_path(path, progress)

    def _import_path(self, path, progress=None):
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

    def _existing_ebl_item(self, group):
        with self.lock:
            for source in group:
                resolved = Path(source).resolve()
                for item in self._entries.values():
                    if self.path(item["id"]).resolve() == resolved:
                        return dict(item)
            signature = self._source_signature(group)
            return next(
                (
                    dict(item)
                    for item in self._entries.values()
                    if item.get("source_signature") == signature
                ),
                None,
            )

    def _retain_group_originals(self, group, identifier):
        """Hard-link (or copy) each source segment into the library."""
        if not self.keep_ebl_originals:
            return []
        self.originals.mkdir(parents=True, exist_ok=True)
        existing = {}
        for item in self._entries.values():
            for record in (item.get("file_info") or {}).get("ebl_originals", ()):
                existing.setdefault(record.get("source"), record.get("file"))
        retained = []
        for index, source in enumerate(group):
            source = Path(source).resolve()
            name = existing.get(str(source))
            if not name:
                name = f"{identifier}_{index}_{source.name}"
            target = self.originals / name
            if not target.exists():
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copy2(source, target)
            retained.append(
                {
                    "file": target.name,
                    "source": str(source),
                    "size": source.stat().st_size,
                }
            )
        return retained

    def decode_group_to_temp(
        self, group, identifier, decode_progress=None, stop=None, include_log=False
    ):
        """Decode one EBL group into temporary logs, without installing them."""

        def report(done, total):
            if stop is not None and stop():
                raise LogImportCancelled()
            if decode_progress is not None:
                decode_progress(done, total)

        from .ebllog import iter_ebl_sessions, write_decoded_log

        group = tuple(Path(path) for path in group)
        self.originals.mkdir(parents=True, exist_ok=True)
        staged = []
        first_log = None
        for session_log in iter_ebl_sessions(group, progress=report):
            if not session_log.entries:
                continue
            temporary = self.originals / f".{identifier}_{len(staged)}.log.tmp"
            write_decoded_log(session_log, temporary)
            metadata = self._ebl_metadata(session_log)
            metadata["file_info"]["ebl_session_key"] = self._ebl_session_key(
                group, session_log
            )
            metadata["file_info"]["ebl_source"] = str(group[0].parent.resolve())
            staged.append((temporary, metadata))
            if first_log is None:
                first_log = session_log
        return staged, first_log

    def install_group(
        self,
        group,
        identifier,
        source_signature,
        staged,
        first_log=None,
        progress=None,
    ):
        """Install decoded session logs sequentially under the library lock."""
        if not staged:
            if len(group) == 1:
                return self._import_original(group[0], progress=progress)
            return self._import_ebl_archive(
                group, identifier, source_signature, progress
            )
        retained = self._retain_group_originals(group, identifier)
        if retained:
            for _, metadata in staged:
                metadata["file_info"]["ebl_originals"] = retained
        multiple = len(staged) > 1
        with self.lock:
            known = {
                item.get("file_info", {}).get("ebl_session_key"): item
                for item in self._entries.values()
                if item.get("file_info", {}).get("ebl_session_key")
            }

        pending = []
        backups = []
        installed = []
        first_item = None
        first_path = None
        try:
            for index, (temporary, metadata) in enumerate(staged):
                key = metadata["file_info"]["ebl_session_key"]
                existing = known.get(key) if key else None
                if existing is not None:
                    same = (
                        existing.get("source_signature") == source_signature
                        and existing.get("file_info", {}).get("entries")
                        == metadata["file_info"]["entries"]
                        and existing.get("file_info", {}).get("duration")
                        == metadata["file_info"]["duration"]
                    )
                    if same:
                        temporary.unlink(missing_ok=True)
                        if first_item is None:
                            first_item = dict(existing)
                            first_path = self.path(existing["id"])
                        continue
                    target = self.path(existing["id"])
                    backup = target.with_name(f".{target.name}.{uuid4().hex}.bak")
                    os.replace(target, backup)
                    backups.append((target, backup))
                    os.replace(temporary, target)
                    installed.append(target)
                    item = dict(existing)
                    item.update(
                        status=metadata["status"],
                        diagnostics=metadata["diagnostics"],
                        source_signature=source_signature,
                        file_info={**metadata["file_info"], "path": str(target)},
                    )
                else:
                    session_identifier = (
                        identifier if not multiple else f"{identifier}_{index}"
                    )
                    target = self.originals / f"{session_identifier}.log"
                    os.replace(temporary, target)
                    installed.append(target)
                    item = dict(
                        id=session_identifier,
                        file=target.name,
                        name=self._ebl_session_name(group, index, multiple),
                        vessel="",
                        notes="",
                        imported=datetime.now(timezone.utc).isoformat(),
                        status=metadata["status"],
                        diagnostics=metadata["diagnostics"],
                        source_signature=source_signature,
                        file_info={**metadata["file_info"], "path": str(target)},
                    )
                pending.append(item)
                if first_item is None:
                    first_item = item
                    first_path = target
            if pending:
                with self.lock:
                    entries = dict(self._entries)
                    entries.update({item["id"]: item for item in pending})
                    self._save(entries)
        except Exception:
            for target in installed:
                target.unlink(missing_ok=True)
            for target, backup in backups:
                if backup.exists():
                    os.replace(backup, target)
            for temporary, _ in staged:
                temporary.unlink(missing_ok=True)
            raise
        else:
            for _, backup in backups:
                backup.unlink(missing_ok=True)

        result = dict(first_item)
        if first_log is not None and first_path is not None:
            result["_decoded_log"] = replace(first_log, path=str(first_path))
        return result

    def _import_ebl(self, group, progress=None, decode_progress=None, stop=None):
        """Decode an EBL (single or stitched) and store one log per session."""
        group = tuple(Path(path) for path in group)
        if not group:
            raise ValueError("EBL import requires at least one file")
        if any(path.suffix.lower() != ".ebl" or not path.is_file() for path in group):
            raise ValueError("EBL segments must be local files")
        existing = self._existing_ebl_item(group)
        if existing is not None:
            if progress is not None:
                total = sum(path.stat().st_size for path in group)
                progress(0, total)
                progress(total, total)
            return existing
        identifier = uuid4().hex
        source_signature = self._source_signature(group)
        try:
            staged, first_log = self.decode_group_to_temp(
                group, identifier, decode_progress, stop, include_log=True
            )
        except LogImportCancelled:
            raise
        except Exception:
            # Preserve a failed or unsupported archive for later inspection.
            for temporary in self.originals.glob(f".{identifier}_*.log.tmp"):
                temporary.unlink(missing_ok=True)
            if len(group) == 1:
                return self._import_original(group[0], progress=progress)
            return self._import_ebl_archive(
                group, identifier, source_signature, progress
            )
        return self.install_group(
            group, identifier, source_signature, staged, first_log, progress
        )

    def _import_ebl_archive(self, group, identifier, source_signature, progress):
        """Copy an unsupported EBL series unchanged into the library."""
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

    @staticmethod
    def _ebl_metadata(session_log):
        diagnostics = list(session_log.diagnostics)
        status = (
            (
                f"{session_log.format}: {session_log.frame_count:,} raw CAN frames; "
                f"{len(session_log.entries):,} replay entries"
            )
            if session_log.entries and session_log.frame_count is not None
            else (
                f"{session_log.format}: {len(session_log.entries):,} sentences"
                if session_log.entries
                else "Archive only — no supported replay data"
            )
        )
        return {
            "status": status,
            "diagnostics": diagnostics,
            "file_info": {
                "path": "",
                "format": "NMEA 0183 (decoded from EBL)",
                "entries": len(session_log.entries),
                "duration": session_log.duration,
                "timestamp_origin": (
                    session_log.timestamp_origin.isoformat()
                    if getattr(session_log, "timestamp_origin", None) is not None
                    else None
                ),
                "description": getattr(session_log, "ebl_description", None),
                "timestamped": session_log.timestamped,
                "skipped": session_log.skipped,
                "frame_count": session_log.frame_count,
                "source_tracks": list(session_log.source_tracks),
                "device_metadata_available": session_log.device_metadata_available,
                "devices": [asdict(device) for device in session_log.devices],
                "diagnostics": diagnostics,
            },
        }

    @staticmethod
    def _ebl_session_name(group, index, multiple):
        first = group[0]
        if not multiple:
            return (
                f"{first.parent.name}/{first.stem.split('_')[0]}.log"
                if len(group) > 1
                else first.stem + ".log"
            )
        return f"{first.parent.name}/{first.stem.split('_')[0]}_session{index + 1}.log"

    @staticmethod
    def _ebl_session_key(group, session_log):
        """Stable identity for a recording burst.

        The resolved source directory namespaces simultaneous recorders, and
        the first replay frame index separates memory-budget continuations
        that share one TimeUTC value. Untimed bursts use ``untimed`` plus that
        index, so growth still updates the same entry.
        """
        entries = getattr(session_log, "entries", ())
        first_index = getattr(entries[0][1], "log_index", 0) if entries else 0
        origin = getattr(session_log, "timestamp_origin", None)
        stamp = origin.isoformat() if origin is not None else "untimed"
        source = str(group[0].parent.resolve())
        return f"{source}|{stamp}|{first_index}"

    @staticmethod
    def group_paths(paths):
        """Group selected split EBL siblings in first-occurrence import order."""
        return _ebl_series(paths)

    def import_paths(self, paths, progress=None, decode_progress=None, stop=None):
        """Import paths, concatenating selected sibling vendor segments losslessly."""
        with self.import_lock:
            return self._import_paths(paths, progress, decode_progress, stop)

    def _import_paths(self, paths, progress=None, decode_progress=None, stop=None):
        groups = self.group_paths(paths)
        results = {}
        ebl_groups = []
        other_groups = []
        for index, group in enumerate(groups):
            if all(path.suffix.lower() == ".ebl" for path in group):
                ebl_groups.append((index, group))
            else:
                other_groups.append((index, group))
        prepared = []
        for index, group in ebl_groups:
            existing = self._existing_ebl_item(group)
            if existing is not None:
                results[index] = existing
            else:
                prepared.append((index, group))
        if len(prepared) > 1:
            installed = set()
            try:
                decoded = self._decode_groups_parallel(prepared, decode_progress, stop)
                for index, group, identifier, signature, staged in decoded:
                    results[index] = self.install_group(
                        group, identifier, signature, staged, None, progress
                    )
                    installed.add(index)
            except LogImportCancelled:
                raise
            except Exception:
                for index, group in prepared:
                    if index not in installed:
                        results[index] = self._import_ebl(
                            group, progress, decode_progress, stop
                        )
        else:
            for index, group in prepared:
                results[index] = self._import_ebl(
                    group, progress, decode_progress, stop
                )
        for index, group in other_groups:
            if len(group) == 1:
                results[index] = self.import_path(group[0], progress=progress)
            else:
                for path in group:
                    results[index] = self.import_path(path, progress=progress)
        return [results[index] for index in sorted(results)]

    def _decode_groups_parallel(self, prepared, decode_progress=None, stop=None):
        """Decode independent EBL groups in spawned processes."""
        from concurrent.futures import FIRST_COMPLETED, wait

        context = multiprocessing.get_context("spawn")
        total = sum(sum(path.stat().st_size for path in group) for _, group in prepared)
        results = []
        completed = 0
        executor = ProcessPoolExecutor(
            max_workers=min(len(prepared), os.cpu_count() or 1),
            mp_context=context,
        )
        identifiers = {}
        try:
            futures = {}
            for index, group in prepared:
                identifier = uuid4().hex
                identifiers[index] = identifier
                signature = self._source_signature(group)
                future = executor.submit(
                    _decode_group_to_temp,
                    str(self.originals),
                    tuple(str(path) for path in group),
                    identifier,
                    None,
                    False,
                )
                futures[future] = (index, group, identifier, signature)
            pending = set(futures)
            while pending:
                if stop is not None and stop():
                    for future in pending:
                        future.cancel()
                    raise LogImportCancelled()
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    index, group, identifier, signature = futures[future]
                    staged, _ = future.result()
                    results.append((index, group, identifier, signature, staged))
                    completed += sum(path.stat().st_size for path in group)
                    if decode_progress is not None and total:
                        decode_progress(min(completed, total), total)
            return results
        except BaseException:
            for index in identifiers:
                for temporary in self.originals.glob(
                    f".{identifiers[index]}_*.log.tmp"
                ):
                    temporary.unlink(missing_ok=True)
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

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
            "name",
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
