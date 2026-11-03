"""Compare two viewport selection-pan baseline reports.

The baseline report is committed; candidates are produced locally with
``tools/benchmark_viewport_selection_pan.py``. A candidate is compatible only
when its snapshot, camera, workload and settings match the baseline.
"""

import argparse
import json
import math
from pathlib import Path


def summary(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}

    def percentile(fraction):
        index = (len(ordered) - 1) * fraction
        lower = math.floor(index)
        upper = math.ceil(index)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)

    return {
        "count": len(values),
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": ordered[-1],
    }


def change(before, after):
    return None if not before else 100.0 * (after / before - 1.0)


def compare(before, after):
    for key in ("schema_version", "kind", "snapshot_sha256", "camera", "workload"):
        if before.get(key) != after.get(key):
            raise ValueError(f"Incompatible {key}; refusing a misleading comparison")
    if before.get("settings") != after.get("settings"):
        raise ValueError("Incompatible settings; refusing a misleading comparison")
    rows = {}
    for name in ("update_ms", "stage_selection_ms", "node_anchor_atlas_ms"):
        old = summary(before["samples"].get(name, []))
        new = summary(after["samples"].get(name, []))
        rows[name] = {
            "before": old,
            "after": new,
            "p50_change_percent": change(old.get("p50_ms"), new.get("p50_ms")),
            "p95_change_percent": change(old.get("p95_ms"), new.get("p95_ms")),
            "p99_change_percent": change(old.get("p99_ms"), new.get("p99_ms")),
        }
    counters = {
        key: {
            "before": before.get("counters", {}).get(key),
            "after": after.get("counters", {}).get(key),
            "change": (
                after.get("counters", {}).get(key, 0)
                - before.get("counters", {}).get(key, 0)
            ),
        }
        for key in sorted(
            set(before.get("counters", {})) | set(after.get("counters", {}))
        )
    }
    return {
        "compatible": True,
        "snapshot_sha256": before["snapshot_sha256"],
        "timings": rows,
        "counters": counters,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = compare(
        json.loads(args.baseline.read_text()),
        json.loads(args.candidate.read_text()),
    )
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
