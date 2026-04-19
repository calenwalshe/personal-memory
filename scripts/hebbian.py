"""
hebbian.py — Live Hebbian weight updates + decay for graph.db

Implements "neurons that fire together, wire together" as an incremental,
per-session update. Called from vault chunk after new atoms are written.

Each atom carries a list of entities. For every pair of entities that
co-appeared in this session's atoms, we add ETA to their relation weight
in graph.db. For every hebbian edge that did NOT fire this session, we
apply multiplicative decay: w *= (1 - DECAY). Edges below MIN_WEIGHT
are pruned (deleted).

Parameters validated by experiments 001–003 and research dossier hebbian-decay:
    ETA = 0.3        Per-session increment. Exp-003 validated.
    MAX_WEIGHT = 20.0  Soft ceiling — preserves 32x gradient spread.
    DECAY = 0.03     Per-session multiplicative decay for non-firing edges.
                     Half-life ~23 days at 1 session/day. Derived from
                     steady-state formula: w* = p·η / (d·(1-p)).
                     d=0.03 → p=0.20 pairs stabilize at ~2.5,
                               p=0.50 pairs stabilize at ~10.0.
    MIN_WEIGHT = 0.01  Prune threshold — edges below this are deleted
                     rather than left as floating-point noise.

Increment vs decay order (within one vault chunk call):
    1. update_from_atoms() — fire increment on co-appearing pairs
    2. apply_decay()       — decay non-firing hebbian edges
    Fired pairs receive +ETA first; decay is never applied to a pair
    that fired in the same session.
"""

import json
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
from itertools import combinations
from collections import defaultdict

VAULT = Path(__file__).parent.parent
ATOMS_DB = VAULT / "atoms.db"
GRAPH_DB = VAULT / "graph.db"   # symlink → active snapshot

ETA = 0.3          # per-session learning rate (validated by exp-003)
MAX_WEIGHT = 20.0  # soft ceiling — preserves gradient, prevents runaway
DECAY = 0.03       # per-session multiplicative decay for non-firing edges
MIN_WEIGHT = 0.01  # prune threshold — edges below this are deleted


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
    row = conn.execute(
        """SELECT id, weight FROM relations
           WHERE ((source_entity=? AND target_entity=?) OR (source_entity=? AND target_entity=?))
           AND relation_type='related_to'""",
        (eid_a, eid_b, eid_b, eid_a)
    ).fetchone()

    if row:
        new_weight = min(row[1] + ETA, MAX_WEIGHT)
        conn.execute(
            "UPDATE relations SET weight=?, last_seen=?, updated_at=? WHERE id=?",
            (new_weight, ts, ts, row[0])
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
    Run Hebbian increment for the given atom IDs.

    Loads atoms from atoms.db, extracts entity pairs that co-appeared,
    and adds ETA to each pair's edge weight in graph.db.

    Returns a summary dict including fired_entity_ids for use by apply_decay().
    """
    if not atom_ids:
        return {
            "pairs_found": 0, "edges_updated": 0, "edges_created": 0,
            "fired_entity_ids": set(),
        }

    db_path = graph_db_path or GRAPH_DB
    db_path = db_path.resolve() if hasattr(db_path, 'resolve') else Path(db_path).resolve()

    if not db_path.exists():
        return {"error": f"graph.db not found at {db_path}", "fired_entity_ids": set()}

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
        return {
            "pairs_found": 0, "edges_updated": 0, "edges_created": 0,
            "fired_entity_ids": set(),
        }

    # Resolve to entity IDs
    g_conn = sqlite3.connect(str(db_path))
    g_conn.row_factory = sqlite3.Row

    entity_ids = {}
    for name in session_entities:
        eid = _resolve_entity_id(name, g_conn)
        if eid:
            entity_ids[name] = eid

    fired_entity_ids = set(entity_ids.values())

    if len(entity_ids) < 2:
        g_conn.close()
        return {
            "pairs_found": 0, "edges_updated": 0, "edges_created": 0,
            "fired_entity_ids": fired_entity_ids,
        }

    before_count = g_conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]

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
        "fired_entity_ids": fired_entity_ids,
    }


def apply_decay(
    fired_entity_ids: set,
    graph_db_path: Path = None,
    decay: float = DECAY,
    min_weight: float = MIN_WEIGHT,
) -> dict:
    """
    Apply multiplicative decay to all hebbian-written related_to edges
    that did NOT fire in the current session.

    For each non-firing edge: w = max(0.0, w * (1 - decay))
    Edges below min_weight are deleted (pruned).

    Args:
        fired_entity_ids: set of entity IDs that appeared in this session's
                          atoms (from update_from_atoms return value). These
                          edges are excluded from decay.
        graph_db_path:    override path to graph.db (defaults to active symlink)
        decay:            per-session decay factor (default DECAY=0.03)
        min_weight:       prune threshold (default MIN_WEIGHT=0.01)

    Returns:
        {edges_decayed, edges_pruned}
    """
    db_path = graph_db_path or GRAPH_DB
    db_path = db_path.resolve() if hasattr(db_path, 'resolve') else Path(db_path).resolve()

    if not db_path.exists():
        return {"edges_decayed": 0, "edges_pruned": 0,
                "error": f"graph.db not found at {db_path}"}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ts = now()

    # Load all hebbian related_to edges
    rows = conn.execute(
        """SELECT id, source_entity, target_entity, weight
           FROM relations
           WHERE relation_type='related_to' AND description='hebbian'"""
    ).fetchall()

    edges_decayed = 0
    edges_pruned = 0
    to_delete = []
    to_update = []

    for row in rows:
        # Skip edges where either entity fired this session
        if row["source_entity"] in fired_entity_ids or row["target_entity"] in fired_entity_ids:
            continue

        new_weight = row["weight"] * (1.0 - decay)

        if new_weight < min_weight:
            to_delete.append(row["id"])
            edges_pruned += 1
        else:
            to_update.append((new_weight, ts, ts, row["id"]))
            edges_decayed += 1

    if to_update:
        conn.executemany(
            "UPDATE relations SET weight=?, last_seen=?, updated_at=? WHERE id=?",
            to_update
        )

    if to_delete:
        placeholders = ",".join("?" * len(to_delete))
        conn.execute(f"DELETE FROM relations WHERE id IN ({placeholders})", to_delete)

    conn.commit()
    conn.close()

    return {"edges_decayed": edges_decayed, "edges_pruned": edges_pruned}


def stale_edge_count(graph_db_path: Path = None, days: int = 30) -> int:
    """
    Count hebbian edges that have not fired in the last `days` days.
    Used by vault graph stats for stale persistence monitoring.
    """
    db_path = graph_db_path or GRAPH_DB
    db_path = db_path.resolve() if hasattr(db_path, 'resolve') else Path(db_path).resolve()

    if not db_path.exists():
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(str(db_path))
    count = conn.execute(
        """SELECT COUNT(*) FROM relations
           WHERE relation_type='related_to' AND description='hebbian'
           AND last_seen < ?""",
        (cutoff,)
    ).fetchone()[0]
    conn.close()
    return count


if __name__ == "__main__":
    import sys
    atom_ids = sys.argv[1:]
    if not atom_ids:
        print("Usage: python3 hebbian.py <atom_id> [atom_id ...]")
        sys.exit(1)
    result = update_from_atoms(atom_ids)
    fired = result.pop("fired_entity_ids", set())
    decay_result = apply_decay(fired)
    result.update(decay_result)
    print(json.dumps(result, indent=2))
