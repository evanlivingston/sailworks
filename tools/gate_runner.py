#!/usr/bin/env python3
"""One-command performance gate runner over the existing benchmark harnesses.

The runner **composes** the tools that already own each measurement; it does
not re-implement, replace or re-tune them.  One command gates one milestone:

    python tools/gate_runner.py --gate <gate-id> [--snapshot PATH] [--output DIR]

It looks the gate up in the registry (:mod:`tools.gates`), invokes the owning
tool with the right flags, evaluates the threshold from the emitted JSON,
prints a single compact PASS/FAIL line and writes a small machine-readable
result next to the raw tool report.  It exits non-zero on FAIL so CI can use
it.

Deliberate properties

* **No heavy imports.**  Only the standard library and the pure-data registry
  are imported at module import time, so ``--list`` and ``--help`` are instant
  and side-effect free.  GDAL, Qt and the application are only ever reached
  inside a child process.
* **One scenario by default.**  The default workload is a single scenario with
  a small frame/repeat count.  A full matrix is opt-in behind ``--full``.
* **No silent re-measurement.**  A gate whose reference is a *measured
  baseline* needs the caller to supply that artifact with ``--reference``; the
  runner prints the exact command that produces it instead of re-measuring.
* **Endpoints are labelled.**  Render callbacks are proxies, readback is
  fidelity; no endpoint here is presentation FPS.
* **The interpreter is checked before the tool runs.**  A gate that cannot be
  evaluated is reported as ``NOT-EVALUATED``, never as a threshold failure.
  If the current interpreter cannot import what the composed tool needs, the
  runner re-executes itself under the project virtualenv (guarded against a
  loop) and, failing that, names the interpreter to use.
"""

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]

# The registry is loaded by path, like the sibling harnesses do, so that
# importing this module never depends on sys.path or on a package install.
GATES_SPEC = importlib.util.spec_from_file_location(
    "gate_registry", Path(__file__).with_name("gates.py")
)
gates = importlib.util.module_from_spec(GATES_SPEC)
GATES_SPEC.loader.exec_module(gates)

# Exit codes.  PASS and FAIL are different outcomes, and "the gate could not
# be evaluated" is a third outcome that must never be confused with either.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_NOT_EVALUATED = 2
EXIT_SELFTEST = 3
EXIT_USAGE = 64

# Set on the environment of a re-executed runner so it can never re-exec
# itself a second time.
REEXEC_FLAG = "GWR_GATE_RUNNER_REEXEC"

# Verdicts.  The three outcomes are deliberately distinct words: a gate that
# was evaluated and passed, a gate that was evaluated and whose threshold was
# violated, and a gate that could not be evaluated at all.
VERDICT_PASS = "pass"
VERDICT_PASS_OPEN = "pass-with-open-checks"
VERDICT_FAIL = "fail"
VERDICT_NOT_EVALUATED = "not-evaluated"
VERDICT_LABELS = {
    VERDICT_PASS: "PASS",
    VERDICT_PASS_OPEN: "PASS-WITH-OPEN-CHECKS",
    VERDICT_FAIL: "FAIL",
    VERDICT_NOT_EVALUATED: "NOT-EVALUATED",
}

_SEGMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[(\*|\d+)\])?$")


# --------------------------------------------------------------------------
# metric resolution
# --------------------------------------------------------------------------
def parse_path(path):
    """Split ``a.b[*].c[0]`` into ``[("a", None), ("b", "*"), ("c", 0)]``."""
    segments = []
    for part in path.split("."):
        match = _SEGMENT.match(part)
        if not match:
            raise ValueError(f"Invalid metric path segment: {part!r} in {path!r}")
        index = match.group(2)
        segments.append(
            (
                match.group(1),
                None if index is None else ("*" if index == "*" else int(index)),
            )
        )
    return segments


def resolve(document, path):
    """Return ``[(parent, key, value)]`` leaves for a metric path.

    ``parent`` is the containing mapping, so a check can filter on sibling
    attributes with ``where`` (for example the diagnostic case name).
    """
    current = [(None, None, document)]
    for name, index in parse_path(path):
        following = []
        for parent, _key, value in current:
            if not isinstance(value, dict) or name not in value:
                continue
            child = value[name]
            if index is None:
                following.append((value, name, child))
            elif index == "*":
                if not isinstance(child, list):
                    continue
                for position, item in enumerate(child):
                    following.append((child, position, item))
            else:
                if not isinstance(child, list) or index >= len(child):
                    continue
                following.append((child, index, child[index]))
        current = following
        if not current:
            return []
    return current


