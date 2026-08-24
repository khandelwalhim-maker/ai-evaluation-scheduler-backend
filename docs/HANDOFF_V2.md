# Handoff v2: AI Evaluation Scheduler (Backend + Frontend)

Status snapshot as of **2026-08-24**, after Phases 0-4. Supersedes
`docs/HANDOFF.md` (the Phase 0-2 snapshot) — that file is left in place for
history, but this one is current. Written so a new engineer (or a new AI
session with no memory of the work) can pick this up cold.

## What this project is

A FastAPI backend plus a Vite/React (TanStack Start) frontend called
"Evaluation Studio," for SPJIMR's PGDM programme. The frontend was
originally a Lovable-generated mock-only prototype; it is now wired to the
real backend end to end. **`docs/SPEC.md` is the single source of truth for
domain rules.** Read it before changing `backend/app/engine.py`,
`workload.py`, `parser.py`, or the prompts — every rule name (H1-H5, S1-S4)
is defined there in full.

Repo root: `evaluation-studio-main/`. Commits so far:

```
0727e59  fix: file upload race condition and duplicate grid keys from live testing
0d64c80  phase 4: real api integration, seven-day grid, duration prompts, state mirror
e344765  phase 3: orchestrator, session state, api routes
efecbb8  phase 2: interval engine, persona workload, ground-truth tests
9c7937b  phase 1: llm client, pdf extraction, parsers, fixture tests
4c36406  phase 0: scaffold backend, spec, dockerfile
```

## Architecture at a glance

