#!/usr/bin/env bash
# sessionend-session-summary.sh v2.2
# SessionEnd hook — writes durable session summary at exit.
# Writes session-memory.md for Cortex and a fully-annotated L1 episode to the vault.
# Runs BOTH away_summary (Track A) and Haiku (Track B) independently every time.

set -uo pipefail

# ── Concurrency lock — max 3 simultaneous hook instances ──────────────────
LOCK_DIR="/tmp/sessionend-locks"
mkdir -p "$LOCK_DIR"
LOCK_COUNT=$(ls "$LOCK_DIR"/*.lock 2>/dev/null | wc -l | tr -d ' ')
if [[ "$LOCK_COUNT" -ge 3 ]]; then
  echo "[sessionend] skipping — $LOCK_COUNT instances already running (max 3)" >&2
  exit 0
fi
LOCK_FILE="$LOCK_DIR/$$.lock"
touch "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT
# ──────────────────────────────────────────────────────────────────────────

. "$(dirname "$0")/cortex-supervisor-log.sh"
supervisor_log "sessionend-session-summary"

HOOK_START_MS=$(date -u +%s%3N 2>/dev/null || echo 0)
HOOK_SCRIPT="sessionend-session-summary.sh"
HOOK_VERSION="2.1"
HOOK_TRIGGER="SessionEnd"

# ── Payload extraction ─────────────────────────────────────────────────────
PAYLOAD=$(cat)
TRANSCRIPT=$(printf '%s' "$PAYLOAD" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('transcript_path',''))
" 2>/dev/null || echo "")
SESSION_ID=$(printf '%s' "$PAYLOAD" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('session_id','unknown'))
" 2>/dev/null || echo "unknown")
MATCHER=$(printf '%s' "$PAYLOAD" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('matcher','other'))
" 2>/dev/null || echo "other")

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SESSION_MEMORY="${CLAUDE_PROJECT_DIR}/.cortex/session-memory.md"
mkdir -p "${CLAUDE_PROJECT_DIR}/.cortex"

# ── Machine + git context ──────────────────────────────────────────────────
HOSTNAME_SHORT=$(hostname -s 2>/dev/null || echo "unknown")
GIT_BRANCH=$(git -C "${CLAUDE_PROJECT_DIR:-$PWD}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "none")
GIT_SHA=$(git -C "${CLAUDE_PROJECT_DIR:-$PWD}" rev-parse --short HEAD 2>/dev/null || echo "none")
GIT_DIRTY_COUNT=$(git -C "${CLAUDE_PROJECT_DIR:-$PWD}" status --porcelain 2>/dev/null | wc -l | tr -d ' ' || echo "0")
GIT_DIRTY=$([[ "${GIT_DIRTY_COUNT}" -gt 0 ]] && echo true || echo false)

# ── Transcript stats ───────────────────────────────────────────────────────
TRANSCRIPT_LINE_COUNT=0
AWAY_SUMMARY_COUNT=0
if [[ -n "$TRANSCRIPT" && -f "$TRANSCRIPT" ]]; then
  TRANSCRIPT_LINE_COUNT=$(wc -l < "$TRANSCRIPT" 2>/dev/null | tr -d ' ' || echo 0)
  AWAY_SUMMARY_COUNT=$(python3 -c "
import json, sys
count = 0
for line in open('$TRANSCRIPT'):
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        if d.get('type') == 'system' and d.get('subtype') == 'away_summary':
            count += 1
    except: pass
print(count)
" 2>/dev/null || echo 0)
fi

# ── Read transcript text for Haiku (cap at 8000 chars from end) ───────────
TRANSCRIPT_TEXT=""
TURNS_EXTRACTED=0
if [[ -n "$TRANSCRIPT" && -f "$TRANSCRIPT" ]]; then
  TRANSCRIPT_TEXT=$(python3 -c "
import sys, json
lines = []
for line in open('$TRANSCRIPT'):
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        # New format: role/content nested under 'message'
        msg = d.get('message') or d
        role = msg.get('role', '') or d.get('role', '')
        content = msg.get('content', '') or d.get('content', '')
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict):
                    if c.get('type') == 'text':
                        parts.append(c.get('text', ''))
                    elif c.get('type') == 'tool_use':
                        parts.append(f'[tool:{c.get(\"name\",\"?\")}]')
            content = ' '.join(parts)
        if role and content and role in ('user', 'assistant'):
            lines.append(f'{role}: {str(content)[:500]}')
    except: pass
text = '\n'.join(lines)
if len(text) > 8000:
    text = '...' + text[-8000:]
print(text)
" 2>/dev/null || echo "")
  if [[ -z "$TRANSCRIPT_TEXT" ]]; then
    TURNS_EXTRACTED=0
  else
    TURNS_EXTRACTED=$(printf '%s' "$TRANSCRIPT_TEXT" | grep -c '^' 2>/dev/null || echo 0)
  fi
fi

# ── TRACK A: away_summary ──────────────────────────────────────────────────
AWAY_SUMMARY=""
AWAY_SUMMARY_CHAR_COUNT=0
AWAY_SUMMARY_SENTENCE_COUNT=0
AWAY_SUMMARY_SOURCE="none"

if [[ -n "$TRANSCRIPT" && -f "$TRANSCRIPT" ]]; then
  AWAY_SUMMARY=$(python3 -c "
import json, sys
entries = []
for line in open('$TRANSCRIPT'):
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        if d.get('type') == 'system' and d.get('subtype') == 'away_summary':
            entries.append(d.get('content', '').strip())
    except: pass
if entries:
    print(entries[-1])
" 2>/dev/null || echo "")
fi

if [[ -n "$AWAY_SUMMARY" ]]; then
  AWAY_SUMMARY_SOURCE="transcript:system:away_summary"
  AWAY_SUMMARY_CHAR_COUNT=${#AWAY_SUMMARY}
  AWAY_SUMMARY_SENTENCE_COUNT=$(printf '%s' "$AWAY_SUMMARY" | python3 -c "
import sys, re
text = sys.stdin.read().strip()
sentences = [s.strip() for s in re.split(r'[.!?]+', text) if s.strip()]
print(len(sentences))
" 2>/dev/null || echo 0)
fi

# ── TRACK B: Haiku structured summary ─────────────────────────────────────
HAIKU_BODY=""
HAIKU_EXIT=0
HAIKU_TIMED_OUT=false
HAIKU_FALLBACK_REASON="none"
HAIKU_MODEL="claude-haiku-4-5-20251001"
HAIKU_TIMEOUT_SEC=30
HAIKU_CHAR_COUNT=0
HAIKU_PRESENT=false

if [[ -n "$TRANSCRIPT_TEXT" ]]; then
  if [[ "$MATCHER" == "clear" ]]; then
    PROMPT="This session was cleared with /clear. Given the transcript, respond with EXACTLY these labeled fields (no extra text):
Project area: <area of codebase or project worked on>
Accomplished: <what was completed or attempted>
Decisions: <key decisions made, as bullet points starting with -, or none>
Open thread: none
status: cleared

TRANSCRIPT:
${TRANSCRIPT_TEXT}"
  else
    PROMPT="Given this session transcript, respond with EXACTLY these labeled fields (no extra text):
Project area: <area of codebase or project worked on>
Accomplished: <what was completed in this session>
Decisions: <1-3 key decisions made, as bullet points starting with -, or none>
Open thread: <unfinished work or next immediate task>
status: resolved|paused|blocked

TRANSCRIPT:
${TRANSCRIPT_TEXT}"
  fi
  HAIKU_BODY=$(printf '%s' "$PROMPT" | env -u ANTHROPIC_API_KEY timeout ${HAIKU_TIMEOUT_SEC} claude -p --model ${HAIKU_MODEL} 2>/dev/null) || HAIKU_EXIT=$?
  if [[ $HAIKU_EXIT -eq 124 ]]; then
    HAIKU_TIMED_OUT=true
    HAIKU_FALLBACK_REASON="timeout"
  elif [[ $HAIKU_EXIT -ne 0 ]] || [[ -z "$HAIKU_BODY" ]]; then
    HAIKU_FALLBACK_REASON="error"
  fi
else
  HAIKU_FALLBACK_REASON="no_transcript"
fi

if [[ -n "$HAIKU_BODY" ]]; then
  HAIKU_PRESENT=true
  HAIKU_CHAR_COUNT=${#HAIKU_BODY}
else
  # Structured fallback so Cortex fields still parse
  if [[ "$MATCHER" == "clear" ]]; then
    HAIKU_BODY="Project area: unknown
Accomplished: [Haiku unavailable — session cleared]
Decisions: none
Open thread: none
status: cleared"
  else
    HAIKU_BODY="Project area: unknown
Accomplished: [Haiku unavailable]
Decisions: none
Open thread: unknown
status: paused"
  fi
  [[ "$HAIKU_FALLBACK_REASON" == "none" ]] && HAIKU_FALLBACK_REASON="unavailable"
fi

# ── Write session-memory.md (Haiku structured — Cortex reads this) ─────────
SUMMARY_BODY="${HAIKU_BODY}"
EXISTING_SNAPSHOT=""
if [[ -f "$SESSION_MEMORY" ]]; then
  EXISTING_SNAPSHOT=$(python3 -c "
content = open('$SESSION_MEMORY').read()
idx = content.find('## Session Snapshot')
end = content.find('## Session Summary', idx) if idx >= 0 else -1
if idx >= 0:
    if end > idx:
        print(content[idx:end].rstrip())
    else:
        print(content[idx:].rstrip())
" 2>/dev/null || echo "")
fi

TMP=$(mktemp "${CLAUDE_PROJECT_DIR}/.cortex/.session-memory-XXXXXX.md")
{
  if [[ -n "$EXISTING_SNAPSHOT" ]]; then
    printf '%s\n\n\n' "$EXISTING_SNAPSHOT"
  fi
  printf '## Session Summary — %s\n\n' "$TIMESTAMP"
  printf '%s\n' "$SUMMARY_BODY"
} > "$TMP"
mv "$TMP" "$SESSION_MEMORY"

# ── Vault L1 episodic write ────────────────────────────────────────────────
VAULT="${MEMORY_VAULT:-$HOME/memory/vault}"
VAULT_SESSIONS="${VAULT}/raw/sessions"
VAULT_INDEX="${VAULT}/INDEX.md"
mkdir -p "${VAULT_SESSIONS}" 2>/dev/null || true

PROJECT_NAME="$(basename "${CLAUDE_PROJECT_DIR:-$PWD}")"
EPISODE_TS="$(date -u +%Y%m%dT%H%M%SZ)"

# INDEX one-liner: prefer away_summary (narrative), fall back to Haiku Accomplished field
if [[ -n "${AWAY_SUMMARY}" ]]; then
  SUMMARY_LINE="$(printf '%s' "${AWAY_SUMMARY}" | head -1 | cut -c1-120)"
  INDEX_SOURCE_USED="away_summary"
else
  SUMMARY_LINE="$(printf '%s' "${HAIKU_BODY}" | grep -m1 "^Accomplished:" | sed "s/^Accomplished: //" 2>/dev/null || true)"
  INDEX_SOURCE_USED="haiku"
fi
[[ -z "${SUMMARY_LINE}" ]] && SUMMARY_LINE="(session end)"

STATUS_LINE="$(printf '%s' "${HAIKU_BODY}" | grep -m1 "^status:" | sed "s/^status: //" 2>/dev/null || true)"
[[ -z "${STATUS_LINE}" ]] && STATUS_LINE="unknown"

EVENT_TYPE="sessionend"
[[ "$MATCHER" == "clear" ]] && EVENT_TYPE="clear"

HOOK_END_MS=$(date -u +%s%3N 2>/dev/null || echo 0)
HOOK_DURATION_MS=$(( HOOK_END_MS - HOOK_START_MS ))

EPISODE_FILE="${VAULT_SESSIONS}/${EPISODE_TS}-${EVENT_TYPE}.md"
{
  printf -- '---\n'
  # Identity
  printf 'id: %s-%s\n'                "${EPISODE_TS}" "${EVENT_TYPE}"
  printf 'type: episode\n'
  printf 'event: %s\n'                "${EVENT_TYPE}"
  printf 'trigger_event: %s\n'        "${HOOK_TRIGGER}"
  # Source identification
  printf 'hook_script: %s\n'          "${HOOK_SCRIPT}"
  printf 'hook_version: "%s"\n'       "${HOOK_VERSION}"
  printf 'session_id: "%s"\n'         "${SESSION_ID}"
  printf 'matcher: "%s"\n'            "${MATCHER}"
  # Project context
  printf 'project: %s\n'              "${PROJECT_NAME}"
  printf 'project_dir: "%s"\n'        "${CLAUDE_PROJECT_DIR:-$PWD}"
  printf 'cwd: "%s"\n'                "${PWD}"
  printf 'git_branch: "%s"\n'         "${GIT_BRANCH}"
  printf 'git_sha: "%s"\n'            "${GIT_SHA}"
  printf 'git_dirty: %s\n'            "${GIT_DIRTY}"
  # Timing
  printf 'timestamp: %s\n'            "${TIMESTAMP}"
  printf 'hook_duration_ms: %s\n'     "${HOOK_DURATION_MS}"
  # Transcript provenance
  printf 'transcript_path: "%s"\n'    "${TRANSCRIPT}"
  printf 'transcript_line_count: %s\n' "${TRANSCRIPT_LINE_COUNT}"
  printf 'turns_extracted: %s\n'      "${TURNS_EXTRACTED}"
  printf 'away_summary_count: %s\n'   "${AWAY_SUMMARY_COUNT}"
  # Track A — away_summary
  printf 'away_summary_present: %s\n' "$([[ -n "${AWAY_SUMMARY}" ]] && echo true || echo false)"
  printf 'away_summary_source: "%s"\n'        "${AWAY_SUMMARY_SOURCE}"
  printf 'away_summary_char_count: %s\n'      "${AWAY_SUMMARY_CHAR_COUNT}"
  printf 'away_summary_sentence_count: %s\n'  "${AWAY_SUMMARY_SENTENCE_COUNT}"
  # Track B — Haiku
  printf 'haiku_present: %s\n'        "${HAIKU_PRESENT}"
  printf 'haiku_model: "%s"\n'        "${HAIKU_MODEL}"
  printf 'haiku_timeout_sec: %s\n'    "${HAIKU_TIMEOUT_SEC}"
  printf 'haiku_exit_code: %s\n'      "${HAIKU_EXIT}"
  printf 'haiku_timed_out: %s\n'      "${HAIKU_TIMED_OUT}"
  printf 'haiku_char_count: %s\n'     "${HAIKU_CHAR_COUNT}"
  printf 'haiku_fallback_reason: "%s"\n' "${HAIKU_FALLBACK_REASON}"
  # Index entry metadata
  printf 'index_source_used: "%s"\n'  "${INDEX_SOURCE_USED}"
  printf 'status: %s\n'               "${STATUS_LINE}"
  # Machine context
  printf 'hostname: "%s"\n'           "${HOSTNAME_SHORT}"
  printf -- '---\n\n'
  # Body — both tracks
  if [[ -n "${AWAY_SUMMARY}" ]]; then
    printf '## away_summary (Claude Code native)\n%s\n\n' "${AWAY_SUMMARY}"
  else
    printf '## away_summary\n(not present this session)\n\n'
  fi
  printf '## haiku_summary (structured)\n%s\n' "${HAIKU_BODY}"
} > "${EPISODE_FILE}" 2>/dev/null || true

printf '\n- session-episode %s [%s] %s — %s:%s\n' \
  "${EPISODE_TS}" "${PROJECT_NAME}" "${SUMMARY_LINE}" "${EVENT_TYPE}" "${STATUS_LINE}" \
  >> "${VAULT_INDEX}" 2>/dev/null || true

# ── L1 chunker moved to sessionend-turns.sh (runs after turn extraction) ──
# Previously kicked here but raced with async turn extraction, so the chunker
# often ran before new L0 turns were in the DB.  Now fires at the end of
# sessionend-turns.sh which guarantees turns are written first.

exit 0
