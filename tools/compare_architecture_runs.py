"""A0b explicit process supervision and whole-path architecture comparison."""

import argparse
import json
from pathlib import Path
import sys

import architecture_run_protocol as protocol

ROOT = Path(__file__).resolve().parents[1]


def check_pair(
    reference,
    candidate,
    reference_status,
    candidate_status,
    reference_resources,
    candidate_resources,
):
    protocol.require_clean(reference_status)
    protocol.require_clean(candidate_status)
    if candidate.get("scenario") == "coverage_miss":
        raise ValueError(
            "Coverage-miss safety-only prototype cannot pass whole-path comparison; "
            "new-area preparation/post-load fidelity is unimplemented"
        )
    if (
        not candidate["static_complete_pixels_exact"]
        or not candidate["nonblank_guard_passed"]
    ):
        raise ValueError("Candidate content gate failed")
    old_host, new_host = reference["native_fidelity"]["host"], candidate["host"]
    if any(old_host[key] != new_host[key] for key in ("qt_platform", "backend", "dpr")):
        raise ValueError("Rendering host configuration differs")
    a, b = reference["native_fidelity"]["priming"], candidate["priming"]
    if not a or not b:
        raise ValueError("Native priming metadata missing on one side")
    if a["protocol"] != b["protocol"] or a["requested_ms"] != b["requested_ms"]:
        raise ValueError("Native priming protocols differ")
    if a["requested_ms"] < 1000:
        raise ValueError("Native priming below one second")
    if any(len(p["gui_tick_seconds"]) < 30 for p in (a, b)):
        raise ValueError("Insufficient native priming GUI ticks")
    # Render callbacks during static priming are metadata only: QQuickWidget does
    # not re-render an unchanged scene graph on every update(), so callback
    # counts reflect Qt render-loop behavior, not priming quality. The equivalent
    # event-loop warm-up is established by matching protocol, duration and tick
    # counts. Input-phase render observations are reported as-is in the analysis
    # (coalesced/missed revisions are part of what is being compared).
    # The effective redirected render target is recorded as diagnostic metadata
    # only: this Qt/RHI configuration returns a null target from
    # getResource(RhiRedirectRenderTarget) during afterRendering, so it cannot
    # gate a comparison. Surface-format sample/alpha queries are likewise
    # diagnostic (-1 indicates the query itself is unreliable here).
    targets = [p.get("effective_render_target") for p in (a, b)]
    if any(t is not None for t in targets):
        reference["native_fidelity"]["render_target_diagnostic"] = targets[0]
        candidate["render_target_diagnostic"] = targets[1]
    # Multi-time resource identity: forecast-time stamps and per-step field data.
    if reference_resources["clocks"] != candidate_resources["clocks"]:
        raise ValueError("Forecast-time stamps differ")
    ref_meta = reference_resources["field_meta"]
    cand_meta = candidate_resources["field_meta"]
    if len(ref_meta) != len(cand_meta):
        raise ValueError("Time step count differs")
    for rm, cm in zip(ref_meta, cand_meta):
        if rm["step"] != cm["step"]:
            raise ValueError(f"Time step index differs: {rm['step']} vs {cm['step']}")
        for key in ("before", "after", "generation", "node_count"):
            if rm[key] != cm[key]:
                raise ValueError(f"Field meta differs at step {rm['step']}: {key}")
    for key in ("background.npy", "navigation.npy", "oracle.png"):
        if reference_resources["files"][key] != candidate_resources["files"][key]:
            raise ValueError("Prepared visual content differs")
    for fm in ref_meta:
        if (
            reference_resources["files"][fm["file"]]
            != candidate_resources["files"][fm["file"]]
        ):
            raise ValueError(f"Field content differs: {fm['file']}")
    old, new = (
        reference["native_render_probe"]["inputs"],
        candidate["native_render_probe"]["inputs"],
    )
    if (
        len(old) != 24
        or len(new) != 24
        or any(
            i["revision"] != n for stream in (old, new) for n, i in enumerate(stream)
        )
    ):
        raise ValueError("Incomplete or duplicated input stream")
    for stream in (old, new):
        if any("forecast_time" not in i["state"] for i in stream):
            raise ValueError("Input state missing forecast_time")
    for ref_input, cand_input in zip(old, new):
        if ref_input["state"] != cand_input["state"]:
            raise ValueError("Camera/forecast-time trajectories differ")


def analyze(reference, candidate):
    # Existing verified attribution retains the all-input denominator. These are
    # render callbacks, not compositor presentation; conditional timing stays named.
    import experiment_native_retained_weather as native

    candidate_inputs = candidate["native_render_probe"]["inputs"]
    time_steps = {}
    for i in candidate_inputs:
        ts = i["state"].get("forecast_time")
        time_steps.setdefault(ts, []).append(i["dispatch_late_ms"])
    per_time = {
        ts: {"count": len(v), "dispatch_late_max_ms": max(v)}
        for ts, v in sorted(time_steps.items())
    }

    submissions = candidate["native_render_probe"].get("submissions", [])
    miss_submissions = [
        s
        for s in submissions
        if s.get("content", {}).get("numerical_weather", {}).get("coverage_miss")
    ]
    covered_submissions = [
        s
        for s in submissions
        if not s.get("content", {}).get("numerical_weather", {}).get("coverage_miss")
    ]
    return dict(
        reference=native.observations(reference, 16.667),
        candidate=native.observations(candidate, 16.667),
        gui_selection_ms=candidate["gui_selection_ms"],
        complete_initial_pixels_exact=True,
        clean_child_exits=True,
        host_and_input_identity_match=True,
        end_to_end_speedup_proven=False,
        asynchronous_preparation_tested=False,
        presentation_measured=False,
        time_steps=candidate.get("time_steps", 0),
        per_time_dispatch_lateness=per_time,
        scenario=candidate.get("scenario", "scrub"),
        coverage_miss_count=len(miss_submissions),
        coverage_miss_revisions=sorted(s["revision"] for s in miss_submissions),
        covered_submission_count=len(covered_submissions),
        coverage_miss_submission_count=len(miss_submissions),
    )


