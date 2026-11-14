#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <memory>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "native_routing_tree_identity.h"
#include "sailworks_performance.h"
#include "supercpn/weather_routing/Rounding.h"
#include "synthetic.h"
#include "user_request.h"

namespace py = pybind11;
namespace wr = supercpn::weather_routing;
using sailworks::native_routing::SyntheticRun;
using sailworks::native_routing::kKnotsPerMetreSecond;

namespace {

std::string roundingSideName(wr::RoundingSide side) {
  return side == wr::RoundingSide::Port ? "port" : "starboard";
}

std::string warningName(wr::RoutingWarningCode code) {
  switch (code) {
    case wr::RoutingWarningCode::ClimatologyWindUsed:
      return "climatology_wind_used";
    case wr::RoutingWarningCode::WindCoverageIncomplete:
      return "wind_coverage_incomplete";
    case wr::RoutingWarningCode::XtdCurrentUsed: return "xtd_current_used";
    case wr::RoutingWarningCode::CurrentAssumedZero:
      return "current_assumed_zero";
    case wr::RoutingWarningCode::CurrentCoverageIncomplete:
      return "current_coverage_incomplete";
    case wr::RoutingWarningCode::WaveDataMissing: return "wave_data_missing";
    case wr::RoutingWarningCode::WaveHeightEstimated:
      return "wave_height_estimated";
    case wr::RoutingWarningCode::WaveCoverageIncomplete:
      return "wave_coverage_incomplete";
    case wr::RoutingWarningCode::EstimatedPolar: return "estimated_polar";
    case wr::RoutingWarningCode::SearchPruned: return "search_pruned";
    case wr::RoutingWarningCode::SearchIncomplete: return "search_incomplete";
    case wr::RoutingWarningCode::EnsembleMemberFailed:
      return "ensemble_member_failed";
    case wr::RoutingWarningCode::CoastalEndpointLeeway:
      return "coastal_endpoint_leeway";
  }
  return "unknown";
}

std::string progressStageName(wr::RoutingProgressStage stage) {
  switch (stage) {
    case wr::RoutingProgressStage::Preflight: return "preflight";
    case wr::RoutingProgressStage::CurrentCoverage: return "current_coverage";
    case wr::RoutingProgressStage::ForwardIsochrone:
      return "forward_isochrone";
    case wr::RoutingProgressStage::ReverseRecovery:
      return "reverse_recovery";
    case wr::RoutingProgressStage::FrontierRecovery:
      return "frontier_recovery";
    case wr::RoutingProgressStage::GraphFallback: return "graph_fallback";
    case wr::RoutingProgressStage::Validation: return "validation";
    case wr::RoutingProgressStage::Complete: return "complete";
  }
  return "unknown";
}

// The progress checkpoint is the one place the engine cooperatively checks a
// cancellation token. Forwarding an update stays an observation, except that a
// set cancel event deliberately trips the token so the engine stops the search
// instead of the caller killing the worker process.
wr::RoutingProgressCallback makeProgressCallback(
    const py::object& callback,
    wr::CancellationToken token = wr::CancellationToken{},
    const py::object& cancelEvent = py::none()) {
  if (callback.is_none() && cancelEvent.is_none()) return {};
  return [callback, token, cancelEvent](const wr::RoutingProgressUpdate& update) {
    if (!callback.is_none()) {
      try {
        py::dict payload;
        payload["stage"] = progressStageName(update.stage);
        payload["attempt"] = update.attempt;
        payload["total_attempts"] = update.totalAttempts;
        payload["generated_states"] = update.generatedStates;
        payload["retained_states"] = update.retainedStates;
        payload["land_checks"] = update.landChecks;
        if (std::isfinite(update.closestApproachNm))
          payload["closest_approach_nm"] = update.closestApproachNm;
        else
          payload["closest_approach_nm"] = py::none();
        payload["effort_percent"] = update.effortPercent;
        callback(payload);
      } catch (...) {
        // A failed observer never changes the solve or leaves an error set.
        PyErr_Clear();
      }
    }
    if (!cancelEvent.is_none()) {
      try {
        if (py::cast<bool>(cancelEvent.attr("is_set")())) token.cancel();
      } catch (...) {
        // A cancellation probe never changes the solve or leaves an error set.
        PyErr_Clear();
      }
    }
  };
}

std::string tackName(wr::Tack tack) {
  switch (tack) {
    case wr::Tack::Port: return "port";
    case wr::Tack::Starboard: return "starboard";
    case wr::Tack::Unknown: return "unknown";
  }
  return "unknown";
}

// Warnings are always a JSON array, for both route-level and per-leg output.
// Each entry carries the engine's full structured warning: {"code", "message"}.
py::list warningsList(const std::vector<wr::RoutingWarning>& warnings) {
  py::list result;
  for (const auto& warning : warnings) {
    py::dict item;
    item["code"] = warningName(warning.code);
    item["message"] = warning.message;
    result.append(item);
  }
  return result;
}

std::int64_t seconds(wr::Duration duration) {
  return static_cast<std::int64_t>(duration.count());
}

std::int64_t timestamp(wr::TimePoint time) {
  return static_cast<std::int64_t>(time.time_since_epoch().count());
}

py::dict geoPoint(wr::GeoPoint point) {
  py::dict result;
  result["latitude"] = point.latitude;
  result["longitude"] = point.longitude;
  return result;
}

py::dict sourceMetadata(const wr::EnvironmentalSourceMetadata& metadata) {
  py::dict result;
  result["source"] = wr::toString(metadata.source);
  result["dataset_identity"] = metadata.datasetIdentity;
  result["model_identity"] = metadata.modelIdentity;
  if (metadata.modelRunTime) {
    result["model_run_time"] = timestamp(*metadata.modelRunTime);
  } else {
    result["model_run_time"] = py::none();
  }
  if (metadata.sourceTimestamp) {
    result["source_timestamp"] = timestamp(*metadata.sourceTimestamp);
  } else {
    result["source_timestamp"] = py::none();
  }
  if (metadata.spatialResolutionDegrees) {
    result["spatial_resolution_degrees"] =
        *metadata.spatialResolutionDegrees;
  } else {
    result["spatial_resolution_degrees"] = py::none();
  }
  result["fallback_reason"] = metadata.fallbackReason;
  return result;
}

py::dict coverageDict(const wr::ParameterCoverage& coverage) {
  py::dict result;
  result["available"] = coverage.available;
  result["dataset_identity"] = coverage.datasetIdentity;
  result["begins"] = coverage.begins ? py::cast(timestamp(*coverage.begins))
                                     : py::none();
  result["ends"] =
      coverage.ends ? py::cast(timestamp(*coverage.ends)) : py::none();
  py::dict area;
  area["south"] = coverage.area.south;
  area["west"] = coverage.area.west;
  area["north"] = coverage.area.north;
  area["east"] = coverage.area.east;
  result["area"] = area;
  return result;
}

std::string requiredActionName(wr::RequiredUserAction action) {
  switch (action) {
    case wr::RequiredUserAction::LoadSailingPolar: return "load_sailing_polar";
    case wr::RequiredUserAction::ConfigureVesselPerformance:
      return "configure_vessel_performance";
    case wr::RequiredUserAction::LoadWeatherGrib: return "load_weather_grib";
    case wr::RequiredUserAction::GenerateWeatherGrib:
      return "generate_weather_grib";
    case wr::RequiredUserAction::ConfirmClimatologyWindFallback:
      return "confirm_climatology_wind_fallback";
    case wr::RequiredUserAction::LoadCurrentPredictionDataset:
      return "load_current_prediction_dataset";
    case wr::RequiredUserAction::ConfirmRoutingWithoutCurrent:
      return "confirm_routing_without_current";
    case wr::RequiredUserAction::ConfirmRoutingWithoutWaveData:
      return "confirm_routing_without_wave_data";
    case wr::RequiredUserAction::LoadDepthData: return "load_depth_data";
  }
  return "unknown";
}

py::dict validationDict(const wr::RouteValidationResult& validation) {
  py::dict result;
  result["passed"] = validation.passed;
  result["failure_reason"] = validation.failureReason;
  result["accepted_prefix_legs"] = validation.acceptedPrefixLegs;
  result["coastal_departure_egress_legs"] =
      validation.coastalDepartureEgressLegs;
  result["samples"] = validation.samples;
  result["estimated_polar_wind"] = validation.estimatedPolarWind;
  return result;
}

py::dict legDict(const wr::RouteLeg& leg) {
  py::dict result;
  result["start"] = geoPoint(leg.start);
  result["end"] = geoPoint(leg.end);
  result["start_time"] = timestamp(leg.startTime);
  result["end_time"] = timestamp(leg.endTime);
  result["heading_degrees"] = leg.headingDegrees;
  result["course_through_water_degrees"] = leg.courseThroughWaterDegrees;
  result["course_over_ground_degrees"] = leg.courseOverGroundDegrees;
  result["speed_through_water_knots"] = leg.speedThroughWaterKnots;
  result["speed_over_ground_knots"] = leg.speedOverGroundKnots;
  result["true_wind_speed_knots"] = leg.trueWindSpeedKnots;
  result["true_wind_angle_degrees"] = leg.trueWindAngleDegrees;
  // A leg always carries a vector; availability distinguishes a real zero
  // sample from absent data whose components default to zero.
  result["wind_available"] =
      leg.windSource.source != wr::EnvironmentalSource::Missing;
  result["wind_east_knots"] = leg.wind.eastKnots;
  result["wind_north_knots"] = leg.wind.northKnots;
  result["wind_source"] = sourceMetadata(leg.windSource);
  result["current_available"] =
      leg.currentSource.source != wr::EnvironmentalSource::Missing;
  result["current_east_knots"] = leg.current.eastKnots;
  result["current_north_knots"] = leg.current.northKnots;
  result["current_source"] = sourceMetadata(leg.currentSource);
  result["waves_available"] = leg.waves.available;
  result["wave_source"] = sourceMetadata(leg.waveSource);
  if (leg.waves.available) {
    result["wave_height_metres"] = leg.waves.significantHeightMetres;
    result["wave_period_seconds"] = leg.waves.periodSeconds;
    result["wave_direction_from_degrees"] = leg.waves.directionFromDegrees;
  } else {
    result["wave_height_metres"] = py::none();
    result["wave_period_seconds"] = py::none();
    result["wave_direction_from_degrees"] = py::none();
  }
  result["propulsion_mode"] = wr::toString(leg.propulsionMode);
  result["profile_role"] = wr::toString(leg.profileRole);
  result["profile_identity"] = leg.profileIdentity;
  result["sail_plan"] = leg.sailPlan;
  result["tack"] = tackName(leg.tack);
  result["tack_transition"] = leg.tackTransition;
  result["gybe_transition"] = leg.gybeTransition;
  result["propulsion_transition"] = leg.propulsionTransition;
  result["stationary_wait"] = leg.stationaryWait;
  result["coastal_departure_egress"] = leg.coastalDepartureEgress;
  result["integration_maximum_slice_seconds"] =
      seconds(leg.integrationMaximumSlice);
  result["estimated_fuel_litres"] = leg.estimatedFuelLitres;
  py::dict margins;
  margins["wind_knots"] = leg.margins.windKnots;
  margins["wave_metres"] = leg.margins.waveMetres;
  margins["fuel_litres"] = leg.margins.fuelLitres;
  margins["motor_seconds"] = leg.margins.motorSeconds;
  result["safety_margins"] = margins;
  result["warnings"] = warningsList(leg.warnings);
  if (leg.windGustKnots) {
    result["wind_gust_knots"] = *leg.windGustKnots;
  } else {
    result["wind_gust_knots"] = py::none();
  }
  return result;
}

py::dict metricsDict(const wr::RouteMetrics& metrics) {
  py::dict result;
  result["elapsed_seconds"] = seconds(metrics.elapsed);
  result["sailing_seconds"] = seconds(metrics.sailingTime);
  result["motor_sailing_seconds"] = seconds(metrics.motorSailingTime);
  result["motor_only_seconds"] = seconds(metrics.motorOnlyTime);
  result["waiting_seconds"] = seconds(metrics.waitingTime);
  result["distance_nm"] = metrics.distanceNm;
  result["estimated_fuel_litres"] = metrics.estimatedFuelLitres;
  result["propulsion_transitions"] = metrics.propulsionTransitions;
  result["tack_count"] = metrics.tackCount;
  result["gybe_count"] = metrics.gybeCount;
  result["maximum_wind_knots"] = metrics.maximumWindKnots;
  result["maximum_wave_height_metres"] = metrics.maximumWaveHeightMetres;
  return result;
}

py::dict environmentUsageDict(const wr::EnvironmentalSourceUsage& usage) {
  py::dict result;
  result["grib_wind_seconds"] = seconds(usage.gribWindDuration);
  result["climatology_wind_seconds"] = seconds(usage.climatologyWindDuration);
  result["grib_current_seconds"] = seconds(usage.gribCurrentDuration);
  result["xtd_current_seconds"] = seconds(usage.xtdCurrentDuration);
  result["current_assumed_zero_seconds"] =
      seconds(usage.currentAssumedZeroDuration);
  result["grib_wave_seconds"] = seconds(usage.gribWaveDuration);
  result["missing_wave_seconds"] = seconds(usage.missingWaveDuration);
  result["estimated_wave_seconds"] = seconds(usage.estimatedWaveDuration);
  return result;
}

py::dict diagnosticsDict(const wr::RoutingDiagnostics& diagnostics) {
  py::dict result;
  result["generated_states"] = diagnostics.generatedStates;
  result["coastal_endpoint_generated_states"] =
      diagnostics.coastalEndpointGeneratedStates;
  result["forward_generated_states"] = diagnostics.forwardGeneratedStates;
  result["frontier_recovery_generated_states"] =
      diagnostics.frontierRecoveryGeneratedStates;
  result["graph_generated_states"] = diagnostics.graphGeneratedStates;
  result["retained_states"] = diagnostics.retainedStates;
  result["weather_samples"] = diagnostics.weatherSamples;
  result["land_checks"] = diagnostics.landChecks;
  result["land_rejections"] = diagnostics.landRejections;
  result["validation_samples"] = diagnostics.validationSamples;
  result["wait_states"] = diagnostics.waitStates;
  result["completed_effort_percent"] = diagnostics.completedEffortPercent;
  result["cumulative_generated_states"] =
      diagnostics.cumulativeGeneratedStates;
  py::list efforts;
  for (const unsigned effort : diagnostics.effortTiersAttempted)
    efforts.append(effort);
  result["effort_tiers_attempted"] = efforts;
  py::list reasons;
  for (const auto& reason : diagnostics.stageStopReasons)
    reasons.append(reason);
  result["stage_stop_reasons"] = reasons;
  if (diagnostics.roundingAttempted) {
    const bool measured =
        std::isfinite(diagnostics.roundingClosestDistanceNm);
    result["rounding_attempted"] = true;
    result["rounding_failure_code"] = diagnostics.roundingFailureCode;
    if (measured) {
      result["rounding_closest_distance_nm"] =
          diagnostics.roundingClosestDistanceNm;
      result["rounding_terminal_bearing_deg"] =
          diagnostics.roundingTerminalBearingDegrees;
      result["rounding_terminal_course_deg"] =
          diagnostics.roundingTerminalCourseDegrees;
    } else {
      result["rounding_closest_distance_nm"] = py::none();
      result["rounding_terminal_bearing_deg"] = py::none();
      result["rounding_terminal_course_deg"] = py::none();
    }
  }
  return result;
}

py::dict contourDict(const wr::IsochroneContour& contour) {
  py::dict result;
  py::list points;
  for (const auto& point : contour.points) points.append(geoPoint(point));
  result["points"] = points;
  result["point_count"] = contour.points.size();
  // Engine classification only: an open contour is never closed here.
  result["closed"] = contour.closed;
  return result;
}

py::dict traceDict(const wr::IsochroneTrace& trace) {
  py::dict result;
  result["endpoint"] = geoPoint(trace.endpoint);
  py::list route;
  for (const auto& point : trace.route) route.append(geoPoint(point));
  result["route"] = route;
  result["point_count"] = trace.route.size();
  if (trace.times.empty()) {
    // The producer supplied no predecessor instants. Unknown is not zero and
    // must never be replaced by a synthesized constant step.
    result["times"] = py::none();
  } else {
    py::list times;
    for (const auto& time : trace.times) times.append(timestamp(time));
    result["times"] = times;
  }
  return result;
}

py::dict layerDict(const wr::IsochroneLayer& layer, std::size_t index) {
  py::dict result;
  result["index"] = index;
  result["id"] = "isochrone-layer-" + std::to_string(index);
  // Exact engine instant as UTC epoch seconds.
  result["time"] = timestamp(layer.time);
  result["reverse"] = layer.reverse;
  py::list contours;
  for (const auto& contour : layer.contours)
    contours.append(contourDict(contour));
  result["contours"] = contours;
  py::list traces;
  for (const auto& trace : layer.traces) traces.append(traceDict(trace));
  result["traces"] = traces;
  return result;
}

// Selected GSHHS shoreline supplied by Python so the native search and its
// independent replay use exactly the reviewed Sailworks predicates. The route
// call keeps the GIL; no native concurrency is enabled by this adapter.
class PythonLandProvider final : public wr::LandAndBoundaryProvider {
 public:
  PythonLandProvider(py::object land, std::string identity)
      : land_(std::move(land)), identity_(std::move(identity)) {}

