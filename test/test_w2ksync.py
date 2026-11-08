"""Tests for mirroring W2K-1 EBL segments into the log library."""

import tempfile
import unittest
from pathlib import Path

from sailworks.core.w2kapi import W2KDevice, W2KFile, W2KFolder
from sailworks.core.w2ksync import ACTIVE, NEW, PRESENT, W2KSync


class FakeClient:
    def __init__(self, folders, files, serial="W2K-12345", content=None):
        self.folder_list = list(folders)
        self.file_map = dict(files)
        self.serial = serial
        self.content = content
        self.downloaded = []

    def info(self):
        return W2KDevice(serial_number=self.serial, firmware="1.398")

    def folders(self):
        return list(self.folder_list)

    def files(self, folder):
        return list(self.file_map.get(folder.name, []))

    def download(self, file, destination, progress=None):
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = self.content if self.content is not None else b"x" * file.size
        target.write_bytes(data)
        if progress is not None:
            progress(len(data), len(data))
        self.downloaded.append(file.key)
        return len(data)


class FakeLibrary:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.imports = []

    def import_paths(self, paths, decode_progress=None):
        if decode_progress is not None:
            total = sum(Path(path).stat().st_size for path in paths)
            decode_progress(total, total)
        self.imports.append(tuple(paths))
        return [{"id": f"log-{len(self.imports)}"}]


def folder(name, in_progress=False, size=0):
    return W2KFolder(name=name, size=size, in_progress=in_progress)


def ebl(folder_name, name, size):
    return W2KFile(folder=folder_name, name=name, size=size, time=1700000000)


