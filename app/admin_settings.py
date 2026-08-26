from __future__ import annotations

# Operator-supplied text appended after the base system prompt for each
# LLM-backed flow, edited via the Developer Options admin endpoints
# (app/main.py's /api/admin/settings). Keyed by prompt filename without
# ".txt", matching orchestrator.py's and parser.py's own _load_prompt().
#
# Deliberately additive, not a full prompt replacement: intent.txt's
# strict JSON-output contract is branched on programmatically by
# orchestrator.py, so a full overwrite could silently break that contract.
# Appending can degrade classification quality but can't remove the
# contract itself. In-memory only, like the rest of this project's runtime
# state -- resets to blank on every process restart/redeploy.
EXTRA_INSTRUCTIONS: dict[str, str] = {
    "intent": "",
    "narrate": "",
    "extract_outline": "",
}
