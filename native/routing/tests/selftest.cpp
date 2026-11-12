#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#include "native_routing_tree_identity.h"
#include "sailworks_performance.h"
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