def filter_where(leaves, where):
    if not where:
        return leaves
    kept = []
    for parent, key, value in leaves:
        if not isinstance(parent, dict):
            continue
        if all(parent.get(name) == expected for name, expected in where.items()):
            kept.append((parent, key, value))
    return kept


def percentile(values, fraction):
    """Linear-interpolated percentile, matching the harnesses' own summary."""
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def aggregate(values, kind, match=None):
    if kind == "count":
        return len(values)
    if kind == "list_length":
        lengths = [len(value) for value in values if isinstance(value, list)]
        if not lengths:
            raise ValueError("list_length needs at least one list value")
        return sum(lengths)
    if kind == "count_eq":
        return sum(1 for value in values if value == match)
    if kind == "count_gt":
        return sum(1 for value in values if value > match)
    if kind == "count_ge":
        return sum(1 for value in values if value >= match)
    if kind == "count_lt":
        return sum(1 for value in values if value < match)
    if kind == "count_le":
        return sum(1 for value in values if value <= match)
    if not values:
        raise ValueError("no values to aggregate")
    if kind == "max":
        return max(values)
    if kind == "min":
        return min(values)
    if kind == "sum":
        return sum(values)
    if kind == "mean":
        return sum(values) / len(values)
    if kind == "first":
        return values[0]
    if kind == "last":
        return values[-1]
    if kind == "all":
        return all(bool(value) for value in values)
    if kind == "any":
        return any(bool(value) for value in values)
    if kind.startswith("p") and kind[1:].isdigit():
        return percentile([float(value) for value in values], int(kind[1:]) / 100)
    raise ValueError(f"Unknown aggregate: {kind}")


def compare(observed, operator, threshold):
    if operator == "is_true":
        return bool(observed) is True
    if operator == "is_false":
        return bool(observed) is False
    if operator == "==":
        return observed == threshold
    if operator == "!=":
        return observed != threshold
    if operator == "<=":
        return observed <= threshold
    if operator == "<":
        return observed < threshold
    if operator == ">=":
        return observed >= threshold
    if operator == ">":
        return observed > threshold
    raise ValueError(f"Unknown comparison: {operator}")


# --------------------------------------------------------------------------
# check evaluation
# --------------------------------------------------------------------------
def evaluate_check(check, document, baseline=None, comparable=True):
    """Evaluate one check and return its result record."""
    result = {
        "label": check["label"],
        "metric": check["metric"],
        "aggregate": check.get("aggregate", "max"),
        "op": check["op"],
        "source": check.get("source"),
        "ratified": check.get("ratified", True),
    }
    if "where" in check:
        result["where"] = check["where"]
    if check.get("fixture_sensitive"):
        result["fixture_sensitive"] = True
    try:
        leaves = filter_where(resolve(document, check["metric"]), check.get("where"))
        if not leaves:
            result.update(verdict="fail", observed=None, reason="metric not found")
            return result
        observed = aggregate(
            [value for _parent, _key, value in leaves],
            result["aggregate"],
            check.get("match"),
        )
    except (TypeError, ValueError) as exc:
        result.update(verdict="fail", observed=None, reason=str(exc))
        return result
    result["observed"] = observed
    baseline_spec = check.get("baseline")
    use_baseline = baseline is not None and baseline_spec and comparable
    try:
        if check["op"] in ("is_true", "is_false"):
            threshold = None
            result["basis"] = "boolean gate"
        elif use_baseline:
            base_leaves = filter_where(
                resolve(baseline, baseline_spec["metric"]),
                baseline_spec.get("where"),
            )
            if not base_leaves:
                result.update(verdict="fail", reason="baseline metric not found")
                return result
            base = aggregate(
                [value for _parent, _key, value in base_leaves],
                baseline_spec.get("aggregate", "max"),
                baseline_spec.get("match"),
            )
            threshold = base * baseline_spec.get("scale", 1.0)
            result["baseline_observed"] = base
            result["basis"] = "measured baseline supplied with --reference"
        elif "value" not in check:
            result.update(
                verdict="not-evaluated",
                reason=(
                    "this check needs a measured baseline; supply it with "
                    "--reference. The runner does not re-measure a baseline."
                ),
            )
            return result
        elif isinstance(check["value"], dict):
            other_leaves = filter_where(
                resolve(document, check["value"]["metric"]), check["value"].get("where")
            )
            if not other_leaves:
                result.update(verdict="fail", reason="comparison metric not found")
                return result
            threshold = aggregate(
                [value for _parent, _key, value in other_leaves],
                check["value"].get("aggregate", "max"),
                check["value"].get("match"),
            )
            result["basis"] = "recorded metric comparison"
        else:
            threshold = check["value"]
            result["basis"] = "recorded evidence"
    except (TypeError, ValueError) as exc:
        result.update(verdict="fail", reason=str(exc))
        return result
    result["threshold"] = threshold
    try:
        passed = compare(observed, check["op"], threshold)
    except TypeError as exc:
        result.update(verdict="fail", reason=f"incomparable values: {exc}")
        return result
    if passed:
        result["verdict"] = "pass"
    elif check.get("fixture_sensitive") and not comparable:
        result["verdict"] = "not-comparable"
        result["reason"] = (
            "the current fixture differs from the fixture the recorded "
            "reference was measured on; supply --reference with a baseline "
            "artifact from the same fixture to make this check binding"
        )
    else:
        result["verdict"] = "fail"
    return result


