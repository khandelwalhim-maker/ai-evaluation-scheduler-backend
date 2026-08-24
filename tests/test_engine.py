from __future__ import annotations

from app.engine import ScheduleRequest, apply_holiday, approve, propose_slots
from app.schemas import (
    CalendarState,
    CohortKind,
    CohortRegistry,
    EntryKind,
    TimetableDay,
    TimetableEntry,
)

DIVISIONS = ["A", "B", "C"]
WEEKDAYS = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]
SATURDAY = "2026-08-29"
SUNDAY = "2026-08-30"
ALL_DATES = WEEKDAYS + [SATURDAY, SUNDAY]


def _core_entry(division: str) -> TimetableEntry:
    return TimetableEntry(
        raw_label=f"ABA-{division}",
        row_label="Core",
        cohort_kind=CohortKind.division,
        cohort_id=division,
        course_guess="ABA",
        session_numbers=[1],
        start=540,  # 09:00
        end=610,  # 10:10
        entry_kind=EntryKind.class_,
    )


def _minor_class(minor: str, start: int, end: int) -> TimetableEntry:
    return TimetableEntry(
        raw_label=f"{minor} elective",
        row_label=minor,
        cohort_kind=CohortKind.minor,
        cohort_id=minor,
        course_guess="FIN101" if minor == "Finance" else "MKT101",
        session_numbers=[1],
        start=start,
        end=end,
        entry_kind=EntryKind.class_,
    )


def _banner(scope_minor: str | None, start: int, end: int, label: str, course: str) -> TimetableEntry:
    return TimetableEntry(
        raw_label=label,
        row_label=scope_minor or "Core",
        cohort_kind=CohortKind.minor if scope_minor else CohortKind.banner,
        cohort_id=scope_minor,
        course_guess=course,
        session_numbers=[],
        start=start,
        end=end,
        entry_kind=EntryKind.banner,
    )


def build_fixture_state() -> CalendarState:
    """A hand-built ground-truth calendar matching the real Week 13 shape
    (synchronized core sessions for A/B/C, a Finance minor row, existing
    end-term banners on Fri/Sat/Sun) so this phase does not depend on LLM
    parsing.
    """
    state = CalendarState(cohorts=CohortRegistry(divisions=DIVISIONS, minors=["Finance", "Marketing"]))

    for date in WEEKDAYS:
        entries = [_core_entry(d) for d in DIVISIONS]
        if date == "2026-08-24":
            entries.append(_minor_class("Finance", 640, 710))  # Mon 10:40-11:50
        state.dates[date] = TimetableDay(date=date, entries=entries)

    # Saturday: no core class, so a core-scoped candidate there only ever
    # conflicts via the minor session below (isolates H4-vs-minor-teaching).
    state.dates[SATURDAY] = TimetableDay(date=SATURDAY, entries=[_minor_class("Finance", 540, 610)])
    state.dates[SUNDAY] = TimetableDay(date=SUNDAY, entries=[])

    # Existing end-term banners on Friday, Saturday, and Sunday. Placed in
    # the afternoon on Sat/Sun so they don't collide with the AM slots the
    # tests below probe.
    state.dates["2026-08-28"].entries.append(_banner("Finance", 540, 660, "Finance End Term", "FIN101"))
    state.dates[SATURDAY].entries.append(_banner(None, 870, 990, "SDBKAM END TERM EXAM", "SDBKAM"))
    state.dates[SUNDAY].entries.append(_banner(None, 870, 990, "SM END TERM EXAM", "SM"))

    return state


def test_core_endterm_feasible_sunday_infeasible_weekdays():
    state = build_fixture_state()
    base = dict(course="ABA", name="ABA End Term", type="endterm", scope="core", duration_minutes=120)

    # Sunday already carries a same-day core banner (SM END TERM EXAM), so a
    # second core assessment that day is an S1 (soft, one-per-persona-per-day)
    # matter, not a hard-rule one; overriding S1 isolates hard feasibility.
    proposal = propose_slots(state, ScheduleRequest(**base, overrides={"S1"}))

    top_slots = {(c.date, c.start) for c in proposal.candidates}
    assert (SUNDAY, 540) in top_slots

    blocked_slots = {(b.date, b.start) for b in proposal.blocked}
    for date in WEEKDAYS:
        assert (date, 495) in blocked_slots
        assert (date, 540) in blocked_slots
    assert proposal.blocked
    for b in proposal.blocked:
        assert b.reason

    # Without the override, Sunday remains hard-feasible but S1 keeps it out
    # of the default top candidates because of the pre-existing same-day
    # banner -- a separate, overridable soft-rule concern.
    default_proposal = propose_slots(state, ScheduleRequest(**base))
    assert (SUNDAY, 540) not in {(c.date, c.start) for c in default_proposal.candidates}
    sunday_reasons = {(b.date, b.start): b.reason for b in default_proposal.blocked}
    assert sunday_reasons.get((SUNDAY, 540), "").startswith("S1")


