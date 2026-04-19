#!/usr/bin/env bash
# sessionstart-snapshot.sh v1.0
# SessionStart hook — captures the full context state at the beginning of a session.
# Writes a 'start' snapshot to session_snapshots and seeds the sessions row.
# This is the baseline everything else is diffed against.

set -uo pipefail

HOOK_VERSION="1.0"
HOOK_SCRIPT="sessionstart-snapshot.sh"
DB="${MEMORY_VAULT:-$HOME/memory/vault}/events.db"

PAYLOAD=$(cat)

python3 - <<PYEOF
import json, sqlite3, os, sys, subprocess, hashlib
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
    payload = {}

session_id = payload.get("session_id", "unknown")
timestamp  = datetime.now(timezone.utc).isoformat()
project    = os.path.basename(project_dir)

# ── Git state ──────────────────────────────────────────────────────────────
def git(cmd, cwd=project_dir):
    try:
        return subprocess.check_output(
            ["git", "-C", cwd] + cmd,
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return ""

git_branch    = git(["rev-parse", "--abbrev-ref", "HEAD"]) or "none"
git_sha       = git(["rev-parse", "--short", "HEAD"]) or "none"
git_log       = git(["log", "--oneline", "-15"])
git_status    = git(["status", "--short"])

# ── File context ───────────────────────────────────────────────────────────
# Files modified in last 24 hours
import glob, time
recently_modified = []
try:
    cutoff = time.time() - 86400
    for root, dirs, files in os.walk(project_dir):
        # Skip hidden dirs and common noise
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                   ('node_modules', '__pycache__', '.git', 'venv', '.venv')]
        for f in files:
            fpath = os.path.join(root, f)
            try:
                if os.path.getmtime(fpath) > cutoff:
                    recently_modified.append(
                        os.path.relpath(fpath, project_dir)
                    )
            except Exception:
                pass
    recently_modified = recently_modified[:30]  # cap at 30
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

# ── Write to DB ────────────────────────────────────────────────────────────
if not os.path.isfile(db_path):
    sys.exit(0)

conn = sqlite3.connect(db_path)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")

# Seed the session row
conn.execute("""
    INSERT OR IGNORE INTO sessions
        (session_id, project, project_dir, git_branch, git_sha,
         hostname, hook_version, started_at)
    VALUES (?,?,?,?,?,?,?,?)
""", (session_id, project, project_dir, git_branch, git_sha,
      hostname, hook_version, timestamp))

# Write start snapshot
snapshot_id = f"{session_id}:start"
conn.execute("""
    INSERT OR IGNORE INTO session_snapshots (
        snapshot_id, session_id, project, snapshot_type, timestamp,
        git_branch, git_sha, git_log_recent, git_status_short,
        project_file_count, recently_modified,
        hook_script, hook_version, hostname
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
""", (
    snapshot_id, session_id, project, "start", timestamp,
    git_branch, git_sha, git_log, git_status,
    file_count, json.dumps(recently_modified),
    hook_script, hook_version, hostname
))

conn.commit()
conn.close()
PYEOF

exit 0
