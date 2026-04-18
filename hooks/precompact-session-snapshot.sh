#!/usr/bin/env bash
# precompact-session-snapshot.sh v2.1
# PreCompact hook — captures working context snapshot before compaction.
# Writes session-memory.md for Cortex and a fully-annotated L1 episode to the vault.
# Runs BOTH away_summary (Track A) and Haiku (Track B) independently every time.

set -uo pipefail
. "$(dirname "$0")/cortex-supervisor-log.sh"
supervisor_log "precompact-session-snapshot"

HOOK_START_MS=$(date -u +%s%3N 2>/dev/null || echo 0)
HOOK_SCRIPT="precompact-session-snapshot.sh"
HOOK_VERSION="2.1"
HOOK_TRIGGER="PreCompact"

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
REASON=$(printf '%s' "$PAYLOAD" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('reason','auto'))
" 2>/dev/null || echo "auto")

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

# ── Extract last 30 turns for Haiku ───────────────────────────────────────
TURNS=""
TURNS_EXTRACTED=0
if [[ -n "$TRANSCRIPT" && -f "$TRANSCRIPT" ]]; then
  TURNS=$(tail -n 100 "$TRANSCRIPT" 2>/dev/null | python3 -c "
import sys, json
lines = []
for line in sys.stdin:
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
            lines.append(f'{role}: {str(content)[:400]}')
    except: pass
print('\n'.join(lines[-30:]))
" 2>/dev/null || echo "")
  if [[ -z "$TURNS" ]]; then
    TURNS_EXTRACTED=0
  else
    TURNS_EXTRACTED=$(printf '%s' "$TURNS" | grep -c '^' 2>/dev/null || echo 0)
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
HAIKU_TIMEOUT_SEC=10
HAIKU_CHAR_COUNT=0
HAIKU_PRESENT=false

if [[ -n "$TURNS" ]]; then
  PROMPT="Given these recent conversation turns, respond with EXACTLY these 5 labeled lines (no extra text):
What we're doing: <current task in one sentence>
Current state: <what is done or in progress>
Next step: <the immediate next action>
Blocking context: <anything blocking progress, or none>
Key files: <relevant file paths, or none>

TURNS:
${TURNS}"
  HAIKU_BODY=$(printf '%s' "$PROMPT" | env -u ANTHROPIC_API_KEY timeout ${HAIKU_TIMEOUT_SEC} claude -p --model ${HAIKU_MODEL} 2>/dev/null) || HAIKU_EXIT=$?
  if [[ $HAIKU_EXIT -eq 124 ]]; then
    HAIKU_TIMED_OUT=true
    HAIKU_FALLBACK_REASON="timeout"
  elif [[ $HAIKU_EXIT -ne 0 ]] || [[ -z "$HAIKU_BODY" ]]; then
    HAIKU_FALLBACK_REASON="error"
  fi
else
  HAIKU_FALLBACK_REASON="no_turns"
fi

if [[ -n "$HAIKU_BODY" ]]; then
  HAIKU_PRESENT=true
  HAIKU_CHAR_COUNT=${#HAIKU_BODY}
else
  # Raw fallback
  if [[ -n "$TURNS" ]]; then
    RAW=$(printf '%s' "$TURNS" | tail -5)
    HAIKU_BODY="[Haiku unavailable — raw fallback]

${RAW}"
    [[ "$HAIKU_FALLBACK_REASON" == "none" ]] && HAIKU_FALLBACK_REASON="unavailable"
  else
    HAIKU_BODY="[no transcript available]"
    HAIKU_FALLBACK_REASON="no_transcript"
  fi
fi

# ── Write session-memory.md (Haiku structured — Cortex reads this) ─────────
SNAPSHOT_BODY="${HAIKU_BODY}"
EXISTING_SUMMARY=""
if [[ -f "$SESSION_MEMORY" ]]; then
  EXISTING_SUMMARY=$(python3 -c "
content = open('$SESSION_MEMORY').read()
idx = content.find('## Session Summary')
if idx >= 0:
    print(content[idx:].rstrip())
" 2>/dev/null || echo "")
fi

TMP=$(mktemp "${CLAUDE_PROJECT_DIR}/.cortex/.session-memory-XXXXXX.md")
{
  printf '## Session Snapshot — %s\n\n' "$TIMESTAMP"
  printf '%s\n' "$SNAPSHOT_BODY"
  if [[ -n "$EXISTING_SUMMARY" ]]; then
    printf '\n\n%s\n' "$EXISTING_SUMMARY"
  fi
} > "$TMP"
mv "$TMP" "$SESSION_MEMORY"

# ── Vault L1 episodic write ────────────────────────────────────────────────
VAULT="${MEMORY_VAULT:-$HOME/memory/vault}"
VAULT_SESSIONS="${VAULT}/raw/sessions"
VAULT_INDEX="${VAULT}/INDEX.md"
mkdir -p "${VAULT_SESSIONS}" 2>/dev/null || true

PROJECT_NAME="$(basename "${CLAUDE_PROJECT_DIR:-$PWD}")"
EPISODE_TS="$(date -u +%Y%m%dT%H%M%SZ)"

# INDEX one-liner: prefer away_summary (narrative), fall back to Haiku field
if [[ -n "${AWAY_SUMMARY}" ]]; then
  SUMMARY_LINE="$(printf '%s' "${AWAY_SUMMARY}" | head -1 | cut -c1-120)"
  INDEX_SOURCE_USED="away_summary"
else
  SUMMARY_LINE="$(printf '%s' "${HAIKU_BODY}" | grep -m1 "^What we.re doing:" | sed "s/^What we.re doing: //" 2>/dev/null || true)"
  INDEX_SOURCE_USED="haiku"
fi
[[ -z "${SUMMARY_LINE}" ]] && SUMMARY_LINE="(precompact snapshot)"

HOOK_END_MS=$(date -u +%s%3N 2>/dev/null || echo 0)
HOOK_DURATION_MS=$(( HOOK_END_MS - HOOK_START_MS ))

EPISODE_FILE="${VAULT_SESSIONS}/${EPISODE_TS}-precompact.md"
{
  printf -- '---\n'
  # Identity
  printf 'id: %s-precompact\n'         "${EPISODE_TS}"
  printf 'type: episode\n'
  printf 'event: precompact\n'
  printf 'trigger_event: %s\n'         "${HOOK_TRIGGER}"
  # Source identification
  printf 'hook_script: %s\n'           "${HOOK_SCRIPT}"
  printf 'hook_version: "%s"\n'        "${HOOK_VERSION}"
  printf 'session_id: "%s"\n'          "${SESSION_ID}"
  printf 'compact_reason: %s\n'        "${REASON}"
  # Project context
  printf 'project: %s\n'               "${PROJECT_NAME}"
  printf 'project_dir: "%s"\n'         "${CLAUDE_PROJECT_DIR:-$PWD}"
  printf 'cwd: "%s"\n'                 "${PWD}"
  printf 'git_branch: "%s"\n'          "${GIT_BRANCH}"
  printf 'git_sha: "%s"\n'             "${GIT_SHA}"
  printf 'git_dirty: %s\n'             "${GIT_DIRTY}"
  # Timing
  printf 'timestamp: %s\n'             "${TIMESTAMP}"
  printf 'hook_duration_ms: %s\n'      "${HOOK_DURATION_MS}"
  # Transcript provenance
  printf 'transcript_path: "%s"\n'     "${TRANSCRIPT}"
  printf 'transcript_line_count: %s\n' "${TRANSCRIPT_LINE_COUNT}"
  printf 'turns_extracted: %s\n'       "${TURNS_EXTRACTED}"
  printf 'away_summary_count: %s\n'    "${AWAY_SUMMARY_COUNT}"
  # Track A — away_summary
  printf 'away_summary_present: %s\n'  "$([[ -n "${AWAY_SUMMARY}" ]] && echo true || echo false)"
  printf 'away_summary_source: "%s"\n' "${AWAY_SUMMARY_SOURCE}"
  printf 'away_summary_char_count: %s\n'     "${AWAY_SUMMARY_CHAR_COUNT}"
  printf 'away_summary_sentence_count: %s\n' "${AWAY_SUMMARY_SENTENCE_COUNT}"
  # Track B — Haiku
  printf 'haiku_present: %s\n'         "${HAIKU_PRESENT}"
  printf 'haiku_model: "%s"\n'         "${HAIKU_MODEL}"
  printf 'haiku_timeout_sec: %s\n'     "${HAIKU_TIMEOUT_SEC}"
  printf 'haiku_exit_code: %s\n'       "${HAIKU_EXIT}"
  printf 'haiku_timed_out: %s\n'       "${HAIKU_TIMED_OUT}"
  printf 'haiku_char_count: %s\n'      "${HAIKU_CHAR_COUNT}"
  printf 'haiku_fallback_reason: "%s"\n' "${HAIKU_FALLBACK_REASON}"
  # Index entry metadata
  printf 'index_source_used: "%s"\n'   "${INDEX_SOURCE_USED}"
  # Machine context
  printf 'hostname: "%s"\n'            "${HOSTNAME_SHORT}"
  printf -- '---\n\n'
  # Body — both tracks
  if [[ -n "${AWAY_SUMMARY}" ]]; then
    printf '## away_summary (Claude Code native)\n%s\n\n' "${AWAY_SUMMARY}"
  else
    printf '## away_summary\n(not present this session)\n\n'
  fi
  printf '## haiku_summary (structured)\n%s\n' "${HAIKU_BODY}"
} > "${EPISODE_FILE}" 2>/dev/null || true

printf '\n- session-episode %s [%s] %s — precompact\n' \
  "${EPISODE_TS}" "${PROJECT_NAME}" "${SUMMARY_LINE}" >> "${VAULT_INDEX}" 2>/dev/null || true

# ── Kick L1 chunker (async, flock-safe) ──────────────────────────────────
VAULT_BIN="${HOME}/.local/bin/vault"
[[ ! -x "$VAULT_BIN" ]] && VAULT_BIN="${HOME}/memory/vault/bin/vault"
if [[ -x "$VAULT_BIN" && -n "$PROJECT_NAME" ]]; then
    nohup "$VAULT_BIN" chunk "$PROJECT_NAME" >> /tmp/vault-chunker.log 2>&1 &
    disown $!
fi

exit 0
