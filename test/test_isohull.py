"""Convex hull geometry behind the isochrone fill and outline payload.

Only the pure helpers are covered here: clustering, the hull ring, its area,
and the point-count and dateline guards. The payload that consumes them is
covered by ``test_isochrone_outlines.py`` and ``test_isochrone_fill.py``.
"""

from __future__ import annotations

import pytest

from sailworks.rendering.isohull import (
    CLUSTER_CELL_FACTOR,
    MIN_CLUSTER_CELL_DEGREES,
    MIN_HULL_POINTS,
    cluster_cell,
    cluster_points,
    convex_hull,
    hull_area,
    layer_hulls,
)


def _square(size, lat=48.0, lon=-124.0):
    return [
        (lat, lon),
        (lat, lon + size),
        (lat + size, lon + size),
        (lat + size, lon),
    ]


def _signed_area(ring):
    """Shoelace in x=longitude, y=latitude: positive means counter-clockwise."""
    total = 0.0
    for index, (lat1, lon1) in enumerate(ring):
        lat2, lon2 = ring[(index + 1) % len(ring)]
        total += lon1 * lat2 - lon2 * lat1
    return total / 2.0


def _arc(lat, lon, count, step=0.05):
    return [(lat, lon + step * index) for index in range(count)]


def test_convex_hull_returns_a_counter_clockwise_ring():
    ring = convex_hull([(48.0, -124.0), (48.0, -123.0), (49.0, -124.0)])

    assert ring == ((48.0, -124.0), (48.0, -123.0), (49.0, -124.0))
    assert _signed_area(ring) > 0.0
    assert hull_area(ring) > 0.0


def test_convex_hull_keeps_only_the_envelope_corners():
    # An interior point is not a corner of the envelope, so the ring is still
    # the four corners of the square, in counter-clockwise order.
    corners = set(_square(1.0))
    ring = convex_hull(_square(1.0) + [(48.4, -123.6)])

    assert len(ring) == 4
    assert set(ring) == corners
    assert _signed_area(ring) > 0.0


def test_hull_area_is_the_projected_square():
    from math import cos, radians

    # Longitude is scaled by cos(mean latitude) so the ordering matches the
    # chart; one degree square at 48..49N is therefore slightly under one.
    assert hull_area(_square(1.0)) == pytest.approx(cos(radians(48.5)), rel=1e-6)
    assert hull_area(_square(0.5)) < hull_area(_square(1.0)) < hull_area(_square(2.0))
    assert hull_area([(48.0, -124.0), (48.5, -123.5)]) == 0.0


def test_convex_hull_drops_degenerate_input():
    assert convex_hull([]) == ()
    assert convex_hull([(48.0, -124.0)]) == ()
    assert convex_hull([(48.0, -124.0), (48.5, -123.5)]) == ()
    # Three points on one line enclose no area, so no ring is invented.
    assert convex_hull([(48.0, -124.0), (48.5, -123.5), (49.0, -123.0)]) == ()
    # Repeated points are not extra vertices.
    assert convex_hull([(48.0, -124.0)] * 4) == ()


def test_clustering_keeps_a_contiguous_arc_as_one_cluster():
    arm = _arc(48.0, -124.0, 10)
    clusters = cluster_points(arm, cell=0.1)

    assert [len(cluster) for cluster in clusters] == [10]


def test_clustering_separates_distant_arms():
    near = _arc(48.0, -124.0, 5)
    far = _arc(47.0, -110.0, 5)

    clusters = cluster_points(near + far, cell=0.1)

    assert sorted(len(cluster) for cluster in clusters) == [5, 5]
    assert all(cluster in (near, far) for cluster in clusters)


def test_cluster_cell_follows_spacing_with_a_floor():
    assert cluster_cell([]) == MIN_CLUSTER_CELL_DEGREES
    assert cluster_cell([(48.0, -124.0)]) == MIN_CLUSTER_CELL_DEGREES
    # A dense contour would otherwise produce thousands of cells for one front.
    assert cluster_cell(_arc(48.0, -124.0, 10, step=0.001)) == MIN_CLUSTER_CELL_DEGREES
    assert cluster_cell(_arc(48.0, -124.0, 10)) == pytest.approx(
        CLUSTER_CELL_FACTOR * 0.05
    )


def test_layer_hulls_clusters_before_hulling():
    first = _square(0.2, lon=-124.0)
    second = _square(0.2, lon=-118.0)

    hulls = layer_hulls(first + second)

    assert len(hulls) == 2
    assert [len(hull) for hull in hulls] == [4, 4]
    assert all(_signed_area(hull) > 0.0 for hull in hulls)


def test_layer_hulls_drops_a_dateline_spanning_ring():
    spanning = [(48.0, -179.0), (48.0, 179.0), (50.0, 179.0), (50.0, -179.0)]

    # A cell wide enough to keep the ring as one cluster: the drop is the
    # longitude guard, not an artefact of the clustering.
    assert len(cluster_points(spanning, cell=200.0)) == 1
    assert layer_hulls(spanning, cell=200.0) == ()
    # The same ring away from the antimeridian is a usable envelope, so the
    # guard is what dropped the first one.
    control = [(48.0, -124.0), (48.0, -122.0), (50.0, -122.0), (50.0, -124.0)]
    assert len(layer_hulls(control, cell=200.0)) == 1


def test_layer_hulls_requires_the_minimum_point_count():
    assert MIN_HULL_POINTS == 3
    # Two points, and a third cluster that is only two distinct points, are
    # both dropped; a triangle is the smallest usable envelope.
    assert layer_hulls([(48.0, -124.0), (48.5, -123.5)], cell=0.1) == ()
    assert layer_hulls([(48.0, -124.0), (48.0, -123.0), (49.0, -124.0)], cell=2.0) == (
        ((48.0, -124.0), (48.0, -123.0), (49.0, -124.0)),
    )
    # The caller can raise the floor, which drops a whole square.
    assert layer_hulls(_square(1.0), min_points=5) == ()