class W2KSyncTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.library = FakeLibrary(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _sync(self, folders, files):
        client = FakeClient(folders, files)
        return client, W2KSync(client, self.library)

    def test_first_sync_downloads_closed_and_skips_active_segment(self):
        closed = folder("EBL000000")
        open_folder = folder("EBL000001", in_progress=True)
        files = {
            "EBL000000": [ebl("EBL000000", "000000_080.ebl", 100)],
            "EBL000001": [
                ebl("EBL000001", "000000_080.ebl", 200),
                ebl("EBL000001", "000000_160.ebl", 300),
            ],
        }
        client, sync = self._sync([closed, open_folder], files)

        progress = []
        summary = sync.run(progress=progress.append)

        self.assertEqual(
            client.downloaded,
            [
                "EBL000000/000000_080.ebl",
                "EBL000001/000000_080.ebl",
                "EBL000001/000000_160.ebl",
            ],
        )
        self.assertEqual(summary.downloaded, 3)
        self.assertEqual(summary.imported, 2)
        self.assertEqual(summary.size, 600)
        self.assertEqual(summary.pending, ["EBL000001/000000_160.ebl"])
        self.assertEqual(len(self.library.imports), 2)
        self.assertEqual(
            [Path(item).name for item in self.library.imports[1]],
            ["000000_080.ebl", "000000_160.ebl"],
        )
        self.assertEqual(progress[-1], 100)

    def test_second_sync_downloads_nothing(self):
        closed = folder("EBL000000")
        open_folder = folder("EBL000001", in_progress=True)
        files = {
            "EBL000000": [ebl("EBL000000", "000000_080.ebl", 100)],
            "EBL000001": [
                ebl("EBL000001", "000000_080.ebl", 200),
                ebl("EBL000001", "000000_160.ebl", 300),
            ],
        }
        client, sync = self._sync([closed, open_folder], files)
        sync.run()
        second_client = FakeClient([closed, open_folder], files)
        second = W2KSync(second_client, self.library)

        summary = second.run()

        self.assertEqual(second_client.downloaded, [])
        self.assertEqual(summary.downloaded, 0)
        self.assertEqual(summary.imported, 0)
        self.assertEqual(summary.skipped, 3)
        self.assertEqual(len(self.library.imports), 2)

    def test_resync_imports_a_grown_active_file(self):
        open_folder = folder("EBL000001", in_progress=True)
        files = {"EBL000001": [ebl("EBL000001", "000000_080.ebl", 100)]}
        _, sync = self._sync([open_folder], files)
        sync.run()

        files["EBL000001"] = [ebl("EBL000001", "000000_080.ebl", 200)]
        grown = FakeClient([open_folder], files)
        summary = W2KSync(grown, self.library).run()

        self.assertEqual(grown.downloaded, ["EBL000001/000000_080.ebl"])
        self.assertEqual(summary.downloaded, 1)
        self.assertEqual(summary.imported, 1)
        self.assertEqual(len(self.library.imports), 2)

    def test_scan_reports_new_present_and_active(self):
        closed = folder("EBL000000")
        open_folder = folder("EBL000001", in_progress=True)
        files = {
            "EBL000000": [ebl("EBL000000", "000000_080.ebl", 100)],
            "EBL000001": [
                ebl("EBL000001", "000000_080.ebl", 200),
                ebl("EBL000001", "000000_160.ebl", 300),
            ],
        }
        _, sync = self._sync([closed, open_folder], files)
        sync.run()
        client = FakeClient([closed, open_folder], files)
        scan = W2KSync(client, self.library).scan()

        states = {
            (entry.folder, entry.file.name): entry.state for entry in scan.entries
        }
        self.assertEqual(states[("EBL000000", "000000_080.ebl")], PRESENT)
        self.assertEqual(states[("EBL000001", "000000_080.ebl")], PRESENT)
        self.assertEqual(states[("EBL000001", "000000_160.ebl")], ACTIVE)
        self.assertEqual(scan.counts(), {NEW: 0, PRESENT: 2, ACTIVE: 1})

    def test_new_closed_folder_is_downloaded_and_imported(self):
        first = folder("EBL000000")
        files = {"EBL000000": [ebl("EBL000000", "000000_080.ebl", 100)]}
        _, sync = self._sync([first], files)
        sync.run()

        added = folder("EBL000001")
        files["EBL000001"] = [ebl("EBL000001", "000000_080.ebl", 400)]
        client = FakeClient([first, added], files)
        summary = W2KSync(client, self.library).run()

        self.assertEqual(client.downloaded, ["EBL000001/000000_080.ebl"])
        self.assertEqual(summary.imported, 1)
        self.assertEqual(len(self.library.imports), 2)

    def test_empty_device_is_a_no_op(self):
        client, sync = self._sync([], {})
        summary = sync.run()
        self.assertEqual(client.downloaded, [])
        self.assertEqual(summary.downloaded, 0)
        self.assertEqual(summary.imported, 0)

    def test_seed_adopts_only_files_that_match_the_device(self):
        card = Path(self.temporary.name) / "card"
        (card / "EBL000000").mkdir(parents=True)
        (card / "EBL000000" / "000000_080.ebl").write_bytes(b"a" * 100)
        (card / "EBL000001").mkdir()
        (card / "EBL000001" / "000000_080.ebl").write_bytes(b"b" * 200)

        closed = folder("EBL000000")
        growing = folder("EBL000001")
        files = {
            "EBL000000": [ebl("EBL000000", "000000_080.ebl", 100)],
            "EBL000001": [ebl("EBL000001", "000000_080.ebl", 300)],
        }
        client = FakeClient([closed, growing], files)
        sync = W2KSync(client, self.library)

        self.assertEqual(sync.seed(card), 1)
        self.assertTrue(sync.present("W2K-12345", files["EBL000000"][0]))
        self.assertFalse(sync.present("W2K-12345", files["EBL000001"][0]))
        self.assertTrue((sync.staging / "W2K-12345/EBL000000/000000_080.ebl").is_file())

        summary = sync.run()
        self.assertEqual(client.downloaded, ["EBL000001/000000_080.ebl"])
        self.assertEqual(summary.downloaded, 1)

    def test_seed_accepts_the_ebl_data_logs_subfolder(self):
        card = Path(self.temporary.name) / "card"
        root = card / "ebl_data_logs"
        (root / "EBL000000").mkdir(parents=True)
        (root / "EBL000000" / "000000_080.ebl").write_bytes(b"a" * 100)

        closed = folder("EBL000000")
        files = {"EBL000000": [ebl("EBL000000", "000000_080.ebl", 100)]}
        sync = W2KSync(FakeClient([closed], files), self.library)

        self.assertEqual(sync.seed(card), 1)

    def test_seed_rejects_a_folder_without_ebl_directories(self):
        root = Path(self.temporary.name) / "empty"
        root.mkdir()
        sync = W2KSync(FakeClient([], {}), self.library)
        with self.assertRaises(ValueError):
            sync.seed(root)

    def test_sync_imports_a_real_ebl_into_the_library(self):
        from datetime import datetime, timezone

        from sailworks.core.ebl import FILETIME_EPOCH
        from sailworks.core.loglibrary import LogLibrary

        def tag(identifier, data):
            payload = bytes([identifier]) + data
            return b"\x1b\x01" + payload.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\x0a"

        stamp = datetime(2024, 1, 2, 12, 34, 56, 789012, tzinfo=timezone.utc)
        delta = stamp - FILETIME_EPOCH
        ticks = (
            (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
        ) * 10
        frame = bytes.fromhex("95 0e 01 20 30 02 f8 09 ff fc 37 0a 00 10 ff ff")
        payload = (
            tag(3, ticks.to_bytes(8, "little"))
            + tag(1, (1002).to_bytes(4, "little"))
            + tag(7, frame)
        )

        library = LogLibrary(self.temporary.name)
        closed = folder("EBL000000")
        files = {
            "EBL000000": [
                ebl("EBL000000", "000000_080.ebl", len(payload)),
            ]
        }
        client = FakeClient([closed], files, content=payload)
        summary = W2KSync(client, library).run()

        self.assertEqual(summary.imported, 1)
        entries = library.entries()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["status"])
        self.assertTrue(library.path(entries[0]["id"]).is_file())


if __name__ == "__main__":
    unittest.main()
