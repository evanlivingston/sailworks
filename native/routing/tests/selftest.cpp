#include <cmath>
#include <cstdlib>
#include <iostream>
#include <string>

#include "native_routing_tree_identity.h"
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

  const SyntheticRun first = runSynthetic("main");
  const SyntheticRun second = runSynthetic("main");
  require(first.accepted, "main synthetic route was not accepted");
  require(first.result.validation.passed,
          "main synthetic engine validation did not pass");
  require(first.independentValidation.passed,
          "independent replay validation did not pass");
  require(routeDigest(first.result) == routeDigest(second.result),
          "two synthetic routes are not deterministic");

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

  const SyntheticRun invalid = runInvalidDestination();
  require(!invalid.accepted, "invalid destination reported accepted completion");

  if (failures != 0) return EXIT_FAILURE;
  std::cout << "native routing selftest passed\n";
  return EXIT_SUCCESS;
}
