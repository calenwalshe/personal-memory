#!/usr/bin/env bash
# postuse-event-logger.sh v1.0
# PostToolUse hook — logs every tool call as a SCAPE stimulus compound to events.db.
# async: true — never blocks responses.
#
# SCAPE mapping:
#   stimulus  = tool_input (what was presented to the model to act on)
#   purpose   = last user message in transcript (why we're doing this)
#   context   = project + branch + session + cwd
#   processing = tool_name + sequence position
#   fluency    = exit code, error detection, retry detection

set -uo pipefail

HOOK_VERSION="1.0"
HOOK_SCRIPT="postuse-event-logger.sh"

DB="${MEMORY_VAULT:-$HOME/memory/vault}/events.db"
ARCHIVE_DIR="${MEMORY_VAULT:-$HOME/memory/vault}/raw/event-log"
mkdir -p "${ARCHIVE_DIR}" 2>/dev/null || true

PAYLOAD=$(cat)

python3 - <<PYEOF
import json, sqlite3, hashlib, os, sys, re
from datetime import datetime, timezone

payload_raw = """${PAYLOAD}"""
db_path = """${DB}"""
archive_dir = """${ARCHIVE_DIR}"""
hook_script = """${HOOK_SCRIPT}"""
hook_version = """${HOOK_VERSION}"""
project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
hostname = os.uname().nodename.split(".")[0]

try:
    payload = json.loads(payload_raw)
except Exception:
    sys.exit(0)

session_id  = payload.get("session_id", "unknown")
tool_name   = payload.get("tool_name", "")
tool_input  = payload.get("tool_input", {})
tool_resp   = payload.get("tool_response", {})
transcript  = payload.get("transcript_path", "")

if not tool_name:
    sys.exit(0)

timestamp = datetime.now(timezone.utc).isoformat()
project   = os.path.basename(project_dir)
cwd       = os.getcwd()

# ── Git context ────────────────────────────────────────────────────────────
import subprocess
def git(cmd):
    try:
        return subprocess.check_output(
            ["git", "-C", project_dir] + cmd,
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return "none"

git_branch = git(["rev-parse", "--abbrev-ref", "HEAD"])
git_sha    = git(["rev-parse", "--short", "HEAD"])

# ── Stimulus: tool input ───────────────────────────────────────────────────
if isinstance(tool_input, dict):
    input_str = json.dumps(tool_input)
else:
    input_str = str(tool_input)

input_preview   = input_str[:400]
input_hash      = hashlib.sha256(input_str.encode()).hexdigest()[:16]
input_char_count = len(input_str)

# ── Purpose: last user message from transcript ─────────────────────────────
purpose_full       = ""
purpose_preview    = ""
purpose_char_count = 0
if transcript and os.path.isfile(transcript):
    try:
        last_user = ""
        with open(transcript) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    role = d.get("role", "")
                    if role == "user":
                        content = d.get("content", "")
                        if isinstance(content, list):
                            parts = [c.get("text","") for c in content
                                     if isinstance(c, dict) and c.get("type") == "text"]
                            content = " ".join(parts)
                        if content:
                            last_user = str(content)  # full, no truncation
                except Exception:
                    pass
        purpose_full       = last_user
        purpose_preview    = last_user[:300]
        purpose_char_count = len(last_user)
    except Exception:
        pass

# ── Outcome: tool response ─────────────────────────────────────────────────
if isinstance(tool_resp, dict):
    resp_str = tool_resp.get("output", tool_resp.get("content", json.dumps(tool_resp)))
    exit_code = str(tool_resp.get("exit_code", tool_resp.get("returnCode", "")))
else:
    resp_str  = str(tool_resp)
    exit_code = ""

resp_full       = str(resp_str)           # full, no truncation
resp_preview    = resp_full[:400]
resp_char_count = len(resp_full)

# ── Fluency signals ────────────────────────────────────────────────────────
had_error  = 0
error_type = None

# Exit code signal
if exit_code and exit_code not in ("", "0", "None"):
    had_error  = 1
    error_type = "exit_nonzero"

# Error keywords in response
error_keywords = ["Error:", "error:", "Exception:", "Traceback", "FAILED", "fatal:"]
if any(kw in resp_preview for kw in error_keywords):
    had_error  = 1
    error_type = error_type or "error_in_output"

# ── Sequence number within session ─────────────────────────────────────────
conn = sqlite3.connect(db_path)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")

# Ensure session row exists
conn.execute("""
    INSERT OR IGNORE INTO sessions
        (session_id, project, project_dir, git_branch, git_sha,
         hostname, hook_version, started_at)
    VALUES (?,?,?,?,?,?,?,?)
""", (session_id, project, project_dir, git_branch, git_sha,
      hostname, hook_version, timestamp))

# Get next sequence number for this session
row = conn.execute(
    "SELECT event_count FROM sessions WHERE session_id = ?",
    (session_id,)
).fetchone()
seq_n = (row[0] if row else 0) + 1

# Detect retry: same tool + same input hash in this session
retry_row = conn.execute(
    "SELECT id FROM events WHERE session_id=? AND tool_name=? AND tool_input_hash=?",
    (session_id, tool_name, input_hash)
).fetchone()
is_retry = 1 if retry_row else 0

event_id = f"{session_id}:{seq_n}"

# ── Write event row ────────────────────────────────────────────────────────
conn.execute("""
    INSERT OR IGNORE INTO events (
        event_id, session_id, project, project_dir, sequence_n, timestamp,
        tool_name, tool_input_preview, tool_input_hash, tool_input_char_count,
        purpose_preview, purpose_char_count,
        cwd, git_branch, git_sha,
        tool_response_preview, tool_response_char_count, tool_exit_code,
        had_error, error_type, is_retry,
        hook_script, hook_version, hostname
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
""", (
    event_id, session_id, project, project_dir, seq_n, timestamp,
    tool_name, input_preview, input_hash, input_char_count,
    purpose_preview, purpose_char_count,
    cwd, git_branch, git_sha,
    resp_preview, resp_char_count, exit_code,
    had_error, error_type, is_retry,
    hook_script, hook_version, hostname
))

# Update session event count
conn.execute(
    "UPDATE sessions SET event_count=? WHERE session_id=?",
    (seq_n, session_id)
)

# ── Write full content (no truncation) ────────────────────────────────────
conn.execute("""
    INSERT OR IGNORE INTO event_content
        (event_id, tool_input_full, tool_response_full, purpose_full)
    VALUES (?,?,?,?)
""", (event_id, input_str, resp_full, purpose_full))

conn.commit()
conn.close()

# ── JSONL archive (one file per project per day) ───────────────────────────
date_str  = timestamp[:10]
log_file  = os.path.join(archive_dir, f"{project}-{date_str}.jsonl")
record = {
    "event_id": event_id,
    "session_id": session_id,
    "project": project,
    "timestamp": timestamp,
    "tool_name": tool_name,
    "tool_input_hash": input_hash,
    "tool_input_preview": input_preview,
    "tool_input_char_count": input_char_count,
    "purpose_preview": purpose_preview,
    "git_branch": git_branch,
    "git_sha": git_sha,
    "had_error": had_error,
    "error_type": error_type,
    "is_retry": is_retry,
    "exit_code": exit_code,
    "hook_version": hook_version,
}
try:
    with open(log_file, "a") as f:
        f.write(json.dumps(record) + "\n")
except Exception:
    pass

PYEOF

exit 0
