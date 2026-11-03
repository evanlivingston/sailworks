from types import SimpleNamespace

from sailworks.rendering.isochrones import isochrone_overlay_data


def test_isochrone_overlay_data_preserves_connected_segments_and_gaps():
    frontier = [(48.0, -123.0), (48.001, -123.001), (49.0, -122.0)]
    frame = SimpleNamespace(
        isochrones=(frontier,),
        path=[(48.0, -123.0), (48.1, -123.1)],
        exclusions=[{"latitude": 48.2, "longitude": -123.2}],
    )
    data = isochrone_overlay_data(frame)
    assert len(data["frontiers"]) == 1
    assert len(data["frontiers"][0]["segments"]) == 1
    assert data["frontiers"][0]["points"] == [[49.0, -122.0]]
    assert data["path"] == [[48.0, -123.0], [48.1, -123.1]]
    assert data["exclusions"] == [[48.2, -123.2]]
