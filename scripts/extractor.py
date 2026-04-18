"""
extractor.py — Two-source fact extraction for the vault promote pipeline.

Source 1: L1 episode files (vault/raw/sessions/*.md)
  → episodic facts: what happened, decisions made, outcomes

Source 2: messages table in events.db
  → procedural/semantic facts: patterns, gotchas, how-to knowledge

Both sources always populate project_scope from episode frontmatter or DB.
Uses claude -p --model haiku (default) for extraction.

Usage:
  from extractor import promote_session
  result = promote_session(session_id, model="claude-haiku-4-5-20251001", dry_run=False)
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("MEMORY_VAULT", Path.home() / "memory/vault"))
EVENTS_DB = VAULT / "events.db"
SESSIONS_DIR = VAULT / "raw" / "sessions"

_NVM_BIN = Path.home() / ".nvm/versions/node/v20.20.0/bin"
CLAUDE_BIN = (
    os.environ.get("CLAUDE_BIN")
    or shutil.which("claude")
    or str(_NVM_BIN / "claude")
)

sys.path.insert(0, str(VAULT / "scripts"))


# ── Helpers ────────────────────────────────────────────────────────────────

def _llm(prompt: str, model: str, timeout: int = 45) -> str:
    # Unset ANTHROPIC_API_KEY so claude -p uses subscription, not API credits
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    result = subprocess.run(
        [CLAUDE_BIN, "-p", "--model", model, prompt],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    return result.stdout.strip()


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_raw = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    meta = {}
    for line in fm_raw.splitlines():
        m = re.match(r'^([\w_-]+):\s*"?(.*?)"?\s*$', line)
        if m:
            meta[m.group(1)] = m.group(2)
    return meta, body


def _parse_haiku_fields(body: str) -> dict:
    fields = {}
    for key in ("Project area", "Accomplished", "Decisions", "Open thread", "status"):
        m = re.search(rf"^{re.escape(key)}:\s*(.+)", body, re.MULTILINE)
        if m:
            fields[key.lower().replace(" ", "_")] = m.group(1).strip()
    return fields


def _extract_json_array(text: str) -> list[dict]:
    """Pull the first JSON array out of LLM output (handles markdown fences)."""
    text = re.sub(r"```(?:json)?\s*", "", text).replace("```", "")
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return []


def _find_episodes(session_id: str) -> list[Path]:
    """Find all L1 episode files that belong to this session_id.
    Matches only on frontmatter session_id field — not body text."""
    results = []
    if not SESSIONS_DIR.exists():
        return results
    # Build pattern that matches the frontmatter line, not body mentions
    pattern = re.compile(r'^session_id:\s*"?' + re.escape(session_id) + r'"?\s*$', re.MULTILINE)
    for f in sorted(SESSIONS_DIR.glob("*.md")):
        try:
            head = f.read_text(errors="replace")[:600]
            if pattern.search(head):
                results.append(f)
        except Exception:
            pass
    return results


def _get_messages(session_id: str, char_limit: int = 8000) -> list[dict]:
    """Fetch messages for a session from events.db, capped at char_limit."""
    if not EVENTS_DB.exists():
        return []
    conn = sqlite3.connect(str(EVENTS_DB))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT role, content_full, sequence_n FROM messages "
            "WHERE session_id = ? ORDER BY sequence_n ASC",
            [session_id],
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()

    messages = []
    total = 0
    for r in rows:
        content = r["content_full"] or ""
        # Skip system/tool noise
        if content.startswith("<local-command-caveat>") or content.startswith("<bash-"):
            continue
        if total + len(content) > char_limit:
            break
        messages.append({"role": r["role"], "content": content})
        total += len(content)
    return messages


# ── Source 1: Episode extraction ───────────────────────────────────────────

EPISODIC_PROMPT = """You are extracting episodic memory facts from a session summary.

Episodic facts = significant things that HAPPENED in this session:
- Decisions made and why
- Things built, shipped, fixed, broken, or discovered
- Approaches tried and abandoned
- Blockers hit and how resolved
- Open threads (unfinished work)

Rules:
- Each fact must be a single concrete statement (1 sentence)
- Be specific: name the project, tool, file, or outcome
- Do NOT extract general knowledge (that is semantic memory)
- Do NOT extract how-to instructions (that is procedural memory)
- 3-6 facts per session, quality over quantity

Respond ONLY with a JSON array, no other text:
[
  {
    "content": "...",
    "topic": "short label",
    "entities": ["Entity1", "Entity2"],
    "confidence": 0.0-1.0,
    "importance": 0.0-1.0,
    "scope": "technical|decision|outcome|blocker"
  }
]