  [[nodiscard]] bool pointForbidden(wr::GeoPoint point) const override {
    py::gil_scoped_acquire guard;
    return py::cast<bool>(
        land_.attr("point_forbidden")(point.latitude, point.longitude));
  }

  [[nodiscard]] bool segmentForbidden(wr::GeoPoint start, wr::GeoPoint end,
                                      double safetyMarginNm) const override {
    py::gil_scoped_acquire guard;
    return py::cast<bool>(land_.attr("segment_forbidden")(
        start.latitude, start.longitude, end.latitude, end.longitude,
        safetyMarginNm));
  }

  [[nodiscard]] double distanceToForbiddenNm(wr::GeoPoint point) const override {
    py::gil_scoped_acquire guard;
    return py::cast<double>(
        land_.attr("distance_nm")(point.latitude, point.longitude));
  }

  // User avoid zones are native geometry: the polygon provider is composed
  // with this shoreline adapter by the bridge and never calls back into Python
  // per leg. See ComposedLandProvider in boundary_provider.h.

  [[nodiscard]] std::string identity() const override { return identity_; }

 private:
  py::object land_;
  std::string identity_;
};

// Weather supplied by Python callables so ordinary Sailworks sampling can drive
// the native solver. The route call keeps the GIL, so callbacks execute on the
// calling thread; no native concurrency is enabled by this adapter.
class PythonWeatherProvider final : public wr::WeatherProvider {
 public:
  PythonWeatherProvider(py::function wind, py::function current,
                        py::object waves, wr::ParameterCoverage waveCoverage,
                        wr::ParameterCoverage coverage, std::string identity,
                        wr::EnvironmentalSource source)
      : wind_(std::move(wind)),
        current_(std::move(current)),
        waves_(std::move(waves)),
        waveCoverage_(std::move(waveCoverage)),
        coverage_(std::move(coverage)),
        identity_(std::move(identity)),
        source_(source) {}

