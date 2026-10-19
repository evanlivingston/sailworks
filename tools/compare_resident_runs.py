"""Matched source-residency controls with deferred fidelity, not speedup acceptance."""

import argparse
import json
from pathlib import Path

import architecture_run_protocol as protocol
from experiment_vertical_renderer import read_bundle, assert_same_prepared_assets


def check_reports(reference, candidate):
    if (reference["data_policy"], candidate["data_policy"]) != ("reopen", "resident"):
        raise ValueError("Expected reopen control and resident candidate")
    for key in ("protocol", "snapshot_sha256", "initial_view", "clocks", "host"):
        if reference[key] != candidate[key]:
            raise ValueError(f"Matched workload differs: {key}")
    for report in (reference, candidate):
        protocol.require_clean(report["producer_status"])
        if (
            report["measured_window_has_oracle_or_readback"]
            or report["explicit_readback_guard"]["forbidden"]
        ):
            raise ValueError("Readback/oracle contaminated measured window")
        records = report["post_window_validation"]["records"]
        if [r["generation"] for r in records] != [1, 2, 3, 5] or not all(
            r["unchanged_assets"]
            and r["scene_pixels_exact"]
            and r["fidelity"]["exact_reference_selections"] == 25
            for r in records
        ):
            raise ValueError("Deferred fidelity incomplete")
        if [p["generation"] for p in report["freshness"]["published"]] != [5] or report[
            "freshness"
        ]["failures"]:
            raise ValueError("Native freshness gate failed")
        worker = report["data_worker"]
        if worker["owner"] == worker["producer_gui_thread"] or {
            e["thread"] for e in worker["events"]
        } != {worker["owner"]}:
            raise ValueError("Numerical resource crossed owner threads")
        opens = sum(e["event"] == "open" for e in worker["events"])
        closes = sum(e["event"] == "close" for e in worker["events"])
        tasks = sum(e["event"] == "task" for e in worker["events"])
        if (
            tasks != 8
            or opens != closes
            or opens != (1 if report["data_policy"] == "resident" else tasks)
        ):
            raise ValueError("Numerical resource retention/release differs from policy")
        if (
            not all(leg["after_rendering_seconds"] for leg in report["diagnostics"])
            or not report["freshness"]["after_rendering_seconds"]
        ):
            raise ValueError("Measured windows have no render signal evidence")
    old_inputs = [
        [i["state"] for i in leg["inputs"]] for leg in reference["diagnostics"]
    ]
    new_inputs = [
        [i["state"] for i in leg["inputs"]] for leg in candidate["diagnostics"]
    ]
    if old_inputs != new_inputs or [len(x) for x in old_inputs] != [32, 32]:
        raise ValueError("Fixed input trajectories differ")
    old_transitions = [
        (i["generation"], i["view"]) for i in reference["freshness"]["transitions"]
    ]
    new_transitions = [
        (i["generation"], i["view"]) for i in candidate["freshness"]["transitions"]
    ]
    if old_transitions != new_transitions:
        raise ValueError("Freshness trajectories differ")


def compare(directories):
    reports, manifests = [], []
    for root in directories:
        manifest = json.loads((root / "manifest.json").read_text())
        protocol.require_clean(manifest["status"])
        if protocol.file_hash(root / "replay.json") != manifest["replay_sha256"]:
            raise ValueError("Replay evidence changed")
        reports.append(json.loads((root / "replay.json").read_text()))
        manifests.append(manifest)
        timed_jobs = json.loads(
            (root / "preparation" / "producer" / "service-result.json").read_text()
        )["completed"]
        if [job["generation"] for job in timed_jobs] != [1, 2, 3, 5] or any(
            job["error"]
            or not job["fidelity"].get("oracle_pending")
            or job["fidelity"]["exact_reference_selections"] != 0
            for job in timed_jobs
        ):
            raise ValueError(
                "Timed job, including obsolete generation, ran oracle or failed"
            )
    if manifests[0]["harness_identity"] != manifests[1]["harness_identity"]:
        raise ValueError("Harness differs between matched controls")
    check_reports(*reports)
    for generation in (1, 2, 3, 5):
        bundles = [
            read_bundle(root / "preparation" / "producer" / f"generation-{generation}")
            for root in directories
        ]
        assert_same_prepared_assets(*bundles)

    def observed(report):
        return dict(
            producer_startup_seconds=report["producer_ready_seconds"],
            prepare_seconds=[
                x["response"]["prepare_seconds"] for x in report["requests"]
            ],
            gui_publication_ms=[x["gui_publication_ms"] for x in report["requests"]],
            gui_tick_gap_ms=[x["gui_tick_gap_ms"] for x in report["diagnostics"]],
            gui_callback_ms=[x["gui_callback_ms"] for x in report["diagnostics"]],
            source_opens=sum(
                e["event"] == "open" for e in report["data_worker"]["events"]
            ),
            post_validation_seconds=report["post_window_validation"]["seconds"],
        )

    return dict(
        protocol="matched-resident-worker-a0h-v1",
        matched_identity_and_fidelity=True,
        reference=observed(reports[0]),
        candidate=observed(reports[1]),
        presentation_measured=False,
        application_speedup_accepted=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Refusing to overwrite comparison evidence")
    args.output.write_text(
        json.dumps(compare([args.reference, args.candidate]), indent=2) + "\n"
    )
    print(
        "Matched inputs, ownership and deferred fidelity passed; no application speedup acceptance"
    )


if __name__ == "__main__":
    main()
