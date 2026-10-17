"""Read-only replay of saved wind sources, with per-stage timings and counts."""

import json
import argparse
from datetime import datetime
from pathlib import Path
from threading import Event
from time import perf_counter
from statistics import median
from types import SimpleNamespace

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.mapviewport import MapViewport
from gweatherrouting.core.storage import DATA_DIR, GRIB_DIR
from gweatherrouting.rendering.overlayrenderer import RenderJob, RenderState
from gweatherrouting.rendering.windpainter import WindPainter
from gweatherrouting.rendering.windrenderer import WindOverlayRenderer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--style", choices=("barbs", "streamlines"), default="barbs")
    parser.add_argument(
        "--density", choices=("sparse", "standard", "dense", "very_dense", "all")
    )
    args = parser.parse_args()
    directory = Path(DATA_DIR)
    settings = json.loads((directory / "settings.json").read_text())
    if args.density is not None:
        settings["gribBarbDensity"] = args.density
    workspace = json.loads((directory / "workspace-qt.json").read_text())
    saved = workspace.get("workspace", workspace)
    if "map" not in saved:
        saved = next(
            value
            for value in workspace.values()
            if isinstance(value, dict) and "map" in value
        )
    position = saved["map"]
    opened = json.loads((directory / "grib-manager.json").read_text())["opened"]
    sources = tuple((Grib.parse(str(Path(GRIB_DIR) / name)),) for name in opened)
    canvas = SimpleNamespace(
        _center_lat=position["latitude"],
        _center_lon=position["longitude"],
        _zoom=position["zoom"],
        get_allocated_width=lambda: 1500 + 512,
        get_allocated_height=lambda: 900 + 512,
        queue_draw=lambda: None,
    )
    viewport = MapViewport(canvas, margin=256)
    palette = (
        datetime.now().replace(minute=0, second=0, microsecond=0),
        settings["gribArrowOpacity"],
        True,
        settings["gribIsobarsVisible"],
        True,
        settings["gribIsobarInterval"],
        settings["gribIsobarOpacity"],
        settings["gribIsobarLabels"],
        args.style,
        settings["gribBarbDensity"],
    )
    key = (palette, 2, tuple(source for source, in sources))
    job = RenderJob(key, viewport, sources, 2, palette, cancel=Event())
    renderer = WindOverlayRenderer(WindPainter)
    contains = renderer._land_mask.contains
    mask_seconds = 0.0

    def timed_contains(lat, lon):
        nonlocal mask_seconds
        start = perf_counter()
        result = contains(lat, lon)
        mask_seconds += perf_counter() - start
        return result

    # refresh_source invalidates the original cache, not this timing wrapper.
    timed_contains.cache_clear = contains.cache_clear
    renderer._land_mask.contains = timed_contains

    class TimedPainter(WindPainter):
        def draw(self, *args, **kwargs):
            start = perf_counter()
            result = super().draw(*args, **kwargs)
            print(
                json.dumps(dict(stage="paint", seconds=perf_counter() - start)),
                flush=True,
            )
            return result

    renderer._painter = TimedPainter
    for name in ("_load_wind", "_load_pressure", "_resample_field", "_render_fields"):
        original = getattr(renderer, name)

        def measured(*args, _name=name, _original=original):
            start = perf_counter()
            print(f"start {_name}", flush=True)
            value = _original(*args)
            count = sum(map(len, value)) if isinstance(value, list) else None
            print(
                json.dumps(
                    dict(stage=_name, seconds=perf_counter() - start, points=count)
                ),
                flush=True,
            )
            return value

        setattr(renderer, name, measured)
    print(
        json.dumps(
            dict(sources=len(sources), time=palette[0].isoformat(), position=position)
        ),
        flush=True,
    )
    start = perf_counter()
    surface = renderer._render(job)
    print(
        json.dumps(
            dict(
                render_seconds=perf_counter() - start,
                mask_seconds=mask_seconds,
                streamline_segments=(
                    0 if job.streamlines is None else len(job.streamlines[0])
                ),
                samples=0 if job.barb_flat is None else len(job.barb_flat[0]),
                land=None if job.barb_land is None else int(job.barb_land.sum()),
                streamline_land_segments=(
                    None if job.streamlines is None else int(job.streamlines[5].sum())
                ),
            )
        ),
        flush=True,
    )
    # Geometry counts at the real saved view, independent of background paint.
    marker = object()
    renderer._states[marker] = RenderState(key=key, frame=(job, surface))
    for over_land in (True, False):
        renderer._barbs_over_land[marker] = over_land
        durations = []
        for offset in range(20):
            started = perf_counter()
            renderer.live_geometry_for_view(
                marker,
                1500,
                900,
                viewport._center_lat + (offset + 1) * 1e-6,
                viewport._center_lon,
                viewport._zoom,
                density=settings["gribBarbDensity"],
            )
            durations.append((perf_counter() - started) * 1000)
        geometry, opacity = renderer.live_geometry_for_view(
            marker,
            1500,
            900,
            viewport._center_lat,
            viewport._center_lon,
            viewport._zoom,
            density=settings["gribBarbDensity"],
        )
        print(
            json.dumps(
                dict(
                    over_land=over_land,
                    lines=sum(len(value[0]) for value in geometry.values()),
                    opacity=opacity,
                    moving_geometry_median_ms=median(durations),
                )
            ),
            flush=True,
        )
    renderer.forget(marker)
    for (source,) in sources:
        source.close()


if __name__ == "__main__":
    main()
