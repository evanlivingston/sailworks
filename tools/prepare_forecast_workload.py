"""Build a covered-clock diagnostic snapshot with a bounded historical GFS run.

This is a test/diagnostic fixture builder, not part of the application.  It
downloads one GFS run for a small geographic box, copies an existing snapshot,
adds the GRIB file to the saved selection, sets the snapshot clock to a valid
instant and re-writes the manifest identity.  The new snapshot is a new
workload: record its hash with any measurement.

Network access is required.  The tool never modifies the source snapshot.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_BOUNDS = "46.5,-126.0,50.0,-120.5"
DEFAULT_STEPS = ",".join(str(hour) for hour in range(0, 49, 3))


def parse_steps(value):
    """The sorted, unique forecast hours of one download."""
    steps = sorted({int(part) for part in value.split(",") if part.strip()})
    if not steps or steps[0] < 0:
        raise ValueError("At least one non-negative forecast hour is required")
    return steps


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def snapshot_entries(root):
    """The manifest inventory for a snapshot's data directory."""
    data = Path(root) / "data"
    return [
        {
            "path": path.relative_to(data).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": digest(path),
        }
        for path in sorted(data.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]


def copy_snapshot(source, destination):
    """Copy without touching the source; clone files on APFS when possible."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"Destination already exists: {destination}")
    destination.mkdir(parents=True)
    if sys.platform == "darwin":
        subprocess.run(
            ["cp", "-cR", str(source) + "/.", str(destination)],
            check=True,
        )
    else:
        shutil.copytree(source, destination, dirs_exist_ok=True)
    return destination


def add_opened(snapshot, filename):
    """Add one GRIB file to the snapshot's saved selection, keeping order."""
    path = Path(snapshot) / "data" / "grib-manager.json"
    manager = json.loads(path.read_text())
    opened = list(manager.get("opened", []))
    if filename not in opened:
        opened.append(filename)
    manager["opened"] = opened
    path.write_text(json.dumps(manager))
    return opened


def install_grib(snapshot, grib):
    """Copy the GRIB into the snapshot and add it to the opened list."""
    target = Path(snapshot) / "data" / "grib" / Path(grib).name
    shutil.copy2(grib, target)
    return target, add_opened(snapshot, target.name)


def update_manifest(snapshot, forecast_time):
    """Rewrite the manifest inventory and set the snapshot's clock."""
    path = Path(snapshot) / "manifest.json"
    manifest = json.loads(path.read_text())
    entries = snapshot_entries(snapshot)
    manifest["files"] = entries
    manifest["content_sha256"] = hashlib.sha256(
        json.dumps(entries, sort_keys=True).encode()
    ).hexdigest()
    manifest["forecast_time"] = forecast_time
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def download_gfs(day, cycle, steps, bounds, destination):
    """Download one bounded GFS run with the application's own source."""
    from osgeo import gdal

    from sailworks.core.gribsources.noaagfs import NOAAGFSSource

    source = NOAAGFSSource()
    source.bounds = tuple(float(part) for part in bounds.split(","))
    identifier = json.dumps(
        {
            "date": day,
            "cycle": cycle,
            "hours": max(steps),
            "steps": steps,
            "fields": ["wind", "pressure"],
        }
    )
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    path = source.download(identifier, str(destination), lambda percent: None)
    dataset = gdal.Open(path)
    if dataset is None or dataset.RasterCount < 1:
        raise RuntimeError(f"Downloaded GRIB has no bands: {path}")
    bands = dataset.RasterCount
    dataset = None
    return Path(path), bands


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--date", required=True, help="run date, YYYYMMDD (UTC)")
    parser.add_argument("--cycle", required=True, help="run cycle hour, e.g. 12")
    parser.add_argument("--steps", default=DEFAULT_STEPS)
    parser.add_argument("--bounds", default=DEFAULT_BOUNDS)
    parser.add_argument(
        "--forecast-time",
        help=(
            "snapshot clock as local ISO 8601; default: the middle step's UTC "
            "instant converted to the host's local time"
        ),
    )
    parser.add_argument("--download-dir", type=Path)
    args = parser.parse_args(argv)
    steps = parse_steps(args.steps)
    run = datetime.strptime(args.date + args.cycle, "%Y%m%d%H").replace(
        tzinfo=timezone.utc
    )
    if args.forecast_time:
        forecast_time = datetime.fromisoformat(args.forecast_time)
        if forecast_time.tzinfo is not None:
            forecast_time = forecast_time.astimezone().replace(tzinfo=None)
    else:
        middle = steps[len(steps) // 2]
        forecast_time = (
            (run + timedelta(hours=middle)).astimezone().replace(tzinfo=None)
        )
    download_dir = args.download_dir or (args.output.parent / "forecast-download")
    grib, bands = download_gfs(args.date, args.cycle, steps, args.bounds, download_dir)
    snapshot = copy_snapshot(args.snapshot, args.output)
    target, opened = install_grib(snapshot, grib)
    manifest = update_manifest(snapshot, forecast_time.isoformat())
    summary = {
        "snapshot": str(snapshot),
        "content_sha256": manifest["content_sha256"],
        "files": len(manifest["files"]),
        "bytes": sum(entry["bytes"] for entry in manifest["files"]),
        "forecast_time": manifest["forecast_time"],
        "grib": str(grib),
        "grib_bands": bands,
        "gfs": {"date": args.date, "cycle": args.cycle, "steps": steps},
        "bounds": args.bounds,
        "opened": opened,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
