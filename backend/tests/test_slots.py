"""Unit tests for slot generation: timezones, DST gaps/overlaps, filtering."""

import itertools
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.modules.bookings.slots import Slot, free_slots, generate_slots, rules_overlap


@dataclass(frozen=True)
class Rule:
    weekday: int
    start_time: time
    end_time: time
    slot_minutes: int = 30


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def starts(slots: list[Slot]) -> list[datetime]:
    return [s.start for s in slots]


def test_kolkata_slots_are_converted_to_utc() -> None:
    monday = date(2026, 12, 7)
    assert monday.weekday() == 0
    slots = generate_slots([Rule(0, time(9, 0), time(10, 30))], ZoneInfo("Asia/Kolkata"), monday, 1)
    # 09:00 IST = 03:30 UTC (IST is UTC+5:30, no DST)
    assert starts(slots) == [
        utc(2026, 12, 7, 3, 30),
        utc(2026, 12, 7, 4, 0),
        utc(2026, 12, 7, 4, 30),
    ]
    assert all(s.end - s.start == timedelta(minutes=30) for s in slots)


def test_only_matching_weekdays_produce_slots() -> None:
    slots = generate_slots(
        [Rule(2, time(10, 0), time(11, 0), 60)], ZoneInfo("Asia/Kolkata"), date(2026, 12, 7), 7
    )
    assert starts(slots) == [utc(2026, 12, 9, 4, 30)]  # Wednesday 10:00 IST


def test_partial_slot_at_end_of_window_is_dropped() -> None:
    slots = generate_slots(
        [Rule(0, time(9, 0), time(9, 50), 30)], ZoneInfo("Asia/Kolkata"), date(2026, 12, 7), 1
    )
    assert len(slots) == 1


def test_dst_spring_forward_skips_nonexistent_local_time() -> None:
    # 2026-03-08 in New York: 02:00 -> 03:00 (02:xx does not exist).
    day = date(2026, 3, 8)
    slots = generate_slots(
        [Rule(day.weekday(), time(1, 0), time(4, 0), 60)], ZoneInfo("America/New_York"), day, 1
    )
    assert starts(slots) == [
        utc(2026, 3, 8, 6, 0),  # 01:00 EST (UTC-5)
        utc(2026, 3, 8, 7, 0),  # 03:00 EDT (UTC-4); 02:00 skipped
    ]
    assert all(s.end - s.start == timedelta(hours=1) for s in slots)


def test_dst_fall_back_uses_real_elapsed_time() -> None:
    # 2026-11-01 in New York: 02:00 EDT -> 01:00 EST (01:xx happens twice).
    day = date(2026, 11, 1)
    slots = generate_slots(
        [Rule(day.weekday(), time(0, 0), time(3, 0), 60)], ZoneInfo("America/New_York"), day, 1
    )
    assert starts(slots) == [
        utc(2026, 11, 1, 4, 0),  # 00:00 EDT
        utc(2026, 11, 1, 5, 0),  # 01:00 EDT (first occurrence)
        utc(2026, 11, 1, 7, 0),  # 02:00 EST
    ]
    # Slots never overlap even though wall-clock 01:00 repeats.
    for a, b in itertools.pairwise(slots):
        assert a.end <= b.start


def test_free_slots_filters_window_and_busy() -> None:
    slots = [Slot(utc(2026, 12, 7, h), utc(2026, 12, 7, h, 30)) for h in (3, 4, 5, 6)]
    busy = [(utc(2026, 12, 7, 4, 15), utc(2026, 12, 7, 4, 45))]  # overlaps the 04:00 slot
    result = free_slots(
        slots, busy, not_before=utc(2026, 12, 7, 3, 30), not_after=utc(2026, 12, 7, 5, 0)
    )
    assert starts(result) == [utc(2026, 12, 7, 5, 0)]


def test_back_to_back_booking_does_not_block_next_slot() -> None:
    slots = [Slot(utc(2026, 12, 7, 4), utc(2026, 12, 7, 4, 30))]
    busy = [(utc(2026, 12, 7, 3, 30), utc(2026, 12, 7, 4, 0))]  # ends exactly at slot start
    assert free_slots(slots, busy, not_before=utc(2026, 1, 1), not_after=utc(2027, 1, 1)) == slots


def test_rules_overlap_detection() -> None:
    assert rules_overlap([Rule(0, time(9), time(12)), Rule(0, time(11), time(13))])
    assert not rules_overlap([Rule(0, time(9), time(12)), Rule(0, time(12), time(13))])
    assert not rules_overlap([Rule(0, time(9), time(12)), Rule(1, time(9), time(12))])
