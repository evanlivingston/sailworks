"""One-way sync of W2K-1 EBL logs into the local log library.

The device records each session into a folder of ~5 MB EBL segments. A sync
downloads segments that are not already present and imports closed folders as
one series. The segment being written in an in-progress folder is left until
the device rolls over, so a partial file never becomes a library entry.
"""

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .w2kapi import W2KError, W2KFile, safe_component

MANIFEST_VERSION = 1
NEW = "new"
PRESENT = "present"
ACTIVE = "active"


class W2KSyncCancelled(Exception):
    """Raised when a sync is asked to stop between files or folders."""


@dataclass(frozen=True)
class W2KEntry:
    folder: str
    file: W2KFile
    state: str


@dataclass
class W2KScan:
    serial: str
    entries: list = field(default_factory=list)

    def counts(self):
        total = {NEW: 0, PRESENT: 0, ACTIVE: 0}
        for entry in self.entries:
            total[entry.state] = total.get(entry.state, 0) + 1
        return total


@dataclass
class W2KSyncSummary:
    serial: str = ""
    folders: int = 0
    downloaded: int = 0
    skipped: int = 0
    imported: int = 0
    size: int = 0
    pending: list = field(default_factory=list)

    def describe(self):
        parts = [f"{self.downloaded} downloaded"]
        if self.imported:
            parts.append(f"{self.imported} imported")
        if self.skipped:
            parts.append(f"{self.skipped} already present")
        if self.pending:
            parts.append(f"{len(self.pending)} still recording")
        return ", ".join(parts)


