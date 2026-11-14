#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <numbers>
#include <stdexcept>
#include <string>
#include <vector>

#include "native_routing_tree_identity.h"
#include "sailworks_performance.h"
#include "supercpn/weather_routing/Rounding.h"
#include "synthetic.h"
#include "user_request.h"

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
  const wr::RoundingSpec portSpec{wr::RoundingSide::Port};
  const wr::RoundingSpec starboardSpec{wr::RoundingSide::Starboard};
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

  // The legacy radius field is inert: the same geometry gives the same verdict
  // at any radius.
  const std::vector<wr::GeoPoint> radiusTrack{pointAt(20.0, -10.0),
                                              pointAt(20.0, 10.0)};
  const wr::RoundingConstraint tinyRadius{wr::RoundingSide::Port, 1e-6};
  const wr::RoundingConstraint hugeRadius{wr::RoundingSide::Port, 1e6};
  const auto tinyVerdict = wr::measureRounding(tinyRadius, radiusTrack, mark, 0.0);
  const auto hugeVerdict = wr::measureRounding(hugeRadius, radiusTrack, mark, 0.0);
  require(tinyVerdict.satisfied && hugeVerdict.satisfied &&
              tinyVerdict.failure == hugeVerdict.failure &&
              tinyVerdict.passageCount == 1 &&
              near(tinyVerdict.closestDistanceNm, 20.0, 0.1),
          "radius has no effect on a correct-side pass");
  const wr::RoundingConstraint hugeStarboard{wr::RoundingSide::Starboard, 1e6};
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
  const wr::RoundingConstraint tinyCandidateRadius{wr::RoundingSide::Port, 1e-6};
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
  const wr::RoundingConstraint smallLegacyRadius{wr::RoundingSide::Port, 0.001};
  const wr::RoundingConstraint largeLegacyRadius{wr::RoundingSide::Port, 1000.0};
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

  wr::RoundingConstraint wrapper{wr::RoundingSide::Port, 0.5};
  require(wr::roundingConstraintValid(wrapper),
          "valid rounding constraint is accepted");

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

  // Frozen user no-go zones: native polygon geometry composed with the
  // shoreline adapter, with no per-leg Python callback.
  const auto openWater = std::make_shared<wr::OpenWaterProvider>();
  const std::string zoneDigest(64, 'a');
  const wr::GeoPoint ringSouthWest{48.00, -123.60};
  const wr::GeoPoint ringSouthEast{48.00, -123.20};
  const wr::GeoPoint ringNorthEast{48.30, -123.20};
  const wr::GeoPoint ringNorthWest{48.30, -123.60};
  AvoidZone trafficZone;
  trafficZone.id = "zone-traffic";
  trafficZone.name = "Traffic separation";
  trafficZone.vertices = {ringSouthWest, ringSouthEast, ringNorthEast,
                          ringNorthWest};
  const std::vector<AvoidZone> zoneSet{trafficZone};
  validateAvoidZones(zoneSet);
  validateAvoidZoneDigest(zoneDigest);

  const auto zoneProvider = buildAvoidZoneProvider(zoneSet, zoneDigest);
  require(zoneProvider->identity() == "user-avoid-zones:" + zoneDigest,
          "the native zone provider identity does not carry the digest");
  const auto composed =
      composeAvoidZones(openWater, zoneProvider, zoneDigest, "provider");
  require(composed->identity() ==
              "open-water+avoid-zones:" + zoneDigest,
          "the composed identity does not name both halves and the digest");

  const wr::GeoPoint insideZone{48.15, -123.40};
  const wr::GeoPoint alsoInsideZone{48.25, -123.30};
  const wr::GeoPoint outsideZone{48.15, -123.90};
  const wr::GeoPoint southOfZone{47.80, -123.40};
  const wr::GeoPoint northOfZone{48.50, -123.40};
  require(composed->pointForbidden(insideZone) &&
              !composed->pointForbidden(outsideZone),
          "a zone point test does not distinguish inside from outside");
  require(near(composed->distanceToForbiddenNm(insideZone), 0.0),
          "a point inside a zone does not report zero clearance");
  require(composed->segmentForbidden(southOfZone, northOfZone, 0.0) &&
              composed->segmentFromKnownSafeForbidden(southOfZone,
                                                      northOfZone, 0.0),
          "a leg crossing a zone is not forbidden");

  // Zones are exact hard boundaries: the configured land stand-off never
  // widens them, and a leg that passes close without touching is allowed.
  const wr::GeoPoint nearSouthWest{47.995, -123.60};
  const wr::GeoPoint nearSouthEast{47.995, -123.20};
  require(!composed->segmentForbidden(nearSouthWest, nearSouthEast, 0.0) &&
              !composed->segmentForbidden(nearSouthWest, nearSouthEast, 2.0),
          "a leg passing near a zone without touching it was rejected, or "
          "the land stand-off was applied to a zone");

  // The known-safe departure exemption belongs to the shoreline buffer only.
  // The raw polygon provider would grant egress from inside a ring; the
  // composite must not.
  require(!zoneProvider->segmentFromKnownSafeForbidden(insideZone,
                                                      alsoInsideZone, 0.0),
          "raw polygon provider unexpectedly rejected an interior leg");
  require(composed->segmentFromKnownSafeForbidden(insideZone, alsoInsideZone,
                                                  0.0) &&
              composed->validationSegmentFromKnownSafeForbiddenAt(
                  insideZone, alsoInsideZone, wr::TimePoint{}, 0.0),
          "the composite extended the known-safe exemption to a zone");
  require(composed->validationSegmentFromKnownSafeForbiddenAt(
              southOfZone, northOfZone, wr::TimePoint{}, 0.0),
          "independent replay did not judge the same zone geometry");

  // The shoreline half still provides its stand-off, clearance and egress.
  const std::vector<std::vector<wr::GeoPoint>> landPolygons{{{
      47.00, -124.00}, {47.00, -123.00}, {47.10, -123.00}, {47.10, -124.00}}};
  const auto shoreline = std::make_shared<wr::PolygonBoundaryProvider>(
      landPolygons, "test-shoreline");
  const auto coastal =
      composeAvoidZones(shoreline, zoneProvider, zoneDigest, "provider");
  require(coastal->identity() ==
              "test-shoreline+avoid-zones:" + zoneDigest,
          "the composite lost the shoreline identity");
  // About 1 NM offshore of the land edge and far from the zone.
  const wr::GeoPoint harbour{47.116, -123.50};
  const wr::GeoPoint offshore{47.60, -123.50};
  const double harbourClearance = coastal->distanceToForbiddenNm(harbour);
  require(harbourClearance > 0.5 && harbourClearance < 1.2,
          "the composite did not report the worse (shoreline) clearance");
  require(!coastal->segmentFromKnownSafeForbidden(harbour, offshore, 2.0),
          "a departure from a stand-off buffer was rejected");
  require(coastal->segmentFromKnownSafeForbidden(
              harbour, wr::GeoPoint{47.05, -123.50}, 2.0),
          "the composite lost the shoreline segment rejection");
  require(near(coastal->distanceToForbiddenNm(insideZone), 0.0),
          "the zone did not win the worse-of-two clearance");

  // A ring may be sent explicitly closed; the engine closes it implicitly.
  AvoidZone closedZone = trafficZone;
  closedZone.vertices.push_back(ringSouthWest);
  require(buildAvoidZoneProvider({closedZone}, zoneDigest)
              ->pointForbidden(insideZone),
          "an explicitly closed ring was not accepted");

  const auto expectInvalid =
      [](const std::vector<AvoidZone>& bad, const std::string& needle,
         const std::string& description) {
        try {
          validateAvoidZones(bad);
        } catch (const std::invalid_argument& error) {
          require(std::string(error.what()).find(needle) != std::string::npos,
                  description + ": message does not name the zone");
          return;
        }
        require(false, description + ": invalid zone set was accepted");
      };
  AvoidZone twoVertices = trafficZone;
  twoVertices.vertices = {ringSouthWest, ringSouthEast};
  expectInvalid({twoVertices}, "zone-traffic",
                "a two-vertex ring must be rejected");
  AvoidZone repeated = trafficZone;
  repeated.vertices = {ringSouthWest, ringSouthWest, ringNorthEast};
  expectInvalid({repeated}, "zone-traffic",
                "a repeated consecutive vertex must be rejected");
  AvoidZone bowtie = trafficZone;
  bowtie.vertices = {{48.00, -123.60}, {48.20, -123.20}, {48.00, -123.20},
                     {48.20, -123.60}};
  expectInvalid({bowtie}, "zone-traffic",
                "a self-intersecting ring must be rejected");
  AvoidZone spanning = trafficZone;
  spanning.vertices = {{0.00, -170.00}, {1.00, 20.00}, {2.00, 10.00}};
  expectInvalid({spanning}, "zone-traffic",
                "a ring spanning more than 180 degrees must be rejected");
  AvoidZone nonFinite = trafficZone;
  nonFinite.vertices = {{48.00, -123.60}, {48.20, -123.20},
                        {std::nan(""), -123.20}};
  expectInvalid({nonFinite}, "zone-traffic",
                "a non-finite vertex must be rejected");
  expectInvalid({trafficZone, trafficZone}, "duplicate",
                "duplicate zone ids must be rejected");
  try {
    buildAvoidZoneProvider(zoneSet, "not-a-digest");
    require(false, "a malformed digest was accepted");
  } catch (const std::invalid_argument&) {
  }
  require(containingAvoidZoneId(zoneSet, insideZone) ==
                  std::optional<std::string>("zone-traffic") &&
              !containingAvoidZoneId(zoneSet, outsideZone),
          "the named zone for a contained point is wrong");

  // End-to-end: the engine path really consults the composed provider. Place a
  // small zone on the midpoint of the unconstrained fixture route (so the
  // baseline demonstrably crosses it) and require that any accepted
  // zone-constrained route keeps every leg out of it, including independent
  // replay.
  wr::RoutingEnvironment zoneEnvironment = fixtureEnvironment();
  const auto fixtureLand = zoneEnvironment.landAndBoundaries;
  double bestMidpointDistance = 0.0;
  wr::GeoPoint zoneCentre{};
  bool haveZoneCentre = false;
  for (const auto& leg : first.result.legs) {
    const wr::GeoPoint midpoint{(leg.start.latitude + leg.end.latitude) / 2.0,
                                (leg.start.longitude + leg.end.longitude) / 2.0};
    const double clearance = std::min(
        wr::distanceNm(midpoint, fixtureRequest().start),
        wr::distanceNm(midpoint, fixtureRequest().destination));
    if (clearance > bestMidpointDistance) {
      bestMidpointDistance = clearance;
      zoneCentre = midpoint;
      haveZoneCentre = true;
    }
  }
  if (haveZoneCentre && bestMidpointDistance > 1.0) {
    AvoidZone blockingZone;
    blockingZone.id = "zone-fixture-route";
    blockingZone.name = "Fixture route exclusion";
    blockingZone.vertices = {
        {zoneCentre.latitude - 0.010, zoneCentre.longitude - 0.010},
        {zoneCentre.latitude - 0.010, zoneCentre.longitude + 0.010},
        {zoneCentre.latitude + 0.010, zoneCentre.longitude + 0.010},
        {zoneCentre.latitude + 0.010, zoneCentre.longitude - 0.010}};
    const auto blockingProvider =
        buildAvoidZoneProvider({blockingZone}, zoneDigest);
    const auto blockingLand = composeAvoidZones(
        fixtureLand, blockingProvider, zoneDigest, "fixture");
    bool baselineCrosses = false;
    for (const auto& leg : first.result.legs)
      baselineCrosses = baselineCrosses ||
                        blockingLand->segmentForbidden(leg.start, leg.end, 0.0);
    require(baselineCrosses,
            "the fixture zone does not block the unconstrained route");
    zoneEnvironment.landAndBoundaries = blockingLand;
    const SyntheticRun zoned =
        runRequest("main", fixtureRequest(), std::move(zoneEnvironment));
    require(zoned.result.validation.passed ==
                zoned.independentValidation.passed,
            "zone search and independent replay disagree");
    if (zoned.accepted) {
      for (const auto& leg : zoned.result.legs)
        require(!blockingLand->segmentForbidden(leg.start, leg.end, 0.0),
                "an accepted zone-constrained route crosses a frozen zone");
    }
  }

  if (failures != 0) return EXIT_FAILURE;
  std::cout << "native routing selftest passed\n";
  return EXIT_SUCCESS;
}
