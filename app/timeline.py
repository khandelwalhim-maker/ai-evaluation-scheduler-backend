from __future__ import annotations

from datetime import date as _date
from typing import Protocol

Interval = tuple[int, int]


class PersonaLike(Protocol):
    division: str
    minor: str


# 09:00-10:10, 10:40-11:50, 12:10-13:20, 14:30-15:40, 16:00-17:10,
# 17:30-18:40, 19:00-20:10. Lunch 13:20-14:30 falls in the gap and is not a
# slot itself.
TEACHING_SLOTS: list[Interval] = [
    (540, 610),
    (640, 710),
    (730, 800),
    (870, 940),
    (960, 1030),
    (1050, 1120),
    (1140, 1210),
]

QUIZ_WINDOW: Interval = (495, 530)  # 08:15-08:50
QUIZ_MAX_DURATION = 35

EXAM_START_TIMES: tuple[int, ...] = (495, 540)  # 08:15, 09:00


def overlaps(a: Interval, b: Interval) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _parse_date(date_str: str) -> _date:
    year, month, day = (int(x) for x in date_str.split("-"))
    return _date(year, month, day)


def is_weekday(date_str: str) -> bool:
    return _parse_date(date_str).isoweekday() <= 5


def iso_week(date_str: str) -> tuple[int, int]:
    iso = _parse_date(date_str).isocalendar()
    return iso[0], iso[1]


def next_day(date_str: str) -> str:
    from datetime import timedelta

    return (_parse_date(date_str) + timedelta(days=1)).isoformat()


def teaching_intervals(state, persona: PersonaLike, date: str) -> list[Interval]:
    """core session intervals of the persona's division plus session
    intervals of the persona's minor on that date (SPEC: Audience and
    conflicts)."""
    from app.schemas import CohortKind, EntryKind

    day = state.dates.get(date)
    if day is None:
        return []

    intervals: list[Interval] = []
    for entry in day.entries:
        if entry.entry_kind != EntryKind.class_:
            continue
        if entry.start is None or entry.end is None:
            continue
        if entry.cohort_kind == CohortKind.division and entry.cohort_id == persona.division:
            intervals.append((entry.start, entry.end))
        elif entry.cohort_kind == CohortKind.minor and entry.cohort_id == persona.minor:
            intervals.append((entry.start, entry.end))
    return intervals
