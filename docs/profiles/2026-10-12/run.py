import cProfile
import json
import pstats
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import weatherrouting
from osgeo import ogr
from pyinstrument import Profiler
from pyinstrument.renderers import JSONRenderer
from gweatherrouting.core.grib import Grib
from gweatherrouting.core.marine.fields import FieldStore, FieldSelection
from gweatherrouting.core.marine.routing import (
    CurrentIsoRouter,
    CurrentRoutingEnvironment,
    FrozenRoutingWind,
)
from gweatherrouting.gtk.charts.gshhs import GSHHSVectorChart

OUT = Path("/private/tmp/gwr-routing-profile")
OUT.mkdir(parents=True, exist_ok=True)
DATA = Path.home() / "Library/Application Support/gweatherrouting"
start = datetime(2026, 10, 12, 4, 55, tzinfo=timezone.utc)
track = next(
    t
    for t in json.loads((DATA / "track.json").read_text())["data"]["elements"]
    if t["name"] == "track"
)
points = [p[:2] for p in track["points"]]
gribs = [Grib.parse(str(p)) for p in sorted((DATA / "grib").glob("*.grib2"))]
fields = FieldStore(DATA / "marine/fields").snapshot().fields
print("INPUTS", len(gribs), "gribs", len(fields), "current fields", points, flush=True)


def run(label, profiler=None):
    wind = FrozenRoutingWind(SimpleNamespace(gribs=gribs))
    env = CurrentRoutingEnvironment(wind, FieldSelection(fields))
    shore = GSHHSVectorChart.__new__(GSHHSVectorChart)
    shore.lpvFile = ogr.Open(str(DATA / "gshhs/GSHHS_shp/h/GSHHS_h_L1.shp"), 0)
    assert shore.lpvFile is not None
    route = weatherrouting.Routing(
        partial(CurrentIsoRouter, environment=env),
        weatherrouting.Polar(str(DATA / "polars/bavaria38.pol")),
        points,
        wind,
        start,
        points_validity=shore.points_validity,
        lines_validity=shore.lines_validity,
    )
    rows = []
    if profiler:
        if isinstance(profiler, cProfile.Profile):
            profiler.enable()
        else:
            profiler.start()
    began = time.perf_counter()
    for step in range(150):
        tick = time.perf_counter()
        result = route.step()
        rows.append(
            dict(
                step=step + 1,
                seconds=time.perf_counter() - tick,
                frontier=len(result.isochrones[-1]),
            )
        )
        if step % 10 == 0 or route.end:
            print(
                label,
                step + 1,
                round(time.perf_counter() - began, 3),
                "seconds",
                "end",
                route.end,
                flush=True,
            )
        if route.end:
            break
    elapsed = time.perf_counter() - began
    if profiler:
        if isinstance(profiler, cProfile.Profile):
            profiler.disable()
        else:
            profiler.stop()
    assert route.end, "Profile route did not complete"
    path = [dict(position=list(p.pos), time=p.time.isoformat()) for p in result.path]
    report = dict(
        label=label,
        seconds=elapsed,
        steps=len(rows),
        rows=rows,
        path=path,
        current_sources=sorted(env.selection.used),
        wind_sources=sorted(wind.used),
        excluded_samples=len(env.excluded_samples),
    )
    (OUT / (label + "-run.json")).write_text(json.dumps(report, indent=2, default=str))
    return report


reports = []
for label in ["baseline-1", "baseline-2"]:
    reports.append(run(label))
prof = Profiler(interval=0.001, async_mode="disabled")
reports.append(run("sampling", prof))
(OUT / "sampling.html").write_text(prof.output_html())
(OUT / "sampling.txt").write_text(
    prof.output_text(unicode=False, color=False, show_all=True)
)
(OUT / "sampling.json").write_text(prof.output(renderer=JSONRenderer()))
cp = cProfile.Profile()
reports.append(run("cprofile", cp))
cp.dump_stats(str(OUT / "routing.prof"))
with (OUT / "cprofile.txt").open("w") as stream:
    pstats.Stats(cp, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(
        65
    )
    pstats.Stats(cp, stream=stream).strip_dirs().sort_stats("tottime").print_stats(65)
assert all(r["path"] == reports[0]["path"] for r in reports), "Profile changed result"
assert all(r["current_sources"] == reports[0]["current_sources"] for r in reports)
(OUT / "summary.json").write_text(
    json.dumps(
        [{k: v for k, v in r.items() if k not in ("rows", "path")} for r in reports],
        indent=2,
        default=str,
    )
)
print("DONE: matched paths and current sources for all runs", flush=True)
