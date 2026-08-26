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
# state -- resets to these defaults on every process restart/redeploy.
#
# Pre-filled with genuinely useful starting instructions rather than blank
# strings -- each is a narrow addition that complements its base prompt
# without contradicting it (see app/prompts/*.txt). An operator can edit or
# clear any of these from Developer Options; clearing one reverts that flow
# to exactly the base prompt's behavior.
EXTRA_INSTRUCTIONS: dict[str, str] = {
    "intent": (
        "If a message is ambiguous between two possible actions, prefer asking a "
        "clarifying question (action: question) rather than guessing which one was meant."
    ),
    "narrate": (
        "Refer to the reader as \"you\". Refer to academic terms as \"Term\" followed by "
        "the number (e.g. \"Term IV\"), never \"semester\"."
    ),
    "extract_outline": (
        "If an evaluation's weightage is given as a range (e.g. \"10-15%\"), use the "
        "higher number. If multiple quizzes share one combined weightage, split it evenly "
        "across them unless the outline states otherwise."
    ),
}
