# -*- coding: utf-8 -*-
"""Race review data imported from YB Tracking and similar trackers."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile

try:
    import requests
except Exception:  # pragma: no cover
    requests = None
from dataclasses import dataclass, field
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse


def _parse_time(value: Any) -> Optional[datetime]:
    """Parse a timestamp from a number, ISO string, or datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        if abs(number) > 1e12:
            number /= 1000.0
        return datetime.fromtimestamp(number, tz=timezone.utc).replace(tzinfo=None)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _parse_time(int(text))
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    parsed = None
            if parsed is None:
                return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def _as_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _normalize_point(value: Any) -> Optional[tuple[float, float]]:
    if isinstance(value, dict):
        lat = _as_float(value.get("lat", value.get("latitude")))
        lon = _as_float(value.get("lon", value.get("longitude")))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        lat = _as_float(value[0])
        lon = _as_float(value[1])
    else:
        return None
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return (lat, lon)


@dataclass
class RacePosition:
    timestamp: datetime
    latitude: float
    longitude: float
    speed: Optional[float] = None
    course: Optional[float] = None
    distance_to_finish: Optional[float] = None

    @property
    def epoch(self) -> int:
        # ``timestamp`` is stored as a naive UTC datetime; interpret it as UTC
        # rather than the process local time zone.
        return int(self.timestamp.replace(tzinfo=timezone.utc).timestamp())

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "speed": self.speed,
            "course": self.course,
            "distance_to_finish": self.distance_to_finish,
        }


@dataclass
class RaceBoat:
    boat_id: Any
    name: str = ""
    sail: str = ""
    model: str = ""
    status: str = ""
    positions: list[RacePosition] = field(default_factory=list)

    def __post_init__(self):
        # Keep the caller's list (and its original report order) untouched.
        self.positions = list(self.positions or [])
        self._position_sorted_times = []
        self._position_sorted_indices = []
        self._indexed_positions = self.positions
        self._indexed_length = len(self.positions)
        self._indexed_times = [p.timestamp for p in self.positions]
        self._rebuild_position_index()

    def _rebuild_position_index(self):
        positions = self.positions
        order = sorted(range(len(positions)), key=lambda i: positions[i].timestamp)
        self._position_sorted_times = [positions[i].timestamp for i in order]
        prefix_max_original_index = []
        current = -1
        for original_index in order:
            if original_index > current:
                current = original_index
            prefix_max_original_index.append(current)
        self._position_sorted_indices = prefix_max_original_index
        self._indexed_positions = positions
        self._indexed_length = len(positions)
        self._indexed_times = [p.timestamp for p in positions]

    # The index is rebuilt when the positions list identity, length, or
    # timestamps change. In-place replacement that preserves both the
    # length and every timestamp is not detected; callers needing that
    # should assign a new positions list.
    def _ensure_position_index(self):
        positions = self.positions
        if (
            self._indexed_positions is not positions
            or len(positions) != self._indexed_length
            or [p.timestamp for p in positions] != self._indexed_times
        ):
            self._rebuild_position_index()

    @property
    def first_time(self) -> Optional[datetime]:
        return self.positions[0].timestamp if self.positions else None

    @property
    def last_time(self) -> Optional[datetime]:
        return self.positions[-1].timestamp if self.positions else None

    def position_at(self, when: datetime) -> Optional[RacePosition]:
        """Return the last reported position at or before ``when``.

        Uses a cached, sorted timestamp index so scrubbing a long race does
        not scan every report. The returned position is the last one whose
        timestamp is ``<= when`` (or ``None`` when ``when`` precedes the
        first report).
        """
        if not self.positions:
            return None
        self._ensure_position_index()
        times = self._position_sorted_times
        order = self._position_sorted_indices
        index = bisect_right(times, when) - 1
        if index < 0:
            return None
        original_index = order[index]
        if original_index < 0 or original_index >= len(self.positions):
            return None
        return self.positions[original_index]

    def position_index_at(self, when: datetime) -> int:
        """Return the index of the last position with timestamp <= ``when``."""
        if not self.positions:
            return -1
        self._ensure_position_index()
        times = self._position_sorted_times
        order = self._position_sorted_indices
        index = bisect_right(times, when) - 1
        if index < 0:
            return -1
        return order[index]

    def track_points(
        self, when: Optional[datetime] = None
    ) -> list[tuple[float, float]]:
        positions = (
            self.positions
            if when is None
            else [p for p in self.positions if p.timestamp <= when]
        )
        return [(p.latitude, p.longitude) for p in positions]

    def to_dict(self) -> dict:
        return {
            "id": self.boat_id,
            "name": self.name,
            "sail": self.sail,
            "model": self.model,
            "status": self.status,
            "positions": [p.to_dict() for p in self.positions],
        }


