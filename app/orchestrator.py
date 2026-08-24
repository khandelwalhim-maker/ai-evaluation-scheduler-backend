from __future__ import annotations

import json
import re
from datetime import date as _date
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from app import config, engine
from app.calendar_summary import build_calendar_markdown
from app.llm import LLMClient
from app.schemas import (
    CalendarState,
    CohortKind,
    ConfirmationQuestion,
    TimetableDay,
)
from app.session import PendingRequest, SessionState

_PROMPTS_DIR = Path(__file__).parent / "prompts"

_CANCEL_WORDS = {"cancel", "never mind", "nevermind", "forget it", "stop", "nvm"}

_WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(hours?|hrs?)\b|(\d+(?:\.\d+)?)\s*(minutes?|mins?)\b",
    re.IGNORECASE,
)

_AFTER_SESSION_RE = re.compile(r"after\s+session\s+(\d+)", re.IGNORECASE)


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


# --- intent classification schema -------------------------------------------------
# intent.txt is a single prompt covering every action; the fields below are a
# superset across all of them so the LLM gets one concrete, typed JSON schema
# (mirroring how extract_outline.txt/extract_timetable.txt already pair a
# prose prompt with a Pydantic response schema) instead of a bare dict.

IntentAction = Literal[
    "schedule_request",
    "holiday_declared",
    "whatif",
    "approve_candidate",
    "adjust_rule",
    "answer_confirmation",
    "question",
    "smalltalk",
]


class IntentFields(BaseModel):
    course: Optional[str] = Field(default=None, description="course name or code, for schedule_request")
    name: Optional[str] = Field(default=None, description="assessment name, for schedule_request")
    type: Optional[Literal["quiz", "midterm", "endterm"]] = Field(
        default=None, description="assessment type, for schedule_request"
    )
    duration_minutes: Optional[int] = Field(
        default=None, description="duration in minutes, only if the user explicitly stated one"
    )
    scope: Optional[str] = Field(
        default=None, description="'core' or a minor name, only if the user explicitly stated one"
    )
    after_session: Optional[int] = Field(
        default=None, description="session number the assessment must follow, only if explicitly stated"
    )
    window_start: Optional[str] = Field(default=None, description="earliest ISO date to consider, if a window was hinted")
    window_end: Optional[str] = Field(default=None, description="latest ISO date to consider, if a window was hinted")
    date: Optional[str] = Field(
        default=None,
        description=(
            "for holiday_declared or whatif: the date or weekday name exactly as the user wrote it "
            "(for example 'Friday' or '2026-08-28'); never compute or resolve the actual calendar date"
        ),
    )
    candidate_index: Optional[int] = Field(
        default=None, description="1-based rank of the candidate the user wants to approve (first/1 = top choice)"
    )
    proposal_id: Optional[str] = Field(default=None, description="proposal id, only if the user stated one explicitly")
    rule: Optional[str] = Field(default=None, description="for adjust_rule: which soft rule to change")
    value: Optional[float] = Field(default=None, description="for adjust_rule: the new value")
    question_context: Optional[str] = Field(
        default=None, description="for answer_confirmation: the context of the open question being answered"
    )
    answer: Optional[str] = Field(default=None, description="for answer_confirmation: the user's answer")


class IntentResult(BaseModel):
    action: IntentAction
    fields: IntentFields = Field(default_factory=IntentFields)
    missing_fields: list[str] = Field(default_factory=list)


class ChatReply(BaseModel):
    action: str
    reply: str
    proposal: Optional[engine.Proposal] = None
    impact: Optional[engine.Impact] = None
    questions: list[ConfirmationQuestion] = Field(default_factory=list)
    awaiting: list[str] = Field(default_factory=list)


# --- rule adjustment ----------------------------------------------------------------
# engine.py's soft-rule weights are module-level constants read directly
# inside its functions, not parameters, so the only way to change them at
# runtime without editing engine.py is to mutate the module attribute. This
# is process-wide (affects every session), matching the same single-process
# scope already accepted for engine._PROPOSALS (docs/HANDOFF.md decision #4).
_ADJUSTABLE_RULES: dict[str, str] = {
    "s1_weight": "S1_WEIGHT",
    "s2_weight": "S2_WEIGHT",
    "s3_weight": "S3_WEIGHT",
    "s2_max_per_week": "S2_MAX_PER_WEEK",
    "max_per_week": "S2_MAX_PER_WEEK",
    "assessments_per_week": "S2_MAX_PER_WEEK",
    "s2": "S2_MAX_PER_WEEK",
}


