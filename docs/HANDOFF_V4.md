# Handoff v4: AI Evaluation Scheduler (course registry + Developer Options)

Status snapshot as of **2026-08-27**. Supersedes `docs/HANDOFF_V3.md` (the
two-repo-split + deterministic-parser snapshot, still in this repo for
history) and its predecessors `docs/HANDOFF_V2.md`/`docs/HANDOFF.md`.
Written so a new engineer, or a new AI session with no memory of the work,
can pick this up cold. Covers everything since v3: a course registry that
resolves timetable codes to real names, a "Developer Options" panel for
runtime LLM/prompt configuration, a real security decision that was made
and then explicitly reversed mid-phase, and a fresh instance of this
project's recurring API-key-exposure problem.

## What changed since v3, in one paragraph

Two features shipped, both live in production. The **course registry**
(`app/course_registry.py`, new `course_registry` field on `CalendarState`)
lets the operator maintain a standing abbreviation-to-course-name mapping
that the timetable parser consults before generating "Confirm Parsed Data"
identity questions — download a template prefilled with whatever codes are
still unmapped, or add/edit/remove one mapping at a time inline in the UI,
no per-term redo required. **Developer Options** (a same-page slide-over
panel, not the route it started as) lets the operator change the LLM
provider config and per-prompt "extra instructions" at runtime, no
redeploy. It shipped with an `ADMIN_TOKEN` gate first — justified given
this backend has zero auth anywhere else and one of the editable fields
(`LLM_BASE_URL`) is a real key-exfiltration vector if pointed at a server
an attacker controls — then the gate was **explicitly removed at the
user's direction** after that exact risk was explained to them. It is not
coming back unless asked for again; see Known issues #2 and Design
decision #4 before "fixing" this.

## Repo topology

Unchanged from v3 — still the same two repos, same deployment targets,
same monorepo left in place for history:

- **`ai-evaluation-scheduler-backend/`** — Railway,
  `https://backend-production-72958.up.railway.app`. This repo, this file.
- **`ai-evaluation-scheduler-frontend/`** — Vercel,
  `https://ai-evaluation-scheduler-frontend.vercel.app`.
- **`evaluation-studio-main/`** — pre-split monorepo, `docs/SPEC.md` there
  is still the domain-rules source of truth, unchanged, still identical to
  the copy in this repo.

Backend commits this phase, oldest first:
```
36d5b96  Add course registry and admin-gated Developer Options settings
bb04e98  Remove Developer Options access gate; pre-fill default instructions
```

Frontend commits this phase, oldest first:
```
1069198  Add course registry table and Developer Options page
8ec939e  Move Developer Options into a same-page panel; remove access gate
```

