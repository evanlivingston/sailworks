"""Seed-free synthetic routing fixtures and their content-addressed manifest.

The JSON/polar/obstacle files in ``test/fixtures/native_routing`` are the
frozen source of truth.  Tests build GDAL GeoTIFF sampling inputs from the
canonical forecast JSON so no platform-specific raster bytes are frozen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

FIXTURE_ID = "native-routing-synthetic-v1"
REVISION = 1
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "native_routing"
GENERATOR_PATH = "test/native_routing_fixtures.py"

LON0, LAT0, DX, DY, NX, NY = -124.0, 48.0, 0.5, 0.5, 5, 5
REFERENCE_TIME = "2026-01-01T00:00:00Z"
VALID_HOURS = (0, 3, 6)
CENTER, SUBCENTER, PDTN, PROCESS = 7, 0, 0, 96

GRID = {
    "crs": "EPSG:4326",
    "lon0": LON0,
    "lat0": LAT0,
    "dx": DX,
    "dy": DY,
    "nx": NX,
    "ny": NY,
    "lat0_semantics": "pixel center of the southwest corner cell",
    "row_order": "north_to_south",
}

FORMULAS = {
    "indices": "east i=0..4, north j=0..4, valid hour h in {0,3,6}",
    "u_ms": "6 + 0.5*i + 1.0*j + 1.5*h",
    "v_ms": "2 + 0.25*i - 0.5*j + 0.5*h",
    "pressure_pa": "101200 + 100*i + 200*j + 50*h",
    "current_east_ms": "0.20 + 0.01*i + 0.02*j + 0.01*h",
    "current_north_ms": "-0.10 + 0.01*i - 0.01*j + 0.005*h",
    "wind_from_deg": "(degrees(atan2(-u, -v)) + 360) % 360",
    "wind_speed_ms": "hypot(u, v)",
}

POLAR_TWA = (30, 60, 90, 120, 150, 180)
POLAR_TWS = (4, 8, 12, 16)
POLAR = {
    "axes": {"twa_deg": list(POLAR_TWA), "tws_kn": list(POLAR_TWS)},
    "columns": ["TWA\\TWS"] + [str(tws) for tws in POLAR_TWS],
    "formula": "boatspeed_kn = round(0.02*twa_deg + 0.1*tws_kn, 2)",
    "source": "synthetic",
    "synthetic": True,
    "units": {
        "twa_deg": "degrees true wind angle",
        "tws_kn": "knots true wind speed",
        "boatspeed_kn": "knots",
    },
}
TEMPORAL_INTERPOLATION = (
    "linear between adjacent valid hours in each sampled variable "
    "(U, V, pressure, current east/north)"
)
OBSTACLE_RING = [
    [-123.1, 48.9],
    [-122.9, 48.9],
    [-122.9, 49.1],
    [-123.1, 49.1],
    [-123.1, 48.9],
]

KNOT_MS = 1852 / 3600


def u_ms(i: int, j: int, hour: int) -> float:
    return 6.0 + 0.5 * i + 1.0 * j + 1.5 * hour


def v_ms(i: int, j: int, hour: int) -> float:
    return 2.0 + 0.25 * i - 0.5 * j + 0.5 * hour


def pressure_pa(i: int, j: int, hour: int) -> float:
    return 101200.0 + 100.0 * i + 200.0 * j + 50.0 * hour


def current_east_ms(i: int, j: int, hour: int) -> float:
    return round(0.20 + 0.01 * i + 0.02 * j + 0.01 * hour, 6)


def current_north_ms(i: int, j: int, hour: int) -> float:
    return round(-0.10 + 0.01 * i - 0.01 * j + 0.005 * hour, 6)


def polar_speed_kn(twa_deg: float, tws_kn: float) -> float:
    return round(0.02 * twa_deg + 0.1 * tws_kn, 2)


def grid_values(value, hour):
    """Evaluate ``value(i, j, hour)`` in JSON north-to-south row order."""
    rows = []
    for row in range(NY):
        north_index = NY - 1 - row
        rows.append([round(value(i, north_index, hour), 6) for i in range(NX)])
    return rows


def geotransform():
    return [LON0 - DX / 2, DX, 0.0, LAT0 + (NY - 1) * DY + DY / 2, 0.0, -DY]


def expected_observables():
    def wind(i, j, hour):
        u, v = u_ms(i, j, hour), v_ms(i, j, hour)
        return {
            "u_ms": u,
            "v_ms": v,
            "speed_ms": math.hypot(u, v),
            "speed_knots": math.hypot(u, v) / KNOT_MS,
            "from_deg": (math.degrees(math.atan2(-u, -v)) + 360) % 360,
        }

    return {
        "wind_sw_h0": wind(0, 0, 0),
        "wind_sw_h1_blended": wind(0, 0, 1),
        "wind_ne_h0": wind(NX - 1, NY - 1, 0),
        "pressure_sw_h0_hpa": pressure_pa(0, 0, 0) / 100,
        "pressure_ne_h0_hpa": pressure_pa(NX - 1, NY - 1, 0) / 100,
        "current_sw_h0": {
            "east_ms": current_east_ms(0, 0, 0),
            "north_ms": current_north_ms(0, 0, 0),
        },
        "current_ne_h0": {
            "east_ms": current_east_ms(NX - 1, NY - 1, 0),
            "north_ms": current_north_ms(NX - 1, NY - 1, 0),
        },
        "current_ne_h3": {
            "east_ms": current_east_ms(NX - 1, NY - 1, 3),
            "north_ms": current_north_ms(NX - 1, NY - 1, 3),
        },
        "current_ne_h6": {
            "east_ms": current_east_ms(NX - 1, NY - 1, 6),
            "north_ms": current_north_ms(NX - 1, NY - 1, 6),
        },
        "polar_60_8_kn": polar_speed_kn(60, 8),
        "polar_90_12_kn": polar_speed_kn(90, 12),
        "polar_180_16_kn": polar_speed_kn(180, 16),
        "obstacle_bbox": [-123.1, 48.9, -122.9, 49.1],
        "obstacle_center": [-123.0, 49.0],
    }


def contract():
    return {
        "fixture_id": FIXTURE_ID,
        "revision": REVISION,
        "synthetic": True,
        "canonical_encoding": {
            "json": "UTF-8, sorted keys, 2-space indent, trailing newline",
            "csv": "UTF-8, LF, tab separated, trailing newline",
        },
        "grid": GRID,
        "orientation": {
            "geotransform": geotransform(),
            "pixel_center": (
                "lon = lon0 + col*dx; lat = lat0 + north_index*dy; "
                "GeoTransform origin is the northwest corner"
            ),
            "note": "no negative-latitude or flipped convention is implied",
        },
        "reference_time_utc": REFERENCE_TIME,
        "temporal_interpolation": TEMPORAL_INTERPOLATION,
        "valid_hours": list(VALID_HOURS),
        "source_metadata": {
            "center": CENTER,
            "subcenter": SUBCENTER,
            "pdt": PDTN,
            "process": PROCESS,
            "wind_level": "10-HTGL",
            "pressure_level": "0-MSL",
            "wind_unit": "m/s",
            "pressure_unit": "Pa",
            "pressure_established_sampler_unit": "hPa (WeatherSampler scale 0.01)",
            "wind_frame": "U east, V north; sampler direction is FROM",
            "current_frame": "east, north toward; m/s",
            "current_unit": "m/s",
        },
        "polar": POLAR,
        "formulas": FORMULAS,
        "expected_observables": expected_observables(),
    }


def forecast():
    bands = [
        {
            "element": "UGRD",
            "level": "10-HTGL",
            "unit": "m/s",
            "frames": [
                {"hour": hour, "values": grid_values(u_ms, hour)}
                for hour in VALID_HOURS
            ],
        },
        {
            "element": "VGRD",
            "level": "10-HTGL",
            "unit": "m/s",
            "frames": [
                {"hour": hour, "values": grid_values(v_ms, hour)}
                for hour in VALID_HOURS
            ],
        },
        {
            "element": "PRMSL",
            "level": "0-MSL",
            "unit": "Pa",
            "frames": [
                {"hour": hour, "values": grid_values(pressure_pa, hour)}
                for hour in VALID_HOURS
            ],
        },
    ]
    return {
        "fixture_id": FIXTURE_ID,
        "kind": "synthetic_forecast",
        "reference_time_utc": REFERENCE_TIME,
        "valid_hours": list(VALID_HOURS),
        "bands": bands,
    }


def current():
    return {
        "fixture_id": FIXTURE_ID,
        "kind": "synthetic_current",
        "reference_time_utc": REFERENCE_TIME,
        "valid_hours": list(VALID_HOURS),
        "bands": [
            {
                "component": "east",
                "unit": "m/s",
                "frames": [
                    {"hour": hour, "values": grid_values(current_east_ms, hour)}
                    for hour in VALID_HOURS
                ],
            },
            {
                "component": "north",
                "unit": "m/s",
                "frames": [
                    {"hour": hour, "values": grid_values(current_north_ms, hour)}
                    for hour in VALID_HOURS
                ],
            },
        ],
    }


def polar_csv():
    lines = ["TWA\\TWS\t" + "\t".join(str(tws) for tws in POLAR_TWS)]
    for twa in POLAR_TWA:
        speeds = "\t".join(f"{polar_speed_kn(twa, tws):g}" for tws in POLAR_TWS)
        lines.append(f"{twa}\t{speeds}")
    return "\n".join(lines) + "\n"


def obstacle_geojson():
    return {
        "type": "Feature",
        "properties": {
            "fixture_id": FIXTURE_ID,
            "synthetic": True,
            "name": "synthetic-islet",
            "crs": "EPSG:4326",
        },
        "geometry": {"type": "Polygon", "coordinates": [OBSTACLE_RING]},
    }


def canonical_json(value) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_bytes():
    return {
        "contract.json": canonical_json(contract()),
        "forecast.json": canonical_json(forecast()),
        "current.json": canonical_json(current()),
        "polar.csv": polar_csv().encode("utf-8"),
        "obstacle.geojson": canonical_json(obstacle_geojson()),
    }


def band_metadata(band, key):
    frames = band["frames"]
    metadata = {
        key: band[key],
        "unit": band["unit"],
        "hours": [frame["hour"] for frame in frames],
        "shapes": [[len(frame["values"]), len(frame["values"][0])] for frame in frames],
    }
    if "level" in band:
        metadata["level"] = band["level"]
    return metadata


def build_manifest(artifacts, baseline_commit, generator_source, generator_path):
    return {
        "fixture_id": FIXTURE_ID,
        "revision": REVISION,
        "identity": {
            "scheme": (
                "fixture_id+revision name the set; each artifact is bound by "
                "SHA-256 and byte size; generator SHA-256 binds the uncommitted "
                "producer; baseline_commit identifies the commit whose frozen "
                "artifacts were reused"
            ),
            "baseline_commit": baseline_commit,
            "generator": {
                "path": generator_path,
                "bytes": len(generator_source),
                "sha256": sha256_bytes(generator_source),
            },
        },
        "grid": GRID,
        "reference_time_utc": REFERENCE_TIME,
        "temporal_interpolation": TEMPORAL_INTERPOLATION,
        "valid_hours": list(VALID_HOURS),
        "source_metadata": contract()["source_metadata"],
        "bands": {
            "forecast": [
                band_metadata(band, "element") for band in forecast()["bands"]
            ],
            "current": [
                band_metadata(band, "component") for band in current()["bands"]
            ],
        },
        "artifacts": [
            {"path": path, "bytes": len(data), "sha256": sha256_bytes(data)}
            for path, data in sorted(artifacts.items())
        ],
    }


def write_fixtures(
    root, baseline_commit, generator_source, generator_path=GENERATOR_PATH
):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    artifacts = artifact_bytes()
    for name, data in artifacts.items():
        (root / name).write_bytes(data)
    manifest = build_manifest(
        artifacts, baseline_commit, generator_source, generator_path
    )
    (root / "manifest.json").write_bytes(canonical_json(manifest))
    return manifest


def existing_baseline(root):
    manifest = Path(root) / "manifest.json"
    if not manifest.exists():
        return None
    return json.loads(manifest.read_text(encoding="utf-8"))["identity"][
        "baseline_commit"
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=FIXTURE_DIR)
    parser.add_argument("--baseline-commit")
    args = parser.parse_args(argv)
    baseline = args.baseline_commit or existing_baseline(args.root)
    if baseline is None:
        parser.error(
            "--baseline-commit is required when creating a new fixture directory"
        )
    source = Path(__file__).read_bytes()
    write_fixtures(args.root, baseline, source)
    print(f"wrote {FIXTURE_ID} revision {REVISION} to {args.root}")


if __name__ == "__main__":
    main()