  [[nodiscard]] wr::ParameterCoverage windCoverage() const override {
    return coverage_;
  }
  [[nodiscard]] wr::ParameterCoverage currentCoverage() const override {
    return coverage_;
  }
  [[nodiscard]] wr::ParameterCoverage waveCoverage() const override {
    return waveCoverage_;
  }

  [[nodiscard]] wr::WindSample wind(wr::GeoPoint position,
                                    wr::TimePoint time) const override {
    py::gil_scoped_acquire guard;
    const py::object value =
        wind_(position.latitude, position.longitude, seconds(time));
    if (value.is_none()) return {};
    const py::tuple item = asTuple(value, "wind");
    wr::WindSample sample;
    // The callback reports established sampling semantics: wind FROM degrees
    // and speed in metres per second. The engine stores a toward-vector in
    // knots, so the conversion happens here once.
    const double fromDegrees = asDouble(item[0], "wind from_degrees");
    const double speedMs = asDouble(item[1], "wind speed_ms");
    const bool available = asBool(item[2], "wind available");
    sample.available = available;
    if (available) {
      const double radians = fromDegrees * 3.14159265358979323846 / 180.0;
      sample.velocity = wr::Vector2{
          -speedMs * std::sin(radians) * kKnotsPerMetreSecond,
          -speedMs * std::cos(radians) * kKnotsPerMetreSecond};
    }
    sample.metadata = metadata(time);
    return sample;
  }

  [[nodiscard]] wr::CurrentSample current(wr::GeoPoint position,
                                          wr::TimePoint time) const override {
    py::gil_scoped_acquire guard;
    const py::object value =
        current_(position.latitude, position.longitude, seconds(time));
    if (value.is_none()) return {};
    const py::tuple item = asTuple(value, "current");
    wr::CurrentSample sample;
    // Currents keep the Sailworks component convention: east/north metres per
    // second pointing toward where the water moves.
    const double eastMs = asDouble(item[0], "current east_ms");
    const double northMs = asDouble(item[1], "current north_ms");
    const bool available = asBool(item[2], "current available");
    sample.available = available;
    if (available) {
      sample.velocity = wr::Vector2{eastMs * kKnotsPerMetreSecond,
                                    northMs * kKnotsPerMetreSecond};
    }
    sample.metadata = metadata(time);
    if (available && py::len(item) == 4) {
      const auto dataset = py::cast<std::string>(item[3]);
      if (dataset.empty())
        throw py::value_error("selected current dataset identity is empty");
      sample.metadata.datasetIdentity = dataset;
      sample.metadata.modelIdentity = dataset;
    }
    return sample;
  }

  [[nodiscard]] wr::WaveSample waves(wr::GeoPoint position,
                                     wr::TimePoint time) const override {
    if (waves_.is_none()) return {};
    py::gil_scoped_acquire guard;
    const py::object value =
        waves_(position.latitude, position.longitude, seconds(time));
    if (value.is_none()) return {};
    const py::tuple item = asTuple(value, "waves");
    if (py::len(item) < 4)
      throw py::value_error(
          "wave provider must return (height_m, period_s, "
          "direction_from_deg, available[, dataset[, model]])");
    wr::WaveSample sample;
    const bool available = asBool(item[3], "wave available");
    sample.available = available;
    if (available) {
      const double height = asDouble(item[0], "wave height_m");
      const double period = asDouble(item[1], "wave period_s");
      const double direction = asDouble(item[2], "wave direction_from_deg");
      if (!std::isfinite(height) || height < 0.0 ||
          !std::isfinite(period) || period <= 0.0 ||
          !std::isfinite(direction))
        throw py::value_error(
            "available wave sample needs a non-negative height, a positive "
            "period and a finite direction");
      sample.significantHeightMetres = height;
      sample.periodSeconds = period;
      double normalized = std::fmod(direction, 360.0);
      if (normalized < 0.0) normalized += 360.0;
      sample.directionFromDegrees = normalized;
    }
    sample.metadata = metadata(time);
    if (available && py::len(item) >= 5) {
      const auto dataset = py::cast<std::string>(item[4]);
      if (dataset.empty())
        throw py::value_error("selected wave dataset identity is empty");
      sample.metadata.datasetIdentity = dataset;
      if (py::len(item) >= 6) {
        const auto model = py::cast<std::string>(item[5]);
        if (!model.empty()) sample.metadata.modelIdentity = model;
      } else {
        sample.metadata.modelIdentity = dataset;
      }
    }
    return sample;
  }

  [[nodiscard]] std::string identity() const override { return identity_; }

 private:
  static std::int64_t seconds(wr::TimePoint time) {
    return static_cast<std::int64_t>(time.time_since_epoch().count());
  }

  static py::tuple asTuple(const py::object& value, const char* label) {
    if (!py::isinstance<py::tuple>(value))
      throw std::runtime_error(std::string(label) +
                               " provider must return a tuple");
    return py::reinterpret_borrow<py::tuple>(value);
  }

  static double asDouble(const py::handle& value, const char* label) {
    if (!py::isinstance<py::float_>(value) && !py::isinstance<py::int_>(value))
      throw std::runtime_error(std::string(label) + " must be a number");
    return py::cast<double>(value);
  }

  static bool asBool(const py::handle& value, const char* label) {
    if (!py::isinstance<py::bool_>(value))
      throw std::runtime_error(std::string(label) + " must be a boolean");
    return py::cast<bool>(value);
  }

  [[nodiscard]] wr::EnvironmentalSourceMetadata metadata(
      wr::TimePoint time) const {
    wr::EnvironmentalSourceMetadata value;
    value.source = source_;
    value.datasetIdentity = identity_;
    value.modelIdentity = identity_;
    value.sourceTimestamp = time;
    return value;
  }