class W2KSync:
    """Mirror one device's closed EBL segments into a :class:`LogLibrary`."""

    def __init__(self, client, library, manifest=None, staging=None):
        self.client = client
        self.library = library
        base = Path(library.directory)
        self.manifest_path = Path(manifest or base / "w2k-sync.json")
        self.staging = Path(staging or base / "w2k")
        self._manifest = self._load_manifest()
        self._retained_sources = None

    # -- manifest ---------------------------------------------------------

    def _load_manifest(self):
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": MANIFEST_VERSION, "devices": {}}
        if data.get("version") != MANIFEST_VERSION or not isinstance(
            data.get("devices"), dict
        ):
            return {"version": MANIFEST_VERSION, "devices": {}}
        return data

    def _save_manifest(self):
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_name(
            f".{self.manifest_path.name}.{uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(self._manifest, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.manifest_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _state(self, serial):
        devices = self._manifest.setdefault("devices", {})
        return devices.setdefault(serial, {"files": {}, "imported": []})

    def _library_has_original(self, path):
        """True when the library retained this staged segment as an original."""
        if self._retained_sources is None:
            self._retained_sources = {
                record.get("source")
                for item in self.library.entries()
                for record in (item.get("file_info") or {}).get("ebl_originals", ())
            }
        return str(Path(path).resolve()) in self._retained_sources

    def _folder_has_replay(self, serial, folder):
        source = str((self.staging / serial / folder).resolve())
        for item in self.library.entries():
            info = item.get("file_info") or {}
            if info.get("ebl_source") == source and info.get("entries", 0) > 0:
                return True
        return False

    def present(self, serial, file):
        files = self._state(serial).get("files", {})
        if files.get(file.key) != file.size:
            return False
        staged = self.staging / serial / file.folder / file.name
        if staged.is_file() or self._library_has_original(staged):
            return True
        retained = self._state(serial).get("retained", {})
        return retained.get(file.key) == file.size

    @staticmethod
    def _folder_signature(entries):
        return "|".join(
            f"{entry.file.name}:{entry.file.size}"
            for entry in sorted(entries, key=lambda item: item.file.name)
        )

    def imported(self, serial, folder):
        return folder in self._state(serial).get("imported", [])

    # -- planning ---------------------------------------------------------

    def _scan(self):
        info = self.client.info()
        serial = info.serial_number or "unknown"
        if serial != "unknown":
            safe_component(serial, "serial number")
        entries = []
        for folder in self.client.folders():
            files = sorted(
                (
                    item
                    for item in self.client.files(folder)
                    if item.name.lower().endswith(".ebl")
                ),
                key=lambda item: item.name,
            )
            if not files:
                continue
            active = files[-1].name if folder.in_progress else None
            for item in files:
                if item.name == active:
                    state = ACTIVE
                elif self.present(serial, item):
                    state = PRESENT
                else:
                    state = NEW
                entries.append(W2KEntry(folder.name, item, state))
        return info, serial, entries

    def scan(self):
        """Describe what a sync would do without downloading anything."""
        _, serial, entries = self._scan()
        return W2KScan(serial=serial, entries=entries)

    # -- local copy -------------------------------------------------------

    @staticmethod
    def _link_or_copy(source, target):
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)

    def seed(self, local_root):
        """Stage files from a copied SD card and record them as present.

        Only files that the device also reports with the same size are staged:
        a segment still growing on the device, or a stale local copy, is left
        for a normal download. Returns the number of staged files.
        """
        root = Path(local_root)
        if (root / "ebl_data_logs").is_dir():
            root = root / "ebl_data_logs"
        if not any(root.glob("EBL*")):
            raise ValueError("Choose the folder that contains EBL000xxx folders")

        _, serial, entries = self._scan()
        known = {entry.file.key: entry.file.size for entry in entries}
        state = self._state(serial)
        staged = 0
        for folder_dir in sorted(root.glob("EBL*")):
            if not folder_dir.is_dir():
                continue
            for source in sorted(folder_dir.glob("*.ebl")):
                key = f"{folder_dir.name}/{source.name}"
                size = source.stat().st_size
                if known.get(key) != size:
                    continue
                target = self.staging / serial / folder_dir.name / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists() or target.stat().st_size != size:
                    target.unlink(missing_ok=True)
                    self._link_or_copy(source, target)
                state.setdefault("files", {})[key] = size
                staged += 1
        self._save_manifest()
        return staged

    # -- sync -------------------------------------------------------------

    def run(self, progress=None, message=None, stop=None):
        _, serial, entries = self._scan()
        state = self._state(serial)
        summary = W2KSyncSummary(serial=serial)
        summary.folders = len({entry.folder for entry in entries})
        summary.pending = [entry.file.key for entry in entries if entry.state == ACTIVE]

        pending_files = [
            entry
            for entry in entries
            if entry.state in (NEW, ACTIVE) and not self.present(serial, entry.file)
        ]
        pending_bytes = sum(entry.file.size for entry in pending_files)

        folders = {}
        for entry in entries:
            folders.setdefault(entry.folder, []).append(entry)
        imported = state.setdefault("folders", {})
        library_sources = {
            (item.get("file_info") or {}).get("ebl_source")
            for item in self.library.entries()
            if (item.get("file_info") or {}).get("ebl_source")
        }
        import_folders = []
        for folder, folder_entries in folders.items():
            signature = self._folder_signature(folder_entries)
            source = str((self.staging / serial / folder).resolve())
            if imported.get(folder) == signature and source in library_sources:
                continue
            import_folders.append((folder, signature))
        import_bytes = sum(
            sum(entry.file.size for entry in folders[folder])
            for folder, _ in import_folders
        )
        total_bytes = pending_bytes + import_bytes
        download_done = 0
        import_done = 0

        def report():
            if progress is None:
                return
            if not total_bytes:
                progress(100)
            else:
                progress(round(100 * (download_done + import_done) / total_bytes))

        if message is not None:
            message(f"Connected to {serial or 'W2K-1'}")
        report()

        for entry in pending_files:
            if stop is not None and stop():
                raise W2KSyncCancelled()
            item = entry.file
            root = (self.staging / serial).resolve()
            destination = root / item.folder / item.name
            if not destination.resolve().is_relative_to(root):
                raise ValueError(f"Unsafe download destination: {destination}")
            if message is not None:
                message(f"Downloading {item.folder}/{item.name}")

            def on_bytes(written, total, base=download_done):
                if progress is None or not total_bytes:
                    return
                progress(round(100 * (base + written) / total_bytes))

            written = self.client.download(item, destination, progress=on_bytes)
            if written != item.size and entry.state != ACTIVE:
                destination.unlink(missing_ok=True)
                raise W2KError(
                    f"Short download for {item.key}: {written} of {item.size} bytes"
                )
            state.setdefault("files", {})[item.key] = written
            self._save_manifest()
            download_done += written
            summary.downloaded += 1
            summary.size += written
            report()

        summary.skipped = len(entries) - len(pending_files)

        jobs = []
        for folder, signature in import_folders:
            folder_entries = folders[folder]
            root = (self.staging / serial / folder).resolve()
            paths = [root / entry.file.name for entry in folder_entries]
            if not all(path.is_file() for path in paths):
                continue
            jobs.append((folder, signature, paths, folder_entries))
        if jobs:
            from .loglibrary import LogLibrary

            if message is not None:
                message(f"Importing {len(jobs)} folder{'s' if len(jobs) != 1 else ''}")
            keep = getattr(self.library, "keep_ebl_originals", True)

            def mark(folder, signature, paths, folder_entries):
                nonlocal import_done
                import_done += sum(entry.file.size for entry in folder_entries)
                imported[folder] = signature
                if not keep and self._folder_has_replay(serial, folder):
                    # The decoded log is installed; the staged copy is no
                    # longer needed and the manifest prevents re-download.
                    retained = state.setdefault("retained", {})
                    for path, entry in zip(paths, folder_entries):
                        path.unlink(missing_ok=True)
                        retained[entry.file.key] = entry.file.size
                self._save_manifest()
                summary.imported += 1
                self._retained_sources = None
                report()

            if isinstance(self.library, LogLibrary) and len(jobs) > 1:
                all_paths = [path for _, _, paths, _ in jobs for path in paths]

                def on_decode(read, total, base=import_done):
                    if progress is None or not total_bytes:
                        return
                    progress(round(100 * (download_done + base + read) / total_bytes))

                self.library.import_paths(
                    [str(path) for path in all_paths],
                    decode_progress=on_decode,
                    stop=stop,
                )
                for job in jobs:
                    mark(*job)
            else:
                for folder, signature, paths, folder_entries in jobs:
                    if stop is not None and stop():
                        raise W2KSyncCancelled()

                    def on_decode(read, total, base=import_done):
                        if progress is None or not total_bytes:
                            return
                        progress(
                            round(100 * (download_done + base + read) / total_bytes)
                        )

                    self.library.import_paths(
                        [str(path) for path in paths], decode_progress=on_decode
                    )
                    mark(folder, signature, paths, folder_entries)

        if message is not None:
            message(summary.describe())
        if progress is not None:
            progress(100)
        return summary
