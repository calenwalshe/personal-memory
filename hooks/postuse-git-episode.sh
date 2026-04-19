#!/usr/bin/env bash
# postuse-git-episode.sh v2.1
# PostToolUse hook — captures git commits as annotated L1 episodic vault entries.
# Fires on every Bash tool call. Exits in microseconds if not a git commit.
# async: true — never blocks the response.

set -uo pipefail

HOOK_START_MS=$(date -u +%s%3N 2>/dev/null || echo 0)
HOOK_SCRIPT="postuse-git-episode.sh"
HOOK_VERSION="2.1"
HOOK_TRIGGER="PostToolUse:Bash"

PAYLOAD=$(cat)

# ── Fast exit if not a Bash git commit ────────────────────────────────────
TOOL=$(printf '%s' "$PAYLOAD" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('tool_name', ''))
except: print('')
" 2>/dev/null || echo "")
[[ "$TOOL" != "Bash" ]] && exit 0

COMMAND=$(printf '%s' "$PAYLOAD" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('tool_input', {}).get('command', ''))
except: print('')
" 2>/dev/null || echo "")
printf '%s' "$COMMAND" | grep -qE 'git\s+commit' || exit 0

# ── Payload extraction ─────────────────────────────────────────────────────
SESSION_ID=$(printf '%s' "$PAYLOAD" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('session_id', 'unknown'))
except: print('unknown')
" 2>/dev/null || echo "unknown")

