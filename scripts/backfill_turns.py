"""
backfill_turns.py — Bulk extract turns from historical transcript JSONL files.

Scans ~/.claude/projects/*/  for *.jsonl transcript files, runs the same
extraction logic as sessionend-extract-turns.py, and writes turns to events.db.

Skips:
- Sessions already extracted (session_id exists in turns table)
- Subagent transcripts (*/subagents/*)
- Non-interactive sessions (entrypoint != "cli")
- Empty/tiny transcripts (<3 lines)

Usage:
    python3 backfill_turns.py [--dry-run] [--project PATTERN] [--limit N]
"""

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

# Reuse the existing turn extraction logic
VAULT = Path(os.environ.get("MEMORY_VAULT", Path.home() / "memory/vault"))

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "sessionend_extract_turns",
    Path.home() / ".claude/hooks/sessionend-extract-turns.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
extract_turns = _mod.extract_turns
write_turns = _mod.write_turns


CLAUDE_PROJECTS = Path.home() / ".claude/projects"
DB_PATH = VAULT / "events.db"

# Map claude project dir names back to human-readable project names
# e.g. "-home-agent-projects-yt-dj" -> "yt-dj"
def dir_to_project(dirname: str) -> str:
    """Convert Claude's project dir name to a project name."""
    # Strip common prefixes
    name = dirname
    for prefix in ["-home-agent-projects-", "-home-agent-", "-tmp-", "-"]:
        if name.startswith(prefix) and len(name) > len(prefix):
            name = name[len(prefix):]
            break
    return name or dirname


def find_transcripts(project_pattern=None):
    """Find all JSONL transcript files, excluding subagents."""
    transcripts = []
    for jsonl in CLAUDE_PROJECTS.rglob("*.jsonl"):
        # Skip subagent transcripts
        if "subagents" in str(jsonl):
            continue
        # Skip non-project dirs (like skills)
        if "skills" in str(jsonl):
            continue

        # Filter by project pattern if specified
        project_dir = jsonl.parent.name
        project_name = dir_to_project(project_dir)
        if project_pattern and project_pattern.lower() not in project_name.lower():
            continue

        # Skip tiny files (<3 lines = probably empty/hook session)
        try:
            with open(jsonl) as f:
                line_count = sum(1 for _ in f)
            if line_count < 3:
                continue
        except OSError:
            continue

        # Session ID is the filename without extension
        session_id = jsonl.stem

        transcripts.append({
            "path": jsonl,
            "session_id": session_id,
            "project_dir": project_dir,
            "project_name": project_name,
            "line_count": line_count,
        })

    return sorted(transcripts, key=lambda t: t["project_name"])


def get_extracted_sessions(conn):
    """Get set of session IDs already in the turns table."""
    rows = conn.execute("SELECT DISTINCT session_id FROM turns").fetchall()
    return {r[0] for r in rows}


def main():
    dry_run = "--dry-run" in sys.argv
    project_pattern = None
    limit = None

    for i, arg in enumerate(sys.argv):
        if arg == "--project" and i + 1 < len(sys.argv):
            project_pattern = sys.argv[i + 1]
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    already_extracted = get_extracted_sessions(conn)
    print(f"Sessions already extracted: {len(already_extracted)}")

    transcripts = find_transcripts(project_pattern)
    print(f"Transcript files found: {len(transcripts)}")

    # Filter out already-extracted
    to_process = [t for t in transcripts if t["session_id"] not in already_extracted]
    print(f"New transcripts to process: {len(to_process)}")

    if limit:
        to_process = to_process[:limit]
        print(f"Limited to: {limit}")

    if not to_process:
        print("Nothing to backfill.")
        conn.close()
        return

    # Group by project for reporting
    by_project = {}
    for t in to_process:
        by_project.setdefault(t["project_name"], []).append(t)

    print(f"\nProjects to process: {len(by_project)}")
    for proj, items in sorted(by_project.items(), key=lambda x: -len(x[1])):
        print(f"  {proj}: {len(items)} transcripts")

    if dry_run:
        print("\n[dry-run] Would process the above. Exiting.")
        conn.close()
        return

    start = time.monotonic()
    total_turns = 0
    total_written = 0
    total_skipped = 0
    processed = 0

    for t in to_process:
        transcript_path = str(t["path"])
        session_id = t["session_id"]
        project_name = t["project_name"]

        # Derive project_dir (the actual filesystem path the project lives at)
        # We use the project name as-is since that's what the turns table expects
        project_dir = str(t["path"].parent)

        try:
            turns = extract_turns(transcript_path, session_id, project_name, project_dir)
        except Exception as e:
            print(f"  ERROR extracting {session_id}: {e}", file=sys.stderr)
            total_skipped += 1
            continue

        if not turns:
            total_skipped += 1
            continue

        written = write_turns(str(DB_PATH), turns)
        total_turns += len(turns)
        total_written += written
        processed += 1

        if processed % 100 == 0:
            elapsed = round(time.monotonic() - start, 1)
            print(f"  [{processed}/{len(to_process)}] {total_written} turns written, {elapsed}s")

    elapsed = round(time.monotonic() - start, 1)
    conn.close()

    print(f"\nBackfill complete:")
    print(f"  Transcripts processed: {processed}")
    print(f"  Transcripts skipped: {total_skipped} (non-interactive or empty)")
    print(f"  Turns extracted: {total_turns}")
    print(f"  Turns written: {total_written} (new)")
    print(f"  Time: {elapsed}s")


if __name__ == "__main__":
    main()
