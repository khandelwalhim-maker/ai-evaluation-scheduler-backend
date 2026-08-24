from app.calendar_summary import build_calendar_markdown
from app.schemas import CalendarState, EntryKind, TimetableDay, TimetableEntry


def test_empty_calendar_returns_sentinel():
    state = CalendarState()
    assert build_calendar_markdown(state) == "No timetable data has been uploaded yet."


def test_populated_calendar_includes_holiday_and_banner():
    state = CalendarState()
    state.dates["2026-08-25"] = TimetableDay(
        date="2026-08-25",
        holiday=True,
        entries=[
            TimetableEntry(
                raw_label="ABA End Term",
                row_label="IMA",
                cohort_kind="minor",
                cohort_id="IMA",
                course_guess="ABA",
                entry_kind=EntryKind.banner,
                start=540,
                end=610,
                confidence=0.6,
            )
        ],
    )

    markdown = build_calendar_markdown(state)

    assert "2026-08-25" in markdown
    assert "HOLIDAY" in markdown
    assert "ABA" in markdown
