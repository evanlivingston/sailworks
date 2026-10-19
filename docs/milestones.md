# Architecture program — issue index

**The authoritative tracker is GitHub.** This file is a local pointer, not a second
source of truth. Update the issues, not this file.

**Epic:** [#51 — evidence-gated renderer modernization](https://github.com/evanlivingston/gweatherrouting/issues/51)

## Issues

| Issue | Title | Milestone | Depends on |
| --- | --- | --- | --- |
| [#62](https://github.com/evanlivingston/gweatherrouting/issues/62) | ADR: target GRIB rendering architecture | M0 | — |
| [#63](https://github.com/evanlivingston/gweatherrouting/issues/63) | Tooling: one-command performance gate runner | M0 | #62 |
| [#64](https://github.com/evanlivingston/gweatherrouting/issues/64) | GPU-resident numerical field cache | M1 | #62, #63 |
| [#65](https://github.com/evanlivingston/gweatherrouting/issues/65) | GPU spatial interpolation | M2 | #64 |
| [#66](https://github.com/evanlivingston/gweatherrouting/issues/66) | Async double-buffered pipeline | M2 | #64 |
| [#67](https://github.com/evanlivingston/gweatherrouting/issues/67) | Instant forecast-time scrubbing | M2 | #64, #66 |
| [#68](https://github.com/evanlivingston/gweatherrouting/issues/68) | Production GPU weather fill | M2 | #64, #65 |
| [#69](https://github.com/evanlivingston/gweatherrouting/issues/69) | Chart/weather layer separation, then remaining layers | M2/M3 | #62, #63 |
| [#70](https://github.com/evanlivingston/gweatherrouting/issues/70) | Validation, soak and rollout decision | M4 | #64–#69 |

## Evidence

| Document | Purpose |
| --- | --- |
| [`grib-architecture-analysis.md`](grib-architecture-analysis.md) | Current-state architecture with file/line citations; the seven structural gaps. |
| [`reference-architecture-analysis.md`](reference-architecture-analysis.md) | Reference techniques T1–T8 with evidence labels and coverage limits. |
| [`rendering-performance-plan.md`](rendering-performance-plan.md) | Canonical performance roadmap; ratified targets. |
| [`rendering-architecture-evaluation.md`](rendering-architecture-evaluation.md) | A0a–A0k findings, including negative results. |
| [`gpu-weather-residency.md`](gpu-weather-residency.md) | What #50's bounded residency delivers, and its explicit limits. |
| `profiles/rendering/2026-10-18/`, `profiles/rendering/2026-10-19/` | Recorded machine-readable results. These are the reference baselines — do not re-measure them. |

## Standing rules for agents

1. **One command to gate.** Use the runner from #63. Do not run the full test suite.
2. **Feature flag, default OFF.** The default path must stay byte-identical.
3. **Never relax a threshold.** The 2e-5 numerical gate, the A0k zero-alpha
   missing-data gate, and the existing barbs selection/atlas gates are fixed.
4. **Preserve negative results** with their reasons.
5. **One blocker report, then stop.** File the ≤150-word report in the issue.
6. **Correctness, lifecycle and performance are separate gates.**
