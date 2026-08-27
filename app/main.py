import logging
import os
import re
import tempfile
from datetime import date as _date, timedelta
from typing import Optional

logger = logging.getLogger("uvicorn.error")

from fastapi import APIRouter, Body, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError

from app import admin_settings, cache, config, course_registry, engine, orchestrator, parser
from app.course_registry import CourseRegistryFormatError
from app.llm import LLMClient, LLMError
from app.schemas import CalendarState, CohortKind, CohortRegistry, ConfirmationQuestion, EntryKind
from app.session import DEFAULT_SESSION_ID, STORE, SessionState
from app.timetable_grid_parser import TimetableGridFormatError

app = FastAPI(title="AI Evaluation Scheduler")
router = APIRouter(prefix="/api")

# This is a pure API service -- the frontend is a separate deployment (see
# ai-evaluation-scheduler-frontend) calling it cross-origin. See
# config.CORS_ORIGINS for how the allowed origin(s) are configured.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_UPLOAD_KINDS = {"course_outline", "timetable"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
WEEKDAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def get_session_id(x_session_id: str = Header(default=DEFAULT_SESSION_ID)) -> str:
    return x_session_id or DEFAULT_SESSION_ID


def require_session(session_id: str = Depends(get_session_id)) -> SessionState:
    """Every route except /state/restore and /import depends on this instead
    of STORE.get(), so a session the process has never seen -- including one
    it used to know before an in-memory restart -- 404s instead of silently
    resuming against an empty session. A client mirroring state locally can
    treat that 404 as the signal to POST /api/state/restore and retry."""
    session = STORE.get_existing(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown session '{session_id}'")
    return session


_STATUS_RE = re.compile(r"failed with (\d+)")


def _classify_llm_error(exc: Exception) -> str:
    """Classifies a probe failure into a status safe to show a client,
    without ever forwarding the upstream provider's raw response text --
    see the hardening note on the three sites below for why that matters.
    Defensive on two axes, not just the happy path: LLMError message
    shapes other than llm.py's "failed with {status}" (e.g. rate-limit
    exhaustion, schema-validation-failed messages), and non-LLMError
    exceptions entirely (a network-level failure from httpx.post, which
    llm.py's _chat() doesn't wrap in its own try/except) both fall back to
    "other_error" instead of propagating or misparsing."""
    if not isinstance(exc, LLMError):
        return "other_error"
    match = _STATUS_RE.search(str(exc))
    if not match:
        return "other_error"
    status = int(match.group(1))
    if status in (401, 403):
        return "auth_error"
    if status == 429:
        return "rate_limited"
    return "other_error"


class ChatRequest(BaseModel):
    message: str


class ConfirmRequest(BaseModel):
    context: str
    resolution: str


class ApproveRequest(BaseModel):
    proposal_id: str
    candidate_index: int  # 0-based, for programmatic (frontend) callers


def _sync_cohort_registry(state: CalendarState) -> None:
    """Derive the divisions/minors registry from parsed timetable entries.
    Data-driven per SPEC ("never hardcode the minor list"); nothing else
    populates CalendarState.cohorts from a real parse (docs/HANDOFF.md gap #4)."""
    divisions = set(state.cohorts.divisions)
    minors = set(state.cohorts.minors)
    for day in state.dates.values():
        for entry in day.entries:
            if entry.cohort_kind == CohortKind.division and entry.cohort_id:
                divisions.add(entry.cohort_id)
            elif entry.cohort_kind == CohortKind.minor and entry.cohort_id:
                minors.add(entry.cohort_id)
    state.cohorts = CohortRegistry(divisions=sorted(divisions), minors=sorted(minors))


def _format_minutes(m: Optional[int]) -> Optional[str]:
    if m is None:
        return None
    return f"{m // 60:02d}:{m % 60:02d}"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/selfcheck")
def selfcheck() -> dict:
    """Diagnoses a broken deployment: is the LLM key configured, is the parse
    cache directory writable, and (only if a key is configured) can we
    actually reach the LLM. Every check is best-effort and independent --
    one failing never prevents the others from reporting. This service no
    longer serves a frontend bundle (see ai-evaluation-scheduler-frontend),
    so there is no static-bundle check here."""
    llm_api_key = {"present": bool(config.LLM_API_KEY)}

    try:
        cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        probe = cache.CACHE_DIR / ".selfcheck_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        parse_cache = {"writable": True, "path": str(cache.CACHE_DIR)}
    except OSError as exc:
        parse_cache = {"writable": False, "path": str(cache.CACHE_DIR), "error": str(exc)}

    if not config.LLM_API_KEY:
        llm_ping = {"status": "skipped", "reason": "LLM_API_KEY is not set"}
    else:
        try:
            client = LLMClient()
            client.complete_text("Reply with one word.", "ping", config.MODEL_PARSE, max_tokens=1)
            llm_ping = {"status": "ok", "model": config.MODEL_PARSE}
        except LLMError as exc:
            # Never forward the raw upstream response text here -- an
            # LLMError's message can carry the full raw provider response
            # body (see llm.py's _chat()), and this is a real third-party
            # secret's blast radius, unrelated to whether this route (or
            # /admin/settings/test) has an access gate. The full exception
            # (llm.py already truncates it) still reaches the operator via
            # logger.error at the LLMError raise site itself.
            llm_ping = {"status": "error", "model": config.MODEL_PARSE, "detail": "error contacting provider"}

    ok = parse_cache["writable"] and llm_ping["status"] != "error"
    return {
        "status": "ok" if ok else "degraded",
        "llm_api_key": llm_api_key,
        "parse_cache": parse_cache,
        "llm_ping": llm_ping,
    }


class AdminSettingsUpdate(BaseModel):
    # Every field optional and omit-to-leave-unchanged. llm_api_key is
    # write-only by design: there is no way to read it back in full, only
    # masked (see get_admin_settings), matching how it could previously
    # only ever be set via an env var. A blank/omitted value never clears
    # llm_api_key/llm_base_url/model_* (those must never legitimately be
    # blank -- LLMClient would break), but a blank string *does* clear an
    # extra_*_instructions field back to just the base prompt, since blank
    # is a meaningful value there.
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    model_parse: Optional[str] = None
    model_narrate: Optional[str] = None
    model_fallback: Optional[str] = None
    extra_intent_instructions: Optional[str] = None
    extra_narrate_instructions: Optional[str] = None
    extra_outline_instructions: Optional[str] = None


def _mask_key(key: Optional[str]) -> Optional[str]:
    if not key:
        return None
    if len(key) <= 4:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


@router.get("/admin/settings")
def get_admin_settings() -> dict:
    return {
        "llm_api_key_masked": _mask_key(config.LLM_API_KEY),
        "llm_base_url": config.LLM_BASE_URL,
        "model_parse": config.MODEL_PARSE,
        "model_narrate": config.MODEL_NARRATE,
        "model_fallback": config.MODEL_FALLBACK,
        "extra_intent_instructions": admin_settings.EXTRA_INSTRUCTIONS["intent"],
        "extra_narrate_instructions": admin_settings.EXTRA_INSTRUCTIONS["narrate"],
        "extra_outline_instructions": admin_settings.EXTRA_INSTRUCTIONS["extract_outline"],
    }


@router.post("/admin/settings")
def update_admin_settings(payload: AdminSettingsUpdate) -> dict:
    # Mutates app.config's attributes directly (read via live `config.X`
    # access everywhere -- llm.py, orchestrator.py, parser.py -- never
    # `from app.config import X`), so every call site picks this up
    # immediately with no restart, the same way orchestrator.py's
    # adjust_rule already mutates engine.py's constants at runtime.
    # In-memory only: resets to the Railway env vars on the next deploy.
    changed: list[str] = []

    if payload.llm_api_key:
        config.LLM_API_KEY = payload.llm_api_key
        changed.append("llm_api_key")
    if payload.llm_base_url:
        config.LLM_BASE_URL = payload.llm_base_url
        changed.append("llm_base_url")
    if payload.model_parse:
        config.MODEL_PARSE = payload.model_parse
        changed.append("model_parse")
    if payload.model_narrate:
        config.MODEL_NARRATE = payload.model_narrate
        changed.append("model_narrate")
    if payload.model_fallback:
        config.MODEL_FALLBACK = payload.model_fallback
        changed.append("model_fallback")
    if payload.extra_intent_instructions is not None:
        admin_settings.EXTRA_INSTRUCTIONS["intent"] = payload.extra_intent_instructions
        changed.append("extra_intent_instructions")
    if payload.extra_narrate_instructions is not None:
        admin_settings.EXTRA_INSTRUCTIONS["narrate"] = payload.extra_narrate_instructions
        changed.append("extra_narrate_instructions")
    if payload.extra_outline_instructions is not None:
        admin_settings.EXTRA_INSTRUCTIONS["extract_outline"] = payload.extra_outline_instructions
        changed.append("extra_outline_instructions")

    logger.info("Developer Options: admin settings updated: %s", changed)  # names only, never values
    return {"status": "updated", "changed": changed}


@router.post("/admin/settings/test")
def test_admin_settings() -> dict:
    """Runs the same probe /selfcheck does, against whatever config is
    currently active, so a just-saved key/model can be verified immediately.
    Deliberately broad except: classifies auth/rate-limit/network/any other
    failure into a status without ever forwarding the upstream provider's
    raw response text back to the browser -- see _classify_llm_error."""
    try:
        client = LLMClient()
        client.complete_text("Reply with one word.", "ping", config.MODEL_PARSE, max_tokens=1)
        return {"status": "ok", "model": config.MODEL_PARSE}
    except Exception as exc:
        logger.exception("Developer Options: settings test probe failed")
        return {"status": _classify_llm_error(exc), "model": config.MODEL_PARSE}


@router.post("/upload")
async def upload_document(
    kind: str = Form(...),
    file: UploadFile = File(...),
    session_id: str = Depends(get_session_id),
    session: SessionState = Depends(require_session),
):
    if kind not in ALLOWED_UPLOAD_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {sorted(ALLOWED_UPLOAD_KINDS)}")

    if not STORE.register_upload(session_id):
        raise HTTPException(status_code=429, detail="Upload rate limit exceeded: 10 uploads per 10 minutes")

    content_type_ok = (file.content_type or "").lower() == "application/pdf"
    filename_ok = (file.filename or "").lower().endswith(".pdf")
    if not (content_type_ok or filename_ok):
        raise HTTPException(status_code=400, detail="Only PDF uploads are accepted")

    body = await file.read()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 10 MB upload limit")
    if not body.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="File does not look like a PDF")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(body)
        tmp_path = tmp.name

    try:
        if kind == "course_outline":
            outline = parser.parse_course_outline(tmp_path)
            session.calendar.courses.append(outline)
            new_questions: list[ConfirmationQuestion] = []
            summary = {"kind": kind, "course": outline.model_dump(mode="json")}
        else:
            parsed = parser.parse_timetable(tmp_path, session.calendar.course_registry)
            parser.merge_timetables(session.calendar, parsed)
            _sync_cohort_registry(session.calendar)
            new_questions = parsed.questions
            summary = {
                "kind": kind,
                "days_parsed": len(parsed.days),
                "dates": sorted(day.date for day in parsed.days),
            }
    except (LLMError, TimetableGridFormatError) as exc:
        # detail is intentionally static, not f"...: {exc}" -- an LLMError's
        # message can carry the full raw upstream provider response body
        # (see llm.py's _chat()), and this response reaches the browser.
        # The full exception is still logged here for the operator.
        logger.error("Document parsing failed (kind=%s, file=%s): %s", kind, file.filename, exc)
        raise HTTPException(status_code=502, detail="Document parsing failed -- check server logs for details.") from exc
    finally:
        os.unlink(tmp_path)

    session.confirmation_queue.extend(new_questions)
    session.bump()

    return {
        "summary": summary,
        "confirmation_questions": [q.model_dump(mode="json") for q in new_questions],
        "state_version": session.state_version,
    }


