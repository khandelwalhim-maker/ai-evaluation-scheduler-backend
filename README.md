# AI Evaluation Scheduler -- Backend

FastAPI backend for the AI Evaluation Scheduler (SPJIMR PGDM programme). A
deterministic Python engine proposes assessment slots that minimize student
workload clustering; an LLM (via Groq) only parses uploaded PDFs, classifies
chat intent, and narrates the engine's own decisions -- it never picks a
date itself. See `docs/SPEC.md` for the full domain specification and
`docs/HANDOFF_V2.md` for architecture and design-decision notes.

This service is a pure JSON API. The frontend
([ai-evaluation-scheduler-frontend](https://github.com/khandelwalhim-maker/ai-evaluation-scheduler-frontend))
is a separate deployment that calls it cross-origin.

## Running locally

```bash
python -m venv .venv
./.venv/Scripts/pip install -r requirements.txt      # Windows; use bin/ on mac/linux
LLM_API_KEY=... ./.venv/Scripts/python -m uvicorn app.main:app --port 7860
```

```bash
./.venv/Scripts/python -m pytest -v   # LLM-backed tests skip cleanly without a key
```

Copy `.env.example` to `.env` (or export the same variables) to configure
`LLM_API_KEY` and, once you have a frontend URL, `CORS_ORIGINS`.

## API

Every route is under `/api` except `GET /health`. See `app/main.py` for the
full list: `upload`, `confirm`, `chat`, `schedule/approve`, `grid`, `state`,
`state/restore`, `export`, `import`, and `selfcheck` (a diagnostic endpoint
reporting whether `LLM_API_KEY` is set, whether the parse cache directory
is writable, and a one-token LLM connectivity check).

## Testing a deployment end to end

```bash
scripts/smoke.sh https://your-backend-url
```

Runs the real upload → confirm → chat → approve → grid → export journey
against a live instance using the fixtures in `tests/fixtures/`. Spends
real LLM tokens on the two uploads (parse results are cached by content
hash, so re-running it against the same fixtures on the same instance
doesn't re-spend).

## Deploying to Railway

1. Push this repo to GitHub.
2. In the Railway dashboard, New Project → Deploy from GitHub repo, and
   select this repository. Railway detects the `Dockerfile` automatically
   (`railway.json` pins the builder and health check explicitly).
3. Set the `LLM_API_KEY` variable in the service's Variables tab.
4. Once you have a frontend URL (from the Vercel deployment), set
   `CORS_ORIGINS` to that origin (comma-separated if there's more than
   one, e.g. a production domain and a preview deployment). It defaults to
   `*` so things work immediately, but that allows any site to call this
   API -- worth tightening once the frontend URL is known.
5. Railway injects `PORT` itself; the Dockerfile's `CMD` already respects
   it (`--port ${PORT:-7860}`).

Confirm the deployment with `scripts/smoke.sh https://<railway-url>`.
