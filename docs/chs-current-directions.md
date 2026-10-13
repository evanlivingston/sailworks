# CHS current direction investigation

Investigated 2026-10-11. **Seymour Narrows' API bearings are consistent with the
published CHS tables: flood flows toward 180 degrees true (south), ebb toward
0 degrees true (north). No 32-degree correction or flood/ebb remapping is needed.**

The initial suggestion that 0/180 might be a simplified phase axis was not borne
out by the published tables. Those happen to be the station's actual stated
bearings.

## Evidence

1. The [2026 Seymour Narrows current tables](https://tides.gc.ca/sites/tides/files/2025-11/08108_2026.pdf)
   show flood 180 true and ebb 0 true on all four quarterly pages. The same
   headings appear in [2026 Volume 6](https://waves-vagues.dfo-mpo.gc.ca/library-bibliotheque/chs-shc-tct-tmc-vol6-2026-41311267.pdf),
   printed pages 68–71 (PDF pages 71–74).
2. IWLS station metadata reports `floodDirection: 180`, `ebbDirection: 0`. The
   continuous `wcdp1` series contains those same bearings. All four current extrema
   in the sampled UTC day agree with their event qualifier: flood samples are
   180, ebb samples are 0. Comparisons use the nearest 15-minute direction sample,
   at most six minutes from the event in this sample.
3. The [public Seymour Narrows page](https://tides.gc.ca/en/stations/08108/currents)
   contains a 032/212 true legend that conflicts with the published table and
   API. The same legend appears on the [Dodd Narrows page](https://tides.gc.ca/en/stations/07487/currents),
   despite that station's different API bearings. This strongly suggests a shared
   display/template defect, but the underlying server template was not available
   for inspection. We have not reported the issue to CHS.
4. The public [current-chart JavaScript](https://tides.gc.ca/sites/tides/modules/custom/dfo_mpo_api/js/dfo-mpo-api-stations-currents.js)
   labels the direction axis as degrees true and passes the supplied series to
   the chart without applying a station-bearing correction. It does not document
   an alternative 0/180 phase code.

The [CHS current-table index](https://tides.gc.ca/en/current-predictions-station)
states that official annual publications take precedence over online-table
differences. This supports using the published table to resolve this discrepancy.

## Checks at other stations

A reproducible audit of the 2026-10-11 UTC day produced:

| Station | Metadata flood / ebb (degrees) | Continuous bearings | Extrema consistency |
| --- | --- | --- | --- |
| Seymour Narrows 08108 | 180 / 0 | Two values: 0 and 180 | All four extrema agree exactly with the corresponding metadata bearing |
| Dodd Narrows 07487 | 355 / 155 | 28 distinct sampled bearings | Flood near 355; ebb near 155, with small variations |
| Juan de Fuca East 07100 | 90 / 270 | 63 distinct sampled bearings | Flood near east; ebb near west, with variations |

This rules out treating `wcdp1` globally as a binary phase code. The continuous
direction can vary through a cycle and need not equal a nominal flood/ebb heading
exactly. The other two station checks establish consistency with metadata, not
independent physical validation of their entire prediction series.

The annual tables use fixed PST (UTC-8), even during daylight saving time. The
public page can display Pacific daylight time (UTC-7). For example, the October
11 flood maximum at 02:06 PST is 10:06 UTC and 03:06 PDT. Compare timestamps in
UTC, rather than treating a one-hour clock difference as a prediction discrepancy.

## Implementation decision

- Treat verified CHS current bearings as the direction water flows toward,
  clockwise from true north. Preserve speed magnitude separately from phase.
- Convert knots to metres per second, then use
  `east = speed * sin(bearing)` and `north = speed * cos(bearing)`.
- Use the time-varying direction series where available. Do not replace it with
  fixed flood/ebb headings or a hardcoded offset.
- Preserve explicit event qualifiers for flood/ebb timing. Do not infer flood
  from northward flow: Seymour Narrows and Dodd Narrows differ.
- At slack, treat direction as physically indeterminate; never interpolate a
  compass angle directly through a reversal. Future samplers must interpolate
  vectors with suitable temporal resolution and retain exact passage events.
- Keep station predictions local to their station/passage. Resolving bearings
  does not validate spatial extrapolation, depth, or CIOPS passage accuracy.

## Reproduce

```sh
.venv/bin/python tools/marine_probe.py \
  --output /private/tmp/gwr-marine-probe-20261011 --date 2026-10-11 \
  --direction-stations 08108 07487 07100
```

Repeat with `--offline` to verify cached responses without network access. Output
is `direction-report.json`, separate from the broader `report.json`. The audit
compares bearings only near explicitly classified extrema; it reports missing,
invalid or distant samples and excludes slack. Raw data and checksums remain in
the local snapshot. Tests use authored samples, not copied CHS predictions.