@router.delete("/course/{index}")
def remove_course(index: int, session: SessionState = Depends(require_session)):
    """Undoes a single wrong course-outline upload. Course outlines never
    generate confirmation_queue entries (only timetable parsing does -- see
    upload_document above, where new_questions is hardcoded empty for
    kind == "course_outline"), so removing one has no other state to clean
    up."""
    courses = session.calendar.courses
    if not (0 <= index < len(courses)):
        raise HTTPException(status_code=404, detail=f"No course outline at index {index}")
    removed = courses.pop(index)
    session.bump()
    return {"status": "removed", "removed": removed.model_dump(mode="json"), "state_version": session.state_version}


@router.delete("/timetable")
def clear_timetable(session: SessionState = Depends(require_session)):
    """Undoes a wrong timetable upload. Timetable entries merge by (date,
    cohort, label) with no per-upload provenance kept (see
    parser.merge_timetables), so there is no way to undo just one upload
    once merged -- this clears all parsed timetable data so the user can
    re-upload cleanly. Also drops confirmation_queue, since every
    confirmation question originates from timetable parsing and would
    otherwise dangle, referencing entries that no longer exist."""
    session.calendar.dates = {}
    session.calendar.cohorts = CohortRegistry()
    session.calendar.questions = []
    session.confirmation_queue = []
    session.bump()
    return {"status": "cleared", "state_version": session.state_version}


