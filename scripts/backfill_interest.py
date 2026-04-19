"""
backfill_interest.py — One-time backfill of interest_signal, interest_tags,
and user_intent for existing L1 atoms.

Uses Haiku to classify each atom based on its content + trigger (the user's
original words). Processes in batches of 20.

Usage:
    python3 backfill_interest.py [--dry-run] [--batch-size 20]
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

VAULT = Path(os.environ.get("MEMORY_VAULT", Path.home() / "memory/vault"))
DB_PATH = VAULT / "atoms.db"

_NVM_BIN = Path.home() / ".nvm/versions/node/v20.20.0/bin"
CLAUDE_BIN = (
    os.environ.get("CLAUDE_BIN")
    or shutil.which("claude")
    or str(_NVM_BIN / "claude")
)

BATCH_SIZE = 20
TIMEOUT = 60

CLASSIFY_PROMPT = """You are classifying memory atoms for a personal interest graph.

Each atom has:
- content: what happened or was learned
- trigger: the user's original words that started this work
- atom_type: decision, discovery, failure, pattern, gotcha, or outcome
- topic: short label
- project: which project this belongs to

For each atom, determine:

1. interest_signal (true/false): Did the USER initiate this by choice, expressing
   personal interest? Or was it reactive — fixing errors, system requirements,
   infrastructure maintenance?
   - true: "let's build...", "I want to research...", "can we plan...", creative work,
     family planning, personal projects, exploring topics out of curiosity
   - false: "fix this bug", "it's broken", debugging, security patches, CI/CD plumbing,
     routine maintenance, infrastructure that HAD to be done

2. interest_tags (list): What personal interests does this reveal? Not technical
   entities, but interest-level concepts. Examples: family-travel, music-curation,
   creative-projects, health-tracking, cooking, real-estate, finance, art,
   home-automation, geology-research, parenting, fitness, etc.
   Empty list [] if no personal interest signal.

3. user_intent (string): What was the user trying to do? One of:
   explore, build, fix, plan, research, decide, organize
   Empty string "" if unclear.

Respond with a JSON array matching the input order:
[
  {"id": "atom-id-here", "interest_signal": true, "interest_tags": ["family-travel"], "user_intent": "plan"},
  {"id": "atom-id-here", "interest_signal": false, "interest_tags": [], "user_intent": "fix"}
]

ATOMS:
"""


def load_unclassified(conn):
    """Load atoms that need interest classification."""
    rows = conn.execute("""
        SELECT id, content, trigger, atom_type, topic, project
        FROM atoms
        WHERE interest_signal = 0 OR interest_signal IS NULL
        ORDER BY time_first ASC
    """).fetchall()
    return [dict(r) for r in rows]


def format_batch(atoms):
    """Format a batch of atoms for the Haiku prompt."""
    lines = []
    for i, a in enumerate(atoms):
        trigger = (a["trigger"] or "")[:200]
        content = (a["content"] or "")[:200]
        lines.append(
            f"[{i}] id={a['id']}\n"
            f"  project: {a['project']}\n"
            f"  type: {a['atom_type']}\n"
            f"  topic: {a['topic']}\n"
            f"  trigger: {trigger}\n"
            f"  content: {content}"
        )
    return "\n\n".join(lines)


def call_haiku(prompt):
    """Call Haiku and parse JSON response."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", "claude-haiku-4-5-20251001", prompt],
            capture_output=True, text=True, timeout=TIMEOUT, env=env,
        )
        raw = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"  Haiku call failed: {e}", file=sys.stderr)
        return []

    raw = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "")
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        print(f"  No JSON in response: {raw[:200]}", file=sys.stderr)
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}", file=sys.stderr)
        return []


def update_atoms(conn, classifications):
    """Write classifications back to atoms.db."""
    valid_intents = {"explore", "build", "fix", "plan", "research", "decide", "organize"}
    updated = 0
    for c in classifications:
        atom_id = c.get("id", "")
        if not atom_id:
            continue
        interest_signal = 1 if c.get("interest_signal") else 0
        interest_tags = json.dumps(c.get("interest_tags", []))
        user_intent = c.get("user_intent", "")
        if user_intent not in valid_intents:
            user_intent = ""
        conn.execute(
            "UPDATE atoms SET interest_signal=?, interest_tags=?, user_intent=? WHERE id=?",
            (interest_signal, interest_tags, user_intent, atom_id),
        )
        updated += 1
    conn.commit()
    return updated


def main():
    dry_run = "--dry-run" in sys.argv
    batch_size = BATCH_SIZE
    for i, arg in enumerate(sys.argv):
        if arg == "--batch-size" and i + 1 < len(sys.argv):
            batch_size = int(sys.argv[i + 1])

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    atoms = load_unclassified(conn)
    total = len(atoms)
    print(f"Atoms to classify: {total} (batch_size={batch_size}, dry_run={dry_run})")

    if total == 0:
        print("Nothing to backfill.")
        conn.close()
        return

    total_updated = 0
    total_interest = 0
    start = time.monotonic()

    for batch_start in range(0, total, batch_size):
        batch = atoms[batch_start:batch_start + batch_size]
        batch_num = batch_start // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size

        print(f"\nBatch {batch_num}/{total_batches} ({len(batch)} atoms)...")

        prompt = CLASSIFY_PROMPT + format_batch(batch)

        if dry_run:
            print(f"  [dry-run] Would send {len(batch)} atoms to Haiku")
            continue

        classifications = call_haiku(prompt)
        if not classifications:
            print(f"  Haiku returned nothing, skipping batch")
            continue

        # Match by position if IDs don't match (Haiku sometimes mangles UUIDs)
        if len(classifications) == len(batch):
            for j, c in enumerate(classifications):
                if "id" not in c or c["id"] != batch[j]["id"]:
                    c["id"] = batch[j]["id"]

        updated = update_atoms(conn, classifications)
        interest_count = sum(1 for c in classifications if c.get("interest_signal"))
        total_updated += updated
        total_interest += interest_count

        print(f"  Updated: {updated}, Interest signals: {interest_count}/{len(batch)}")

        # Brief pause between batches to avoid rate limits
        if batch_start + batch_size < total:
            time.sleep(1)

    elapsed = round(time.monotonic() - start, 1)
    conn.close()

    print(f"\nBackfill complete: {total_updated}/{total} atoms updated, "
          f"{total_interest} interest signals, {elapsed}s elapsed")


if __name__ == "__main__":
    main()
