"""Supervise A0e new-area load/fidelity probe; not a performance comparison."""

import argparse
import json
from pathlib import Path
import sys

import architecture_run_protocol as protocol

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("app-root", "snapshot", "resources", "directory"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--persistent", action="store_true")
    parser.add_argument(
        "--data-policy", choices=("legacy", "reopen", "resident"), default="legacy"
    )
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    output = args.directory / "replay.json"
    env = protocol.environment()
    env["PYTHONPATH"] = str(args.app_root.resolve()) + ":" + str(ROOT)
    command = [
        sys.executable,
        str(ROOT / "tools/experiment_vertical_renderer.py"),
        "--candidate",
        "--scenario",
        (
            ("coverage_perf" if args.data_policy != "legacy" else "coverage_stream")
            if args.persistent
            else "coverage_load"
        ),
        "--app-root",
        str(args.app_root.resolve()),
        "--snapshot",
        str(args.snapshot.resolve()),
        "--resources",
        str(args.resources.resolve()),
        "--preparation-directory",
        str(args.directory.resolve() / "preparation"),
        "--output",
        str(output.resolve()),
        "--data-policy",
        args.data_policy,
    ]
    status = protocol.supervise(
        command, ROOT, env, args.directory / "native", timeout=120
    )
    hashes = {
        name: protocol.file_hash(ROOT / "tools" / name)
        for name in (
            "run_coverage_load.py",
            "experiment_vertical_renderer.py",
            "benchmark_startup_rendering.py",
            "coverage_scheduler.py",
            "architecture_run_protocol.py",
            "persistent_coverage.py",
            "resident_weather_worker.py",
        )
    }
    manifest = dict(
        status=status,
        harness_identity=hashes,
        initial_resources_sha256=protocol.file_hash(args.resources / "resources.json"),
        replay_sha256=protocol.file_hash(output) if output.exists() else None,
    )
    (args.directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(status), flush=True)
    protocol.require_clean(status)
    if not output.exists():
        raise ValueError("Clean child did not produce load evidence")


if __name__ == "__main__":
    main()
