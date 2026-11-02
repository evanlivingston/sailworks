"""Validated, staged polar files without changing routing interpolation."""

import math
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import requests
import weatherrouting

ORC_ROOT = "https://raw.githubusercontent.com/jieter/orc-data/refs/heads/master/site"


def read_polar(path):
    try:
        # Supply the stream so it also closes when the external parser fails.
        with Path(path).open(encoding="utf-8-sig") as stream:
            polar = weatherrouting.Polar(str(path), stream)
    except (OSError, ValueError, IndexError) as exc:
        raise ValueError(f"Unable to read polar: {exc}") from exc
    _validate_axes(polar)
    if len(polar.speed_table) != len(polar.twa) or any(
        len(row) != len(polar.tws) for row in polar.speed_table
    ):
        raise ValueError("Each polar angle must contain one boat speed per wind speed")
    if any(not math.isfinite(v) or v < 0 for row in polar.speed_table for v in row):
        raise ValueError("Polar boat speeds must be finite and nonnegative")
    return polar


def _validate_axes(polar):
    for values, maximum, label in (
        (polar.tws, math.inf, "Wind speeds"),
        (polar.twa, math.pi, "Wind angles"),
    ):
        if len(values) < 2 or any(
            not math.isfinite(v) or v < 0 or v > maximum for v in values
        ):
            raise ValueError(
                f"{label} must contain at least two finite, nonnegative values"
            )
        if any(a >= b for a, b in zip(values, values[1:])):
            raise ValueError(f"{label} must be strictly increasing")


def install_polar(source, directory, name=None):
    source = Path(source).resolve()
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    read_polar(source)
    name = name or source.name
    if Path(name).name != name or Path(name).suffix.lower() != ".pol":
        raise ValueError("Choose a .pol file")
    destination = root / name
    if source == destination:
        return destination.name
    if destination.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = root / f"{Path(name).stem}-{stamp}.pol"
    with tempfile.NamedTemporaryFile(dir=root, suffix=".part", delete=False) as out:
        temporary = Path(out.name)
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination.name


def orc_filename(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", identifier
    ):
        raise ValueError("Invalid ORC boat identifier")
    country, boat = identifier.split("/")
    if boat in (".", ".."):
        raise ValueError("Invalid ORC boat identifier")
    return f"{country}_{boat}.pol"


def orc_catalog():
    with requests.get(ORC_ROOT + "/index.json", timeout=(10, 30)) as response:
        response.raise_for_status()
        rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("Invalid ORC catalog")
    result = []
    for row in rows:
        if (
            not isinstance(row, list)
            or len(row) < 3
            or not all(isinstance(v, str) for v in row[:3])
        ):
            continue
        try:
            orc_filename(row[0])
        except ValueError:
            continue
        result.append(tuple(row[:3]))
    return result


def download_orc_file(identifier, directory):
    # Keep the shared converter's precision and endpoint behavior.
    from .polarmanager import json_to_polars

    filename = orc_filename(identifier)
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    with requests.get(
        ORC_ROOT + f"/data/{identifier}.json", timeout=(10, 30)
    ) as response:
        response.raise_for_status()
        data = response.json()
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        path = Path(temporary) / filename
        json_to_polars(data["vpp"], str(path))
        installed = install_polar(path, root, filename)
    metadata = {
        "sail": data.get("sailnumber", identifier),
        "name": data.get("name", identifier.split("/")[1]),
        "type": data.get("boat", {}).get("type", ""),
    }
    return installed, metadata
