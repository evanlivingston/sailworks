#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <numbers>
#include <string>
#include <vector>

#include "native_routing_tree_identity.h"
#include "sailworks_performance.h"
#include "supercpn/weather_routing/Rounding.h"
#include "synthetic.h"

namespace {

int failures = 0;

void require(bool condition, const std::string& message) {
  if (condition) return;
  std::cerr << "FAIL: " << message << '\n';
  ++failures;
}

bool near(double actual, double expected, double tolerance = 1e-6) {
  return std::abs(actual - expected) <= tolerance;
}

}  // namespace

int main() {
  using namespace sailworks::native_routing;

  require(std::string(SAILWORKS_NATIVE_ROUTING_TREE_SHA256).size() == 64,
          "tree identity hash is missing");
  require(SAILWORKS_NATIVE_ROUTING_TREE_FILE_COUNT > 0,
          "tree identity has no files");
  require(std::string(SAILWORKS_NATIVE_ROUTING_UPSTREAM_COMMIT).size() == 40,
          "upstream commit pin is missing");

  const FixtureObservables observables = fixtureObservables();
  require(observables.windSouthWestHour0.available,
          "synthetic wind sample is unavailable");
  require(near(observables.windSouthWestHour0.velocity.eastKnots,
               6.0 * kKnotsPerMetreSecond) &&
              near(observables.windSouthWestHour0.velocity.northKnots,
                   2.0 * kKnotsPerMetreSecond),
          "synthetic wind components do not match the fixture formula");
  require(near(observables.pressureSouthWestHour0Pa, 101200.0) &&
              near(observables.pressureNorthEastHour0Pa, 102400.0),
          "synthetic pressure does not match the fixture formula");
  require(near(observables.currentNorthEastHour3.velocity.eastKnots,
               0.35 * kKnotsPerMetreSecond) &&
              near(observables.currentNorthEastHour6.velocity.northKnots,
                   -0.07 * kKnotsPerMetreSecond),
          "synthetic current does not match the fixture formula");
  require(near(observables.polarSpeed60At8, 2.0) &&
              near(observables.polarSpeed90At12, 3.0) &&
              near(observables.polarSpeed180At16, 5.2),
          "synthetic polar speeds do not match the fixture formula");

  const wr::RoutingOptions defaults;
  require(near(defaults.destinationToleranceNm, 1.0) &&
              near(defaults.maximumSearchAngleDegrees, 120.0) &&
              defaults.timeStep == wr::Duration{std::chrono::hours{1}},
          "routing defaults are unchanged by rounding support");
  const wr::GeoPoint mark{48.0, -123.0};
  // Shared closest-pass rounding evaluator: side of track, never distance.
  const wr::RoundingSpec portSpec{wr::RoundingSide::Port, std::nullopt,
                                  std::nullopt};
  const wr::RoundingSpec starboardSpec{wr::RoundingSide::Starboard,
                                       std::nullopt, std::nullopt};
  const auto pointAt = [&mark](double eastNm, double northNm) -> wr::GeoPoint {
    if (eastNm == 0.0 && northNm == 0.0) return mark;
    double bearing = std::atan2(eastNm, northNm) * 180.0 / std::numbers::pi;
    if (bearing < 0.0) bearing += 360.0;
    return wr::destinationPoint(mark, bearing, std::hypot(eastNm, northNm));
  };

  // A 20 NM wide correct-side pass is compliant at any distance.
  const std::vector<wr::GeoPoint> portWide{pointAt(20.0, -20.0),
                                           pointAt(20.0, 20.0)};
  const auto portWidePass = wr::measureRounding(portSpec, portWide, mark, 0.0);
  require(portWidePass.satisfied &&
              portWidePass.failure == wr::RoundingFailure::None &&
              portWidePass.failureCode() == "none" &&
              portWidePass.achievedSide == wr::RoundingSide::Port &&
              near(portWidePass.closestDistanceNm, 20.0, 0.1) &&
              portWidePass.passageCount == 1,
          "a 20 NM wide port pass is accepted with no distance limit");

  const std::vector<wr::GeoPoint> starboardWide{pointAt(-20.0, -20.0),
                                                pointAt(-20.0, 20.0)};
  const auto starboardWidePass =
      wr::measureRounding(starboardSpec, starboardWide, mark, 0.0);
  require(starboardWidePass.satisfied &&
              starboardWidePass.achievedSide ==
                  wr::RoundingSide::Starboard &&
              near(starboardWidePass.closestDistanceNm, 20.0, 0.1) &&
              starboardWidePass.passageCount == 1,
          "a 20 NM wide starboard pass is accepted with no distance limit");

  // The closest pass decides the side: this track passes the mark to
  // starboard and only ends with the mark looking to port.
  const std::vector<wr::GeoPoint> wrongSideTrack{
      pointAt(30.0, 0.0), pointAt(0.3, -0.3), pointAt(-30.0, -10.0),
      pointAt(-10.0, -10.0)};
  const auto wrongSidePass =
      wr::measureRounding(portSpec, wrongSideTrack, mark, 90.0);
  const double terminalBearingDegrees =
      wr::initialBearingDegrees(wrongSideTrack.back(), mark);
  const double terminalLateralNm =
      std::sin((90.0 - terminalBearingDegrees) * std::numbers::pi / 180.0) *
      wr::distanceNm(wrongSideTrack.back(), mark);
  require(terminalLateralNm > 1.0,
          "the wrong-side track ends with the mark on the port side");
  require(!wrongSidePass.satisfied &&
              wrongSidePass.failure == wr::RoundingFailure::WrongSide &&
              wrongSidePass.failureCode() == "wrong_side" &&
              wrongSidePass.achievedSide == wr::RoundingSide::Starboard &&
              wrongSidePass.passageCount == 1,
          "a wrong-side closest pass is rejected despite a port-looking end");

  // The closest pass between two recorded points is measured on the
  // interpolation, not at the vertices.
  const std::vector<wr::GeoPoint> interpolated{pointAt(10.0, 10.0),
                                               pointAt(-10.0, 10.0)};
  const auto interpolatedPass =
      wr::measureRounding(portSpec, interpolated, mark, 270.0);
  require(interpolatedPass.satisfied &&
              interpolatedPass.achievedSide == wr::RoundingSide::Port &&
              near(interpolatedPass.closestDistanceNm, 10.0, 0.1) &&
              std::abs(interpolatedPass.closestPoint.longitude -
                       mark.longitude) < 0.05 &&
              wr::distanceNm(interpolated.front(), mark) >
                  interpolatedPass.closestDistanceNm + 1.0 &&
              wr::distanceNm(interpolated.back(), mark) >
                  interpolatedPass.closestDistanceNm + 1.0,
          "the closest pass is interpolated between recorded points");

  // Many short legs still count exactly one passage.
  std::vector<wr::GeoPoint> manyLegs;
  for (int step = 0; step <= 60; ++step) {
    const double north = -20.0 + 40.0 * static_cast<double>(step) / 60.0;
    manyLegs.push_back(pointAt(3.0, north));
  }
  const auto manyLegsPass = wr::measureRounding(portSpec, manyLegs, mark, 0.0);
  require(manyLegsPass.satisfied && manyLegsPass.passageCount == 1 &&
              near(manyLegsPass.closestDistanceNm, 3.0, 0.1) &&
              manyLegsPass.achievedSide == wr::RoundingSide::Port,
          "many short legs measure one continuous ray crossing");

  // A full lap of 8 legs over a 20 NM circle crosses the abeam ray once, not
  // once per delivered segment.
  std::vector<wr::GeoPoint> lap;
  for (int leg = 0; leg <= 8; ++leg)
    lap.push_back(
        wr::destinationPoint(mark, 90.0 - 45.0 * (leg % 8), 20.0));
  const auto lapPass = wr::measureRounding(portSpec, lap, mark, 0.0);
  require(lapPass.satisfied && lapPass.passageCount == 1 &&
              lapPass.achievedSide == wr::RoundingSide::Port &&
              near(lapPass.closestDistanceNm,
                   20.0 * std::cos(std::numbers::pi / 8.0), 0.2),
          "an 8-leg one-lap circle counts one ray crossing, not one per leg");

  // Zero ray crossings means the mark was not passed.
  const std::vector<wr::GeoPoint> notPassedTrack{pointAt(5.0, 5.0),
                                                 pointAt(10.0, 10.0)};
  const auto notPassed =
      wr::measureRounding(portSpec, notPassedTrack, mark, 45.0);
  require(!notPassed.satisfied && notPassed.passageCount == 0 &&
              notPassed.failure == wr::RoundingFailure::NotPassed &&
              notPassed.failureCode() == "mark_not_passed" &&
              notPassed.failureReason() == "mark not passed on this leg",
          "zero passages is reported as not passed");

  // Two passages are rejected with the named reason even on the right side.
  const std::vector<wr::GeoPoint> twoPassageTrack{
      pointAt(1.0, -10.0), pointAt(1.0, 10.0), pointAt(20.0, -10.0),
      pointAt(20.0, 10.0)};
  const auto twoPassages =
      wr::measureRounding(portSpec, twoPassageTrack, mark, 0.0);
  require(!twoPassages.satisfied && twoPassages.passageCount == 2 &&
              twoPassages.achievedSide == wr::RoundingSide::Port &&
              twoPassages.failure ==
                  wr::RoundingFailure::CirclesMoreThanOnce &&
              twoPassages.failureCode() ==
                  "route_circles_rounding_mark_more_than_once" &&
              twoPassages.failureReason() ==
                  "route circles the rounding mark more than once",
          "two passages are rejected with the named reason");

  // A pass through the mark leaves the side undefined.
  const std::vector<wr::GeoPoint> coincidentTrack{pointAt(-5.0, 0.0), mark,
                                                  pointAt(5.0, 0.0)};
  const auto coincident =
      wr::measureRounding(portSpec, coincidentTrack, mark, 90.0);
  require(!coincident.satisfied && coincident.closestDistanceNm <= 1e-6 &&
              coincident.failure == wr::RoundingFailure::AmbiguousPass &&
              coincident.failureCode() == "ambiguous_pass",
          "a pass through the mark is ambiguous");

  // A lateral inside 0.01 NM is ambiguous, not a side.
  const std::vector<wr::GeoPoint> ambiguousSide{pointAt(-1.0, 0.002),
                                                pointAt(1.0, 0.002)};
  const auto sideAmbiguous =
      wr::measureRounding(portSpec, ambiguousSide, mark, 90.0);
  require(!sideAmbiguous.satisfied && !sideAmbiguous.achievedSide &&
              sideAmbiguous.failure ==
                  wr::RoundingFailure::SideAmbiguous &&
              sideAmbiguous.failureCode() == "side_ambiguous",
          "a lateral within the ambiguity epsilon is rejected");

  // The exit range is inclusive on the delivered outbound course.
  const wr::GeoPoint exitBase = wr::destinationPoint(mark, 315.0, 5.0);
  const std::vector<wr::GeoPoint> exitTrack{
      wr::destinationPoint(exitBase, 225.0, 15.0),
      wr::destinationPoint(exitBase, 45.0, 15.0)};
  const auto exitAtMinAndMax = wr::measureRounding(
      wr::RoundingSpec{wr::RoundingSide::Starboard, 45.0, 45.0}, exitTrack,
      mark, 45.0);
  require(exitAtMinAndMax.satisfied && exitAtMinAndMax.exitRangeSatisfied,
          "the exit range accepts exactly min and exactly max");
  require(wr::measureRounding(
              wr::RoundingSpec{wr::RoundingSide::Starboard, 0.0, 45.0},
              exitTrack, mark, 45.0)
              .satisfied,
          "the exit range accepts exactly its maximum");
  require(wr::measureRounding(
              wr::RoundingSpec{wr::RoundingSide::Starboard, 45.0, 90.0},
              exitTrack, mark, 45.0)
              .satisfied,
          "the exit range accepts exactly its minimum");
  const auto exitBelow = wr::measureRounding(
      wr::RoundingSpec{wr::RoundingSide::Starboard, 46.0, 90.0}, exitTrack,
      mark, 45.0);
  require(!exitBelow.satisfied &&
              exitBelow.failure ==
                  wr::RoundingFailure::ExitBearingOutOfRange &&
              !exitBelow.exitRangeSatisfied &&
              exitBelow.failureCode() == "exit_bearing_out_of_range",
          "a course below the exit range is rejected");
  require(wr::measureRounding(
              wr::RoundingSpec{wr::RoundingSide::Starboard, 0.0, 44.0},
              exitTrack, mark, 45.0)
                  .failure == wr::RoundingFailure::ExitBearingOutOfRange,
          "a course above the exit range is rejected");

  // The legacy radius field is inert: the same geometry gives the same verdict
  // at any radius.
  const std::vector<wr::GeoPoint> radiusTrack{pointAt(20.0, -10.0),
                                              pointAt(20.0, 10.0)};
  const wr::RoundingConstraint tinyRadius{wr::RoundingSide::Port, 1e-6,
                                          std::nullopt, std::nullopt};
  const wr::RoundingConstraint hugeRadius{wr::RoundingSide::Port, 1e6,
                                          std::nullopt, std::nullopt};
  const auto tinyVerdict = wr::measureRounding(tinyRadius, radiusTrack, mark, 0.0);
  const auto hugeVerdict = wr::measureRounding(hugeRadius, radiusTrack, mark, 0.0);
  require(tinyVerdict.satisfied && hugeVerdict.satisfied &&
              tinyVerdict.failure == hugeVerdict.failure &&
              tinyVerdict.passageCount == 1 &&
              near(tinyVerdict.closestDistanceNm, 20.0, 0.1),
          "radius has no effect on a correct-side pass");
  const wr::RoundingConstraint hugeStarboard{wr::RoundingSide::Starboard, 1e6,
                                             std::nullopt, std::nullopt};
  require(wr::measureRounding(hugeStarboard, radiusTrack, mark, 0.0).failure ==
              wr::RoundingFailure::WrongSide,
          "a huge radius never rescues a wrong-side pass");

  // An interior mark is judged over the concatenated arriving and departing
  // track. The arriving sub-run alone passes on port; the departure brings the
  // track closer on starboard, so the concatenated verdict is wrong side.
  const std::vector<wr::GeoPoint> arrivingPort{pointAt(-10.0, -10.0),
                                               pointAt(10.0, -10.0)};
  const auto arrivingAlone =
      wr::measureRounding(portSpec, arrivingPort, mark, 90.0);
  require(arrivingAlone.satisfied &&
              arrivingAlone.achievedSide == wr::RoundingSide::Port,
          "the arriving sub-run alone passes on port");
  std::vector<wr::GeoPoint> concatenated = arrivingPort;
  concatenated.push_back(pointAt(0.0, -2.0));
  concatenated.push_back(pointAt(-10.0, -10.0));
  const auto concatenatedVerdict =
      wr::measureRounding(portSpec, concatenated, mark, 0.0);
  require(!concatenatedVerdict.satisfied &&
              concatenatedVerdict.achievedSide ==
                  wr::RoundingSide::Starboard &&
              concatenatedVerdict.failure == wr::RoundingFailure::WrongSide &&
              concatenatedVerdict.failureCode() == "wrong_side",
          "the concatenated track rejects a wrong-side pass the arriving "
          "sub-run alone would accept");

  // Ending marks are exact course points with no side constraint.
  const std::vector<wr::GeoPoint> exactEnding{pointAt(-10.0, 10.0), mark};
  const auto endingExact = wr::measureEndingMark(exactEnding, mark);
  require(endingExact.satisfied &&
              endingExact.failure == wr::RoundingFailure::None,
          "an ending mark reached exactly is satisfied with no side "
          "constraint");
  const std::vector<wr::GeoPoint> offsetEnding{pointAt(-10.0, 10.0),
                                               pointAt(0.5, 0.0)};
  const auto endingOffset = wr::measureEndingMark(offsetEnding, mark);
  require(!endingOffset.satisfied &&
              endingOffset.failure == wr::RoundingFailure::NotPassed,
          "an ending mark that is not reached exactly is rejected");

  // Terminal candidates are a bounded solver decision, not a radius circle.
  const wr::RoundingConstraint tinyCandidateRadius{
      wr::RoundingSide::Port, 1e-6, std::nullopt, std::nullopt};
  const auto candidates =
      wr::roundingTargets(pointAt(-30.0, 0.0), mark, tinyCandidateRadius);
  bool wideCandidate = false;
  for (const auto& candidate : candidates)
    if (wr::distanceNm(candidate, mark) >= 9.0) wideCandidate = true;
  require(!candidates.empty() && candidates.size() <= 10 && wideCandidate,
          "the candidate set stays bounded and includes a wide passing point "
          "even when the legacy radius is tiny");

  // The legacy radius is optional: a payload that omits it is valid, and any
  // arbitrary legacy value produces the same verdict.
  wr::RoundingConstraint noRadius;
  noRadius.side = wr::RoundingSide::Port;
  require(wr::roundingConstraintValid(noRadius),
          "a rounding payload without a radius is accepted");
  const wr::RoundingConstraint smallLegacyRadius{wr::RoundingSide::Port, 0.001,
                                                 std::nullopt, std::nullopt};
  const wr::RoundingConstraint largeLegacyRadius{wr::RoundingSide::Port, 1000.0,
                                                 std::nullopt, std::nullopt};
  require(wr::roundingConstraintValid(smallLegacyRadius) &&
              wr::roundingConstraintValid(largeLegacyRadius),
          "legacy radii are not validated");
  const auto smallLegacyVerdict =
      wr::measureRounding(smallLegacyRadius, radiusTrack, mark, 0.0);
  const auto largeLegacyVerdict =
      wr::measureRounding(largeLegacyRadius, radiusTrack, mark, 0.0);
  require(smallLegacyVerdict.satisfied && largeLegacyVerdict.satisfied &&
              smallLegacyVerdict.failure == largeLegacyVerdict.failure &&
              smallLegacyVerdict.passageCount ==
                  largeLegacyVerdict.passageCount &&
              near(smallLegacyVerdict.signedLateralNm,
                   largeLegacyVerdict.signedLateralNm, 1e-12),
          "arbitrary legacy radii produce identical verdicts");

  wr::RoundingConstraint wrapper{wr::RoundingSide::Port, 0.5, std::nullopt,
                                 std::nullopt};
  require(wr::roundingConstraintValid(wrapper),
          "valid rounding constraint is accepted");
  wrapper.exitBearingMinDegrees = 20.0;
  wrapper.exitBearingMaxDegrees = 10.0;
  require(!wr::roundingConstraintValid(wrapper),
          "wrapped exit range is rejected");

  auto vessel = fixtureVesselConfiguration();
  vessel.profiles.front().efficiency = 0.85;
  const wr::WaveSample missingWaves{};
  const SailworksPerformance baseline(vessel, {}, {}, 5.0, false);
  const SailworksPerformance policy(vessel, 30.0, 3.0, 5.0, true);
  const auto sail = baseline.evaluate(wr::PropulsionMode::Sail,
                                      wr::ProfileRole::SailOnly, {}, 8.0, 60.0,
                                      missingWaves);
  const auto motored = policy.evaluate(wr::PropulsionMode::Sail,
                                       wr::ProfileRole::SailOnly, {}, 8.0, 60.0,
                                       missingWaves);
  require(near(sail.speedThroughWaterKnots, 1.7) &&
              near(motored.speedThroughWaterKnots, 5.0) &&
              motored.mode == wr::PropulsionMode::Sail,
          "Sailworks motor fallback must substitute speed after efficiency");
  const auto proxied = policy.evaluate(wr::PropulsionMode::Sail,
                                       wr::ProfileRole::SailOnly, {}, 24.0, 180.0,
                                       missingWaves);
  const auto unproxied = baseline.evaluate(wr::PropulsionMode::Sail,
                                           wr::ProfileRole::SailOnly, {}, 24.0,
                                           180.0, missingWaves);
  require(near(proxied.speedThroughWaterKnots,
               unproxied.speedThroughWaterKnots * 0.95),
          "wind-based wave proxy must not require a wave sample");
  require(policy.candidates(31.0, 90.0, missingWaves,
                            wr::PropulsionMode::Sail, wr::Duration{}).empty() &&
              !policy.evaluate(wr::PropulsionMode::Sail,
                               wr::ProfileRole::SailOnly, {}, 31.0, 90.0,
                               missingWaves).valid,
          "wind limit must reject search and independent replay candidates");

  std::vector<wr::RoutingProgressUpdate> progress;
  wr::RoutingRequest progressRequest = fixtureRequest(false, false);
  progressRequest.progress = [&progress](const wr::RoutingProgressUpdate& update) {
    progress.push_back(update);
  };
  const SyntheticRun progressRun =
      runRequest("main", progressRequest, fixtureEnvironment());
  require(progressRun.accepted,
          "synthetic route with a progress callback was not accepted");
  require(!progress.empty(),
          "engine did not call the request progress callback");
  require(progress.front().stage == wr::RoutingProgressStage::Preflight,
          "progress did not begin at Preflight");
  require(std::any_of(progress.begin(), progress.end(), [](const auto& update) {
            return update.stage == wr::RoutingProgressStage::Validation;
          }),
          "progress never reached Validation");
  require(progress.back().stage == wr::RoutingProgressStage::Complete,
          "progress did not end at Complete");
  require(progress.back().effortPercent > 0,
          "progress did not report the engine effort percent");

  const SyntheticRun first = runSynthetic("main");
  const SyntheticRun second = runSynthetic("main");
  require(first.accepted, "main synthetic route was not accepted");
  require(first.result.validation.passed,
          "main synthetic engine validation did not pass");
  require(first.independentValidation.passed,
          "independent replay validation did not pass");
  require(routeDigest(first.result) == routeDigest(second.result),
          "two synthetic routes are not deterministic");

  for (const std::string mode : {"standard", "original", "professional"}) {
    const SyntheticRun run = runSynthetic(mode);
    require(run.result.validation.passed == run.independentValidation.passed,
            mode + " replay validation disagrees");
    require(!run.accepted || (run.result.validation.passed &&
                              run.independentValidation.passed),
            mode + " accepted without both validations");
    require(run.incumbentForwarded ==
                (mode == "professional" && run.incumbentValidated),
            mode + " incumbent handoff disagrees with validation");
  }

  const SyntheticRun cancelled = runCancelled("main");
  require(cancelled.result.status == wr::RoutingStatus::Cancelled,
          "cancelled request did not report Cancelled");
  require(!cancelled.accepted,
          "cancelled request reported accepted completion");

  const SyntheticRun original = runCancelled("original");
  require(original.result.status == wr::RoutingStatus::Cancelled,
          "original cancelled request did not report Cancelled");
  require(!original.accepted,
          "original cancelled request reported accepted completion");
  const SyntheticRun professional = runCancelled("professional");
  require(professional.result.status == wr::RoutingStatus::Cancelled &&
              !professional.incumbentForwarded && !professional.accepted,
          "professional cancellation forwarded or accepted incumbent");

  const SyntheticRun invalid = runInvalidDestination();
  require(!invalid.accepted, "invalid destination reported accepted completion");

  if (failures != 0) return EXIT_FAILURE;
  std::cout << "native routing selftest passed\n";
  return EXIT_SUCCESS;
}
