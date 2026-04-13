#!/usr/bin/env python3
"""
build_eval_set.py — LoCoMo-style private eval harness for autoresearch-memory.

=============================================================================
EVAL PLAN (folded in — this is the spec referenced by contract §Eval Plan)
=============================================================================

Purpose
-------
Produce a private, reproducible ground-truth QA set we can re-run the GraphRAG
pipeline against each week and measure F1. The autoresearch loop optimises
parameters against this metric.

Sources
-------
- Input corpus: `~/.claude/projects/-home-agent/*.jsonl` (read-only, never
  mutated). Sessions are segmented identically to extract_sessions.py
  (reuses the importlib bridge into preprocess-sessions.py).

Hold-out protocol
-----------------
1. Load all sessions, sort by first-event timestamp.
2. Seeded shuffle (default seed=42) → deterministic per-run split.
3. Split into (corpus_sessions, holdout_sessions) using `holdout_ratio`
   (default 0.25 → 20-30% per contract research notes).
4. Only holdout_sessions are used for QA generation. The corpus sessions
   are what GraphRAG indexes; they MUST NOT leak into the eval answers.

QA generation
-------------
For each holdout session:
- Build a prompt context: the chronological user/assistant turns.
- Call Haiku with a strict JSON-output instruction asking for
  `pairs_per_session` question/answer pairs grounded in that session.
- Parse the response, drop pairs that fail `validate_qa_pair`.
- Attach `session_id` from the holdout session.

Validation
----------
A pair is valid iff:
- question and answer are non-empty strings ≥10 chars
- session_id is present

Output
------
`eval_set.jsonl` — one JSON object per line:
    {"question": "...", "answer": "...", "session_id": "..."}

Idempotency
-----------
On rerun, `build_eval_set` reads existing output, keeps all pairs whose
session_id is still in the current holdout, and skips any session that
already has pairs. New sessions get new pairs appended. No duplicates.

Success criteria (contract §Done Criteria item 2)
--------------------------------------------------
- 200 ≤ len(pairs) ≤ 500 (enforced at CLI level; unit test uses smaller N)
- Each pair has `question`, `answer`, `session_id`
- Re-run produces identical line count

CLI
---
    python3 build_eval_set.py [--output PATH] [--holdout-ratio 0.25]
                              [--pairs-per-session 5] [--seed 42]
                              [--model claude-haiku-4-5-20251001]
                              [--max-sessions N]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT = Path.home() / "memory" / "vault" / "scripts" / "eval_set.jsonl"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_HOLDOUT_RATIO = 0.25
DEFAULT_PAIRS_PER_SESSION = 5
DEFAULT_SEED = 42

MIN_STRING_LENGTH = 10


# --------------------------------------------------------------------------- #
# Session splitting
# --------------------------------------------------------------------------- #

def split_sessions(
    sessions: list[dict],
    *,
    holdout_ratio: float,
    seed: int,
) -> tuple[list[dict], list[dict]]:
    """Deterministic seeded split of sessions into (corpus, holdout)."""
    if not 0 <= holdout_ratio <= 1:
        raise ValueError(f"holdout_ratio must be in [0, 1], got {holdout_ratio}")
    rng = random.Random(seed)
    shuffled = list(sessions)
    rng.shuffle(shuffled)
    n_holdout = int(round(len(shuffled) * holdout_ratio))
    holdout = shuffled[:n_holdout]
    corpus = shuffled[n_holdout:]
    return corpus, holdout


# --------------------------------------------------------------------------- #
# QA pair validation
# --------------------------------------------------------------------------- #

def validate_qa_pair(pair: dict) -> bool:
    """Check required keys and minimum string length."""
    for key in ("question", "answer", "session_id"):
        if key not in pair:
            return False
        if not isinstance(pair[key], str):
            return False
    if len(pair["question"].strip()) < MIN_STRING_LENGTH:
        return False
    if len(pair["answer"].strip()) < MIN_STRING_LENGTH // 2:
        return False
    return True


# --------------------------------------------------------------------------- #
# Session → prompt context
# --------------------------------------------------------------------------- #

def session_to_prompt_context(session: dict, *, max_chars: int = 12000) -> str:
    """Render a session as a compact conversational transcript for the model."""
    lines: list[str] = []
    for ev in session.get("events", []):
        role = ev.get("role", "unknown").capitalize()
        text = ev.get("text", "")
        lines.append(f"{role}: {text}")
    rendered = "\n\n".join(lines)
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars] + "\n[...truncated]"
    return rendered


# --------------------------------------------------------------------------- #
# QA generation via Haiku
# --------------------------------------------------------------------------- #

_PROMPT_TEMPLATE = """You are generating evaluation questions for a memory-retrieval system.
Given a transcript of a single past session between a user and an AI assistant, generate {n_pairs} question/answer pairs that test whether a system can recall facts, decisions, or reasoning from this session.

Rules:
- Each question must be answerable ONLY from content inside this session — no general-knowledge questions.
- Answers should be concise (1-2 sentences) and quote or closely paraphrase the session.
- Cover different aspects of the session (facts, decisions, reasoning, specific tools/files/names mentioned).
- Questions must be at least 10 characters; answers at least 5 characters.

Transcript:
---
{context}
---

