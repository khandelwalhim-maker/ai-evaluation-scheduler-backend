# Handoff v3: AI Evaluation Scheduler (two-repo deployment + deterministic parser)

Status snapshot as of **2026-08-24**. Supersedes `docs/HANDOFF_V2.md` (the
Phase 0-4 snapshot, still in this repo for history) and its own predecessor
`docs/HANDOFF.md`. Written so a new engineer, or a new AI session with no
memory of the work, can pick this up cold. Covers everything since v2:
splitting the monorepo into two deployed services, undo/remove upload UI,
an LLM fallback bug, a full day spent fighting Groq's rate limits and daily
quota, a provider switch to Mistral, and a complete rebuild of timetable
parsing from LLM-based to deterministic.

## What changed since v2, in one paragraph

v2 described one repo (`evaluation-studio-main/`) with a backend and
frontend that ran locally against each other. Since then the project became
two separately deployed services — a FastAPI backend on Railway and a
React/TanStack Start frontend on Vercel, each in its own private GitHub
repo — and the biggest technical change of this phase: **timetable PDF
parsing no longer calls an LLM at all.** It used to; that path failed in
four different ways in production (rate limits, an account-wide daily quota
that a fresh API key didn't fix, empty completions under JSON mode, and
output truncation), so it was replaced with a deterministic Python parser
that reads the same `pdfplumber`-extracted table directly. The LLM's role
narrowed to what's left: classifying chat intent, narrating results, and
parsing course-outline PDFs (short prose, never had these problems).

## Repo topology (this is the part that changed structurally)

Three directories now exist under `D:\Users\evaluation-studio-main\`:

- **`ai-evaluation-scheduler-backend/`** — the active backend. GitHub:
  `khandelwalhim-maker/ai-evaluation-scheduler-backend` (private). Deployed
  on Railway at `https://backend-production-72958.up.railway.app`. This
  repo, this file.
- **`ai-evaluation-scheduler-frontend/`** — the active frontend. GitHub:
  `khandelwalhim-maker/ai-evaluation-scheduler-frontend` (private). Deployed
  on Vercel at `https://ai-evaluation-scheduler-frontend.vercel.app`.
- **`evaluation-studio-main/`** — the original monorepo from v1/v2. Left in
  place, no longer the deployment target. Its `docs/HANDOFF.md` and
  `docs/HANDOFF_V2.md` are the pre-split history; `docs/SPEC.md` there is
  still the domain-rules source of truth and was copied into the backend
  repo unchanged (`docs/SPEC.md` here is identical).

Backend commits, oldest first:
```
9b30fdd  Initial commit: FastAPI backend split out for standalone Railway deployment
e44a2b2  Fix frontend repo cross-link in README
d62b6bf  Add endpoints to undo a wrong upload
57f8430  Fix silent no-op fallback escalation on parse failures
384b7f5  Fix Groq rate limiting on timetable uploads
ea8854c  Add diagnostic logging for timetable upload failures
387b651  Switch LLM provider from Groq to Mistral
b5443ff  Raise timetable parse max_tokens from 4000 to 12000
f73cd4c  Replace LLM-based timetable parsing with a deterministic grid parser
```

Frontend commits, oldest first:
```
dadecb1  Initial commit: React frontend split out for standalone Vercel deployment
fc0332e  Fix backend repo cross-link in README
252581d  Add UI to undo a wrong upload
```

