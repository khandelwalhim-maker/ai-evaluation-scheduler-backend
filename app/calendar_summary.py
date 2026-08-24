from __future__ import annotations

from app.schemas import CalendarState, EntryKind

_ASSESSMENT_KINDS = (EntryKind.banner, EntryKind.existing_assessment)


def build_calendar_markdown(state: CalendarState, *, max_days: int = 60) -> str:
    """Compact markdown view of what's already on the calendar -- holidays
    and existing assessments/banners by date -- for grounding context in
    the chat/intent LLM call. Deliberately does not restate known_courses
    or cohort_registry, which _build_context already sends as structured
    JSON; this covers the chronological view JSON doesn't give the model
    cleanly. Caps at max_days to bound prompt size for the small
    parse/narrate models.
    """
    dates = sorted(state.dates.keys())
    if not dates:
        return "No timetable data has been uploaded yet."

    lines = [f"Calendar loaded: {len(dates)} day(s), {dates[0]} to {dates[-1]}."]
    for date in dates[:max_days]:
        day = state.dates[date]
        parts = ["HOLIDAY"] if day.holiday else []
        for entry in day.entries:
            if entry.entry_kind not in _ASSESSMENT_KINDS:
                continue
            label = entry.course_guess or entry.raw_label
            scope = entry.cohort_id or "core"
            time = f"{entry.start // 60:02d}:{entry.start % 60:02d}" if entry.start is not None else "?"
            parts.append(f"{label} ({scope}, {time})")
        if parts:
            lines.append(f"- {date}: {'; '.join(parts)}")

    if len(dates) > max_days:
        lines.append(f"... ({len(dates) - max_days} more day(s) not shown)")

    return "\n".join(lines)
