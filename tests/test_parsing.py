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

requires_groq_key = pytest.mark.skipif(
    not os.environ.get("GROQ_API_KEY"), reason="GROQ_API_KEY not set"
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


def test_llm_client_requires_api_key(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", None)
    with pytest.raises(LLMError):
        LLMClient(api_key=None)


@requires_groq_key
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


@requires_groq_key
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

    double_session_entries = [
        entry
        for day in parsed.days
        for entry in day.entries
        if len(entry.session_numbers) >= 2
    ]
    assert double_session_entries, "expected at least one entry with double sessions"

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


@requires_groq_key
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
