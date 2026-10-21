"""Gate registry for ``tools/gate_runner.py``.

This module is **data only**.  It imports nothing beyond the standard library,
so ``python tools/gate_runner.py --list`` stays instant and side-effect free:
no GDAL, no Qt and no application import happens at module import time.

Every gate records

``milestone``
    The issue, or the issues, the gate protects (``#64`` .. ``#70``).
``kind``
    ``correctness``, ``fidelity``, ``lifecycle``, ``performance`` or
    ``validation``.  Correctness, lifecycle and performance stay separate
    gates; one never substitutes for another.
``tool``
    The **existing** tool that owns the measurement.  The runner composes it
    and never re-implements or replaces it.
``command``
    argv template for that tool.  ``{tool}`` is the tool, ``{tool_output}`` the
    path it writes to, ``{snapshot}`` the caller supplied fixture, and
    ``{scenarios}``/``{frames}``/``{repeats}`` the workload.
``dependencies``
    The modules the composed tool needs.  The runner probes the interpreter
    for them **before** the tool is invoked, so a gate that cannot run is
    reported as not evaluated rather than as a threshold failure.
``checks``
    Threshold evaluation against the JSON the tool emits.  Each check names a
    metric path, an aggregate, a comparison and the threshold.
``reference``
    Where the threshold comes from.  ``kind`` is one of

    ``recorded-evidence``
        A number that already exists in this repository: a recorded summary
        under ``docs/profiles/rendering/`` or a written contract in ``docs/``.
        The value is fixed in the registry and is never re-measured.
    ``measured-baseline``
        A number produced by a prior run of the same tool on the same
        fixture.  The caller supplies that artifact with ``--reference``; the
        runner refuses to re-measure it on its own and says so.

Threshold provenance is spelled out per check in ``checks[*].source`` so a
reviewer can trace every number back to a recorded document.

Thresholds that are **not** derivable from recorded evidence are not invented.
They are declared with ``"ratified": False`` and are reported as unratified:
the gate still runs and still enforces its ratified checks, but the result
records that a bound is missing and the agent must ratify one.
"""

# Recorded evidence fixtures.  The runner records the fixture identity with
# every result so a number is never compared across workloads silently.
FIXTURES = {
    "97cfda3c62a494c3d462ee8cde449d4d80ed1c62f387d70edcd9192dfb65d360": (
        "A0c/A0h/A0g recorded evidence fixture, 2026-10-16 08:00, 4569 files "
        "(33 forecasts). Recorded under docs/profiles/rendering/2026-10-18/."
    ),
    "1ed1bca1281ad4096713ce20af9c1473e70bd4eb0766a54965849b7ace698a03": (
        "A0j/A0k current fixture, 2026-10-19 08:00, 5693 files (22 sources). "
        "Recorded under docs/profiles/rendering/2026-10-19/."
    ),
}

# Endpoint labels.  A render callback is a proxy; a readback is fidelity.  No
# endpoint in this repository measures presentation.
ENDPOINT_PROBE = (
    "QQuickWindow.afterRendering render-callback proxy (embedded offscreen "
    "scene). NOT presentation FPS, NOT input-to-present, NOT scanout."
)
ENDPOINT_GRAB = (
    "Synchronous QWidget.grab()/readback probe. NOT presentation FPS, NOT "
    "input-to-present, NOT scanout."
)
ENDPOINT_FIDELITY = (
    "Framebuffer readback compared against an independent CPU oracle. "
    "Fidelity only; the readback duration is not a timing measurement."
)

