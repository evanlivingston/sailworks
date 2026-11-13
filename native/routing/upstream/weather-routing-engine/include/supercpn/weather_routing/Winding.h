#pragma once

#include <optional>
#include <string>
#include <vector>

#include "supercpn/weather_routing/Types.h"

namespace supercpn::weather_routing {

// Why a leg sub-track does not satisfy its mark's rounding contract. The
// decision table is exhaustive; NotEvaluated is the pre-measurement default.
enum class WindingFailure {
  None,
  // Non-integer winding, a reduced sub-track that touches or starts/ends on
  // the mark, or a closing chord through the mark, so the loop is degenerate.
  Ambiguous,
  // The mark was not passed on this leg.
  NotPassed,
  // The net passage is on the wrong side of the requested side.
  WrongSide,
  // The route circles the mark more than once.
  CirclesMoreThanOnce,
  NotEvaluated,
};

// Measured taut-string passage of one mark.
struct WindingMeasurement {
  // Net signed revolutions of the bearing from the mark around the closed
  // loop formed by the reduced sub-track plus the chord from its last point
  // back to its first, in turns. A leg that does not enclose the mark gives 0;
  // one that passes around it gives +/-1. Positive is a port (mark-left)
  // passage; negative is starboard.
  double winding{};
  // False only when the reduced sub-track or its closing chord is degenerate
  // per the ambiguity rule; a defined but non-integer winding remains
  // `defined`.
  bool defined{};
  std::optional<RoundingSide> achievedSide;
  // |winding| rounded to the nearest integer when the winding is integral.
  int passCount{};
  bool satisfied{};
  WindingFailure failure{WindingFailure::NotEvaluated};
  // Stable machine code for reports and schema round-trips.
  std::string failureCode() const;
  // Human-readable named reason. The multi-lap reason is the issue's exact
  // wording.
  std::string failureReason() const;
};

// Taut-string reduction: drop consecutive duplicates within 1e-9 degrees, then
// collapse collinear reversals until the string is stable. A pure string
// operation on delivered points; it invents nothing.
std::vector<GeoPoint> reduceTautString(const std::vector<GeoPoint>& track);

// Apply the shared winding rule to the delivered sub-track that passes `mark`.
// The sub-track is closed with the chord from its last point back to its
// first before the winding is measured. A sub-track that already ends at its
// start has a zero-length chord, which is skipped rather than treated as
// degenerate. `spec` carries the requested side; exit bearings are evaluated
// by the engine's terminal-course check.
WindingMeasurement measureWinding(const std::vector<GeoPoint>& subTrack,
                                  GeoPoint mark, const RoundingSpec& spec);

}  // namespace supercpn::weather_routing
