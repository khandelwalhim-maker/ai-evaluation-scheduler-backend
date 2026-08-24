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
MODEL_FALLBACK = os.getenv("MODEL_FALLBACK", "openai/gpt-oss-120b")
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
