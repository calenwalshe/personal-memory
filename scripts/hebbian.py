"""
hebbian.py — Live Hebbian weight updates for graph.db

Implements "neurons that fire together, wire together" as an incremental,
per-session update. Called from vault chunk after new atoms are written.

Each atom carries a list of entities. For every pair of entities that
co-appeared in this session's atoms, we add ETA to their relation weight
in graph.db. Run after every chunker invocation — weights accumulate
naturally over time.

Parameters:
    ETA = 0.1       Small per-session increment. Over 100 sessions a
                    strongly co-active pair reaches ~10 — on par with
                    the top co-occurrence baseline weights.
    MIN_ATOMS = 1   Minimum atoms a pair must share to get a boost.
                    (Always 1 in the live case — one session fires once.)
"""

import json
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone
from itertools import combinations
from collections import defaultdict

VAULT = Path(__file__).parent.parent
ATOMS_DB = VAULT / "atoms.db"
GRAPH_DB = VAULT / "graph.db"   # symlink → active snapshot

ETA = 0.1   # per-session learning rate


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_entity_id(name: str, conn: sqlite3.Connection) -> str | None:
    """Return entity ID for a canonical name or alias. None if not found."""
    name_lower = name.lower().strip()
    row = conn.execute(
        "SELECT id FROM entities WHERE lower(canonical_name)=?", (name_lower,)
    ).fetchone()
    if row:
        return row[0]
    # Try alias match
    rows = conn.execute("SELECT id, aliases FROM entities").fetchall()
    for r in rows:
        try:
            aliases = [a.lower().strip() for a in json.loads(r[1] or "[]")]
            if name_lower in aliases:
                return r[0]
        except Exception:
            pass
    return None


def _upsert_hebbian_edge(eid_a: str, eid_b: str, conn: sqlite3.Connection):
    """
    Add ETA to the related_to edge between eid_a and eid_b.
    Creates the edge if it doesn't exist.
    """
    ts = now()
    # Check existing edge (either direction)
    row = conn.execute(
        """SELECT id, weight FROM relations
           WHERE ((source_entity=? AND target_entity=?) OR (source_entity=? AND target_entity=?))
           AND relation_type='related_to'""",
        (eid_a, eid_b, eid_b, eid_a)
    ).fetchone()

    if row:
        conn.execute(
            "UPDATE relations SET weight=weight+?, last_seen=?, updated_at=? WHERE id=?",
            (ETA, ts, ts, row[0])
        )
    else:
        conn.execute(
            """INSERT INTO relations
               (id, source_entity, target_entity, relation_type, weight,
                description, atom_ids, first_seen, last_seen, created_at, updated_at)
               VALUES (?, ?, ?, 'related_to', ?, 'hebbian', '[]', ?, ?, ?, ?)""",
            (str(uuid.uuid4()), eid_a, eid_b, ETA, ts, ts, ts, ts)
        )


def update_from_atoms(atom_ids: list[str], graph_db_path: Path = None) -> dict:
    """
    Run Hebbian update for the given atom IDs.

    Loads the atoms from atoms.db, extracts entity pairs that co-appeared,
    and adds ETA to each pair's edge weight in graph.db.

    Returns a summary dict: {pairs_found, edges_updated, edges_created}
    """
    if not atom_ids:
        return {"pairs_found": 0, "edges_updated": 0, "edges_created": 0}

    db_path = graph_db_path or GRAPH_DB
    db_path = db_path.resolve() if hasattr(db_path, 'resolve') else Path(db_path).resolve()

    if not db_path.exists():
        return {"error": f"graph.db not found at {db_path}"}

    # Load entities from the new atoms
    a_conn = sqlite3.connect(str(ATOMS_DB))
    a_conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(atom_ids))
    rows = a_conn.execute(
        f"SELECT entities FROM atoms WHERE id IN ({placeholders})",
        atom_ids
    ).fetchall()
    a_conn.close()

    # Collect all entities that fired in this batch
    session_entities = set()
    for row in rows:
        try:
            entities = json.loads(row["entities"] or "[]")
            for e in entities:
                if e:
                    session_entities.add(e.lower().strip())
        except (json.JSONDecodeError, TypeError):
            continue

    if len(session_entities) < 2:
        return {"pairs_found": 0, "edges_updated": 0, "edges_created": 0}

    # Resolve to entity IDs
    g_conn = sqlite3.connect(str(db_path))
    g_conn.row_factory = sqlite3.Row

    entity_ids = {}
    for name in session_entities:
        eid = _resolve_entity_id(name, g_conn)
        if eid:
            entity_ids[name] = eid

    if len(entity_ids) < 2:
        g_conn.close()
        return {"pairs_found": 0, "edges_updated": 0, "edges_created": 0}

    # Count edges before update
    before_count = g_conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]

    # Apply Hebbian update for every pair
    pairs = list(combinations(sorted(entity_ids.values()), 2))
    for eid_a, eid_b in pairs:
        _upsert_hebbian_edge(eid_a, eid_b, g_conn)

    g_conn.commit()

    after_count = g_conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    g_conn.close()

    return {
        "pairs_found": len(pairs),
        "entities_resolved": len(entity_ids),
        "edges_updated": len(pairs) - (after_count - before_count),
        "edges_created": after_count - before_count,
    }


if __name__ == "__main__":
    import sys
    atom_ids = sys.argv[1:]
    if not atom_ids:
        print("Usage: python3 hebbian.py <atom_id> [atom_id ...]")
        sys.exit(1)
    result = update_from_atoms(atom_ids)
    print(json.dumps(result, indent=2))
