"""Offline place search for ports, towns and loaded chart features."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import gzip
import json
import math
from pathlib import Path
import re
from threading import Lock
import unicodedata

from sailworks.common import resource_path

DATA_VERSION = 1
DEFAULT_ZOOM = {"port": 14.0, "city": 10.0, "harbour": 15.0, "chart": 13.0}
KIND_LABELS = {
    "port": "Port",
    "city": "City",
    "harbour": "Harbour",
    "chart": "Chart feature",
}
HARBOUR_LAYERS = {
    "PRTARE",
    "HRBARE",
    "LOCALE",
    "TOWNS",
    "BERTHS",
    "ACHARE",
    "ANCBRT",
    "MORFAC",
}
CITY_LAYERS = {"BUAARE"}
COUNTRY_ALIASES = {
    "united states of america": "united states",
    "usa": "united states",
    "russian federation": "russia",
    "republic of korea": "south korea",
    "korea, south": "south korea",
    "korea, north": "north korea",
    "viet nam": "vietnam",
    "czech republic": "czechia",
    "great britain": "united kingdom",
    "turkiye": "turkey",
    "congo. dem. rep. of": "democratic republic of the congo",
    "democratic republic of the congo": "democratic republic of the congo",
    "united republic of tanzania": "tanzania",
    "plurinational state of bolivia": "bolivia",
    "venezuela, bolivarian republic of": "venezuela",
    "islamic republic of iran": "iran",
    "syrian arab republic": "syria",
    "lao people's democratic republic": "laos",
}
LAYER_DETAILS = {
    "PRTARE": "Port area",
    "HRBARE": "Harbour area",
    "LOCALE": "Locale",
    "TOWNS": "Town",
    "BERTHS": "Berth",
    "ACHARE": "Anchorage",
    "ANCBRT": "Anchorage berth",
    "MORFAC": "Mooring",
    "BUAARE": "Built-up area",
    "SEAARE": "Sea area",
    "LNDRGN": "Land region",
    "RIVERS": "River",
    "LAKARE": "Lake",
    "RESARE": "Restricted area",
    "BRIDGE": "Bridge",
    "LNDMRK": "Landmark",
    "BCNLAT": "Beacon",
    "BCNSPP": "Beacon",
    "BOYLAT": "Buoy",
    "BOYSPP": "Buoy",
    "RTPBCN": "RACON",
    "LIGHTS": "Light",
    "FOGSIG": "Fog signal",
    "UWTROC": "Rock",
    "PYLONS": "Pylon",
    "C_AGGR": "Traffic scheme",
}


def normalize(text):
    """Case/diacritic-insensitive search form with all punctuation removed."""
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = "".join(
        character for character in value if not unicodedata.combining(character)
    )
    value = value.casefold()
    return " ".join(re.findall(r"[a-z0-9]+", value))


def country_key(country):
    value = normalize(country)
    return COUNTRY_ALIASES.get(value, value)


def distance_km(first_lat, first_lon, second_lat, second_lon):
    """Great-circle distance in kilometres."""
    first_lat, second_lat = math.radians(first_lat), math.radians(second_lat)
    delta_lat = second_lat - first_lat
    delta_lon = math.radians(second_lon - first_lon)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(first_lat) * math.cos(second_lat) * math.sin(delta_lon / 2) ** 2
    )
    return 6371.0088 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


@dataclass(frozen=True)
class Place:
    name: str
    kind: str
    latitude: float
    longitude: float
    country: str = ""
    region: str = ""
    source: str = ""
    population: int = 0
    zoom: float = 0.0
    detail: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def kind_label(self):
        if self.kind == "chart" and self.detail:
            return self.detail
        label = KIND_LABELS.get(self.kind, self.kind.title() or "Place")
        if self.kind in ("port", "harbour") and self.detail:
            return f"{label} ({self.detail})"
        return label

    @property
    def default_zoom(self):
        return self.zoom or DEFAULT_ZOOM.get(self.kind, 12.0)

    @property
    def label(self):
        parts = [self.kind_label]
        if self.region and self.region != self.country:
            parts.append(self.region)
        if self.country:
            parts.append(self.country)
        return f"{self.name} — {', '.join(parts)}"

    @property
    def rank(self):
        # Cities carry population; ports get a small tie-break so a named
        # harbour wins over an equally-sized inland town.
        return max(0, int(self.population)) + (1000 if self.kind == "port" else 0)

    def to_record(self):
        record = {
            "name": self.name,
            "kind": self.kind,
            "lat": round(float(self.latitude), 6),
            "lon": round(float(self.longitude), 6),
            "source": self.source,
        }
        for key, value in (
            ("country", self.country),
            ("region", self.region),
            ("population", self.population),
            ("zoom", self.zoom),
            ("detail", self.detail),
        ):
            if value:
                record[key] = value
        if self.aliases:
            record["aliases"] = list(self.aliases)
        return record

    @classmethod
    def from_record(cls, record):
        return cls(
            name=str(record.get("name", "")),
            kind=str(record.get("kind", "place")),
            latitude=float(record.get("lat", 0.0)),
            longitude=float(record.get("lon", 0.0)),
            country=str(record.get("country", "")),
            region=str(record.get("region", "")),
            source=str(record.get("source", "")),
            population=int(record.get("population", 0) or 0),
            zoom=float(record.get("zoom", 0.0) or 0.0),
            detail=str(record.get("detail", "")),
            aliases=tuple(str(alias) for alias in record.get("aliases", ())),
        )


def _tokens(place):
    values = [place.name, *place.aliases]
    return frozenset(token for value in values for token in normalize(value).split())


def _merge(existing, incoming):
    """Merge a duplicate place, preferring the established city/port record."""
    kind = existing.kind
    if existing.kind in ("port", "city") and incoming.kind in ("port", "city"):
        kind = "port" if "port" in (existing.kind, incoming.kind) else existing.kind
    elif existing.kind == "chart" and incoming.kind != "chart":
        kind = incoming.kind
    aliases = list(existing.aliases)
    for alias in incoming.aliases:
        if alias not in aliases:
            aliases.append(alias)
    if normalize(incoming.name) != normalize(existing.name):
        aliases.append(incoming.name)
    sources = list(
        dict.fromkeys(source for source in (existing.source, incoming.source) if source)
    )
    return replace(
        existing,
        kind=kind,
        country=existing.country or incoming.country,
        region=existing.region or incoming.region,
        source=" + ".join(sources),
        population=max(existing.population, incoming.population),
        zoom=existing.zoom or incoming.zoom,
        detail=existing.detail or incoming.detail,
        aliases=tuple(aliases),
    )


class PlaceIndex:
    """In-memory prefix index; small enough to extend with loaded chart names.

    Ports and towns come from the bundled data file. Chart features are added
    in the background when the application's vector charts are already open;
    no query touches the network.
    """

    def __init__(self, places=()):
        self._lock = Lock()
        self._snapshot = ()
        self.add_places(places)

    @classmethod
    def from_path(cls, path):
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError, gzip.BadGzipFile):
            return cls()
        return cls(Place.from_record(record) for record in payload.get("places", ()))

    @classmethod
    def from_default(cls):
        path = resource_path("sailworks", "data/places.json.gz")
        return cls.from_path(path)

    def __len__(self):
        return len(self._snapshot)

    def places(self):
        return tuple(place for place, _name, _tokens in self._snapshot)

    def add_places(self, places, merge_radius_km=0.0):
        """Append places, optionally replacing a nearby same-name record."""
        incoming = tuple(places)
        if not incoming:
            return
        with self._lock:
            rows = [place for place, _name, _tokens in self._snapshot]
            names = [normalize(place.name) for place in rows]
            token_sets = [_tokens(place) for place in rows]
            by_name = {}
            for index, name in enumerate(names):
                by_name.setdefault(name, []).append(index)
            changed = False
            for place in incoming:
                if not place.name.strip():
                    continue
                name = normalize(place.name)
                target = None
                if merge_radius_km > 0:
                    for index in by_name.get(name, ()):
                        candidate = rows[index]
                        separation = distance_km(
                            candidate.latitude,
                            candidate.longitude,
                            place.latitude,
                            place.longitude,
                        )
                        same_country = (
                            not candidate.country
                            or not place.country
                            or country_key(candidate.country)
                            == country_key(place.country)
                        )
                        if separation <= merge_radius_km and (
                            same_country or separation <= 5.0
                        ):
                            target = index
                            break
                if target is None:
                    index = len(rows)
                    rows.append(place)
                    names.append(name)
                    token_sets.append(_tokens(place))
                    by_name.setdefault(name, []).append(index)
                else:
                    rows[target] = _merge(rows[target], place)
                    names[target] = normalize(rows[target].name)
                    token_sets[target] = _tokens(rows[target])
                changed = True
            if not changed:
                return
            self._snapshot = tuple(zip(rows, names, token_sets))

    def search(self, text, limit=20, near=None):
        query = normalize(text)
        if not query:
            return []
        terms = tuple(query.split())
        if not terms:
            return []
        near_lat = near_lon = None
        if near is not None:
            try:
                near_lat, near_lon = float(near[0]), float(near[1])
            except (IndexError, TypeError, ValueError):
                near_lat = near_lon = None
        scored = []
        last = len(terms) - 1
        for place, name, tokens in self._snapshot:
            if not all(
                (
                    any(token.startswith(term) for token in tokens)
                    if index == last
                    else term in tokens
                )
                for index, term in enumerate(terms)
            ):
                continue
            if name == query:
                match = 0
            elif name.startswith(query):
                match = 1
            elif query in name:
                match = 2
            else:
                match = 3
            proximity = 0.0
            if near_lat is not None:
                proximity = distance_km(
                    near_lat, near_lon, place.latitude, place.longitude
                )
            scored.append((match, proximity, -place.rank, name, place))
        scored.sort(key=lambda entry: entry[:4])
        return [entry[4] for entry in scored[: max(1, int(limit))]]


def chart_places(path, cancelled=lambda: False):
    """Extract named features from one S57/ENC dataset for the search index."""
    try:
        from osgeo import ogr
    except ImportError:  # pragma: no cover - Qt-only builds without GDAL
        return ()
    dataset = ogr.Open(str(path))
    if dataset is None:
        return ()
    places = []
    try:
        for layer_index in range(dataset.GetLayerCount()):
            if cancelled():
                return ()
            layer = dataset.GetLayerByIndex(layer_index)
            definition = layer.GetLayerDefn()
            name_index = definition.GetFieldIndex("OBJNAM")
            alternate_index = definition.GetFieldIndex("NOBJNM")
            if name_index < 0 and alternate_index < 0:
                continue
            layer_name = layer.GetName()
            for feature in layer:
                if cancelled():
                    return ()
                name = (
                    feature.GetFieldAsString(name_index) if name_index >= 0 else ""
                ).strip()
                alternate = (
                    feature.GetFieldAsString(alternate_index)
                    if alternate_index >= 0
                    else ""
                ).strip()
                if not name and alternate:
                    name, alternate = alternate, ""
                if not name:
                    continue
                geometry = feature.GetGeometryRef()
                if geometry is None:
                    continue
                west, east, south, north = geometry.GetEnvelope()
                latitude = (south + north) / 2.0
                longitude = (west + east) / 2.0
                if not (-90 <= latitude <= 90):
                    continue
                if layer_name in HARBOUR_LAYERS:
                    kind = "harbour"
                elif layer_name in CITY_LAYERS:
                    kind = "city"
                else:
                    kind = "chart"
                places.append(
                    Place(
                        name=name,
                        kind=kind,
                        latitude=latitude,
                        longitude=longitude,
                        source="ENC",
                        detail=LAYER_DETAILS.get(layer_name, layer_name.title()),
                        aliases=(alternate,) if alternate else (),
                    )
                )
    finally:
        dataset = None
    return tuple(places)


def index_chart_files(paths, progress=None, cancelled=lambda: False):
    """Read every chart in a worker and return searchable places."""
    paths = tuple(paths)
    places = []
    for index, path in enumerate(paths, start=1):
        if cancelled():
            break
        try:
            places.extend(chart_places(path, cancelled))
        except Exception:
            continue
        if progress is not None:
            progress(index * 100 / max(1, len(paths)))
    return tuple(places)
