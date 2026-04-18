"""
sessionend-extract-turns.py — Extract turns from transcript JSONL into turns table.

A turn = one cycle of human interaction:
  user message -> model thinking -> tool calls + results -> model text response

Reads the SessionEnd hook payload from stdin, parses the transcript JSONL,
builds turn records, and writes them to the turns table in events.db.

Filters out hook-spawned sessions (claude -p) by checking entrypoint field.
"""

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone


def strip_system_tags(text):
    """Remove <system-reminder>, command tags, and similar injected noise from user messages."""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
    text = re.sub(r"<local-command-caveat>.*?</local-command-caveat>", "", text, flags=re.DOTALL)
    text = re.sub(r"<local-command-stdout>.*?</local-command-stdout>", "", text, flags=re.DOTALL)
    # Strip /command XML wrappers but keep the command name and args
    m = re.match(
        r"<command-name>/(\w+)</command-name>\s*"
        r"<command-message>\w+</command-message>\s*"
        r"<command-args>(.*?)</command-args>",
        text,
        flags=re.DOTALL,
    )
    if m:
        cmd, args = m.group(1), m.group(2).strip()
        text = f"/{cmd} {args}".strip() if args else f"/{cmd}"
    return text.strip()


def extract_user_text(entry):
    """Extract plain text from a user message entry. Returns None if not a human message."""
    msg = entry.get("message", {})
    if isinstance(msg, str):
        return msg.strip() or None

    content = msg.get("content", "")

    # If content is a list, check for tool_result blocks (automated agentic loop)
    if isinstance(content, list):
        has_tool_result = any(
            isinstance(c, dict) and c.get("type") == "tool_result"
            for c in content
        )
        if has_tool_result:
            return None  # This is a tool result fed back, not a human message

        # Extract text parts
        text_parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                text_parts.append(c.get("text", ""))
            elif isinstance(c, str):
                text_parts.append(c)
        content = "\n".join(text_parts)

    text = strip_system_tags(str(content)).strip()
    return text if text else None


def extract_turns(transcript_path, session_id, project, project_dir):
    """Parse transcript JSONL into turn records."""
    with open(transcript_path) as f:
        entries = []
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Filter: skip hook sessions (claude -p calls use sdk-cli entrypoint)
    for entry in entries:
        ep = entry.get("entrypoint")
        if ep:
            if ep != "cli":
                return []  # Not an interactive session
            break

    turns = []
    current = None
    turn_n = 0

    def finalize_turn(t):
        """Finalize a turn record before appending."""
        if not t or not t.get("user_message"):
            return None

        thinking = "\n\n".join(t["_thinking_parts"]) if t["_thinking_parts"] else None
        response = "\n\n".join(t["_text_parts"]) if t["_text_parts"] else None
        tool_names_list = list(dict.fromkeys(tc["name"] for tc in t["_tool_calls"]))

        return {
            "turn_id": f"{session_id}:turn:{t['turn_n']}",
            "session_id": session_id,
            "project": project,
            "project_dir": project_dir,
            "turn_n": t["turn_n"],
            "started_at": t["started_at"],
            "ended_at": t.get("ended_at"),
            "duration_ms": t.get("duration_ms"),
            "user_message": t["user_message"],
            "user_message_preview": t["user_message"][:300],
            "user_message_hash": hashlib.sha256(t["user_message"].encode()).hexdigest()[:16],
            "thinking_text": thinking,
            "thinking_preview": (thinking[:500] if thinking else None),
            "response_text": response,
            "response_preview": (response[:500] if response else None),
            "tool_calls": json.dumps(
                [
                    {
                        "name": tc["name"],
                        "input_preview": tc["input_preview"],
                        "had_error": tc["had_error"],
                    }
                    for tc in t["_tool_calls"]
                ]
            )
            if t["_tool_calls"]
            else None,
            "tool_call_count": len(t["_tool_calls"]),
            "tool_names": json.dumps(tool_names_list) if tool_names_list else None,
            "input_tokens": t["_tokens"]["input"] or None,
            "output_tokens": t["_tokens"]["output"] or None,
            "cache_read_tokens": t["_tokens"]["cache_read"] or None,
            "cache_create_tokens": t["_tokens"]["cache_create"] or None,
            "cwd": t.get("cwd"),
            "git_branch": t.get("git_branch"),
            "git_sha": t.get("git_sha"),
            "had_error": 1 if t["_error_count"] > 0 else 0,
            "error_count": t["_error_count"],
            "agentic_loops": t["_agentic_loops"],
        }

    def new_turn(user_text, entry, n):
        return {
            "turn_n": n,
            "started_at": entry.get("timestamp", ""),
            "ended_at": None,
            "duration_ms": None,
            "user_message": user_text,
            "cwd": entry.get("cwd", ""),
            "git_branch": entry.get("gitBranch", ""),
            "git_sha": entry.get("gitSha", ""),
            "_thinking_parts": [],
            "_text_parts": [],
            "_tool_calls": [],
            "_tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0},
            "_error_count": 0,
            "_agentic_loops": 0,
        }

    for entry in entries:
        entry_type = entry.get("type", "")

        # Skip non-conversation entries
        if entry_type in (
            "permission-mode",
            "file-history-snapshot",
            "last-prompt",
            "queue-operation",
        ):
            continue

        # Attachments: skip (tool results are in the user tool_result messages)
        if entry_type == "attachment":
            continue

        # User message
        if entry_type == "user":
            # Skip meta messages (skill listings, system context)
            if entry.get("isMeta"):
                continue

            user_text = extract_user_text(entry)
            if user_text is None:
                continue  # tool_result or empty

            # Close previous turn
            if current:
                finalized = finalize_turn(current)
                if finalized:
                    turns.append(finalized)

            # Start new turn
            turn_n += 1
            current = new_turn(user_text, entry, turn_n)
            continue

        # Assistant message
        if entry_type == "assistant" and current is not None:
            msg = entry.get("message", entry)
            blocks = msg.get("content", [])
            if isinstance(blocks, str):
                blocks = [{"type": "text", "text": blocks}]

            stop_reason = msg.get("stop_reason", "")

            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")

                if btype == "thinking":
                    thinking = block.get("thinking", "")
                    if thinking:
                        current["_thinking_parts"].append(thinking)

                elif btype == "text":
                    text = block.get("text", "")
                    if text:
                        current["_text_parts"].append(text)

                elif btype == "tool_use":
                    current["_agentic_loops"] += 1
                    current["_tool_calls"].append(
                        {
                            "name": block.get("name", ""),
                            "input_preview": json.dumps(
                                block.get("input", {})
                            )[:400],
                            "had_error": False,
                            "tool_use_id": block.get("id", ""),
                        }
                    )

            # Aggregate token usage
            usage = msg.get("usage", {})
            if usage:
                current["_tokens"]["input"] += usage.get("input_tokens", 0)
                current["_tokens"]["output"] += usage.get("output_tokens", 0)
                current["_tokens"]["cache_read"] += usage.get(
                    "cache_read_input_tokens", 0
                )
                current["_tokens"]["cache_create"] += usage.get(
                    "cache_creation_input_tokens", 0
                )

            # Track end timestamp
            if stop_reason == "end_turn":
                current["ended_at"] = entry.get("timestamp", "")

            # Detect errors in tool results from content blocks
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "")
                            for c in content
                            if isinstance(c, dict)
                        )
                    if any(
                        kw in str(content)
                        for kw in [
                            "Error:",
                            "error:",
                            "Traceback",
                            "FAILED",
                            "fatal:",
                            "Exit code 1",
                        ]
                    ):
                        current["_error_count"] += 1
                        # Mark the most recent tool call as errored
                        if current["_tool_calls"]:
                            current["_tool_calls"][-1]["had_error"] = True

            continue

    # Close final turn
    if current:
        finalized = finalize_turn(current)
        if finalized:
            turns.append(finalized)

    return turns