**Deployment mechanics, learned the hard way:** pushing to GitHub does
**not** trigger a Railway deploy for this project's setup — only `railway
up` (CLI, run from the backend repo root) actually ships new code. Vercel,
by contrast, deploys automatically on a push to the connected branch once
`vercel git connect` has been run once. Don't assume a `git push` alone put
new backend code live; it didn't, several times this session.

## Architecture at a glance (backend)

```
app/
  main.py                 FastAPI app, CORS-enabled for the separate Vercel
                           origin (config.CORS_ORIGINS). No static-file
                           serving or SPA catch-all anymore -- this is a
                           pure JSON API now that the frontend is a
                           separate deployment. Routes (all under /api
                           except /health):
                             POST   /upload            kind=course_outline|timetable
                             DELETE /course/{index}     undo one course-outline upload
                             DELETE /timetable           undo/clear the whole timetable
                             POST   /confirm
                             POST   /chat
                             POST   /schedule/approve
                             GET    /grid?week_start=YYYY-MM-DD
                             GET    /state
                             POST   /state/restore
                             GET    /export
                             POST   /import
                             GET    /selfcheck          diagnostic: LLM key
                                     present?, parse cache writable?, one
                                     live LLM ping
  config.py                LLM_API_KEY, MODEL_PARSE, MODEL_NARRATE,
                           MODEL_FALLBACK, LLM_BASE_URL, CORS_ORIGINS.
                           Provider-agnostic names on purpose (see Design
                           decisions) -- currently pointed at Mistral:
                           LLM_BASE_URL=https://api.mistral.ai/v1,
                           MODEL_PARSE=ministral-8b-latest,
                           MODEL_FALLBACK/MODEL_NARRATE=ministral-3b-latest.

  --- parsing: timetable is now deterministic, course outlines still LLM ---
  timetable_grid_parser.py  NEW this phase. The whole deterministic grid
                           parser -- see its own section below.
  parser.py                parse_course_outline (still LLM-based, MODEL_PARSE,
                           unchanged) and parse_timetable (now a thin wrapper
                           calling timetable_grid_parser.parse_timetable_grid,
                           then the existing _confirmation_questions and
                           merge_timetables, both untouched).
  pdf_extract.py           extract_text_generic, extract_grid, extract_grid_chunks
                           (unchanged, still used by course-outline parsing
                           and OCR), plus new extract_grid_bands (raw
                           list[list[str]] rows per day-band, what the
                           deterministic parser actually consumes).
  cache.py                 Unchanged. SHA-256 file-hash cache under
                           /tmp/parse_cache/.
  llm.py                   LLMClient over httpx. RATE_LIMIT_ATTEMPTS raised
                           3->5, MAX_RETRY_DELAY raised 5.0->20.0s this
                           phase (see Known issues). Still used by course-
                           outline parsing and by orchestrator.py's chat/
                           narration calls -- not used by timetable parsing
                           at all anymore.
  calendar_summary.py       NEW this phase. build_calendar_markdown(state) --
                           a compact markdown listing of holidays and
                           existing assessments by date, pulled from
                           CalendarState. Fed into orchestrator._build_context
                           as a new "calendar_summary" key so the chat LLM
                           has a grounded view of what's already scheduled,
                           without re-deriving it from raw structure.
  orchestrator.py           Unchanged except _build_context gaining the
                           calendar_summary key above. The IntentResult ->
                           Python dispatch -> engine.py loop is exactly as
                           v2 described it -- see that file for the full
                           mechanics, still accurate.
  engine.py, workload.py,
  timeline.py, session.py   Unchanged since v2.

tests/
  test_timetable_grid_parser.py  NEW. Band-count regression guard (locks in
                           the real-world quirk that drove the forward-fill
                           design -- see below), dates/scope/banner/tbc/
                           multi-session/punctuation-tolerance assertions
                           against both real fixtures, a header-format-error
                           guard test.
  test_calendar_summary.py NEW. Empty-state sentinel, populated-state content
                           checks.
  test_parsing.py          test_parse_timetable no longer needs an LLM key
                           at all (dropped @requires_llm_key) -- it's fully
                           deterministic now. test_parse_course_outline
                           still does.
  test_orchestrator.py     One addition: _build_context includes a non-
                           empty calendar_summary key.
  fixtures/timetable_week14.pdf  NEW real-world fixture. Critical: this
                           file's minor-cohort row labels (Mktg/Ops & S C/
                           Fin/IMA/Consulting) only print on Monday's band;
                           Tuesday-Sunday's IMA/Consulting rows are
                           physically absent from the table (not blank --
                           absent), and A/B/C rows carry no label at all on
                           those days. week13.pdf alone would never have
                           surfaced this; both fixtures together are what
                           make the test suite trustworthy.
