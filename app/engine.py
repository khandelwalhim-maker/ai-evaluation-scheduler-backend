from __future__ import annotations

import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas import (
    CalendarState,
    CohortKind,
    ConfirmationQuestion,
    EntryKind,
    TimetableDay,
    TimetableEntry,
)
from app.timeline import (
    EXAM_START_TIMES,
    QUIZ_MAX_DURATION,
    QUIZ_WINDOW,
    is_weekday,
    iso_week,
    next_day,
    overlaps,
    teaching_intervals,
)
from app.workload import (
    Assessment,
    Persona,
    WEIGHTS,
    assessment_audience,
    build_personas,
    existing_assessments,
    infer_assessment_type,
    weekly_load,
)

AssessmentType = Literal["quiz", "midterm", "endterm"]

S1_WEIGHT = 10.0
S2_WEIGHT = 5.0
S3_WEIGHT = 5.0
S2_MAX_PER_WEEK = 3


class ScheduleRequest(BaseModel):
    course: str
    name: str
    type: AssessmentType
    scope: str  # "core" or a minor name matching the calendar's registry
    duration_minutes: Optional[int] = None
    after_session: Optional[int] = None
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    overrides: set[str] = Field(default_factory=set)

    @field_validator("duration_minutes", mode="after")
    @classmethod
    def _validate_duration(cls, v, info):
        req_type = info.data.get("type")
        if req_type == "quiz":
            if v is None:
                return QUIZ_MAX_DURATION
            if v > QUIZ_MAX_DURATION:
                raise ValueError(f"quizzes may not exceed {QUIZ_MAX_DURATION} minutes")
            return v
        if v is None:
            raise ValueError("duration_minutes is required for exams (midterm/endterm)")
        return v


class Candidate(BaseModel):
    date: str
    start: int
    end: int
    score: float
    reasons: list[str] = Field(default_factory=list)


class BlockedDate(BaseModel):
    date: str
    start: int
    end: int
    reason: str