def evaluate(gate, document, baseline=None, comparable=True):
    results = [
        evaluate_check(check, document, baseline, comparable)
        for check in gate["checks"]
    ]
    for check, result in zip(gate["checks"], results):
        if not check.get("ratified", True):
            result["verdict"] = "unratified"
    return results


# --------------------------------------------------------------------------
# gate execution
# --------------------------------------------------------------------------
def git_revision():
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    try:
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
    except (OSError, subprocess.CalledProcessError):
        dirty = None
    return {"commit": commit, "dirty": dirty}


def build_command(gate, gate_id, settings, outdir, snapshot):
    substitutions = {
        "python": sys.executable,
        "tool": str(ROOT / gate["tool"]),
        "outdir": str(outdir),
        "app_root": str(ROOT),
        "gate": gate_id,
        "snapshot": str(snapshot) if snapshot else "",
        "scenarios": ",".join(settings["scenarios"]),
        "frames": str(settings["frames"]),
        "repeats": str(settings["repeats"]),
        "sections": str(settings.get("sections", "")),
    }
    output = Path(gate["tool_output"].format(**substitutions))
    result = Path(gate["result"].format(**substitutions))
    substitutions["tool_output"] = str(output)
    command = [part.format(**substitutions) for part in gate["command"]]
    return command, output, result


def environment_for(gate):
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in gate.get("env", {}).items()})
    return env


