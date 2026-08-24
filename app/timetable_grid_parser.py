from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple

from app.pdf_extract import extract_grid_bands
from app.schemas import CohortKind, EntryKind, ParsedTimetable, TimetableDay, TimetableEntry

# Divisions D and E belong to a different program (PGDM-Business Management,
# Strategy minor) and are explicitly out of scope for this scheduler --
# docs/SPEC.md: "Out of scope: PGDM-Business Management (Divisions D and E,
# Strategy minor)". Their rows are populated with real course codes in the
# source PDFs, so this is a deliberate drop, not treating them as empty.
_IN_SCOPE_DIVISIONS = {"A", "B", "C"}
_OUT_OF_SCOPE_DIVISIONS = {"D", "E"}


class TimetableGridFormatError(ValueError):
    """Raised when the extracted table doesn't match the expected SPJIMR
    Term IV timetable header template closely enough to parse reliably --
    signals a format change needing human attention, instead of silently
    producing garbage entries."""


@dataclass(frozen=True)
class _HeaderMap:
    date_col: int
    row_label_col: int
    am_div_col: int
    pm_div_col: int
    time_cols: dict[int, tuple[str, str]]  # col index -> ("HH:MM", "HH:MM")


_TIME_RANGE_RE = re.compile(
    r"^\s*(\d{1,2})[.:](\d{2})\s*-\s*(\d{1,2})[.:](\d{2})\s*(am|pm)?\s*$", re.IGNORECASE
)


def _to_hhmm(hour: str, minute: str, meridiem: str) -> str:
    h = int(hour)
    if meridiem == "pm" and h != 12:
        h += 12
    if meridiem == "am" and h == 12:
        h = 0
    return f"{h:02d}:{int(minute):02d}"


def _parse_time_range(cell_text: str, default_meridiem: str) -> tuple[str, str] | None:
    match = _TIME_RANGE_RE.match(cell_text)
    if not match:
        return None
    h1, m1, h2, m2, meridiem = match.groups()
    # A single meridiem token applies to both ends of the range -- verified
    # against every real header cell across two fixtures; SPJIMR's slots
    # never straddle noon/midnight in a way that would need per-end
    # inference.
    meridiem = (meridiem or default_meridiem).lower()
    return _to_hhmm(h1, m1, meridiem), _to_hhmm(h2, m2, meridiem)


def _parse_header(header: list[str]) -> _HeaderMap:
    if len(header) < 9:
        raise TimetableGridFormatError(f"expected >=9 header columns, got {len(header)}: {header!r}")

    normalized = [h.replace("\n", "").strip() for h in header]
    lunch_idx = [i for i, h in enumerate(normalized) if "LUNCH" in h.upper()]
    div_idx = [i for i, h in enumerate(normalized) if "DIV" in h.upper()]
    room_idx = [i for i, h in enumerate(normalized) if h.upper() == "CR"]

    if len(lunch_idx) != 1 or len(div_idx) != 2 or len(room_idx) != 2:
        raise TimetableGridFormatError(
            f"header doesn't match the expected timetable template "
            f"(lunch={lunch_idx}, div={div_idx}, room={room_idx}): {header!r}"
        )
    am_div_col, pm_div_col = div_idx
    if not (am_div_col < lunch_idx[0] < pm_div_col):
        raise TimetableGridFormatError(f"header column order looks wrong: {header!r}")

    skip = {0, 1, am_div_col, pm_div_col, room_idx[0], room_idx[1], lunch_idx[0]}
    time_cols: dict[int, tuple[str, str]] = {}
    for i, h in enumerate(normalized):
        if i in skip:
            continue
        default_meridiem = "am" if i < lunch_idx[0] else "pm"
        parsed = _parse_time_range(h, default_meridiem)
        if parsed:
            time_cols[i] = parsed

    if not time_cols:
        raise TimetableGridFormatError(f"found no parseable time-slot columns in header: {header!r}")

    return _HeaderMap(date_col=0, row_label_col=1, am_div_col=am_div_col, pm_div_col=pm_div_col, time_cols=time_cols)


_DATE_ANY_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})")


def _extract_band_date(band: list[list[str]], date_col: int) -> str:
    # Concatenate every row's date-column text in band order and search the
    # combined string once -- the date/weekday marker's row position drifts
    # unpredictably across real bands (same cell, split across two rows two
    # different ways, at different row indices in different bands), so
    # scanning all rows beats assuming a fixed one.
    fragments = [row[date_col].strip() for row in band if date_col < len(row) and row[date_col].strip()]
    combined = " ".join(fragments)
    match = _DATE_ANY_RE.search(combined)
    if not match:
        raise TimetableGridFormatError(f"no date found in day-band (col0 fragments={fragments!r})")
    return match.group(1)


_LEADING_CODE_RE = re.compile(r"^([A-Z][A-Z&]+)")
_SESSION_PAREN_RE = re.compile(r"\((\d+)\)")
_SESSION_AMP_RE = re.compile(r"(\d+)\s*&\s*(\d+)")
_SESSION_HYPHEN_RE = re.compile(r"[-_]\s*(\d+)(?!\d)")
_SESSION_TBC_RE = re.compile(r"\btbc\b", re.IGNORECASE)
_BANNER_KEYWORDS = ("end term", "endterm", "mid term", "midterm", "exam", "quiz")

CONF_CLEAN = 1.0  # code + session cleanly found, or code + explicit "tbc"
CONF_BANNER = 0.6  # any banner -- identity mapping always warrants human confirmation
CONF_AMBIGUOUS = 0.5  # code found, but no session number and no "tbc" (truncated/garbled cell)
CONF_NO_CODE = 0.3  # couldn't even find a leading code token


