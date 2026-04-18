#!/usr/bin/env bash
# sessionstart-postclear-recall.sh v2.0
# SessionStart hook — detects post-/clear and reconstructs context from vault
#
# Fires on every SessionStart. Checks if the previous session for this project
# ended <120s ago (indicating /clear or rapid restart). If so, gathers:
#   1. L0 turns (user messages + assistant responses from the turns table)
#   2. L1 atoms (cross-session knowledge)
#   3. session-memory.md (if present)
# Then synthesizes a brief reconstruction via Haiku and injects as additionalContext.
#
# v2.0: Switched from abandoned events/messages tables to the turns table,
#       which is populated by sessionend-extract-turns.py at SessionEnd.

set -uo pipefail

DB="${MEMORY_VAULT:-$HOME/memory/vault}/events.db"
ATOMS_DB="${MEMORY_VAULT:-$HOME/memory/vault}/atoms.db"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
PROJECT=$(basename "$PROJECT_DIR")
PAYLOAD=$(cat)

[[ -f "$DB" ]] || exit 0

# ── Single Python script: detect + gather + format ────────────────────────
RECONSTRUCTION=$(python3 - "$DB" "$ATOMS_DB" "$PROJECT" "$PROJECT_DIR" "$PAYLOAD" <<'PYEOF'
import sqlite3, os, sys, json
from datetime import datetime, timezone

db_path, atoms_db_path, project, project_dir, payload_raw = sys.argv[1:6]

try:
    payload = json.loads(payload_raw)
except Exception:
    payload = {}

current_session_id = payload.get("session_id", "")

if not os.path.isfile(db_path):
    sys.exit(0)

conn = sqlite3.connect(db_path)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=5000")

# ── Detect post-clear ────────────────────────────────────────────────────
# Find the most recent session for this project that has turns (L0 turn data).
# Use COALESCE(ended_at, last turn timestamp) since ended_at may lag.
row = conn.execute("""
    SELECT s.session_id,
           COALESCE(s.ended_at, (SELECT MAX(t.started_at) FROM turns t WHERE t.session_id = s.session_id)) as effective_end,
           (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.session_id) as turn_count
    FROM sessions s
    WHERE s.project = ?
      AND s.session_id != ?
      AND EXISTS (SELECT 1 FROM turns t WHERE t.session_id = s.session_id)
    ORDER BY effective_end DESC
    LIMIT 1
""", (project, current_session_id)).fetchone()

if not row:
    sys.exit(0)

prev_sid, ended_at_str, turn_count = row

try:
    ended_at = datetime.fromisoformat(ended_at_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    delta = (now - ended_at).total_seconds()
except Exception:
    sys.exit(0)

if delta > 120:
    sys.exit(0)

# ── It's a post-clear! Gather reconstruction material ────────────────────
parts = []

# Source 1: L0 turns (primary source — user messages + assistant responses)
turns = conn.execute("""
    SELECT turn_n, user_message_preview, response_preview, tool_call_count, tool_names, started_at
    FROM turns
    WHERE session_id = ?
    ORDER BY turn_n
""", (prev_sid,)).fetchall()

if turns:
    turn_lines = []
    for turn_n, user_prev, resp_prev, tool_count, tool_names, started in turns:
        user_text = (user_prev or "").strip()
        resp_text = (resp_prev or "").strip()
        # Skip /clear commands and empty turns
        if user_text.startswith("/clear") or not user_text:
            continue
        if len(user_text) > 400:
            user_text = user_text[:400] + "..."
        if len(resp_text) > 400:
            resp_text = resp_text[:400] + "..."
        tools_info = f" [{tool_count} tools: {tool_names}]" if tool_count else ""
        turn_lines.append(f"  [user] {user_text}")
        if resp_text:
            turn_lines.append(f"  [assistant]{tools_info} {resp_text}")
    if turn_lines:
        # Take last 30 lines to keep it manageable
        recent = turn_lines[-30:]
        parts.append(f"CONVERSATION ({len(turns)} turns):\n" + "\n".join(recent))

conn.close()

# Source 2: L1 atoms (cross-session knowledge)
if atoms_db_path and os.path.isfile(atoms_db_path):
    try:
        aconn = sqlite3.connect(atoms_db_path)
        aconn.execute("PRAGMA busy_timeout=3000")
        atoms = aconn.execute("""
            SELECT atom_type, topic, content
            FROM atoms
            WHERE project = ?
              AND invalidated_by IS NULL
            ORDER BY time_first DESC
            LIMIT 5
        """, (project,)).fetchall()
        aconn.close()
        if atoms:
            atom_lines = [f"  [{t}/{tp}] {(c or '')[:200]}" for t, tp, c in atoms]
            parts.append("ATOMS (cross-session):\n" + "\n".join(atom_lines))
    except Exception:
        pass

# Source 3: session-memory.md
sm_path = os.path.join(project_dir, ".cortex", "session-memory.md")
if os.path.isfile(sm_path):
    try:
        with open(sm_path) as f:
            sm = f.read().strip()[:600]
        if sm:
            parts.append("SESSION MEMORY:\n" + sm)
    except Exception:
        pass

if not parts:
    sys.exit(0)

print("\n\n".join(parts))
PYEOF
)

[[ -z "$RECONSTRUCTION" ]] && exit 0

# ── Synthesize with Haiku ─────────────────────────────────────────────────
# Budget: keep reconstruction input under 8000 chars to control Haiku cost
RECONSTRUCTION=$(echo "$RECONSTRUCTION" | head -c 8000)

SYNTHESIS=$(
  env -u ANTHROPIC_API_KEY claude -p --model claude-haiku-4-5-20251001 \
    "You are reconstructing context after a /clear command wiped the conversation.
Below is raw data from the session that just ended in project '${PROJECT}' (${PROJECT_DIR}).

Synthesize a brief, actionable context restoration:
- What the user was working on
- Key decisions made or outcomes reached
- What was in progress when cleared
- Likely next step

Be concise (under 400 words). Use bullet points. Skip preamble.
If the data is mostly hook/infrastructure noise with no real user work, say so briefly.

--- RAW DATA ---
${RECONSTRUCTION}
--- END ---" 2>/dev/null
)

# Fallback: if Haiku failed, use truncated raw data
if [[ -z "$SYNTHESIS" ]]; then
  SYNTHESIS=$(echo "$RECONSTRUCTION" | head -c 2000)
  LABEL="POST-CLEAR RAW CONTEXT"
else
  LABEL="POST-CLEAR RECONSTRUCTION"
fi

# ── Inject as additionalContext ───────────────────────────────────────────
python3 -c "
import json, sys
label = sys.argv[1]
project = sys.argv[2]
content = sys.stdin.read().strip()
if not content:
    sys.exit(0)
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SessionStart',
        'additionalContext': f'{label} ({project})\n\n{content}'
    }
}))
" "$LABEL" "$PROJECT" <<< "$SYNTHESIS"

exit 0
