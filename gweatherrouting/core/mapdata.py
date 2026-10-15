"""Staged, cancellable installation of the base data used by both frontends."""

import tempfile
import zipfile
from pathlib import Path

import requests
from osgeo import ogr

COASTLINES_URL = (
    "https://github.com/dakk/gweatherrouting/releases/download/"
    "gshhs2.3.6/gshhg-shp-2.3.6.zip"
)
SEAMARKS_URL = "https://github.com/dakk/osm-seamarks/raw/master/seamarks.pbf"
DATASETS = {
    "coastlines": ("World coastlines (~140 MB)", COASTLINES_URL, "gshhs"),
    "seamarks": ("OpenSeaMap sea marks (~2 MB)", SEAMARKS_URL, "seamarks.pbf"),
}


class MapDownloadCancelledError(Exception):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise MapDownloadCancelledError("Download cancelled")


def download_file(url, path, cancel, progress):
    check_cancel(cancel)
    with requests.get(url, stream=True, timeout=(15, 30)) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        size, last_percent = 0, -1
        progress(None if not total else 0)
        with path.open("wb") as output:
            for chunk in response.iter_content(chunk_size=65536):
                check_cancel(cancel)
                output.write(chunk)
                size += len(chunk)
                percent = int(size * 100 / total) if total else -1
                if percent != last_percent:
                    progress(min(size / total, 1))
                    last_percent = percent
        if total and size != total:
            raise ValueError("The map download is incomplete. Please retry.")
        if not size:
            raise ValueError("The map download was empty. Please retry.")


def unpack_coastlines(archive, staging, cancel):
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            check_cancel(cancel)
            target = (staging / member.filename).resolve()
            if not target.is_relative_to(staging.resolve()):
                raise ValueError("The coastline archive contains an invalid path.")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("The coastline archive contains a symbolic link.")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(member) as source, target.open("wb") as output:
                while True:
                    check_cancel(cancel)
                    chunk = source.read(65536)
                    if not chunk:
                        break
                    output.write(chunk)
    roots = list(staging.rglob("GSHHS_shp"))
    if len(roots) != 1:
        raise ValueError("The download does not contain world coastline data.")
    return roots[0].parent


def validate_data(kind, path, cancel):
    paths = (
        [path / "GSHHS_shp" / q / f"GSHHS_{q}_L1.shp" for q in "fhilc"]
        if kind == "coastlines"
        else [path]
    )
    for source in paths:
        check_cancel(cancel)
        driver = "ESRI Shapefile" if kind == "coastlines" else "OSM"
        dataset = ogr.GetDriverByName(driver).Open(str(source))
        if dataset is None or dataset.GetLayerCount() == 0:
            raise ValueError("Downloaded map data could not be opened: " + source.name)
        if kind == "coastlines":
            for suffix in (".shp", ".shx", ".dbf"):
                if not source.with_suffix(suffix).is_file():
                    raise ValueError("Downloaded coastline data is incomplete.")
        dataset = None


def install_one(kind, directory, cancel, report):
    """Keep an existing installation until a complete replacement is validated."""
    label, url, filename = DATASETS[kind]
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".map-download-", dir=directory) as temp:
        staging = Path(temp)
        download = staging / ("coastlines.zip" if kind == "coastlines" else filename)
        download_file(
            url,
            download,
            cancel,
            lambda fraction: report("Downloading " + label, fraction),
        )
        report("Checking " + label, None)
        ready = (
            unpack_coastlines(download, staging / "unpacked", cancel)
            if kind == "coastlines"
            else download
        )
        validate_data(kind, ready, cancel)
        check_cancel(cancel)
        destination, backup = directory / filename, staging / "previous"
        if destination.exists():
            destination.replace(backup)
        try:
            ready.replace(destination)
        except Exception:
            if backup.exists():
                backup.replace(destination)
            raise
        report(label + " installed", 1)
    return kind


def install_map_data(kinds, directory, cancel, report):
    """Return completed datasets even when a later download fails or is cancelled."""
    installed = []
    try:
        for kind in kinds:
            check_cancel(cancel)
            installed.append(install_one(kind, directory, cancel, report))
    except Exception as error:
        return tuple(installed), error
    return tuple(installed), None
