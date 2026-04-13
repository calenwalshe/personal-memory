"""
window_classifier.py — 3-turn learning moment detector + atomic fact extractor.

Usage:
  python3 window_classifier.py [--tui] [--sessions N:M] [--dry-run]

Reads all session JSONLs, slides a window over turns, pre-filters by question
markers, calls codex/claude to classify and extract atomic facts, writes to
fact_store.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
STATE_FILE = VAULT / "scripts/extraction_state.json"
SESSIONS_DIR = Path.home() / ".claude/projects"
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "codex")

# Resolve binary paths at startup (handles nvm shims not in subprocess PATH)
# We invoke `node cli.js` directly to avoid shebang resolution issues in child processes.
_NVM_BIN = Path.home() / ".nvm/versions/node/v20.20.0/bin"
NODE_BIN = os.environ.get("NODE_BIN") or shutil.which("node") or str(_NVM_BIN / "node")
_claude_sym = shutil.which("claude") or str(_NVM_BIN / "claude")
CLAUDE_CLI = os.environ.get("CLAUDE_CLI") or str(Path(_claude_sym).resolve())
_codex_sym = shutil.which("codex") or str(_NVM_BIN / "codex")
CODEX_CLI = os.environ.get("CODEX_CLI") or str(Path(_codex_sym).resolve())

QUESTION_MARKERS = re.compile(
    r'\?|how |why |what |wait |i don\'t|doesn\'t|so you|that means|'
    r'i see|explain|confused|understand|difference between|what\'s the',
    re.IGNORECASE,
)

CLASSIFIER_PREAMBLE = """You are a learning moment detector analyzing developer Q&A sessions.
A learning moment is a 3-turn pattern: (1) user question showing ignorance or confusion,
(2) assistant explanation, (3) user comprehension confirmation.
Task-execution exchanges (user asks Claude to DO something, not explain something) are NOT learning moments.
Always respond with valid JSON only. No markdown fences. No explanation."""


def llm(prompt: str) -> str:
    if LLM_PROVIDER == "codex":
        cmd = [NODE_BIN, CODEX_CLI, "exec", "--full-auto", "--profile", "llm",
               "--cd", "/tmp", "--skip-git-repo-check", prompt]
    else:
        cmd = [NODE_BIN, CLAUDE_CLI, "-p", prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return result.stdout.strip()


def llm_json(prompt: str) -> dict:
    raw = llm(CLASSIFIER_PREAMBLE + "\n\n" + prompt + "\n\nRespond with JSON only.")
    raw = raw.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Retry once with stricter instruction
        raw2 = llm(CLASSIFIER_PREAMBLE + "\n\n" + prompt +
                   "\n\nYour response MUST be raw JSON only. No text before or after.")
        raw2 = raw2.strip()
        if raw2.startswith("```"):
            raw2 = re.sub(r'^```[a-z]*\n?', '', raw2)
            raw2 = re.sub(r'\n?```$', '', raw2)
        try:
            return json.loads(raw2)
        except json.JSONDecodeError:
            return {"is_learning_moment": False, "confidence": 0.0,
                    "fact_statements": [], "error": "parse_failed"}


def load_sessions(session_range: tuple[int, int] = None) -> list[Path]:
    sessions = sorted(SESSIONS_DIR.rglob("*.jsonl"))
    # Exclude /tmp project dirs
    sessions = [s for s in sessions if not any(
        p in str(s) for p in ["-tmp-", "/-tmp/"]
    )]
    if session_range:
        start, end = session_range
        sessions = sessions[start:end]
    return sessions


def parse_turns(session_path: Path) -> list[dict]:
    turns = []
    try:
        with open(session_path) as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    message = msg.get("message", {})
                    if not isinstance(message, dict):
                        continue
                    role = message.get("role", "")
                    content = message.get("content", "")
                    if role not in ("user", "assistant") or not content:
                        continue
                    if isinstance(content, list):
                        text = " ".join(
                            c.get("text", "") for c in content
                            if isinstance(c, dict) and c.get("type") == "text"
                        )
                    else:
                        text = str(content)
                    if text.strip():
                        turns.append({"role": role, "text": text[:1000]})
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception:
        pass
    return turns


def prefilter_window(turns: list[dict]) -> bool:
    """Return True if this window contains a question marker in a user turn."""
    for t in turns:
        if t["role"] == "user" and QUESTION_MARKERS.search(t["text"]):
            return True
    return False


def classify_window(turns: list[dict], session_id: str,
                    turn_start: int) -> dict:
    window_text = "\n".join(
        f"[{t['role'].upper()}]: {t['text'][:400]}" for t in turns
    )
    prompt = f"""Analyze this conversation window and determine if it contains a learning moment.

WINDOW:
{window_text}

Respond with JSON:
{{
  "is_learning_moment": true/false,
  "confidence": 0.0-1.0,
  "fact_statements": ["atomic fact 1", "atomic fact 2"],
  "topic": "short topic label",
  "entities": ["entity1", "entity2"],
  "scope": "technical|decision|preference|learning"
}}

