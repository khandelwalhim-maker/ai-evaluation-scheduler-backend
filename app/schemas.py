from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class EvaluationType(str, Enum):
    quiz = "quiz"
    midterm = "midterm"
    endterm = "endterm"
    group = "group"
    other = "other"


class Evaluation(BaseModel):
    name: str
    type: EvaluationType
    weightage: Optional[int] = None
    timing_note: Optional[str] = None
    in_scope: bool = True

    @field_validator("weightage", mode="before")
    @classmethod
    def _parse_weightage(cls, v):
        if isinstance(v, str):
            v = v.strip().rstrip("%").strip()
            if v == "":
                return None
            return int(float(v))
        return v

    @field_validator("in_scope", mode="after")
    @classmethod
    def _group_work_is_out_of_scope(cls, v, info):
        if info.data.get("type") == EvaluationType.group:
            return False
        return v


class CourseOutline(BaseModel):
    name: str
    code: Optional[str] = None
    term: Optional[str] = None
    instructor: Optional[str] = None
    evaluations: list[Evaluation] = Field(default_factory=list)


class CohortKind(str, Enum):
    division = "division"
    minor = "minor"
    banner = "banner"
    unknown = "unknown"


class EntryKind(str, Enum):
    class_ = "class"
    existing_assessment = "existing_assessment"
    banner = "banner"
    unknown = "unknown"


_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*(am|pm)?$", re.IGNORECASE)


def _time_to_minutes(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid time value: {value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text == "":
        return None
    match = _TIME_RE.match(text)
    if not match:
        raise ValueError(f"unrecognised time format: {value!r}")
    hour, minute, meridiem = match.groups()
    hour, minute = int(hour), int(minute)
    if meridiem:
        meridiem = meridiem.lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
    return hour * 60 + minute


class TimetableEntry(BaseModel):
    raw_label: str
    row_label: str
    cohort_kind: CohortKind
    cohort_id: Optional[str] = None
    course_guess: Optional[str] = None
    # Set once at parse time to the same value as course_guess, then never
    # mutated again -- unlike course_guess, which _confirmation_questions
    # (parser.py) and resolve_confirmation (orchestrator.py) both overwrite in
    # place once identity resolves. Anything that needs to key off "the
    # original code" regardless of resolution state (course_specializations
    # lookups) must join on this field, not course_guess.
    course_code: Optional[str] = None
    session_numbers: list[int] = Field(default_factory=list)
    start: Optional[int] = None
    end: Optional[int] = None
    entry_kind: EntryKind
    confidence: float = 1.0

    @field_validator("start", "end", mode="before")
    @classmethod
    def _normalize_time(cls, v):
        return _time_to_minutes(v)

    @field_validator("session_numbers", mode="before")
    @classmethod
    def _normalize_sessions(cls, v):
        if v is None:
            return []
        if isinstance(v, (int, str)):
            v = [v]
        numbers = []
        for item in v:
            if isinstance(item, int):
                numbers.append(item)
                continue
            digits = re.findall(r"\d+", str(item))
            numbers.extend(int(d) for d in digits)
        return numbers


_DATE_RE_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")
_DATE_RE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _normalize_date(value: object) -> str:
    text = str(value).strip()
    if _DATE_RE_ISO.match(text):
        return text
    match = _DATE_RE_SLASH.match(text)
    if not match:
        raise ValueError(f"unrecognised date format: {value!r}")
    day, month, year = match.groups()
    year_i = int(year)
    if year_i < 100:
        year_i += 2000
    return f"{year_i:04d}-{int(month):02d}-{int(day):02d}"


class TimetableDay(BaseModel):
    date: str
    holiday: bool = False
    entries: list[TimetableEntry] = Field(default_factory=list)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date(cls, v):
        return _normalize_date(v)


class ConfirmationQuestion(BaseModel):
    kind: str
    question: str
    context: Optional[str] = None


class ParsedTimetable(BaseModel):
    days: list[TimetableDay] = Field(default_factory=list)
    questions: list[ConfirmationQuestion] = Field(default_factory=list)


class CohortRegistry(BaseModel):
    divisions: list[str] = Field(default_factory=list)
    minors: list[str] = Field(default_factory=list)


class CalendarState(BaseModel):
    dates: dict[str, TimetableDay] = Field(default_factory=dict)
    cohorts: CohortRegistry = Field(default_factory=CohortRegistry)
    courses: list[CourseOutline] = Field(default_factory=list)
    questions: list[ConfirmationQuestion] = Field(default_factory=list)
    # User-supplied ABBR -> canonical course name mapping, uploaded via a
    # downloadable CSV/XLSX template. Consulted by parser._confirmation_questions
    # to resolve timetable course_guess codes without a manual confirmation.
    course_registry: dict[str, str] = Field(default_factory=dict)
    # User-supplied ABBR -> minor specialization name (one of cohorts.minors),
    # keyed identically to course_registry (course_registry.normalize_abbreviation).
    # Kept as a separate dict rather than widening course_registry's value type,
    # since parser.py's `set(registry.values())` requires those values stay
    # hashable strings. Purely descriptive/display metadata -- never consulted
    # by parsing or scheduling logic. Absent key means "core" (not yet tagged,
    # or a core course that runs across all divisions and has no single
    # specialization value to store).
    course_specializations: dict[str, str] = Field(default_factory=dict)
