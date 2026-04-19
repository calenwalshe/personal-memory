#!/usr/bin/env bash
# sessionend-db-update.sh v1.1
# SessionEnd hook — closes session row, extracts full transcript into messages table,
# writes end snapshot with git diff vs session start, and builds sequence records.
# async: true

set -uo pipefail

HOOK_VERSION="1.1"
HOOK_SCRIPT="sessionend-db-update.sh"
DB="${MEMORY_VAULT:-$HOME/memory/vault}/events.db"

PAYLOAD=$(cat)

python3 - <<PYEOF
import json, sqlite3, os, sys, subprocess, hashlib, re
from datetime import datetime, timezone

payload_raw = """${PAYLOAD}"""
db_path = """${DB}"""
hook_script = """${HOOK_SCRIPT}"""
hook_version = """${HOOK_VERSION}"""
project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
hostname = os.uname().nodename.split(".")[0]

try:
    payload = json.loads(payload_raw)
except Exception:
    sys.exit(0)

session_id = payload.get("session_id", "unknown")
matcher    = payload.get("matcher", "other")
transcript = payload.get("transcript_path", "")
timestamp  = datetime.now(timezone.utc).isoformat()
project    = os.path.basename(project_dir)

if not os.path.isfile(db_path):
    sys.exit(0)

conn = sqlite3.connect(db_path)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")

# ── Close session row ──────────────────────────────────────────────────────
conn.execute("""
    UPDATE sessions SET ended_at=?, matcher=? WHERE session_id=?
""", (timestamp, matcher, session_id))

# ── Git state at session end ───────────────────────────────────────────────
def git(cmd, cwd=project_dir):
    try:
        return subprocess.check_output(
            ["git", "-C", cwd] + cmd,
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return ""

git_branch = git(["rev-parse", "--abbrev-ref", "HEAD"]) or "none"
git_sha    = git(["rev-parse", "--short", "HEAD"]) or "none"
git_log    = git(["log", "--oneline", "-15"])
git_status = git(["status", "--short"])

# Get session start SHA for diff
start_row = conn.execute("""
    SELECT git_sha FROM session_snapshots
    WHERE session_id=? AND snapshot_type='start'
""", (session_id,)).fetchone()
start_sha = start_row[0] if start_row else None
git_diff_stat = ""
if start_sha and start_sha != "none":
    git_diff_stat = git(["diff", "--stat", start_sha, "HEAD"])

# File context
import glob, time
recently_modified = []
try:
    cutoff = time.time() - 86400
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                   ('node_modules', '__pycache__', '.git', 'venv', '.venv')]
        for f in files:
            fpath = os.path.join(root, f)
            try:
                if os.path.getmtime(fpath) > cutoff:
                    recently_modified.append(os.path.relpath(fpath, project_dir))
            except Exception:
                pass
    recently_modified = recently_modified[:30]
except Exception:
    pass

file_count = 0
try:
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                   ('node_modules', '__pycache__', '.git', 'venv', '.venv')]
        file_count += len(files)
except Exception:
    pass

# away_summary from transcript
away_summary = ""
if transcript and os.path.isfile(transcript):
    try:
        entries = []
        with open(transcript) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    d = json.loads(line)
                    if d.get("type") == "system" and d.get("subtype") == "away_summary":
                        entries.append(d.get("content", "").strip())
                except Exception:
                    pass
        if entries:
            away_summary = entries[-1]
    except Exception:
        pass

# Write end snapshot
snapshot_id = f"{session_id}:end"
conn.execute("""
    INSERT OR REPLACE INTO session_snapshots (
        snapshot_id, session_id, project, snapshot_type, timestamp,
        git_branch, git_sha, git_log_recent, git_status_short,
        git_diff_stat, git_diff_stat_start_sha,
        project_file_count, recently_modified,
        away_summary,
        hook_script, hook_version, hostname
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
""", (
    snapshot_id, session_id, project, "end", timestamp,
    git_branch, git_sha, git_log, git_status,
    git_diff_stat, start_sha or "",
    file_count, json.dumps(recently_modified),
    away_summary,
    hook_script, hook_version, hostname
))

# ── NOTE: messages and sequences extraction removed ──────────────────────
# Turn extraction is now handled by sessionend-extract-turns.py (v1.0)
# which writes to the turns table instead of messages/sequences.

conn.commit()
conn.close()
PYEOF

exit 0
