from datetime import datetime, timedelta

from sailworks.core.race import RaceBoat, RacePosition


def test_position_append_does_not_traverse_existing_positions(monkeypatch):
    boat = RaceBoat(boat_id="append")

    reads = []

    class CountedPosition(RacePosition):
        def __getattribute__(self, name):
            if name == "_boats":
                reads.append(self)
            return super().__getattribute__(name)

    # Populate first: the guard below measures only what the next append does.
    for index in range(8):
        boat.positions.append(CountedPosition())
    reads.clear()

    added = CountedPosition()
    boat.positions.append(added)

    # An append inspects only the new position, never the tracked entries.
    assert reads and all(position is added for position in reads)


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
