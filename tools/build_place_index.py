"""Build the bundled offline place index from public-domain datasets.

    python tools/build_place_index.py --download

Inputs:
  World Port Index (NGA Pub 150) - public domain
  Natural Earth 10m populated places - public domain
"""

import argparse
import csv
import gzip
import json
from pathlib import Path
import re
import sys
import tempfile

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sailworks.core.places import Place, PlaceIndex, normalize  # noqa: E402

WPI_URL = (
    "https://data.apps.fao.org/catalog/dataset/158a8b96-6924-48a2-8ae2-"
    "d5cc1626915e/resource/64255fd8-0d18-4c7e-bfe3-73d70b4919f0/download/"
    "sea_ports_updatedpub150.csv"
)
CITIES_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_10m_populated_places.geojson"
)
DEFAULT_OUTPUT = ROOT / "sailworks/data/places.json.gz"


def _download(url, suffix):
    print(f"downloading {url}")
    response = requests.get(url, timeout=(15, 120))
    response.raise_for_status()
    temporary = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    temporary.write(response.content)
    temporary.close()
    return Path(temporary.name)


def _float(value):
    try:
        return float(str(value).strip())
    except (AttributeError, TypeError, ValueError):
        return None


def _unique(value, existing):
    value = str(value or "").strip()
    if value and normalize(value) != existing:
        return value
    return ""


def port_places(path):
    places = []
    with open(path, encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            name = (row.get("Main Port Name") or "").strip()
            latitude = _float(row.get("Latitude"))
            longitude = _float(row.get("Longitude"))
            if not name or latitude is None or longitude is None:
                continue
            normalized = normalize(name)
            aliases = []
            for value in re.split(r"[;,]", row.get("Alternate Port Name") or ""):
                alias = _unique(value, normalized)
                if alias and alias not in aliases:
                    aliases.append(alias)
            places.append(
                Place(
                    name=name,
                    kind="port",
                    latitude=latitude,
                    longitude=longitude,
                    country=(row.get("Country Code") or "").strip(),
                    region="",
                    source="WPI",
                    detail=(row.get("Harbor Size") or "").strip(),
                    aliases=tuple(aliases),
                )
            )
    return places


def city_places(path):
    with open(path, encoding="utf-8") as stream:
        payload = json.load(stream)
    places = []
    for feature in payload.get("features", ()):
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or ()
        if geometry.get("type") != "Point" or len(coordinates) < 2:
            continue
        name = (properties.get("NAME") or properties.get("NAMEASCII") or "").strip()
        latitude, longitude = _float(coordinates[1]), _float(coordinates[0])
        if not name or latitude is None or longitude is None:
            continue
        normalized = normalize(name)
        aliases = []
        for key in ("NAMEASCII", "NAMEPAR", "NAMEALT"):
            alias = _unique(properties.get(key), normalized)
            if alias and alias not in aliases:
                aliases.append(alias)
        population = properties.get("POP_MAX")
        try:
            population = max(0, int(population))
        except (TypeError, ValueError):
            population = 0
        places.append(
            Place(
                name=name,
                kind="city",
                latitude=latitude,
                longitude=longitude,
                country=(properties.get("ADM0NAME") or "").strip(),
                region=(properties.get("ADM1NAME") or "").strip(),
                source="Natural Earth",
                population=population,
                aliases=tuple(aliases),
            )
        )
    return places


def build(wpi_path, cities_path, output):
    ports = port_places(wpi_path)
    cities = city_places(cities_path)
    index = PlaceIndex()
    index.add_places(ports)
    port_count = len(index)
    index.add_places(cities, merge_radius_km=20.0)
    places = index.places()
    payload = {
        "schema": 1,
        "sources": [
            {
                "name": "World Port Index (NGA Pub 150)",
                "license": "CC BY 4.0",
                "url": WPI_URL,
            },
            {
                "name": "Natural Earth 10m populated places",
                "license": "Public domain",
                "url": CITIES_URL,
            },
        ],
        "counts": {
            "ports": port_count,
            "cities": len(cities),
            "places": len(places),
        },
        "places": [place.to_record() for place in places],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output, "wt", encoding="utf-8", compresslevel=9) as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
    print(
        f"{len(places)} places from {port_count} ports and {len(cities)} cities "
        f"-> {output} ({output.stat().st_size / 1024:.0f} KiB)"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wpi", type=Path, help="UpdatedPub150.csv")
    parser.add_argument("--cities", type=Path, help="ne_10m_populated_places.geojson")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--download",
        action="store_true",
        help="download inputs that were not supplied",
    )
    args = parser.parse_args(argv)

    wpi = args.wpi
    cities = args.cities
    if wpi is None:
        if not args.download:
            parser.error("--wpi is required unless --download is used")
        wpi = _download(WPI_URL, ".csv")
    if cities is None:
        if not args.download:
            parser.error("--cities is required unless --download is used")
        cities = _download(CITIES_URL, ".geojson")
    build(wpi, cities, args.out)


if __name__ == "__main__":
    main()
