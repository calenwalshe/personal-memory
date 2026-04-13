#!/usr/bin/env python3
"""
preprocess-sessions.py — JSONL session corpus → .md files

Reads ~/.claude/projects/**/*.jsonl, filters to user+assistant messages,
segments by session boundary (30-minute gap), writes one .md file per session
to a target output directory.

This is the foundation layer for contract-002 (agentic corpus mining).
It is NOT called by any v1 skills — it is a standalone preprocessing tool.

Usage:
    python3 preprocess-sessions.py [--output-dir DIR] [--dry-run] [--project SLUG]

Options:
    --output-dir DIR    Where to write .md files (default: ~/memory/vault/sessions/)
    --dry-run           Print what would be written without writing
    --project SLUG      Only process sessions from a specific project slug
    --since TIMESTAMP   Only process sessions modified after this ISO8601 timestamp
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_OUTPUT_DIR = Path.home() / "memory" / "vault" / "sessions"
SESSION_GAP = timedelta(minutes=30)

# Message types that carry semantic value
KEEP_TYPES = {"user", "assistant", "summary"}
# Noise types to strip
NOISE_TYPES = {"tool_use", "tool_result", "thinking", "system", "attachment",
               "file-history-snapshot", "permission-mode", "ping", "error"}


def extract_text(message) -> str:
    """Extract plain text from a message object. Handles string and list content."""
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        parts = []
        for block in message:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", "").strip())
                elif block.get("type") == "tool_result":
                    # Skip tool results — noise
                    pass
        return "\n".join(p for p in parts if p)
    if isinstance(message, dict):
        return message.get("text", "").strip()
    return ""


def parse_session_file(path: Path) -> list[dict]:
    """Parse a session JSONL file, returning filtered message events."""
    events = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type", "")

                # Skip explicit noise types
                if msg_type in NOISE_TYPES:
                    continue

                # For assistant/user messages, extract content
                if msg_type in ("user", "assistant"):
                    content = obj.get("message", {})
                    if isinstance(content, dict):
                        role = content.get("role", msg_type)
                        raw_content = content.get("content", "")
                    else:
                        role = msg_type
                        raw_content = content

                    text = extract_text(raw_content)
                    if not text or len(text) < 10:
                        continue

                    ts_raw = obj.get("timestamp", "")
                    try:
                        if isinstance(ts_raw, (int, float)):
                            ts = datetime.fromtimestamp(ts_raw / 1000, tz=timezone.utc)
                        else:
                            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    except (ValueError, OSError):
                        ts = datetime.now(timezone.utc)

                    events.append({
                        "role": role,
                        "text": text,
                        "timestamp": ts,
                        "session_id": obj.get("sessionId", "unknown"),
                    })

    except (OSError, PermissionError) as e:
        print(f"  [skip] {path.name}: {e}", file=sys.stderr)

    return events


def segment_sessions(events: list[dict]) -> list[list[dict]]:
    """Split events into sessions using time-gap segmentation."""
    if not events:
        return []

    sessions = []
    current = [events[0]]

    for event in events[1:]:
        gap = event["timestamp"] - current[-1]["timestamp"]
        if gap > SESSION_GAP:
            sessions.append(current)
            current = [event]
        else:
            current.append(event)

    if current:
        sessions.append(current)

    return sessions


def session_to_markdown(session: list[dict], project_slug: str) -> str:
    """Convert a session (list of events) to a markdown string."""
    if not session:
        return ""

    start_ts = session[0]["timestamp"].strftime("%Y-%m-%dT%H:%M:%SZ")
    end_ts = session[-1]["timestamp"].strftime("%Y-%m-%dT%H:%M:%SZ")
    session_id = session[0].get("session_id", "unknown")
    turn_count = len(session)

    lines = [
        f"<!-- session: {session_id} | project: {project_slug} -->",
        f"<!-- start: {start_ts} | end: {end_ts} | turns: {turn_count} -->",
        "",
    ]

    for event in session:
        role = event["role"].capitalize()
        text = event["text"]
        # Truncate very long assistant responses (tool outputs, code blocks)
        if role == "Assistant" and len(text) > 2000:
            text = text[:2000] + "\n[...truncated]"
        lines.append(f"**{role}:** {text}")
        lines.append("")

    return "\n".join(lines)


def get_project_slug(project_dir: Path) -> str:
    """Convert a .claude/projects/ directory name to a readable slug."""
    name = project_dir.name
    # Remove leading dash, replace remaining dashes with slashes for readability
    return name.lstrip("-").replace("-", "/", 1) if name.startswith("-home-agent") else name


def main():
    parser = argparse.ArgumentParser(description="Preprocess Claude Code session JSONL → markdown")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--project", help="Filter to specific project slug (partial match)")
    parser.add_argument("--since", help="Only process files modified after ISO8601 timestamp")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    since_dt = None
    if args.since:
        since_dt = datetime.fromisoformat(args.since.replace("Z", "+00:00"))

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    total_sessions = 0
    total_files = 0
    total_turns = 0

    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue

        project_slug = get_project_slug(project_dir)

        # Project filter
        if args.project and args.project.lower() not in project_slug.lower():
            continue

        jsonl_files = sorted(project_dir.glob("*.jsonl"))
        if not jsonl_files:
            continue

        for jsonl_path in jsonl_files:
            # Since filter
            if since_dt:
                mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=timezone.utc)
                if mtime < since_dt:
                    continue

            events = parse_session_file(jsonl_path)
            if not events:
                continue

            sessions = segment_sessions(events)

            for session in sessions:
                if len(session) < 2:
                    # Skip single-turn fragments
                    continue

                start_ts = session[0]["timestamp"].strftime("%Y%m%dT%H%M%SZ")
                project_safe = project_slug.replace("/", "-")
                filename = f"session-{start_ts}-{project_safe}.md"
                out_path = output_dir / filename

                if args.dry_run:
                    print(f"[dry-run] would write: {filename} ({len(session)} turns)")
                else:
                    md = session_to_markdown(session, project_slug)
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(md)

                total_sessions += 1
                total_turns += len(session)

            total_files += 1

    print(f"\nPreprocessing complete:")
    print(f"  JSONL files processed: {total_files}")
    print(f"  Sessions extracted:    {total_sessions}")
    print(f"  Total turns:           {total_turns}")
    if not args.dry_run:
        print(f"  Output directory:      {output_dir}")


if __name__ == "__main__":
    main()