class Proposal(BaseModel):
    id: str
    request: ScheduleRequest
    candidates: list[Candidate] = Field(default_factory=list)
    blocked: list[BlockedDate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    questions: list[ConfirmationQuestion] = Field(default_factory=list)


class AffectedAssessment(BaseModel):
    raw_label: str
    date: str
    start: Optional[int] = None
    end: Optional[int] = None
    reproposal: Proposal


class Impact(BaseModel):
    date: str
    affected: list[AffectedAssessment] = Field(default_factory=list)


_PROPOSALS: dict[str, Proposal] = {}


def _target_dates(state: CalendarState, request: ScheduleRequest) -> list[str]:
    dates = sorted(state.dates.keys())
    if request.window_start:
        dates = [d for d in dates if d >= request.window_start]
    if request.window_end:
        dates = [d for d in dates if d <= request.window_end]
    return dates


def _candidate_starts(date: str, request: ScheduleRequest) -> list[int]:
    if request.type == "quiz":
        return [QUIZ_WINDOW[0]] if is_weekday(date) else []
    return list(EXAM_START_TIMES)


def _find_session_end(state: CalendarState, course: str, session: int) -> Optional[tuple[str, int]]:
    latest: Optional[tuple[str, int]] = None
    for date in sorted(state.dates.keys()):
        for entry in state.dates[date].entries:
            if entry.entry_kind != EntryKind.class_:
                continue
            if entry.course_guess != course:
                continue
            if entry.end is None or session not in entry.session_numbers:
                continue
            latest = (date, entry.end)
    return latest


def _check_h5(state: CalendarState, request: ScheduleRequest, date: str, start: int) -> str:
    """Returns "ok", "blocked", or "unknown" (SPEC H5)."""
    if request.after_session is None:
        return "ok"
    found = _find_session_end(state, request.course, request.after_session)
    if found is None:
        return "unknown"
    session_date, session_end = found
    if date < session_date:
        return "blocked"
    if date == session_date and start < session_end:
        return "blocked"
    return "ok"


def _check_hard_rules(
    state: CalendarState,
    personas: list[Persona],
    request: ScheduleRequest,
    date: str,
    start: int,
    end: int,
) -> Optional[str]:
    """Returns None if feasible, "H5_UNKNOWN" if only H5 is unverifiable
    (not a block, per SPEC), or a non-empty machine-readable block reason."""
    day = state.dates.get(date)
    if day is not None and day.holiday:
        return f"H1: {date} is a declared holiday"

    if request.type == "quiz":
        if not is_weekday(date):
            return "H2: quizzes are only allowed on weekdays"
        if start != QUIZ_WINDOW[0] or end > QUIZ_WINDOW[1]:
            return "H2: quiz must start at 08:15 and finish by 08:50"
    else:
        if start not in EXAM_START_TIMES:
            return "H3: exam start must be 08:15 or 09:00"

    candidate = Assessment(date=date, start=start, end=end, type=request.type, scope=request.scope, name=request.name)
    audience = assessment_audience(candidate, personas)

    for persona in audience:
        for t_start, t_end in teaching_intervals(state, persona, date):
            if overlaps((start, end), (t_start, t_end)):
                return f"H4: overlaps a teaching session for division {persona.division} / minor {persona.minor}"

    audience_set = set(audience)
    for other in existing_assessments(state):
        if other.date != date or not overlaps((start, end), (other.start, other.end)):
            continue
        if audience_set.intersection(assessment_audience(other, audience)):
            return f"H4: overlaps existing assessment '{other.name}'"

    h5 = _check_h5(state, request, date, start)
    if h5 == "blocked":
        return f"H5: begins before session {request.after_session} of {request.course} ends"
    if h5 == "unknown":
        return "H5_UNKNOWN"

    return None


def _s1_violation(state: CalendarState, audience: list[Persona], date: str) -> bool:
    return any(a.date == date and assessment_audience(a, audience) for a in existing_assessments(state))


def _score_candidate(
    state: CalendarState, audience: list[Persona], candidate: Assessment, s1_hit: bool
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    if s1_hit:
        score += S1_WEIGHT
        reasons.append("S1: a persona in the audience already has an assessment that day (overridden)")

    week = iso_week(candidate.date)
    s2_hits = 0
    s3_hits = 0
    peak_load = 0.0

    for persona in audience:
        week_count = sum(
            1
            for a in existing_assessments(state)
            if iso_week(a.date) == week and assessment_audience(a, [persona])
        )
        if week_count + 1 > S2_MAX_PER_WEEK:
            s2_hits += 1

        if any(
            a.type == "endterm" and next_day(a.date) == candidate.date and assessment_audience(a, [persona])
            for a in existing_assessments(state)
        ):
            s3_hits += 1

        new_load = weekly_load(state, persona, week) + WEIGHTS.get(candidate.type, 0)
        peak_load = max(peak_load, new_load)

    if s2_hits:
        score += S2_WEIGHT * s2_hits
        reasons.append(f"S2: would push {s2_hits} persona(s) over {S2_MAX_PER_WEEK} assessments this ISO week")
    if s3_hits:
        score += S3_WEIGHT * s3_hits
        reasons.append(f"S3: falls the day after an end term for {s3_hits} persona(s)")

    score += peak_load
    reasons.append(f"S4: worst affected persona's weekly weighted load would be {peak_load:g}")

    return score, reasons


def propose_slots(state: CalendarState, request: ScheduleRequest) -> Proposal:
    personas = build_personas(state.cohorts)

    blocked: list[BlockedDate] = []
    feasible: list[tuple[str, int, int]] = []
    h5_unknown = False

    for date in _target_dates(state, request):
        for start in _candidate_starts(date, request):
            end = start + request.duration_minutes
            reason = _check_hard_rules(state, personas, request, date, start, end)
            if reason is None:
                feasible.append((date, start, end))
            elif reason == "H5_UNKNOWN":
                h5_unknown = True
                feasible.append((date, start, end))
            else:
                blocked.append(BlockedDate(date=date, start=start, end=end, reason=reason))

    scored: list[Candidate] = []
    for date, start, end in feasible:
        candidate = Assessment(date=date, start=start, end=end, type=request.type, scope=request.scope, name=request.name)
        audience = assessment_audience(candidate, personas)

        s1_hit = _s1_violation(state, audience, date)
        if s1_hit and "S1" not in request.overrides:
            blocked.append(
                BlockedDate(
                    date=date,
                    start=start,
                    end=end,
                    reason="S1: a persona in the audience already has an assessment that day",
                )
            )
            continue

        score, reasons = _score_candidate(state, audience, candidate, s1_hit)
        scored.append(Candidate(date=date, start=start, end=end, score=score, reasons=reasons))

    scored.sort(key=lambda c: c.score)
    top = scored[:3]

    warnings: list[str] = []
    questions: list[ConfirmationQuestion] = []
    if h5_unknown:
        message = (
            f"Could not verify when session {request.after_session} of {request.course} ends "
            f"from the uploaded timetables; the after-session timing constraint was not enforced."
        )
        warnings.append(message)
        questions.append(ConfirmationQuestion(kind="h5_unknown", question=message, context=request.course))

    proposal = Proposal(
        id=str(uuid.uuid4()),
        request=request,
        candidates=top,
        blocked=blocked,
        warnings=warnings,
        questions=questions,
    )
    _PROPOSALS[proposal.id] = proposal
    return proposal


def apply_holiday(state: CalendarState, date: str) -> Impact:
    day = state.dates.get(date)
    affected_entries = [
        entry
        for entry in (day.entries if day is not None else [])
        if entry.entry_kind in (EntryKind.banner, EntryKind.existing_assessment)
        and entry.start is not None
        and entry.end is not None
    ]

    hypothetical = state.model_copy(deep=True)
    if date in hypothetical.dates:
        hypothetical.dates[date].holiday = True
    else:
        hypothetical.dates[date] = TimetableDay(date=date, holiday=True)

    affected: list[AffectedAssessment] = []
    for entry in affected_entries:
        scope = entry.cohort_id if entry.cohort_kind == CohortKind.minor else "core"
        request = ScheduleRequest(
            course=entry.course_guess or entry.raw_label,
            name=entry.raw_label,
            type=infer_assessment_type(entry.raw_label),
            scope=scope,
            duration_minutes=entry.end - entry.start,
        )
        reproposal = propose_slots(hypothetical, request)
        affected.append(
            AffectedAssessment(
                raw_label=entry.raw_label,
                date=date,
                start=entry.start,
                end=entry.end,
                reproposal=reproposal,
            )
        )

    return Impact(date=date, affected=affected)


def approve(state: CalendarState, proposal_id: str, candidate_index: int) -> CalendarState:
    proposal = _PROPOSALS.get(proposal_id)
    if proposal is None:
        raise KeyError(f"unknown proposal id: {proposal_id}")
    if not (0 <= candidate_index < len(proposal.candidates)):
        raise IndexError(f"candidate index {candidate_index} out of range for proposal {proposal_id}")

    candidate = proposal.candidates[candidate_index]
    request = proposal.request

    day = state.dates.get(candidate.date)
    if day is None:
        day = TimetableDay(date=candidate.date)
        state.dates[candidate.date] = day

    is_core = request.scope == "core"
    entry = TimetableEntry(
        raw_label=request.name,
        row_label=request.scope,
        cohort_kind=CohortKind.banner if is_core else CohortKind.minor,
        cohort_id=None if is_core else request.scope,
        course_guess=request.course,
        session_numbers=[],
        start=candidate.start,
        end=candidate.end,
        entry_kind=EntryKind.existing_assessment,
        confidence=1.0,
    )
    day.entries.append(entry)
    return state
