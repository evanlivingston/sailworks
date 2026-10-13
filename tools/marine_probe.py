#!/usr/bin/env python3
"""Bounded, replayable CHS/CIOPS research probe; never used by the router.

Run with the application's Python environment for GDAL/NumPy support.
Raw provider responses stay in the user-selected output directory.
"""

import argparse
import hashlib
import json
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

CHS = "https://api-sine.dfo-mpo.gc.ca/api/v1/"
CIOPS = "https://dd.weather.gc.ca/today/model_ciops/"
STATIONS = ("07120", "07795", "08074", "08105", "08108", "08615")
POINTS = {
    "Juan de Fuca": (48.25, -123.5),
    "Strait of Georgia": (49.3, -123.65),
    "Seymour Narrows": (50.133333, -125.35),
    "Tofino offshore": (49.1, -126.0),
}
SERIES = ("wlp", "wlp-hilo", "wcsp1", "wcdp1", "wcp1-events")
DOMAINS = {"salish-sea": "500m", "west": "2km"}
MAX_BYTES = 8 * 1024 * 1024


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A UTC offset is required")
    return parsed.astimezone(timezone.utc)


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


class Cache:
    """URL-addressed responses with checksums and an explicit offline mode."""

    def __init__(self, output, offline=False):
        self.directory = Path(output) / "responses"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self.records = {}
        self.last_request = None

    def get(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.directory / key
        metadata = path.with_suffix(".json")
        if path.exists() and metadata.exists():
            record = json.loads(metadata.read_text())
            payload = path.read_bytes()
            if (
                record["url"] != url
                or hashlib.sha256(payload).hexdigest() != record["sha256"]
            ):
                raise ValueError("Cache integrity failure: " + url)
        else:
            if self.offline:
                raise ValueError("Response unavailable offline: " + url)
            # Below CHS's 30 requests/minute and 3 requests/second limits.
            if self.last_request is not None:
                time.sleep(max(0, 2.1 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            request = Request(url, headers={"User-Agent": "gweatherrouting-research/1"})
            with urlopen(request, timeout=30) as response:
                payload = response.read(MAX_BYTES + 1)
                if len(payload) > MAX_BYTES:
                    raise ValueError("Response exceeds 8 MiB limit: " + url)
                record = {
                    "url": url,
                    "retrieved_at": stamp(datetime.now(timezone.utc)),
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "content_type": response.headers.get("Content-Type"),
                    "last_modified": response.headers.get("Last-Modified"),
                    "etag": response.headers.get("ETag"),
                }
            path.write_bytes(payload)
            metadata.write_text(json.dumps(record, indent=2) + "\n")
        self.records[url] = dict(record, cache_file=path.name)
        return path

    def json(self, endpoint, **params):
        url = CHS + endpoint
        if params:
            url += "?" + urlencode(params)
        data = json.loads(self.get(url).read_text())
        if not isinstance(data, (list, dict)):
            raise ValueError("Unexpected JSON response: " + url)
        return data


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(value for key, value in attrs if key == "href")


def listing(cache, url, pattern):
    parser = Links()
    parser.feed(cache.get(url).read_text())
    # Accept only local filenames/subdirectories, never arbitrary listing URLs.
    return sorted({href for href in parser.hrefs if re.fullmatch(pattern, href)})


def summarize_series(rows):
    if not isinstance(rows, list):
        raise ValueError("Expected a list of CHS samples")
    times = [utc(row["eventDate"]) for row in rows]
    values = [row.get("value") for row in rows]
    numeric = [
        value
        for value in values
        if isinstance(value, (int, float)) and math.isfinite(value)
    ]
    return {
        "count": len(rows),
        "first": stamp(min(times)) if times else None,
        "last": stamp(max(times)) if times else None,
        "strictly_increasing": all(b > a for a, b in zip(times, times[1:])),
        "interval_seconds": sorted(
            {(b - a).total_seconds() for a, b in zip(times, times[1:])}
        ),
        "missing_or_invalid_values": len(rows) - len(numeric),
        "minimum": min(numeric) if numeric else None,
        "maximum": max(numeric) if numeric else None,
        "qc_flags": sorted({str(row.get("qcFlagCode")) for row in rows}),
        "qualifiers": sorted(
            {row["qualifier"] for row in rows if row.get("qualifier")}
        ),
    }


def audit_directions(directions, events, max_offset_seconds=450):
    """Compare bearings near extrema, excluding slack where direction is unstable.

    This checks phase consistency, not whether a bearing is geographically correct.
    """
    samples = [
        (utc(row["eventDate"]), row["value"])
        for row in directions
        if isinstance(row.get("value"), (int, float))
        and math.isfinite(row["value"])
        and 0 <= row["value"] <= 360
    ]
    comparisons = []
    for event in events:
        qualifier = event.get("qualifier")
        if qualifier not in ("EXTREMA_FLOOD", "EXTREMA_EBB"):
            continue
        instant = utc(event["eventDate"])
        nearest = min(
            samples,
            key=lambda sample: abs((sample[0] - instant).total_seconds()),
            default=None,
        )
        offset = abs((nearest[0] - instant).total_seconds()) if nearest else None
        matched = nearest is not None and offset <= max_offset_seconds
        comparisons.append(
            {
                "event_time": stamp(instant),
                "phase": qualifier,
                "status": "matched" if matched else "no_nearby_direction",
                "sample_time": stamp(nearest[0]) if matched else None,
                "offset_seconds": offset if matched else None,
                "bearing_degrees": nearest[1] if matched else None,
            }
        )
    return {
        "distinct_bearings": sorted({bearing for _, bearing in samples}),
        "invalid_direction_count": len(directions) - len(samples),
        "extrema": comparisons,
        "note": "No direction inference at slack; no automatic flood/ebb remapping",
    }


def probe_chs(cache, start, end, station_codes=STATIONS):  # noqa: C901
    stations = cache.json("stations", **{"chs-region-code": "PAC"})
    phenomena = {row["id"]: row for row in cache.json("phenomena")}
    definitions = cache.json("time-series-definitions")
    result = {"definitions": definitions, "stations": [], "errors": []}
    by_code = {row["code"]: row for row in stations}
    for code in station_codes:
        entry = {"code": code, "series": {}, "errors": []}
        result["stations"].append(entry)
        if code not in by_code:
            entry["errors"].append("Station absent from Pacific listing")
            continue
        station = by_code[code]
        entry.update(
            name=station["officialName"],
            latitude=station["latitude"],
            longitude=station["longitude"],
            operating=station.get("operating"),
            advertised_series=[row["code"] for row in station["timeSeries"]],
        )
        endpoint = "stations/" + station["id"]
        try:
            entry["metadata"] = cache.json(endpoint + "/metadata")
            entry["holdings"] = cache.json(endpoint + "/holdings")
        except (OSError, ValueError) as error:
            entry["errors"].append(str(error))
        sampled_rows = {}
        for series in station["timeSeries"]:
            if series["code"] not in SERIES:
                continue
            try:
                rows = cache.json(
                    endpoint + "/data",
                    **{
                        "time-series-code": series["code"],
                        "from": stamp(start),
                        "to": stamp(end),
                        "resolution": (
                            "ALL"
                            if series["code"].endswith(("events", "hilo"))
                            else "FIFTEEN_MINUTES"
                        ),
                    },
                )
                entry["series"][series["code"]] = dict(
                    summarize_series(rows),
                    phenomenon=phenomena.get(series.get("phenomenonId")),
                )
                sampled_rows[series["code"]] = rows
            except (OSError, ValueError, KeyError) as error:
                entry["errors"].append(series["code"] + ": " + str(error))
        if "wcdp1" in sampled_rows and "wcp1-events" in sampled_rows:
            entry["direction_audit"] = audit_directions(
                sampled_rows["wcdp1"], sampled_rows["wcp1-events"]
            )
    return result


def nearest_cell(latitudes, longitudes, latitude, longitude):
    """Return the nearest grid indices, without searching for a wet substitute."""
    import numpy as np

    latitudes, longitudes = np.asarray(latitudes), np.asarray(longitudes)
    if latitudes.ndim != 1 or longitudes.ndim != 1:
        raise ValueError("Only rectilinear coordinates are supported by this probe")
    if not np.isfinite(latitudes).all() or not np.isfinite(longitudes).all():
        raise ValueError("Invalid coordinates")
    if longitudes.min() >= 0:
        longitude %= 360
    if not (
        latitudes.min() <= latitude <= latitudes.max()
        and longitudes.min() <= longitude <= longitudes.max()
    ):
        return None
    return (
        int(np.abs(latitudes - latitude).argmin()),
        int(np.abs(longitudes - longitude).argmin()),
    )


def inspect_netcdf(path, component):  # noqa: C901
    import numpy as np
    from osgeo import gdal

    gdal.UseExceptions()
    dataset = gdal.OpenEx(str(path), gdal.OF_MULTIDIM_RASTER)
    root = dataset.GetRootGroup()
    lat = root.OpenMDArray("latitude").ReadAsArray()
    lon = root.OpenMDArray("longitude").ReadAsArray()
    clock = root.OpenMDArray("time")
    variable = root.OpenMDArray({"X": "vozocrtx", "Y": "vomecrty"}[component])
    if variable is None:
        raise ValueError("Velocity variable missing")
    values = variable.ReadAsArray()
    if values.shape != (1, len(lat), len(lon)):
        raise ValueError("Unexpected velocity dimensions")
    values = values[0]
    attributes = {a.GetName(): a.Read() for a in variable.GetAttributes()}
    valid = np.isfinite(values)
    nodata = variable.GetNoDataValueAsDouble()
    if nodata is not None:
        valid &= values != nodata
    for name, comparison in (
        ("valid_min", np.greater_equal),
        ("valid_max", np.less_equal),
    ):
        if name in attributes:
            valid &= comparison(values, attributes[name])
    if variable.GetUnit() != "m s-1":
        raise ValueError("Unexpected velocity units")
    units = clock.GetUnit()
    prefix = "seconds since "
    if not units.startswith(prefix):
        raise ValueError("Unexpected forecast time units")
    if clock.GetAttribute("calendar").Read() not in ("gregorian", "standard"):
        raise ValueError("Unsupported calendar")
    epoch = datetime.fromisoformat(units[len(prefix) :]).replace(tzinfo=timezone.utc)
    valid_time = epoch + timedelta(seconds=float(clock.ReadAsArray()[0]))
    samples = {}
    for name, (latitude, longitude) in POINTS.items():
        cell = nearest_cell(lat, lon, latitude, longitude)
        if cell is None:
            samples[name] = {"status": "outside_grid"}
        else:
            y, x = cell
            samples[name] = {
                "status": "valid_cell" if valid[y, x] else "masked_or_invalid",
                "grid_latitude": float(lat[y]),
                "grid_longitude": (float(lon[x]) + 180) % 360 - 180,
                "value_m_s": float(values[y, x]) if valid[y, x] else None,
            }
    return {
        "global_attributes": {a.GetName(): a.Read() for a in root.GetAttributes()},
        "valid_time": stamp(valid_time),
        "unit": variable.GetUnit(),
        "attributes": attributes,
        "shape": list(values.shape),
        "latitude_bounds": [float(lat.min()), float(lat.max())],
        "longitude_bounds_native": [float(lon.min()), float(lon.max())],
        "valid_cells": int(valid.sum()),
        "masked_or_invalid_cells": int((~valid).sum()),
        "samples": samples,
        "vector_reference": "Requires verification; X/Y names alone are insufficient",
    }


def probe_ciops(cache, hours):  # noqa: C901
    result = []
    for domain, resolution in DOMAINS.items():
        entry = {"domain": domain, "files": [], "errors": []}
        result.append(entry)
        base = CIOPS + domain + "/" + resolution + "/"
        try:
            cycles = listing(cache, base, r"(?:00|06|12|18)/")
            if not cycles:
                raise ValueError("No forecast cycles advertised")
            cycle = cycles[-1]
            entry["cycle_directory"] = cycle
            for hour in hours:
                directory = urljoin(base, cycle + f"{hour:03d}/")
                try:
                    files = listing(
                        cache,
                        directory,
                        r"\d{8}T\d{2}Z_MSC_CIOPS-[A-Za-z]+_SeaWaterVelocity[XY]"
                        r"_DBS-0\.5m_LatLon[\d.x]+_PT\d{3}H\.nc",
                    )
                    pairs = {}
                    for name in files:
                        component = re.search(r"Velocity([XY])", name).group(1)
                        pairs.setdefault(
                            name.replace("Velocity" + component, "Velocity"), {}
                        )[component] = name
                    complete = [
                        pair for pair in pairs.values() if set(pair) == {"X", "Y"}
                    ]
                    if len(complete) != 1:
                        raise ValueError("Expected one matching surface X/Y pair")
                    pair_times = []
                    for component, name in sorted(complete[0].items()):
                        url = urljoin(directory, name)
                        if urlparse(url).netloc != urlparse(CIOPS).netloc:
                            raise ValueError("Unexpected download host")
                        path = cache.get(url)
                        details = inspect_netcdf(path, component)
                        run = datetime.strptime(name[:11], "%Y%m%dT%H").replace(
                            tzinfo=timezone.utc
                        )
                        if details["valid_time"] != stamp(run + timedelta(hours=hour)):
                            raise ValueError(
                                "Filename and NetCDF validity time disagree"
                            )
                        pair_times.append(details["valid_time"])
                        entry["files"].append(
                            dict(details, url=url, forecast_hour=hour, run=stamp(run))
                        )
                    if len(set(pair_times)) != 1:
                        raise ValueError("X/Y forecast times differ")
                except (OSError, ValueError, RuntimeError) as error:
                    entry["errors"].append(f"hour {hour}: {error}")
        except (OSError, ValueError) as error:
            entry["errors"].append(str(error))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--date", type=date.fromisoformat, default=datetime.now(timezone.utc).date()
    )
    parser.add_argument(
        "--forecast-hours", type=int, nargs="+", default=[0, 6, 12, 18, 24]
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--direction-stations",
        nargs="+",
        metavar="CODE",
        help="Audit up to six CHS current stations and skip CIOPS downloads",
    )
    args = parser.parse_args()
    if args.direction_stations and (
        len(args.direction_stations) > 6
        or any(not re.fullmatch(r"\d{5}", code) for code in args.direction_stations)
    ):
        parser.error("Choose at most six five-digit CHS station codes")
    hours = sorted(set(args.forecast_hours))
    if len(hours) > 9 or any(hour < 0 or hour > 48 for hour in hours):
        parser.error("Choose at most nine forecast hours from 0 through 48")
    cache = Cache(args.output, args.offline)
    start = datetime.combine(args.date, datetime.min.time(), tzinfo=timezone.utc)
    report = {
        "schema_version": 1,
        "purpose": "Local data research only; not for navigation or route calculation",
        "routing_ready": False,
        "limitations": [
            "Preserve CHS provider terms and attribution alongside source provenance",
            "Verify station bearings against published tables; "
            "retain depth and vector-reference limits",
            "Nearest-cell availability does not establish passage accuracy",
            "CIOPS uses currently advertised cycles; --date selects only the CHS day",
        ],
        "chs_licence": "https://www.tides.gc.ca/en/licence-agreement",
        "ciops_licence": "https://eccc-msc.github.io/open-data/licence/readme_en/",
        "requested_start": stamp(start),
        "requested_end": stamp(start + timedelta(days=1)),
        "forecast_hours": hours,
        "errors": [],
    }
    probes = [
        (
            "chs",
            lambda: probe_chs(
                cache,
                start,
                start + timedelta(days=1),
                args.direction_stations or STATIONS,
            ),
        )
    ]
    if not args.direction_stations:
        probes.append(("ciops", lambda: probe_ciops(cache, hours)))
    report["direction_stations"] = args.direction_stations
    for name, probe in probes:
        print("Inspecting " + name + (" offline" if args.offline else ""), flush=True)
        try:
            report[name] = probe()
        except (OSError, ValueError, KeyError, ImportError, RuntimeError) as error:
            report["errors"].append(name + ": " + str(error))
    report["responses"] = sorted(cache.records.values(), key=lambda row: row["url"])

    # Nested failures remain visible and make automation fail as well.
    def errors(value):
        if isinstance(value, dict):
            return bool(value.get("errors")) or any(errors(v) for v in value.values())
        if isinstance(value, list):
            return any(errors(v) for v in value)
        return False

    report["status"] = "incomplete" if errors(report) else "complete"
    filename = "direction-report.json" if args.direction_stations else "report.json"
    destination = args.output / filename
    destination.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(str(destination) + " (" + report["status"] + ")", flush=True)
    return 1 if report["status"] == "incomplete" else 0


if __name__ == "__main__":
    raise SystemExit(main())
