"""
procedural_extractor.py — Extract procedural memory facts from a session JSONL.

Procedural memory = how to do things, especially correction patterns:
  - Bugs fixed and the root-cause pattern
  - Mistakes corrected (wrong approach → right approach)
  - Gotchas, traps, anti-patterns encountered
  - Workflows or sequences that worked after iteration
  - Tool invocation patterns that solved a problem

Usage:
  python3 procedural_extractor.py --session <session_id> [--dry-run] [--session-file <path>]

Output: Facts written to fact_store with memory_type='procedural'.

Environment:
  LLM_PROVIDER=claude|codex (default: codex)
  VAULT_DIR=<path>           (default: ~/memory/vault)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
SESSIONS_DIR = Path.home() / ".claude/projects"
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "codex")

# Resolve binary paths at startup (handles nvm shims not in subprocess PATH)
_NVM_BIN = Path.home() / ".nvm/versions/node/v20.20.0/bin"
NODE_BIN = os.environ.get("NODE_BIN") or shutil.which("node") or str(_NVM_BIN / "node")
_claude_sym = shutil.which("claude") or str(_NVM_BIN / "claude")
CLAUDE_CLI = os.environ.get("CLAUDE_CLI") or str(Path(_claude_sym).resolve())
_codex_sym = shutil.which("codex") or str(_NVM_BIN / "codex")
CODEX_CLI = os.environ.get("CODEX_CLI") or str(Path(_codex_sym).resolve())

PROCEDURAL_PREAMBLE = """You are a procedural memory extractor. Your job is to identify correction
patterns and actionable how-to knowledge from a session transcript.

Procedural facts capture:
- Bugs fixed and the root-cause pattern (not just "X was fixed" but "X fails when Y, fix: Z")
- Mistakes corrected: wrong approach tried → why it failed → right approach
- Tool gotchas: invocation patterns, flags, or sequences that work vs. fail
- Anti-patterns encountered and how to avoid them
- Workflows that required multiple iterations to get right

Do NOT extract:
- Things that merely happened (that is episodic memory)
- General knowledge about how technologies work (that is semantic memory)
- Things that are obvious from documentation

Each procedural fact must:
- Describe a reusable, actionable pattern (not a one-time event)
- Be specific enough to be useful as a future reminder
- Include the context in which it applies

Always respond with valid JSON only. No markdown fences. No explanation."""


def llm(prompt: str, timeout: int = 90) -> str:
    if LLM_PROVIDER == "codex":
        cmd = [NODE_BIN, CODEX_CLI, "exec", "--full-auto", "--profile", "llm",
               "--cd", "/tmp", "--skip-git-repo-check", prompt]
    else:
        cmd = [NODE_BIN, CLAUDE_CLI, "-p", prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def llm_json(prompt: str) -> dict | list:
    raw = llm(PROCEDURAL_PREAMBLE + "\n\n" + prompt + "\n\nRespond with JSON only.")
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raw2 = llm(PROCEDURAL_PREAMBLE + "\n\n" + prompt +
                   "\n\nYour response MUST be raw JSON only. No text before or after.")
        raw2 = raw2.strip()
        if raw2.startswith("```"):
            raw2 = re.sub(r'^```[a-z]*\n?', '', raw2)
            raw2 = re.sub(r'\n?```$', '', raw2)
        try:
            return json.loads(raw2)
        except json.JSONDecodeError:
            return []


def load_session(session_id: str, session_file: Path = None) -> list[dict]:
    """Load all turns from a session JSONL."""
    if session_file:
        path = session_file
    else:
        matches = list(SESSIONS_DIR.rglob(f"{session_id}.jsonl"))
        if not matches:
            print(f"ERROR: session JSONL not found for {session_id}", file=sys.stderr)
            sys.exit(1)
        path = matches[0]

    turns = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return turns


def extract_session_date(turns: list[dict]) -> str:
    """Extract the date of the session from JSONL timestamps."""
    for turn in turns:
        ts = turn.get("timestamp") or turn.get("created_at") or turn.get("ts")
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%d")
            except (ValueError, AttributeError):
                pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def summarize_session(turns: list[dict], max_chars: int = 8000) -> str:
    """Build a condensed text summary of the session for LLM analysis."""
    lines = []
    char_count = 0

    for turn in turns:
        role = turn.get("type") or turn.get("role", "")
        if role in ("human", "user"):
            prefix = "USER"
        elif role in ("assistant", "ai"):
            prefix = "ASSISTANT"
        else:
            continue

        content = turn.get("message", {})
        if isinstance(content, dict):
            content = content.get("content", "")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            content = " ".join(parts)
        if not isinstance(content, str):
            content = str(content)

        if len(content) > 600:
            content = content[:600] + "...[truncated]"

        line = f"{prefix}: {content}"
        char_count += len(line)
        if char_count > max_chars:
            lines.append("[...session truncated for length...]")
            break
        lines.append(line)

    return "\n".join(lines)


def extract_procedural_facts(session_id: str, session_text: str, session_date: str) -> list[dict]:
    """Call LLM to extract procedural facts from the session text."""
    prompt = f"""Analyze this session transcript and extract procedural memory facts — correction