def write_turns(db_path, turns):
    """Write turn records to the turns table."""
    if not turns:
        return 0

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    now = datetime.now(timezone.utc).isoformat()
    written = 0

    for t in turns:
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO turns (
                    turn_id, session_id, project, project_dir, turn_n,
                    started_at, ended_at, duration_ms,
                    user_message, user_message_preview, user_message_hash,
                    thinking_text, thinking_preview,
                    response_text, response_preview,
                    tool_calls, tool_call_count, tool_names,
                    input_tokens, output_tokens, cache_read_tokens, cache_create_tokens,
                    cwd, git_branch, git_sha,
                    had_error, error_count, agentic_loops,
                    extracted_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
                (
                    t["turn_id"],
                    t["session_id"],
                    t["project"],
                    t["project_dir"],
                    t["turn_n"],
                    t["started_at"],
                    t["ended_at"],
                    t["duration_ms"],
                    t["user_message"],
                    t["user_message_preview"],
                    t["user_message_hash"],
                    t["thinking_text"],
                    t["thinking_preview"],
                    t["response_text"],
                    t["response_preview"],
                    t["tool_calls"],
                    t["tool_call_count"],
                    t["tool_names"],
                    t["input_tokens"],
                    t["output_tokens"],
                    t["cache_read_tokens"],
                    t["cache_create_tokens"],
                    t["cwd"],
                    t["git_branch"],
                    t["git_sha"],
                    t["had_error"],
                    t["error_count"],
                    t["agentic_loops"],
                    now,
                ),
            )
            written += 1
        except sqlite3.IntegrityError:
            pass  # duplicate turn_id, skip

    conn.commit()
    conn.close()
    return written


def main():
    db_path = os.environ.get("MEMORY_DB", os.path.expanduser("~/memory/vault/events.db"))
    hook_version = os.environ.get("HOOK_VERSION", "1.0")

    payload_raw = sys.stdin.read()
    try:
        payload = json.loads(payload_raw)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    session_id = payload.get("session_id", "unknown")
    transcript = payload.get("transcript_path", "")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    project = os.path.basename(project_dir)

    if not transcript or not os.path.isfile(transcript):
        sys.exit(0)

    if not os.path.isfile(db_path):
        sys.exit(0)

    turns = extract_turns(transcript, session_id, project, project_dir)
    written = write_turns(db_path, turns)

    # Log result
    result = {
        "session_id": session_id,
        "project": project,
        "turns_extracted": len(turns),
        "turns_written": written,
    }
    print(json.dumps(result), file=sys.stderr)


if __name__ == "__main__":
    main()