@router.post("/course-registry")
async def upload_course_registry(
    file: UploadFile = File(...),
    session: SessionState = Depends(require_session),
):
    body = await file.read()
    try:
        new_registry, new_specializations, collapsed_keys = course_registry.parse_registry_upload(
            body, file.filename or ""
        )
    except CourseRegistryFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Upsert, last-write-wins: a re-upload that corrects an existing
    # abbreviation is expected to take effect, not be ignored.
    session.calendar.course_registry.update(new_registry)
    # A blank specialization cell is omitted from new_specializations entirely
    # (see parse_registry_upload's docstring), so .update() here can never
    # clear an existing specialization the user set one-at-a-time -- only a
    # non-blank cell overwrites.
    session.calendar.course_specializations.update(new_specializations)
    session.bump()

    summary: dict = {"added_or_updated": len(new_registry)}
    if collapsed_keys:
        summary["rows_collapsed"] = collapsed_keys
    return {"summary": summary, "state_version": session.state_version}


@router.get("/course-registry/template")
def download_course_registry_template(format: str = "csv", session: SessionState = Depends(require_session)):
    if format not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'xlsx'")

    unresolved = {
        entry.course_guess.upper()
        for day in session.calendar.dates.values()
        for entry in day.entries
        if entry.course_guess and course_registry.is_unresolved_code(entry.course_guess)
    }
    known_names = [c.name for c in session.calendar.courses]

    content, filename, media_type = course_registry.build_registry_template(
        sorted(unresolved), known_names, format  # type: ignore[arg-type]
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/course-registry")
def clear_course_registry(session: SessionState = Depends(require_session)):
    session.calendar.course_registry = {}
    session.calendar.course_specializations = {}
    session.bump()
    return {"status": "cleared", "state_version": session.state_version}


class RegistryEntryUpsert(BaseModel):
    course_name: str
    # Optional minor specialization tag (one of session.calendar.cohorts.minors,
    # picked from a dropdown on the frontend -- never a division letter, since
    # a core course genuinely runs across all divisions at once and has no
    # single division value to store). Blank or omitted both clear any
    # existing tag: unlike the bulk upload path (course_registry.py), a UI
    # dropdown always resubmits its full current value, so there is no
    # separate "leave unchanged" state to preserve here -- this is the
    # intentional opposite of parse_registry_upload's blank-means-omit rule.
    specialization: Optional[str] = None


@router.put("/course-registry/{abbreviation}")
def upsert_course_registry_entry(
    abbreviation: str, payload: RegistryEntryUpsert, session: SessionState = Depends(require_session)
):
    """Adds or edits exactly one abbreviation -> name mapping, so fixing a
    typo or registering one new code doesn't require the full CSV/XLSX
    download-edit-upload round trip the bulk endpoint above needs."""
    name = payload.course_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="course_name must not be blank")
    key = course_registry.normalize_abbreviation(abbreviation)
    if not key:
        raise HTTPException(status_code=400, detail="abbreviation must not be blank")
    session.calendar.course_registry[key] = name

    specialization = (payload.specialization or "").strip()
    if specialization:
        session.calendar.course_specializations[key] = specialization
    else:
        session.calendar.course_specializations.pop(key, None)

    session.bump()
    return {"status": "updated", "abbreviation": key, "state_version": session.state_version}