GATES = {
    "enc-viewport": {
        "title": "Complete faithful ENC charts in the experimental viewport",
        "milestone": "#78 under #51 M3",
        "kind": "fidelity",
        "tool": "tools/check_enc_viewport.py",
        "endpoint": ENDPOINT_PROBE,
        "requires": ["snapshot"],
        "dependencies": ["cairo", "numpy", "osgeo.gdal", "PySide6"],
        "timeout_s": 110,
        "env": {"QT_QPA_PLATFORM": "cocoa", "QSG_RHI_BACKEND": "metal"},
        "command": [
            "{python}",
            "{tool}",
            "--snapshot",
            "{snapshot}",
            "--output",
            "{tool_output}",
        ],
        "defaults": {
            "scenarios": ["san-francisco", "puget", "multi-cell"],
            "frames": 10,
            "repeats": 2,
        },
        "tool_output": "{outdir}/{gate}.tool.json",
        "result": "{outdir}/{gate}.tool.json",
        "reference": {
            "kind": "recorded-evidence",
            "source": "docs/enc-viewport-integration.md; issue #78; R0.1",
        },
        "checks": [
            {
                "label": name.replace("_", " "),
                "metric": "checks." + name,
                "aggregate": "all",
                "op": "is_true",
                "source": "docs/enc-viewport-integration.md; issue #78",
            }
            for name in (
                "fixture_verified",
                "detailed_enc_present",
                "source_selection_exact",
                "multi_cell_coverage_exercised",
                "zero_alpha_preserved",
                "annotations_faithful",
                "native_metal_dpr2",
                "native_chart_nonblank",
                "stale_chart_hidden",
                "camera_weather_advance_while_chart_stalled",
                "latest_generation_only",
                "resources_bounded_and_released",
            )
        ]
        + [
            {
                "label": label,
                "metric": "fidelity[*]." + metric,
                "aggregate": aggregate,
                "op": op,
                **({"value": value} if value is not None else {}),
                "source": "docs/rendering-implementation-plan.md R0.1; issue #78",
            }
            for label, metric, aggregate, op, value in (
                ("ENC image MAE <=1/255", "mae_255", "max", "<=", 1.0),
                ("ENC image p99 <=8/255", "p99_255", "max", "<=", 8.0),
                ("ENC readback is nonblank", "nonblank", "all", "is_true", None),
                (
                    "ENC silhouette precision >=99%",
                    "precision",
                    "min",
                    ">=",
                    0.99,
                ),
                ("ENC silhouette recall >=99%", "recall", "min", ">=", 0.99),
                (
                    "ENC alpha coverage >=95%",
                    "coverage",
                    "min",
                    ">=",
                    0.95,
                ),
                (
                    "ENC alpha coverage <=105%",
                    "coverage",
                    "max",
                    "<=",
                    1.05,
                ),
                (
                    "annotation coverage >=95%",
                    "annotations.coverage",
                    "min",
                    ">=",
                    0.95,
                ),
                (
                    "annotation coverage <=105%",
                    "annotations.coverage",
                    "max",
                    "<=",
                    1.05,
                ),
            )
        ],
        "open_requirements": [
            "Experimental integration is not a default rollout; presentation, platform matrix, RSS/VRAM and complete catalogue coverage remain separate."
        ],
    },
    "viewport-interaction": {
        "title": "Independent GRIB viewport sustained interaction",
        # The interaction contract of #64, the structural observations of
        # #66's overlapped preparation, the continuity of #67's resident time
        # window, #68's numerical fill layer and #69's chart layer.
        "milestone": "#64/#66/#67/#68/#69",
        "kind": "correctness",
        "tool": "test/qt_gribviewport_checks.py",
        "endpoint": ENDPOINT_PROBE,
        "requires": ["snapshot"],
        "dependencies": ["PySide6", "numpy", "osgeo.gdal"],
        "timeout_s": 110,
        "env": {},
        "command": [
            "{python}",
            "{tool}",
            "--snapshot",
            "{snapshot}",
            "--output",
            "{tool_output}",
            "--repeats",
            "{repeats}",
            "--sections",
            "{sections}",
        ],
        "defaults": {
            "scenarios": ["pan-zoom-time"],
            "frames": 120,
            "repeats": 1,
            "sections": "cold_load,coverage_miss,interval_crossing",
        },
        "full": {
            "scenarios": ["pan-zoom-time"],
            "frames": 120,
            "repeats": 2,
            "sections": "cold_load,coverage_miss,interval_crossing,matrix,platform",
        },
        "tool_output": "{outdir}/{gate}.tool.json",
        "result": "{outdir}/{gate}.tool.json",
        "reference": {
            "kind": "recorded-evidence",
            "source": "docs/rendering-performance-plan.md: Try-out gate contract",
        },
        "checks": [
            {
                "label": label,
                "metric": metric,
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #66 experiment contract (structural observation, "
                    "not a timing threshold)"
                    if metric
                    in (
                        "prepared_while_presenting",
                        "superseded_never_published",
                        "generation_mismatch_rejected",
                    )
                    else (
                        "issue #66's overlapped, double-buffered preparation "
                        "pipeline and issue #67's resident time window "
                        "(docs/profiles/rendering/2026-10-19/"
                        "viewport-time-window-summary.json): a covered camera "
                        "move is served from the resident field and a "
                        "forecast-time change inside the window is served from "
                        "the resident store, so every interaction input finds "
                        "current weather. Behavioral invariant, not a timing "
                        "threshold."
                        if metric == "continuous_weather"
                        else (
                            "docs/rendering-performance-plan.md: Try-out gate "
                            "contract (behavioral invariant, not a timing threshold)"
                        )
                    )
                ),
            }
            for label, metric in (
                ("pan, zoom and time settle without stale content", "complete"),
                ("selected forecast order preserved", "source_order_preserved"),
                (
                    "camera accepts input during stalled preparation",
                    "stalled_camera_responsive",
                ),
                ("miss shows loading, not stale weather", "honest_loading"),
                ("retained payload budgets respected", "retained_budgets_respected"),
                ("pending close stops worker", "worker_stopped"),
                (
                    "weather stays current through the overlapped preparation "
                    "and the resident time window",
                    "continuous_weather",
                ),
                (
                    "preparation overlaps presentation",
                    "prepared_while_presenting",
                ),
                (
                    "superseded preparation never publishes",
                    "superseded_never_published",
                ),
                (
                    "generation mismatch is rejected",
                    "generation_mismatch_rejected",
                ),
            )
        ]
        + [
            {
                "label": "the numerical fill's shader is ready",
                "metric": "fill.shader_ready",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #68: the fill renders from the resident numerical "
                    "field (docs/profiles/rendering/2026-10-19/"
                    "a0k-shader-safety-summary.json #initial_selected_symbols="
                    "1053 with the numerical shader active). A behavioural "
                    "invariant, not a timing threshold."
                ),
            },
            {
                "label": "the numerical fill paints the resident field",
                "metric": "fill.painted_pixels",
                "aggregate": "max",
                "op": ">",
                "value": 0,
                "source": (
                    "issue #68: the fill renders from the resident numerical "
                    "field. Presence of painted pixels, not a threshold; the "
                    "recorded A0k rule is that a finite control is painted "
                    "everywhere (docs/profiles/rendering/2026-10-19/"
                    "a0k-shader-safety-summary.json "
                    "#finite_control_pixels_unchanged=true)."
                ),
            },
            {
                "label": "an all-missing fill contributes exactly zero alpha pixels",
                "metric": "fill.all_missing.alpha_pixels",
                "aggregate": "max",
                "op": "==",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json"
                    "#missing_only_layers_exactly_transparent=true; "
                    "docs/history/rendering-a0k-shader-safety.md (missing-only "
                    "controls require exactly zero alpha pixels, not a low "
                    "fraction of errors)"
                ),
            },
            {
                "label": "an all-missing fill contributes no significant ink",
                "metric": "fill.all_missing.significant_alpha_pixels",
                "aggregate": "max",
                "op": "==",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json"
                    "#significant_alpha_threshold=25 (the recorded A0k "
                    "significance rule; the corrected run reports zero)"
                ),
            },
            {
                "label": "the finite control field is painted everywhere",
                "metric": "fill.finite_control.alpha_pixels",
                "aggregate": "max",
                "op": "==",
                "value": {
                    "metric": "fill.finite_control.total_pixels",
                    "aggregate": "max",
                },
                "source": (
                    "docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json"
                    "#finite_control_pixels_unchanged=true (the finite control "
                    "must be painted, not discarded)"
                ),
            },
            {
                "label": "the fill's wind speed matches the CPU reference",
                "metric": "fill.values.max_abs_error",
                "aggregate": "max",
                "op": "<=",
                "value": 2e-05,
                "source": (
                    "docs/history/rendering-m0-float-textures.md (unchanged 2e-5 "
                    "gate; E1b measured 2.384185791015625e-7 with explicit "
                    "texelFetch arithmetic and E1 hardware filtering measured "
                    "0.002604166666666824 and failed). The comparison reads "
                    "the float32 the application's own weather shader wrote, "
                    "at probe-chosen positions, against the CPU reference."
                ),
            },
            {
                "label": "a fill change does not re-stage the barbs",
                "metric": "fill.independence.barbs_publications_by_fill_change",
                "aggregate": "max",
                "op": "==",
                "value": 0,
                "source": (
                    "issue #68: fill and barbs must be independently updatable "
                    "- changing one does not re-stage the other (measured: no "
                    "additional uploads attributable to the unchanged layer). "
                    "An exact count, not a performance threshold."
                ),
            },
            {
                "label": "a fill change does not change the barbs' pixels",
                "metric": ("fill.independence.barbs_pixels_unchanged_by_fill_change"),
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #68: the barbs' selection, thinning, anchors, "
                    "colours and atlas bytes are unchanged by a fill change. "
                    "A behavioural invariant, not a timing threshold."
                ),
            },
            {
                "label": "a selection change does not re-stage the fill",
                "metric": "fill.independence.fill_publications_by_selection_change",
                "aggregate": "max",
                "op": "==",
                "value": 0,
                "source": (
                    "issue #68: fill and barbs must be independently updatable "
                    "- changing one does not re-stage the other (measured: no "
                    "additional uploads attributable to the unchanged layer). "
                    "An exact count, not a performance threshold."
                ),
            },
            {
                "label": (
                    "the fill's value at a screen position matches the CPU "
                    "reference at that position's geography"
                ),
                "metric": "fill.placement.max_abs_error",
                "aggregate": "max",
                "op": "<=",
                "value": 2e-05,
                "source": (
                    "docs/history/rendering-m0-float-textures.md (unchanged 2e-5 "
                    "gate) and tools/experiment_float_textures.py's "
                    "bilinear_reference: the CPU reference is the CPU's own "
                    "interpolation of the same field. Issue #65 anchors that "
                    "comparison at the geographic position of the fragment, so "
                    "the fill's raster is proved to be placed on the map."
                ),
            },
            {
                "label": "a missing lattice node is not filled from a neighbour",
                "metric": "fill.missing_neighbour.touching_cell_missing",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json"
                    "#missing_only_layers_exactly_transparent=true; "
                    "docs/history/rendering-a0k-shader-safety.md (a missing "
                    "sample is never filled from a neighbour; it contributes "
                    "exactly zero alpha)"
                ),
            },
            {
                "label": "the fill away from a missing node is still painted",
                "metric": "fill.missing_neighbour.other_cell_missing",
                "aggregate": "max",
                "op": "==",
                "value": False,
                "source": (
                    "issue #65: the missing rule is local. A cell away from "
                    "the missing node is still painted, so the observation is "
                    "the locality of the rule, not a performance threshold."
                ),
            },
            {
                "label": (
                    "the cell of a missing lattice node contributes exactly "
                    "zero alpha pixels"
                ),
                "metric": "fill.missing_neighbour.node_cell_alpha_pixels",
                "aggregate": "max",
                "op": "==",
                "value": 0,
                "source": (
                    "issue #65: the A0k rule read in the framebuffer itself, "
                    "not only in the byte-exact readback. The missing node's "
                    "own cell is measured in the framebuffer's own device "
                    "scale, and it must contribute no alpha at all: a missing "
                    "sample is never filled from a neighbour "
                    "(docs/profiles/rendering/2026-10-19/"
                    "a0k-shader-safety-summary.json "
                    "#missing_only_layers_exactly_transparent=true)."
                ),
            },
            {
                "label": "the weather is presented before the chart layer is ready",
                "metric": "chart.weather_before_chart",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #69: chart readiness is not a prerequisite for "
                    "weather presentation. The observation compares the "
                    "viewport's own record of the weather's first ready "
                    "presentation with the chart layer's first publication: "
                    "the weather is presented first. A behavioural invariant, "
                    "not a timing threshold."
                ),
            },
            {
                "label": "the chart is requested only after the weather is presented",
                "metric": "chart.chart_requested_after_weather",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #69: the chart layer is an independent generation "
                    "and it never waits for the weather. A behavioural "
                    "invariant, not a timing threshold."
                ),
            },
            {
                "label": "the chart layer has its own explicit loading state",
                "metric": "chart.chart_loading_state_explicit",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #69: the chart layer needs its own explicit "
                    "loading state, as the viewport already has one for the "
                    "weather. A behavioural invariant, not a timing threshold."
                ),
            },
            {
                "label": "the camera survives the chart's publication",
                "metric": "chart.camera_preserved",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #69 and docs/history/rendering-a0j-early-display.md "
                    "(adding charts preserved the field and camera): the "
                    "camera is recorded when the chart is requested and when "
                    "it is published, and it is the same. A behavioural "
                    "invariant, not a timing threshold."
                ),
            },
            {
                "label": "the forecast time survives the chart's publication",
                "metric": "chart.forecast_time_preserved",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #69 and docs/history/rendering-a0j-early-display.md "
                    "(adding charts preserved the field and camera): the "
                    "forecast time is recorded when the chart is requested "
                    "and when it is published, and it is the same. A "
                    "behavioural invariant, not a timing threshold."
                ),
            },
            {
                "label": "the weather survives the chart's publication",
                "metric": "chart.weather_preserved",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #69 and docs/history/rendering-a0j-early-display.md "
                    "(adding charts preserved the field and camera): the "
                    "published weather generation is the same before and "
                    "after the chart's publication. A behavioural invariant, "
                    "not a timing threshold."
                ),
            },
            {
                "label": "a superseded chart generation never publishes",
                "metric": "chart_supersession.superseded_never_published",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #69: chart composition and weather generation are "
                    "independently cancellable and replaceable, exactly as "
                    "the field pipeline guarantees for fields (the same "
                    "contract as this gate's superseded_never_published "
                    "observation for the field). The probe replaces a "
                    "composing chart generation and the older generation may "
                    "never publish. A behavioural invariant, not a timing "
                    "threshold."
                ),
            },
            # ------------------------------------------------ #70 per layer
            {
                "label": "the fill layer is presented by the GPU's shader",
                "metric": "layers.fill.gpu",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #70: every layer reports its own GPU, loading and "
                    "fallback state, read from the layer's own QML object. "
                    "The fill's state is true when its ShaderEffect is "
                    "compiled and its images are loaded. A behavioural "
                    "invariant, not a timing threshold."
                ),
            },
            {
                "label": "the barbs layer is presented by the GPU's shader",
                "metric": "layers.barbs.gpu",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #70: the barbs' own state, read from the barbs' "
                    "QML object, not the viewport's guess. A behavioural "
                    "invariant, not a timing threshold."
                ),
            },
            {
                "label": "the chart layer is composited by the scene graph",
                "metric": "layers.chart.gpu",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #70: the chart's composed surface reaches the "
                    "scene graph as an image and is composited by the GPU. A "
                    "behavioural invariant, not a timing threshold."
                ),
            },
            {
                "label": "the fill layer reports its loading state explicitly",
                "metric": "layers.fill.loading_state_explicit",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #70: the fill is published from the same resident "
                    "field as the barbs, so the viewport's weather state is "
                    "its loading state, and that state must be explicit: the "
                    "status is shown and the stale weather is hidden. A "
                    "behavioural invariant, not a timing threshold."
                ),
            },
            {
                "label": "the chart layer reports its own loading state",
                "metric": "layers.chart.loading_state_explicit",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #69 and issue #70: the chart layer has its own "
                    "explicit loading state and never uses the weather's. A "
                    "behavioural invariant, not a timing threshold."
                ),
            },
            {
                "label": "the fill layer did not fall back",
                "metric": "layers.fill.fallback_active",
                "aggregate": "all",
                "op": "is_false",
                "source": (
                    "issue #70: no layer of the viewport has a per-layer CPU "
                    "fallback, so a fallback on a layer would be an "
                    "unexpected silent change of path. The retained CPU "
                    "fallback is the application's CPU map renderer, which "
                    "the validation gate proves separately."
                ),
            },
            {
                "label": "the barbs layer did not fall back",
                "metric": "layers.barbs.fallback_active",
                "aggregate": "all",
                "op": "is_false",
                "source": (
                    "issue #70: the barbs are on the GPU path; a silent "
                    "fallback would change the portrayal without saying so."
                ),
            },
            {
                "label": "the chart layer did not fall back",
                "metric": "layers.chart.fallback_active",
                "aggregate": "all",
                "op": "is_false",
                "source": (
                    "issue #70: the chart is composited by the scene graph; a "
                    "silent fallback would change the portrayal without "
                    "saying so."
                ),
            },
            # ------------------------------------------- #70 separate limits
            {
                "label": "the cold load reaches a ready presentation",
                "metric": "limits.cold_load.ready",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #70: the cold load is its own limit, measured in "
                    "its own fresh session: a cold field preparation and the "
                    "first ready presentation. A behavioural invariant, not a "
                    "timing threshold."
                ),
            },
            {
                "label": "a coverage miss shows loading, not stale weather",
                "metric": "limits.coverage_miss.attempts[*].stale_weather_hidden",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "issue #70: a demand outside the resident field's window "
                    "shows the loading state and hides the stale weather. "
                    "This is the same honest-loading contract the interaction "
                    "gate already checks for a blocked preparation, applied to "
                    "a spatial miss. A behavioural invariant, not a timing "
                    "threshold."
                ),
            },
            {
                "label": "a coverage miss recovers",
                "metric": "limits.coverage_miss.attempts[*].recovered",
                "aggregate": "any",
                "op": "is_true",
                "source": (
                    "issue #70: the viewport recovers from a coverage miss by "
                    "preparing the field the new camera needs. A behavioural "
                    "invariant, not a timing threshold."
                ),
            },
            {
                "label": "an interval crossing recovers",
                "metric": "limits.interval_crossing.attempts[*].recovered",
                "aggregate": "any",
                "op": "is_true",
                "source": (
                    "issue #70: a forecast instant beyond the resident time "
                    "window is prepared again and presented. A behavioural "
                    "invariant, not a timing threshold."
                ),
            },
            {
                "label": "the cold load limit (reported, not ratified)",
                "metric": "limits.cold_load.ms",
                "aggregate": "max",
                "op": "<=",
                "value": 0,
                "ratified": False,
                "source": (
                    "NOT DERIVABLE as a bound. The recorded cold preparation "
                    "is a measurement, not a limit: 792 ms "
                    "(docs/profiles/rendering/2026-10-19/prepare-breakdown.json) "
                    "and 324 ms for the second pass's own script "
                    "(docs/profiles/rendering/2026-10-19/"
                    "viewport-gridded-field-summary.json). No recorded "
                    "evidence sets an acceptance bound for the cold load, so "
                    "the placeholder 0 is never enforced and the number is "
                    "reported without affecting the verdict."
                ),
            },
            {
                "label": "the coverage-miss recovery limit (reported, not ratified)",
                "metric": "limits.coverage_miss.attempts[*].recovery_ms",
                "aggregate": "max",
                "op": "<=",
                "value": 0,
                "ratified": False,
                "source": (
                    "NOT DERIVABLE as a bound. The recorded preparation costs "
                    "(792 ms cold, 159 ms re-centred) are measurements of "
                    "different scripts and are not an acceptance bound for a "
                    "coverage miss. The placeholder 0 is never enforced and "
                    "the number is reported without affecting the verdict."
                ),
            },
            {
                "label": (
                    "the interval-crossing recovery limit (reported, not ratified)"
                ),
                "metric": "limits.interval_crossing.attempts[*].recovery_ms",
                "aggregate": "max",
                "op": "<=",
                "value": 0,
                "ratified": False,
                "source": (
                    "NOT DERIVABLE as a bound. No recorded evidence sets an "
                    "acceptance bound for a forecast instant beyond the "
                    "resident time window. The placeholder 0 is never enforced "
                    "and the number is reported without affecting the verdict."
                ),
            },
        ],
    },
    # ------------------------------------------------------------------ #64
    "residency": {
        "title": "GPU-resident numerical field cache",
        "milestone": "#64",
        "kind": "correctness",
        "tool": "tools/benchmark_startup_rendering.py",
        "endpoint": ENDPOINT_PROBE,
        "requires": ["snapshot"],
        "dependencies": ["PySide6", "numpy", "cairo", "osgeo.gdal"],
        "timeout_s": 110,
        "env": {"GWR_GPU_WEATHER": "1", "GWR_GPU_RESIDENT_FIELDS": "1"},
        "command": [
            "{python}",
            "{tool}",
            "--snapshot",
            "{snapshot}",
            "--map-renderer",
            "gpu",
            "--measurement-mode",
            "native-render",
            "--scenarios",
            "{scenarios}",
            "--frames",
            "{frames}",
            "--repeats",
            "{repeats}",
            "--output",
            "{tool_output}",
        ],
        "defaults": {"scenarios": ["scrub_return"], "frames": 24, "repeats": 1},
        "full": {
            "scenarios": ["scrub_return", "pan_pause_pan", "reverse_pan"],
            "frames": 180,
            "repeats": 3,
        },
        "tool_output": "{outdir}/{gate}.tool.json",
        "result": "{outdir}/{gate}.tool.json",
        "checks": [
            {
                "label": "texture uploads during the scrub input phase",
                "metric": "runs[*].native_render_probe.gpu_counter_delta.texture_uploads",
                "aggregate": "max",
                "op": "<=",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "a0c-timeline-scrub-summary.json"
                    "#candidate_texture_uploads_during_input=0"
                ),
            },
            {
                "label": "texture upload bytes during the scrub input phase",
                "metric": (
                    "runs[*].native_render_probe.gpu_counter_delta."
                    "texture_upload_bytes"
                ),
                "aggregate": "max",
                "op": "<=",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "a0c-timeline-scrub-summary.json"
                    "#candidate_texture_upload_bytes_during_input=0"
                ),
            },
            {
                "label": "numerical weather path is active (no silent CPU fallback)",
                "metric": "runs[*].numerical_weather.active",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "pr50-residency-functional-summary.json"
                    "#scrub_return.final_cpu_symbol_batches=0,"
                    "#scrub_return.final_cpu_raster_symbols=false"
                ),
            },
            {
                "label": "resident decoded-band budget is respected",
                "metric": "runs[*].numerical_weather.band_cache.peak_bytes",
                "aggregate": "max",
                "op": "<=",
                "value": 134217728,
                "source": (
                    "docs/history/gpu-weather-residency.md (128 MiB retained decoded-band "
                    "budget per GPU weather worker); "
                    "docs/profiles/rendering/2026-10-18/"
                    "pr50-residency-functional-summary.json"
                    "#retained_decoded_byte_limit=134217728"
                ),
            },
            {
                "label": "the gate does not claim presentation latency",
                "metric": "runs[*].native_render_probe.presentation_latency_measured",
                "aggregate": "all",
                "op": "is_false",
                "source": (
                    "docs/adr-001-grib-rendering-architecture.md section 9 "
                    "standing constraint 4 (render callbacks are proxies)"
                ),
            },
            {
                "label": "resident field texture uploads during the scrub input phase",
                "metric": (
                    "runs[*].native_render_probe.gpu_counter_delta."
                    "weather_field_uploads"
                ),
                "aggregate": "max",
                "op": "<=",
                "value": 0,
                "ratified": False,
                "source": (
                    "NOT DERIVABLE for this workload. The recorded bound of zero "
                    "texture uploads during a scrub "
                    "(docs/profiles/rendering/2026-10-18/"
                    "a0c-timeline-scrub-summary.json"
                    "#candidate_texture_uploads_during_input=0) was measured on "
                    "the independent vertical viewport, which renders no chart "
                    "layer and never crosses a resident weather window. The app's "
                    "scrub_return workload crosses the resident time window twice, "
                    "and each crossing is a different field with different "
                    "content, so no recorded evidence bounds this metric. It is "
                    "reported without affecting the verdict."
                ),
            },
            {
                "label": "the resident field store stays inside its declared budget",
                "metric": "runs[*].numerical_weather.field_store.current_bytes",
                "aggregate": "max",
                "op": "<=",
                "value": 33554432,
                "source": (
                    "The 32 MiB CPU staging limit the presenter already enforced "
                    "for one published pair "
                    "(gweatherrouting/qt/gpuweather.py WeatherImageProvider."
                    "byte_limit); the resident field store declares the same "
                    "ceiling (gweatherrouting/qt/gpuweatherjobs.py "
                    "FIELD_BYTE_LIMIT), so residency does not raise the memory "
                    "ceiling of the presenter. The 128 MiB decoded-band budget "
                    "(docs/history/gpu-weather-residency.md) is separate and "
                    "unchanged."
                ),
            },
            {
                "label": "chart-layer texture uploads during the scrub input phase",
                "metric": (
                    "runs[*].native_render_probe.gpu_counter_delta."
                    "chart_texture_uploads"
                ),
                "aggregate": "max",
                "op": "<=",
                "value": 0,
                "ratified": False,
                "source": (
                    "NOT DERIVABLE for this workload. The chart base image and "
                    "the navigation overlay contain the forecast-time dependent "
                    "isobars and clock, so they are re-rendered and re-uploaded "
                    "on every forecast-time change; that is chart work "
                    "(#65/#68/#69) and no recorded evidence bounds it here. It is "
                    "reported without affecting the verdict so the residual cost "
                    "of the chart layer stays visible next to the field cost."
                ),
            },
        ],
        "reference": {
            "kind": "recorded-evidence",
            "source": (
                "docs/profiles/rendering/2026-10-18/a0c-timeline-scrub-summary.json"
            ),
            "fixture": (
                "97cfda3c62a494c3d462ee8cde449d4d80ed1c62f387d70edcd9192dfb65d360"
            ),
            "note": (
                "The recorded candidate made 0 texture uploads and 0 upload "
                "bytes during 24 scrub inputs, against 41 uploads / 568 MB for "
                "the recorded serial reference. The bound is a property of the "
                "architecture (a resident field is not re-uploaded), not a "
                "same-workload timing comparison."
            ),
        },
    },
    # ------------------------------------------------------------------ #71
    "float-transport": {
        "title": "Native float texture transport in the Qt Quick scene graph",
        "milestone": "#71",
        "kind": "correctness",
        "tool": "tools/experiment_float_transport_qsg.py",
        "endpoint": ENDPOINT_FIDELITY,
        "requires": [],
        "dependencies": ["PySide6", "numpy"],
        "timeout_s": 120,
        "env": {"QT_SCALE_FACTOR": "1"},
        "command": [
            "{python}",
            "{tool}",
            "--output",
            "{tool_output}",
        ],
        "defaults": {"scenarios": ["generated-field"], "frames": 1, "repeats": 1},
        "full": {"scenarios": ["generated-field"], "frames": 1, "repeats": 1},
        "tool_output": "{outdir}/{gate}.tool.json",
        "result": "{outdir}/{gate}.tool.json",
        "checks": [
            {
                "label": "maximum interpolation error at a probe-chosen position",
                "metric": (
                    "transports.float_through_texture_type_provider."
                    "probe_position_max_abs_error"
                ),
                "aggregate": "max",
                "op": "<=",
                "value": 2e-05,
                "source": (
                    "docs/history/rendering-m0-float-textures.md (unchanged 2e-5 "
                    "gate; E1b measured 2.384185791015625e-7 with explicit "
                    "texelFetch arithmetic and E1 hardware filtering measured "
                    "0.002604166666666824 and failed)"
                ),
            },
            {
                "label": "the probe's own error limit is still 2e-5",
                "metric": "numerical_limit",
                "aggregate": "max",
                "op": "==",
                "value": 2e-05,
                "source": (
                    "docs/history/rendering-m0-float-textures.md (the unchanged "
                    "program numerical gate; it is not relaxed here)"
                ),
            },
            {
                "label": "float32 transport preserved every sampled texel exactly",
                "metric": (
                    "transports.float_through_texture_type_provider."
                    "sampled_cells_exact"
                ),
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "docs/history/rendering-m0-float-textures.md (E1: R32F "
                    "upload/readback preserved all eight test bit patterns, "
                    "including signed zero, a NaN payload and infinities)"
                ),
            },
            {
                "label": "an all-missing field contributes exactly zero alpha pixels",
                "metric": "missing_data.all_missing.alpha_pixels",
                "aggregate": "max",
                "op": "==",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json"
                    "#missing_only_layers_exactly_transparent=true; "
                    "docs/history/rendering-a0k-shader-safety.md (missing-only "
                    "controls require exactly zero alpha pixels, not a low "
                    "fraction of errors)"
                ),
            },
            {
                "label": "an all-missing field contributes no significant ink",
                "metric": "missing_data.all_missing.significant_alpha_pixels",
                "aggregate": "max",
                "op": "==",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json"
                    "#significant_alpha_threshold=25 (the recorded A0k "
                    "significance rule; the corrected run reports zero)"
                ),
            },
            {
                "label": "the finite control field is painted everywhere",
                "metric": "missing_data.finite_control.alpha_pixels",
                "aggregate": "max",
                "op": "==",
                "value": {
                    "metric": "missing_data.finite_control.total_pixels",
                    "aggregate": "max",
                },
                "source": (
                    "docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json"
                    "#finite_control_pixels_unchanged=true (the finite control "
                    "must be painted, not discarded)"
                ),
            },
            {
                "label": "the float transport's end-to-end error is no worse "
                "than the application's own byte-packed transport",
                "metric": (
                    "transports.float_through_texture_type_provider.numerical."
                    "max_abs_error"
                ),
                "aggregate": "max",
                "op": "<=",
                "value": {
                    "metric": "transports.control_packed.numerical.max_abs_error",
                    "aggregate": "max",
                },
                "ratified": False,
                "source": (
                    "NOT DERIVABLE from recorded evidence. The comparison is "
                    "against the byte-packed control measured in the same run "
                    "of tools/experiment_float_transport_qsg.py, not against a "
                    "recorded number: no recorded artifact holds the "
                    "interpolation error of the application's byte-packed "
                    "transport through the QSG path. The placeholder bound is "
                    "never enforced; #71 step 2 must record the byte-packed "
                    "control error and ratify this comparison."
                ),
            },
            {
                "label": "the float texture reached the scene graph in a float "
                "format",
                "metric": "transports.simple_texture_node_float__isolated.rhi_format",
                "aggregate": "max",
                "op": "==",
                "value": "RGBA32F",
                "source": (
                    "docs/adr-001-grib-rendering-architecture.md section 4.1 (D1 "
                    "transport format: native float textures); "
                    "docs/history/rendering-m0-float-textures.md (E1/E1b used "
                    "native R32F and RGBA32F textures)"
                ),
            },
            {
                "label": "the probe ran every stage to completion",
                "metric": "status",
                "aggregate": "max",
                "op": "==",
                "value": "completed",
                "source": (
                    "tools/experiment_float_transport_qsg.py (the probe writes a "
                    "completed report only after every transport, the missing-"
                    "data cases and the replication have run)"
                ),
            },
            {
                "label": "the gate does not claim presentation latency",
                "metric": "presentation_latency_measured",
                "aggregate": "all",
                "op": "is_false",
                "source": (
                    "docs/adr-001-grib-rendering-architecture.md section 9 "
                    "standing constraint 4 (render callbacks are proxies)"
                ),
            },
        ],
        "reference": {
            "kind": "recorded-evidence",
            "source": (
                "docs/history/rendering-m0-float-textures.md; "
                "docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json"
            ),
            "note": (
                "The 2e-5 numerical gate is the unchanged program gate, "
                "recorded in the E1b evidence: explicit texelFetch arithmetic "
                "measured 2.384185791015625e-7 and hardware linear filtering "
                "measured 0.002604166666666824 and failed. The zero-alpha "
                "missing-data gate is the recorded A0k gate: missing-only "
                "controls require exactly zero alpha pixels. Neither threshold "
                "is relaxed here."
            ),
        },
    },
    # ------------------------------------------------------------------ #65
    "interpolation": {
        "title": "GPU spatial interpolation keeps the 2e-5 error bound",
        "milestone": "#65",
        "kind": "correctness",
        "tool": "tools/experiment_float_textures.py",
        "endpoint": ENDPOINT_FIDELITY,
        "requires": [],
        "dependencies": ["PySide6", "numpy"],
        "timeout_s": 110,
        "command": [
            "{python}",
            "{tool}",
            "--sampling",
            "manual",
            "--output",
            "{tool_output}",
        ],
        "defaults": {"scenarios": ["generated-field"], "frames": 1, "repeats": 1},
        "full": {"scenarios": ["generated-field"], "frames": 1, "repeats": 1},
        "tool_output": "{outdir}/{gate}.tool.json",
        "result": "{outdir}/{gate}.tool.json",
        "checks": [
            {
                "label": "maximum sampling error",
                "metric": "max_sampling_error",
                "aggregate": "max",
                "op": "<=",
                "value": 2e-05,
                "source": (
                    "docs/history/rendering-m0-float-textures.md (unchanged 2e-5 gate; "
                    "E1b measured 2.384185791015625e-7, E1 hardware filtering "
                    "measured 0.002604166666666824 and failed)"
                ),
            },
            {
                "label": "the probe's own error limit is still 2e-5",
                "metric": "sampling_error_limit",
                "aggregate": "max",
                "op": "==",
                "value": 2e-05,
                "source": (
                    "docs/rendering-performance-plan.md E1b (retain the "
                    "hardware-linear mode and the unchanged 2e-5 threshold)"
                ),
            },
            {
                "label": "probe status",
                "metric": "status",
                "aggregate": "max",
                "op": "==",
                "value": "passed",
                "source": "tools/experiment_float_textures.py (existing tool)",
            },
            {
                "label": "float32 transport preserved every bit pattern",
                "metric": "transport_bits_match",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "docs/rendering-performance-plan.md E1 (R32F bit-preservation "
                    "check: signed zero, NaN payload, infinities)"
                ),
            },
        ],
        "reference": {
            "kind": "recorded-evidence",
            "source": "docs/history/rendering-m0-float-textures.md",
            "note": (
                "The 2e-5 threshold is the unchanged numerical gate of the "
                "program; the recorded E1b run measured 2.384185791015625e-7 on "
                "native Metal. Hardware linear filtering measured "
                "0.002604166666666824 and failed; this gate uses the adopted "
                "explicit-arithmetic ('manual') sampling path only."
            ),
        },
    },
    # ------------------------------------------------------------------ #66
    "pipeline": {
        "title": "Async double-buffered preparation pipeline",
        "milestone": "#66",
        "kind": "performance",
        "tool": "tools/benchmark_startup_rendering.py",
        "endpoint": ENDPOINT_PROBE,
        "requires": ["snapshot"],
        "dependencies": ["PySide6", "numpy", "cairo", "osgeo.gdal"],
        "timeout_s": 110,
        "env": {"GWR_GPU_WEATHER": "1"},
        "command": [
            "{python}",
            "{tool}",
            "--snapshot",
            "{snapshot}",
            "--map-renderer",
            "gpu",
            "--measurement-mode",
            "native-render",
            "--scenarios",
            "{scenarios}",
            "--frames",
            "{frames}",
            "--repeats",
            "{repeats}",
            "--output",
            "{tool_output}",
        ],
        "defaults": {"scenarios": ["scrub_return"], "frames": 24, "repeats": 1},
        "full": {
            "scenarios": ["scrub_return", "pan_pause_pan", "reverse_pan"],
            "frames": 180,
            "repeats": 3,
        },
        "tool_output": "{outdir}/{gate}.tool.json",
        "result": "{outdir}/{gate}.tool.json",
        "checks": [
            {
                "label": "inputs dropped before they rendered",
                "metric": "runs[*].native_render_probe.inputs[*].status",
                "aggregate": "count_eq",
                "match": "superseded_unobserved",
                "op": "==",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "a0c-timeline-scrub-summary.json"
                    "#candidate_observed_revisions=24 of 24 "
                    "(recorded serial reference: 12 of 24)"
                ),
            },
            {
                "label": "inputs that never rendered",
                "metric": "runs[*].native_render_probe.inputs[*].status",
                "aggregate": "count_eq",
                "match": "timeout_unobserved",
                "op": "==",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "a0c-timeline-scrub-summary.json"
                    "#candidate_observed_revisions=24 of 24"
                ),
            },
            {
                "label": "inputs dispatched later than 50 ms",
                "metric": "runs[*].native_render_probe.inputs[*].dispatch_late_ms",
                "aggregate": "count_gt",
                "match": 50,
                "op": "<",
                "value": 22,
                "fixture_sensitive": True,
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "a0c-timeline-scrub-summary.json"
                    "#reference_dispatch_over_50_ms=22 of 24 inputs "
                    "(recorded candidate: 0)"
                ),
            },
            {
                "label": "input dispatch lateness p95 (ms)",
                "metric": "runs[*].native_render_probe.inputs[*].dispatch_late_ms",
                "aggregate": "p95",
                "op": "<",
                "value": 357.7485605550464,
                "fixture_sensitive": True,
                "baseline": {
                    "metric": "runs[*].native_render_probe.inputs[*].dispatch_late_ms",
                    "aggregate": "p95",
                    "scale": 1.0,
                },
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "a0c-timeline-scrub-summary.json"
                    "#reference_dispatch_lateness_p95_ms=357.7485605550464"
                ),
            },
            {
                "label": "the gate does not claim presentation latency",
                "metric": "runs[*].native_render_probe.presentation_latency_measured",
                "aggregate": "all",
                "op": "is_false",
                "source": (
                    "docs/adr-001-grib-rendering-architecture.md section 9 "
                    "standing constraint 4 (render callbacks are proxies)"
                ),
            },
        ],
        "reference": {
            "kind": "recorded-evidence",
            "source": (
                "docs/profiles/rendering/2026-10-18/a0c-timeline-scrub-summary.json"
            ),
            "fixture": (
                "97cfda3c62a494c3d462ee8cde449d4d80ed1c62f387d70edcd9192dfb65d360"
            ),
            "note": (
                "The recorded serial reference dispatched 22 of 24 inputs over "
                "50 ms with p95 357.7 ms and observed 12 of 24 revisions; the "
                "recorded resident candidate reached p95 2.0 ms, 0 inputs over "
                "50 ms and 24 of 24 revisions. The gate is the improvement "
                "bound, not a claim of presentation latency."
            ),
        },
    },
    # ------------------------------------------------------------------ #67
    "scrub": {
        "title": "Instant forecast-time scrubbing from a resident time window",
        "milestone": "#67",
        "kind": "performance",
        "tool": "tools/benchmark_startup_rendering.py",
        "endpoint": ENDPOINT_PROBE,
        "requires": ["snapshot"],
        "dependencies": ["PySide6", "numpy", "cairo", "osgeo.gdal"],
        "timeout_s": 110,
        "env": {"GWR_GPU_WEATHER": "1"},
        "command": [
            "{python}",
            "{tool}",
            "--snapshot",
            "{snapshot}",
            "--map-renderer",
            "gpu",
            "--measurement-mode",
            "native-render",
            "--scenarios",
            "{scenarios}",
            "--frames",
            "{frames}",
            "--repeats",
            "{repeats}",
            "--output",
            "{tool_output}",
        ],
        "defaults": {"scenarios": ["scrub_return"], "frames": 24, "repeats": 1},
        "full": {
            "scenarios": ["scrub_return", "pan_pause_pan", "reverse_pan"],
            "frames": 180,
            "repeats": 3,
        },
        "tool_output": "{outdir}/{gate}.tool.json",
        "result": "{outdir}/{gate}.tool.json",
        "checks": [
            {
                "label": "texture uploads during the scrub input phase",
                "metric": "runs[*].native_render_probe.gpu_counter_delta.texture_uploads",
                "aggregate": "max",
                "op": "<=",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "a0c-timeline-scrub-summary.json"
                    "#candidate_texture_uploads_during_input=0"
                ),
            },
            {
                "label": "texture upload bytes during the scrub input phase",
                "metric": (
                    "runs[*].native_render_probe.gpu_counter_delta."
                    "texture_upload_bytes"
                ),
                "aggregate": "max",
                "op": "<=",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "a0c-timeline-scrub-summary.json"
                    "#candidate_texture_upload_bytes_during_input=0"
                ),
            },
            {
                "label": "input dispatch lateness p95 (ms)",
                "metric": "runs[*].native_render_probe.inputs[*].dispatch_late_ms",
                "aggregate": "p95",
                "op": "<",
                "value": 357.7485605550464,
                "fixture_sensitive": True,
                "baseline": {
                    "metric": "runs[*].native_render_probe.inputs[*].dispatch_late_ms",
                    "aggregate": "p95",
                    "scale": 1.0,
                },
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "a0c-timeline-scrub-summary.json"
                    "#reference_dispatch_lateness_p95_ms=357.7485605550464"
                ),
            },
            {
                "label": "the gate does not claim presentation latency",
                "metric": "runs[*].native_render_probe.presentation_latency_measured",
                "aggregate": "all",
                "op": "is_false",
                "source": (
                    "docs/adr-001-grib-rendering-architecture.md section 9 "
                    "standing constraint 4 (render callbacks are proxies)"
                ),
            },
        ],
        "reference": {
            "kind": "recorded-evidence",
            "source": (
                "docs/profiles/rendering/2026-10-18/a0c-timeline-scrub-summary.json"
            ),
            "fixture": (
                "97cfda3c62a494c3d462ee8cde449d4d80ed1c62f387d70edcd9192dfb65d360"
            ),
            "note": (
                "The recorded resident candidate issued 0 uploads and 0 upload "
                "bytes across 24 scrub inputs and reached dispatch p95 2.0 ms, "
                "against 41 uploads / 568 MB and p95 357.7 ms for the recorded "
                "serial reference. The recorded 41-upload / 568 MB cost is the "
                "cost this milestone removes."
            ),
        },
    },
    # ------------------------------------------------------------------ #68
    "fill": {
        "title": "GPU weather fill: production path and unchanged barbs gates",
        "milestone": "#68",
        "kind": "fidelity",
        "tool": "tools/benchmark_startup_rendering.py",
        "endpoint": ENDPOINT_GRAB,
        "requires": ["snapshot"],
        "dependencies": ["PySide6", "numpy", "cairo", "osgeo.gdal"],
        "timeout_s": 110,
        "env": {"GWR_GPU_WEATHER": "1"},
        "command": [
            "{python}",
            "{tool}",
            "--snapshot",
            "{snapshot}",
            "--map-renderer",
            "gpu",
            "--scenarios",
            "{scenarios}",
            "--frames",
            "{frames}",
            "--repeats",
            "{repeats}",
            "--output",
            "{tool_output}",
        ],
        "defaults": {"scenarios": ["stationary"], "frames": 24, "repeats": 1},
        "full": {
            "scenarios": ["stationary", "pan", "scrub_return"],
            "frames": 180,
            "repeats": 3,
        },
        "tool_output": "{outdir}/{gate}.tool.json",
        "result": "{outdir}/{gate}.tool.json",
        "checks": [
            {
                "label": "barb selection count is unchanged",
                "metric": "runs[*].numerical_weather.symbols",
                "aggregate": "max",
                "op": "==",
                "value": 1053,
                "source": (
                    "docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json"
                    "#initial_selected_symbols=1053 on fixture 1ed1bca1 "
                    "(the barbs selection/count gate, unchanged)"
                ),
            },
            {
                "label": "the numerical shader is ready",
                "metric": "runs[*].numerical_weather.shader_ready",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json"
                    "#initial_selected_symbols=1053 with the numerical shader active"
                ),
            },
            {
                "label": "the numerical path is active",
                "metric": "runs[*].numerical_weather.active",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "pr50-residency-functional-summary.json"
                    "#scrub_return.final_cpu_symbol_batches=0"
                ),
            },
            {
                "label": "no silent CPU symbol fallback",
                "metric": "runs[*].numerical_weather.cpu_symbol_batches",
                "aggregate": "max",
                "op": "==",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "pr50-residency-functional-summary.json"
                    "#scrub_return.final_cpu_symbol_batches=0"
                ),
            },
            {
                "label": "no silent CPU raster fallback",
                "metric": "runs[*].numerical_weather.cpu_raster_symbols",
                "aggregate": "all",
                "op": "is_false",
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "pr50-residency-functional-summary.json"
                    "#scrub_return.final_cpu_raster_symbols=false"
                ),
            },
            {
                "label": "no run reported an overlay error",
                "metric": "runs[*].warnings",
                "aggregate": "list_length",
                "op": "==",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "pr50-residency-functional-summary.json#scrub_return.warnings=0"
                ),
            },
            {
                "label": "barb atlas image bytes",
                "metric": "runs[*].numerical_weather.atlas_bytes",
                "aggregate": "max",
                "op": "<=",
                "value": 0,
                "ratified": False,
                "source": (
                    "NOT DERIVABLE for this fixture. The only recorded atlas byte "
                    "value is 41760 in "
                    "docs/profiles/rendering/2026-10-18/pr50-residency-functional-summary.json"
                    "#scrub_return.atlas_image_bytes, measured on fixture 97cfda3c "
                    "(33 forecasts), not on fixture 1ed1bca1 (22 sources). The "
                    "placeholder threshold is never enforced; #68 must record an "
                    "atlas bound for its own fixture and set it here."
                ),
            },
        ],
        "open_requirements": [
            "A0k zero-alpha missing-data readback gate: the only existing owner is "
            "tools/diagnose_barb_overlay.py, and it refuses to run against the "
            "current production shader (tools/experiment_app_persistent_weather.py"
            "#shader_sources raises 'Reference barb vertex shader changed' because "
            "gweatherrouting/qt/shaders/barbs.vert no longer carries the "
            "experimental node-id indirection it rewrites). #68 must restore that "
            "diagnostic against the production shader - or ratify an equivalent "
            "all-missing/finite-control readback owner - before the A0k gate can "
            "be claimed. The A0k thresholds themselves are fixed: exactly zero "
            "alpha for all-missing layers, byte-identical finite control pixels, "
            "and zero ink outside the 3 physical-pixel rasterisation support "
            "(docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json).",
        ],
        "reference": {
            "kind": "recorded-evidence",
            "source": (
                "docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json"
            ),
            "fixture": (
                "1ed1bca1281ad4096713ce20af9c1473e70bd4eb0766a54965849b7ace698a03"
            ),
            "note": (
                "The recorded A0k run on this same fixture selected 1053 barbs "
                "with the numerical shader active, all-missing layers exactly "
                "transparent and finite control pixels unchanged. The barbs "
                "selection/count gate is enforced here unchanged; the readback "
                "half of the A0k gate has no runnable owner today and is listed "
                "as an open requirement."
            ),
        },
    },
    # ------------------------------------------------------------------ #69
    "chart-separation": {
        "title": "Chart composition separated from weather generation",
        "milestone": "#69",
        "kind": "lifecycle",
        "tool": "tools/check_early_weather_startup.py",
        "endpoint": (
            "Real application startup observation against the frozen fixture "
            "clock. The recorded end-to-end readback duration is a fidelity "
            "probe, not a presentation measurement."
        ),
        "requires": ["snapshot"],
        "dependencies": ["PySide6", "numpy", "cairo", "osgeo.gdal"],
        "timeout_s": 115,
        "command": [
            "{python}",
            "{tool}",
            "--fixture-tools",
            "{app_root}/tools",
            "--snapshot",
            "{snapshot}",
            "--directory",
            "{tool_output}",
        ],
        "defaults": {"scenarios": ["early-weather"], "frames": 1, "repeats": 1},
        "full": {"scenarios": ["early-weather", "baseline"], "frames": 1, "repeats": 1},
        "tool_output": "{outdir}/{gate}.tool",
        "result": "{outdir}/{gate}.tool/result.json",
        "checks": [
            {
                "label": "the early-weather startup check passed",
                "metric": "passed",
                "aggregate": "max",
                "op": "is_true",
                "source": (
                    "tools/check_early_weather_startup.py (existing tool, own "
                    "verdict: no errors, clean entry exit, weather preserved)"
                ),
            },
            {
                "label": "weather was presented before chart readiness (s)",
                "metric": "first_weather_seconds",
                "aggregate": "max",
                "op": "<",
                "value": {
                    "metric": "charts_initialized_seconds",
                    "aggregate": "max",
                },
                "source": (
                    "docs/profiles/rendering/2026-10-19/a0j-early-display-summary.json"
                    "#passing_run.field_available_seconds=2.39 vs "
                    "#passing_run.chart_ready_seconds=28.54"
                ),
            },
            {
                "label": "weather was available no later than the recorded "
                "chart-ready time (s)",
                "metric": "first_weather_seconds",
                "aggregate": "max",
                "op": "<=",
                "value": 28.386719958041795,
                "source": (
                    "docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json"
                    "#fresh_display.chart_ready_seconds=28.386719958041795"
                ),
            },
            {
                "label": "adding charts did not reset weather or the camera",
                "metric": "camera_weather_preserved",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json"
                    "#fresh_display.weather_pixels_unchanged_before_chart_bind=true,"
                    "#fresh_display.camera_and_field_preserved_on_chart_bind=true"
                ),
            },
        ],
        "reference": {
            "kind": "recorded-evidence",
            "source": (
                "docs/profiles/rendering/2026-10-19/a0j-early-display-summary.json"
            ),
            "fixture": (
                "1ed1bca1281ad4096713ce20af9c1473e70bd4eb0766a54965849b7ace698a03"
            ),
            "note": (
                "The recorded early-weather run published weather at 2.39-2.73 s "
                "while the chart host became ready at 28.39-28.54 s and bound at "
                "31.88-32.04 s, with the camera and field preserved across the "
                "chart bind. The bound used here is the recorded chart-ready "
                "time: weather must be published while charts are still loading."
            ),
        },
    },
    # ------------------------------------------------------------------ #70
    "validation": {
        "title": "Validation, soak and rollout decision",
        "milestone": "#70",
        "kind": "validation",
        "tool": "tools/benchmark_startup_rendering.py",
        "endpoint": ENDPOINT_GRAB,
        "requires": ["snapshot"],
        "dependencies": ["PySide6", "numpy", "cairo", "osgeo.gdal"],
        "timeout_s": 110,
        "env": {"GWR_GPU_WEATHER": "1"},
        "command": [
            "{python}",
            "{tool}",
            "--snapshot",
            "{snapshot}",
            "--map-renderer",
            "gpu",
            "--scenarios",
            "{scenarios}",
            "--frames",
            "{frames}",
            "--repeats",
            "{repeats}",
            "--cpu-fallback-probe",
            "--output",
            "{tool_output}",
        ],
        "defaults": {"scenarios": ["stationary"], "frames": 24, "repeats": 1},
        "full": {
            "scenarios": [
                "stationary",
                "pan",
                "reverse_pan",
                "zoom_out",
                "scrub_return",
                "pan_pause_pan",
                "trackpad_pan",
                "wheel_zoom",
                "wheel_zoom_reverse",
                "pan_zoom_reverse",
            ],
            "frames": 180,
            "repeats": 3,
        },
        "tool_output": "{outdir}/{gate}.tool.json",
        "result": "{outdir}/{gate}.tool.json",
        "min_runs_per_scenario": 3,
        "checks": [
            {
                "label": "the numerical path is reported, not silently skipped",
                "metric": "runs[*].numerical_weather.enabled",
                "aggregate": "all",
                "op": "is_true",
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "pr50-residency-functional-summary.json"
                    "#scrub_return.final_cpu_symbol_batches=0"
                ),
            },
            {
                "label": "no silent CPU symbol fallback",
                "metric": "runs[*].numerical_weather.cpu_symbol_batches",
                "aggregate": "max",
                "op": "==",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "pr50-residency-functional-summary.json"
                    "#scrub_return.final_cpu_symbol_batches=0"
                ),
            },
            {
                "label": "no silent CPU raster fallback",
                "metric": "runs[*].numerical_weather.cpu_raster_symbols",
                "aggregate": "all",
                "op": "is_false",
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "pr50-residency-functional-summary.json"
                    "#scrub_return.final_cpu_raster_symbols=false"
                ),
            },
            {
                "label": "no run reported an overlay error",
                "metric": "runs[*].warnings",
                "aggregate": "list_length",
                "op": "==",
                "value": 0,
                "source": (
                    "docs/profiles/rendering/2026-10-18/"
                    "pr50-residency-functional-summary.json#scrub_return.warnings=0"
                ),
            },
            {
                "label": "every run produced frame samples",
                "metric": "runs[*].summary.grab_ms.count",
                "aggregate": "min",
                "op": ">",
                "value": 0,
                "source": (
                    "docs/history/rendering-performance.md (a run without frame samples "
                    "is not an acceptance timing; do not treat a partial report "
                    "as a completed baseline)"
                ),
            },
            {
                "label": "memory plateau bound",
                "metric": "runs[*].tracked_surface_bytes_per_frame[*]",
                "aggregate": "max",
                "op": "<=",
                "value": 0,
                "ratified": False,
                "source": (
                    "NOT DERIVABLE from recorded evidence. No numeric memory "
                    "plateau budget is recorded in docs/; "
                    "docs/rendering-implementation-plan.md (Memory row) requires "
                    "a recorded numeric budget and a long-run plateau, and "
                    "docs/profiles/rendering/2026-10-18/a0g-unattended-summary.json"
                    "#limitations.global_memory_plateau_measured=false. The "
                    "placeholder threshold 0 is a placeholder only and is never "
                    "enforced; #70 must ratify a real bound and set it here."
                ),
            },
            {
                "label": "the retained CPU fallback renders the weather",
                "metric": "cpu_fallback.prepared_view_current_frames",
                "aggregate": "max",
                "op": ">",
                "value": 0,
                "source": (
                    "issue #70: the retained CPU fallback is the "
                    "application's own CPU renderer, and it must be proven to "
                    "work, not merely to be present. The probe replays the "
                    "same scenario on the same fixture with the CPU renderer "
                    "and counts the frames whose prepared wind view was "
                    "current for the view. A fallback that is present but "
                    "renders nothing is not a fallback."
                ),
            },
            {
                "label": "the CPU fallback ran the CPU renderer",
                "metric": "cpu_fallback.renderer_requested",
                "aggregate": "first",
                "op": "==",
                "value": "cpu",
                "source": (
                    "issue #70: the probe must exercise the CPU path itself, "
                    "not the GPU path with a label. The requested renderer is "
                    "recorded by the application's own controller."
                ),
            },
            {
                "label": "the CPU fallback reported no overlay error",
                "metric": "cpu_fallback.warnings",
                "aggregate": "list_length",
                "op": "==",
                "value": 0,
                "source": (
                    "issue #70: the retained CPU path must render the weather "
                    "without an overlay error, exactly as the GPU run must "
                    "(docs/profiles/rendering/2026-10-18/"
                    "pr50-residency-functional-summary.json#scrub_return."
                    "warnings=0)."
                ),
            },
        ],
        "reference": {
            "kind": "measured-baseline",
            "source": (
                "A prior run of the same tool on the same fixture, supplied by "
                "the caller with --reference PATH."
            ),
            "produce": (
                "python tools/gate_runner.py --gate validation --snapshot <snap> "
                "--output <dir> --full   # then keep <dir>/validation.tool.json "
                "as the baseline artifact"
            ),
            "note": (
                "The enforced part of this gate is derivable from recorded "
                "evidence (per-layer GPU/loading/fallback reporting, no silent "
                "CPU fallback, no overlay warnings) and needs no baseline. The "
                "no-regression comparison against a stored baseline is only "
                "evaluated when the caller passes --reference; the runner never "
                "re-measures a baseline on its own. The memory plateau bound is "
                "not derivable from recorded evidence and is reported as "
                "unratified until #70 ratifies one."
            ),
        },
        "baseline_checks": [
            {
                "label": "grab p99 did not regress against the stored baseline",
                "metric": "runs[*].summary.grab_ms.p99_ms",
                "aggregate": "max",
                "op": "<=",
                "baseline": {
                    "metric": "runs[*].summary.grab_ms.p99_ms",
                    "aggregate": "max",
                    "scale": 1.0,
                },
                "source": (
                    "matched reference artifact supplied with --reference; "
                    "compare like-for-like on the same fixture and environment"
                ),
            },
        ],
    },
}

