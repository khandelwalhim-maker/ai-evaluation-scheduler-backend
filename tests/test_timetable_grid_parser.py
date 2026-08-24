from pathlib import Path

import pytest

from app.pdf_extract import extract_grid_bands
from app.timetable_grid_parser import TimetableGridFormatError, _parse_cell, parse_timetable_grid

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
WEEK13 = str(FIXTURES_DIR / "timetable_week13.pdf")
WEEK14 = str(FIXTURES_DIR / "timetable_week14.pdf")


def _entries_by_cohort_kind(day, kind):
    return [e for e in day.entries if e.cohort_kind.value == kind]


def test_band_row_counts_lock_in_the_real_world_quirk():
    # week13 repeats all 7 minor/division rows every day; week14 only
    # prints the divisionless IMA/Consulting rows on Monday and omits them
    # entirely (not blank -- absent) on every later day. This is exactly
    # the quirk that makes row-index-based forward-fill wrong and
    # division-letter-keyed forward-fill necessary -- lock it in so a
    # future pdfplumber/library upgrade can't silently change it unnoticed.
    _, week13_bands = extract_grid_bands(WEEK13)
    _, week14_bands = extract_grid_bands(WEEK14)

    assert [len(b) for b in week13_bands] == [7, 7, 7, 7, 7, 7, 7]
    assert [len(b) for b in week14_bands] == [7, 5, 5, 5, 5, 5, 5]


def test_week13_dates_and_scope():
    result = parse_timetable_grid(WEEK13)
    dates = {day.date for day in result.days}

    assert len(result.days) == 7
    assert dates == {
        "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27",
        "2026-08-28", "2026-08-29", "2026-08-30",
    }

    for day in result.days:
        for entry in day.entries:
            if entry.cohort_kind.value == "division":
                assert entry.cohort_id in {"A", "B", "C"}
            assert "strategy" not in entry.row_label.lower()


def test_week13_banner_confidence_and_tbc():
    result = parse_timetable_grid(WEEK13)
    by_date = {d.date: d for d in result.days}

    aba = [e for e in by_date["2026-08-25"].entries if e.raw_label == "ABA End Term"]
    assert aba, "expected the ABA End Term banner on 2026-08-25"
    assert all(e.entry_kind.value == "banner" and e.confidence < 0.85 for e in aba)

    tbc = [e for e in by_date["2026-08-25"].entries if e.raw_label == "BB-MGP-tbc"]
    assert tbc, "expected the BB-MGP-tbc cell on 2026-08-25"
    assert all(e.course_guess == "BB" and e.session_numbers == [] and e.entry_kind.value == "class" for e in tbc)

    sim_lab = [e for e in by_date["2026-08-24"].entries if "SDT-SR-10" in e.raw_label]
    assert sim_lab, "expected the SDT-SR-10 sim-lab cell on 2026-08-24"
    assert all(e.course_guess == "SDT" and e.session_numbers == [10] for e in sim_lab)


def test_multi_session_cell_parsing():
    # Real in-scope (A/B/C) cells in both fixtures only ever express a
    # double slot via two adjacent time-slot cells (e.g. "OSCSD HJ (13)"
    # and "OSCSD HJ (14)" as separate cells -- see
    # test_parsing.py::test_parse_timetable). The single-cell multi-session
    # patterns ("(17) (18)", "17 & 18") only occur in this data inside the
    # out-of-scope Division D/E rows (e.g. "SLUU Guest Session 17 & 18"),
    # so they're exercised directly here rather than via fixture content.
    amp = _parse_cell("SLUU Guest Session 17 & 18")
    assert amp.session_numbers == [17, 18]

    double_paren = _parse_cell("OSCSD HJ (13) (14)")
    assert double_paren.session_numbers == [13, 14]


def test_week14_forward_fill_across_absent_rows():
    result = parse_timetable_grid(WEEK14)
    by_date = {d.date: d for d in result.days}

    # Tuesday's raw row_label cells are blank in the PDF for the A/B/C
    # rows -- the minor label must still resolve via forward-fill from
    # Monday's explicit labels.
    tuesday_minors = _entries_by_cohort_kind(by_date["2026-09-01"], "minor")
    assert any(e.cohort_id == "Mktg" for e in tuesday_minors), (
        "expected a forward-filled Mktg-cohort entry on Tuesday even though "
        "Tuesday's row never prints that label"
    )

    # IMA/Consulting rows are physically absent from the table on every day
    # but Monday -- confirm they only appear there.
    for date, day in by_date.items():
        ima_or_consulting = [
            e for e in day.entries if e.cohort_id in ("IMA", "Consulting")
        ]
        if date == "2026-08-31":
            assert ima_or_consulting, "expected IMA/Consulting entries on Monday"
        else:
            assert not ima_or_consulting, f"did not expect IMA/Consulting entries on {date}"


def test_week14_punctuation_tolerance():
    result = parse_timetable_grid(WEEK14)

    scop = [e for d in result.days for e in d.entries if e.raw_label == "SCOP(18)"]
    assert scop and all(e.course_guess == "SCOP" and e.session_numbers == [18] for e in scop)

    oscsd = [e for d in result.days for e in d.entries if e.raw_label == "OSCSD (18)"]
    assert oscsd and all(e.course_guess == "OSCSD" and e.session_numbers == [18] for e in oscsd)


def test_week14_banners():
    result = parse_timetable_grid(WEEK14)
    banners = {
        e.raw_label for d in result.days for e in d.entries if e.entry_kind.value == "banner"
    }
    assert "BB END TERM EXAM" in banners
    assert "SURPRISE QUIZ" in banners


def test_header_format_error(monkeypatch):
    def fake_bands(path):
        return (["not", "a", "real", "header"], [[["x"]]])

    monkeypatch.setattr("app.timetable_grid_parser.extract_grid_bands", fake_bands)
    with pytest.raises(TimetableGridFormatError):
        parse_timetable_grid("irrelevant.pdf")


def test_no_table_found_error(monkeypatch):
    monkeypatch.setattr("app.timetable_grid_parser.extract_grid_bands", lambda path: ([], []))
    with pytest.raises(TimetableGridFormatError):
        parse_timetable_grid("irrelevant.pdf")