@dataclass
class RaceCourse:
    name: str = ""
    points: list[tuple[float, float]] = field(default_factory=list)
    closed: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "points": list(self.points), "closed": self.closed}


@dataclass
class Race:
    race_id: str
    name: str
    source: str = "yb"
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    course: list[RaceCourse] = field(default_factory=list)
    boats: list[RaceBoat] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.start_time is None or self.end_time is None:
            times = [p.timestamp for boat in self.boats for p in boat.positions]
            if times:
                if self.start_time is None:
                    self.start_time = min(times)
                if self.end_time is None:
                    self.end_time = max(times)

    @property
    def duration(self):
        if self.start_time is None or self.end_time is None:
            return None
        return self.end_time - self.start_time

    @property
    def duration_hours(self) -> float:
        duration = self.duration
        if duration is None:
            return 0.0
        return duration.total_seconds() / 3600.0

    def time_range(self) -> tuple[Optional[datetime], Optional[datetime]]:
        return (self.start_time, self.end_time)

    def bounds(self) -> Optional[tuple[float, float, float, float]]:
        """Return ``(south, west, north, east)`` in degrees."""
        points: list[tuple[float, float]] = []
        for boat in self.boats:
            points.extend((p.latitude, p.longitude) for p in boat.positions)
        for course in self.course:
            points.extend(course.points)
        if not points:
            return None
        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        return (min(lats), min(lons), max(lats), max(lons))

    def boat(self, boat_id: Any) -> Optional[RaceBoat]:
        for item in self.boats:
            if item.boat_id == boat_id:
                return item
        return None

    def positions_at(self, when: datetime) -> dict[Any, RacePosition]:
        return {
            boat.boat_id: position
            for boat in self.boats
            for position in [boat.position_at(when)]
            if position is not None
        }

    def weather_request(
        self,
        models: Optional[Iterable[str]] = None,
        fields: tuple[str, ...] = ("wind", "pressure"),
        padding_degrees: float = 1.0,
    ) -> dict:
        region = self.bounds()
        if region is not None:
            south, west, north, east = region
            south = max(-90.0, south - padding_degrees)
            north = min(90.0, north + padding_degrees)
            west = max(-180.0, west - padding_degrees)
            east = min(180.0, east + padding_degrees)
            region = (south, west, north, east)
        return {
            "race_id": self.race_id,
            "race_name": self.name,
            "models": list(models or ["GFS"]),
            "region": region,
            "start": self.start_time,
            "end": self.end_time,
            "duration_hours": self.duration_hours,
            "fields": tuple(fields),
        }

    def to_dict(self) -> dict:
        return {
            "race_id": self.race_id,
            "name": self.name,
            "source": self.source,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "course": [course.to_dict() for course in self.course],
            "boats": [boat.to_dict() for boat in self.boats],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Race":
        return parse_race(data)


def _load_json(data: Any) -> Any:
    if isinstance(data, (str, Path)):
        text = str(data)
        # JSON text is parsed directly; otherwise treat the value as a path.
        stripped = text.lstrip()
        if stripped.startswith(("{", "[")):
            return json.loads(text)
        path = Path(data)
        if path.exists():
            text = path.read_text(encoding="utf-8")
        return json.loads(text)
    return data


def _first(mapping: dict, *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _unwrap_race(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValueError("race data must be a JSON object")
    for key in ("race", "Race", "race_setup", "RaceSetup", "data"):
        value = data.get(key)
        if isinstance(value, dict):
            merged = dict(value)
            for extra in ("positions", "tracks", "leaderboard", "boats", "teams"):
                if extra in data and extra not in merged:
                    merged[extra] = data[extra]
            return merged
    return data


def _course_from_poi(poi: Any) -> list[RaceCourse]:
    courses: list[RaceCourse] = []
    if isinstance(poi, dict):
        lines = poi.get("lines") or poi.get("polylines") or poi.get("tracks") or []
        for line in lines if isinstance(lines, list) else []:
            nodes = line.get("nodes") if isinstance(line, dict) else None
            points: list[tuple[float, float]] = []
            if isinstance(nodes, str):
                values = [part.strip() for part in nodes.split(",") if part.strip()]
                numeric = [
                    float(part) for part in values if _as_float(part) is not None
                ]
                # RaceSetup stores pairs in (latitude, longitude) order after the
                # viewer's own (longitude, latitude) swap; retain the common
                # (lat, lon) interpretation used by the rest of the app.
                for index in range(0, len(numeric) - 1, 2):
                    point = _normalize_point((numeric[index], numeric[index + 1]))
                    if point is not None:
                        points.append(point)
            elif isinstance(nodes, list):
                for node in nodes:
                    point = _normalize_point(node)
                    if point is not None:
                        points.append(point)
            if isinstance(line, dict):
                other_points = line.get("points") or line.get("coordinates")
                if isinstance(other_points, list):
                    for point in other_points:
                        normalized = _normalize_point(point)
                        if normalized is not None:
                            points.append(normalized)
            name = ""
            if isinstance(line, dict):
                name = str(line.get("name") or line.get("id") or "")
            courses.append(
                RaceCourse(
                    name=name,
                    points=points,
                    closed=(
                        bool(line.get("polygon")) if isinstance(line, dict) else False
                    ),
                )
            )
    if isinstance(poi, list):
        for item in poi:
            point = _normalize_point(item)
            if point is not None:
                courses.append(RaceCourse(points=[point]))
            elif isinstance(item, dict):
                points = []
                for key in ("points", "coordinates", "nodes"):
                    value = item.get(key)
                    if isinstance(value, list):
                        for point in value:
                            normalized = _normalize_point(point)
                            if normalized is not None:
                                points.append(normalized)
                if points:
                    courses.append(
                        RaceCourse(name=str(item.get("name", "")), points=points)
                    )
    return courses


def _course_from_value(value: Any) -> list[RaceCourse]:
    if value is None:
        return []
    if isinstance(value, dict):
        if (
            "lines" in value
            or "nodes" in value
            or "points" in value
            or "coordinates" in value
        ):
            courses = _course_from_poi(value)
            if courses:
                return courses
        points = []
        for key in ("points", "coordinates", "waypoints"):
            item = value.get(key)
            if isinstance(item, list):
                for point in item:
                    normalized = _normalize_point(point)
                    if normalized is not None:
                        points.append(normalized)
        if points:
            return [RaceCourse(name=str(value.get("name", "")), points=points)]
        return []
    if isinstance(value, list):
        points = []
        for item in value:
            if isinstance(item, dict):
                normalized = _normalize_point(item)
                if normalized is not None:
                    points.append(normalized)
            else:
                normalized = _normalize_point(item)
                if normalized is not None:
                    points.append(normalized)
        if points and all(isinstance(item, (list, tuple, dict)) for item in value):
            # A plain list of points is a single course.
            if all(_normalize_point(item) is not None for item in value):
                return [RaceCourse(points=points)]
        courses = _course_from_poi(value)
        return courses if courses else []
    return []


def _boats_from_data(data: dict) -> list[RaceBoat]:
    teams = _first(data, "teams", "boats", "competitors", "entries", "participants")
    if not isinstance(teams, list):
        teams = []
    positions_list = _first(data, "positions", "tracks", "routes")
    positions_by_id: dict[Any, list] = {}
    if isinstance(positions_list, list):
        for entry in positions_list:
            if not isinstance(entry, dict):
                continue
            boat_id = _first(entry, "id", "boat_id", "team_id", "teamId", "boatId")
            moments = _first(entry, "moments", "positions", "points", "track")
            if isinstance(moments, list):
                positions_by_id[boat_id] = moments
                # Some exports store positions directly under the team id only.
            if boat_id is None and "id" in entry:
                positions_by_id[entry["id"]] = moments

    boats: list[RaceBoat] = []
    for team in teams:
        if not isinstance(team, dict):
            continue
        boat_id = _first(team, "id", "boat_id", "team_id", "teamId", "boatId")
        moments = positions_by_id.get(boat_id)
        if moments is None:
            # Fall back to positions embedded in the team record.
            moments = _first(team, "positions", "moments", "track", "points")
        race_positions: list[RacePosition] = []
        if isinstance(moments, list):
            for moment in moments:
                if not isinstance(moment, dict):
                    continue
                timestamp = _parse_time(
                    _first(
                        moment,
                        "gpsAt",
                        "gps_at",
                        "gpsTime",
                        "gps_time",
                        "at",
                        "timestamp",
                        "time",
                        "date",
                        "datetime",
                    )
                )
                point = _normalize_point(moment)
                if point is None:
                    point = _normalize_point((moment.get("lat"), moment.get("lon")))
                if timestamp is None or point is None:
                    continue
                race_positions.append(
                    RacePosition(
                        timestamp=timestamp,
                        latitude=point[0],
                        longitude=point[1],
                        speed=_as_float(
                            _first(
                                moment,
                                "speed",
                                "sog",
                                "sogKnots",
                                "sog_knots",
                                "speedKnots",
                                "speed_knots",
                            )
                        ),
                        course=_as_float(
                            _first(moment, "course", "cog", "heading", "cogDegrees")
                        ),
                        distance_to_finish=_as_float(
                            _first(
                                moment, "dtf", "distance_to_finish", "distanceToFinish"
                            )
                        ),
                    )
                )
        boats.append(
            RaceBoat(
                boat_id=boat_id,
                name=str(_first(team, "name", "boat_name", "team_name") or ""),
                sail=str(_first(team, "sail", "sail_number", "sailNumber") or ""),
                model=str(_first(team, "model", "boat_model", "boatModel") or ""),
                status=str(_first(team, "status") or ""),
                positions=race_positions,
            )
        )

    # Support exports that list positions but no teams.
    if not boats and isinstance(positions_list, list):
        for entry in positions_list:
            if not isinstance(entry, dict) or "id" not in entry:
                continue
            moments = _first(entry, "moments", "positions", "points", "track")
            if not isinstance(moments, list):
                continue
            race_positions = []
            for moment in moments:
                if not isinstance(moment, dict):
                    continue
                timestamp = _parse_time(
                    _first(
                        moment,
                        "gpsAt",
                        "gps_at",
                        "gpsTime",
                        "gps_time",
                        "at",
                        "timestamp",
                        "time",
                        "date",
                        "datetime",
                    )
                )
                point = _normalize_point(moment)
                if timestamp is None or point is None:
                    continue
                race_positions.append(
                    RacePosition(
                        timestamp=timestamp, latitude=point[0], longitude=point[1]
                    )
                )
            boats.append(
                RaceBoat(
                    boat_id=entry["id"],
                    name=str(entry.get("name", "")),
                    positions=race_positions,
                )
            )

    return boats


def parse_race(
    data: Any, race_id: Optional[str] = None, name: Optional[str] = None
) -> Race:
    payload = _load_json(data)
    payload = _unwrap_race(payload)
    if not isinstance(payload, dict):
        raise ValueError("race data must be a JSON object")

    resolved_id = race_id or _first(
        payload, "race_id", "raceId", "id", "race", "race_code", "raceCode", "slug"
    )
    if resolved_id is None:
        resolved_id = "race"
    resolved_name = name or _first(
        payload, "name", "race_name", "raceName", "title", "event"
    )
    if resolved_name is None:
        resolved_name = str(resolved_id)

    start = _parse_time(
        _first(
            payload,
            "start",
            "start_time",
            "startTime",
            "race_start",
            "raceStart",
            "begin",
        )
    )
    end = _parse_time(
        _first(payload, "end", "end_time", "endTime", "race_end", "raceEnd", "finish")
    )

    courses: list[RaceCourse] = []
    for key in (
        "course",
        "courses",
        "poi",
        "pois",
        "geometries",
        "geometry",
        "waypoints",
        "route",
    ):
        courses.extend(_course_from_value(payload.get(key)))
    # RaceSetup sometimes nests course definitions under "poi".
    if "poi" in payload:
        courses.extend(_course_from_poi(payload.get("poi")))
    # De-duplicate identical courses.
    unique: list[RaceCourse] = []
    for course in courses:
        if not course.points:
            continue
        if not any(existing.points == course.points for existing in unique):
            unique.append(course)
    courses = unique

    boats = _boats_from_data(payload)

    race = Race(
        race_id=str(resolved_id),
        name=str(resolved_name),
        source=str(_first(payload, "source") or "yb"),
        start_time=start,
        end_time=end,
        course=courses,
        boats=boats,
        metadata={
            key: value
            for key, value in payload.items()
            if key
            not in {
                "teams",
                "boats",
                "competitors",
                "entries",
                "participants",
                "positions",
                "tracks",
                "routes",
                "course",
                "courses",
                "poi",
                "pois",
                "geometries",
                "geometry",
                "waypoints",
                "route",
                "start",
                "start_time",
                "startTime",
                "end",
                "end_time",
                "endTime",
                "name",
                "race_name",
                "raceName",
                "title",
                "event",
            }
        },
    )
    return race


def load_race(path: Any, **kwargs) -> Race:
    return parse_race(path, **kwargs)


# Backwards/alternate spellings for callers and tests.
RaceCollection = list
parse_yb_race = parse_race
parse_yb_tracking = parse_race
RaceBounds = tuple


def weather_download_request(race: Race, *args, **kwargs) -> dict:
    return race.weather_request(*args, **kwargs)


# ---------------------------------------------------------------------------
# YB Tracking download helpers
# ---------------------------------------------------------------------------

YB_HOST = "yb.tl"
YB_POSITIONS_PATH_TEMPLATE = "API3/Race/{race}/GetPositions"
YB_DOWNLOAD_TIMEOUT = 30
YB_MAX_RESPONSE_BYTES = 50 * 1024 * 1024

_SAFE_SLUG_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


class YBRaceError(Exception):
    """Raised when a YB race cannot be downloaded or parsed."""


RaceDownloadError = YBRaceError


def normalize_yb_race_slug(value: Any) -> Optional[str]:
    """Return a validated slug for a race code or HTTPS yb.tl URL."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "://" in text:
        parsed = urlparse(text)
        if parsed.scheme.lower() != "https":
            return None
        if not parsed.hostname or parsed.hostname.lower() != YB_HOST:
            return None
        # Reject anything after the first path segment and any query/fragment.
        if parsed.query or parsed.fragment:
            return None
        segments = [segment for segment in parsed.path.strip("/").split("/") if segment]
        if len(segments) != 1:
            return None
        slug = segments[0]
    else:
        # A bare value must itself be a single slug; do not silently accept
        # the first segment of a slash-containing value such as "a/b".
        if "/" in text:
            return None
        slug = text.strip()
    if not slug or len(slug) > 128 or any(c not in _SAFE_SLUG_CHARS for c in slug):
        return None
    return slug


normalize_yb_slug = normalize_yb_race_slug
normalize_yb_race = normalize_yb_race_slug
validate_yb_slug = normalize_yb_race_slug
validate_yb_url = normalize_yb_race_slug


def yb_positions_url(slug: str) -> str:
    slug = normalize_yb_race_slug(slug)
    if slug is None:
        raise ValueError("invalid YB race slug")
    return "https://{host}/{path}".format(
        host=YB_HOST, path=YB_POSITIONS_PATH_TEMPLATE.format(race=slug)
    )


def _safe_filename(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return (text or "race") + ".json"


def race_storage_dir(directory: Any = None) -> Path:
    if directory is not None:
        path = Path(directory)
    else:
        env = os.environ.get("GWEATHER_RACE_DATA_DIR")
        if env:
            path = Path(env)
        else:
            try:
                from gweatherrouting.core.storage import DATA_DIR

                path = Path(DATA_DIR) / "races"
            except Exception:
                path = Path.home() / ".gweatherrouting" / "races"
    path.mkdir(parents=True, exist_ok=True)
    return path


RACE_DATA_DIR = None


def _resolve_storage_dir(directory=None):
    if directory is not None:
        return race_storage_dir(directory)
    if RACE_DATA_DIR is not None:
        return race_storage_dir(RACE_DATA_DIR)
    return race_storage_dir()


def save_race(race: Race, directory: Any = None) -> Path:
    """Atomically persist one race as JSON under app data."""
    if not isinstance(race, Race):
        raise TypeError("expected Race")
    storage_dir = _resolve_storage_dir(directory)
    storage_dir.mkdir(parents=True, exist_ok=True)
    destination = storage_dir / _safe_filename(race.race_id)
    payload = race.to_dict()
    fd, stage_name = tempfile.mkstemp(dir=str(storage_dir), prefix=".race-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage_name, destination)
    except BaseException:
        try:
            if os.path.exists(stage_name):
                os.unlink(stage_name)
        except OSError:
            pass
        raise
    return destination


def save_races(races: Iterable[Race], directory: Any = None) -> list[Path]:
    """Persist multiple races. Saves are batched at the helper level so callers
    can avoid repeated per-race filesystem churn where safe."""
    return [save_race(race, directory) for race in races]


def load_race_file(path: Any) -> Race:
    return parse_race(path)


def load_saved_races(directory: Any = None) -> list[Race]:
    storage_dir = _resolve_storage_dir(directory)
    if not storage_dir.exists():
        return []
    races: list[Race] = []
    for path in sorted(storage_dir.glob("*.json")):
        try:
            races.append(parse_race(path))
        except Exception:
            continue
    return races


def race_file_path(race_or_id: Any, directory: Any = None) -> Path:
    storage_dir = _resolve_storage_dir(directory)
    if isinstance(race_or_id, Race):
        value = race_or_id.race_id
    else:
        value = race_or_id
    return storage_dir / _safe_filename(value)


class RaceStore:
    def __init__(self, root: Any = None):
        self.root = _resolve_storage_dir(root)

    @property
    def directory(self) -> Path:
        return self.root

    def path_for(self, race_or_id: Any) -> Path:
        return race_file_path(race_or_id, self.root)

    def save(self, race: Race) -> Path:
        return save_race(race, self.root)

    def load_all(self) -> list[Race]:
        return load_saved_races(self.root)

    def load(self, race_id: Any) -> Race:
        return parse_race(self.path_for(race_id))

    def save_many(self, races: Iterable[Race]) -> list[Path]:
        """Persist several races, returning their paths."""
        return [save_race(race, self.root) for race in races]

    def save_all(self, races: Iterable[Race]) -> list[Path]:
        return self.save_many(races)


RaceStorage = RaceStore
RaceRepository = RaceStore
YBRaceStore = RaceStore


def fetch_yb_positions(
    identifier: Any,
    *,
    timeout: int = YB_DOWNLOAD_TIMEOUT,
    max_bytes: int = YB_MAX_RESPONSE_BYTES,
    session=None,
) -> Any:
    """Download and return the parsed JSON payload for a YB race."""
    slug = normalize_yb_race_slug(identifier)
    if slug is None:
        raise YBRaceError("invalid YB race code or URL")
    # Request the complete position history. A bare GetPositions request returns
    # only the latest report for each boat; t=0 asks for positions from the
    # start of the race.
    url = yb_positions_url(slug) + "?t=0"
    client = session
    if client is None:
        requests_module = globals().get("requests")
        if requests_module is None:
            import requests as requests_module
        client = requests_module
    try:
        response = client.get(url, timeout=timeout, stream=True, allow_redirects=False)
    except Exception as exc:
        raise YBRaceError(f"could not download YB race: {exc}") from exc
    status_code = getattr(response, "status_code", getattr(response, "status", None))
    # Reject redirects before reading any response body; requests' raise_for_status
    # only covers 4xx/5xx, so handle 3xx explicitly.
    if status_code is not None and 300 <= status_code < 400:
        reason = getattr(response, "reason", "") or ""
        try:
            response.close()
        except Exception:
            pass
        raise YBRaceError(
            f"YB download failed: HTTP {status_code} {reason} (redirect not allowed)".strip()
        )
    if status_code is not None and status_code >= 400:
        reason = getattr(response, "reason", "") or ""
        try:
            response.close()
        except Exception:
            pass
        raise YBRaceError(f"YB download failed: HTTP {status_code} {reason}".strip())
    # requests.Response.raise_for_status compatibility.
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        try:
            raise_for_status()
        except Exception as exc:
            try:
                response.close()
            except Exception:
                pass
            raise YBRaceError(f"YB download failed: {exc}") from exc
    chunks: list[bytes] = []
    total = 0
    try:
        iterator = getattr(response, "iter_content", None)
        if callable(iterator):
            try:
                iterable = iterator(chunk_size=65536)
            except TypeError:
                iterable = iterator()
        elif hasattr(response, "content"):
            iterable = [response.content]
        elif hasattr(response, "text"):
            iterable = [response.text.encode("utf-8")]
        else:
            iterable = [response]
        for chunk in iterable:
            if not chunk:
                continue
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            total += len(chunk)
            if total > max_bytes:
                raise YBRaceError("YB response exceeds maximum allowed size")
            chunks.append(chunk)
    except YBRaceError:
        try:
            response.close()
        except Exception:
            pass
        raise
    except Exception as exc:
        try:
            response.close()
        except Exception:
            pass
        raise YBRaceError(f"YB download failed: {exc}") from exc
    finally:
        try:
            response.close()
        except Exception:
            pass
    raw = b"".join(chunks)
    if not raw:
        raise YBRaceError("YB download returned an empty response")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise YBRaceError(f"YB response is not valid JSON: {exc}") from exc
    return payload


fetch_yb_race = fetch_yb_positions
download_yb_positions = fetch_yb_positions
get_yb_positions = fetch_yb_positions


def download_yb_race(
    identifier: Any,
    *,
    timeout: int = YB_DOWNLOAD_TIMEOUT,
    max_bytes: int = YB_MAX_RESPONSE_BYTES,
    session=None,
    directory: Any = None,
    save: bool = True,
) -> Race:
    """Download, parse, optionally persist, and return a YB race."""
    payload = fetch_yb_positions(
        identifier, timeout=timeout, max_bytes=max_bytes, session=session
    )
    slug = normalize_yb_race_slug(identifier)
    race = parse_race(payload, race_id=slug)
    if save:
        save_race(race, directory)
    return race


download_yb = download_yb_race
# Backwards-friendly aliases.
fetch_yb_race_data = fetch_yb_positions