# Functional acceptance is separate from the unchanged strict fidelity gate.
# The user explicitly deferred pixel matching; it must not be reported as passed.
GATES["enc-viewport-functional"] = {
    **GATES["enc-viewport"],
    "title": "Experimental ENC display and independent weather lifecycle",
    "checks": [
        check
        for check in GATES["enc-viewport"]["checks"]
        if check["metric"].startswith("checks.")
    ]
    + [
        {
            "label": "all requested chart views completed",
            "metric": "complete",
            "aggregate": "all",
            "op": "is_true",
            "source": "User-authorized functional acceptance; visual matching deferred",
        }
    ],
    "open_requirements": [
        "Exact pixel matching is deferred, not accepted; default rollout remains disabled."
    ],
}

GATES["enc-viewport-experience"] = {
    **GATES["enc-viewport-functional"],
    "title": "Combined ENC and weather zoom, resize and lifecycle acceptance",
    "command": GATES["enc-viewport"]["command"] + ["--experience-only"],
    "defaults": {"scenarios": ["combined-experience"], "frames": 1, "repeats": 1},
    "checks": [
        {
            "label": name.replace("_", " "),
            "metric": "checks." + name,
            "aggregate": "all",
            "op": "is_true",
            "source": "User-authorized Phase 2 functional acceptance; no legacy pixel parity",
        }
        for name in (
            "fixture_verified",
            "native_metal_dpr2",
            "combined_layers_visible",
            "fractional_zoom_recovers",
            "screen_space_labels",
            "resize_recovers",
            "rapid_reversals_settle",
            "coverage_miss_explicit",
            "coverage_recovery",
            "payload_bounded",
            "camera_weather_advance_while_chart_stalled",
            "stale_chart_hidden",
            "latest_generation_only",
            "resources_bounded_and_released",
        )
    ],
    "open_requirements": [
        "No full catalogue/platform matrix, RSS/VRAM or presentation-latency acceptance; default OFF."
    ],
}

