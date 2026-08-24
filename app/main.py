import os
import tempfile
from datetime import date as _date, timedelta
from typing import Optional

from fastapi import APIRouter, Body, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from app import cache, config, engine, orchestrator, parser
from app.llm import LLMClient, LLMError
from app.schemas import CalendarState, CohortKind, CohortRegistry, ConfirmationQuestion, EntryKind
from app.session import DEFAULT_SESSION_ID, STORE, SessionState

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
    groq_api_key = {"present": bool(config.GROQ_API_KEY)}

    try:
        cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        probe = cache.CACHE_DIR / ".selfcheck_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        parse_cache = {"writable": True, "path": str(cache.CACHE_DIR)}
    except OSError as exc:
        parse_cache = {"writable": False, "path": str(cache.CACHE_DIR), "error": str(exc)}

    if not config.GROQ_API_KEY:
        llm_ping = {"status": "skipped", "reason": "GROQ_API_KEY is not set"}
    else:
        try:
            client = LLMClient()
            client.complete_text("Reply with one word.", "ping", config.MODEL_PARSE, max_tokens=1)
            llm_ping = {"status": "ok", "model": config.MODEL_PARSE}
        except LLMError as exc:
            llm_ping = {"status": "error", "model": config.MODEL_PARSE, "detail": str(exc)}

    ok = parse_cache["writable"] and llm_ping["status"] != "error"
    return {
        "status": "ok" if ok else "degraded",
        "groq_api_key": groq_api_key,
        "parse_cache": parse_cache,
        "llm_ping": llm_ping,
    }


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
            parsed = parser.parse_timetable(tmp_path)
            parser.merge_timetables(session.calendar, parsed)
            _sync_cohort_registry(session.calendar)
            new_questions = parsed.questions
            summary = {
                "kind": kind,
                "days_parsed": len(parsed.days),
                "dates": sorted(day.date for day in parsed.days),
            }
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"Document parsing failed: {exc}") from exc
    finally:
        os.unlink(tmp_path)

    session.confirmation_queue.extend(new_questions)
    session.bump()

    return {
        "summary": summary,
        "confirmation_questions": [q.model_dump(mode="json") for q in new_questions],
        "state_version": session.state_version,
    }


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
        raise HTTPException(status_code=502, detail=f"Assistant is unavailable: {exc}") from exc
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
