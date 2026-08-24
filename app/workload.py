from __future__ import annotations

from dataclasses import dataclass

from app.schemas import CalendarState, CohortKind, CohortRegistry, EntryKind
from app.timeline import iso_week

WEIGHTS: dict[str, float] = {"quiz": 1, "midterm": 2, "endterm": 3}

_EXISTING_KINDS = (EntryKind.banner, EntryKind.existing_assessment)


@dataclass(frozen=True)
class Persona:
    division: str
    minor: str


@dataclass(frozen=True)
class Assessment:
    date: str
    start: int
    end: int
    type: str
    scope: str  # "core" or a minor name matching the registry
    name: str = ""


def build_personas(registry: CohortRegistry) -> list[Persona]:
    return [Persona(division=d, minor=m) for d in registry.divisions for m in registry.minors]


def assessment_audience(assessment: Assessment, personas: list[Persona]) -> list[Persona]:
    if assessment.scope == "core":
        return list(personas)
    return [p for p in personas if p.minor == assessment.scope]


def infer_assessment_type(raw_label: str) -> str:
    label = raw_label.lower()
    if "end term" in label or "endterm" in label:
        return "endterm"
    if "mid term" in label or "midterm" in label:
        return "midterm"
    if "quiz" in label:
        return "quiz"
    return "other"


def existing_assessments(state: CalendarState) -> list[Assessment]:
    result: list[Assessment] = []
    for date, day in state.dates.items():
        for entry in day.entries:
            if entry.entry_kind not in _EXISTING_KINDS:
                continue
            if entry.start is None or entry.end is None:
                continue
            scope = entry.cohort_id if entry.cohort_kind == CohortKind.minor else "core"
            result.append(
                Assessment(
                    date=date,
                    start=entry.start,
                    end=entry.end,
                    type=infer_assessment_type(entry.raw_label),
                    scope=scope,
                    name=entry.raw_label,
                )
            )
    return result


def daily_load(
    state: CalendarState, persona: Persona, date: str, weights: dict[str, float] = WEIGHTS
) -> float:
    return sum(
        weights.get(a.type, 0)
        for a in existing_assessments(state)
        if a.date == date and assessment_audience(a, [persona])
    )


def weekly_load(
    state: CalendarState,
    persona: Persona,
    week: tuple[int, int],
    weights: dict[str, float] = WEIGHTS,
) -> float:
    return sum(
        weights.get(a.type, 0)
        for a in existing_assessments(state)
        if iso_week(a.date) == week and assessment_audience(a, [persona])
    )


def worst_persona_week(
    state: CalendarState, weights: dict[str, float] = WEIGHTS
) -> tuple[Persona, tuple[int, int], float] | None:
    personas = build_personas(state.cohorts)
    weeks = {iso_week(d) for d in state.dates}

    best: tuple[Persona, tuple[int, int], float] | None = None
    for persona in personas:
        for week in weeks:
            load = weekly_load(state, persona, week, weights)
            if best is None or load > best[2]:
                best = (persona, week, load)
    return best
