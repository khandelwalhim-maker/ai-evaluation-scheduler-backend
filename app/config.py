import logging
import os

logger = logging.getLogger(__name__)

# MODEL_PARSE and MODEL_NARRATE originally defaulted to llama-3.3-70b-versatile
# and llama-3.1-8b-instant; Groq has since decommissioned both (confirmed via a
# live /models call), so the defaults below point at models actually served by
# the current Groq catalog.
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_PARSE = os.getenv("MODEL_PARSE", "openai/gpt-oss-120b")
MODEL_NARRATE = os.getenv("MODEL_NARRATE", "openai/gpt-oss-20b")
# llm.py's fallback-escalation logic (_chat/_complete_json_with_model) skips
# straight to re-raising instead of retrying whenever `model == MODEL_FALLBACK`
# already, so this must default to something other than MODEL_PARSE or the
# "retry against a fallback on failure" safety net silently never fires --
# confirmed live: Groq occasionally returns a 400 json_validate_failed with
# an empty completion under json_mode, and with MODEL_FALLBACK equal to
# MODEL_PARSE that surfaced straight to the user with zero retries.
MODEL_FALLBACK = os.getenv("MODEL_FALLBACK", "openai/gpt-oss-20b")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
TIMEZONE_NOTE = "All datetimes are timezone-naive Indian Standard Time by convention"

# The frontend is a separate deployment (e.g. a Vercel project) calling this
# API cross-origin, so its origin(s) must be explicitly CORS-allowed. Comma
# separated, e.g. "https://myapp.vercel.app,https://myapp-git-main.vercel.app".
# Defaults to allowing any origin so local development and preview
# deployments work out of the box; set this explicitly in production.
_cors_env = os.getenv("CORS_ORIGINS", "*").strip()
CORS_ORIGINS = ["*"] if _cors_env == "*" else [o.strip() for o in _cors_env.split(",") if o.strip()]

if not GROQ_API_KEY:
    logger.warning(
        "GROQ_API_KEY is not set. LLM-backed features (document parsing, chat "
        "intent, narration) will fail until it is configured in the environment."
    )
