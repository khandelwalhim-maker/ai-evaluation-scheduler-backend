from __future__ import annotations

import logging
from pathlib import Path

from app import config
from app.cache import cached_parse
from app.llm import LLMClient
from app.pdf_extract import extract_text_generic, ocr_fallback
from app.schemas import (
    CalendarState,
    CohortKind,
    ConfirmationQuestion,
    CourseOutline,
    EntryKind,
    ParsedTimetable,
    TimetableEntry,
)
from app.timetable_grid_parser import parse_timetable_grid

logger = logging.getLogger("uvicorn.error")

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_MIN_TEXT_CHARS = 100
CONFIDENCE_THRESHOLD = 0.85
RECURRENCE_THRESHOLD = 3


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def _extract_document_text(path: str) -> str:
    text = extract_text_generic(path)
    if len(text.strip()) < _MIN_TEXT_CHARS:
        text = ocr_fallback(path)
    return text


def parse_course_outline(path: str, llm_client: LLMClient | None = None) -> CourseOutline:
    def compute() -> CourseOutline:
        llm = llm_client or LLMClient()
        text = _extract_document_text(path)
        system_prompt = _load_prompt("extract_outline.txt")
        return llm.complete_json(
            system_prompt, text, CourseOutline, config.MODEL_PARSE, max_tokens=4000
        )

    return cached_parse(path, "outline", CourseOutline, compute)


def parse_timetable(path: str) -> ParsedTimetable:
    def compute() -> ParsedTimetable:
        combined = parse_timetable_grid(path)
        logger.info("parse_timetable: parsed %d day(s) from %s (deterministic)", len(combined.days), path)
        combined.questions = _confirmation_questions(combined)
        return combined

    return cached_parse(path, "timetable", ParsedTimetable, compute)


def _confirmation_questions(parsed: ParsedTimetable) -> list[ConfirmationQuestion]:
    questions: list[ConfirmationQuestion] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, text: str, context: str) -> None:
        dedupe_key = (kind, context)
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        questions.append(ConfirmationQuestion(kind=kind, question=text, context=context))

    code_days: dict[str, set[str]] = {}

    for day in parsed.days:
        for entry in day.entries:
            label = entry.course_guess or entry.raw_label

            if entry.confidence < CONFIDENCE_THRESHOLD:
                add(
                    "identity",
                    f"Timetable code '{label}' was extracted with low confidence "
                    f"({entry.confidence:.2f}) on {day.date}. Does it match a known course "
                    f"outline, or is it a distinct course? (raw label: '{entry.raw_label}')",
                    entry.raw_label,
                )

            if entry.cohort_kind == CohortKind.unknown:
                add(
                    "cohort",
                    f"Could not determine which division or minor row '{entry.row_label}' "
                    f"belongs to for entry '{entry.raw_label}' on {day.date}. Please confirm "
                    f"the cohort.",
                    entry.raw_label,
                )

            is_scheduled_item = entry.entry_kind in (EntryKind.class_, EntryKind.existing_assessment)
            if is_scheduled_item and (not entry.session_numbers) and "tbc" in entry.raw_label.lower():
                add(
                    "session",
                    f"Session number is unclear ('tbc') for '{entry.raw_label}' on {day.date}. "
                    f"Please confirm the session number.",
                    entry.raw_label,
                )

            if entry.entry_kind == EntryKind.class_ and entry.course_guess:
                code_days.setdefault(entry.course_guess, set()).add(day.date)

    # A code that recurs across many days without ever resolving to a known
    # course is exactly the "might be the same course as X" identity ambiguity
    # the spec calls out (its own worked example is EAB vs. ABA) -- flag it
    # even when each day's chunk individually looked confident, since a
    # per-day extraction never sees the cross-week recurrence pattern.
    for code, dates in code_days.items():
        if len(dates) >= RECURRENCE_THRESHOLD:
            add(
                "identity",
                f"Timetable code '{code}' recurs across {len(dates)} different days "
                f"({', '.join(sorted(dates))}) but was never linked to a known course "
                f"outline. Confirm which course this code refers to.",
                code,
            )

    return questions


def merge_timetables(existing_state: CalendarState, new_parsed: ParsedTimetable) -> CalendarState:
    existing_state.questions.extend(new_parsed.questions)

    for new_day in new_parsed.days:
        existing_day = existing_state.dates.get(new_day.date)
        if existing_day is None:
            existing_state.dates[new_day.date] = new_day
            continue

        by_key: dict[tuple[str, str], TimetableEntry] = {
            (entry.cohort_id or entry.row_label, entry.raw_label): entry
            for entry in existing_day.entries
        }

        for new_entry in new_day.entries:
            key = (new_entry.cohort_id or new_entry.row_label, new_entry.raw_label)
            if key in by_key:
                existing_state.questions.append(
                    ConfirmationQuestion(
                        kind="duplicate",
                        question=(
                            f"Duplicate entry for '{new_entry.row_label}' on {new_day.date} "
                            f"('{new_entry.raw_label}'); the newer upload replaced the existing one."
                        ),
                        context=new_entry.raw_label,
                    )
                )
            by_key[key] = new_entry

        existing_day.entries = list(by_key.values())
        existing_day.holiday = existing_day.holiday or new_day.holiday

    return existing_state
