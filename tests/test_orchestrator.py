"""Phase 3 orchestrator tests. The LLM is mocked throughout via a FakeLLM
stub passed as `llm_client`, so these run with no LLM_API_KEY and make no
network calls.

Manual end-to-end curl sequence (needs a real LLM_API_KEY and a running
server: `LLM_API_KEY=... ./.venv/Scripts/python -m uvicorn app.main:app
--reload`, run from the repo root, with paths below also relative to it):

    # 1. Upload the course outline (defines ABA's evaluations)
    curl -s -F "kind=course_outline" \\
      -F "file=@tests/fixtures/aba_course_outline.pdf;type=application/pdf" \\
      http://127.0.0.1:8000/api/upload

    # 2. Upload the timetable (raises an EAB vs ABA identity question)
    curl -s -F "kind=timetable" \\
      -F "file=@tests/fixtures/timetable_week13.pdf;type=application/pdf" \\
      http://127.0.0.1:8000/api/upload

    # 3. Confirm EAB is the same course as ABA
    curl -s -X POST -H "Content-Type: application/json" \\
      -d '{"context": "EAB", "resolution": "ABA"}' \\
      http://127.0.0.1:8000/api/confirm

    # 4. Ask for an end term slot (missing duration -> parks and asks)
    curl -s -X POST -H "Content-Type: application/json" \\
      -d '{"message": "Please schedule the ABA end term exam"}' \\
      http://127.0.0.1:8000/api/chat

    # 5. Answer with the duration -> completes the request, narrated proposal
    curl -s -X POST -H "Content-Type: application/json" \\
      -d '{"message": "90 minutes"}' \\
      http://127.0.0.1:8000/api/chat

    # 6. Approve the top-ranked candidate (1-based in chat)
    curl -s -X POST -H "Content-Type: application/json" \\
      -d '{"message": "approve candidate one"}' \\
      http://127.0.0.1:8000/api/chat

    # 7. Inspect the resulting week and the full session state
    curl -s "http://127.0.0.1:8000/api/grid?week_start=2026-08-24"
    curl -s http://127.0.0.1:8000/api/state

All calls above omit X-Session-Id and so operate on the default "office"
session.
"""

from __future__ import annotations

from app import engine
from app.orchestrator import IntentFields, IntentResult, handle_message
from app.schemas import (
    CalendarState,
    CohortKind,
    CohortRegistry,
    ConfirmationQuestion,
    CourseOutline,
    EntryKind,
    Evaluation,
    EvaluationType,
    TimetableDay,
    TimetableEntry,
)
from app.session import PendingRequest, SessionState

WEEKDAYS = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]
SATURDAY = "2026-08-29"
SUNDAY = "2026-08-30"


class FakeLLM:
    """Stand-in for LLMClient. json_responses is consumed in order by
    complete_json (one per classify call); complete_text (narration) always
    returns the next queued text response, repeating the last one forever."""

    def __init__(self, json_responses=None, text_responses=None):
        self._json_responses = list(json_responses or [])
        self._text_responses = list(text_responses or ["(narrated reply)"])
        self.json_calls = 0
        self.text_calls = 0

    def complete_json(self, system_prompt, user_content, schema_model, model, **kwargs):
        self.json_calls += 1
        return self._json_responses.pop(0)

    def complete_text(self, system_prompt, user_content, model):
        self.text_calls += 1
        if len(self._text_responses) > 1:
            return self._text_responses.pop(0)
        return self._text_responses[0]


def _base_state() -> CalendarState:
    state = CalendarState(cohorts=CohortRegistry(divisions=["A", "B", "C"], minors=["Finance"]))
    state.courses.append(
        CourseOutline(
            name="Applied Business Analytics",
            code="ABA",
            evaluations=[
                Evaluation(name="ABA End Term", type=EvaluationType.endterm, timing_note="After Session 20"),
            ],
        )
    )

    for date in WEEKDAYS:
        entries = [
            TimetableEntry(
                raw_label=f"ABA-{d}",
                row_label="Core",
                cohort_kind=CohortKind.division,
                cohort_id=d,
                course_guess="ABA",
                session_numbers=[1],
                start=540,  # 09:00
                end=610,  # 10:10
                entry_kind=EntryKind.class_,
            )
            for d in ["A", "B", "C"]
        ]
        state.dates[date] = TimetableDay(date=date, entries=entries)

    state.dates[SATURDAY] = TimetableDay(date=SATURDAY)
    state.dates[SUNDAY] = TimetableDay(date=SUNDAY)
    return state