def _build_context(session: SessionState) -> dict:
    return {
        "known_courses": [
            {"name": c.name, "code": c.code} for c in session.calendar.courses
        ],
        "pending_questions": [
            {"kind": q.kind, "question": q.question, "context": q.context}
            for q in session.confirmation_queue
        ],
        "cohort_registry": {
            "divisions": session.calendar.cohorts.divisions,
            "minors": session.calendar.cohorts.minors,
        },
        "calendar_summary": build_calendar_markdown(session.calendar),
    }


def _parse_duration_minutes(text: str) -> Optional[int]:
    total = 0.0
    found = False
    for hour_val, _hour_unit, min_val, _min_unit in _DURATION_RE.findall(text):
        if hour_val:
            total += float(hour_val) * 60
            found = True
        if min_val:
            total += float(min_val)
            found = True
    if found:
        return int(round(total))

    bare = re.fullmatch(r"\s*(\d+)\s*", text)
    if bare:
        return int(bare.group(1))
    return None


def _duration_question(fields: dict) -> str:
    name = fields.get("name") or fields.get("course") or "this assessment"
    return (
        f"How many minutes should {name} run for? Duration is required and is never assumed; "
        f"please give a number of minutes."
    )


def _infer_scope(state: CalendarState, course: str) -> str:
    if not course:
        return "core"
    for day in state.dates.values():
        for entry in day.entries:
            if entry.course_guess == course and entry.cohort_kind == CohortKind.minor and entry.cohort_id:
                return entry.cohort_id
    return "core"


def _infer_after_session(state: CalendarState, course: str, name: str, assessment_type: str) -> Optional[int]:
    for outline in state.courses:
        if course and outline.code != course and outline.name != course:
            continue
        for ev in outline.evaluations:
            if ev.type.value != assessment_type:
                continue
            if name and ev.name and ev.name.strip().lower() != name.strip().lower():
                continue
            if ev.timing_note:
                match = _AFTER_SESSION_RE.search(ev.timing_note)
                if match:
                    return int(match.group(1))
    return None


