#!/usr/bin/env bash
# End-to-end smoke test against a running deployment: health, selfcheck,
# upload both fixtures, confirm, a chat schedule request, its duration
# follow-up, approval, a grid fetch, and export. Exits non-zero on the
# first failure so it is safe to gate a deploy on.
#
# This is not a mocked test: it spends real LLM tokens against a real
# GROQ_API_KEY (parse results are cached by SHA-256 under the server's
# parse-cache directory, so re-running it against the same fixtures does
# not re-spend on the parts that hit cache).
#
# Usage:
#   scripts/smoke.sh <base-url>
#   scripts/smoke.sh http://localhost:7860
#   scripts/smoke.sh https://ai-evaluation-scheduler-backend.up.railway.app

set -u

BASE_URL="${1:-}"
if [ -z "$BASE_URL" ]; then
  echo "Usage: $0 <base-url>" >&2
  exit 2
fi
BASE_URL="${BASE_URL%/}"

command -v curl >/dev/null 2>&1 || { echo "smoke.sh requires curl" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "smoke.sh requires python3" >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURES_DIR="$REPO_ROOT/tests/fixtures"
# A dedicated, timestamped session so this never touches the "office"
# session the frontend defaults to (ai-evaluation-scheduler-frontend's
# src/lib/api.ts) -- running smoke.sh right before a live demo must not
# leave approved test assessments sitting in the presenter's session.
# Sessions 404 until restored (app/main.py's require_session), so step 3
# below creates this one explicitly.
SESSION_ID="smoke-$(date +%s)-$$"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# jget <json-file> <python-expression-on-"data"> -- small helper so this
# script doesn't need jq, only curl and python3 (both already required by
# this project).
jget() {
  python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
try:
    result = eval(sys.argv[2])
except Exception as exc:
    print(f'<error evaluating {sys.argv[2]!r}: {exc}>', file=sys.stderr)
    sys.exit(1)
print(result)
" "$1" "$2"
}

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

step() {
  echo ""
  echo "==> $1"
}

# request METHOD PATH [curl-args...] -- writes the body to $WORKDIR/last.json
# and prints the HTTP status code. Render free instances take up to a
# minute to wake from spin-down, hence the generous timeout.
request() {
  local method="$1" path="$2"
  shift 2
  curl -s -o "$WORKDIR/last.json" -w "%{http_code}" --max-time 90 \
    -X "$method" -H "X-Session-Id: $SESSION_ID" "$@" "$BASE_URL$path"
}

expect_status() {
  local got="$1" want="$2" label="$3"
  if [ "$got" != "$want" ]; then
    echo "    response body: $(cat "$WORKDIR/last.json")" >&2
    fail "$label expected HTTP $want, got $got"
  fi
  echo "    OK ($label: HTTP $got)"
}

step "GET /health"
status=$(request GET /health)
expect_status "$status" 200 "health"

step "GET /api/selfcheck"
status=$(request GET /api/selfcheck)
expect_status "$status" 200 "selfcheck"
key_present=$(jget "$WORKDIR/last.json" "data['groq_api_key']['present']")
[ "$key_present" = "True" ] || fail "selfcheck: GROQ_API_KEY not set on the server -- export it before running smoke.sh"
echo "    GROQ_API_KEY present"

step "POST /api/state/restore (bootstrap session $SESSION_ID)"
status=$(request POST /api/state/restore -H "Content-Type: application/json" -d '{}')
expect_status "$status" 200 "state/restore"

step "POST /api/upload (course_outline fixture)"
status=$(request POST /api/upload \
  -F "kind=course_outline" \
  -F "file=@$FIXTURES_DIR/aba_course_outline.pdf;type=application/pdf")
expect_status "$status" 200 "upload course_outline"

step "POST /api/upload (timetable fixture)"
status=$(request POST /api/upload \
  -F "kind=timetable" \
  -F "file=@$FIXTURES_DIR/timetable_week13.pdf;type=application/pdf")
expect_status "$status" 200 "upload timetable"

# The real timetable fixture raises an EAB (raw timetable code) vs. ABA
# (course outline code) identity question -- see tests/test_orchestrator.py's
# module docstring, the same walkthrough this script mirrors and extends
# with health/selfcheck/export.
step "POST /api/confirm (EAB is ABA)"
status=$(request POST /api/confirm -H "Content-Type: application/json" \
  -d '{"context": "EAB", "resolution": "ABA"}')
expect_status "$status" 200 "confirm"

step "POST /api/chat (schedule the ABA end term exam)"
status=$(request POST /api/chat -H "Content-Type: application/json" \
  -d '{"message": "Please schedule the ABA end term exam"}')
expect_status "$status" 200 "chat schedule request"
awaiting=$(jget "$WORKDIR/last.json" "','.join(data.get('awaiting', []))")
[ "$awaiting" = "duration_minutes" ] || fail "chat schedule request: expected to be asked for duration_minutes (SPEC H3: never assume a default), got awaiting=[$awaiting]"
echo "    correctly asked for duration instead of assuming one"

step "POST /api/chat (90 minutes)"
status=$(request POST /api/chat -H "Content-Type: application/json" \
  -d '{"message": "90 minutes"}')
expect_status "$status" 200 "chat duration answer"
candidate_count=$(jget "$WORKDIR/last.json" "len(data['proposal']['candidates']) if data.get('proposal') else 0")
[ "${candidate_count:-0}" -ge 1 ] 2>/dev/null || fail "chat duration answer: expected at least one ranked candidate, got: $candidate_count"
proposal_id=$(jget "$WORKDIR/last.json" "data['proposal']['id']")
candidate_date=$(jget "$WORKDIR/last.json" "data['proposal']['candidates'][0]['date']")
echo "    $candidate_count candidate(s) ranked, top candidate on $candidate_date"

step "POST /api/schedule/approve (top-ranked candidate)"
status=$(request POST /api/schedule/approve -H "Content-Type: application/json" \
  -d "{\"proposal_id\": \"$proposal_id\", \"candidate_index\": 0}")
expect_status "$status" 200 "schedule/approve"

week_start=$(python3 -c "
import datetime
d = datetime.date.fromisoformat('$candidate_date')
print((d - datetime.timedelta(days=d.weekday())).isoformat())
")
step "GET /api/grid?week_start=$week_start"
status=$(request GET "/api/grid?week_start=$week_start")
expect_status "$status" 200 "grid"
assessment_total=$(jget "$WORKDIR/last.json" "sum(len(day['assessments']) for day in data['days'])")
[ "${assessment_total:-0}" -ge 1 ] 2>/dev/null || fail "grid: approved assessment does not appear in its own week (got $assessment_total total)"
echo "    approved assessment appears on the grid"

step "GET /api/export"
status=$(request GET /api/export)
expect_status "$status" 200 "export"
exported_ok=$(jget "$WORKDIR/last.json" "'calendar' in data and 'proposal_history' in data")
[ "$exported_ok" = "True" ] || fail "export: response does not look like a session export blob"

echo ""
echo "All smoke checks passed against $BASE_URL (session $SESSION_ID)."