# Gates whose enforced thresholds are not yet derived from recorded evidence.
GATES["enc-app-rollout"] = {
    **GATES["enc-viewport-functional"],
    "title": "Default ENC through the application TrackMap and Weather panel",
    "tool": "tools/check_enc_app_rollout.py",
    "command": [
        "{python}",
        "{tool}",
        "--snapshot",
        "{snapshot}",
        "--output",
        "{tool_output}",
    ],
    "defaults": {"scenarios": ["app-rollout"], "frames": 1, "repeats": 1},
    "checks": [
        {
            "label": name.replace("_", " "),
            "metric": "checks." + name,
            "aggregate": "all",
            "op": "is_true",
            "source": "docs/enc-default-rollout.md; user-authorized default application rollout",
        }
        for name in (
            "charts_without_forecast",
            "no_implicit_forecast",
            "selected_forecast_with_enc",
            "enc_only_no_duplicate_legacy",
            "app_layer_order_preserved",
            "app_pan_zoom",
            "app_resize",
            "app_time_change",
            "charts_with_wind_hidden",
            "chart_switch_off",
            "chart_switch_on",
            "source_clear_keeps_charts",
            "legacy_recovery",
        )
    ],
    "open_requirements": [
        "No full #70 soak/platform/presentation or RSS/VRAM acceptance."
    ],
}