# --------------------------------------------------------------------------
# interpreter resolution
# --------------------------------------------------------------------------
def interpreter_missing(python, modules):
    """Return the modules ``python`` cannot import (empty list means usable).

    The probe runs in a child process, so the runner itself never imports
    GDAL, Qt or the application.
    """
    if not modules:
        return []
    probe = (
        "import importlib\n"
        "missing = []\n"
        f"for name in {list(modules)!r}:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception:\n"
        "        missing.append(name)\n"
        "print('\\n'.join(missing))\n"
    )
    try:
        completed = subprocess.run(
            [str(python), "-c", probe],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return list(modules)
    if completed.returncode != 0:
        return list(modules)
    return [line for line in completed.stdout.split() if line]


def find_project_interpreters():
    """Project virtualenv interpreters, most specific first.

    A git worktree has no ``.venv`` of its own, so the main checkout's
    virtualenv is found through ``git rev-parse --git-common-dir``.
    """
    candidates = [ROOT / ".venv" / "bin" / "python"]
    try:
        common = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        common = ""
    if common:
        path = Path(common)
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        candidates.append(path.parent / ".venv" / "bin" / "python")
    unique, seen = [], set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def resolve_interpreter(modules):
    """Return ``(interpreter, missing)`` for the modules a gate needs.

    The interpreter is ``None`` when no usable one was found; ``missing`` then
    lists what the current interpreter could not import.
    """
    missing = interpreter_missing(sys.executable, modules)
    if not missing:
        return Path(sys.executable), []
    current = os.path.realpath(sys.executable)
    for candidate in find_project_interpreters():
        if os.path.realpath(str(candidate)) == current:
            continue
        if not interpreter_missing(candidate, modules):
            return candidate, missing
    return None, missing


def reexec_under(python):
    """Replace this process with the runner under another interpreter."""
    environment = dict(os.environ)
    environment[REEXEC_FLAG] = "1"
    argv = [str(python), str(Path(__file__).resolve())] + list(sys.argv[1:])
    print(
        f"note: {sys.executable} cannot import the modules this gate needs; "
        f"re-executing the gate runner under {python}",
        file=sys.stderr,
    )
    sys.stderr.flush()
    sys.stdout.flush()
    try:
        os.execve(str(python), argv, environment)
    except OSError as exc:
        # Fall through to the not-evaluated path rather than dying.
        print(f"note: could not re-execute under {python}: {exc}", file=sys.stderr)
        return False
    return True


def not_evaluated_record(gate_id, args, reason):
    """Build the record for a gate that could not be evaluated at all."""
    gate = gates.GATES[gate_id]
    record = {
        "gate": gate_id,
        "title": gate["title"],
        "milestone": gate["milestone"],
        "kind": gate["kind"],
        "tool": gate["tool"],
        "command": [],
        "endpoint": gate["endpoint"],
        "reference": gate["reference"],
        "open_requirements": gate.get("open_requirements", []),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": 0.0,
        "checks": [],
        "verdict": VERDICT_NOT_EVALUATED,
        "evaluated": False,
        "exit_code": EXIT_NOT_EVALUATED,
        "reason": reason,
        "interpreter": sys.executable,
    }
    outdir = (
        Path(args.output).expanduser().resolve()
        if args.output
        else Path(tempfile.mkdtemp(prefix="gwr-gate-"))
    )
    outdir.mkdir(parents=True, exist_ok=True)
    record["output_dir"] = str(outdir)
    _write_result(outdir, gate_id, record)
    return record


def run_gate(gate_id, args):
    """Run one gate and return its result record."""
    gate = gates.GATES[gate_id]
    if args.snapshot is None and "snapshot" in gate["requires"]:
        raise ValueError(
            f"Gate {gate_id!r} needs --snapshot PATH "
            f"(the verified fixture the recorded evidence was measured on)"
        )
    if args.output:
        outdir = Path(args.output).expanduser().resolve()
        outdir.mkdir(parents=True, exist_ok=True)
    else:
        outdir = Path(tempfile.mkdtemp(prefix="gwr-gate-"))
    settings = dict(gate["defaults"])
    if args.full:
        settings.update(gate.get("full", {}))
    if args.frames is not None:
        settings["frames"] = args.frames
    if args.repeats is not None:
        settings["repeats"] = args.repeats
    command, output, result_path = build_command(
        gate, gate_id, settings, outdir, args.snapshot
    )
    timeout = min(int(gate.get("timeout_s", 110)), 120)
    began = time.perf_counter()
    record = {
        "gate": gate_id,
        "title": gate["title"],
        "milestone": gate["milestone"],
        "kind": gate["kind"],
        "tool": gate["tool"],
        "command": command,
        "scenario": settings["scenarios"],
        "frames": settings["frames"],
        "repeats": settings["repeats"],
        "sections": settings.get("sections", ""),
        "full": bool(args.full),
        "endpoint": gate["endpoint"],
        "reference": gate["reference"],
        "open_requirements": gate.get("open_requirements", []),
        "output_dir": str(outdir),
        "tool_report": str(output),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment_for(gate),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        record.update(
            verdict=VERDICT_NOT_EVALUATED,
            evaluated=False,
            exit_code=EXIT_NOT_EVALUATED,
            reason=(
                f"the gate was not evaluated: {gate['tool']} exceeded the "
                f"{timeout} s gate budget"
            ),
            duration_s=round(time.perf_counter() - began, 3),
        )
        (outdir / f"{gate_id}.tool.log").write_text(
            (exc.stdout or "") + (exc.stderr or ""), encoding="utf-8"
        )
        record["checks"] = []
        _write_result(outdir, gate_id, record)
        return record
    duration = round(time.perf_counter() - began, 3)
    (outdir / f"{gate_id}.tool.log").write_text(
        completed.stdout + completed.stderr, encoding="utf-8"
    )
    record["tool_exit_code"] = completed.returncode
    record["duration_s"] = duration
    if completed.returncode != 0:
        record.update(
            verdict=VERDICT_NOT_EVALUATED,
            evaluated=False,
            exit_code=EXIT_NOT_EVALUATED,
            reason=(
                f"the gate was not evaluated: {gate['tool']} exited "
                f"{completed.returncode} before producing a report; see "
                f"{gate_id}.tool.log"
            ),
            checks=[],
        )
        _write_result(outdir, gate_id, record)
        return record
    if not result_path.is_file():
        record.update(
            verdict=VERDICT_NOT_EVALUATED,
            evaluated=False,
            exit_code=EXIT_NOT_EVALUATED,
            reason=(
                f"the gate was not evaluated: {gate['tool']} did not write "
                f"{result_path}"
            ),
            checks=[],
        )
        _write_result(outdir, gate_id, record)
        return record
    document = json.loads(result_path.read_text())
    baseline = None
    baseline_match = False
    if args.reference:
        baseline = json.loads(Path(args.reference).expanduser().read_text())
        record["reference_artifact"] = str(args.reference)
    snapshot = snapshot_summary(document)
    current_sha = snapshot.get("sha256")
    reference_fixture = gate["reference"].get("fixture")
    fixture_match = True
    if reference_fixture:
        fixture_match = bool(current_sha) and current_sha == reference_fixture
    if baseline is not None:
        baseline_sha = snapshot_summary(baseline).get("sha256")
        baseline_match = bool(current_sha) and current_sha == baseline_sha
    comparable = fixture_match or baseline_match
    record["fixture_match"] = fixture_match
    record["baseline_matches_fixture"] = baseline_match
    record["snapshot"] = snapshot
    checks = list(gate["checks"]) + list(gate.get("baseline_checks", []))
    results = []
    for check, result in zip(
        checks, evaluate({"checks": checks}, document, baseline, comparable)
    ):
        results.append(result)
    if args.full and gate.get("min_runs_per_scenario"):
        results.append(min_runs_check(gate, document, settings))
    record["checks"] = results
    record["environment"] = environment_summary(document)
    record["git"] = git_revision()
    failures = [r for r in results if r["verdict"] == "fail"]
    unratified = [r for r in results if r["verdict"] == "unratified"]
    skipped = [r for r in results if r["verdict"] == "not-evaluated"]
    incomparable = [r for r in results if r["verdict"] == "not-comparable"]
    record["unratified_checks"] = [r["label"] for r in unratified]
    record["not_evaluated_checks"] = [r["label"] for r in skipped]
    record["not_comparable_checks"] = [r["label"] for r in incomparable]
    if failures:
        record["verdict"] = "fail"
    elif unratified or incomparable:
        record["verdict"] = "pass-with-open-checks"
    else:
        record["verdict"] = "pass"
    record["evaluated"] = True
    record["exit_code"] = gate_exit_code(record["verdict"])
    _write_result(outdir, gate_id, record)
    return record


def min_runs_check(gate, document, settings):
    """Protocol check: enough repetitions for the result to mean something."""
    scenarios = settings["scenarios"]
    required = int(gate["min_runs_per_scenario"])
    counts = {}
    for run in document.get("runs", []):
        counts[run.get("scenario")] = counts.get(run.get("scenario"), 0) + 1
    short = {
        name: counts.get(name, 0)
        for name in scenarios
        if counts.get(name, 0) < required
    }
    return {
        "label": f"repetitions per scenario (>= {required})",
        "metric": "runs[*].scenario",
        "aggregate": "count",
        "op": "<",
        "value": 0,
        "threshold": required,
        "observed": counts,
        "verdict": "fail" if short else "pass",
        "source": (
            "docs/profiles/rendering/2026-10-18/a0c-timeline-scrub-summary.json"
            "#single_pair=true (one reference/candidate pair is not sufficient)"
        ),
        "reason": "" if not short else f"insufficient repetitions: {short}",
    }


def environment_summary(document):
    env = document.get("environment") or {}
    if not env and document.get("runs"):
        env = document["runs"][0].get("environment") or {}
    backend = None
    if document.get("runs"):
        backend = document["runs"][0].get("renderer_status")
    return {
        "platform": env.get("platform"),
        "machine": env.get("machine"),
        "host": env.get("host"),
        "qt_platform": env.get("qt_platform"),
        "qt": env.get("qt"),
        "dpr": env.get("dpr"),
        "backend": backend,
    }


def snapshot_summary(document):
    workload = document.get("workload") or {}
    sha = workload.get("snapshot_sha256") or document.get("snapshot_sha256")
    if sha:
        return {
            "sha256": sha,
            "files": workload.get("snapshot_files"),
            "forecast_time": workload.get("forecast_time"),
            "known_fixture": sha in gates.FIXTURES,
        }
    return {}


def _write_result(outdir, gate_id, record):
    path = outdir / f"{gate_id}.result.json"
    path.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    record["result_file"] = str(path)


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------
def violating_value(check):
    """A value that breaks the check, for the self test."""
    operator = check["op"]
    if operator == "is_true":
        return False
    if operator == "is_false":
        return True
    if "value" not in check or isinstance(check.get("value"), dict):
        return None
    threshold = check["value"]
    if isinstance(threshold, str):
        return threshold + "-violated" if operator in ("==",) else threshold
    if operator == "<=":
        return threshold + 1
    if operator == "<":
        return threshold
    if operator == ">=":
        return threshold - 1
    if operator == ">":
        return threshold
    if operator == "==":
        return threshold + 1
    if operator == "!=":
        return threshold
    return None


def synthetic_document(check):
    """Build a minimal document whose metric path resolves to a failing value."""
    value = violating_value(check)
    if value is None:
        return None
    aggregate_kind = check.get("aggregate", "max")
    match = check.get("match")
    segments = parse_path(check["metric"])
    if aggregate_kind == "list_length":
        threshold = check.get("value")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            return None
        needed = threshold if check["op"] == "<" else threshold + 1
        needed = max(int(needed), 1)
        return assemble(segments, [0] * needed, check.get("where"))
    if aggregate_kind.startswith("count"):
        threshold = check.get("value")
        operator = check["op"]
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            return None
        needed = threshold if operator == "<" else threshold + 1
        needed = max(int(needed), 1)
        if aggregate_kind == "count_eq":
            value = match
        elif aggregate_kind == "count_gt":
            value = match + 1
        elif aggregate_kind == "count_ge":
            value = match
        elif aggregate_kind == "count_lt":
            value = match - 1
        elif aggregate_kind == "count_le":
            value = match
        return assemble_count(segments, value, needed, check.get("where"))
    return assemble(segments, value, check.get("where"))


def assemble(segments, value, where):
    """Build the nested mapping/list structure for a metric path."""
    node = value
    element = None
    for name, index in reversed(segments):
        if index is None:
            node = {name: node}
        elif index == "*":
            if where is not None and element is None:
                element = node
            node = {name: [node]}
        else:
            if where is not None and element is None:
                element = node
            node = {name: [None] * index + [node]}
    if where is not None and element is None and isinstance(node, dict):
        element = node
    if where is not None and isinstance(element, dict):
        element.update(where)
    return node


def assemble_count(segments, value, needed, where):
    """Build a document whose innermost list holds ``needed`` matching leaves."""
    wildcard = None
    for position, (_name, index) in enumerate(segments):
        if index == "*":
            wildcard = position
    if wildcard is None:
        return None
    inner = assemble(segments[wildcard + 1 :], value, where)
    node = inner
    for name, index in reversed(segments[: wildcard + 1]):
        if index is None:
            node = {name: node}
        elif index == "*":
            node = {name: [node] * needed}
        else:
            node = {name: [None] * index + [node]}
    return node


def self_test(gate_ids):
    """Prove the runner fails (non-zero) when a threshold is violated."""
    failures = []
    for gate_id in gate_ids:
        gate = gates.GATES[gate_id]
        proved = False
        for check in gate["checks"]:
            document = synthetic_document(check)
            if document is None:
                continue
            result = evaluate_check(check, document)
            if result["verdict"] != "fail":
                failures.append(
                    f"{gate_id}: {check['label']} did not fail on a violating value"
                )
                continue
            proved = True
            break
        if not proved:
            failures.append(f"{gate_id}: no synthesizable check to violate")
            continue
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--gate",
                gate_id,
                "--self-test-violate",
                "--output",
                tempfile.mkdtemp(prefix="gwr-selftest-"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            failures.append(
                f"{gate_id}: runner exited 0 on a violated threshold "
                f"(stdout: {completed.stdout.strip()})"
            )
        else:
            print(
                f"  {gate_id}: violated '{check['label']}' -> runner exit "
                f"{completed.returncode} ({completed.stdout.strip()})"
            )
    if failures:
        for failure in failures:
            print(f"SELF-TEST FAIL {failure}", file=sys.stderr)
        return EXIT_SELFTEST
    print(f"SELF-TEST PASS {len(gate_ids)} gates exit non-zero on a violated threshold")
    return EXIT_PASS


def violate_gate(gate_id):
    """Evaluate a gate against a synthetic violating report and exit non-zero."""
    gate = gates.GATES[gate_id]
    for check in gate["checks"]:
        document = synthetic_document(check)
        if document is None:
            continue
        results = evaluate({"checks": [check]}, document)
        if results[0]["verdict"] == "fail":
            print(
                f"{VERDICT_LABELS[VERDICT_FAIL]:<22} {gate_id}  {gate['milestone']}  "
                f"[self-test] {check['label']}: {results[0].get('observed')} "
                f"{check['op']} {results[0].get('threshold')}"
            )
            return EXIT_FAIL
    print(
        f"{VERDICT_LABELS[VERDICT_FAIL]:<22} {gate_id}  {gate['milestone']}  "
        "[self-test] no violating value"
    )
    return EXIT_FAIL


# --------------------------------------------------------------------------
# listing and reporting
# --------------------------------------------------------------------------
def list_gates():
    print(
        f"{'gate':<18} {'issue':<6} {'kind':<12} {'tool':<38} "
        f"{'reference':<18} notes"
    )
    for gate_id, gate in sorted(gates.GATES.items()):
        notes = []
        if gate_id in gates.GATES_NEEDING_RATIFICATION:
            notes.append("needs-ratification")
        if gate.get("open_requirements"):
            notes.append(f"{len(gate['open_requirements'])} open requirement(s)")
        if gate.get("min_runs_per_scenario"):
            notes.append(f"min-runs={gate['min_runs_per_scenario']} in --full")
        if gate.get("baseline_checks"):
            notes.append("optional --reference comparison")
        print(
            f"{gate_id:<18} {gate['milestone']:<6} {gate['kind']:<12} "
            f"{gate['tool']:<38} {gate['reference']['kind']:<18} "
            f"{', '.join(notes) or '-'}"
        )
        print(
            f"{'':<18} {gate['title']}  |  scenario: "
            f"{','.join(gate['defaults']['scenarios'])}  |  endpoint: "
            f"{gate['endpoint']}"
        )
    print(
        f"\n{len(gates.GATES)} gates.  Run one with: "
        "python tools/gate_runner.py --gate <gate-id> [--snapshot PATH] "
        "[--output DIR]"
    )


def format_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if value == 0:
        return 0
    if abs(value) < 1e-3:
        return f"{value:.3g}"
    return round(value, 4)


def format_check(result):
    observed = result.get("observed")
    threshold = result.get("threshold")
    observed = format_number(observed)
    threshold = format_number(threshold)
    label = result["label"]
    verdict = result["verdict"]
    if verdict == "not-evaluated":
        # A single check that could not be run inside an evaluated gate.  It
        # is worded differently from the gate-level NOT-EVALUATED verdict so
        # the two can never be confused.
        return f"{label}=skipped"
    if verdict == "not-comparable":
        return f"{label}=not-comparable"
    if verdict == "unratified":
        return f"{label}=unratified"
    if result["op"] in ("is_true", "is_false"):
        body = f"{label}={observed}"
    elif threshold is None:
        body = f"{label}={observed}"
    else:
        body = f"{label}={observed} {result['op']} {threshold}"
    if verdict == "fail":
        return f"!{body}"
    return body


def gate_exit_code(verdict):
    """The exit code a single gate's verdict implies."""
    if verdict == VERDICT_FAIL:
        return EXIT_FAIL
    if verdict == VERDICT_NOT_EVALUATED:
        return EXIT_NOT_EVALUATED
    return EXIT_PASS


def summary_line(record):
    verdict = VERDICT_LABELS.get(record["verdict"], record["verdict"].upper())
    parts = [format_check(result) for result in record.get("checks", [])]
    if record["verdict"] == VERDICT_NOT_EVALUATED:
        body = record.get("reason", "the gate was not evaluated")
    else:
        body = "; ".join(parts) if parts else record.get("reason", "")
    extras = []
    if record.get("unratified_checks"):
        extras.append(f"{len(record['unratified_checks'])} unratified")
    if record.get("not_evaluated_checks"):
        extras.append(f"{len(record['not_evaluated_checks'])} check(s) skipped")
    if record.get("not_comparable_checks"):
        extras.append(f"{len(record['not_comparable_checks'])} not comparable")
    if record.get("open_requirements"):
        extras.append(f"{len(record['open_requirements'])} open requirement(s)")
    if record.get("fixture_match") is False:
        extras.append("fixture differs from the recorded evidence")
    tail = f"  [{record['endpoint']}]"
    if extras:
        tail = f"  ({'; '.join(extras)})" + tail
    return (
        f"{verdict:<22} {record['gate']}  {record['milestone']}  "
        f"[{record['kind']}]  {body}{tail}  {record.get('duration_s', 0)}s"
    )


class UsageParser(argparse.ArgumentParser):
    """Argument parser whose usage errors do not collide with a gate verdict.

    argparse exits 2 on a usage error, and 2 is the runner's
    "NOT-EVALUATED" code.  A usage error is a different thing entirely, so it
    gets its own code.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def main(argv=None):
    parser = UsageParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--gate",
        action="append",
        metavar="GATE-ID",
        help="gate to run; repeat for several",
    )
    parser.add_argument("--list", action="store_true", help="list the gates and exit")
    parser.add_argument(
        "--snapshot", type=Path, help="verified startup snapshot fixture"
    )
    parser.add_argument("--output", type=Path, help="directory for reports and results")
    parser.add_argument(
        "--reference",
        type=Path,
        help="baseline artifact for gates whose reference is a measured baseline",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="opt in to the full scenario matrix (never the default)",
    )
    parser.add_argument("--frames", type=int, help="override the frame count")
    parser.add_argument("--repeats", type=int, help="override the repeat count")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="prove the runner exits non-zero on a violated threshold",
    )
    parser.add_argument(
        "--self-test-violate",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--self-test-gate",
        metavar="GATE-ID",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if args.self_test_violate:
        gate_id = args.self_test_gate or (args.gate[0] if args.gate else None)
        if not gate_id or gate_id not in gates.GATES:
            parser.error("--self-test-violate needs a known --gate")
        return violate_gate(gate_id)
    if args.list:
        list_gates()
        return EXIT_PASS
    if args.self_test:
        gate_ids = args.gate or sorted(gates.GATES)
        unknown = [gate_id for gate_id in gate_ids if gate_id not in gates.GATES]
        if unknown:
            parser.error(f"Unknown gate(s): {', '.join(unknown)}")
        return self_test(gate_ids)
    if not args.gate:
        parser.error("--gate is required (or use --list)")
    unknown = [gate_id for gate_id in args.gate if gate_id not in gates.GATES]
    if unknown:
        parser.error(
            f"Unknown gate(s): {', '.join(unknown)}.  Known: "
            f"{', '.join(sorted(gates.GATES))}"
        )
    # Resolve the interpreter before any tool runs.  A gate whose tool cannot
    # import its dependencies was never evaluated; it must not be reported as
    # a threshold failure.
    if not os.environ.get(REEXEC_FLAG):
        needed = sorted(
            {
                module
                for gate_id in args.gate
                for module in gates.GATES[gate_id].get("dependencies", [])
            }
        )
        interpreter, missing = resolve_interpreter(needed)
        if interpreter is not None and os.path.realpath(
            str(interpreter)
        ) != os.path.realpath(sys.executable):
            reexec_under(interpreter)
    exit_code = EXIT_PASS
    not_evaluated = False
    for gate_id in args.gate:
        gate = gates.GATES[gate_id]
        missing = interpreter_missing(sys.executable, gate.get("dependencies", []))
        if missing:
            reason = (
                "the gate was not evaluated: the interpreter "
                f"{sys.executable} cannot import {', '.join(missing)}; run the "
                "gate with the project virtualenv interpreter "
                f"({project_interpreter_hint()})"
            )
            record = not_evaluated_record(gate_id, args, reason)
            print(summary_line(record))
            not_evaluated = True
            continue
        try:
            record = run_gate(gate_id, args)
        except (OSError, ValueError) as exc:
            record = not_evaluated_record(
                gate_id, args, f"the gate was not evaluated: {exc}"
            )
            print(summary_line(record))
            not_evaluated = True
            continue
        print(summary_line(record))
        if record["verdict"] == VERDICT_FAIL:
            exit_code = EXIT_FAIL
        elif record["verdict"] == VERDICT_NOT_EVALUATED:
            not_evaluated = True
    if exit_code == EXIT_PASS and not_evaluated:
        exit_code = EXIT_NOT_EVALUATED
    return exit_code


def project_interpreter_hint():
    """The interpreter a user should run the gate with."""
    for candidate in find_project_interpreters():
        return str(candidate)
    return "python -m pip install -e . with the project dependencies"


if __name__ == "__main__":
    sys.exit(main())
