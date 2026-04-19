"""
relation_extractor.py — L2 typed relation extraction from L1 atoms.

Reads atoms from atoms.db, resolves entity pairs via graph.db, sends batches
to Haiku to classify semantic relation types, writes typed edges to graph.db.

Relation vocabulary (kept tight to avoid hallucination):
  uses          — A invokes / calls / applies B
  depends_on    — A requires B to function correctly
  part_of       — A is a component or subset of B
  deployed_on   — A runs inside / on top of B
  replaced_by   — A was superseded or removed in favour of B
  configured_by — A is controlled / managed by B
  built_with    — A was constructed using B as a tool or library
  analogous_to  — A is structurally similar to B (cross-domain bridge)
  related_to    — fallback: co-occur but no clearer type identified

Run modes:
  python3 relation_extractor.py            # backfill all atoms
  python3 relation_extractor.py --dry-run  # print batches, no writes
  python3 relation_extractor.py --atom-ids <id1> <id2>  # specific atoms
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
ATOMS_DB = VAULT / "atoms.db"
GRAPH_DB = VAULT / "graph.db"

RELATION_TYPES = {
    "uses", "depends_on", "part_of", "deployed_on",
    "replaced_by", "configured_by", "built_with",
    "analogous_to", "related_to",
}

ATOMS_PER_BATCH = 10
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

_NVM_BIN = Path.home() / ".nvm/versions/node/v20.20.0/bin"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _claude_bin() -> str:
    import shutil
    return (
        os.environ.get("CLAUDE_BIN")
        or shutil.which("claude")
        or str(_NVM_BIN / "claude")
    )


def _claude_env() -> dict:
    return {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}


# ── DB helpers ────────────────────────────────────────────────────────────────

def _atoms_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(ATOMS_DB))
    conn.row_factory = sqlite3.Row
    return conn


def _graph_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(GRAPH_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _get_canonical_entity(graph: sqlite3.Connection, raw_name: str):
    """Look up entity by alias (case-insensitive). Returns row or None."""
    row = graph.execute(
        "SELECT id, canonical_name, entity_type FROM entities WHERE canonical_name=? COLLATE NOCASE",
        [raw_name]
    ).fetchone()
    if row:
        return row
    # Try alias match
    rows = graph.execute("SELECT id, canonical_name, entity_type, aliases FROM entities").fetchall()
    lower = raw_name.lower()
    for r in rows:
        aliases = json.loads(r["aliases"] or "[]")
        if any(a.lower() == lower for a in aliases):
            return r
    return None


def _upsert_typed_relation(
    graph: sqlite3.Connection,
    source_id: str,
    target_id: str,
    relation_type: str,
    description: str,
    atom_id: str,
    now: str,
):
    """Insert or update a typed relation (idempotent on re-run)."""
    existing = graph.execute(
        "SELECT id, atom_ids FROM relations WHERE source_entity=? AND target_entity=? AND relation_type=?",
        [source_id, target_id, relation_type]
    ).fetchone()

    if existing:
        existing_atoms = set(json.loads(existing["atom_ids"] or "[]"))
        if atom_id in existing_atoms:
            return  # fully idempotent
        merged = list(existing_atoms | {atom_id})
        graph.execute(
            """UPDATE relations SET atom_ids=?, last_seen=?, updated_at=?,
               description=COALESCE(?,description) WHERE id=?""",
            [json.dumps(merged), now, now, description or None, existing["id"]]
        )
    else:
        graph.execute(
            """INSERT INTO relations
               (id, source_entity, target_entity, relation_type, weight,
                description, atom_ids, first_seen, last_seen, created_at, updated_at)
               VALUES (?,?,?,?,1.0,?,?,?,?,?,?)""",
            [
                str(uuid.uuid4()), source_id, target_id, relation_type,
                description or None,
                json.dumps([atom_id]), now, now, now, now,
            ]
        )


# ── LLM batch call ────────────────────────────────────────────────────────────

PROMPT_TEMPLATE = """You are analyzing a personal knowledge graph. For each atom (memory unit) below, identify semantic relations between the listed entities based on the atom content.

Relation types (choose the most specific that applies):
- uses: A invokes/calls/applies B
- depends_on: A requires B to function
- part_of: A is a component/subset of B
- deployed_on: A runs inside/on B
- replaced_by: A was superseded by B
- configured_by: A is controlled/managed by B
- built_with: A was built using B as tool/library
- analogous_to: A is structurally similar to B (cross-domain bridge)
- related_to: co-occur but no clearer type (fallback only)

Rules:
- Only emit relations you can confidently derive from the atom content
- Prefer specific types over related_to
- Direction matters: source -> target (e.g. "FastAPI depends_on Python" not reverse)
- Skip pairs where the relationship is too vague to classify
- analogous_to is bidirectional — only emit once

Return JSON array only, no prose:
[
  {{"atom_id": "...", "source": "EntityA", "target": "EntityB", "type": "relation_type", "description": "one short phrase why"}},
  ...
]

If no confident relations found for an atom, omit it (return empty array is fine).