def test_schedule_request_parks_on_missing_duration_then_completes():
    session = SessionState(calendar=_base_state())
    llm = FakeLLM(
        json_responses=[
            IntentResult(
                action="schedule_request",
                fields=IntentFields(course="ABA", name="ABA End Term", type="endterm"),
                missing_fields=["duration_minutes"],
            )
        ]
    )

    first = handle_message(session, "Please schedule the ABA end term exam", llm_client=llm)
    assert first.action == "schedule_request"
    assert first.proposal is None
    assert "duration" in first.reply.lower()
    assert first.awaiting == ["duration_minutes"]
    assert session.pending_request is not None
    assert session.pending_request.missing_fields == ["duration_minutes"]
    assert llm.json_calls == 1  # the classify call; no second call yet

    second = handle_message(session, "90 minutes", llm_client=llm)
    assert llm.json_calls == 1  # resuming a pending request never re-classifies
    assert session.pending_request is None
    assert second.proposal is not None
    assert second.proposal.request.duration_minutes == 90
    assert second.proposal.request.scope == "core"  # inferred: ABA has no minor-scoped entries
    assert second.proposal.candidates
    assert second.proposal.warnings  # after_session=20 inferred from timing_note, but unverifiable here


def test_holiday_declared_routes_to_apply_holiday_and_returns_impact():
    state = _base_state()
    state.dates["2026-08-28"].entries.append(
        TimetableEntry(
            raw_label="ABA End Term",
            row_label="Core",
            cohort_kind=CohortKind.banner,
            cohort_id=None,
            course_guess="ABA",
            session_numbers=[],
            start=540,
            end=630,
            entry_kind=EntryKind.banner,
        )
    )
    session = SessionState(calendar=state)
    llm = FakeLLM(
        json_responses=[
            IntentResult(action="holiday_declared", fields=IntentFields(date="Friday"), missing_fields=[]),
        ]
    )

    reply = handle_message(session, "Friday has been declared a holiday", llm_client=llm)

    assert reply.action == "holiday_declared"
    assert reply.impact is not None
    assert reply.impact.date == "2026-08-28"
    labels = {a.raw_label for a in reply.impact.affected}
    assert "ABA End Term" in labels
    # unlike engine.apply_holiday alone, the orchestrator commits the
    # declared fact onto the real session calendar
    assert session.calendar.dates["2026-08-28"].holiday is True


def test_adjust_rule_changes_max_per_week_and_next_proposal_reflects_it():
    original_cap = engine.S2_MAX_PER_WEEK
    try:
        state = _base_state()
        for d in ["2026-08-24", "2026-08-25", "2026-08-26"]:
            state.dates[d].entries.append(
                TimetableEntry(
                    raw_label="Finance Quiz",
                    row_label="Finance",
                    cohort_kind=CohortKind.minor,
                    cohort_id="Finance",
                    course_guess="FIN101",
                    session_numbers=[],
                    start=495,  # 08:15
                    end=530,  # 08:50
                    entry_kind=EntryKind.existing_assessment,
                )
            )
        session = SessionState(calendar=state)
        request = engine.ScheduleRequest(
            course="FIN101",
            name="Finance Midterm",
            type="midterm",
            scope="Finance",
            duration_minutes=45,
            window_start="2026-08-27",
            window_end="2026-08-27",
        )

        before = engine.propose_slots(session.calendar, request)
        assert before.candidates
        assert any(r.startswith("S2") for r in before.candidates[0].reasons)

        llm = FakeLLM(
            json_responses=[
                IntentResult(
                    action="adjust_rule",
                    fields=IntentFields(rule="max_per_week", value=4),
                    missing_fields=[],
                ),
            ]
        )
        reply = handle_message(session, "We can allow four assessments per week now", llm_client=llm)
        assert engine.S2_MAX_PER_WEEK == 4
        assert "4" in reply.reply

        after = engine.propose_slots(session.calendar, request)
        assert after.candidates
        assert not any(r.startswith("S2") for r in after.candidates[0].reasons)
        assert after.candidates[0].score < before.candidates[0].score
    finally:
        engine.S2_MAX_PER_WEEK = original_cap


def test_restore_round_trips_serialize_output():
    session = SessionState(calendar=_base_state(), session_id="office")
    session.confirmation_queue.append(
        ConfirmationQuestion(kind="identity", question="Is EAB the same as ABA?", context="EAB")
    )
    session.pending_request = PendingRequest(fields={"course": "ABA"}, missing_fields=["duration_minutes"])
    proposal = engine.propose_slots(
        session.calendar,
        engine.ScheduleRequest(course="ABA", name="Quiz 1", type="quiz", scope="core", duration_minutes=None),
    )
    session.proposal_history.append(proposal)
    session.bump()
    session.bump()

    blob = session.serialize()
    restored = SessionState.restore(blob)

    assert restored == session
    assert restored.state_version == session.state_version
    assert restored.serialize() == blob
    assert engine._PROPOSALS.get(proposal.id) is not None