patterns and actionable how-to knowledge.

SESSION DATE: {session_date}
SESSION ID: {session_id}

TRANSCRIPT:
{session_text}

Look specifically for:
1. Bugs that were diagnosed and fixed — capture the pattern: "When X happens, the cause is Y, fix by Z"
2. Approach corrections — "First tried X (failed because Y), then did Z (worked)"
3. Tool or API gotchas — specific invocation patterns, flag combinations, or sequencing that matters
4. Anti-patterns — approaches that look right but fail, with explanation of why

Return a JSON array of fact objects.
Each object must have:
  - "content": string — a reusable, actionable description (1-3 sentences, pattern-focused)
  - "confidence": float between 0.0 and 1.0
  - "entities": array of strings — key technical entities (tools, files, functions, patterns)
  - "topic": string — one or two word topic label

Return [] if no significant correction patterns occurred in this session.
Return only the JSON array, no other text.

Example output:
[
  {{
    "content": "When shebang resolution fails for nvm-managed node binaries in subprocess calls, use 'node cli.js' directly instead of the shim. The shim path is not in subprocess PATH.",
    "confidence": 0.85,
    "entities": ["nvm", "node", "subprocess", "shebang"],
    "topic": "subprocess invocation"
  }}
]"""

    result = llm_json(prompt)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("facts", "patterns", "items", "results"):
            if key in result and isinstance(result[key], list):
                return result[key]
    return []


def run_extraction(
    session_id: str,
    session_file: Path = None,
    dry_run: bool = False,
) -> list[str]:
    """
    Extract procedural facts from a session and write to fact_store.
    Returns list of fact_ids written (empty on dry_run).
    """
    sys.path.insert(0, str(VAULT / "scripts"))
    from fact_store import init_db, add_fact

    turns = load_session(session_id, session_file)
    if not turns:
        print(f"WARNING: no turns found in session {session_id}", file=sys.stderr)
        return []

    session_date = extract_session_date(turns)
    session_text = summarize_session(turns)

    print(f"Extracting procedural facts from session {session_id} ({session_date})", file=sys.stderr)
    print(f"  Session length: {len(turns)} turns, {len(session_text)} chars", file=sys.stderr)

    raw_facts = extract_procedural_facts(session_id, session_text, session_date)
    print(f"  LLM returned {len(raw_facts)} candidate facts", file=sys.stderr)

    if dry_run:
        print(json.dumps(raw_facts, indent=2))
        return []

    init_db()
    ingestion_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    written_ids = []

    for i, fact in enumerate(raw_facts):
        content = (fact.get("content") or "").strip()
        confidence = float(fact.get("confidence", 0.5))
        entities = fact.get("entities") or []
        topic = fact.get("topic") or "procedural"

        if not content:
            print(f"  Skipping fact {i}: empty content", file=sys.stderr)
            continue
        if confidence < 0.4:
            print(f"  Skipping fact {i}: confidence {confidence:.2f} < 0.4", file=sys.stderr)
            continue

        turn_range = [0, len(turns)]

        fact_id = add_fact(
            content=content,
            session_id=session_id,
            valid_from=session_date,
            topic=topic,
            entities=entities,
            confidence=confidence,
            importance=0.8,  # procedural knowledge tends to be high-value
            scope="procedural",
            turn_range=turn_range,
            memory_type="procedural",
            event_time=None,  # procedural facts don't have a specific event time
            ingestion_time=ingestion_time,
        )
        written_ids.append(fact_id)
        print(f"  Wrote fact [{confidence:.2f}]: {content[:80]}...", file=sys.stderr)

    print(f"  Done: {len(written_ids)} facts written", file=sys.stderr)
    return written_ids


def main():
    parser = argparse.ArgumentParser(description="Extract procedural memory facts from a session")
    parser.add_argument("--session", required=True, help="Session ID (JSONL basename without .jsonl)")
    parser.add_argument("--session-file", help="Direct path to session JSONL file")
    parser.add_argument("--dry-run", action="store_true", help="Print extracted facts without writing to DB")
    args = parser.parse_args()

    session_file = Path(args.session_file) if args.session_file else None
    fact_ids = run_extraction(args.session, session_file=session_file, dry_run=args.dry_run)

    if not args.dry_run:
        print(json.dumps({"session_id": args.session, "facts_written": len(fact_ids), "fact_ids": fact_ids}))


if __name__ == "__main__":
    main()
