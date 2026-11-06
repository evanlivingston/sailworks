"""Offline gazetteer search, merging and bundled-data checks."""

from dataclasses import replace

from sailworks.core.places import Place, PlaceIndex, normalize


def test_prefix_multiword_diacritics_and_alias_search():
    index = PlaceIndex(
        (
            Place("Seattle", "city", 47.6, -122.3, "United States"),
            Place("Port Townsend", "port", 48.1, -122.75, "United States"),
            Place("Ålesund", "port", 62.47, 6.16, "Norway"),
            Place(
                "San Francisco",
                "city",
                37.78,
                -122.4,
                "United States",
                aliases=("SF",),
            ),
        )
    )

    assert [place.name for place in index.search("seat")] == ["Seattle"]
    assert [place.name for place in index.search("port t")] == ["Port Townsend"]
    assert [place.name for place in index.search("alesund")] == ["Ålesund"]
    assert [place.name for place in index.search("sf")] == ["San Francisco"]
    assert normalize("  Ålesund  ") == "alesund"


def test_nearby_results_are_ordered_by_distance():
    index = PlaceIndex(
        (
            Place("Portland", "city", 45.52, -122.68),
            Place("Portland", "city", 43.66, -70.26),
        )
    )

    closest = index.search("portland", near=(45.5, -122.6))
    assert [place.longitude for place in closest] == [-122.68, -70.26]


def test_adding_a_nearby_port_merges_the_city_and_keeps_population():
    index = PlaceIndex(
        (
            Place(
                "Seattle",
                "city",
                47.6062,
                -122.3321,
                "United States of America",
                population=737015,
            ),
        )
    )
    index.add_places(
        (
            Place(
                "Seattle",
                "port",
                47.6,
                -122.333,
                "United States",
                detail="Major",
            ),
        ),
        merge_radius_km=20.0,
    )

    assert len(index) == 1
    place = index.places()[0]
    assert place.kind == "port"
    assert place.population == 737015
    assert place.detail == "Major"
    assert "World" not in place.source


def test_bundled_index_contains_ports_cities_and_chart_ready_names():
    index = PlaceIndex.from_default()
    assert len(index) > 9000
    for query, expected in (
        ("seattle", "Seattle"),
        ("port townsend", "Port Townsend"),
        ("auckland", "Auckland"),
        ("alesund", "Alesund"),
    ):
        names = [place.name for place in index.search(query, limit=5)]
        assert expected in names, (query, names)
    seattle = index.search("seattle", limit=1)[0]
    assert seattle.kind == "port"
    assert seattle.population > 0


def test_ports_rank_above_equal_population_cities():
    city = Place("Example", "city", 0.0, 0.0, population=1000)
    port = replace(city, kind="port")
    index = PlaceIndex((city, port))
    assert index.search("example")[0].kind == "port"
