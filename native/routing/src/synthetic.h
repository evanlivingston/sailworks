#pragma once

#include <array>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>

#include "supercpn/weather_routing/Engine.h"
#include "supercpn/weather_routing/Providers.h"

namespace sailworks::native_routing {

namespace wr = supercpn::weather_routing;

// Frozen synthetic fixture identity (test/native_routing_fixtures.py).
inline constexpr const char* kFixtureId = "native-routing-synthetic-v1";
inline constexpr int kFixtureRevision = 1;
inline constexpr double kKnotsPerMetreSecond = 3600.0 / 1852.0;
inline constexpr double kGridLon0 = -124.0;
inline constexpr double kGridLat0 = 48.0;
inline constexpr double kGridDx = 0.5;
inline constexpr double kGridDy = 0.5;
inline constexpr int kGridNx = 5;
inline constexpr int kGridNy = 5;
inline constexpr std::int64_t kReferenceEpochSeconds = 1767225600; // 2026-01-01Z
inline constexpr int kLastValidHour = 6;
// Frozen contract polar angles (test/native_routing_fixtures.py POLAR_TWA).
// There is deliberately no 0-degree row; the engine clamps lower angles to
// the 30-degree row.
inline constexpr std::array<double, 6> kPolarTwaDegrees{
    30.0, 60.0, 90.0, 120.0, 150.0, 180.0};

// Engine-independent synthetic field used by the bridge and its selftest.
// It implements the frozen analytic formulas, not a reimplementation of the
// production sampler.
class FixtureWeatherProvider final : public wr::WeatherProvider {
 public:
  [[nodiscard]] wr::ParameterCoverage windCoverage() const override;
  [[nodiscard]] wr::ParameterCoverage currentCoverage() const override;
  [[nodiscard]] wr::ParameterCoverage waveCoverage() const override;
  [[nodiscard]] wr::WindSample wind(wr::GeoPoint position,
                                    wr::TimePoint time) const override;
  [[nodiscard]] wr::CurrentSample current(wr::GeoPoint position,
                                           wr::TimePoint time) const override;
  [[nodiscard]] wr::WaveSample waves(wr::GeoPoint position,
                                     wr::TimePoint time) const override;
  [[nodiscard]] std::string identity() const override {
    return "native-routing-fixture-weather";
  }

  struct Observables {
    double windU{};
    double windV{};
    double pressurePa{};
    double currentEast{};
    double currentNorth{};
  };

  [[nodiscard]] bool covered(wr::GeoPoint position,
                             wr::TimePoint time) const;
  [[nodiscard]] Observables sample(wr::GeoPoint position,
                                   wr::TimePoint time) const;
  [[nodiscard]] wr::ParameterCoverage coverage(const char* identity) const;
};

struct SyntheticRun {
  std::string engine;
  // The contour incumbent is a separate solve, never an accepted result by
  // itself when Professional is requested.
  bool incumbentAttempted{};
  bool incumbentForwarded{};
  wr::RoutingStatus incumbentStatus{};
  bool incumbentValidated{};
  wr::RoutingRequest request;
  wr::RoutingEnvironment environment;
  wr::RoutingResult result;
  wr::RouteValidationResult independentValidation;
  bool accepted{};
};

// Narrow request override for weather supplied by an external provider. Only
// the request's start, destination and departure can be relocated; polar,
// constraints, options and limits stay frozen. Empty members keep the frozen
// fixture request values.
struct FixtureRequestOverride {
  std::optional<wr::GeoPoint> start;
  std::optional<wr::GeoPoint> destination;
  std::optional<wr::TimePoint> departure;
};

wr::VesselConfiguration fixtureVesselConfiguration();
wr::RoutingRequest fixtureRequest(
    bool cancelled = false, bool invalidDestination = false,
    const FixtureRequestOverride& overrides = {});
wr::RoutingEnvironment fixtureEnvironment();

SyntheticRun runSynthetic(const std::string& engine,
                          wr::RoutingProgressCallback progress = {});
SyntheticRun runCancelled(const std::string& engine);
SyntheticRun runInvalidDestination();

// Run the same synthetic scenario but with weather supplied by an external
// provider (for example a Python adapter over established Sailworks sampling).
// The fixture polar, constraints and request limits are unchanged. Optional
// request overrides relocate start/destination/departure for external weather
// sources; when absent the frozen synthetic request is used verbatim.
SyntheticRun runWithProvider(
    const std::string& engine, std::shared_ptr<wr::WeatherProvider> provider,
    bool cancelled = false,
    std::optional<wr::VesselConfiguration> vessel = std::nullopt,
    const FixtureRequestOverride& requestOverrides = {},
    wr::RoutingProgressCallback progress = {});

// Explicit user request and environment: no fixture defaults or fixture data.
SyntheticRun runRequest(const std::string& engine, wr::RoutingRequest request,
                        wr::RoutingEnvironment environment);

struct ChainSegment {
  std::size_t index{};
  wr::RoutingStatus status{wr::RoutingStatus::InternalError};
  bool accepted{};
  bool engineValidationPassed{};
  bool independentValidationPassed{};
  std::size_t legCount{};
  std::string message;
};

struct ChainRun {
  SyntheticRun result;
  std::vector<ChainSegment> segments;
  int failedSegment{-1};
};

// Route through a waypoint chain exactly as the legacy workflow does: each
// segment starts at the previous delivered position and departs at the previous
// arrival time. The whole delivered route is then replayed once by the
// engine's independent validator so joins and cumulative times are included.
ChainRun runUserChain(const std::string& engine, wr::RoutingRequest request,
                      wr::RoutingEnvironment environment,
                      const std::vector<wr::GeoPoint>& chain);

[[nodiscard]] bool isCompleteStatus(wr::RoutingStatus status);
[[nodiscard]] std::string statusName(wr::RoutingStatus status);
[[nodiscard]] std::string solverPathName(wr::SolverPath path);
[[nodiscard]] std::string routeDigest(const wr::RoutingResult& result);

// Fixture observables reported exactly as the C++ provider samples them.
struct FixtureObservables {
  wr::WindSample windSouthWestHour0;
  wr::WindSample windNorthEastHour0;
  wr::WindSample windSouthWestHour1;
  double pressureSouthWestHour0Pa{};
  double pressureNorthEastHour0Pa{};
  wr::CurrentSample currentSouthWestHour0;
  wr::CurrentSample currentNorthEastHour0;
  wr::CurrentSample currentNorthEastHour3;
  wr::CurrentSample currentNorthEastHour6;
  double polarSpeed60At8{};
  double polarSpeed90At12{};
  double polarSpeed180At16{};
};
FixtureObservables fixtureObservables();

}  // namespace sailworks::native_routing