def run(args):
    if args.scenario == "coverage_miss" and args.kind == "reference":
        raise ValueError(
            "Coverage-miss currently supports standalone candidate safety only"
        )
    if args.directory.exists():
        raise ValueError("Refusing to overwrite architecture evidence directory")
    args.directory.mkdir(parents=True)
    env = protocol.environment()
    env["PYTHONPATH"] = str(args.app_root.resolve()) + ":" + str(ROOT)
    script = ROOT / "tools/experiment_vertical_renderer.py"
    output = args.directory / "replay.json"
    # Map the high-level scenario to reference/candidate-specific values.
    if args.scenario == "coverage_miss":
        reference_scenario = "reverse_pan"
        candidate_scenario = "coverage_miss"
    else:
        reference_scenario = "scrub_return"
        candidate_scenario = "scrub"
    command = [
        sys.executable,
        str(script),
        "--app-root",
        str(args.app_root.resolve()),
        "--resources",
        str(args.resources.resolve()),
        "--output",
        str(output),
        "--prime-ms",
        "2000",
        "--scrub-interval-seconds",
        str(args.scrub_interval_seconds),
        "--scrub-steps",
        str(args.scrub_steps),
        "--scenario",
        reference_scenario if args.kind == "reference" else candidate_scenario,
        "--coverage-step-px",
        str(args.coverage_step_px),
    ]
    if args.kind == "reference":
        if args.snapshot is None:
            raise ValueError("Reference requires snapshot")
        command += ["--snapshot", str(args.snapshot.resolve())]
    else:
        command += ["--candidate"]
    status = protocol.supervise(
        command, ROOT, env, args.directory / "native", timeout=110
    )
    hashes = {
        name: protocol.file_hash(ROOT / "tools" / name)
        for name in (
            "experiment_vertical_renderer.py",
            "compare_architecture_runs.py",
            "architecture_run_protocol.py",
            "native_rendering_probe.py",
            "experiment_app_persistent_weather.py",
            "experiment_persistent_weather.py",
        )
    }
    manifest = dict(
        kind=args.kind,
        status=status,
        harness_identity=hashes,
        resources=str(args.resources.resolve()),
        replay_sha256=protocol.file_hash(output) if output.is_file() else None,
    )
    (args.directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            dict(
                kind=args.kind,
                exit_code=status["exit_code"],
                timed_out=status["timed_out"],
                exception=status["exception"],
                duration_s=status["duration_s"],
            )
        ),
        flush=True,
    )
    protocol.require_clean(status)
    if not output.is_file():
        raise ValueError("Clean child did not produce replay")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("reference", "candidate"))
    parser.add_argument("--app-root", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--resources", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--scrub-interval-seconds",
        type=int,
        default=1,
        help="Seconds between forecast-time steps for timeline scrub",
    )
    parser.add_argument(
        "--scrub-steps",
        type=int,
        default=12,
        help="Number of forecast-time steps (forward) for timeline scrub",
    )
    parser.add_argument(
        "--scenario",
        choices=("scrub_return", "coverage_miss"),
        default="scrub_return",
        help="Replay scenario (coverage_miss is candidate safety-only, not comparable)",
    )
    parser.add_argument(
        "--coverage-step-px",
        type=int,
        default=48,
        help="Screen pixels per pan step for coverage-miss scenario",
    )
    args = parser.parse_args()
    if args.compare:
        a, b = args.compare
        manifests = [json.loads((p / "manifest.json").read_text()) for p in (a, b)]
        if manifests[0]["kind"] != "reference" or manifests[1]["kind"] != "candidate":
            raise ValueError("Expected reference/candidate reports")
        if manifests[0]["harness_identity"] != manifests[1]["harness_identity"]:
            raise ValueError("Harness identity differs")
        reports = [json.loads((p / "replay.json").read_text()) for p in (a, b)]
        for p, m in zip((a, b), manifests):
            if protocol.file_hash(p / "replay.json") != m["replay_sha256"]:
                raise ValueError("Replay artifact changed")
        resources = [
            json.loads((Path(m["resources"]) / "resources.json").read_text())
            for m in manifests
        ]
        check_pair(*reports, *(m["status"] for m in manifests), *resources)
        result = analyze(*reports)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(
            "Matched whole-path artifacts accepted for diagnostic comparison; no speedup acceptance"
        )
    else:
        if not all((args.kind, args.app_root, args.resources, args.directory)):
            parser.error("--kind, --app-root, --resources, --directory required")
        run(args)


if __name__ == "__main__":
    main()
