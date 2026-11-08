//! Native routing geometry for `weatherrouting.utils`.
//!
//! These functions mirror the Python reference in `weatherrouting.utils`,
//! including its use of the WGS84 ellipsoid for `point_distance`,
//! `ortodromic` and `routage_point_distance`, and its spherical (a = 6370997 m)
//! ellipsoid for `lossodromic`. The Python module remains the fallback and the
//! equivalence reference.

use std::sync::OnceLock;

use geographiclib_rs::{DirectGeodesic, Geodesic, InverseGeodesic};
use pyo3::prelude::*;

/// Matches `weatherrouting.utils.km2nm` and `nm2km`.
const KM_TO_NM: f64 = 0.539957;
/// PROJ `ellps=sphere`, which `latlon.LatLon.distance(ellipse="sphere")` uses.
const SPHERE_RADIUS_M: f64 = 6_370_997.0;

fn wgs84() -> &'static Geodesic {
    static GEODESIC: OnceLock<Geodesic> = OnceLock::new();
    GEODESIC.get_or_init(Geodesic::wgs84)
}

fn sphere() -> &'static Geodesic {
    static GEODESIC: OnceLock<Geodesic> = OnceLock::new();
    GEODESIC.get_or_init(|| Geodesic::new(SPHERE_RADIUS_M, 0.0))
}

/// The range normalization performed by `latlon.Longitude`.
fn longitude(value: f64) -> f64 {
    (value + 180.0).rem_euclid(360.0) - 180.0
}

fn inverse(ellipsoid: &Geodesic, lat_a: f64, lon_a: f64, lat_b: f64, lon_b: f64) -> (f64, f64) {
    let (metres, azimuth, _, _) =
        ellipsoid.inverse(lat_a, longitude(lon_a), lat_b, longitude(lon_b));
    (metres / 1000.0, azimuth)
}

fn distance_in_km_to_unit(km: f64, unit: &str) -> f64 {
    // The Python reference treats every unit other than "nm" as kilometres.
    if unit == "nm" {
        km * KM_TO_NM
    } else {
        km
    }
}

/// Return the WGS84 geodesic distance in `unit` (`"nm"` or kilometres).
#[pyfunction]
#[pyo3(signature = (lat_a, lon_a, lat_b, lon_b, unit="nm"))]
fn point_distance(lat_a: f64, lon_a: f64, lat_b: f64, lon_b: f64, unit: &str) -> f64 {
    let (km, _) = inverse(wgs84(), lat_a, lon_a, lat_b, lon_b);
    distance_in_km_to_unit(km, unit)
}

/// Return `(WGS84 distance in km, initial azimuth in radians)`.
#[pyfunction]
fn ortodromic(lat_a: f64, lon_a: f64, lat_b: f64, lon_b: f64) -> (f64, f64) {
    let (km, azimuth) = inverse(wgs84(), lat_a, lon_a, lat_b, lon_b);
    (km, azimuth.to_radians())
}

/// Return `(spherical distance in km, WGS84 initial azimuth in radians)`.
///
/// The Python reference asks `latlon` for a spherical distance but the default
/// (WGS84) heading, so the two halves use different ellipsoids.
#[pyfunction]
fn lossodromic(lat_a: f64, lon_a: f64, lat_b: f64, lon_b: f64) -> (f64, f64) {
    let (km, _) = inverse(sphere(), lat_a, lon_a, lat_b, lon_b);
    let (_, azimuth) = inverse(wgs84(), lat_a, lon_a, lat_b, lon_b);
    (km, azimuth.to_radians())
}

/// Return the WGS84 point reached by `distance` along `hdg` (radians).
#[pyfunction]
#[pyo3(signature = (lat_a, lon_a, distance, hdg, unit="nm"))]
fn routage_point_distance(
    lat_a: f64,
    lon_a: f64,
    distance: f64,
    hdg: f64,
    unit: &str,
) -> PyResult<(f64, f64)> {
    let km = match unit {
        "nm" => distance / KM_TO_NM,
        "km" => distance,
        other => {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "unsupported distance unit: {other}"
            )))
        }
    };
    let (lat, lon, _, _) = wgs84().direct(lat_a, longitude(lon_a), hdg.to_degrees(), km * 1000.0);
    Ok((lat, longitude(lon)))
}

#[pymodule]
fn _geometry_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(point_distance, m)?)?;
    m.add_function(wrap_pyfunction!(ortodromic, m)?)?;
    m.add_function(wrap_pyfunction!(lossodromic, m)?)?;
    m.add_function(wrap_pyfunction!(routage_point_distance, m)?)?;
    Ok(())
}
