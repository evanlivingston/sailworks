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
#include "supercpn/weather_routing/Winding.h"
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
  const wr::RoundingConstraint port{wr::RoundingSide::Port, 0.5, std::nullopt,
                                    std::nullopt};
  const wr::GeoPoint east = wr::destinationPoint(mark, 90.0, 0.4);
  const auto portMeasurement = wr::measureRounding(port, east, 0.0, mark);
  require(portMeasurement.satisfied,
          "port rounding east of the mark with a north course is satisfied");
  require(portMeasurement.failure == wr::RoundingFailure::None,
          "satisfied port rounding has no failure code");
  const wr::GeoPoint south = wr::destinationPoint(mark, 180.0, 0.4);
  require(wr::measureRounding(port, east, 0.0, mark,
                              wr::RoundingApproach{south, 90.0})
              .satisfied,
          "port closest pass on the port side is accepted");
  require(wr::measureRounding(port, east, 0.0, mark,
                              wr::RoundingApproach{south, 270.0})
                  .failure == wr::RoundingFailure::WrongSide,
          "a starboard closest pass is rejected for a port rounding");
  wr::RoundingConstraint starboard = port;
  starboard.side = wr::RoundingSide::Starboard;
  require(!wr::measureRounding(starboard, east, 0.0, mark).satisfied,
          "starboard rejects a mark to port");
  require(wr::measureRounding(port, wr::destinationPoint(mark, 90.0, 0.6),
                              0.0, mark)
                  .failure == wr::RoundingFailure::DistanceOutOfRange,
          "rounding outside the radius is rejected");
  wr::RoundingConstraint exit = port;
  exit.exitBearingMinDegrees = 180.0;
  exit.exitBearingMaxDegrees = 190.0;
  require(wr::measureRounding(exit, east, 0.0, mark).failure ==
              wr::RoundingFailure::ExitBearingOutOfRange,
          "rounding outside the exit range is rejected");
  wr::RoundingConstraint boundary = port;
  boundary.exitBearingMinDegrees = 0.0;
  boundary.exitBearingMaxDegrees = 0.0;
  require(wr::measureRounding(boundary, east, 0.0, mark).satisfied,
          "inclusive exit-range boundary is accepted");
  require(wr::measureRounding(boundary, east, 0.1, mark).failure ==
              wr::RoundingFailure::ExitBearingOutOfRange,
          "exit bearing just outside the range is rejected");
  require(wr::roundingConstraintValid(port),
          "valid rounding spec is accepted");
  wr::RoundingConstraint tiny = port;
  tiny.radiusNm = 0.005;
  require(wr::roundingGeometricallyImpossible(tiny),
          "a radius below the lateral epsilon is unsatisfiable");
  exit.exitBearingMaxDegrees = 20.0;
  require(!wr::roundingConstraintValid(exit),
          "wrapped exit range is rejected");
  const auto targets = wr::roundingTargets(wr::GeoPoint{47.9, -123.0}, mark,
                                           port);
  require(!targets.empty(), "rounding targets are generated");
  require(near(wr::distanceNm(targets.front(), mark), 0.5, 1e-3),
          "rounding target lies on the radius circle");

  // Shared taut-string winding evaluator: side-of-track rounding contract.
  const wr::RoundingSpec portSpec{wr::RoundingSide::Port, std::nullopt,
                                  std::nullopt};
  const wr::RoundingSpec starboardSpec{wr::RoundingSide::Starboard,
                                       std::nullopt, std::nullopt};
  const auto loopTrack = [](const wr::GeoPoint& centre, double radiusNm,
                            double startBearing, double sweepDegrees,
                            int steps) {
    std::vector<wr::GeoPoint> track;
    for (int step = 0; step <= steps; ++step) {
      const double fraction = static_cast<double>(step) / steps;
      track.push_back(wr::destinationPoint(
          centre, startBearing + sweepDegrees * fraction, radiusNm));
    }
    return track;
  };

  // A decreasing compass bearing is counter-clockwise: the mark stays on the
  // boat's port side and the winding is positive.
  const auto portTrack = loopTrack(mark, 1.0, 90.0, -359.9999, 72);
  const auto portPass = wr::measureWinding(portTrack, mark, portSpec);
  require(portPass.satisfied && portPass.defined,
          "port loop is accepted as a port rounding");
  require(near(portPass.winding, 1.0, 1e-6) && portPass.passCount == 1,
          "port loop has winding +1 and one pass");
  require(portPass.achievedSide == wr::RoundingSide::Port &&
              portPass.failure == wr::WindingFailure::None &&
              portPass.failureCode() == "none",
          "port loop reports the port side and no failure");

  const auto starboardTrack = loopTrack(mark, 1.0, 90.0, 359.9999, 72);
  const auto starboardPass =
      wr::measureWinding(starboardTrack, mark, starboardSpec);
  require(starboardPass.satisfied && near(starboardPass.winding, -1.0, 1e-6),
          "starboard loop has winding -1 and is accepted");
  require(starboardPass.achievedSide == wr::RoundingSide::Starboard,
          "starboard loop reports the starboard side");

  // An ordinary half-turn pass is integral only because of the closing chord:
  // the reduced sub-track alone sweeps a fractional turn.
  const std::vector<wr::GeoPoint> ordinaryPass{
      wr::destinationPoint(mark, 200.0, 1.0),
      wr::destinationPoint(mark, 90.0, 1.5),
      wr::destinationPoint(mark, 340.0, 1.0)};
  double openSweepDegrees = 0.0;
  double openPrevious = wr::initialBearingDegrees(mark, ordinaryPass.front());
  for (std::size_t index = 1; index < ordinaryPass.size(); ++index) {
    const double bearing =
        wr::initialBearingDegrees(mark, ordinaryPass[index]);
    openSweepDegrees +=
        -std::remainder(bearing - openPrevious, 360.0);
    openPrevious = bearing;
  }
  const double openTurns = openSweepDegrees / 360.0;
  require(std::abs(openTurns - std::round(openTurns)) > 1e-6,
          "an ordinary pass is not integral before the closing chord");
  const auto ordinary = wr::measureWinding(ordinaryPass, mark, portSpec);
  require(ordinary.satisfied && ordinary.defined &&
              near(ordinary.winding, 1.0, 1e-6) &&
              ordinary.achievedSide == wr::RoundingSide::Port,
          "the closing chord makes an ordinary port pass integral");

  require(wr::measureWinding(portTrack, mark, starboardSpec).failure ==
              wr::WindingFailure::WrongSide,
          "a correct pass on the other side is rejected as wrong side");
  require(wr::measureWinding(portTrack, mark, starboardSpec).failureCode() ==
              "rounding_wrong_side",
          "wrong-side failure has a stable code");

  // Out and back around different radii leaves the net winding at zero: the
  // mark was never passed, even though the terminal endpoint is far away.
  std::vector<wr::GeoPoint> zeroTrack;
  for (int step = 0; step <= 18; ++step)
    zeroTrack.push_back(wr::destinationPoint(mark, step * 5.0, 2.0));
  for (int step = 17; step >= 0; --step)
    zeroTrack.push_back(wr::destinationPoint(mark, step * 5.0, 1.0));
  const auto zeroPass = wr::measureWinding(zeroTrack, mark, portSpec);
  require(!zeroPass.satisfied &&
              zeroPass.failure == wr::WindingFailure::NotPassed,
          "zero net winding means the mark was not passed");
  require(zeroPass.failureReason() == "mark not passed on this leg",
          "not-passed failure names the reason");

  // Two laps on the requested side are rejected, never accepted as a second
  // pass.
  const auto doublePort =
      wr::measureWinding(loopTrack(mark, 1.0, 90.0, -719.9999, 144), mark,
                         portSpec);
  require(doublePort.failure == wr::WindingFailure::CirclesMoreThanOnce &&
              doublePort.achievedSide == wr::RoundingSide::Port &&
              doublePort.passCount == 2,
          "two port laps are rejected despite the matching side");
  require(doublePort.failureReason() ==
                  "route circles the rounding mark more than once" &&
              doublePort.failureCode() ==
                  "route_circles_rounding_mark_more_than_once",
          "multi-lap failure carries the named reason");

  // Many short legs whose local bearing increments disagree still decide on
  // the net taut string.
  std::vector<wr::GeoPoint> zigzag;
  int contestedIncrements = 0;
  double previousBearing = 0.0;
  for (int step = 0; step <= 360; ++step) {
    const double fraction = static_cast<double>(step) / 360.0;
    const double bearing =
        90.0 - 360.0 * fraction -
        20.0 * std::sin(12.0 * static_cast<double>(std::numbers::pi) *
                        fraction);
    zigzag.push_back(
        wr::destinationPoint(mark, bearing, step % 2 == 0 ? 1.0 : 1.4));
    if (step > 0) {
      const double current =
          wr::initialBearingDegrees(mark, zigzag.back());
      if (std::remainder(current - previousBearing, 360.0) > 0.0)
        ++contestedIncrements;
      previousBearing = current;
    } else {
      previousBearing = wr::initialBearingDegrees(mark, zigzag.back());
    }
  }
  const auto zigzagPass = wr::measureWinding(zigzag, mark, portSpec);
  require(contestedIncrements > 0,
          "many-leg track has locally reversed bearing increments");
  require(zigzagPass.satisfied &&
              near(zigzagPass.winding, 1.0, 1e-6) &&
              zigzagPass.achievedSide == wr::RoundingSide::Port,
          "net winding accepts a many-leg port pass");

  // A square-ish closed zigzag at the integer boundary is unambiguous on both
  // sides. A delivered near-lap is completed by its chord, and a sub-track
  // that already ends at its start has a zero-length chord to skip.
  const auto squarePort = loopTrack(mark, 2.0, 45.0, -359.9999, 4);
  const auto squareStarboard = loopTrack(mark, 2.0, 45.0, 359.9999, 4);
  require(wr::measureWinding(squarePort, mark, portSpec).satisfied &&
              near(wr::measureWinding(squarePort, mark, portSpec).winding, 1.0,
                   1e-6),
          "square-ish port zigzag lands exactly on integral winding");
  require(wr::measureWinding(squareStarboard, mark, starboardSpec).satisfied &&
              near(wr::measureWinding(squareStarboard, mark, starboardSpec)
                       .winding,
                   -1.0, 1e-6),
          "square-ish starboard zigzag lands exactly on integral winding");
  // The closing chord is part of the closed loop, so a delivered arc a small
  // fraction short of a full turn is still exactly one lap.
  const auto nearLap = wr::measureWinding(
      loopTrack(mark, 1.0, 90.0, -359.999, 72), mark, portSpec);
  require(nearLap.satisfied && near(nearLap.winding, 1.0, 1e-6),
          "the closing chord completes a near-lap to one lap");
  // A sub-track that already ends at its start has a zero-length chord: skip
  // it rather than treating it as a chord through the mark.
  const auto explicitLap = wr::measureWinding(
      loopTrack(mark, 1.0, 90.0, -360.0, 72), mark, portSpec);
  require(explicitLap.satisfied && near(explicitLap.winding, 1.0, 1e-6),
          "zero-length closing chords are skipped, not rejected");

  // Degenerate tracks: on the mark, through the mark, a closing chord through
  // the mark, and a pure retrace.
  require(wr::measureWinding({mark, wr::destinationPoint(mark, 0.0, 1.0)},
                             mark, portSpec)
                  .failure == wr::WindingFailure::Ambiguous,
          "a sub-track starting on the mark is ambiguous");
  require(wr::measureWinding({wr::destinationPoint(mark, 180.0, 1.0),
                              wr::destinationPoint(mark, 0.0, 1.0)},
                             mark, portSpec)
                  .failure == wr::WindingFailure::Ambiguous,
          "a sub-track passing through the mark is ambiguous");
  require(wr::measureWinding({wr::destinationPoint(mark, 0.0, 1.0),
                              wr::destinationPoint(mark, 90.0, 1.0),
                              wr::destinationPoint(mark, 180.0, 1.0)},
                             mark, portSpec)
                  .failure == wr::WindingFailure::Ambiguous,
          "a closing chord through the mark is ambiguous");
  const wr::GeoPoint retraceStart = wr::destinationPoint(mark, 90.0, 1.0);
  const wr::GeoPoint retraceFar = wr::destinationPoint(mark, 90.0, 5.0);
  const wr::GeoPoint retraceEnd = wr::destinationPoint(mark, 180.0, 1.0);
  const std::vector<wr::GeoPoint> retraceTrack{
      retraceStart, retraceFar, retraceStart, retraceEnd};
  const auto reducedRetrace = wr::reduceTautString(retraceTrack);
  require(reducedRetrace.size() == 2 &&
              near(wr::distanceNm(reducedRetrace[0], retraceStart), 0.0) &&
              near(wr::distanceNm(reducedRetrace[1], retraceEnd), 0.0),
          "taut-string reduction collapses a retrace to outbound geometry");
  // The out-and-back excursion must contribute nothing to the closed loop.
  const auto retraceMeasurement =
      wr::measureWinding(retraceTrack, mark, portSpec);
  const auto outboundMeasurement =
      wr::measureWinding({retraceStart, retraceEnd}, mark, portSpec);
  require(retraceMeasurement.defined == outboundMeasurement.defined &&
              near(retraceMeasurement.winding, outboundMeasurement.winding,
                   1e-6),
          "a retrace contributes nothing to the closed-loop winding");

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