TOOL_RESPONSE=$(printf '%s' "$PAYLOAD" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    r = d.get('tool_response', {})
    if isinstance(r, dict):
        out = r.get('output', r.get('content', ''))
    else:
        out = str(r)
    print(str(out)[:400])
except: print('')
" 2>/dev/null || echo "")

TOOL_EXIT_CODE=$(printf '%s' "$PAYLOAD" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    r = d.get('tool_response', {})
    if isinstance(r, dict):
        print(r.get('exit_code', r.get('returnCode', '')))
    else:
        print('')
except: print('')
" 2>/dev/null || echo "")

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ── Machine + git context ──────────────────────────────────────────────────
HOSTNAME_SHORT=$(hostname -s 2>/dev/null || echo "unknown")
GIT_BRANCH=$(git -C "${CLAUDE_PROJECT_DIR:-$PWD}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "none")
GIT_SHA=$(git -C "${CLAUDE_PROJECT_DIR:-$PWD}" rev-parse --short HEAD 2>/dev/null || echo "none")
# The commit SHA is the one just created — try to get it from response or HEAD
COMMIT_SHA=$(git -C "${CLAUDE_PROJECT_DIR:-$PWD}" rev-parse --short HEAD 2>/dev/null || echo "none")

# ── Extract commit message ─────────────────────────────────────────────────
COMMIT_MSG=$(printf '%s' "$COMMAND" | python3 -c "
import sys, re
cmd = sys.stdin.read()
# -m '...' or -m \"...\"
m = re.search(r'-m\s+[\"\'](.*?)[\"\']', cmd, re.DOTALL)
if m:
    print(m.group(1).strip()[:200])
    sys.exit(0)
# heredoc: between first EOF and last EOF
m = re.search(r'EOF\s*\n(.*?)\n\s*EOF', cmd, re.DOTALL)
if m:
    print(m.group(1).strip()[:200])
    sys.exit(0)
print('(commit message not extracted)')
" 2>/dev/null || echo "(commit)")

COMMIT_MSG_CHAR_COUNT=${#COMMIT_MSG}
# Detect Co-Authored-By trailer
HAS_COAUTHOR=$(printf '%s' "$COMMIT_MSG" | grep -c "Co-Authored-By" 2>/dev/null || echo 0)

# ── Command analysis ───────────────────────────────────────────────────────
HAS_AMEND=$(printf '%s' "$COMMAND" | grep -c '\-\-amend' 2>/dev/null || echo 0)
HAS_NO_VERIFY=$(printf '%s' "$COMMAND" | grep -c '\-\-no-verify' 2>/dev/null || echo 0)
COMMAND_CHAR_COUNT=${#COMMAND}

# ── Tool response analysis ─────────────────────────────────────────────────
COMMIT_SUCCESS=false
[[ -n "$TOOL_RESPONSE" ]] && printf '%s' "$TOOL_RESPONSE" | grep -qE '^\[' && COMMIT_SUCCESS=true

# ── Vault write ────────────────────────────────────────────────────────────
VAULT="${MEMORY_VAULT:-$HOME/memory/vault}"
VAULT_SESSIONS="${VAULT}/raw/sessions"
VAULT_INDEX="${VAULT}/INDEX.md"
mkdir -p "${VAULT_SESSIONS}" 2>/dev/null || true

TS_ID="$(date -u +%Y%m%dT%H%M%SZ)"
PROJECT_NAME="$(basename "${CLAUDE_PROJECT_DIR:-$PWD}")"

HOOK_END_MS=$(date -u +%s%3N 2>/dev/null || echo 0)
HOOK_DURATION_MS=$(( HOOK_END_MS - HOOK_START_MS ))

EPISODE_FILE="${VAULT_SESSIONS}/${TS_ID}-git-commit.md"
{
  printf -- '---\n'
  # Identity
  printf 'id: %s-git-commit\n'         "${TS_ID}"
  printf 'type: episode\n'
  printf 'event: git-commit\n'
  printf 'trigger_event: %s\n'         "${HOOK_TRIGGER}"
  # Source identification
  printf 'hook_script: %s\n'           "${HOOK_SCRIPT}"
  printf 'hook_version: "%s"\n'        "${HOOK_VERSION}"
  printf 'session_id: "%s"\n'          "${SESSION_ID}"
  printf 'tool_name: Bash\n'
  # Project context
  printf 'project: %s\n'               "${PROJECT_NAME}"
  printf 'project_dir: "%s"\n'         "${CLAUDE_PROJECT_DIR:-$PWD}"
  printf 'cwd: "%s"\n'                 "${PWD}"
  printf 'git_branch: "%s"\n'          "${GIT_BRANCH}"
  printf 'git_sha_before: "%s"\n'      "${GIT_SHA}"
  printf 'commit_sha: "%s"\n'          "${COMMIT_SHA}"
  # Timing
  printf 'timestamp: %s\n'             "${TIMESTAMP}"
  printf 'hook_duration_ms: %s\n'      "${HOOK_DURATION_MS}"
  # Commit content
  printf 'commit_success: %s\n'        "${COMMIT_SUCCESS}"
  printf 'commit_msg_char_count: %s\n' "${COMMIT_MSG_CHAR_COUNT}"
  printf 'has_coauthor_trailer: %s\n'  "$([[ $HAS_COAUTHOR -gt 0 ]] && echo true || echo false)"
  printf 'is_amend: %s\n'              "$([[ $HAS_AMEND -gt 0 ]] && echo true || echo false)"
  printf 'no_verify: %s\n'             "$([[ $HAS_NO_VERIFY -gt 0 ]] && echo true || echo false)"
  printf 'command_char_count: %s\n'    "${COMMAND_CHAR_COUNT}"
  printf 'tool_exit_code: "%s"\n'      "${TOOL_EXIT_CODE}"
  # Machine context
  printf 'hostname: "%s"\n'            "${HOSTNAME_SHORT}"
  printf -- '---\n\n'
  printf '## Commit message\n%s\n\n' "${COMMIT_MSG}"
  printf '## Tool response\n%s\n' "${TOOL_RESPONSE}"
} > "${EPISODE_FILE}" 2>/dev/null || true

printf '\n- git-commit %s [%s] %s\n' \
  "${TS_ID}" "${PROJECT_NAME}" "${COMMIT_MSG}" >> "${VAULT_INDEX}" 2>/dev/null || true

exit 0
