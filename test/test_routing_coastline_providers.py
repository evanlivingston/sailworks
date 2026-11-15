# -*- coding: utf-8 -*-
"""Coastline providers for routing must not depend on the displayed basemap."""

from unittest.mock import MagicMock

from sailworks.core.core import LinePointValidityProvider
from sailworks.rendering.chartcollection import ChartCollection


def _collection():
    """A ChartCollection with only the state the provider methods touch."""
    charts = ChartCollection.__new__(ChartCollection)
    charts.charts = []
    charts.gshhsLayer = None
    charts.load_calls = []
    return charts


def _fake_coastline(charts):
    provider = MagicMock(spec=LinePointValidityProvider)
    provider.enabled = True
    charts.gshhsLayer = object()
    charts.charts.insert(0, provider)
    return provider


def test_ensure_loads_base_gshhs_when_no_provider_exists():
    charts = _collection()

    def load():
        charts.load_calls.append(1)
        return _fake_coastline(charts)

    charts._load_base_gshhs = load

    providers = charts.ensure_line_point_validity_providers()

    assert providers == [charts.charts[0]]
    assert charts.load_calls == [1]
    # A second call is satisfied by the registered provider, no second load.
    assert charts.ensure_line_point_validity_providers() == providers
    assert charts.load_calls == [1]


def test_ensure_keeps_existing_provider_without_loading():
    charts = _collection()
    provider = _fake_coastline(charts)
    charts._load_base_gshhs = lambda: charts.load_calls.append(1)

    assert charts.ensure_line_point_validity_providers() == [provider]
    assert charts.load_calls == []


def test_display_provider_lookup_still_does_not_load():
    charts = _collection()
    charts._load_base_gshhs = lambda: charts.load_calls.append(1)

    assert charts.get_line_point_validity_providers() == []
    assert charts.load_calls == []
