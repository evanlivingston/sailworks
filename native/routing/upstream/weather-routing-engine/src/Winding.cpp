// SPDX-License-Identifier: GPL-3.0-or-later
#include "supercpn/weather_routing/Winding.h"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdlib>
#include <numbers>

#include "supercpn/weather_routing/Engine.h"

namespace supercpn::weather_routing {
namespace {

constexpr double kDuplicateEpsilonDegrees = 1e-9;
constexpr double kCoincidentEpsilonNm = 1e-6;
// The issue requires the winding to be integral within this tolerance.
constexpr double kIntegerEpsilonTurns = 1e-6;
constexpr double kDegreesPerTurn = 360.0;
// Collinearity tolerance for "a reversal along the same line".
constexpr double kRetraceEpsilonNm = 1e-6;

bool samePoint(const GeoPoint& left, const GeoPoint& right) {
  return std::abs(left.latitude - right.latitude) <=
             kDuplicateEpsilonDegrees &&
         std::abs(left.longitude - right.longitude) <= kDuplicateEpsilonDegrees;
}

struct PositionVector {
  double x{};
  double y{};
  double z{};
};

PositionVector positionVector(const GeoPoint& point) {
  const double latitude = point.latitude * std::numbers::pi / 180.0;
  const double longitude = point.longitude * std::numbers::pi / 180.0;
  return PositionVector{std::cos(latitude) * std::cos(longitude),
                        std::cos(latitude) * std::sin(longitude),
                        std::sin(latitude)};
}

double dot(const PositionVector& left, const PositionVector& right) {
  return left.x * right.x + left.y * right.y + left.z * right.z;
}

// A reversal along the same line: all three points lie on one great circle
// and the chord from the middle to the last point is antiparallel to the chord
// into the middle point. Comparing chords avoids the bearing convergence that
// makes reciprocal compass bearings differ by more than a string tolerance,
// even when the points are exactly collinear on the sphere.
bool isReversal(const GeoPoint& before, const GeoPoint& middle,
                const GeoPoint& after) {
  if (samePoint(before, middle) || samePoint(middle, after)) return false;
  if (samePoint(before, after)) return true;
  if (std::abs(crossTrackDistanceNm(before, after, middle)) >
      kRetraceEpsilonNm)
    return false;
  const PositionVector start = positionVector(before);
  const PositionVector turn = positionVector(middle);
  const PositionVector end = positionVector(after);
  const PositionVector incoming{turn.x - start.x, turn.y - start.y,
                                turn.z - start.z};
  const PositionVector outgoing{end.x - turn.x, end.y - turn.y,
                                end.z - turn.z};
  return dot(incoming, outgoing) < 0.0;
}

double segmentDistanceNm(const GeoPoint& start, const GeoPoint& end,
                         const GeoPoint& mark) {
  const double length = distanceNm(start, end);
  const double toStart = distanceNm(start, mark);
  const double toEnd = distanceNm(end, mark);
  if (length <= kCoincidentEpsilonNm)
    return std::min(toStart, toEnd);
  const double cross = std::abs(crossTrackDistanceNm(start, end, mark));
  // The perpendicular foot lies inside the segment when the two right
  // triangles add up to the segment length; otherwise the nearest point is an
  // endpoint.
  const double alongStart =
      std::sqrt(std::max(0.0, toStart * toStart - cross * cross));
  const double alongEnd =
      std::sqrt(std::max(0.0, toEnd * toEnd - cross * cross));
  if (alongStart + alongEnd <= length + kCoincidentEpsilonNm)
    return std::min(cross, std::min(toStart, toEnd));
  return std::min(toStart, toEnd);
}

}  // namespace

std::string WindingMeasurement::failureCode() const {
  switch (failure) {
    case WindingFailure::None:
      return "none";
    case WindingFailure::Ambiguous:
      return "rounding_ambiguous";
    case WindingFailure::NotPassed:
      return "rounding_mark_not_passed";
    case WindingFailure::WrongSide:
      return "rounding_wrong_side";
    case WindingFailure::CirclesMoreThanOnce:
      return "route_circles_rounding_mark_more_than_once";
    case WindingFailure::NotEvaluated:
      return "not_evaluated";
  }
  return "not_evaluated";
}

std::string WindingMeasurement::failureReason() const {
  switch (failure) {
    case WindingFailure::None:
      return "none";
    case WindingFailure::Ambiguous:
      return "ambiguous rounding pass";
    case WindingFailure::NotPassed:
      return "mark not passed on this leg";
    case WindingFailure::WrongSide:
      return "wrong side of the rounding mark";
    case WindingFailure::CirclesMoreThanOnce:
      return "route circles the rounding mark more than once";
    case WindingFailure::NotEvaluated:
      return "not evaluated";
  }
  return "not evaluated";
}

std::vector<GeoPoint> reduceTautString(const std::vector<GeoPoint>& track) {
  std::vector<GeoPoint> points;
  points.reserve(track.size());
  for (const GeoPoint& point : track) {
    if (points.empty() || !samePoint(points.back(), point))
      points.push_back(point);
  }

  bool collapsed = true;
  while (collapsed && points.size() >= 3) {
    collapsed = false;
    for (std::size_t index = 1; index + 1 < points.size(); ++index) {
      if (!isReversal(points[index - 1], points[index], points[index + 1]))
        continue;
      points.erase(points.begin() + static_cast<std::ptrdiff_t>(index));
      collapsed = true;
      break;
    }
  }

  std::vector<GeoPoint> reduced;
  reduced.reserve(points.size());
  for (const GeoPoint& point : points) {
    if (reduced.empty() || !samePoint(reduced.back(), point))
      reduced.push_back(point);
  }
  return reduced;
}

WindingMeasurement measureWinding(const std::vector<GeoPoint>& subTrack,
                                  GeoPoint mark, const RoundingSpec& spec) {
  WindingMeasurement result;
  const std::vector<GeoPoint> reduced = reduceTautString(subTrack);
  if (reduced.size() < 2) {
    result.failure = WindingFailure::Ambiguous;
    return result;
  }

  // Item 6: a sub-track that starts on, ends on, or touches the mark has no
  // unambiguous side.
  if (distanceNm(reduced.front(), mark) <= kCoincidentEpsilonNm ||
      distanceNm(reduced.back(), mark) <= kCoincidentEpsilonNm) {
    result.failure = WindingFailure::Ambiguous;
    return result;
  }
  for (std::size_t index = 1; index < reduced.size(); ++index) {
    if (segmentDistanceNm(reduced[index - 1], reduced[index], mark) <=
        kCoincidentEpsilonNm) {
      result.failure = WindingFailure::Ambiguous;
      return result;
    }
  }
  // The loop is closed by the chord from the last reduced point back to the
  // first. A sub-track that already ends at its start has a zero-length
  // chord: there is nothing to add and no degenerate chord to reject.
  const bool hasClosingChord =
      distanceNm(reduced.back(), reduced.front()) > kCoincidentEpsilonNm;
  if (hasClosingChord &&
      segmentDistanceNm(reduced.back(), reduced.front(), mark) <=
          kCoincidentEpsilonNm) {
    result.failure = WindingFailure::Ambiguous;
    return result;
  }

  double sweepDegrees = 0.0;
  double previousBearing = initialBearingDegrees(mark, reduced.front());
  for (std::size_t index = 1; index < reduced.size(); ++index) {
    const double bearing = initialBearingDegrees(mark, reduced[index]);
    const double increment =
        std::remainder(bearing - previousBearing, kDegreesPerTurn);
    // Compass bearings increase clockwise, so negate to keep the issue's
    // convention: positive winding means the mark is left of travel (port).
    sweepDegrees += -increment;
    previousBearing = bearing;
  }
  if (hasClosingChord) {
    const double closingBearing = initialBearingDegrees(mark, reduced.front());
    sweepDegrees +=
        -std::remainder(closingBearing - previousBearing, kDegreesPerTurn);
  }

  result.defined = true;
  result.winding = sweepDegrees / kDegreesPerTurn;
  if (!std::isfinite(result.winding)) {
    result.defined = false;
    result.failure = WindingFailure::Ambiguous;
    return result;
  }

  const double nearest = std::round(result.winding);
  if (std::abs(result.winding - nearest) > kIntegerEpsilonTurns) {
    result.failure = WindingFailure::Ambiguous;
    return result;
  }

  const auto laps = static_cast<long long>(nearest);
  result.passCount = static_cast<int>(std::min<long long>(std::llabs(laps),
                                                          1000000LL));
  if (laps == 0) {
    result.failure = WindingFailure::NotPassed;
    return result;
  }
  result.achievedSide =
      laps > 0 ? RoundingSide::Port : RoundingSide::Starboard;
  if (std::llabs(laps) >= 2) {
    result.failure = WindingFailure::CirclesMoreThanOnce;
    return result;
  }
  if (result.achievedSide != spec.side) {
    result.failure = WindingFailure::WrongSide;
    return result;
  }

  result.satisfied = true;
  result.failure = WindingFailure::None;
  return result;
}

}  // namespace supercpn::weather_routing