```
backend/app/
  main.py         FastAPI app. GET /health, static mount, SPA catch-all,
                   and everything under /api (router, prefix "/api"):
                     POST /upload            multipart, kind=course_outline|timetable
                     POST /confirm           {context, resolution}
                     POST /chat              {message}
                     POST /schedule/approve  {proposal_id, candidate_index (0-based)}
                     GET  /grid?week_start=YYYY-MM-DD
                     GET  /state
                     POST /state/restore     raw exported blob as JSON body
                     GET  /export            same blob, Content-Disposition attachment
                     POST /import            multipart file, same blob
                   Every route except /state/restore and /import depends on
                   require_session(), which 404s if the session hasn't been
                   seen yet (see Design decisions #1 below) -- upload/confirm/
                   chat/approve/grid/state/export all require an existing
                   session; only restore/import create one.
  config.py       Env vars (GROQ_API_KEY, MODEL_PARSE, MODEL_NARRATE,
                   MODEL_FALLBACK, LLM_BASE_URL).

  --- Phase 1: parsing (LLM-assisted, document -> structured data) ---
  schemas.py      Pydantic models: Evaluation, CourseOutline, TimetableEntry,
                   TimetableDay, ParsedTimetable, ConfirmationQuestion,
                   CohortRegistry, CalendarState.
  pdf_extract.py  extract_text_generic, extract_grid, extract_grid_chunks,
                   ocr_fallback.
  llm.py          LLMClient over httpx. complete_json / complete_text,
                   JSON-schema retry-once, 429/413 rate-limit backoff, model
                   fallback escalation. See Known issues #2 -- its retry
                   sleep is uncapped and this bit us live in Phase 4.
  prompts/        extract_outline.txt, extract_timetable.txt, intent.txt,
                   narrate.txt -- verbatim system prompts, do not edit
                   without re-reading SPEC.md.
  parser.py       parse_course_outline, parse_timetable (chunked, one LLM
                   call per day-band), merge_timetables, the confirmation-
                   question backstop for recurring unresolved codes.
  cache.py        SHA-256 file-hash cache under /tmp/parse_cache/ (resolves
                   to whatever drive the process runs from on Windows --
                   NOT stable across different working directories).

  --- Phase 2: engine (deterministic, no LLM calls) ---
  timeline.py     overlaps(), teaching_intervals(), slot/window constants.
  workload.py     Persona, Assessment, build_personas, assessment_audience,
                   daily_load, weekly_load.
  engine.py       ScheduleRequest, Proposal, Impact, propose_slots(),
                   apply_holiday(), approve(). Module-level soft-rule
                   constants (S1_WEIGHT, S2_WEIGHT, S3_WEIGHT,
                   S2_MAX_PER_WEEK) are mutated directly by the orchestrator
                   for adjust_rule -- see Design decisions #2.

  --- Phase 3: conversational layer + session state ---
  session.py      SessionState (calendar, confirmation_queue, pending_request,
                   proposal_history, state_version), SessionStore keyed by
                   X-Session-Id (default "office"). serialize()/restore()
                   round-trip the whole session. get_existing() (no
                   auto-create) backs main.py's require_session(); get() (auto-
                   create) is still used internally by restore/import's
                   replace(). Per-session upload rate limiting (10 / 10 min).
  orchestrator.py handle_message(session, text, llm_client=None): if a
                   pending_request is awaiting duration_minutes, tries a
                   deterministic regex parse first (no LLM call); otherwise
                   classifies via MODEL_PARSE + intent.txt into one of 8
                   actions and dispatches. Narrates Proposal/Impact via
                   MODEL_NARRATE + narrate.txt, fed only engine JSON.
                   Contains the deterministic (non-LLM) scope- and
                   after_session-inference helpers that close HANDOFF v1's
                   gap #4 (see Design decisions #4).

backend/tests/
  test_health.py, test_parsing.py, test_engine.py  (from Phases 0-2)
  test_orchestrator.py  Phase 3, LLM mocked via a FakeLLM stub. Its module
                   docstring has a manual curl walkthrough of the full
                   upload -> confirm -> chat -> approve journey.

docs/SPEC.md       Domain spec -- source of truth, unchanged since Phase 0.
docs/HANDOFF.md    Phase 0-2 snapshot, superseded by this file.
tests/fixtures/    Real sample PDFs used by test_parsing.py and by the
                   Phase 4 live browser verification (see below).

  --- Phase 4: frontend, wired to the real backend ---
src/lib/api.ts      Typed client for every /api route. Sends X-Session-Id
                   on every call (hardcoded "office" -- see Design decisions
                   #5). After every mutating call, refetches /api/state and
                   returns it alongside the call's own result so callers can
                   push it straight into the React Query cache without a
                   second round trip. Mirrors state to localStorage; on any
                   404 it POSTs /api/state/restore with the mirror and
                   retries the original call once.
src/components/scheduler/
  InputsPanel.tsx    Real multi-file uploads (one /api/upload call per
                     file), the "Confirm Parsed Data" list wired to
                     /api/confirm, a constraints summary computed from
                     state, Generate button enabled once a timetable and an
                     outline are both parsed (see Design decisions #6 for
                     what Generate actually does now).
  TimetableGrid.tsx  Seven columns (Monday-Sunday), each a chronologically
                     sorted stack of cards rather than a fixed-hour grid
                     (see Design decisions #3 for why).
  EvaluationDetail.tsx  Opens on an assessment click; looks up the
                     originating Proposal in proposal_history by matching
                     date/start/end, shows its real reasons, and lets you
                     re-approve a different one of that request's other
                     candidates directly.
  AssistantPanel.tsx Real chat. Renders Proposal candidate cards and Impact
                     re-proposal cards with working Approve buttons; shows
                     an "Enter duration in minutes" hint chip and focuses
                     the input when the backend is awaiting duration.
  Sidebar.tsx        Export (downloads via fetch + blob, not a plain
                     anchor, since the download needs the X-Session-Id
                     header) and Import (multipart) in the footer.
src/routes/index.tsx  Orchestrates everything via TanStack Query: a
                     ["state"] query and a ["grid", weekStart] query, both
                     self-healing through api.ts's 404-recovery. Every
                     mutation invalidates ["grid"] too (state and grid can
                     both change together, e.g. a confirm can rename a
                     course_guess that the grid displays).
src/lib/schedule-data.ts  Deleted -- the mock data module nothing references
                     anymore.
vite.config.ts       Dev server proxies /api to http://localhost:7860.
eslint.config.js     Now ignores backend/ (it was linting the Python venv).
package-lock.json     Now committed (closes HANDOFF v1's gap #1 -- `npm
                     install` has been run for real).
```

## How to run things

```bash
# Backend
cd backend
python -m venv .venv
./.venv/Scripts/pip install -r requirements.txt      # Windows; use bin/ on mac/linux
./.venv/Scripts/python -m pytest -v                   # LLM tests skip cleanly without a key
GROQ_API_KEY=... ./.venv/Scripts/python -m uvicorn app.main:app --port 7860

# Frontend (separate terminal)
npm install            # needs Node on PATH -- see Known issues #1
npm run dev             # vite dev, defaults to :8080, proxies /api -> :7860
npm run build            # vite build (client + SSR + nitro); also run
npx tsc --noEmit         # the real type-check -- vite build alone does NOT
                          # type-check, it just transpiles (see Known issues #7)
npm run lint
npm run format
```

Then open `http://localhost:8080`. The manual curl walkthrough in
`backend/tests/test_orchestrator.py`'s module docstring still works
identically against port 7860.

Docker build remains **written but never executed** (same as HANDOFF v1) --
no Docker in this sandbox. The frontend build assumption
(`.output/public` as the static root) is now at least confirmed to be what
`vite build` actually produces (verified directly this phase, not assumed).