# Gates whose enforced thresholds are not yet derived from recorded evidence.
# They still run and still enforce every ratified check.
GATES["wind-all-points"] = {
    **GATES["enc-app-rollout"],
    "title": "All forecast points: explicit uncapped legacy wind presentation",
    "milestone": "#79 under #51",
    "tool": "tools/check_all_points_wind.py",
    "defaults": {"scenarios": ["all-thinned-all"], "frames": 1, "repeats": 1},
    "checks": [
        {
            "label": name.replace("_", " "),
            "metric": "checks." + name,
            "aggregate": "all",
            "op": "is_true",
            "source": "docs/wind-all-points.md; issue #79 no thinning or cap",
        }
        for name in (
            "all_points_nonblank_membership",
            "dense_uncapped",
            "explicit_legacy_not_loading",
            "charts_navigation_retained",
            "shoreline_membership",
            "density_roundtrip_no_duplicate",
            "zero_opacity_hides_wind",
            "visibility_hides_wind",
            "visible_wind_recovers",
            "dense_camera_input",
        )
    ],
    "open_requirements": [
        "CPU all-points presentation; no GPU all-points, FPS or RSS/VRAM claim."
    ],
}

GATES_NEEDING_RATIFICATION = tuple(
    sorted(
        gate_id
        for gate_id, gate in GATES.items()
        if any(not check.get("ratified", True) for check in gate["checks"])
    )
)