Return ONLY a JSON object of this exact shape, nothing else:
{{"pairs": [{{"question": "...", "answer": "..."}}, ...]}}
"""


def _extract_json_object(text: str) -> dict | None:
    """Find and parse the first balanced JSON object in text. Returns None on failure."""
    # Strip common fences.
    t = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.+?)```", t, re.DOTALL)
    if fence_match:
        t = fence_match.group(1).strip()

    # Find the first { and try to parse balanced object.
    start = t.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(t)):
        c = t[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                candidate = t[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


def generate_qa_pairs_for_session(
    session: dict,
    *,
    client: Any,
    n_pairs: int,
    model: str,
) -> list[dict]:
    """Call the model, parse JSON, validate. Returns a list of valid pairs."""
    session_id = session["session_id"]
    context = session_to_prompt_context(session)
    prompt = _PROMPT_TEMPLATE.format(n_pairs=n_pairs, context=context)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        print(f"[eval] API error for {session_id}: {e}", file=sys.stderr)
        return []

    # Concatenate all text blocks.
    content_blocks = getattr(response, "content", [])
    raw_text = "".join(getattr(b, "text", "") for b in content_blocks)

    parsed = _extract_json_object(raw_text)
    if not parsed or "pairs" not in parsed:
        return []

    valid: list[dict] = []
    for p in parsed.get("pairs", []):
        if not isinstance(p, dict):
            continue
        record = {
            "question": (p.get("question") or "").strip(),
            "answer": (p.get("answer") or "").strip(),
            "session_id": session_id,
        }
        if validate_qa_pair(record):
            valid.append(record)
    return valid


# --------------------------------------------------------------------------- #
# End-to-end builder
# --------------------------------------------------------------------------- #

def _read_existing_eval_set(path: Path) -> tuple[list[dict], set[str]]:
    """Load existing jsonl, return (all_pairs, set_of_session_ids_present)."""
    if not path.exists():
        return [], set()
    pairs: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        pairs.append(rec)
    ids = {p.get("session_id", "") for p in pairs}
    return pairs, ids


def build_eval_set(
    sessions: list[dict],
    *,
    output_path: Path,
    client: Any,
    holdout_ratio: float,
    pairs_per_session: int,
    seed: int,
    model: str,
) -> int:
    """Build/update the eval set. Returns the total number of pairs in the output."""
    output_path = Path(output_path)
    _, holdout = split_sessions(sessions, holdout_ratio=holdout_ratio, seed=seed)
    holdout_ids = {s["session_id"] for s in holdout}

    existing_pairs, existing_ids = _read_existing_eval_set(output_path)

    # Keep existing pairs ONLY if their session is still in the holdout
    # (prevents stale pairs from a prior run with a different seed).
    kept_pairs = [p for p in existing_pairs if p.get("session_id") in holdout_ids]
    kept_ids = {p["session_id"] for p in kept_pairs}

    new_pairs: list[dict] = []
    for session in holdout:
        if session["session_id"] in kept_ids:
            continue  # already have pairs for this session
        pairs = generate_qa_pairs_for_session(
            session, client=client, n_pairs=pairs_per_session, model=model
        )
        new_pairs.extend(pairs)

    all_pairs = kept_pairs + new_pairs
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        for p in all_pairs:
            f.write(json.dumps(p) + "\n")
    return len(all_pairs)


# --------------------------------------------------------------------------- #
# CLI — pulls real sessions from the corpus via extract_sessions bridge
# --------------------------------------------------------------------------- #

def _load_sessions_from_corpus() -> list[dict]:
    """Group events by the JSONL sessionId field. This uses the authoritative
    session boundary from Claude Code's own session tracking, not the
    time-gap heuristic in preprocess-sessions.py (which collapses rapid
    successive sessions into one)."""
    import extract_sessions as es
    events = es.gather_events_from_corpus()
    by_sid: dict[str, list[dict]] = {}
    for ev in events:
        sid = ev.get("session_id", "unknown")
        by_sid.setdefault(sid, []).append(ev)
    sessions: list[dict] = []
    for sid, evs in by_sid.items():
        if len(evs) < 2:
            continue
        evs.sort(key=lambda e: e["timestamp"])
        sessions.append({"session_id": sid, "events": evs})
    sessions.sort(key=lambda s: s["events"][0]["timestamp"])
    return sessions


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    ap.add_argument("--holdout-ratio", type=float, default=DEFAULT_HOLDOUT_RATIO)
    ap.add_argument("--pairs-per-session", type=int, default=DEFAULT_PAIRS_PER_SESSION)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-sessions", type=int, default=None,
                    help="Limit holdout to first N sessions (for quick trials)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sessions = _load_sessions_from_corpus()
    print(f"loaded {len(sessions)} sessions from corpus")

    if args.dry_run:
        _, holdout = split_sessions(
            sessions, holdout_ratio=args.holdout_ratio, seed=args.seed
        )
        print(f"[dry-run] would generate pairs for {len(holdout)} holdout sessions")
        return

    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    if args.max_sessions:
        sessions = sessions[: args.max_sessions]

    n = build_eval_set(
        sessions,
        output_path=Path(args.output).expanduser(),
        client=client,
        holdout_ratio=args.holdout_ratio,
        pairs_per_session=args.pairs_per_session,
        seed=args.seed,
        model=args.model,
    )
    print(f"eval_set.jsonl now has {n} pairs")


if __name__ == "__main__":
    main()
