"""
promote_session.py — Background orchestrator: routes a session JSONL to all extractors.

Runs episodic + procedural extractors on a session. Idempotent: skips sessions
already in the promotion log. On completion, removes session from queue.

Usage:
  python3 promote_session.py --session <session_id> [--dry-run] [--force]

  --dry-run: run extractors in dry-run mode (no DB writes)
  --force:   run even if session already in promotion log

Environment:
  LLM_PROVIDER=claude|codex (default: codex)
  VAULT_DIR=<path>           (default: ~/memory/vault)

Log files:
  ~/.cortex/promotion_log.jsonl    — successful promotions (idempotency check)
  ~/.cortex/promotion_failures.log — failed promotions
  ~/.cortex/promotion_queue.txt    — sessions waiting for promotion
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
CORTEX_DIR = Path.home() / ".cortex"
PROMOTION_LOG = CORTEX_DIR / "promotion_log.jsonl"
FAILURE_LOG = CORTEX_DIR / "promotion_failures.log"
PROMOTION_QUEUE = CORTEX_DIR / "promotion_queue.txt"
SESSIONS_DIR = Path.home() / ".claude/projects"

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "codex")

# Ensure .cortex directory exists
CORTEX_DIR.mkdir(parents=True, exist_ok=True)


def is_promoted(session_id: str) -> bool:
    """Check if session has already been successfully promoted (idempotency)."""
    if not PROMOTION_LOG.exists():
        return False
    with open(PROMOTION_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("session_id") == session_id and entry.get("status") == "success":
                    return True
            except json.JSONDecodeError:
                continue
    return False


def log_promotion(session_id: str, facts_written: dict, duration_s: float):
    """Write a successful promotion to the log."""
    entry = {
        "session_id": session_id,
        "status": "success",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_s": round(duration_s, 2),
        "facts_written": facts_written,
    }
    with open(PROMOTION_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def log_failure(session_id: str, error: str):
    """Write a promotion failure to the failure log."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(FAILURE_LOG, "a") as f:
        f.write(f"[{ts}] session={session_id} error={error}\n")


def remove_from_queue(session_id: str):
    """Remove a session_id from the promotion queue."""
    if not PROMOTION_QUEUE.exists():
        return
    lines = PROMOTION_QUEUE.read_text().splitlines()
    remaining = [l for l in lines if l.strip() and l.strip() != session_id]
    PROMOTION_QUEUE.write_text("\n".join(remaining) + ("\n" if remaining else ""))


def find_session_file(session_id: str) -> Path | None:
    """Find the JSONL file for a session."""
    matches = list(SESSIONS_DIR.rglob(f"{session_id}.jsonl"))
    return matches[0] if matches else None


def run_promotion(session_id: str, dry_run: bool = False, force: bool = False) -> dict:
    """
    Run all extractors on a session. Returns result dict.
    Idempotent: returns early if already promoted (unless --force).
    """
    import time

    # Idempotency check
    if not force and is_promoted(session_id):
        print(f"Session {session_id} already promoted — skipping (use --force to re-run)",
              file=sys.stderr)
        return {"session_id": session_id, "status": "already_promoted", "facts_written": {}}

    # Verify session file exists
    session_file = find_session_file(session_id)
    if session_file is None:
        msg = f"Session JSONL not found: {session_id}"
        print(f"ERROR: {msg}", file=sys.stderr)
        if not dry_run:
            log_failure(session_id, msg)
        return {"session_id": session_id, "status": "error", "error": msg}

    print(f"Promoting session {session_id}", file=sys.stderr)
    print(f"  File: {session_file}", file=sys.stderr)
    print(f"  LLM_PROVIDER: {LLM_PROVIDER}", file=sys.stderr)

    start = time.monotonic()

    # Add scripts dir to path for extractor imports
    scripts_dir = str(VAULT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    facts_written = {}
    errors = []

    # Run episodic extractor
    try:
        print("  Running episodic extractor...", file=sys.stderr)
        from episodic_extractor import run_extraction as run_episodic
        episodic_ids = run_episodic(session_id, session_file=session_file, dry_run=dry_run)
        facts_written["episodic"] = len(episodic_ids)
        print(f"  Episodic: {len(episodic_ids)} facts", file=sys.stderr)
    except Exception as e:
        msg = f"episodic extractor failed: {e}"
        print(f"  ERROR: {msg}", file=sys.stderr)
        errors.append(msg)
        facts_written["episodic"] = 0

    # Run procedural extractor
    try:
        print("  Running procedural extractor...", file=sys.stderr)
        from procedural_extractor import run_extraction as run_procedural
        procedural_ids = run_procedural(session_id, session_file=session_file, dry_run=dry_run)
        facts_written["procedural"] = len(procedural_ids)
        print(f"  Procedural: {len(procedural_ids)} facts", file=sys.stderr)
    except Exception as e:
        msg = f"procedural extractor failed: {e}"
        print(f"  ERROR: {msg}", file=sys.stderr)
        errors.append(msg)
        facts_written["procedural"] = 0

    duration_s = time.monotonic() - start
    total_facts = sum(facts_written.values())
    print(f"  Total: {total_facts} facts in {duration_s:.1f}s", file=sys.stderr)

    if not dry_run:
        if errors and total_facts == 0:
            # Complete failure — log but don't mark as promoted (allow retry)
            for err in errors:
                log_failure(session_id, err)
            remove_from_queue(session_id)
            return {
                "session_id": session_id,
                "status": "error",
                "errors": errors,
                "facts_written": facts_written,
            }
        else:
            # Partial or full success — mark as promoted to prevent duplicates
            log_promotion(session_id, facts_written, duration_s)
            remove_from_queue(session_id)
            if errors:
                for err in errors:
                    log_failure(session_id, f"partial: {err}")

    return {
        "session_id": session_id,
        "status": "success" if not errors else "partial",
        "facts_written": facts_written,
        "duration_s": round(duration_s, 2),
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description="Promote a session to the memory vault")
    parser.add_argument("--session", required=True, help="Session ID to promote")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run extractors without writing to DB")
    parser.add_argument("--force", action="store_true",
                        help="Re-run even if session already promoted")
    args = parser.parse_args()

    result = run_promotion(args.session, dry_run=args.dry_run, force=args.force)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("status") in ("success", "partial", "already_promoted") else 1)


if __name__ == "__main__":
    main()
