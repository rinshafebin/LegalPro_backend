"""Turn weekly availability (advocate-local wall-clock times) into concrete UTC slots.

Pure functions, no I/O, so timezone and DST behaviour can be unit-tested directly.
"""

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class RuleLike(Protocol):
    @property
    def weekday(self) -> int: ...
    @property
    def start_time(self) -> time: ...
    @property
    def end_time(self) -> time: ...
    @property
    def slot_minutes(self) -> int: ...


@dataclass(frozen=True, order=True)
class Slot:
    start: datetime  # aware, UTC
    end: datetime  # aware, UTC

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end


def local_to_utc(naive_local: datetime, tz: ZoneInfo) -> datetime | None:
    """Convert a wall-clock time to UTC. Returns None if it doesn't exist (DST gap).

    For ambiguous times (DST fall-back) the first occurrence (fold=0) is used.
    """
    aware = naive_local.replace(tzinfo=tz, fold=0)
    utc = aware.astimezone(UTC)
    if utc.astimezone(tz).replace(tzinfo=None) != naive_local:
        return None  # skipped by a DST transition
    return utc


def generate_slots(
    rules: Iterable[RuleLike], tz: ZoneInfo, start_date: date, days: int
) -> list[Slot]:
    """All slots for `days` local dates from `start_date` (dates in the advocate's zone)."""
    rules = list(rules)
    slots: set[Slot] = set()
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        for rule in rules:
            if rule.weekday != day.weekday():
                continue
            step = timedelta(minutes=rule.slot_minutes)
            cursor = datetime.combine(day, rule.start_time)
            window_end = datetime.combine(day, rule.end_time)
            while cursor + step <= window_end:
                start_utc = local_to_utc(cursor, tz)
                if start_utc is not None:
                    slots.add(Slot(start_utc, start_utc + step))
                cursor += step
    return sorted(slots)


def free_slots(
    slots: Sequence[Slot],
    busy: Iterable[tuple[datetime, datetime]],
    *,
    not_before: datetime,
    not_after: datetime,
) -> list[Slot]:
    """Drop slots that start too soon / too late or overlap a busy interval."""
    busy = list(busy)
    return [
        slot
        for slot in slots
        if not_before <= slot.start <= not_after
        and not any(slot.overlaps(b_start, b_end) for b_start, b_end in busy)
    ]


def rules_overlap(rules: Sequence[RuleLike]) -> bool:
    by_day: dict[int, list[RuleLike]] = {}
    for rule in rules:
        by_day.setdefault(rule.weekday, []).append(rule)
    for day_rules in by_day.values():
        ordered = sorted(day_rules, key=lambda r: r.start_time)
        for previous, current in itertools.pairwise(ordered):
            if current.start_time < previous.end_time:
                return True
    return False