class _CellParse(NamedTuple):
    course_guess: str | None
    session_numbers: list[int]
    entry_kind: EntryKind
    confidence: float


def _extract_sessions(text: str) -> list[int]:
    paren = [int(n) for n in _SESSION_PAREN_RE.findall(text)]
    if paren:
        return paren
    amp = _SESSION_AMP_RE.search(text)
    if amp:
        return [int(amp.group(1)), int(amp.group(2))]
    hyphen = [int(n) for n in _SESSION_HYPHEN_RE.findall(text)]
    return hyphen if len(hyphen) == 1 else []  # 0 or ambiguous multi-match -> unresolved


def _parse_cell(normalized: str) -> _CellParse:
    is_banner = any(kw in normalized.lower() for kw in _BANNER_KEYWORDS)
    sessions = _extract_sessions(normalized)
    code_match = _LEADING_CODE_RE.match(normalized)
    course_guess = code_match.group(1) if code_match else None

    if is_banner:
        return _CellParse(course_guess, sessions, EntryKind.banner, CONF_BANNER)
    if course_guess and sessions:
        return _CellParse(course_guess, sessions, EntryKind.class_, CONF_CLEAN)
    if course_guess and _SESSION_TBC_RE.search(normalized):
        return _CellParse(course_guess, [], EntryKind.class_, CONF_CLEAN)
    if course_guess:
        return _CellParse(course_guess, [], EntryKind.class_, CONF_AMBIGUOUS)
    return _CellParse(None, [], EntryKind.unknown, CONF_NO_CODE)


@dataclass
class _DocumentState:
    # Forward-fill state, threaded across the *whole document* (not reset
    # per band) -- some real timetables only print a division's minor label
    # on its first appearance (e.g. Monday) and leave every later day's row
    # blank for that same physical column, or omit the row from the table
    # entirely on later days.
    last_label_by_division: dict[str, str] = field(default_factory=dict)
    last_label_by_minor_slot: dict[int, str] = field(default_factory=dict)


def _cell(row: list[str], idx: int) -> str:
    return row[idx].strip() if idx < len(row) else ""


def _normalize_cell_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def _parse_row_cells(
    row: list[str], header_map: _HeaderMap, *, row_label: str, cohort_kind: CohortKind, cohort_id: str | None
) -> list[TimetableEntry]:
    entries: list[TimetableEntry] = []
    for col, (start, end) in header_map.time_cols.items():
        text = _cell(row, col)
        if text in ("", "-"):
            continue
        normalized = _normalize_cell_text(text)
        if not normalized or normalized == "-":
            continue
        parsed = _parse_cell(normalized)
        entries.append(
            TimetableEntry(
                raw_label=normalized,
                row_label=row_label,
                cohort_kind=cohort_kind,
                cohort_id=cohort_id,
                course_guess=parsed.course_guess,
                session_numbers=parsed.session_numbers,
                start=start,
                end=end,
                entry_kind=parsed.entry_kind,
                confidence=parsed.confidence,
            )
        )
    return entries


def _parse_band(band: list[list[str]], header_map: _HeaderMap, state: _DocumentState) -> TimetableDay:
    date_str = _extract_band_date(band, header_map.date_col)
    entries: list[TimetableEntry] = []
    minor_ordinal = 0  # reset per band; ordinal position among divisionless rows

    for row in band:
        div_letter = _cell(row, header_map.am_div_col) or _cell(row, header_map.pm_div_col)
        own_label = _cell(row, header_map.row_label_col)

        if div_letter in _OUT_OF_SCOPE_DIVISIONS:
            continue

        if div_letter in _IN_SCOPE_DIVISIONS:
            if own_label:
                state.last_label_by_division[div_letter] = own_label
            minor_label = state.last_label_by_division.get(div_letter)

            entries += _parse_row_cells(
                row, header_map, row_label=minor_label or f"Division {div_letter}",
                cohort_kind=CohortKind.division, cohort_id=div_letter,
            )
            if minor_label:
                entries += _parse_row_cells(
                    row, header_map, row_label=minor_label,
                    cohort_kind=CohortKind.minor, cohort_id=minor_label,
                )
        elif div_letter:
            # Non-blank but unexpected letter -- surfaced as unknown rather
            # than silently dropped, since it isn't a division we know how
            # to classify.
            entries += _parse_row_cells(
                row, header_map, row_label=own_label or f"row (div={div_letter})",
                cohort_kind=CohortKind.unknown, cohort_id=None,
            )
        else:
            if own_label:
                state.last_label_by_minor_slot[minor_ordinal] = own_label
            label = own_label or state.last_label_by_minor_slot.get(minor_ordinal)
            minor_ordinal += 1
            if label:
                entries += _parse_row_cells(
                    row, header_map, row_label=label,
                    cohort_kind=CohortKind.minor, cohort_id=label,
                )
            else:
                entries += _parse_row_cells(
                    row, header_map, row_label="unknown minor row",
                    cohort_kind=CohortKind.unknown, cohort_id=None,
                )

    return TimetableDay(date=date_str, entries=entries)


def parse_timetable_grid(path: str) -> ParsedTimetable:
    header, day_bands = extract_grid_bands(path)
    if not header or not day_bands:
        raise TimetableGridFormatError(f"pdfplumber found no table structure in {path!r}")
    header_map = _parse_header(header)

    state = _DocumentState()
    days = [_parse_band(band, header_map, state) for band in day_bands]
    return ParsedTimetable(days=days)
