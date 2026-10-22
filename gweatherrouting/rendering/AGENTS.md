# Renderer and viewport work

Applies to files in this directory.

## Read only what the task needs

- `docs/adr-001-grib-rendering-architecture.md` - the accepted decision (D1-D7)
  and the disposition of the structural gaps. Read the section your task
  touches, not the whole file.
- `docs/rendering-performance-plan.md` - current state, the invariants, the
  acceptance gates and the one next decision. The archived checkpoint notes
  and the A0 decision log are in `docs/history/`; read them only when the task
  asks *why* a decision was made.
- `docs/rendering-implementation-plan.md` - the layer/ownership model, the
  R0-R5 sequence and the measurement contract.
- `docs/profiles/rendering/` - recorded evidence. It is the reference: do not
  re-measure a baseline the change does not touch.

## Rules

- Gate a change with one command from the runner built in #63
  (`tools/gate_runner.py`). Do not re-run the full test suite.
- Before an experiment, state the scope and non-goals, the baseline and
  workload, the measurement and fidelity gates, the time/attempt budget and
  the stop condition. State them in the issue body, not in a new file. Stop
  and report when the budget is exhausted or the scope must expand.
- Record the result and the adopt/revise/reject decision in the issue body.
  Correctness tests, synthetic mechanism results and real-workload performance
  acceptance are separate. Never call grab/readback timing presentation FPS.
- At each gate, reevaluate the bottleneck and record continue/narrow/pivot/stop.
  Do not start a provisional later milestone by default.
- Keep explicit loading, unsupported and fallback states. No stale or clipped
  content may masquerade as current.