## Known issues / unfinished business

Ordered roughly by how much they'll bite the next person:

1. **Node.js is not on the persistent PATH.** It's installed at `D:\nodejs`
   but every new terminal needs `export PATH="/d/nodejs:$PATH"` (bash) or
   the PowerShell equivalent before `node`/`npm` work. A background task is
   already queued to fix this (add it to the user/system PATH) but has not
   been run as of this writing.

2. **`llm.py`'s retry backoff has no ceiling.** Confirmed live: this Groq
   account hit its daily token cap mid-Phase-3, and Groq's 429 response's
   implied wait time flowed straight into `time.sleep()` inside a live
   request handler, hanging an upload for over 80 minutes before it was
   killed manually. A background task is queued to cap the sleep and fail
   fast instead (`backend/app/llm.py` `_retry_delay`/`_chat`), but is not
   yet applied. Until it is, a live upload can hang for a very long time if
   the account is rate-limited -- if a request seems stuck, check for this
   before assuming something is broken.

3. **A live GROQ_API_KEY was pasted into chat twice** (once in Phase 3,
   once implicitly still present in the transcript through Phase 4). Same
   as HANDOFF v1's item #3: it was only ever used as an in-memory env var,
   never written to a file or committed, but **it should be rotated** in
   the Groq console if that transcript could ever be seen by anyone else.
   This has been flagged to the user repeatedly; unclear if acted on.

4. **Confirmation questions are per raw_label, not per course, when the
   LLM's `course_guess` extraction is inconsistent across day-chunks.**
   Confirmed live: the real timetable fixture produced separate identity
   questions for `EAB-JR-14`, `EAB-JR-15`, `EAB-JR-16`, `EAB-JR-17`,
   `EAB-JR-18` (5 different raw labels, all really the same "ABA" course)
   instead of one unified backstop question, because `parser.py`'s
   recurrence backstop (`_confirmation_questions`) groups by exact-string
   `course_guess`, and each independent day-chunk LLM call didn't always
   extract that string identically. Confirming one doesn't reconcile the
   others -- a user has to confirm each raw_label variant separately to
   fully clean up one course's identity. Not fixed; `parser.py` is Phase 1
   code, out of Phase 3/4's scope, but worth a look (e.g. fuzzy-matching
   raw labels sharing a prefix before the session-number suffix).

5. **The frontend has no automated test suite.** No Vitest/RTL setup.
   Phase 4 verification was `tsc --noEmit` + `eslint` + `vite build` +
   a real, manual, browser-driven walkthrough (see below) -- thorough, but
   one-off, not repeatable in CI. If this project matures, add component
   tests, especially around `src/lib/api.ts`'s 404-recovery and mirroring
   logic, which is exactly the kind of thing that silently breaks.

6. **Multi-session support doesn't exist in the UI.** `src/lib/api.ts`
   hardcodes `SESSION_ID = "office"`. The backend's session store supports
   arbitrary `X-Session-Id` values, but nothing in the frontend lets a user
   create, switch, or see a different session. Fine for a single-user
   prototype; would need real UI work otherwise.

7. **`vite build` does not type-check.** It transpiles/bundles via
   esbuild-family tooling and will happily "succeed" with real type errors
   present. Always run `npx tsc --noEmit` separately (or wire it into the
   build script) to actually catch them -- this bit nothing in this repo
   only because it was done manually every time this phase, not because
   the build script does it for you.

8. **`render.yaml` still doesn't exist.** Same as HANDOFF v1's item #7,
   still deferred.

## Design decisions worth knowing before you touch this code

These fill gaps neither SPEC.md nor the task prompts spelled out. Numbered
continuing from HANDOFF v1's list conceptually, but restarted at 1 since
this file stands alone.

1. **Sessions 404 until restored; only `/state/restore` and `/import`
   create one.** `SessionStore.get()` (auto-create) is now used only by
   those two routes. Every other route depends on `require_session()`,
   which calls `SessionStore.get_existing()` and 404s if the store has
   never seen that session id -- including after an in-memory process
   restart, since nothing persists `_sessions` across restarts. This exists
   specifically so the frontend's local mirror + 404-recovery pattern has
   something real to react to; before this change, `GET /api/state` on an
   unknown session silently returned an empty 200, which made the
   recovery flow dead code. Verified live: a brand-new session correctly
   404s, `POST /state/restore` creates it, and the retry succeeds.