Session data:
"""

def extract_from_episode(episode_path: Path, model: str, dry_run: bool = False,
                          skip_faiss: bool = False) -> list[str]:
    """Extract episodic facts from one L1 episode file. Returns list of fact IDs written."""
    from fact_store import batch_add_facts

    text = episode_path.read_text(errors="replace")
    meta, body = _parse_frontmatter(text)

    session_id = meta.get("session_id", "").strip('"')
    project = meta.get("project", "").strip('"')
    timestamp = meta.get("timestamp", "").strip('"')
    valid_from = timestamp[:10] if timestamp else datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Skip if no meaningful project or session
    if not session_id or not project:
        return []

    # Skip episodes where Haiku had no transcript to work from — no content to extract
    haiku_fallback = meta.get("haiku_fallback_reason", "").strip('"')
    if haiku_fallback == "no_transcript":
        return []

    # Pull away_summary and haiku fields
    away_m = re.search(r"## away_summary.*?\n(.*?)(?=\n## |\Z)", body, re.DOTALL)
    away_summary = away_m.group(1).strip() if away_m else ""

    haiku_m = re.search(r"## haiku_summary.*?\n(.*?)(?=\n## |\Z)", body, re.DOTALL)
    haiku_raw = haiku_m.group(1).strip() if haiku_m else ""
    haiku = _parse_haiku_fields(haiku_raw)

    # Placeholders that indicate no real content
    _EMPTY = {
        "", "none", "unknown", "n/a",
        "[haiku unavailable]",
        "[no transcript available]",
        "(not present this session)",
        "[haiku unavailable — session cleared]",
    }

    def _is_meaningful(s: str) -> bool:
        return bool(s) and s.strip().lower() not in _EMPTY and len(s.strip()) > 10

    content_signals = [
        away_summary,
        haiku.get("accomplished", ""),
        haiku.get("decisions", ""),
        haiku.get("open_thread", ""),
    ]
    if not any(_is_meaningful(s) for s in content_signals):
        return []

    # Build prompt input
    session_data = f"""Project: {project}
Date: {valid_from}
Event type: {meta.get('event', '')}
Git branch: {meta.get('git_branch', '')}

Summary (native): {away_summary or '(none)'}

Structured summary:
  Project area: {haiku.get('project_area', '(none)')}
  Accomplished: {haiku.get('accomplished', '(none)')}
  Decisions: {haiku.get('decisions', '(none)')}
  Open thread: {haiku.get('open_thread', '(none)')}
  Status: {haiku.get('status', '(none)')}"""

    raw = _llm(EPISODIC_PROMPT + session_data, model)
    facts = _extract_json_array(raw)

    if not facts:
        return []

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    batch = []
    dry_labels = []
    for f in facts:
        content = f.get("content", "").strip()
        if not content:
            continue
        if dry_run:
            dry_labels.append(f"dry:{content[:40]}")
        else:
            batch.append({
                "content": content,
                "session_id": session_id,
                "valid_from": valid_from,
                "topic": f.get("topic"),
                "entities": f.get("entities", []),
                "confidence": float(f.get("confidence", 0.7)),
                "importance": float(f.get("importance", 0.5)),
                "scope": f.get("scope", "outcome"),
                "memory_type": "episodic",
                "project_scope": project,
                "event_time": timestamp or None,
                "ingestion_time": now,
            })

    if dry_run:
        return dry_labels
    return batch_add_facts(batch, skip_faiss=skip_faiss) if batch else []


# ── Source 2: Message extraction ───────────────────────────────────────────

PROCEDURAL_PROMPT = """You are extracting procedural and semantic memory facts from a conversation.

Procedural facts = how to do things:
- Patterns that worked or didn't
- Gotchas, fixes, workarounds
- Step-by-step lessons
- "Always do X when Y"

Semantic facts = general knowledge established in this session:
- How a system works
- What a tool/service does
- Definitions or constraints discovered

Rules:
- Each fact must be a single concrete statement (1 sentence)
- Be specific: name the tool, command, service, or constraint
- Do NOT extract events (what happened) — that is episodic memory
- 3-8 facts per session, quality over quantity

Respond ONLY with a JSON array, no other text:
[
  {
    "content": "...",
    "topic": "short label",
    "entities": ["Entity1", "Entity2"],
    "confidence": 0.0-1.0,
    "importance": 0.0-1.0,
    "memory_type": "procedural|semantic",
    "scope": "technical|learning|reference"
  }
]

