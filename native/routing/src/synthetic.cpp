#include "synthetic.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <exception>
#include <memory>
#include <stdexcept>
#include <utility>

#include "original_routing/Engine.h"
#include "supercpn/weather_routing/QuickEngine.h"

namespace sailworks::native_routing {
namespace {

constexpr double kHalfCell = 0.25;
const wr::TimePoint kReferenceTime{wr::Duration{kReferenceEpochSeconds}};

double hoursSinceReference(wr::TimePoint time) {
  return std::chrono::duration<double>(time - kReferenceTime).count() / 3600.0;
}

double lerp(double a, double b, double factor) {
  return a + (b - a) * factor;
}

struct HourWindow {
  double low{};
  double high{};
  double factor{};
};

HourWindow hourWindow(double hour) {
  const double clamped = std::clamp(hour, 0.0, 6.0);
  const double low = std::min(6.0, std::floor(clamped / 3.0) * 3.0);
  const double high = std::min(6.0, low + 3.0);
  const double factor = high > low ? (clamped - low) / (high - low) : 0.0;
  return {low, high, factor};
}

double fieldU(int i, int j, double hour) {
  return 6.0 + 0.5 * i + 1.0 * j + 1.5 * hour;
}
double fieldV(int i, int j, double hour) {
  return 2.0 + 0.25 * i - 0.5 * j + 0.5 * hour;
}
double fieldPressure(int i, int j, double hour) {
  return 101200.0 + 100.0 * i + 200.0 * j + 50.0 * hour;
}
double fieldCurrentEast(int i, int j, double hour) {
  return 0.20 + 0.01 * i + 0.02 * j + 0.01 * hour;
}
double fieldCurrentNorth(int i, int j, double hour) {
  return -0.10 + 0.01 * i - 0.01 * j + 0.005 * hour;
}

wr::EnvironmentalSourceMetadata syntheticMetadata(wr::TimePoint time) {
  wr::EnvironmentalSourceMetadata metadata;
  metadata.source = wr::EnvironmentalSource::SyntheticTestField;
  metadata.datasetIdentity = kFixtureId;
  metadata.modelIdentity = "native-routing-fixture-formulas-v1";
  metadata.modelRunTime = kReferenceTime;
  metadata.sourceTimestamp = time;
  metadata.spatialResolutionDegrees = kGridDx;
  return metadata;
}

}  // namespace

wr::ParameterCoverage FixtureWeatherProvider::coverage(
    const char* identity) const {
  wr::ParameterCoverage result;
  result.available = true;
  result.begins = kReferenceTime;
  result.ends = kReferenceTime + std::chrono::hours{kLastValidHour};
  result.area = wr::GeoEnvelope{kGridLon0 - kHalfCell,
                                kGridLat0 - kHalfCell,
                                kGridLon0 + (kGridNx - 1) * kGridDx + kHalfCell,
                                kGridLat0 + (kGridNy - 1) * kGridDy + kHalfCell};
  result.datasetIdentity = identity;
  return result;
}

wr::ParameterCoverage FixtureWeatherProvider::windCoverage() const {
  return coverage("native-routing-fixture-wind");
}

wr::ParameterCoverage FixtureWeatherProvider::currentCoverage() const {
  return coverage("native-routing-fixture-current");
}

wr::ParameterCoverage FixtureWeatherProvider::waveCoverage() const {
  wr::ParameterCoverage result;
  result.available = false;
  result.datasetIdentity = "native-routing-fixture-waves-absent";
  return result;
}

bool FixtureWeatherProvider::covered(wr::GeoPoint position,
                                     wr::TimePoint time) const {
  const auto area = windCoverage().area;
  const double hour = hoursSinceReference(time);
  return position.latitude >= area.south && position.latitude <= area.north &&
         position.longitude >= area.west && position.longitude <= area.east &&
         hour >= 0.0 && hour <= static_cast<double>(kLastValidHour);
}

FixtureWeatherProvider::Observables FixtureWeatherProvider::sample(
    wr::GeoPoint position, wr::TimePoint time) const {
  const int i = std::clamp(
      static_cast<int>(std::lround((position.longitude - kGridLon0) / kGridDx)),
      0, kGridNx - 1);
  const int j = std::clamp(
      static_cast<int>(std::lround((position.latitude - kGridLat0) / kGridDy)),
      0, kGridNy - 1);
  const HourWindow window = hourWindow(hoursSinceReference(time));
  auto interpolate = [&](auto field) {
    return lerp(field(i, j, window.low), field(i, j, window.high),
                window.factor);
  };
  return {interpolate(fieldU), interpolate(fieldV),
          interpolate(fieldPressure), interpolate(fieldCurrentEast),
          interpolate(fieldCurrentNorth)};
}

wr::WindSample FixtureWeatherProvider::wind(wr::GeoPoint position,
                                            wr::TimePoint time) const {
  if (!covered(position, time)) return {};
  const Observables values = sample(position, time);
  wr::WindSample result;
  result.available = true;
  result.velocity = wr::Vector2{values.windU * kKnotsPerMetreSecond,
                                values.windV * kKnotsPerMetreSecond};
  result.metadata = syntheticMetadata(time);
  return result;
}

wr::CurrentSample FixtureWeatherProvider::current(wr::GeoPoint position,
                                                  wr::TimePoint time) const {
  if (!covered(position, time)) return {};
  const Observables values = sample(position, time);
  wr::CurrentSample result;
  result.available = true;
  result.velocity = wr::Vector2{values.currentEast * kKnotsPerMetreSecond,
                                values.currentNorth * kKnotsPerMetreSecond};
  result.metadata = syntheticMetadata(time);
  return result;
}

wr::WaveSample FixtureWeatherProvider::waves(wr::GeoPoint,
                                             wr::TimePoint time) const {
  wr::WaveSample result;
  result.available = false;
  result.metadata = syntheticMetadata(time);
  result.metadata.source = wr::EnvironmentalSource::Missing;
  result.metadata.fallbackReason = "synthetic fixture has no wave field";
  return result;
}

wr::VesselConfiguration fixtureVesselConfiguration() {
  wr::PerformanceProfile profile;
  profile.role = wr::ProfileRole::SailOnly;
  profile.identity = "native-routing-fixture-polar";
  profile.assumptions = "boatspeed_kn = 0.02*twa_deg + 0.1*tws_kn";
  for (const double tws : {4.0, 8.0, 12.0, 16.0}) {
    wr::PolarRow row;
    row.trueWindSpeedKnots = tws;
    for (const double twa : kPolarTwaDegrees) {
      row.points.push_back(
          wr::PolarPoint{twa, 0.02 * twa + 0.1 * tws});
    }
    profile.rows.push_back(std::move(row));
  }
  wr::VesselConfiguration vessel;
  vessel.profiles = {std::move(profile)};
  vessel.propulsion.allowSailing = true;
  vessel.propulsion.allowMotorSailing = false;
  vessel.propulsion.allowMotor = false;
  return vessel;
}

wr::RoutingRequest fixtureRequest(bool cancelled, bool invalidDestination) {
  wr::RoutingRequest request;
  request.start = wr::GeoPoint{48.2, -123.7};
  // The valid destination is deliberately farther than the engine's direct
  // completion envelope (max(destinationToleranceNm, timeStepHours * 20) NM),
  // so the search must retain isochrone layers instead of completing from the
  // start node without any inspection geometry. It stays within the synthetic
  // forecast's six-hour validity window.
  request.destination =
      invalidDestination ? wr::GeoPoint{48.2, -120.0} : wr::GeoPoint{48.4, -123.5};
  request.departure = kReferenceTime;
  request.vessel = fixtureVesselConfiguration();
  request.environment.useWind = true;
  request.environment.useCurrent = true;
  request.environment.useWaves = false;
  request.environment.missingCurrent = wr::MissingCurrentPolicy::Disallow;
  request.objective.kind = wr::ObjectiveKind::FastestUnderSafetyLimits;
  request.constraints.minimumTrueWindAngleDegrees = 0.0;
  request.constraints.maximumTrueWindAngleDegrees = 180.0;
  request.constraints.maximumLatitudeDegrees = 89.0;
  request.options.timeStep = std::chrono::minutes{30};
  request.options.minimumTimeStep = std::chrono::minutes{10};
  request.options.headingStepDegrees = 15.0;
  request.options.refinedHeadingStepDegrees = 5.0;
  request.options.maximumSearchAngleDegrees = 180.0;
  request.options.destinationToleranceNm = 0.5;
  request.options.spatialCellNm = 1.0;
  request.options.routingEffortPercent = 100;
  request.limits.maximumRouteDuration = std::chrono::hours{6};
  request.limits.maximumGeneratedStates = 500000;
  request.limits.maximumRetainedStates = 50000;
  request.limits.maximumExplorationDistanceNm = 500.0;
  if (cancelled) request.cancellation.cancel();
  return request;
}

wr::RoutingEnvironment fixtureEnvironment() {
  wr::RoutingEnvironment environment;
  environment.grib = std::make_shared<FixtureWeatherProvider>();
  environment.landAndBoundaries = std::make_shared<wr::OpenWaterProvider>();
  environment.performance = std::make_shared<wr::PolarPerformanceModel>(
      fixtureVesselConfiguration());
  environment.memberIdentity = "native-routing-fixture-member";
  return environment;
}

bool isCompleteStatus(wr::RoutingStatus status) {
  switch (status) {
    case wr::RoutingStatus::Complete:
    case wr::RoutingStatus::CompleteUsingReverseRecovery:
    case wr::RoutingStatus::CompleteUsingFrontierRecovery:
    case wr::RoutingStatus::CompleteUsingGraphFallback:
      return true;
    default:
      return false;
  }
}

std::string statusName(wr::RoutingStatus status) {
  switch (status) {
    case wr::RoutingStatus::Complete: return "complete";
    case wr::RoutingStatus::CompleteUsingReverseRecovery:
      return "complete_using_reverse_recovery";
    case wr::RoutingStatus::CompleteUsingFrontierRecovery:
      return "complete_using_frontier_recovery";
    case wr::RoutingStatus::CompleteUsingGraphFallback:
      return "complete_using_graph_fallback";
    case wr::RoutingStatus::NoFeasibleRoute: return "no_feasible_route";
    case wr::RoutingStatus::SearchIncomplete: return "search_incomplete";
    case wr::RoutingStatus::ResourceLimitReached: return "resource_limit_reached";
    case wr::RoutingStatus::WindForecastRequired: return "wind_forecast_required";
    case wr::RoutingStatus::CurrentDataRequired: return "current_data_required";
    case wr::RoutingStatus::WaveDataRequired: return "wave_data_required";
    case wr::RoutingStatus::WeatherCoverageInsufficient:
      return "weather_coverage_insufficient";
    case wr::RoutingStatus::InvalidPolar: return "invalid_polar";
    case wr::RoutingStatus::InvalidVesselConfiguration:
      return "invalid_vessel_configuration";
    case wr::RoutingStatus::InvalidStart: return "invalid_start";
    case wr::RoutingStatus::InvalidDestination: return "invalid_destination";
    case wr::RoutingStatus::ValidationFailure: return "validation_failure";
    case wr::RoutingStatus::Cancelled: return "cancelled";
    case wr::RoutingStatus::InternalError: return "internal_error";
  }
  return "unknown";
}

std::string solverPathName(wr::SolverPath path) {
  switch (path) {
    case wr::SolverPath::None: return "none";
    case wr::SolverPath::AdaptiveIsochrone: return "adaptive_isochrone";
    case wr::SolverPath::ReverseRecovery: return "reverse_recovery";
    case wr::SolverPath::FrontierRecovery: return "frontier_recovery";
    case wr::SolverPath::GraphFallback: return "graph_fallback";
    case wr::SolverPath::QuickBeam: return "quick_beam";
    case wr::SolverPath::AlternativeSector: return "alternative_sector";
  }
  return "unknown";
}

namespace {

void mixDigest(unsigned long long& hash, const void* data, std::size_t size) {
  const auto* bytes = static_cast<const unsigned char*>(data);
  for (std::size_t index = 0; index < size; ++index) {
    hash ^= bytes[index];
    hash *= 1099511628211ULL;
  }
}

void mixDigest(unsigned long long& hash, double value) {
  char buffer[64];
  const int written = std::snprintf(buffer, sizeof(buffer), "%.12g", value);
  if (written > 0) {
    mixDigest(hash, buffer, static_cast<std::size_t>(
                               std::min<int>(written, sizeof(buffer) - 1)));
  }
}

void mixDigest(unsigned long long& hash, std::int64_t value) {
  mixDigest(hash, &value, sizeof(value));
}

SyntheticRun runWithEngine(const std::string& engine,
                           const wr::RoutingRequest& request,
                           const wr::RoutingEnvironment& environment) {
  if (engine != "main" && engine != "standard" && engine != "original" &&
      engine != "professional") {
    throw std::invalid_argument("unknown synthetic engine: " + engine);
  }
  SyntheticRun run;
  run.engine = engine;
  run.request = request;
  run.environment = environment;
  try {
    if (engine == "main") {
      run.result = wr::RoutingEngine{}.route(run.request, run.environment);
    } else if (engine == "standard") {
      run.result = wr::QuickRoutingEngine{}.route(run.request, run.environment).route;
    } else if (engine == "original") {
      original_routing::Options options;
      options.captureVisualization = true;
      run.result = original_routing::Engine{}.route(run.request, run.environment,
                                                    options);
    } else {
      original_routing::Options options;
      options.captureVisualization = true;
      run.incumbentAttempted = true;
      const wr::RoutingResult quick = original_routing::Engine{}.route(
          run.request, run.environment, options);
      run.incumbentStatus = quick.status;
      if (run.request.cancellation.cancelled() ||
          quick.status == wr::RoutingStatus::Cancelled) {
        run.result.status = wr::RoutingStatus::Cancelled;
        run.result.message = "Professional incumbent search cancelled";
      } else {
        run.incumbentValidated =
            isCompleteStatus(quick.status) && quick.validation.passed &&
            !quick.legs.empty() &&
            wr::RouteValidator{}
                .validate(run.request, run.environment,
                          *run.environment.performance, quick.legs)
                .passed;
        if (run.incumbentValidated) {
          run.incumbentForwarded = true;
          run.result = wr::ProfessionalEngine{}.route(
              run.request, run.environment, &quick);
        } else {
          run.result =
              wr::ProfessionalEngine{}.route(run.request, run.environment);
        }
      }
    }
  } catch (const std::exception& error) {
    run.result.status = wr::RoutingStatus::InternalError;
    run.result.message = error.what();
  }
  run.independentValidation = wr::RouteValidator{}.validate(
      run.request, run.environment, *run.environment.performance,
      run.result.legs);
  run.accepted = isCompleteStatus(run.result.status) &&
                 run.result.validation.passed &&
                 run.independentValidation.passed && !run.result.legs.empty();
  return run;
}

}  // namespace

SyntheticRun runSynthetic(const std::string& engine) {
  return runWithEngine(engine, fixtureRequest(false, false),
                       fixtureEnvironment());
}

SyntheticRun runCancelled(const std::string& engine) {
  return runWithEngine(engine, fixtureRequest(true, false),
                       fixtureEnvironment());
}

SyntheticRun runWithProvider(const std::string& engine,
                             std::shared_ptr<wr::WeatherProvider> provider,
                             bool cancelled,
                             std::optional<wr::VesselConfiguration> vessel) {
  wr::RoutingEnvironment environment = fixtureEnvironment();
  environment.grib = std::move(provider);
  wr::RoutingRequest request = fixtureRequest(cancelled, false);
  if (vessel.has_value()) {
    request.vessel = *vessel;
    environment.performance =
        std::make_shared<wr::PolarPerformanceModel>(*vessel);
  }
  return runWithEngine(engine, request, environment);
}

SyntheticRun runInvalidDestination() {
  return runWithEngine("main", fixtureRequest(false, true),
                       fixtureEnvironment());
}

std::string routeDigest(const wr::RoutingResult& result) {
  unsigned long long hash = 14695981039346656037ULL;
  mixDigest(hash, static_cast<std::int64_t>(result.status));
  mixDigest(hash, static_cast<std::int64_t>(result.legs.size()));
  mixDigest(hash, static_cast<std::int64_t>(
                      result.metrics.elapsed.count()));
  for (const auto& leg : result.legs) {
    mixDigest(hash, leg.start.latitude);
    mixDigest(hash, leg.start.longitude);
    mixDigest(hash, leg.end.latitude);
    mixDigest(hash, leg.end.longitude);
    mixDigest(hash, static_cast<std::int64_t>(leg.startTime.time_since_epoch().count()));
    mixDigest(hash, static_cast<std::int64_t>(leg.endTime.time_since_epoch().count()));
    mixDigest(hash, leg.headingDegrees);
    mixDigest(hash, leg.speedThroughWaterKnots);
    mixDigest(hash, leg.speedOverGroundKnots);
    mixDigest(hash, leg.wind.eastKnots);
    mixDigest(hash, leg.wind.northKnots);
    mixDigest(hash, leg.current.eastKnots);
    mixDigest(hash, leg.current.northKnots);
  }
  char text[32];
  std::snprintf(text, sizeof(text), "%016llx", hash);
  return std::string(text);
}

FixtureObservables fixtureObservables() {
  FixtureWeatherProvider weather;
  const wr::GeoPoint southWest{48.0, -124.0};
  const wr::GeoPoint northEast{50.0, -122.0};
  const auto hour = [](int value) {
    return kReferenceTime + std::chrono::hours{value};
  };
  FixtureObservables result;
  result.windSouthWestHour0 = weather.wind(southWest, hour(0));
  result.windNorthEastHour0 = weather.wind(northEast, hour(0));
  result.windSouthWestHour1 = weather.wind(southWest, hour(1));
  result.pressureSouthWestHour0Pa =
      weather.sample(southWest, hour(0)).pressurePa;
  result.pressureNorthEastHour0Pa =
      weather.sample(northEast, hour(0)).pressurePa;
  result.currentSouthWestHour0 = weather.current(southWest, hour(0));
  result.currentNorthEastHour0 = weather.current(northEast, hour(0));
  result.currentNorthEastHour3 = weather.current(northEast, hour(3));
  result.currentNorthEastHour6 = weather.current(northEast, hour(6));
  result.polarSpeed60At8 = 0.02 * 60.0 + 0.1 * 8.0;
  result.polarSpeed90At12 = 0.02 * 90.0 + 0.1 * 12.0;
  result.polarSpeed180At16 = 0.02 * 180.0 + 0.1 * 16.0;
  return result;
}

}  // namespace sailworks::native_routing
