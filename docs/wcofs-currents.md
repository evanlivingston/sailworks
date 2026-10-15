# WCOFS currents

The marine coverage planner discovers NOAA WCOFS alongside CIOPS and worldwide
current coverage. Downloads use OPeNDAP subsets, not full hourly forecast files.
The selected run must bracket the requested window. Missing forecast hours remain
gaps; the existing current sampler refuses interpolation across gaps over one hour.

ROMS staggered surface components are averaged onto rho points and rotated by the
model's angle field into true east/north. Native curvilinear cells are located
once per download, then bilinearly resampled onto a regular 0.04-degree grid for
the existing current display and routing consumers. This is an explicit spatial
resampling step, not additional model resolution. It does not resolve narrow
channels that the roughly 4 km source grid cannot represent.

NetCDF masks, staggered face masks, and rho land masks remain unavailable values.
No wet-corner renormalization bridges mixed land/water cells. Source provenance
includes the static grid and every hourly subset. SHA-256 hashes identify decoded
subset arrays, not the URL or the full remote NetCDF file. Discovery and hourly
reads check cancellation between requests; an individual netCDF request is handled
by the netCDF library.

Live validation read the official 2026-10-15 03Z forecast for 04–05Z over
125–124.8 W, 47–47.2 N: a 2×6×6 regular field, 72 finite samples, maximum current
speed 0.412 m/s, and three source records. Offline tests cover rotation, masks,
window selection, missing coverage, and source records.

The provider returns total ocean currents including tides. Existing field
selection chooses among overlapping sources; it does not add WCOFS and another
model's tidal current together.