2. **`adjust_rule` mutates `engine.py`'s module-level constants directly**
   (`engine.S1_WEIGHT`, `S2_WEIGHT`, `S3_WEIGHT`, `S2_MAX_PER_WEEK`), via
   plain `setattr`. This is process-wide, not per-session -- one session
   changing "assessments per week" affects every other session's proposals
   too. Deliberate, matching the same single-process scope already
   accepted for `engine._PROPOSALS` per HANDOFF v1's decision #4. Don't
   "fix" this into per-session state without also solving `_PROPOSALS`'s
   equivalent problem; they're the same class of limitation.

3. **The grid is seven columns of chronologically-stacked cards, not a
   fixed-hour-row grid.** The original mock used a rigid `TIMES` array of
   hourly rows (09:00, 10:00, ... 16:00) that doesn't fit real data: quiz
   windows start at 08:15, exams start at 08:15 or 09:00 with arbitrary
   durations, and teaching slots (09:00-10:10, 10:40-11:50, ...) don't
   align to clean hour boundaries. `TimetableGrid.tsx` instead renders each
   day as a vertically stacked, time-sorted list of cards, keeping the same
   card visual language (class/eval/holiday colors) but abandoning the
   fixed-row mechanism entirely. React keys for these rows are positional
   (`${date}-${kind}-${index}`), deliberately separate from the
   content-derived `entryKey()` used only for cross-refetch "is this the
   selected card" matching -- the same course/time recurs once per
   division, so a content-only key isn't unique (this caused a real,
   live-caught "duplicate key" React error; see commit `0727e59`).

4. **The orchestrator infers `scope` and `after_session` deterministically
   in Python, never from the LLM.** `intent.txt` (verbatim, per the task)
   never asks the classifier for `scope`, and SPEC explicitly says the LLM
   must never resolve calendar facts. `orchestrator._infer_scope` looks for
   any parsed `TimetableEntry` with a matching `course_guess` and
   `cohort_kind == minor`, defaulting to `"core"`; `_infer_after_session`
   regex-matches `"after session (\d+)"` against the matching course
   outline's evaluation `timing_note`. This closes HANDOFF v1's gap #4
   (item 3 specifically -- "`ScheduleRequest` needs a scope field...
   whoever builds the bridge will need to decide how to derive this").

5. **The frontend uses one fixed session id, sent explicitly.** Given no
   multi-session UI exists (Known issues #6), `SESSION_ID = "office"` is a
   constant in `api.ts` rather than the header being omitted and left to
   the backend's own default. The task explicitly asked for every call to
   carry the header, so it's sent every time even though today it's always
   the same value.

6. **The "Generate Evaluation Schedule" button doesn't generate a full
   schedule.** There's no backend endpoint for that -- the real system is
   chat-driven, one assessment at a time, via `schedule_request`. Clicking
   Generate instead sends a generic starter prompt into the chat (reusing
   the same lift-up-to-parent pattern the original mock used for
   `pendingQuestion`/`onAsk`), which the assistant answers for real. This
   is a deliberate, documented compromise given the acceptance journey
   itself is entirely chat-driven and never actually exercises a "generate
   everything" action -- worth revisiting if a real bulk-generate workflow
   turns out to be wanted.

7. **Live browser verification method.** Phase 4 was verified against a
   real running backend (port 7860) and a real `vite dev` server (port
   8080) using the in-app Browser tool. The two real PDF fixtures were
   temporarily copied into `public/` (removed afterward) so the browser
   could `fetch()` them directly; file inputs were driven by constructing
   real `File`/`DataTransfer` objects and dispatching a native `change`
   event on the hidden `<input>` -- the standard technique for this,
   equivalent to what Playwright's `setInputFiles` does under the hood.
   This is how the FileList race condition (Design decisions #3's sibling
   bug, Known issues n/a -- already fixed in `0727e59`) was actually
   caught: static review had missed it.

## Suggested next steps

In roughly the order they unblock each other:

1. Persist the Node.js PATH fix (background task already queued).
2. Cap `llm.py`'s retry sleep (background task already queued) -- do this
   before any more live testing, since it's the difference between a fast
   failure and an 80-minute hang under real rate limits.
3. Rotate the Groq key that's been pasted into chat.
4. Decide whether the per-raw_label confirmation-question granularity
   (Known issues #4) is worth smoothing over in `parser.py`, e.g. grouping
   by a normalized prefix (strip trailing `-\d+` / `(\d+)` session
   suffixes) before the recurrence backstop check.
5. If this moves toward a real deployment: `render.yaml`, an actual
   `docker build` run, and a frontend test suite (at minimum around
   `api.ts`'s recovery/mirroring logic).
6. If multi-user use is ever needed: real session UI, and reconsider
   `adjust_rule`'s process-wide mutation and `engine._PROPOSALS`'s
   in-memory-only lifetime together, since they're the same underlying gap.
