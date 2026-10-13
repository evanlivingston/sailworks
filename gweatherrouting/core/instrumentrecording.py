"""Thread-safe recording of interpreted NMEA and original NMEA 2000 PGNs."""

import shutil
from pathlib import Path
from threading import Lock


class InstrumentRecording:
    def __init__(self, path):
        self.path = Path(path)
        self.raw_path = Path(str(path) + ".n2k.log")
        self.lock = Lock()
        self.text = None
        self.raw = None

    def start(self):
        with self.lock:
            self._close()
            self.text = self.path.open("w", buffering=1)
            # Open raw lazily, so NMEA 0183 recordings have no empty companion.
            self.raw_path.unlink(missing_ok=True)

    def write_packets(self, packets):
        with self.lock:
            if self.text is not None:
                for packet in packets:
                    self.text.write(packet.serialize() + "\n")

    def write_nmea2000(self, messages):
        with self.lock:
            if self.text is None:
                return
            if self.raw is None:
                self.raw = self.raw_path.open("w", buffering=1)
            for message in messages:
                self.raw.write(message.to_canboat() + "\n")

    def _close(self):
        for handle in (self.text, self.raw):
            if handle is not None:
                handle.close()
        self.text = self.raw = None

    def close(self):
        with self.lock:
            self._close()

    def save(self, destination):
        with self.lock:
            for handle in (self.text, self.raw):
                if handle is not None:
                    handle.flush()
            destination = Path(destination)
            if destination.resolve() != self.path.resolve():
                shutil.copyfile(self.path, destination)
            if self.raw_path.exists():
                raw_destination = Path(str(destination) + ".n2k.log")
                if raw_destination.resolve() != self.raw_path.resolve():
                    shutil.copyfile(self.raw_path, raw_destination)
