import logging
import os
import shutil
import tempfile
from pathlib import Path

from .polarfiles import download_orc_file, install_polar

from gweatherrouting.common import resource_path
from gweatherrouting.core.storage import POLAR_DIR, Storage
from gweatherrouting.core.utils import EventDispatcher

logger = logging.getLogger("gweatherrouting")


class PolarManagerStorage(Storage):
    def __init__(self):
        Storage.__init__(self, "polar-manager")
        self.load_or_save_default()


class PolarManager(EventDispatcher):
    def __init__(self):
        EventDispatcher.__init__(self)
        self.storage = PolarManagerStorage()
        self.polars_files: list[str] = []
        self.polars = []

        if not self.storage.metadata:
            self.storage.metadata = {}

        self.refresh_local_polars()

        if self.storage.enabled is None:
            self.storage.enabled = self.polars_files[:1]
        self.polars = sorted(set(self.storage.enabled).intersection(self.polars_files))
        self.store_enabled_polars()

    def refresh_local_polars(self):
        if not os.listdir(POLAR_DIR):
            for path in Path(resource_path("gweatherrouting", "data/polars/")).glob(
                "*.pol"
            ):
                shutil.copyfile(path, Path(POLAR_DIR) / path.name)
        self.polars_files = sorted(
            p.name
            for p in Path(POLAR_DIR).iterdir()
            if p.is_file() and p.suffix.lower() == ".pol"
        )
        self.polars = sorted(set(self.polars).intersection(self.polars_files))

    def _save_library(self, enabled, metadata):
        previous = {"enabled": self.storage.enabled, "metadata": self.storage.metadata}
        saved_enabled = sorted(set(enabled))
        values = {"enabled": saved_enabled, "metadata": dict(metadata)}
        for key, value in values.items():
            dict.__setitem__(self.storage, key, value)
            self.storage.__dict__[key] = value
        try:
            self.storage.save()
        except Exception:
            for key, value in previous.items():
                dict.__setitem__(self.storage, key, value)
                self.storage.__dict__[key] = value
            raise
        self.polars = saved_enabled

    def store_enabled_polars(self):
        self._save_library(self.polars, self.storage.metadata)

    def load(self, path):
        self.add_polar_file(path)

    def enable(self, name):
        if name not in self.polars_files:
            raise ValueError("Choose an installed polar")
        self._save_library([*self.polars, name], self.storage.metadata)
        self.dispatch("polars-list-updated", self.polars)

    def disable(self, name):
        self._save_library([p for p in self.polars if p != name], self.storage.metadata)
        self.dispatch("polars-list-updated", self.polars)

    def is_enabled(self, name) -> bool:
        return name in self.polars

    def get_local_path(self, name):
        root = Path(POLAR_DIR).resolve()
        path = root / name
        if (
            Path(name).name != name
            or path.parent != root
            or name not in self.polars_files
        ):
            raise ValueError("Choose an installed polar")
        return str(path)

    def delete_polar(self, name):
        path = Path(self.get_local_path(name))
        metadata = dict(self.storage.metadata)
        metadata.pop(name, None)
        with tempfile.TemporaryDirectory(dir=POLAR_DIR) as directory:
            staged = Path(directory) / name
            path.replace(staged)
            try:
                self._save_library([p for p in self.polars if p != name], metadata)
            except Exception:
                staged.replace(path)
                raise
        self.polars_files.remove(name)
        self.dispatch("polars-list-updated", self.polars)

    def register_polar_file(self, name, metadata=None, dispatch=True):
        path = Path(POLAR_DIR) / name
        if (
            Path(name).name != name
            or not path.is_file()
            or path.suffix.lower() != ".pol"
        ):
            raise ValueError("Choose an installed .pol file")
        saved = dict(self.storage.metadata)
        if metadata is not None:
            saved[name] = metadata
        self._save_library([*self.polars, name], saved)
        self.polars_files = sorted(set([*self.polars_files, name]))
        if dispatch:
            self.dispatch("polars-list-updated", self.polars)

    def add_polar_file(self, polar_path):
        name = install_polar(polar_path, POLAR_DIR)
        self.register_polar_file(name)
        return name

    def get_metadata(self, name):
        return self.storage.metadata.get(name, {})

    def get_path(self, name):
        if name not in self.polars:
            logger.error(f"Polar {name} not found in the list of polars.")
            return None
        return os.path.join(POLAR_DIR, name)

    def download_orc_polar(self, orc_polar_name, dispatch=True):
        from .polarfiles import orc_filename

        filename = orc_filename(orc_polar_name)
        if filename in self.polars_files:
            return filename
        name, metadata = download_orc_file(orc_polar_name, POLAR_DIR)
        self.register_polar_file(name, metadata, dispatch=dispatch)
        return name


def json_to_polars(json_data, destination_path):
    angles = json_data["angles"]
    speeds = json_data["speeds"]
    polars = {}
    for angle in angles:
        if str(angle) in json_data:
            polars[angle] = json_data[str(angle)]

    with open(destination_path, "w") as f:
        speeds_str = "\t".join(map(str, speeds))
        f.write(f"TWA\\TWS\t{speeds_str}\n")
        for _ in range(len(speeds)):
            f.write("0\t")
        f.write("0\n")
        for angle in sorted(polars.keys()):
            line = (
                f"{angle}\t"
                + "\t".join(f"{speed:.2f}" for speed in polars[angle])
                + "\n"
            )
            f.write(line)
        f.write("180\t")
        for _ in range(len(speeds) - 1):
            f.write("0\t")
        f.write("0\n")