Conversation:
"""

def extract_from_messages(session_id: str, project: str, valid_from: str,
                           model: str, dry_run: bool = False,
                           skip_faiss: bool = False) -> list[str]:
    """Extract procedural/semantic facts from messages table. Returns list of fact IDs."""
    from fact_store import batch_add_facts

    messages = _get_messages(session_id)
    if not messages:
        return []

    # Format conversation for LLM
    lines = []
    for m in messages:
        role = m["role"].upper()
        content = (m["content"] or "")[:300]
        lines.append(f"{role}: {content}")
    conversation = "\n".join(lines)

    if len(conversation.strip()) < 100:
        return []

    raw = _llm(PROCEDURAL_PROMPT + conversation, model)
    facts = _extract_json_array(raw)

    if not facts:
        return []

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    batch = []
    dry_labels = []
    for f in facts:
        content = f.get("content", "").strip()
        if not content:
            continue
        mem_type = f.get("memory_type", "semantic")
        if mem_type not in ("procedural", "semantic"):
            mem_type = "semantic"
        if dry_run:
            dry_labels.append(f"dry:{content[:40]}")
        else:
            batch.append({
                "content": content,
                "session_id": session_id,
                "valid_from": valid_from,
                "topic": f.get("topic"),
                "entities": f.get("entities", []),
                "confidence": float(f.get("confidence", 0.7)),
                "importance": float(f.get("importance", 0.5)),
                "scope": f.get("scope", "learning"),
                "memory_type": mem_type,
                "project_scope": project,
                "ingestion_time": now,
            })

    if dry_run:
        return dry_labels
    return batch_add_facts(batch, skip_faiss=skip_faiss) if batch else []


# ── Orchestrator ───────────────────────────────────────────────────────────

def promote_session(session_id: str, model: str = "claude-haiku-4-5-20251001",
                    dry_run: bool = False, skip_faiss: bool = False) -> dict:
    """
    Full promotion: find episodes + messages for session_id, extract facts from both.
    Returns result dict with counts and any errors.
    """
    import sqlite3 as _sqlite3, time
    start = time.monotonic()

    # Session dedup guard — skip if facts already exist for this session
    if not dry_run:
        _fdb = VAULT / "facts.db"
        if _fdb.exists():
            _fc = _sqlite3.connect(str(_fdb))
            _existing = _fc.execute(
                "SELECT COUNT(*) FROM facts WHERE session_id=?", [session_id]
            ).fetchone()[0]
            _fc.close()
            if _existing > 0:
                return {
                    "session_id": session_id,
                    "project": None,
                    "episodes_found": 0,
                    "episodic_facts": 0,
                    "procedural_facts": 0,
                    "total_facts": _existing,
                    "duration_s": 0.0,
                    "dry_run": False,
                    "errors": [],
                    "status": "already_promoted",
                    "note": f"skipped: {_existing} facts already in facts.db for this session",
                }

    # Resolve project from episodes (primary) or events.db sessions table
    project = None
    episodes = _find_episodes(session_id)

    if episodes:
        meta, _ = _parse_frontmatter(episodes[0].read_text(errors="replace")[:500])
        project = meta.get("project", "").strip('"') or None

    if not project and EVENTS_DB.exists():
        conn = sqlite3.connect(str(EVENTS_DB))
        row = conn.execute(
            "SELECT project FROM sessions WHERE session_id = ?", [session_id]
        ).fetchone()
        conn.close()
        if row:
            project = row[0]

    if not project:
        project = "unknown"

    # valid_from from earliest episode or today
    valid_from = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if episodes:
        meta, _ = _parse_frontmatter(episodes[0].read_text(errors="replace")[:500])
        ts = meta.get("timestamp", "").strip('"')
        if ts:
            valid_from = ts[:10]

    episodic_ids = []
    procedural_ids = []
    errors = []

    # Source 1: episodes
    for ep in episodes:
        try:
            ids = extract_from_episode(ep, model=model, dry_run=dry_run, skip_faiss=skip_faiss)
            episodic_ids.extend(ids)
        except Exception as e:
            errors.append(f"episode {ep.name}: {e}")

    # Source 2: messages
    try:
        ids = extract_from_messages(session_id, project, valid_from,
                                     model=model, dry_run=dry_run, skip_faiss=skip_faiss)
        procedural_ids.extend(ids)
    except Exception as e:
        errors.append(f"messages: {e}")

    duration = round(time.monotonic() - start, 2)
    total_facts = len(episodic_ids) + len(procedural_ids)

    if errors and total_facts == 0:
        status = "error"
    elif total_facts == 0:
        status = "skipped"   # nothing extractable — no episodes, or all filtered out
    else:
        status = "ok"

    return {
        "session_id": session_id,
        "project": project,
        "episodes_found": len(episodes),
        "episodic_facts": len(episodic_ids),
        "procedural_facts": len(procedural_ids),
        "total_facts": total_facts,
        "duration_s": duration,
        "dry_run": dry_run,
        "errors": errors,
        "status": status,
    }