@router.delete("/course-registry/{abbreviation}")
def remove_course_registry_entry(abbreviation: str, session: SessionState = Depends(require_session)):
    key = course_registry.normalize_abbreviation(abbreviation)
    if key not in session.calendar.course_registry:
        raise HTTPException(status_code=404, detail=f"No registry entry for '{key}'")
    del session.calendar.course_registry[key]
    # Same normalized key as the course_registry deletion above -- no
    # orphaned specialization tag should survive removing its abbreviation.
    session.calendar.course_specializations.pop(key, None)
    session.bump()
    return {"status": "removed", "abbreviation": key, "state_version": session.state_version}


@router.post("/confirm")
def confirm(payload: ConfirmRequest, session: SessionState = Depends(require_session)):
    message = orchestrator.resolve_confirmation(session, payload.context, payload.resolution)
    return {
        "message": message,
        "remaining_questions": [q.model_dump(mode="json") for q in session.confirmation_queue],
        "state_version": session.state_version,
    }


@router.post("/chat")
def chat(payload: ChatRequest, session: SessionState = Depends(require_session)):
    try:
        result = orchestrator.handle_message(session, payload.message)
    except LLMError as exc:
        # Static detail, same reasoning as the upload handler above: an
        # LLMError's message can carry the raw upstream response body.
        logger.error("Chat handling failed: %s", exc)
        raise HTTPException(status_code=502, detail="Assistant is unavailable -- check server logs for details.") from exc
    return {**result.model_dump(mode="json"), "state_version": session.state_version}