def test_quiz_candidates_only_weekday_0815():
    state = build_fixture_state()
    request = ScheduleRequest(course="ABA", name="Quiz 1", type="quiz", scope="core", duration_minutes=35)
    proposal = propose_slots(state, request)

    all_dates_seen = {c.date for c in proposal.candidates} | {b.date for b in proposal.blocked}
    all_starts_seen = {c.start for c in proposal.candidates} | {b.start for b in proposal.blocked}

    assert all_dates_seen <= set(WEEKDAYS)
    assert SATURDAY not in all_dates_seen
    assert SUNDAY not in all_dates_seen
    assert all_starts_seen == {495}


def test_two_different_minors_may_share_a_slot():
    state = build_fixture_state()
    finance_req = ScheduleRequest(course="FIN101", name="Finance Quiz", type="quiz", scope="Finance", duration_minutes=35)
    finance_proposal = propose_slots(state, finance_req)
    approve(state, finance_proposal.id, 0)
    chosen = finance_proposal.candidates[0]

    marketing_req = ScheduleRequest(course="MKT101", name="Marketing Quiz", type="quiz", scope="Marketing", duration_minutes=35)
    marketing_proposal = propose_slots(state, marketing_req)

    marketing_blocked = {(b.date, b.start) for b in marketing_proposal.blocked}
    assert (chosen.date, chosen.start) not in marketing_blocked
    marketing_candidates = {(c.date, c.start) for c in marketing_proposal.candidates}
    assert (chosen.date, chosen.start) in marketing_candidates or (chosen.date, chosen.start) not in marketing_blocked


def test_core_assessment_cannot_overlap_minor_session():
    state = build_fixture_state()
    request = ScheduleRequest(course="ABA", name="ABA End Term", type="endterm", scope="core", duration_minutes=120)
    proposal = propose_slots(state, request)

    blocked_reasons = {(b.date, b.start): b.reason for b in proposal.blocked}
    assert (SATURDAY, 540) in blocked_reasons
    assert blocked_reasons[(SATURDAY, 540)]


def test_s1_violation_excluded_unless_overridden():
    state = build_fixture_state()
    # Friday already carries a Finance end-term (09:00-11:00). A Finance quiz
    # at 08:15-08:50 the same day doesn't overlap it in time, so only S1 (not
    # H4) is at stake. The window is pinned to Friday alone so the result
    # isn't confounded by other, unpenalized weekdays winning the top-3.
    request = ScheduleRequest(
        course="FIN101",
        name="Finance Quiz",
        type="quiz",
        scope="Finance",
        duration_minutes=35,
        window_start="2026-08-28",
        window_end="2026-08-28",
    )
    proposal = propose_slots(state, request)

    assert proposal.candidates == []
    assert proposal.blocked
    assert proposal.blocked[0].reason.startswith("S1")

    overridden = request.model_copy(update={"overrides": {"S1"}})
    proposal_override = propose_slots(state, overridden)
    assert len(proposal_override.candidates) == 1
    assert proposal_override.candidates[0].date == "2026-08-28"


def test_holiday_on_sunday_impacts_existing_assessment_with_reproposals():
    state = build_fixture_state()
    impact = apply_holiday(state, SUNDAY)

    assert impact.date == SUNDAY
    labels = {a.raw_label for a in impact.affected}
    assert "SM END TERM EXAM" in labels

    for affected in impact.affected:
        assert affected.reproposal.candidates or affected.reproposal.blocked

    # state must not be mutated until approved
    assert state.dates[SUNDAY].holiday is False
    assert any(e.raw_label == "SM END TERM EXAM" for e in state.dates[SUNDAY].entries)


def test_h5_emits_warning_question_when_session_date_not_inferable():
    state = build_fixture_state()
    request = ScheduleRequest(
        course="ABA",
        name="ABA End Term",
        type="endterm",
        scope="core",
        duration_minutes=120,
        after_session=99,  # never appears in any teaching entry
    )
    proposal = propose_slots(state, request)

    assert proposal.warnings
    assert any("99" in w for w in proposal.warnings)
    assert proposal.questions