  py::function wind_;
  py::function current_;
  py::object waves_;
  wr::ParameterCoverage waveCoverage_;
  wr::ParameterCoverage coverage_;
  std::string identity_;
  wr::EnvironmentalSource source_;
};

wr::ParameterCoverage coverageFromDict(const py::dict& value,
                                       const std::string& identity) {
  wr::ParameterCoverage coverage;
  coverage.available = true;
  coverage.datasetIdentity = identity;
  if (value.contains("available")) coverage.available = py::cast<bool>(value["available"]);
  if (value.contains("dataset_identity"))
    coverage.datasetIdentity = py::cast<std::string>(value["dataset_identity"]);
  coverage.area.south = py::cast<double>(value["south"]);
  coverage.area.north = py::cast<double>(value["north"]);
  coverage.area.west = py::cast<double>(value["west"]);
  coverage.area.east = py::cast<double>(value["east"]);
  coverage.begins = wr::TimePoint{wr::Duration{py::cast<std::int64_t>(value["start_epoch_s"])}};
  coverage.ends = wr::TimePoint{wr::Duration{py::cast<std::int64_t>(value["end_epoch_s"])}};
  return coverage;
}

wr::VesselConfiguration vesselFromPolar(const py::object& polar,
                                        const std::string& identity,
                                        const std::string& assumptions) {
  wr::VesselConfiguration vessel;
  wr::PerformanceProfile profile;
  profile.role = wr::ProfileRole::SailOnly;
  profile.identity = identity;
  profile.assumptions = assumptions;
  double previousTws = -1.0;
  for (const py::handle rowValue : py::cast<py::sequence>(polar)) {
    const py::dict row = py::cast<py::dict>(rowValue);
    const double tws = py::cast<double>(row["true_wind_speed_knots"]);
    if (!std::isfinite(tws) || tws <= previousTws)
      throw std::runtime_error(
          "polar true wind speeds must be finite and strictly increasing");
    previousTws = tws;
    wr::PolarRow polarRow;
    polarRow.trueWindSpeedKnots = tws;
    double previousTwa = -1.0;
    for (const py::handle pointValue : py::cast<py::sequence>(row["points"])) {
      const py::sequence point = py::cast<py::sequence>(pointValue);
      if (py::len(point) != 2)
        throw std::runtime_error("polar points must be (twa_degrees, speed_kn)");
      const double twa = py::cast<double>(point[0]);
      const double speed = py::cast<double>(point[1]);
      if (!std::isfinite(twa) || twa < 0.0 || twa > 180.0 || twa <= previousTwa)
        throw std::runtime_error(
            "polar true wind angles must be strictly increasing within 0-180");
      if (!std::isfinite(speed) || speed < 0.0)
        throw std::runtime_error("polar boat speeds must be finite and >= 0");
      previousTwa = twa;
      polarRow.points.push_back(wr::PolarPoint{twa, speed});
    }
    if (polarRow.points.size() < 2)
      throw std::runtime_error("each polar row needs at least two angles");
    profile.rows.push_back(std::move(polarRow));
  }
  if (profile.rows.empty())
    throw std::runtime_error("polar needs at least one true wind speed row");
  vessel.profiles = {std::move(profile)};
  vessel.propulsion.allowSailing = true;
  vessel.propulsion.allowMotorSailing = false;
  vessel.propulsion.allowMotor = false;
  return vessel;
}

wr::EnvironmentalSource sourceFromName(const std::string& name) {
  if (name == "grib" || name == "grib_forecast")
    return wr::EnvironmentalSource::GribForecast;
  if (name == "climatology") return wr::EnvironmentalSource::Climatology;
  if (name == "current_prediction" || name == "xtd_current")
    return wr::EnvironmentalSource::XtdCurrentPrediction;
  if (name == "synthetic") return wr::EnvironmentalSource::SyntheticTestField;
  return wr::EnvironmentalSource::Missing;
}

py::dict runDict(const SyntheticRun& run,
                 const std::vector<sailworks::native_routing::ChainSegment>*
                     chainSegments = nullptr) {
  py::dict result;
  result["engine"] = run.engine;
  result["status"] = sailworks::native_routing::statusName(run.result.status);
  result["status_code"] = static_cast<int>(run.result.status);
  result["accepted"] = run.accepted;
  py::dict orchestration;
  orchestration["incumbent_attempted"] = run.incumbentAttempted;
  orchestration["incumbent_status"] = run.incumbentAttempted
                                           ? py::cast(sailworks::native_routing::statusName(run.incumbentStatus))
                                           : py::none();
  orchestration["incumbent_independently_validated"] = run.incumbentValidated;
  orchestration["incumbent_forwarded"] = run.incumbentForwarded;
  result["orchestration"] = orchestration;
  result["solver_path"] =
      sailworks::native_routing::solverPathName(run.result.solverPath);
  result["message"] = run.result.message;
  result["engine_identity"] = run.result.engineIdentity;
  result["digest"] = sailworks::native_routing::routeDigest(run.result);
  py::list legs;
  for (const auto& leg : run.result.legs) legs.append(legDict(leg));
  result["legs"] = legs;
  result["leg_count"] = run.result.legs.size();
  result["metrics"] = metricsDict(run.result.metrics);
  result["validation"] = validationDict(run.result.validation);
  result["independent_validation"] =
      validationDict(run.independentValidation);
  // Deliberately limited to the pass flags: the full validation dictionaries
  // are exposed separately and may legitimately differ in sample counts or
  // reason text between an engine replay path and the independent replay.
  result["validation_pass_flags_equal"] =
      run.result.validation.passed == run.independentValidation.passed;
  result["environment_usage"] = environmentUsageDict(run.result.environment);
  result["diagnostics"] = diagnosticsDict(run.result.diagnostics);
  result["warnings"] = warningsList(run.result.warnings);
  py::list layers;
  bool hasTraceTimes = false;
  for (std::size_t index = 0;
       index < run.result.visualization.isochrones.size(); ++index) {
    const auto& layer = run.result.visualization.isochrones[index];
    layers.append(layerDict(layer, index));
    for (const auto& trace : layer.traces)
      hasTraceTimes = hasTraceTimes || !trace.times.empty();
  }
  result["layers"] = layers;
  result["layer_count"] = run.result.visualization.isochrones.size();
  py::dict inspection;
  inspection["producer"] = run.engine;
  inspection["source"] = "engine-visualization";
  inspection["engine_ordered_contours"] = true;
  inspection["joins_inferred"] = false;
  inspection["has_trace_times"] = hasTraceTimes;
  inspection["trace_times_semantics"] =
      "null means the producer supplied no predecessor instants; unknown is "
      "not zero and no timestamps are synthesized";
  inspection["note"] =
      "bounded engine inspection geometry: contours and traces may be "
      "sampled, and route acceptance never depends on them";
  result["inspection_geometry"] = inspection;
  py::dict requestInfo;
  requestInfo["start"] = geoPoint(run.request.start);
  requestInfo["destination"] = geoPoint(run.request.destination);
  requestInfo["departure"] = timestamp(run.request.departure);
  requestInfo["maximum_route_duration_seconds"] =
      seconds(run.request.limits.maximumRouteDuration);
  requestInfo["time_step_seconds"] = seconds(run.request.options.timeStep);
  // Native settings are engine-defined for now; expose them so a result can
  // never imply that an unexposed legacy parameter was applied.
  requestInfo["heading_step_degrees"] = run.request.options.headingStepDegrees;
  requestInfo["maximum_search_angle_degrees"] =
      run.request.options.maximumSearchAngleDegrees;
  requestInfo["destination_tolerance_nm"] =
      run.request.options.destinationToleranceNm;
  requestInfo["land_safety_margin_nm"] =
      run.request.constraints.landSafetyMarginNm;
  requestInfo["maximum_generated_states"] =
      run.request.limits.maximumGeneratedStates;
  requestInfo["maximum_retained_states"] =
      run.request.limits.maximumRetainedStates;
  requestInfo["maximum_exploration_distance_nm"] =
      run.request.limits.maximumExplorationDistanceNm;
  result["request"] = requestInfo;
  if (chainSegments != nullptr) {
    py::list segments;
    for (const auto& segment : *chainSegments) {
      py::dict item;
      item["index"] = static_cast<std::size_t>(segment.index);
      item["status"] =
          sailworks::native_routing::statusName(segment.status);
      item["accepted"] = segment.accepted;
      item["engine_validation_passed"] = segment.engineValidationPassed;
      item["independent_validation_passed"] =
          segment.independentValidationPassed;
      item["leg_count"] = segment.legCount;
      item["message"] = segment.message;
      segments.append(item);
    }
    result["segments"] = segments;
    result["metrics_note"] =
        "chain metrics aggregate delivered legs with the engine's own "
        "delivered-route rules; a join maneuver is counted in the segment "
        "that delivered it";
  }
  py::list roundings;
  for (const auto& report : run.roundingReports) {
    if (!report.rounding) continue;
    py::dict item;
    if (report.mark) {
      item["mark"] = geoPoint(*report.mark);
    } else {
      item["mark"] = py::none();
    }
    item["side"] = roundingSideName(report.rounding->side);
    item["radius_nm"] = report.rounding->radiusNm;
    if (std::isfinite(report.measurement.closestDistanceNm)) {
      item["achieved_closest_distance_nm"] =
          report.measurement.closestDistanceNm;
    } else {
      item["achieved_closest_distance_nm"] = py::none();
    }
    if (std::isfinite(report.measurement.terminalBearingToMarkDegrees)) {
      item["achieved_terminal_bearing_deg"] =
          report.measurement.terminalBearingToMarkDegrees;
    } else {
      item["achieved_terminal_bearing_deg"] = py::none();
    }
    if (std::isfinite(report.measurement.terminalCourseDegrees)) {
      item["achieved_terminal_course_deg"] =
          report.measurement.terminalCourseDegrees;
    } else {
      item["achieved_terminal_course_deg"] = py::none();
    }
    if (report.measurement.hasApproach) {
      item["closest_approach_course_deg"] =
          report.measurement.closestApproachCourseDegrees;
      item["closest_approach_bearing_deg"] =
          report.measurement.closestApproachBearingToMarkDegrees;
    } else {
      item["closest_approach_course_deg"] = py::none();
      item["closest_approach_bearing_deg"] = py::none();
    }
    if (report.measurement.achievedSide) {
      item["achieved_side"] = roundingSideName(*report.measurement.achievedSide);
    } else {
      item["achieved_side"] = py::none();
    }
    item["passage_count"] = report.measurement.passageCount;
    item["satisfied"] = report.measurement.satisfied;
    item["failure_code"] = report.measurement.failureCode();
    item["failure_reason"] = report.measurement.failureReason();
    roundings.append(item);
  }
  if (py::len(roundings) > 0) result["roundings"] = roundings;
  return result;
}

py::dict waveProvenanceDict(const wr::RoutingResult& result, bool useWaves,
                            bool windProxy) {
  py::dict provenance;
  if (useWaves && windProxy) {
    provenance["mode"] = "real_waves_and_wind_proxy";
  } else if (useWaves) {
    provenance["mode"] = "real_waves";
  } else if (windProxy) {
    provenance["mode"] = "wind_proxy";
  } else {
    provenance["mode"] = "none";
  }
  provenance["uses_real_wave_heights"] = useWaves;
  provenance["uses_wind_proxy"] = windProxy;
  py::dict units;
  units["height"] = "m";
  units["period"] = "s";
  units["direction"] = "degrees coming from true north";
  provenance["units"] = units;
  provenance["direction_convention"] = "coming from";

  const wr::EnvironmentalSourceMetadata* selected = nullptr;
  std::set<std::string> datasets;
  std::set<std::string> models;
  for (const auto& leg : result.legs) {
    if (!leg.waves.available) continue;
    if (selected == nullptr) selected = &leg.waveSource;
    if (!leg.waveSource.datasetIdentity.empty())
      datasets.insert(leg.waveSource.datasetIdentity);
    if (!leg.waveSource.modelIdentity.empty())
      models.insert(leg.waveSource.modelIdentity);
  }
  if (selected != nullptr) {
    provenance["dataset_identity"] = selected->datasetIdentity;
    provenance["model_identity"] = selected->modelIdentity;
  } else {
    provenance["dataset_identity"] = py::none();
    provenance["model_identity"] = py::none();
  }
  py::list identities;
  for (const auto& value : datasets) identities.append(value);
  provenance["dataset_identities"] = identities;
  py::list modelIdentities;
  for (const auto& value : models) modelIdentities.append(value);
  provenance["model_identities"] = modelIdentities;

  if (result.preflight) {
    py::dict preflight;
    preflight["can_route"] = result.preflight->canRoute;
    py::list actions;
    for (const auto action : result.preflight->requiredActions)
      actions.append(requiredActionName(action));
    preflight["required_actions"] = actions;
    py::dict coverage;
    coverage["wind"] = coverageDict(result.preflight->coverage.wind);
    coverage["current"] = coverageDict(result.preflight->coverage.current);
    coverage["waves"] = coverageDict(result.preflight->coverage.waves);
    preflight["coverage"] = coverage;
    provenance["coverage"] = coverage["waves"];
    provenance["preflight"] = preflight;
  } else {
    provenance["coverage"] = py::none();
  }
  return provenance;
}

// Provenance for a zone-constrained calculation. Emitted only when a zone set
// is frozen in, so a no-zone result payload stays byte-identical to the base
// revision. "active" means the native polygon provider was applied, whether or
// not a leg was rerouted; it is never a claim that a zone was avoided.
void addAvoidZoneProvenance(
    py::dict& result,
    const std::vector<sailworks::native_routing::AvoidZone>& zones,
    const std::string& digest) {
  if (zones.empty()) return;
  py::list descriptors;
  for (const auto& zone : zones) {
    py::dict entry;
    entry["id"] = zone.id;
    entry["name"] = zone.name;
    entry["vertex_count"] = zone.vertices.size();
    descriptors.append(entry);
  }
  result["avoid_zones"] = descriptors;
  result["avoid_zones_digest"] = digest;
  result["avoid_zones_active"] = true;
}

// The digest in the settings is only the caller's identity claim. Recompute it
// from the payload the provider will consume, using the canonical Python
// encoder (the GIL is held here), and reject a digest that does not describe
// that geometry. A malformed digest is left to buildAvoidZoneProvider, which
// owns its fixed 64-lowercase-hex error message.
void verifyAvoidZoneDigest(const py::object& payload,
                           const std::string& digest) {
  if (digest.size() != 64) return;
  for (const char digit : digest)
    if (!((digit >= '0' && digit <= '9') || (digit >= 'a' && digit <= 'f')))
      return;
  const py::object encoded =
      py::module_::import("sailworks.core.geo.mapshapes")
          .attr("avoid_zones_digest_from_native")(payload);
  if (py::cast<std::string>(encoded) != digest)
    throw py::value_error(
        "zone digest mismatch: avoid_zones_digest does not describe the "
        "supplied avoid zone geometry");
}

py::dict runUserBridge(py::function wind, py::function current,
                           py::object waves, const py::object& waveCoverage,
                           const py::dict& coverage, const py::object& polar,
                           const py::dict& settings,
                           const py::object& land,
                           const std::string& providerIdentity,
                           const std::string& polarIdentity,
                           const std::string& engine,
                           const py::object& progress,
                           const py::object& cancelEvent) {
  const std::set<std::string> required{
      "start", "destination", "departure_epoch_s", "use_currents",
      "polar_efficiency", "tws_max_kn", "min_sailing_speed_kn",
      "motor_speed_kn", "wave_correct", "coastline_checks",
      "land_safety_margin_nm", "waypoints", "roundings"};
  const std::set<std::string> optional{"use_waves", "avoid_zones",
                                       "avoid_zones_digest"};
  if (py::len(settings) < required.size() ||
      py::len(settings) > required.size() + optional.size())
    throw py::value_error(
        "real request requires all supported settings and no unknown settings");
  for (const auto& key : required)
    if (!settings.contains(py::str(key)))
      throw py::value_error("real request missing setting: " + key);
  for (const auto& item : settings)
    if (!required.count(py::cast<std::string>(item.first)) &&
        !optional.count(py::cast<std::string>(item.first)))
      throw py::value_error("real request has an unknown setting");
  // User avoid zones are native polygons frozen into the request; they never
  // travel through the Python land adapter, so only shoreline checks need one.
  if (land.is_none() && py::cast<bool>(settings["coastline_checks"]))
    throw py::value_error(
        "real request shoreline checks require a worker-local shoreline "
        "adapter");
  auto point = [](const py::handle value) {
    const auto pair = py::cast<py::sequence>(value);
    if (py::len(pair) != 2)
      throw py::value_error("real request point must be (latitude, longitude)");
    return wr::GeoPoint{py::cast<double>(pair[0]), py::cast<double>(pair[1])};
  };
  auto optionalSpeed = [](const py::handle value) -> std::optional<double> {
    if (value.is_none()) return std::nullopt;
    return py::cast<double>(value);
  };
  // Frozen user no-go zones: an ordered list of
  // {id, name, vertices: [[latitude, longitude], ...]}. The rider order is part
  // of the frozen input. Geometry is validated and turned into native engine
  // polygons here; no Python object is consulted per route leg.
  std::vector<sailworks::native_routing::AvoidZone> avoidZones;
  py::list avoidZonePayload;
  if (settings.contains("avoid_zones") &&
      !settings["avoid_zones"].is_none()) {
    if (!py::isinstance<py::sequence>(settings["avoid_zones"]) ||
        py::isinstance<py::str>(settings["avoid_zones"]))
      throw py::value_error("avoid_zones must be a list of zone mappings");
    const std::set<std::string> zoneFields{"id", "name", "vertices"};
    for (const auto& value : py::cast<py::sequence>(settings["avoid_zones"])) {
      if (!py::isinstance<py::dict>(value))
        throw py::value_error("avoid_zones entries must be mappings");
      const auto item = py::reinterpret_borrow<py::dict>(value);
      for (const auto& entry : item)
        if (!zoneFields.count(py::cast<std::string>(entry.first)))
          throw py::value_error("avoid zone has an unknown field");
      for (const auto& field : zoneFields)
        if (!item.contains(py::str(field)))
          throw py::value_error("avoid zone missing field: " + field);
      sailworks::native_routing::AvoidZone zone;
      zone.id = py::cast<std::string>(item["id"]);
      zone.name = py::cast<std::string>(item["name"]);
      if (!py::isinstance<py::sequence>(item["vertices"]) ||
          py::isinstance<py::str>(item["vertices"]))
        throw py::value_error("avoid zone vertices must be a list of points");
      for (const auto& vertex : py::cast<py::sequence>(item["vertices"]))
        zone.vertices.push_back(point(vertex));
      // Keep the parsed record so the digest can be re-derived from exactly
      // what the provider receives.
      avoidZonePayload.append(item);
      avoidZones.push_back(std::move(zone));
    }
  }
  std::string avoidZonesDigest;
  if (settings.contains("avoid_zones_digest") &&
      !settings["avoid_zones_digest"].is_none())
    avoidZonesDigest =
        py::cast<std::string>(settings["avoid_zones_digest"]);
  if (!avoidZones.empty())
    verifyAvoidZoneDigest(avoidZonePayload, avoidZonesDigest);
  std::shared_ptr<const wr::PolygonBoundaryProvider> avoidZoneProvider;
  if (!avoidZones.empty()) {
    avoidZoneProvider =
        sailworks::native_routing::buildAvoidZoneProvider(avoidZones,
                                                          avoidZonesDigest);
  } else if (!avoidZonesDigest.empty()) {
    throw py::value_error(
        "avoid_zones_digest requires a non-empty avoid_zones list");
  }
  sailworks::native_routing::UserSettings input;
  input.start = point(settings["start"]);
  input.destination = point(settings["destination"]);
  input.departureEpochSeconds = py::cast<std::int64_t>(settings["departure_epoch_s"]);
  input.useCurrents = py::cast<bool>(settings["use_currents"]);
  input.polarEfficiency = py::cast<double>(settings["polar_efficiency"]);
  input.maximumWindKnots = optionalSpeed(settings["tws_max_kn"]);
  input.minimumSailingKnots = optionalSpeed(settings["min_sailing_speed_kn"]);
  input.motorKnots = py::cast<double>(settings["motor_speed_kn"]);
  input.windBasedWaveProxy = py::cast<bool>(settings["wave_correct"]);
  input.useWaves = settings.contains("use_waves")
                       ? py::cast<bool>(settings["use_waves"])
                       : false;
  input.coastlineChecks = py::cast<bool>(settings["coastline_checks"]);
  input.landSafetyMarginNm =
      py::cast<double>(settings["land_safety_margin_nm"]);
  for (const auto& value : py::cast<py::sequence>(settings["roundings"])) {
    if (value.is_none()) {
      input.roundings.push_back(std::nullopt);
      continue;
    }
    const auto item = py::reinterpret_borrow<py::dict>(value);
    wr::RoundingConstraint rounding;
    const std::string side = py::cast<std::string>(item["side"]);
    if (side == "port") {
      rounding.side = wr::RoundingSide::Port;
    } else if (side == "starboard") {
      rounding.side = wr::RoundingSide::Starboard;
    } else {
      throw py::value_error("rounding side must be port or starboard");
    }
    // Radius is legacy metadata, not part of the side-of-track contract.
    // Older payloads carried it; the Python boundary no longer sends it, so
    // keep the historical default for this additive stage.
    constexpr double kLegacyRoundingRadiusNm = 0.5;
    rounding.radiusNm = kLegacyRoundingRadiusNm;
    if (item.contains("radius_nm") && !item["radius_nm"].is_none())
      rounding.radiusNm = py::cast<double>(item["radius_nm"]);
    if (!wr::roundingConstraintValid(rounding))
      throw py::value_error("rounding side is invalid");
    input.roundings.push_back(rounding);
  }

  if (polar.is_none())
    throw py::value_error("real request requires selected polar rows");
  if (input.useWaves && (waves.is_none() || waveCoverage.is_none()))
    throw py::value_error(
        "wave routing requires a wave sampler and wave coverage");
  const auto windCoverage = coverageFromDict(coverage, providerIdentity);
  wr::ParameterCoverage waveFieldsCoverage;
  if (!waveCoverage.is_none())
    waveFieldsCoverage = coverageFromDict(
        py::reinterpret_borrow<py::dict>(waveCoverage),
        providerIdentity + "-waves");
  if (input.useWaves && !waveFieldsCoverage.available)
    throw py::value_error("selected wave coverage is unavailable");
  std::vector<wr::GeoPoint> chain{input.start};
  for (const auto& value : py::cast<py::sequence>(settings["waypoints"])) {
    const wr::GeoPoint next = point(value);
    if (next.latitude == chain.back().latitude &&
        next.longitude == chain.back().longitude)
      continue;
    chain.push_back(next);
  }
  if (chain.size() == 1 || chain.back().latitude != input.destination.latitude ||
      chain.back().longitude != input.destination.longitude)
    chain.push_back(input.destination);
  // A frozen zone containing a fixed chain mark (departure, waypoint or
  // destination) makes the request unsatisfiable. Name the zone rather than
  // returning a generic no-route, and never drop or shrink the zone.
  for (const auto& mark : chain) {
    const auto containing =
        sailworks::native_routing::containingAvoidZoneId(avoidZones, mark);
    if (containing)
      throw py::value_error("avoid zone '" + *containing +
                            "' contains a required route point");
  }
  if (!input.roundings.empty() &&
      input.roundings.size() != chain.size() - 1)
    throw py::value_error(
        "rounding specs must align one-to-one with chain marks");
  if (std::any_of(input.roundings.begin(), input.roundings.end(),
                  [](const auto& rounding) { return rounding.has_value(); }) &&
      engine != "main" && engine != "professional")
    throw py::value_error(
        "rounding constraints require the main or professional native engine");

  auto vessel = vesselFromPolar(polar, polarIdentity, "selected Sailworks polar");
  auto request = sailworks::native_routing::buildUserRequest(
      input, vessel, windCoverage, waveFieldsCoverage);
  const wr::CancellationToken cancellation;
  request.cancellation = cancellation;
  // Install the checkpoint even without a progress callback so a cancel event
  // still gets the engine's ~1 s cooperative checkpoints.
  request.progress = makeProgressCallback(progress, cancellation, cancelEvent);
  wr::RoutingEnvironment environment;
  environment.grib = std::make_shared<PythonWeatherProvider>(
      std::move(wind), std::move(current), std::move(waves),
      waveFieldsCoverage, windCoverage, providerIdentity,
      wr::EnvironmentalSource::GribForecast);
  // A no-zone run keeps the exact shoreline adapter (or open water) of the base
  // revision: no polygon provider is constructed, no zone predicate runs and
  // the land identity is unchanged. Zones are composed only when frozen in.
  std::shared_ptr<const wr::LandAndBoundaryProvider> coastline;
  if (input.coastlineChecks) {
    coastline = std::make_shared<PythonLandProvider>(
        std::move(land), providerIdentity + "-shoreline");
  }
  if (avoidZoneProvider) {
    environment.landAndBoundaries = sailworks::native_routing::composeAvoidZones(
        std::move(coastline), avoidZoneProvider, avoidZonesDigest,
        providerIdentity);
  } else if (coastline) {
    environment.landAndBoundaries = std::move(coastline);
  } else {
    environment.landAndBoundaries = std::make_shared<wr::OpenWaterProvider>();
  }
  environment.performance = std::make_shared<sailworks::native_routing::SailworksPerformance>(
      std::move(vessel), input.maximumWindKnots, input.minimumSailingKnots,
      input.motorKnots, input.windBasedWaveProxy);

  if (chain.size() == 2) {
    auto run = sailworks::native_routing::runRequest(
        engine, std::move(request), std::move(environment));
    py::dict result = runDict(run);
    result["wave_provenance"] = waveProvenanceDict(
        run.result, input.useWaves, input.windBasedWaveProxy);
    addAvoidZoneProvenance(result, avoidZones, avoidZonesDigest);
    return result;
  }
  const auto run = sailworks::native_routing::runUserChain(
      engine, std::move(request), std::move(environment), chain,
      input.roundings);
  py::dict result = runDict(run.result, &run.segments);
  result["wave_provenance"] = waveProvenanceDict(
      run.result.result, input.useWaves, input.windBasedWaveProxy);
  addAvoidZoneProvenance(result, avoidZones, avoidZonesDigest);
  return result;
}

py::dict identityDict() {
  py::dict result;
  result["bridge"] = "sailworks-native-routing-bridge";
  result["bridge_version"] = SAILWORKS_NATIVE_ROUTING_BRIDGE_VERSION;
  result["abi"] = 1;

  py::dict source;
  source["repository"] = "rgleason/weather_routing_pi";
  source["commit"] = SAILWORKS_NATIVE_ROUTING_UPSTREAM_COMMIT;
  source["tree_sha256"] = SAILWORKS_NATIVE_ROUTING_TREE_SHA256;
  source["tree_file_count"] = SAILWORKS_NATIVE_ROUTING_TREE_FILE_COUNT;
  source["tree_identity_algorithm"] =
      "sha256 of sorted lines '<sha256(file)>  <posix-relative-path>\\n' "
      "for every non-hidden regular file under native/routing/upstream; "
      "the generated identity header is outside that tree";
  result["source"] = source;

  py::dict versions;
  versions["engine"] = SAILWORKS_NATIVE_ROUTING_ENGINE_VERSION;
  versions["engine_cmake_project"] =
      SAILWORKS_NATIVE_ROUTING_ENGINE_CMAKE_VERSION;
  versions["original_engine_cmake_project"] =
      SAILWORKS_NATIVE_ROUTING_ORIGINAL_CMAKE_VERSION;
  versions["original_engine_label"] =
      "catalogue v1.15.45.7 (unverified upstream label)";
  versions["version_scheme"] =
      "VERSION file and CMake project() are read from the vendored tree at "
      "configure time and reported separately";
  result["versions"] = versions;

  py::dict licenses;
  licenses["engine"] = "Apache-2.0";
  licenses["original_engine"] = "GPL-3.0-or-later";
  licenses["original_engine_zlib_dependency"] = "Zlib";
  licenses["supercpn_notice"] =
      "third_party/supercpn-weather-routing/NOTICE.md";
  result["licenses"] = licenses;

  py::dict localPatches;
  py::list patchEntries;
  const auto addPatch = [&patchEntries](const char* id) {
    py::dict entry;
    entry["id"] = id;
    entry["documentation"] =
        "native/routing/upstream/weather-routing-engine/LOCAL_PATCHES.md";
    patchEntries.append(entry);
  };
  addPatch("xweatherrouting-1.29.2-coastal-departure-validation");
  addPatch("xweatherrouting-1.29-low-wind-motor-headings");
  addPatch("weather-routing-1.17.5-bounded-progress-and-forward-retry");
  addPatch("rounding-constraints");
  localPatches["count"] = static_cast<int>(patchEntries.size());
  localPatches["entries"] = patchEntries;
  localPatches["documentation"] =
      "native/routing/upstream/weather-routing-engine/LOCAL_PATCHES.md";
  localPatches["byte_identical_to_pinned_commit"] = false;
  localPatches["policy"] =
      "imported integration snapshot at the pinned plugin commit, not an "
      "upstream byte-identical copy; the integration repository carries the "
      "documented fixes in LOCAL_PATCHES.md";
  result["local_patches"] = localPatches;

  py::dict capabilities;
  capabilities["native_calculation"] = true;
  capabilities["deterministic_synthetic"] = true;
  capabilities["independent_replay_validation"] = true;
  capabilities["cancellation_without_accepted_completion"] = true;
  capabilities["cancellation_scope"] =
      "cooperative mid-route stop: the caller's cancel_event is polled at the "
      "engine progress checkpoint (latency bounded by the engine's ~1 s "
      "progress throttle) and trips RoutingRequest::cancellation; process "
      "termination is only the fallback if the worker does not stop in time; "
      "a cancelled run reports status 'cancelled' and never publishes an "
      "accepted or partial route";
  capabilities["progress"] = true;
  capabilities["progress_updates"] = true;
  capabilities["progress_source"] = "engine";
  capabilities["progress_contract"] =
      "RoutingRequest::progress reports RoutingProgressUpdate with stage, "
      "attempt/totalAttempts, generated/retained states, land checks, closest "
      "approach and effortPercent; the bridge forwards those engine values "
      "and the worker throttles them over its result pipe. No percentage is "
      "synthesized when the engine reports none";
  py::list progressStages;
  progressStages.append("preflight");
  progressStages.append("current_coverage");
  progressStages.append("forward_isochrone");
  progressStages.append("reverse_recovery");
  progressStages.append("frontier_recovery");
  progressStages.append("graph_fallback");
  progressStages.append("validation");
  progressStages.append("complete");
  capabilities["progress_stages"] = progressStages;
  capabilities["route_model_omissions"] =
      "leg wind/current/wave availability and per-leg safety metadata are "
      "exposed; bounded engine inspection geometry (isochrone layers, "
      "disconnected contours and predecessor traces) is exported, but no "
      "route-level alternative or ensemble model is exported";
  capabilities["inspection_geometry"] = true;
  capabilities["inspection_geometry_capture"] =
      "always enabled by the vendored engine; no request flag is required";
  capabilities["inspection_geometry_source"] =
      "engine IsochroneLayer/IsochroneContour/IsochroneTrace; bounded and "
      "possibly sampled (at most 48 traces per layer, one contour "
      "representative per heading sector)";
  capabilities["inspection_geometry_producers"] =
      "main and original-contour searches can emit layers; Standard Quick "
      "may return none; Professional returns only its selected result's layers";
  capabilities["inspection_trace_times"] =
      "absent times mean unknown, never zero and never a synthesized constant "
      "step";
  py::list engines;
  engines.append("main");
  engines.append("standard");
  engines.append("original");
  engines.append("professional");
  capabilities["engines"] = engines;
  capabilities["professional_incumbent_wired"] = true;
  capabilities["professional_parity_claimed"] = false;
  capabilities["orchestration_limits"] =
      "Standard uses QuickRoutingEngine default budgets; original contour "
      "uses its default 15-minute runtime and resource limits with inspection "
      "capture; Professional runs contour Quick first and forwards only an "
      "independently validated incumbent. Its second-stage deadline starts "
      "after Quick, not at the beginning of the combined operation; host "
      "memory probing and multi-worker parallel search are not implemented";
  capabilities["gui"] = false;
  capabilities["opencpn"] = false;
  capabilities["wxwidgets"] = false;
  capabilities["bundled_shoreline_data"] = false;
  capabilities["gshhg_data"] = false;
  capabilities["real_forecast_acquisition"] = false;
  capabilities["external_weather_provider"] = true;
  capabilities["external_weather_contract"] =
      "route_with_weather(wind, current, coverage) calls Python for every "
      "sample; wind is (from_degrees, speed_ms, available) and current is "
      "(east_ms, north_ms, available) pointing toward motion; the solver runs "
      "with the GIL held on the calling thread and no native concurrency";
  capabilities["wave_data_routing"] =
      "route_user_request accepts an optional wave sampler and coverage with "
      "dataset identity; use_waves makes missing coverage an explicit "
      "wave_data_required failure and records real-versus-proxy provenance. "
      "SailworksPerformance still applies only the optional wind-based proxy; "
      "the engine wave-data penalty is not enabled by this bridge";
  capabilities["external_weather_vessel_model"] =
      "the fixture polar, constraints and request limits are used unless a "
      "polar is supplied to route_with_weather as (twa_degrees, speed_kn) "
      "rows; polar_rows_from_polar() converts Sailworks radians and m/s "
      "without changing interpolation or efficiency";
  capabilities["production_routing_default"] = false;
  result["capabilities"] = capabilities;

  py::dict geometry;
  geometry["provider"] = "open-water";
  geometry["shoreline_data"] = false;
  geometry["depth_data"] = false;
  geometry["chart_hazards"] = false;
  geometry["note"] =
      "synthetic smoke routes use OpenWaterProvider; GSHHG/OpenCPN chart "
      "geometry is compiled as upstream source but no shoreline dataset is "
      "shipped";
  result["geometry"] = geometry;

  py::dict time;
  time["coverage"] =
      "synthetic valid hours 0,3,6 from 2026-01-01T00:00:00Z; linear "
      "interpolation between adjacent hours";
  time["timezone"] = "UTC";
  time["real_forecast"] = false;
  time["absolute_timestamps"] = true;
  result["time"] = time;

  py::dict platforms;
  platforms["verified"] = py::list();
  py::list declared;
  declared.append("macos-arm64");
  declared.append("linux-x86_64");
  declared.append("windows-x64");
  platforms["declared_build_targets"] = declared;
  platforms["note"] =
      "build and test verdicts are runner-owned; this bridge does not "
      "declare cross-platform support";
  result["platforms"] = platforms;

  py::dict fixture;
  fixture["id"] = "native-routing-synthetic-v1";
  fixture["revision"] = 1;
  fixture["synthetic"] = true;
  fixture["formulas"] =
      "wind U/V, pressure, current east/north and polar speed formulas from "
      "test/native_routing_fixtures.py";
  py::list polarTwa;
  for (const double angle : sailworks::native_routing::kPolarTwaDegrees)
    polarTwa.append(angle);
  fixture["polar_twa_degrees"] = polarTwa;
  fixture["polar_note"] =
      "the frozen contract has no 0-degree polar row; the engine clamps true "
      "wind angles below 30 degrees to the 30-degree row rather than "
      "extrapolating a separate 0-degree speed";
  result["fixture"] = fixture;

  py::dict missing;
  py::list unknowns;
  unknowns.append("no bundled shoreline or GSHHG dataset");
  unknowns.append("no depth or chart-hazard provider");
  unknowns.append("no real GRIB/current acquisition or sampler adapter");
  unknowns.append("no host GUI, OpenCPN callbacks or worker orchestration");
  unknowns.append(
      "inspection geometry is bounded engine output and may be sampled; trace "
      "times are absent when the producer emitted none");
  unknowns.append("cross-platform support is not established by this slice");
  missing["unknown_or_absent"] = unknowns;
  result["missing"] = missing;
  return result;
}

py::dict observablesDict() {
  const auto values = sailworks::native_routing::fixtureObservables();
  py::dict result;
  auto wind = [](const wr::WindSample& sample) {
    py::dict item;
    item["available"] = sample.available;
    item["east_knots"] = sample.velocity.eastKnots;
    item["north_knots"] = sample.velocity.northKnots;
    item["speed_knots"] =
        wr::vectorMagnitudeKnots(sample.velocity);
    double fromDegrees =
        std::fmod(wr::vectorDirectionToDegrees(sample.velocity) + 180.0, 360.0);
    if (fromDegrees < 0.0) fromDegrees += 360.0;
    item["from_degrees"] = fromDegrees;
    item["source"] = sourceMetadata(sample.metadata);
    return item;
  };
  auto current = [](const wr::CurrentSample& sample) {
    py::dict item;
    item["available"] = sample.available;
    item["east_knots"] = sample.velocity.eastKnots;
    item["north_knots"] = sample.velocity.northKnots;
    item["east_ms"] = sample.velocity.eastKnots / (3600.0 / 1852.0);
    item["north_ms"] = sample.velocity.northKnots / (3600.0 / 1852.0);
    item["source"] = sourceMetadata(sample.metadata);
    return item;
  };
  result["wind_sw_h0"] = wind(values.windSouthWestHour0);
  result["wind_ne_h0"] = wind(values.windNorthEastHour0);
  result["wind_sw_h1_blended"] = wind(values.windSouthWestHour1);
  result["pressure_sw_h0_hpa"] = values.pressureSouthWestHour0Pa / 100.0;
  result["pressure_ne_h0_hpa"] = values.pressureNorthEastHour0Pa / 100.0;
  result["current_sw_h0"] = current(values.currentSouthWestHour0);
  result["current_ne_h0"] = current(values.currentNorthEastHour0);
  result["current_ne_h3"] = current(values.currentNorthEastHour3);
  result["current_ne_h6"] = current(values.currentNorthEastHour6);
  result["polar_60_8_kn"] = values.polarSpeed60At8;
  result["polar_90_12_kn"] = values.polarSpeed90At12;
  result["polar_180_16_kn"] = values.polarSpeed180At16;
  return result;
}

py::dict validationFailureDict() {
  const SyntheticRun run = sailworks::native_routing::runSynthetic("main");
  py::dict result;
  result["original_route_accepted"] = run.accepted;
  if (run.result.legs.empty()) {
    result["tampered"] = false;
    result["passed"] = false;
    result["failure_reason"] = "synthetic route produced no legs to tamper";
    return result;
  }
  auto legs = run.result.legs;
  legs.back().end.latitude += 1.0;
  legs.back().end.longitude += 1.0;
  const wr::RouteValidationResult validation = wr::RouteValidator{}.validate(
      run.request, run.environment, *run.environment.performance, legs);
  result["tampered"] = true;
  result["tamper"] =
      "final delivered leg endpoint shifted +1 degree latitude/longitude";
  result["passed"] = validation.passed;
  result["failure_reason"] = validation.failureReason;
  result["samples"] = validation.samples;
  return result;
}

}  // namespace

