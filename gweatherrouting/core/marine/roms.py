"""ROMS staggered-grid math for NOS Operational Forecast Systems.

ROMS places east-west velocity on u-points (between rho columns) and
north-south velocity on v-points (between rho rows). Velocities are
grid-relative: the ``angle`` field rotates xi-direction to true east.
Downstream code only consumes regular true east/north grids, so this
module averages staggered components to rho points, rotates them, and
bilinearly resamples onto a requested regular lat/lon grid.

Pure numpy, no network: unit-testable with synthetic grids.
"""

import numpy as np


def destagger_to_rho(u, v, mask_u=None, mask_v=None):
    """Average staggered components to rho points.

    u has shape (..., eta, xi - 1), v has shape (..., eta - 1, xi);
    both become (..., eta, xi). Edges reuse the nearest interior face.
    Returns (u_rho, v_rho, water) with water in [0, 1] when masks are
    given, else None.
    """
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    u_rho = np.empty(u.shape[:-2] + (u.shape[-2], u.shape[-1] + 1))
    u_rho[..., 0] = u[..., 0]
    u_rho[..., -1] = u[..., -1]
    u_rho[..., 1:-1] = 0.5 * (u[..., :-1] + u[..., 1:])
    v_rho = np.empty(v.shape[:-2] + (v.shape[-2] + 1, v.shape[-1]))
    v_rho[..., 0, :] = v[..., 0, :]
    v_rho[..., -1, :] = v[..., -1, :]
    v_rho[..., 1:-1, :] = 0.5 * (v[..., :-1, :] + v[..., 1:, :])
    water = None
    if mask_u is not None and mask_v is not None:
        mu = np.asarray(mask_u, dtype=float)
        mv = np.asarray(mask_v, dtype=float)
        wu = np.empty(mu.shape[:-2] + (mu.shape[-2], mu.shape[-1] + 1))
        wu[..., 0] = mu[..., 0]
        wu[..., -1] = mu[..., -1]
        wu[..., 1:-1] = 0.5 * (mu[..., :-1] + mu[..., 1:])
        wv = np.empty(mv.shape[:-2] + (mv.shape[-2] + 1, mv.shape[-1]))
        wv[..., 0, :] = mv[..., 0, :]
        wv[..., -1, :] = mv[..., -1, :]
        wv[..., 1:-1, :] = 0.5 * (mv[..., :-1, :] + mv[..., 1:, :])
        water = np.minimum(wu, wv)
    return u_rho, v_rho, water


def rotate_to_east_north(u_grid, v_grid, angle):
    """Rotate grid-relative components to true east/north."""
    u_grid = np.asarray(u_grid, dtype=float)
    v_grid = np.asarray(v_grid, dtype=float)
    angle = np.asarray(angle, dtype=float)
    cos_a, sin_a = np.cos(angle), np.sin(angle)
    return u_grid * cos_a - v_grid * sin_a, u_grid * sin_a + v_grid * cos_a


def _cell_bilinear(quad_lat, quad_lon, lat, lon):
    """Invert one bilinear cell; returns (fe, fx) or None when outside."""
    # quad order: (e,x), (e,x+1), (e+1,x), (e+1,x+1); two Newton steps
    # from the cell centre on the locally smooth ROMS grid.
    fe, fx = 0.5, 0.5
    for _ in range(3):
        w00 = (1 - fe) * (1 - fx)
        w01 = (1 - fe) * fx
        w10 = fe * (1 - fx)
        w11 = fe * fx
        plat = (
            w00 * quad_lat[0]
            + w01 * quad_lat[1]
            + w10 * quad_lat[2]
            + w11 * quad_lat[3]
        )
        plon = (
            w00 * quad_lon[0]
            + w01 * quad_lon[1]
            + w10 * quad_lon[2]
            + w11 * quad_lon[3]
        )
        # Jacobian of the bilinear map at (fe, fx).
        dlat_fe = (1 - fx) * (quad_lat[2] - quad_lat[0]) + fx * (
            quad_lat[3] - quad_lat[1]
        )
        dlat_fx = (1 - fe) * (quad_lat[1] - quad_lat[0]) + fe * (
            quad_lat[3] - quad_lat[2]
        )
        dlon_fe = (1 - fx) * (quad_lon[2] - quad_lon[0]) + fx * (
            quad_lon[3] - quad_lon[1]
        )
        dlon_fx = (1 - fe) * (quad_lon[1] - quad_lon[0]) + fe * (
            quad_lon[3] - quad_lon[2]
        )
        det = dlat_fe * dlon_fx - dlat_fx * dlon_fe
        if abs(det) < 1e-18:
            return None
        fe += ((lat - plat) * dlon_fx - (lon - plon) * dlat_fx) / det
        fx += (dlat_fe * (lon - plon) - dlon_fe * (lat - plat)) / det
    if -1e-6 <= fe <= 1 + 1e-6 and -1e-6 <= fx <= 1 + 1e-6:
        return min(max(fe, 0.0), 1.0), min(max(fx, 0.0), 1.0)
    return None