@router.post("/schedule/approve")
def approve_candidate(payload: ApproveRequest, session: SessionState = Depends(require_session)):
    try:
        engine.approve(session.calendar, payload.proposal_id, payload.candidate_index)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IndexError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.bump()
    return {"status": "approved", "state_version": session.state_version}


@router.get("/grid")
def get_grid(week_start: str, session: SessionState = Depends(require_session)):
    try:
        start = _date.fromisoformat(week_start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="week_start must be YYYY-MM-DD") from exc

    days = []
    for offset in range(7):
        d = start + timedelta(days=offset)
        date_str = d.isoformat()
        day = session.calendar.dates.get(date_str)
        classes = []
        assessments = []
        if day is not None:
            for entry in day.entries:
                shaped = {
                    "raw_label": entry.raw_label,
                    "course": entry.course_guess,
                    "course_code": entry.course_code,
                    "cohort_kind": entry.cohort_kind.value,
                    "cohort_id": entry.cohort_id,
                    "session_numbers": entry.session_numbers,
                    "start": entry.start,
                    "end": entry.end,
                    "start_time": _format_minutes(entry.start),
                    "end_time": _format_minutes(entry.end),
                    "confidence": entry.confidence,
                }
                if entry.entry_kind == EntryKind.class_:
                    classes.append(shaped)
                elif entry.entry_kind in (EntryKind.banner, EntryKind.existing_assessment):
                    assessments.append(shaped)
        days.append(
            {
                "date": date_str,
                "weekday": WEEKDAY_LABELS[d.weekday()],
                "holiday": bool(day.holiday) if day is not None else False,
                "classes": classes,
                "assessments": assessments,
            }
        )

    return {"week_start": start.isoformat(), "days": days}


@router.get("/state")
def get_state(session: SessionState = Depends(require_session)):
    return session.serialize()


@router.post("/state/restore")
def restore_state(payload: dict = Body(...), session_id: str = Depends(get_session_id)):
    try:
        restored = SessionState.restore(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid session state: {exc}") from exc
    STORE.replace(session_id, restored)
    return {"status": "restored", "state_version": restored.state_version}


@router.get("/export")
def export_state(session: SessionState = Depends(require_session)):
    blob = session.serialize()
    return JSONResponse(
        content=blob,
        headers={"Content-Disposition": f'attachment; filename="session_{session.session_id}.json"'},
    )


@router.post("/import")
async def import_state(file: UploadFile = File(...), session_id: str = Depends(get_session_id)):
    body = await file.read()
    try:
        restored = SessionState.restore(body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid session export: {exc}") from exc
    STORE.replace(session_id, restored)
    return {"status": "imported", "state_version": restored.state_version}


app.include_router(router)