Both `railway up` (backend) and a plain push (frontend, Vercel auto-deploys)
were used and reconfirmed working exactly as v3 described — see that file's
"Deployment mechanics" note, still accurate, plus a sharper lesson learned
this phase: **`/health` alone is not evidence a new deploy landed.** Railway
keeps the old container answering requests during a rolling deploy, so
`/health` returned 200 for a stale build throughout the whole build window.
The reliable check is a route that only exists in the new code (e.g. hit
the newly-added endpoint and confirm it isn't a 404) polled alongside
`railway status` until it reads "Online" with no "Building"/"Deploying"
suffix.

## Architecture at a glance (backend)

```
app/
  main.py                  Routes added this phase (all under /api):
                              GET    /admin/settings
                              POST   /admin/settings
                              POST   /admin/settings/test
                              POST   /course-registry            bulk CSV/XLSX upload
                              GET    /course-registry/template    ?format=csv|xlsx
                              DELETE /course-registry              clear all
                              PUT    /course-registry/{abbreviation}  add/edit one
                              DELETE /course-registry/{abbreviation}  remove one
                            None of the /admin/* or /course-registry/*
                            routes have any auth -- same as every other
                            route in this API (see Design decisions #4).
                            Three pre-existing routes (/upload, /chat,
                            /selfcheck) had their exception handlers
                            changed to stop putting raw LLMError text in
                            the client-visible response body -- see "The
                            key-leak mechanism" section below.

  course_registry.py        NEW this phase. parse_registry_upload (CSV via
                            stdlib csv, XLSX via openpyxl; row 0 always a
                            header, a row counts only if BOTH columns are
                            non-blank -- this is also what safely skips a
                            template's blank-abbreviation reference rows
                            with no separate marker needed), 
                            build_registry_template (prefills the
                            abbreviation column from *unresolved* codes
                            actually seen in the timetable, not from
                            course-outline names -- those are a different
                            vocabulary; falls back to literal examples on
                            a fresh session), normalize_abbreviation and
                            is_unresolved_code (shared between the bulk
                            parser and the single-entry endpoints, and
                            between the template's prefill query and the
                            resolution check in parser.py).

  admin_settings.py         NEW this phase. EXTRA_INSTRUCTIONS: dict[str,str]
                            keyed by prompt filename without ".txt"
                            (intent/narrate/extract_outline). Pre-filled
                            with real starting instructions, not blank --
                            see Design decisions #3. Consulted by
                            _load_prompt() in both orchestrator.py and
                            parser.py, which append it after the base
                            prompt when non-empty.

  parser.py                 parse_timetable(path, course_registry=None) --
                            the registry param is new. Registry resolution
                            and confirmation-question generation now run
                            OUTSIDE cached_parse's boundary deliberately --
                            see Design decisions #1. _confirmation_questions
                            takes the same optional param and resolves
                            identity in the same loop that checks
                            confidence, so a registry hit suppresses the
                            question even when confidence was low -- see
                            Design decisions #2. _load_prompt now appends
                            admin_settings.EXTRA_INSTRUCTIONS["extract_outline"].

  orchestrator.py           _load_prompt now appends
                            admin_settings.EXTRA_INSTRUCTIONS["intent"/"narrate"].
                            Nothing else changed -- the IntentResult ->
                            Python dispatch -> engine.py loop is exactly as
                            v2/v3 described it.

  schemas.py                CalendarState gained course_registry: dict[str,str].

  config.py                 ADMIN_TOKEN was added this phase, then removed
                            again in the same phase when the gate was
                            reversed -- it does not exist in the current
                            code. Don't be surprised if you find it
                            mentioned in intermediate commits; HEAD has it
                            fully gone, not just unused.

  llm.py                    Unchanged. Still the file whose _chat() raise
                            site is the literal leak mechanism described
                            below -- worth rereading if you touch error
                            handling here.

requirements.txt            + openpyxl (XLSX read/write for the registry
                            template/upload).

tests/
  test_course_registry.py   NEW. Module-level tests for parse_registry_upload
                            /build_registry_template (case-collision
                            last-write-wins, blank-row skipping, xlsx
                            round-trip through a second reference sheet)
                            plus route-level tests for the single-entry
                            PUT/DELETE endpoints.
  test_admin.py             NEW. No auth tests (there's no auth) --
                            documents the open-by-design state via
                            test_admin_settings_accessible_without_any_token
                            rather than leaving its absence unexplained.
                            Covers key masking, blank-is-no-op for model
                            fields vs blank-does-clear for extra
                            instructions, and the settings-test probe's
                            error classification (auth/rate-limit/network
                            failure all collapse to a safe status, never
                            raw provider text).
  test_parsing.py           +2 tests: registry resolves the real fixture's
                            documented EAB/ABA identity questions (the
                            exact EAB-JR-14..18 case v2/v3 already
                            document), and an unrelated registry entry
                            doesn't suppress unrelated questions.
```

## The key-leak mechanism, found and partially closed this phase

v3's Known issue #1 said four keys leaked into chat transcripts across past
sessions but never identified the actual code-level mechanism. This phase
found it: `llm.py`'s `_chat()` raises
`LLMError(f"LLM request failed with {status}: {response.text}")` — the
**full, untruncated upstream provider response body** — and that message
was being forwarded verbatim to the browser from three places: the upload
handler's 502 detail, the chat handler's 502 detail, and (worse)
`/selfcheck`'s `llm_ping.detail`, which has **no auth at all** and is
publicly reachable. That's exactly the kind of response a human debugging
"why did my upload fail" pastes into a chat session for help — reproducing
the leak regardless of whether the key appears verbatim in `response.text`.

All three sites now use a static client-facing message; the full exception
still reaches the operator via the existing `logger.error` call at each
site (Railway logs), which is the right audience for upstream provider
detail. The new `/admin/settings/test` probe was built with the same
protection from day one: `_classify_llm_error()` in `main.py` turns any
failure — auth, rate limit, a non-`LLMError` network exception `_chat()`
doesn't wrap in its own try/except — into one of `ok`/`auth_error`/
`rate_limited`/`other_error`, never the raw text.

**This does not mean the key-leak problem is solved**, only that this one
code path is closed. See Known issues #1 below.

## Known issues / unfinished business

Ordered roughly by how much they'll bite the next person.

1. **Six API keys now need rotation, not four.** v3's original four (three
   Groq, one Mistral) are still presumably unrotated — still unclear if
   ever acted on, now flagged in four consecutive handoff documents. This
   phase added two more: running `railway variables` (no flags) during a
   session to confirm `ADMIN_TOKEN` had landed printed the live
   `LLM_API_KEY` and a leftover, unused `GROQ_API_KEY` in full into that
   session's transcript. Same mechanism, fresh instance. `railway variable
   --help` itself warns "avoid sharing command output from
   secret-bearing variable commands" — prefer `railway variable set
   --stdin --skip-deploys` for writes, and avoid `railway variable list`/
   `railway variables` unless there's no other way to answer the question.

2. **Developer Options has no access control, by deliberate, informed
   decision — do not silently re-add it.** It shipped gated
   (`ADMIN_TOKEN`, fail-closed dependency, masked key on read, a
   self-service rotation endpoint) specifically because this backend has
   no other auth and `LLM_BASE_URL` is attacker-redirectable to exfiltrate
   the live key on every subsequent request, not just read once. That risk
   was stated plainly to the user before removal; they confirmed "yes,
   remove it entirely" anyway, in the same session. The previous
   implementation is fully recoverable from git history (backend commit
   `36d5b96` has it; `bb04e98` removes it) if a future session is asked to
   bring it back — but that should be a fresh, explicit ask, not an
   assumption that the current open state is a bug.

3. **Registry resolution is not retroactive.** Adding or editing a mapping
   only affects timetable entries parsed *after* the registry already has
   that entry — it does not walk back through `session.calendar.dates` or
   the existing `confirmation_queue` to resolve entries from a prior
   upload. The mitigation is that the template's abbreviation column is
   computed from *currently unresolved* codes, so re-downloading it after
   a partial fill only asks about what's actually still missing — but a
   confirmation question already sitting in the queue from before the
   registry existed will sit there until manually confirmed or the
   timetable is cleared and re-uploaded.

4. **Developer Options settings are in-memory only, same lifetime as
   session state.** A Railway redeploy resets `config.LLM_API_KEY`/
   `LLM_BASE_URL`/`MODEL_*` to whatever the Railway env vars say, and
   `admin_settings.EXTRA_INSTRUCTIONS` back to its coded-in defaults. The
   UI says this directly; worth knowing before assuming a change is
   permanent.

5. **Everything from v3's Known Issues that wasn't touched this phase is
   presumably still open**: the deterministic parser's hard-fail-on-
   format-change behavior, `llm.py`'s retry ceiling being raised rather
   than removed, Groq's account-wide daily quota (moot unless Groq is
   reintroduced — nothing this phase touched provider choice), and
   everything in v3's own #5 (multi-session UI, no frontend test suite —
   still true, this phase only added *backend* tests, `adjust_rule`'s
   process-wide mutation, `engine._PROPOSALS`'s in-memory lifetime).

## Design decisions worth knowing before you touch this code

Continuing conceptually from v3's list, restarted at 1 since this file
stands alone.

1. **Registry resolution runs outside `cached_parse`'s boundary,
   deliberately.** `cached_parse` (`cache.py`) keys purely on file hash +
   kind, with no awareness of the registry. Resolving inside the cached
   `compute()` closure would mean re-uploading the same timetable PDF after
   updating the registry silently returns a stale, pre-registry result —
   the exact kind of bug that's invisible until someone notices identity
   questions that should be gone aren't. `parse_timetable` caches only the
   raw grid-parse; `_confirmation_questions` (which does the registry
   lookup) runs fresh on every call against that cached-or-fresh result.

2. **A registry hit overrides the confidence heuristic for identity
   specifically, not confidence in general.** `_confirmation_questions`
   resolves `course_guess` against the registry and gates the identity
   question on `not known`, regardless of the cell's numeric confidence
   score — because a human-supplied mapping is a stronger identity signal
   than a heuristic that was tuned for session-number ambiguity, not
   identity. Cohort-unknown and "tbc"-session checks are separate `if`
   blocks, untouched by this — registry resolution only ever removes
   "identity" questions.

3. **Prompt customization is additive-only, never a full overwrite.**
   `EXTRA_INSTRUCTIONS` text is appended after each base prompt, never
   replaces it — `intent.txt`'s strict JSON-output contract is branched on
   programmatically by `orchestrator.py`, so a full overwrite could
   silently break that contract; appending can only degrade classification
   quality, never remove the contract. This is also why the three fields
   ship with real default text instead of blank strings: a curated
   addition is safe by construction, a curated *replacement* would not be.

4. **Developer Options' current no-auth state is a reversal, not an
   oversight — see Known issues #2 for the full reasoning.** If asked to
   revisit this, the right question to ask first is *why now* (is this
   moving beyond a course project? is the URL more widely known?), not to
   assume the gate should simply go back to how it was.

5. **Config mutation happens directly on `app.config`'s module attributes**
   (`config.MODEL_PARSE = payload.model_parse`, etc.), not a separate
   settings object. This works because every consumer — `llm.py`,
   `orchestrator.py`, `parser.py`, `main.py` — reads `config.X` via live
   attribute access (`from app import config` then `config.X`), never
   `from app.config import X`, so a mutation takes effect everywhere
   immediately with zero restart. This is the same pattern
   `orchestrator.py`'s pre-existing `adjust_rule` intent already used on
   `engine.py`'s constants — Developer Options' settings endpoints just
   apply it to LLM config too, rather than introducing a new mechanism.

## Suggested next steps

In roughly the order they unblock each other.

1. Rotate all six exposed API keys — see Known issues #1. Standing
   recommendation, now flagged in four consecutive handoff documents.
2. Close the retroactive-resolution gap (Known issues #3) if it becomes a
   recurring annoyance in practice — re-scanning `session.calendar.dates`
   and the existing `confirmation_queue` against an updated registry is a
   bounded, well-scoped addition, not a redesign.
3. If Developer Options' open-access posture ever needs to change,
   reintroducing a gate is a small, well-understood change with a working
   reference implementation already in git history (commit `36d5b96`) —
   but confirm the actual trigger for wanting it back before assuming the
   old design is still the right one.
4. Everything still open from v3 that this phase didn't touch (see Known
   issues #5), unchanged priority order from that file.