def _try_iso_date(text: str) -> Optional[str]:
    try:
        return _date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _resolve_date(state: CalendarState, raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    text = raw.strip()

    iso = _try_iso_date(text)
    if iso:
        return iso

    weekday = _WEEKDAY_NAMES.get(text.lower())
    if weekday is None:
        return None
    for date_str in sorted(state.dates.keys()):
        if _date.fromisoformat(date_str).weekday() == weekday:
            return date_str
    return None


def _friendly_validation_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    return f"That request is not valid: {first['msg']}."


def _narrate(llm: LLMClient, payload: dict) -> str:
    prompt = _load_prompt("narrate.txt")
    return llm.complete_text(prompt, json.dumps(payload), config.MODEL_NARRATE)


def _build_schedule_request(session: SessionState, fields: IntentFields) -> engine.ScheduleRequest:
    course = (fields.course or "").strip()
    assessment_type = fields.type or "endterm"
    name = fields.name or (f"{course} {assessment_type}".strip() or assessment_type)
    scope = fields.scope or _infer_scope(session.calendar, course)
    after_session = fields.after_session
    if after_session is None:
        after_session = _infer_after_session(session.calendar, course, name, assessment_type)

    return engine.ScheduleRequest(
        course=course,
        name=name,
        type=assessment_type,
        scope=scope,
        duration_minutes=fields.duration_minutes,
        after_session=after_session,
        window_start=fields.window_start,
        window_end=fields.window_end,
    )


def _run_schedule_request(session: SessionState, request: engine.ScheduleRequest, llm: LLMClient) -> ChatReply:
    proposal = engine.propose_slots(session.calendar, request)
    session.proposal_history.append(proposal)
    session.bump()
    reply_text = _narrate(llm, proposal.model_dump(mode="json"))
    return ChatReply(action="schedule_request", reply=reply_text, proposal=proposal, questions=proposal.questions)


def _build_and_run(session: SessionState, fields: IntentFields, llm: LLMClient) -> ChatReply:
    try:
        request = _build_schedule_request(session, fields)
    except ValidationError as exc:
        return ChatReply(action="schedule_request", reply=_friendly_validation_error(exc))
    return _run_schedule_request(session, request, llm)


def _handle_schedule_request(session: SessionState, result: IntentResult, llm: LLMClient) -> ChatReply:
    if "duration_minutes" in result.missing_fields:
        pending_fields = result.fields.model_dump(exclude_none=True)
        session.pending_request = PendingRequest(fields=pending_fields, missing_fields=["duration_minutes"])
        session.bump()
        return ChatReply(
            action="schedule_request",
            reply=_duration_question(pending_fields),
            awaiting=["duration_minutes"],
        )

    return _build_and_run(session, result.fields, llm)


def _resume_pending(session: SessionState, text: str, llm: LLMClient) -> ChatReply:
    pending = session.pending_request
    assert pending is not None

    if "duration_minutes" in pending.missing_fields:
        minutes = _parse_duration_minutes(text)
        if minutes is None:
            if text.strip().lower() in _CANCEL_WORDS:
                session.pending_request = None
                session.bump()
                return ChatReply(action="cancelled", reply="Okay, that request has been cancelled.")
            return ChatReply(
                action="schedule_request",
                reply=_duration_question(pending.fields),
                awaiting=list(pending.missing_fields),
            )
        pending.fields["duration_minutes"] = minutes
        pending.missing_fields = [f for f in pending.missing_fields if f != "duration_minutes"]

    if pending.missing_fields:
        session.bump()
        return ChatReply(
            action="schedule_request",
            reply="I still need a bit more information to schedule this.",
            awaiting=list(pending.missing_fields),
        )

    fields = IntentFields.model_validate(pending.fields)
    session.pending_request = None
    return _build_and_run(session, fields, llm)


def _handle_holiday(session: SessionState, result: IntentResult, llm: LLMClient) -> ChatReply:
    resolved = _resolve_date(session.calendar, result.fields.date)
    if resolved is None:
        return ChatReply(
            action="holiday_declared",
            reply="Which date should be marked a holiday? Please give an exact date or a weekday present in the uploaded timetable.",
        )

    impact = engine.apply_holiday(session.calendar, resolved)

    day = session.calendar.dates.get(resolved)
    if day is None:
        day = TimetableDay(date=resolved)
        session.calendar.dates[resolved] = day
    day.holiday = True
    session.proposal_history.extend(a.reproposal for a in impact.affected)
    session.bump()

    reply_text = _narrate(llm, impact.model_dump(mode="json"))
    return ChatReply(action="holiday_declared", reply=reply_text, impact=impact)


def _handle_whatif(session: SessionState, result: IntentResult, llm: LLMClient) -> ChatReply:
    resolved = _resolve_date(session.calendar, result.fields.date)
    if resolved is None:
        return ChatReply(action="whatif", reply="Which date should I evaluate as a hypothetical holiday?")

    # Deliberately does not commit anything to session.calendar -- apply_holiday
    # already computes on an internal copy, so a whatif is just that same
    # computation without the follow-up mutation _handle_holiday performs.
    impact = engine.apply_holiday(session.calendar, resolved)
    session.bump()
    reply_text = _narrate(llm, impact.model_dump(mode="json"))
    return ChatReply(action="whatif", reply=reply_text, impact=impact)


def _handle_approve(session: SessionState, result: IntentResult) -> ChatReply:
    index = result.fields.candidate_index
    if index is None:
        return ChatReply(action="approve_candidate", reply="Which candidate number would you like to approve?")

    proposal_id = result.fields.proposal_id
    if not proposal_id:
        if not session.proposal_history:
            return ChatReply(action="approve_candidate", reply="There is no proposal to approve yet.")
        proposal_id = session.proposal_history[-1].id

    try:
        engine.approve(session.calendar, proposal_id, index - 1)
    except (KeyError, IndexError) as exc:
        return ChatReply(action="approve_candidate", reply=f"Could not approve that candidate: {exc}")

    session.bump()
    return ChatReply(action="approve_candidate", reply=f"Confirmed: candidate {index} has been added to the calendar.")


def _handle_adjust_rule(session: SessionState, result: IntentResult) -> ChatReply:
    rule_key = (result.fields.rule or "").strip().lower().replace(" ", "_")
    attr = _ADJUSTABLE_RULES.get(rule_key)
    value = result.fields.value

    if attr is None or value is None:
        return ChatReply(
            action="adjust_rule",
            reply=(
                "Which rule should be adjusted, and to what value? For example, "
                "'allow four assessments per week' or 'change the S2 weight to 8'."
            ),
        )

    old_value = getattr(engine, attr)
    new_value = int(value) if attr == "S2_MAX_PER_WEEK" else float(value)
    setattr(engine, attr, new_value)
    session.bump()

    return ChatReply(
        action="adjust_rule",
        reply=f"Confirmed: {attr} changed from {old_value:g} to {new_value:g}. This applies to proposals from now on.",
    )


def resolve_confirmation(session: SessionState, context: str, resolution: str) -> str:
    """Resolve a pending ConfirmationQuestion by its `context` key (matches
    ConfirmationQuestion.context, for example a raw timetable code like
    'EAB'). For identity questions this reconciles every TimetableEntry
    sharing that raw label or course_guess onto the confirmed canonical
    course code. Shared by the /api/confirm route and the answer_confirmation
    chat action. Returns a short human-readable description of what changed.
    """
    matches = [q for q in session.confirmation_queue if q.context == context]
    if not matches:
        return f"No open question found for '{context}'."

    session.confirmation_queue = [q for q in session.confirmation_queue if q.context != context]

    changed = 0
    if any(q.kind == "identity" for q in matches):
        for day in session.calendar.dates.values():
            for entry in day.entries:
                if entry.course_guess == context or entry.raw_label == context:
                    entry.course_guess = resolution
                    entry.confidence = 1.0
                    changed += 1

    session.bump()
    if changed:
        return f"Confirmed: '{context}' resolves to '{resolution}' ({changed} entries updated)."
    return f"Confirmed: '{context}' resolves to '{resolution}'."


def _handle_answer_confirmation(session: SessionState, result: IntentResult) -> ChatReply:
    context = result.fields.question_context
    answer = result.fields.answer
    if not context or not answer:
        return ChatReply(
            action="answer_confirmation",
            reply="Which open question are you answering, and with what value?",
        )
    message = resolve_confirmation(session, context, answer)
    return ChatReply(action="answer_confirmation", reply=message)


def _dispatch(session: SessionState, text: str, result: IntentResult, llm: LLMClient) -> ChatReply:
    action = result.action
    if action == "schedule_request":
        return _handle_schedule_request(session, result, llm)
    if action == "holiday_declared":
        return _handle_holiday(session, result, llm)
    if action == "whatif":
        return _handle_whatif(session, result, llm)
    if action == "approve_candidate":
        return _handle_approve(session, result)
    if action == "adjust_rule":
        return _handle_adjust_rule(session, result)
    if action == "answer_confirmation":
        return _handle_answer_confirmation(session, result)
    if action == "question":
        return ChatReply(
            action=action,
            reply="I can schedule assessments, apply holidays, adjust soft rules, or answer open confirmation questions. What would you like to do?",
        )
    return ChatReply(action=action, reply="Happy to help with scheduling whenever you're ready.")


def handle_message(session: SessionState, text: str, llm_client: Optional[LLMClient] = None) -> ChatReply:
    llm = llm_client or LLMClient()

    if session.pending_request is not None:
        return _resume_pending(session, text, llm)

    prompt = _load_prompt("intent.txt")
    user_content = json.dumps({"message": text, "context": _build_context(session)})
    result = llm.complete_json(prompt, user_content, IntentResult, config.MODEL_PARSE)
    return _dispatch(session, text, result, llm)
