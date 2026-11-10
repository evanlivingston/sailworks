#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cmath>
#include <cstdint>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "native_routing_tree_identity.h"
#include "synthetic.h"

namespace py = pybind11;
namespace wr = supercpn::weather_routing;
using sailworks::native_routing::SyntheticRun;

namespace {

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

py::dict runDict(const SyntheticRun& run) {
  py::dict result;
  result["engine"] = run.engine;
  result["status"] = sailworks::native_routing::statusName(run.result.status);
  result["status_code"] = static_cast<int>(run.result.status);
  result["accepted"] = run.accepted;
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
  result["request"] = requestInfo;
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
      "pre-cancelled request only; no mid-route cancellation";
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
      "isochrone layers are emitted by the main isochrone search; the "
      "original/professional entry points may return an empty layer list";
  capabilities["inspection_trace_times"] =
      "absent times mean unknown, never zero and never a synthesized constant "
      "step";
  py::list engines;
  engines.append("main");
  engines.append("original");
  engines.append("professional");
  capabilities["engines"] = engines;
  capabilities["professional_incumbent_wired"] = true;
  capabilities["professional_parity_claimed"] = false;
  capabilities["gui"] = false;
  capabilities["opencpn"] = false;
  capabilities["wxwidgets"] = false;
  capabilities["bundled_shoreline_data"] = false;
  capabilities["gshhg_data"] = false;
  capabilities["real_forecast_acquisition"] = false;
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
      [](const std::string& engine) {
        return runDict(sailworks::native_routing::runSynthetic(engine));
      },
      py::arg("engine") = "main",
      "Run the deterministic synthetic scenario through a native engine");
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
}
