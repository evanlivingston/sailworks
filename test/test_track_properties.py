"""A waypoint track carries the defaults route planning starts from."""

from __future__ import annotations

import pytest

from PySide6.QtCore import QDateTime

from sailworks.core.geo.track import Track


def test_track_round_trips_departure_and_colour():
    track = Track(
        "passage",
        [[1.0, 2.0], [3.0, 4.0]],
        color="#ff8800",
        default_departure="2026-06-01T09:15:00",
    )

    payload = track.to_json()
    restored = Track.from_json(payload)

    assert payload["color"] == "#ff8800"
    assert payload["default_departure"] == "2026-06-01T09:15:00"
    assert restored.color == "#ff8800"
    assert restored.default_departure == "2026-06-01T09:15:00"


def test_track_without_defaults_keeps_the_old_payload_shape():
    payload = Track("passage", [[1.0, 2.0]]).to_json()

    assert "color" not in payload
    assert "default_departure" not in payload


def test_track_rejects_a_malformed_departure():
    with pytest.raises(Exception, match="default_departure"):
        Track("passage", [[1.0, 2.0]], default_departure="tomorrow")


def test_properties_dialog_reports_the_saved_values(qtbot):
    from sailworks.qt.trackpropertiesdialog import TrackPropertiesDialog

    dialog = TrackPropertiesDialog(departure="2026-06-01T09:15:00", colour="#3366cc")
    qtbot.addWidget(dialog)

    assert dialog.use_departure.isChecked()
    assert dialog.departure.dateTime().toPython().year == 2026
    values = dialog.values()
    assert values["default_departure"].startswith("2026-06-01T09:15")
    assert values["color"] == "#3366cc"

    # Clearing the checkbox drops the saved default instead of inventing one.
    dialog.use_departure.setChecked(False)
    assert dialog.values()["default_departure"] is None


def test_properties_dialog_starts_unset_for_a_plain_track(qtbot):
    from sailworks.qt.trackpropertiesdialog import TrackPropertiesDialog

    dialog = TrackPropertiesDialog()
    qtbot.addWidget(dialog)

    assert not dialog.use_departure.isChecked()
    assert not dialog.departure.isEnabled()
    assert dialog.values() == {"default_departure": None, "color": None}


def test_track_colour_parsing():
    from sailworks.rendering.navigation import parse_track_colour

    assert parse_track_colour("#ff8800") == (1.0, 136 / 255, 0.0, 1.0)
    assert parse_track_colour("3366cc") == (51 / 255, 102 / 255, 204 / 255, 1.0)
    # A malformed or absent value keeps the default dashed style.
    assert parse_track_colour(None) is None
    assert parse_track_colour("red") is None
    assert parse_track_colour("#12345") is None