PYBIND11_MODULE(sailworks_native_routing, module) {
  module.doc() =
      "Pinned native weather-routing bridge with a deterministic synthetic "
      "fixture; no GUI, forecast acquisition or bundled shoreline data.";
  module.attr("__version__") = SAILWORKS_NATIVE_ROUTING_BRIDGE_VERSION;

  module.def("identity", &identityDict,
             "Pinned source identity, versions, licences and capability gaps");
  module.def("synthetic_observables", &observablesDict,
             "Synthetic fixture samples evaluated by the native provider");
  module.def(
      "synthetic_route",
      [](const std::string& engine, const py::object& progress) {
        return runDict(sailworks::native_routing::runSynthetic(
            engine, makeProgressCallback(progress)));
      },
      py::arg("engine") = "main", py::arg("progress") = py::none(),
      "Run the deterministic synthetic scenario through a native engine; "
      "an optional progress callback receives engine RoutingProgressUpdate "
      "dictionaries");
  module.def(
      "synthetic_cancelled_route",
      [](const std::string& engine) {
        return runDict(sailworks::native_routing::runCancelled(engine));
      },
      py::arg("engine") = "main",
      "Run a pre-cancelled request; it must never report accepted completion");
  module.def("synthetic_invalid_destination", []() {
    return runDict(sailworks::native_routing::runInvalidDestination());
  }, "Run a structured failure outside the synthetic coverage area");
  module.def("synthetic_validation_failure", &validationFailureDict,
             "Replay a tampered leg with the independent validator");
  module.def(
      "route_with_weather",
      [](py::function wind, py::function current, py::dict coverage,
         const std::string& provider_identity, const std::string& source,
         const std::string& engine, bool cancelled,
         const py::object& polar, const std::string& polar_identity,
         const std::string& polar_assumptions, const py::object& start,
         const py::object& destination,
         const py::object& departure_epoch_s,
         const py::object& progress) {
        auto provider = std::make_shared<PythonWeatherProvider>(
            std::move(wind), std::move(current), py::none(),
            wr::ParameterCoverage{},
            coverageFromDict(coverage, provider_identity), provider_identity,
            sourceFromName(source));
        std::optional<wr::VesselConfiguration> vessel;
        if (!polar.is_none())
          vessel = vesselFromPolar(polar, polar_identity, polar_assumptions);
        auto requestPoint = [](const py::object& value,
                               const char* name) -> std::optional<wr::GeoPoint> {
          if (value.is_none()) return std::nullopt;
          const py::sequence pair = py::cast<py::sequence>(value);
          if (py::len(pair) != 2)
            throw py::value_error(std::string(name) +
                                  " must be a (latitude, longitude) pair");
          return wr::GeoPoint{py::cast<double>(pair[0]),
                              py::cast<double>(pair[1])};
        };
        sailworks::native_routing::FixtureRequestOverride overrides;
        overrides.start = requestPoint(start, "start");
        overrides.destination = requestPoint(destination, "destination");
        if (!departure_epoch_s.is_none()) {
          overrides.departure = wr::TimePoint{wr::Duration{
              py::cast<std::int64_t>(departure_epoch_s)}};
        }
        return runDict(sailworks::native_routing::runWithProvider(
            engine, std::move(provider), cancelled, std::move(vessel),
            overrides, makeProgressCallback(progress)));
      },
      py::arg("wind"), py::arg("current"), py::arg("coverage"),
      py::arg("provider_identity") = "python-weather-provider",
      py::arg("source") = "grib", py::arg("engine") = "main",
      py::arg("cancelled") = false, py::arg("polar") = py::none(),
      py::arg("polar_identity") = "supplied-polar",
      py::arg("polar_assumptions") = "", py::arg("start") = py::none(),
      py::arg("destination") = py::none(),
      py::arg("departure_epoch_s") = py::none(),
      py::arg("progress") = py::none(),
      "Run the synthetic scenario with weather sampled through Python "
      "callables; wind returns (from_degrees, speed_ms, available) and "
      "current returns (east_ms, north_ms, available) toward motion. An "
      "optional polar replaces the fixture vessel model. Optional start and "
      "destination are (latitude, longitude) pairs and departure_epoch_s is "
      "UTC epoch seconds; setting them relocates only the request geometry, "
       "the polar, constraints, options and limits stay frozen. Optional "
       "progress receives engine RoutingProgressUpdate dictionaries.");
  module.def("route_user_request",
             [](py::function wind, py::function current, py::dict coverage,
                py::object polar, py::dict settings, py::object land,
                const std::string& providerIdentity,
                const std::string& polarIdentity,
                const std::string& engine, py::object waves,
                py::object waveCoverage, const py::object& progress,
                const py::object& cancelEvent) {
               return runUserBridge(std::move(wind), std::move(current),
                                    std::move(waves), waveCoverage, coverage,
                                    polar, settings, land, providerIdentity,
                                    polarIdentity, engine, progress,
                                    cancelEvent);
             },
             py::arg("wind"), py::arg("current"), py::arg("coverage"),
             py::arg("polar"), py::arg("settings"), py::arg("land"),
             py::arg("provider_identity"), py::arg("polar_identity"),
             py::arg("engine") = "main", py::arg("waves") = py::none(),
             py::arg("wave_coverage") = py::none(),
             py::arg("progress") = py::none(),
             py::arg("cancel_event") = py::none(),
             "Run a selected request, including its waypoint chain, through the "
             "native engine; reject unsupported settings rather than "
             "inheriting synthetic fixture defaults. Optional waves returns "
             "(height_m, period_s, direction_from_deg, available[, dataset[, "
             "model]]) and wave_coverage carries its area, times and dataset "
             "identity; use_waves makes missing wave coverage an explicit "
             "failure. Optional cancel_event is polled at each engine progress "
             "checkpoint; when it is set the engine stops cooperatively and "
             "reports status 'cancelled'.");
}