fact_statements: 1-3 concise atomic facts extracted IF is_learning_moment is true. Empty array if false."""
    return llm_json(prompt)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "total_sessions": 0,
        "processed_sessions": [],
        "facts_written": 0,
        "windows_seen": 0,
        "windows_prefiltered": 0,
        "windows_classified": 0,
        "learning_moments": 0,
        "contradictions_auto": 0,
        "contradictions_queued": 0,
        "errors": [],
        "started_at": None,
        "last_updated": None,
        "current_session": None,
    }


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def run_extraction(session_range=None, dry_run=False, tui=False,
                   window_size=5, confidence_threshold=0.5):
    from fact_store import init_db, add_fact, fact_exists, get_facts_by_entities
    from fact_store import invalidate_fact, queue_contradiction_review
    from fact_store import AUTO_INVALIDATE_THRESHOLD

    init_db()
    sessions = load_sessions(session_range)
    state = load_state()
    state["total_sessions"] = len(sessions)
    if not state["started_at"]:
        state["started_at"] = datetime.now(timezone.utc).isoformat()

    if tui:
        from extraction_tui import ExtractionTUI
        tui_obj = ExtractionTUI(state)
        tui_obj.start()
    else:
        tui_obj = None

    processed = set(state["processed_sessions"])

    try:
        for i, session_path in enumerate(sessions):
            session_id = session_path.stem
            if session_id in processed:
                continue

            state["current_session"] = {
                "id": session_id,
                "index": i + 1,
                "path": str(session_path),
                "size_kb": session_path.stat().st_size // 1024,
            }
            save_state(state)

            turns = parse_turns(session_path)
            if len(turns) < 3:
                processed.add(session_id)
                state["processed_sessions"] = list(processed)
                save_state(state)
                continue

            # Get session date from file mtime
            mtime = datetime.fromtimestamp(
                session_path.stat().st_mtime, tz=timezone.utc
            )
            valid_from = mtime.strftime("%Y-%m-%d")

            total_windows = max(0, len(turns) - window_size + 1)
            state["windows_seen"] += total_windows

            for w in range(total_windows):
                window = turns[w: w + window_size]

                if not prefilter_window(window):
                    state["windows_prefiltered"] += 1
                    continue

                if fact_exists(session_id, [w, w + window_size]):
                    continue

                if dry_run:
                    state["windows_classified"] += 1
                    continue

                state["windows_classified"] += 1
                result = classify_window(window, session_id, w)

                if result.get("error"):
                    state["errors"].append({
                        "session": session_id,
                        "window": w,
                        "error": result["error"],
                    })
                    continue

                if not result.get("is_learning_moment"):
                    continue

                if result.get("confidence", 0) < confidence_threshold:
                    continue

                state["learning_moments"] += 1
                fact_statements = result.get("fact_statements", [])

                for fact_text in fact_statements:
                    if not fact_text.strip():
                        continue

                    # ClassifyOperation: check existing facts with same entities
                    entities = result.get("entities", [])
                    existing = get_facts_by_entities(entities)

                    new_fact_id = add_fact(
                        content=fact_text,
                        session_id=session_id,
                        valid_from=valid_from,
                        topic=result.get("topic"),
                        entities=entities,
                        confidence=result.get("confidence", 0.7),
                        importance=0.6,
                        scope=result.get("scope", "learning"),
                        turn_range=[w, w + window_size],
                    )
                    state["facts_written"] += 1

                    # Check for contradictions with existing facts
                    for old_fact in existing:
                        if old_fact["id"] == new_fact_id:
                            continue
                        contradiction_result = _check_contradiction(
                            fact_text, old_fact["content"]
                        )
                        conf = contradiction_result.get("contradiction_confidence", 0)
                        if conf >= AUTO_INVALIDATE_THRESHOLD:
                            invalidate_fact(old_fact["id"], new_fact_id)
                            state["contradictions_auto"] += 1
                        elif conf >= 0.5:
                            queue_contradiction_review(
                                new_fact_id,
                                old_fact["id"],
                                contradiction_result.get("reason", ""),
                            )
                            state["contradictions_queued"] += 1

                save_state(state)

            if not dry_run:
                processed.add(session_id)
                state["processed_sessions"] = list(processed)
                save_state(state)

            if tui_obj:
                tui_obj.update(state)

    except KeyboardInterrupt:
        print(f"\nPaused at session {len(processed)}/{len(sessions)} — re-run to resume.")
        save_state(state)
    finally:
        if tui_obj:
            tui_obj.stop()

    return state


def _check_contradiction(new_fact: str, old_fact: str) -> dict:
    prompt = f"""Do these two facts contradict each other?

New fact: {new_fact}
Old fact: {old_fact}

Respond with JSON:
{{
  "contradiction_confidence": 0.0-1.0,
  "reason": "brief explanation"
}}"""
    return llm_json(prompt)


def main():
    parser = argparse.ArgumentParser(description="Extract learning moments from sessions")
    parser.add_argument("--tui", action="store_true", help="Show live TUI")
    parser.add_argument("--dry-run", action="store_true", help="Pre-filter only, no LLM calls")
    parser.add_argument("--sessions", help="Session range e.g. 0:50", default=None)
    parser.add_argument("--window-size", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()

    session_range = None
    if args.sessions:
        parts = args.sessions.split(":")
        session_range = (int(parts[0]), int(parts[1]))

    sys.path.insert(0, str(VAULT / "scripts"))

    state = run_extraction(
        session_range=session_range,
        dry_run=args.dry_run,
        tui=args.tui,
        window_size=args.window_size,
        confidence_threshold=args.threshold,
    )

    print(f"\n=== Extraction complete ===")
    print(f"Sessions processed : {len(state['processed_sessions'])}/{state['total_sessions']}")
    print(f"Windows seen       : {state['windows_seen']}")
    print(f"Pre-filtered (skip): {state['windows_prefiltered']}")
    print(f"Classified         : {state['windows_classified']}")
    print(f"Learning moments   : {state['learning_moments']}")
    print(f"Facts written      : {state['facts_written']}")
    print(f"Contradictions     : {state['contradictions_auto']} auto, {state['contradictions_queued']} queued")
    print(f"Errors             : {len(state['errors'])}")


if __name__ == "__main__":
    main()
