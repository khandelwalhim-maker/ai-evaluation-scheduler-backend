import os
import shutil
from pathlib import Path

import pytest

from app import config
from app.cache import CACHE_DIR
from app.llm import LLMClient, LLMError
from app.parser import parse_course_outline, parse_timetable
from app.schemas import EvaluationType

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
OUTLINE_PATH = str(FIXTURES_DIR / "aba_course_outline.pdf")
TIMETABLE_PATH = str(FIXTURES_DIR / "timetable_week13.pdf")
TIMETABLE_PATH_2 = str(FIXTURES_DIR / "timetable_week14.pdf")

requires_llm_key = pytest.mark.skipif(
    not os.environ.get("LLM_API_KEY"), reason="LLM_API_KEY not set"
)


@pytest.fixture(autouse=True)
def _clear_parse_cache():
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
    yield


@pytest.fixture(autouse=True)
def _fixtures_exist():
    assert Path(OUTLINE_PATH).is_file(), f"missing fixture: {OUTLINE_PATH}"
    assert Path(TIMETABLE_PATH).is_file(), f"missing fixture: {TIMETABLE_PATH}"
    assert Path(TIMETABLE_PATH_2).is_file(), f"missing fixture: {TIMETABLE_PATH_2}"


def test_llm_client_requires_api_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", None)
    with pytest.raises(LLMError):
        LLMClient(api_key=None)


@requires_llm_key
def test_parse_course_outline():
    outline = parse_course_outline(OUTLINE_PATH)

    assert "applied business analytics" in outline.name.lower()
    assert len(outline.evaluations) == 4

    group_evals = [e for e in outline.evaluations if e.type == EvaluationType.group]
    assert group_evals, "expected a group assignment evaluation"
    assert all(e.in_scope is False for e in group_evals)

    quiz1 = next(
        (e for e in outline.evaluations if e.timing_note and "session 8" in e.timing_note.lower()),
        None,
    )
    assert quiz1 is not None, "expected an evaluation with an after-session-8 timing note"
    assert quiz1.type == EvaluationType.quiz

    endterms = [e for e in outline.evaluations if e.type == EvaluationType.endterm]
    assert len(endterms) == 1


def test_parse_timetable():
    parsed = parse_timetable(TIMETABLE_PATH)

    dates = {day.date for day in parsed.days}
    assert len(parsed.days) == 7
    assert dates == {
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
        "2026-08-27",
        "2026-08-28",
        "2026-08-29",
        "2026-08-30",
    }

    aba_banner_dates = {
        day.date
        for day in parsed.days
        for entry in day.entries
        if "aba" in (entry.course_guess or entry.raw_label).lower()
        and entry.entry_kind.value == "banner"
    }
    assert "2026-08-25" in aba_banner_dates, (
        "expected an 'ABA' banner on 2026-08-25 (verified from the real fixture via "
        "pdfplumber table extraction); got banners on: " + repr(aba_banner_dates)
    )

    # Two adjacent time-slot cells (not one cell with two session numbers)
    # is how the real timetable expresses a double slot for one course --
    # e.g. "OSCSD HJ (13)" and "OSCSD HJ (14)" on 2026-08-24.
    oscsd_sessions = sorted(
        n
        for day in parsed.days if day.date == "2026-08-24"
        for entry in day.entries
        if entry.course_guess == "OSCSD"
        for n in entry.session_numbers
    )
    assert oscsd_sessions.count(13) >= 1 and oscsd_sessions.count(14) >= 1, (
        "expected separate OSCSD sessions 13 and 14 as adjacent time-slot entries "
        "on 2026-08-24; got: " + repr(oscsd_sessions)
    )

    assert parsed.questions, "expected a non-empty confirmation queue"
    identity_questions = [
        q
        for q in parsed.questions
        if "eab" in q.question.lower() or "aba" in q.question.lower()
        or "eab" in (q.context or "").lower() or "aba" in (q.context or "").lower()
    ]
    assert identity_questions, (
        "expected a confirmation question referencing EAB or ABA; got: "
        + repr([q.question for q in parsed.questions])
    )


def test_parse_timetable_with_registry_resolves_identity_questions():
    # Same real fixture as test_parse_timetable, which documents that it
    # produces low-confidence/recurring EAB/ABA identity questions with no
    # registry. Supplying a registry should resolve both codes and remove
    # every question that references them, without touching cohort/tbc
    # questions (which are independent checks).
    registry = {"EAB": "Economic Analysis for Business", "ABA": "Applied Business Analytics"}
    parsed = parse_timetable(TIMETABLE_PATH, registry)

    leftover_identity_mentions = [
        q
        for q in parsed.questions
        if "eab" in q.question.lower() or "aba" in q.question.lower()
        or "eab" in (q.context or "").lower() or "aba" in (q.context or "").lower()
    ]
    assert not leftover_identity_mentions, (
        "registry should have resolved every EAB/ABA identity question; still present: "
        + repr([q.question for q in leftover_identity_mentions])
    )

    resolved_names = {
        entry.course_guess
        for day in parsed.days
        for entry in day.entries
        if entry.course_guess in registry.values()
    }
    assert resolved_names == set(registry.values()), (
        "expected timetable entries' course_guess to be rewritten to the registry's canonical "
        "names; got: " + repr(resolved_names)
    )

    resolved_entries = [
        entry
        for day in parsed.days
        for entry in day.entries
        if entry.course_guess in registry.values()
    ]
    assert resolved_entries, "expected at least one resolved entry to check course_code against"
    assert all(entry.course_code in registry for entry in resolved_entries), (
        "course_code must keep the original raw code (EAB/ABA) even after registry "
        "resolution overwrites course_guess to the canonical name -- it is the stable "
        "join key course_specializations lookups depend on"
    )


def test_parse_timetable_registry_does_not_suppress_unrelated_questions():
    # A registry entry for an unrelated code must not accidentally suppress
    # cohort/tbc questions, which are independent of identity resolution.
    baseline = parse_timetable(TIMETABLE_PATH)
    with_unrelated_registry = parse_timetable(TIMETABLE_PATH, {"ZZZ": "Not A Real Course"})

    non_identity = lambda qs: [q for q in qs if q.kind != "identity"]
    assert len(non_identity(with_unrelated_registry.questions)) == len(non_identity(baseline.questions))


@requires_llm_key
def test_parse_course_outline_uses_cache(monkeypatch):
    calls = {"n": 0}
    original = LLMClient.complete_json

    def counting(self, *args, **kwargs):
        calls["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(LLMClient, "complete_json", counting)

    first = parse_course_outline(OUTLINE_PATH)
    second = parse_course_outline(OUTLINE_PATH)

    assert calls["n"] == 1, "second parse of the same file should hit the cache, not the LLM"
    assert first == second