ATOMS:
{atoms_block}"""


def _build_atom_block(atoms: list[dict]) -> str:
    parts = []
    for a in atoms:
        entities = ", ".join(a["entities"]) if a["entities"] else "(none)"
        parts.append(
            f"atom_id: {a['id']}\n"
            f"type: {a['atom_type']}\n"
            f"entities: {entities}\n"
            f"content: {a['content']}"
        )
    return "\n\n---\n\n".join(parts)


def _call_haiku(prompt: str, dry_run: bool) -> list[dict]:
    if dry_run:
        print("    [dry-run] would call Haiku")
        return []

    bin_ = _claude_bin()
    env = _claude_env()
    try:
        proc = subprocess.run(
            [bin_, "-p", "--model", DEFAULT_MODEL, prompt],
            capture_output=True, text=True, timeout=120, env=env,
        )
        raw = proc.stdout.strip()
    except Exception as e:
        print(f"    FAILED ({e})")
        return []

    # Strip markdown fences
    raw = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    # Find JSON array
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        print(f"    FAILED (no JSON array in response)")
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        print(f"    FAILED (JSON parse: {e})")
        return []


# ── Main extraction pipeline ──────────────────────────────────────────────────

def extract_relations(
    atom_ids: list[str] | None = None,
    dry_run: bool = False,
    batch_size: int = ATOMS_PER_BATCH,
):
    """
    Main entry point. Extracts typed relations from L1 atoms.
    If atom_ids is None, processes all atoms in atoms.db.
    """
    ac = _atoms_conn()
    gc = _graph_conn()

    # Load atoms
    if atom_ids:
        placeholders = ",".join("?" * len(atom_ids))
        rows = ac.execute(
            f"SELECT id, content, atom_type, entities FROM atoms "
            f"WHERE id IN ({placeholders}) AND invalidated_by IS NULL",
            atom_ids
        ).fetchall()
    else:
        rows = ac.execute(
            "SELECT id, content, atom_type, entities FROM atoms WHERE invalidated_by IS NULL"
        ).fetchall()
    ac.close()

    # Filter atoms that actually have multiple entities (need pairs)
    atoms = []
    for r in rows:
        raw_entities = []
        try:
            raw_entities = json.loads(r["entities"] or "[]")
        except (json.JSONDecodeError, TypeError):
            pass
        if len(raw_entities) < 2:
            continue  # no pairs possible
        # Resolve entity strings to canonical names (filter unresolved)
        resolved = []
        for name in raw_entities:
            e = _get_canonical_entity(gc, name)
            if e:
                resolved.append({"id": e["id"], "name": e["canonical_name"]})
        if len(resolved) < 2:
            continue
        atoms.append({
            "id": r["id"],
            "content": r["content"],
            "atom_type": r["atom_type"],
            "entities": [e["name"] for e in resolved],
            "_entity_map": {e["name"]: e["id"] for e in resolved},
        })

    print(f"Atoms with 2+ resolved entities: {len(atoms)}")
    if not atoms:
        print("Nothing to extract.")
        return

    # Process in batches
    batches = [atoms[i:i+batch_size] for i in range(0, len(atoms), batch_size)]
    total_written = 0
    total_skipped = 0
    now = _now()

    for bi, batch in enumerate(batches):
        print(f"  Batch {bi+1}/{len(batches)} ({len(batch)} atoms)...", end=" ", flush=True)

        atoms_block = _build_atom_block(batch)
        prompt = PROMPT_TEMPLATE.format(atoms_block=atoms_block)
        relations = _call_haiku(prompt, dry_run)

        if dry_run:
            continue

        # Build entity_map for this batch
        entity_map: dict[str, dict] = {}
        for a in batch:
            entity_map.update(a["_entity_map"])

        written = 0
        for rel in relations:
            atom_id = rel.get("atom_id", "")
            src_name = rel.get("source", "")
            tgt_name = rel.get("target", "")
            rtype = rel.get("type", "related_to")
            desc = rel.get("description", "")

            # Validate relation type
            if rtype not in RELATION_TYPES:
                rtype = "related_to"

            # Resolve entity IDs
            src_id = entity_map.get(src_name)
            tgt_id = entity_map.get(tgt_name)
            if not src_id or not tgt_id or src_id == tgt_id:
                total_skipped += 1
                continue

            try:
                _upsert_typed_relation(gc, src_id, tgt_id, rtype, desc, atom_id, now)
                written += 1
            except Exception as e:
                print(f"\n    WARNING: {e}")
                total_skipped += 1

        gc.commit()
        total_written += written
        print(f"wrote {written} relations")

    gc.close()

    # Print summary
    print(f"\nDone: {total_written} typed relations written, {total_skipped} skipped")
    if not dry_run:
        # Show breakdown by type
        gc2 = _graph_conn()
        rows = gc2.execute(
            "SELECT relation_type, COUNT(*) as n FROM relations "
            "GROUP BY relation_type ORDER BY n DESC"
        ).fetchall()
        print("\nRelation type breakdown:")
        for r in rows:
            print(f"  {r['relation_type']:15s} {r['n']}")
        gc2.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract typed relations from L1 atoms")
    parser.add_argument("--dry-run", action="store_true", help="Print batches, no writes")
    parser.add_argument("--atom-ids", nargs="+", help="Process specific atom IDs only")
    parser.add_argument("--batch-size", type=int, default=ATOMS_PER_BATCH)
    args = parser.parse_args()

    print(f"=== Relation Extractor ===")
    print(f"dry_run={args.dry_run}, batch_size={args.batch_size}")
    print()

    t0 = time.time()
    extract_relations(
        atom_ids=args.atom_ids,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
    )
    print(f"\nTotal time: {time.time()-t0:.1f}s")
