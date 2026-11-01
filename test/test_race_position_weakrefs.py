from datetime import datetime, timedelta

from gweatherrouting.core.race import RaceBoat, RacePosition


def test_position_append_does_not_traverse_existing_positions(monkeypatch):
    boat = RaceBoat(boat_id="append")

    class ExistingPosition(RacePosition):
        def __getattribute__(self, name):
            if name == "_boats":
                raise AssertionError("existing position was traversed")
            return super().__getattribute__(name)

    # Populate before guarding access: each append must only register the new
    # position, not inspect all previously tracked entries.
    for index in range(8):
        boat.positions.append(ExistingPosition())


def test_equal_boats_keep_separate_position_invalidation_registrations():
    first_time = datetime(2024, 1, 1)
    shared = RacePosition(timestamp=first_time)
    first = RaceBoat(boat_id="same", positions=[shared])
    second = RaceBoat(boat_id="same", positions=[shared])

    assert first == second
    assert first is not second

    first.positions.remove(shared)
    shared.timestamp = first_time + timedelta(hours=1)

    second._ensure_position_index()
    assert second._position_sorted_times == [shared.timestamp]
