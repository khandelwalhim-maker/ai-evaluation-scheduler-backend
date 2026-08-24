from __future__ import annotations

import time
from typing import Any, Optional, Union

from pydantic import BaseModel, Field

from app import engine
from app.engine import Proposal
from app.schemas import CalendarState, ConfirmationQuestion

DEFAULT_SESSION_ID = "office"

UPLOAD_LIMIT = 10
UPLOAD_WINDOW_SECONDS = 600


class PendingRequest(BaseModel):
    """Slot-filling state for an in-progress schedule_request. `fields` holds
    whatever the intent classifier already extracted (course, name, type,
    scope, ...); `missing_fields` names what still has to be supplied before
    the engine can be called -- duration_minutes in practice, since SPEC
    requires it always be asked and never defaulted for exams."""

    fields: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)


class SessionState(BaseModel):
    session_id: str = DEFAULT_SESSION_ID
    calendar: CalendarState = Field(default_factory=CalendarState)
    confirmation_queue: list[ConfirmationQuestion] = Field(default_factory=list)
    pending_request: Optional[PendingRequest] = None
    proposal_history: list[Proposal] = Field(default_factory=list)
    state_version: int = 0

    def bump(self) -> None:
        self.state_version += 1

    def serialize(self) -> dict:
        return self.model_dump(mode="json")

    @classmethod
    def restore(cls, blob: Union[dict, str, bytes]) -> "SessionState":
        if isinstance(blob, (str, bytes)):
            state = cls.model_validate_json(blob)
        else:
            state = cls.model_validate(blob)

        # Re-hydrate the engine's proposal lookup table so approve() keeps
        # working after a restore within the same process. engine._PROPOSALS
        # is an in-memory, process-wide dict (see docs/HANDOFF.md design
        # decision #4); it resets on process restart regardless.
        for proposal in state.proposal_history:
            engine._PROPOSALS[proposal.id] = proposal
        return state


class SessionStore:
    """Single in-memory store keyed by X-Session-Id (default "office").
    Upload rate-limit bookkeeping lives here rather than on SessionState
    because it is operational state, not business state that should
    round-trip through export/import."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._upload_times: dict[str, list[float]] = {}

    def get(self, session_id: str = DEFAULT_SESSION_ID) -> SessionState:
        session = self._sessions.get(session_id)
        if session is None:
            session = SessionState(session_id=session_id)
            self._sessions[session_id] = session
        return session

    def get_existing(self, session_id: str) -> Optional[SessionState]:
        """Like get(), but never auto-creates. Lets callers (the HTTP layer)
        return 404 for a session the process has never seen -- including one
        it used to know before an in-memory restart -- so a client holding a
        local mirror can detect the loss and restore it via
        POST /api/state/restore instead of silently continuing against an
        empty session."""
        return self._sessions.get(session_id)

    def replace(self, session_id: str, state: SessionState) -> SessionState:
        state.session_id = session_id
        self._sessions[session_id] = state
        return state

    def register_upload(self, session_id: str) -> bool:
        """Records an upload attempt and returns whether it is allowed under
        the 10-uploads-per-10-minutes cap. Denied attempts are not counted
        again on retry (only allowed uploads consume the budget)."""
        now = time.time()
        times = self._upload_times.setdefault(session_id, [])
        cutoff = now - UPLOAD_WINDOW_SECONDS
        times[:] = [t for t in times if t > cutoff]
        if len(times) >= UPLOAD_LIMIT:
            return False
        times.append(now)
        return True


STORE = SessionStore()