def locate(lat_rho, lon_rho, lats, lons):
    """Fractional rho indices for query points; NaN where uncovered."""
    lat_rho = np.asarray(lat_rho, dtype=float)
    lon_rho = np.asarray(lon_rho, dtype=float)
    lats = np.atleast_1d(np.asarray(lats, dtype=float))
    lons = np.atleast_1d(np.asarray(lons, dtype=float))
    eta, xi = lat_rho.shape
    flat_lat = lat_rho.ravel()
    flat_lon = lon_rho.ravel()
    fe = np.full(len(lats), np.nan)
    fx = np.full(len(lats), np.nan)
    for i, (lat, lon) in enumerate(zip(lats, lons)):
        if not (np.isfinite(lat) and np.isfinite(lon)):
            continue
        nearest = int(np.argmin((flat_lat - lat) ** 2 + (flat_lon - lon) ** 2))
        e0, x0 = nearest // xi, nearest % xi
        for e in (e0 - 1, e0):
            for x in (x0 - 1, x0):
                if e < 0 or x < 0 or e + 1 >= eta or x + 1 >= xi:
                    continue
                quad_lat = (
                    lat_rho[e, x],
                    lat_rho[e, x + 1],
                    lat_rho[e + 1, x],
                    lat_rho[e + 1, x + 1],
                )
                quad_lon = (
                    lon_rho[e, x],
                    lon_rho[e, x + 1],
                    lon_rho[e + 1, x],
                    lon_rho[e + 1, x + 1],
                )
                if not (all(np.isfinite(quad_lat)) and all(np.isfinite(quad_lon))):
                    continue
                found = _cell_bilinear(quad_lat, quad_lon, lat, lon)
                if found is not None:
                    fe[i], fx[i] = e + found[0], x + found[1]
                    break
            if np.isfinite(fe[i]):
                break
    return fe, fx


def sample_rho(field, fe, fx):
    """Bilinearly sample a rho-grid field at fractional indices."""
    field = np.asarray(field, dtype=float)
    eta, xi = field.shape[-2:]
    fe = np.asarray(fe, dtype=float)
    fx = np.asarray(fx, dtype=float)
    out = np.full(fe.shape, np.nan)
    ok = (
        np.isfinite(fe)
        & np.isfinite(fx)
        & (fe >= 0)
        & (fx >= 0)
        & (fe <= eta - 1)
        & (fx <= xi - 1)
    )
    if not ok.any():
        return out
    e0 = np.floor(fe[ok]).astype(int).clip(0, eta - 2)
    x0 = np.floor(fx[ok]).astype(int).clip(0, xi - 2)
    de, dx = fe[ok] - e0, fx[ok] - x0
    corners = (
        field[..., e0, x0],
        field[..., e0, x0 + 1],
        field[..., e0 + 1, x0],
        field[..., e0 + 1, x0 + 1],
    )
    values = (
        corners[0] * (1 - de) * (1 - dx)
        + corners[1] * (1 - de) * dx
        + corners[2] * de * (1 - dx)
        + corners[3] * de * dx
    )
    valid = np.isfinite(corners[0]) & np.isfinite(corners[1])
    valid &= np.isfinite(corners[2]) & np.isfinite(corners[3])
    out[np.flatnonzero(ok)[valid]] = values[valid]
    return out


def regular_field(lat_rho, lon_rho, east_rho, north_rho, water, lats, lons):
    """Resample rotated rho fields onto regular nodes; masked → NaN."""
    fe, fx = locate(lat_rho, lon_rho, lats, lons)
    east = sample_rho(east_rho, fe, fx)
    north = sample_rho(north_rho, fe, fx)
    if water is not None:
        wet = sample_rho(np.asarray(water, dtype=float), fe, fx)
        dry = ~np.isfinite(wet) | (wet < 0.5)
        east[dry] = np.nan
        north[dry] = np.nan
    return east, north


def output_grid(area, step_deg=0.04):
    """Regular node vectors covering an Area (west/south/east/north)."""
    lats = np.arange(area.south, area.north + step_deg / 2, step_deg)
    lons = np.arange(area.west, area.east + step_deg / 2, step_deg)
    return lats, lons


def prep_window(lat_rho, lon_rho, area, pad=0.25):
    """Rho index window covering an area; keeps OPeNDAP reads small."""
    inside = (
        (lat_rho >= area.south - pad)
        & (lat_rho <= area.north + pad)
        & (lon_rho >= area.west - pad)
        & (lon_rho <= area.east + pad)
    )
    rows, cols = np.nonzero(inside)
    if not len(rows):
        raise ValueError("Requested area lies outside the model grid")
    return (
        int(rows.min()),
        int(rows.max()) + 1,
        int(cols.min()),
        int(cols.max()) + 1,
    )