```

## The deterministic timetable grid parser, in plain terms

`pdfplumber.extract_tables()` already recovers the timetable as clean rows
and columns -- it always did. The old design asked an LLM to re-derive that
structure from a text serialization of it, which is why it kept failing.
`timetable_grid_parser.parse_timetable_grid(path)` reads the table directly:

- **Header parsing** locates the DIV/CR/LUNCH columns by keyword match
  against the header text (not fixed position), and the time-slot columns
  by a regex tolerant of the real header text's inconsistent `.`/`:`
  separators and line-wraps. Raises `TimetableGridFormatError` if the
  header doesn't look like the expected template at all, rather than
  silently producing garbage -- a future SPJIMR format change will fail
  loudly.
- **Date/weekday extraction** concatenates every row's date-column text
  within a day-band and regex-searches the combined string, because the
  date marker's row position genuinely drifts across real bands (same
  cell, split across two rows two different ways, at different row
  indices in different bands) -- confirmed by hand-tracing both fixtures.
- **Course-code parsing** is one unified regex pipeline: leading all-caps
  token as the course code, session number(s) from `(N)`, `(N) (M)`,
  `N & M`, or trailing `-N`/`_N`, `tbc` detection, banner-keyword
  detection (`end term`, `mid term`, `exam`, `quiz`). Professor initials
  are never parsed -- `TimetableEntry` has no field for them.
- **Row/band assembly**: Division letters D and E are dropped entirely,
  not flagged low-confidence -- `docs/SPEC.md` explicitly says
  "Out of scope: PGDM-Business Management (Divisions D and E, Strategy
  minor)", and those rows are populated with real course codes in the
  source PDFs, so this is a deliberate exclusion, not a gap. Divisions A/B/C
  each produce **two** entries per populated cell (one `cohort_kind=division`,
  one `cohort_kind=minor`) -- required, not stylistic, because it's the only
  way `state.cohorts.divisions` ever gets populated; without it every
  conflict check in `engine.py` (H4, S1-S4) silently no-ops. Minor-cohort
  labels are forward-filled, keyed by division letter (not row position,
  which breaks under week14.pdf's variable band length) from the most
  recent non-blank label seen anywhere earlier in the document.
- Anything that doesn't cleanly parse gets a low-confidence or
  `entry_kind=unknown` entry rather than a silent guess, which routes
  through the existing, unchanged `_confirmation_questions` mechanism.

One side effect worth knowing: this incidentally fixed a v2 known issue.
v2 documented that independent per-day-chunk LLM calls sometimes extracted
`course_guess` inconsistently, so the recurrence backstop (which groups by
exact-string `course_guess`) produced five separate confirmation questions
for `EAB-JR-14` through `EAB-JR-18` instead of one. The deterministic
parser applies the same regex function to every cell, so extraction is
consistent by construction -- that specific failure mode no longer exists.

## Known issues / unfinished business

Ordered roughly by how much they'll bite the next person.

1. **Multiple API keys were pasted into chat this session and need
   rotation.** Three different Groq keys (progressively rotated as each
   one hit rate limits or quota) and one Mistral key, all in plaintext in
   this transcript. None were committed to git (verified before every
   commit), but if this transcript is ever visible to anyone else, rotate
   all four in the Groq console and the Mistral console. This has now been
   flagged in three consecutive handoff documents; still unclear if acted
   on.

2. **Groq's free-tier daily token quota is scoped to the whole account,
   not the individual API key.** Confirmed live: generating a fresh Groq
   key on the same account did not reset the quota -- the very first call
   with the "new" key still reported ~197k/200k tokens used for the day.
   If Groq is ever reintroduced as a provider option, remember this before
   assuming a key rotation buys headroom.

3. **The deterministic parser is tuned to this exact SPJIMR timetable
   template**: the 15-column half-day-split structure (separate morning/
   afternoon DIV, CR, and time columns), the 7-row-per-day pattern, and
   the specific course-code punctuation conventions observed in two real
   files. A genuinely different future format (different column count,
   different row order, a scanned/non-tabular PDF) will raise
   `TimetableGridFormatError` and hard-fail the upload with a 502, rather
   than falling back to OCR+LLM the way the old path did. This is a
   deliberate tradeoff (reliability over flexibility was the entire point
   of the rewrite) but worth knowing if the registrar's office ever sends
   something unusual.

4. **`llm.py`'s retry ceiling was raised, not removed.**
   `RATE_LIMIT_ATTEMPTS` is now 5 (was 3), `MAX_RETRY_DELAY` is now 20.0s
   (was 5.0s) -- this was in response to short-lived per-minute rate
   limits, and does help those. It does nothing for an account-wide daily
   quota exhaustion (item 2 above), which needs minutes-to-hours, not
   seconds, so a request can still take a genuinely long time (up to
   `5 attempts x 20s` per call) before failing, or before falling back to
   `MODEL_FALLBACK`. This only affects course-outline parsing and chat now,
   since timetable parsing no longer calls the LLM at all.

5. **Everything from v2's Known Issues list that wasn't touched this
   phase is presumably still open**: multi-session support doesn't exist
   in the frontend UI (`SESSION_ID` is still a hardcoded `"office"`
   constant); no automated frontend test suite; `adjust_rule` still
   mutates `engine.py`'s module-level constants process-wide, and
   `engine._PROPOSALS` is still in-memory-only. None of these were
   revisited this phase. The Node.js PATH issue v2 flagged is very likely
   resolved in practice (frontend build/lint/deploy commands ran
   successfully many times this session) but was never explicitly
   re-verified as fixed.

## Design decisions worth knowing before you touch this code

Continuing conceptually from v2's list, restarted at 1 since this file
stands alone.

1. **`LLM_API_KEY`/`LLM_BASE_URL`/`MODEL_*` are provider-agnostic names on
   purpose**, renamed this phase from `GROQ_API_KEY` and Groq-specific
   defaults. `LLMClient` only ever speaks the OpenAI-compatible
   chat/completions shape both Groq and Mistral implement, so the actual
   provider swap (once the need became clear) was a config-only change:
   new base URL, new key, new default model names. If a third provider is
   ever needed, the same pattern should hold, provided it speaks the same
   request/response shape.

2. **The AI's role was deliberately narrowed, not removed.** It no longer
   does raw structured extraction from timetable PDFs at all. What's left:
   classifying one chat message into an `IntentResult` (unchanged from
   v2), narrating an already-computed `Proposal`/`Impact` into prose
   (unchanged from v2), parsing course-outline PDFs (short prose, kept on
   the LLM path since it never had the timetable path's failure modes),
   and now reading a markdown `calendar_summary` as grounding context for
   chat. The "AI proposes, Python applies" loop `orchestrator.py` already
   had (per v2) is completely unchanged; this phase only added one field
   to what it feeds the model.

3. **Divisions D and E are silently dropped, not flagged.** This was
   validated directly against `docs/SPEC.md`'s explicit scope statement
   before implementing it, not inferred. If a future reader wonders why
   ~2/7 of every timetable row disappears, that's why, and it's correct.

4. **Dual-emission (division entry + minor entry per populated cell) is
   required, not a style choice.** Removing it would silently break every
   H4/S1-S4 conflict check in `engine.py`, since those depend on
   `state.cohorts.divisions` actually being populated. The existing
   `merge_timetables` dedupe key (`cohort_id or row_label`, `raw_label`)
   already disambiguates the division vs. minor variant of the same cell
   without collision, so this didn't require any change downstream.

5. **Forward-fill for minor-cohort labels is keyed by division letter,
   not row position.** An earlier design (row-index-based forward-fill)
   was caught as wrong specifically because `timetable_week14.pdf` has
   variable band length (7 rows on Monday, 5 rows every other day) --
   row-position 3 means something different depending on the day. This
   was caught during planning, before it shipped, by extracting and
   hand-tracing a second real fixture rather than trusting the first one.

## Suggested next steps

In roughly the order they unblock each other.

1. Rotate every exposed API key (three Groq, one Mistral) -- see Known
   issues #1. This should happen before anything else, regardless of what
   else is worked on next.
2. Investigate why Railway doesn't auto-deploy from a GitHub push for this
   project, so `railway up` isn't a manually-remembered step every
   deployment. Likely a dashboard configuration gap (build trigger not
   wired to the GitHub integration), not something wrong with the code.
3. If the registrar's office is likely to ever send a scanned or
   differently-formatted timetable, decide whether the deterministic
   parser should regain some kind of fallback (even just OCR-then-flag-
   for-manual-entry, not necessarily back to the LLM) rather than a hard
   502.
4. Everything still open from v2 that this phase didn't touch: real
   multi-session UI, a frontend test suite (at minimum around
   `api.ts`'s recovery/mirroring logic), and reconsidering
   `adjust_rule`'s process-wide mutation together with
   `engine._PROPOSALS`'s in-memory-only lifetime, since they're the same
   underlying limitation.
