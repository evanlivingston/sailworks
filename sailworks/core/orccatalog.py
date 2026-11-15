"""Full ORC catalog listings, cached metadata and lazily read boat identity."""

import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from .polarfiles import ORC_ROOT, orc_catalog, orc_filename
from .storage import DATA_DIR

ORC_TREE_URL = (
    "https://api.github.com/repos/jieter/orc-data/git/trees/master?recursive=1"
)
ORC_CATALOG_FILE = Path(DATA_DIR) / "orc-polars.json"
TREE_ENTRY = re.compile(r"^site/data/([^/]+)/([^/]+)\.json$")


def orc_tree_catalog():
    """Identifiers for every boat JSON in the upstream repository tree."""
    with requests.get(ORC_TREE_URL, timeout=(10, 30)) as response:
        response.raise_for_status()
        payload = response.json()
    tree = payload.get("tree") if isinstance(payload, dict) else None
    if not isinstance(tree, list):
        raise ValueError("Invalid ORC catalog")
    identifiers = set()
    for entry in tree:
        path = entry.get("path") if isinstance(entry, dict) else None
        match = TREE_ENTRY.match(path) if isinstance(path, str) else None
        if match is None:
            continue
        identifier = f"{match.group(1)}/{match.group(2)}"
        try:
            orc_filename(identifier)
        except ValueError:
            continue
        identifiers.add(identifier)
    return sorted(identifiers)


def orc_catalog_merged():
    """Tree identifiers with boat name/type taken from the published index."""
    names = {row[0]: (row[1], row[2]) for row in orc_catalog()}
    identifiers = set(names) | set(orc_tree_catalog())
    return [
        (identifier, *names.get(identifier, ("", "")))
        for identifier in sorted(identifiers)
    ]


def load_orc_catalog():
    try:
        payload = json.loads(ORC_CATALOG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        if not all(isinstance(value, str) for value in row[:3]):
            continue
        try:
            orc_filename(row[0])
        except ValueError:
            continue
        result.append(tuple(row[:3]))
    return result


def save_orc_catalog(rows, updated=None):
    updated = updated or datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {"updated": updated, "rows": [list(row) for row in rows]}
    path = Path(ORC_CATALOG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, suffix=".part", delete=False, encoding="utf-8"
    ) as out:
        temporary = Path(out.name)
        json.dump(payload, out, indent=4, sort_keys=True)
        out.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return updated


def orc_catalog_updated():
    try:
        payload = json.loads(ORC_CATALOG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    updated = payload.get("updated") if isinstance(payload, dict) else None
    return updated if isinstance(updated, str) else ""


def orc_boat_identity(identifier):
    """(sail number, name, type) for one boat, read from its upstream JSON."""
    orc_filename(identifier)
    with requests.get(
        ORC_ROOT + f"/data/{identifier}.json", timeout=(10, 30)
    ) as response:
        response.raise_for_status()
        data = response.json()
    return (
        data.get("sailnumber", identifier),
        data.get("name", identifier.split("/")[1]),
        data.get("boat", {}).get("type", ""),
    )
