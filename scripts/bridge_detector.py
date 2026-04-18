"""
bridge_detector.py — Cross-domain bridge detection for graph.db

Promoted from experiment 002-cross-domain-bridges (score 3.1/5, grade: promising).

Finds community pairs that live in different interest areas but have high
cosine similarity between their LLM-generated summaries. These are latent
analogies: structurally similar patterns that the user never explicitly connected.

Call via: vault graph bridges
Or directly: python3 bridge_detector.py [--threshold 0.45] [--top 20]

Validated parameters:
    THRESHOLD = 0.45   Cosine cutoff for interesting bridges.
                       Exp-002 finding: 0.72 gave 1 bridge (too strict);
                       0.45 gave 17 meaningful cross-domain pairs.
                       Signal lives at 0.45–0.55 for MiniLM cross-domain.
    MAX_BRIDGES = 20   Max edges written to graph.db per run. Idempotent.
"""

import json
import sqlite3
import uuid
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
from itertools import combinations

VAULT = Path(__file__).parent.parent
GRAPH_DB = VAULT / "graph.db"   # symlink → active snapshot

THRESHOLD = 0.45
MAX_BRIDGES = 20


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0


def load_communities(conn: sqlite3.Connection) -> list[dict]:
    """Load all communities that have a summary and embedding."""
    rows = conn.execute(
        """SELECT id, label, entity_ids, interest_area_ids, summary, summary_embedding
           FROM communities
           WHERE summary IS NOT NULL AND summary != '' AND summary_embedding IS NOT NULL"""
    ).fetchall()

    communities = []
    for r in rows:
        try:
            entity_ids = json.loads(r[2] or "[]")
            interest_area_ids = json.loads(r[3] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue

        emb = np.frombuffer(r[5], dtype="float32") if r[5] else None
        if emb is None or len(emb) == 0:
            continue

        communities.append({
            "id": r[0],
            "label": r[1],
            "entity_ids": entity_ids,
            "interest_area_ids": interest_area_ids,
            "summary": r[4],
            "embedding": emb,
        })

    return communities


def get_interest_area_name(ia_id: str, conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT display_name FROM interest_areas WHERE id=?", (ia_id,)
    ).fetchone()
    return row[0] if row else ia_id[:12]


def get_top_entity(entity_ids: list[str], conn: sqlite3.Connection) -> tuple[str, str]:
    """Return (entity_id, canonical_name) for highest atom_count entity in a community."""
    if not entity_ids:
        return None, None
    placeholders = ",".join("?" * len(entity_ids))
    row = conn.execute(
        f"SELECT id, canonical_name FROM entities WHERE id IN ({placeholders}) "
        f"ORDER BY atom_count DESC LIMIT 1",
        entity_ids
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def find_bridges(communities: list[dict], threshold: float = THRESHOLD) -> list[dict]:
    """
    Find community pairs in different interest areas with cosine > threshold.
    Returns list of bridge dicts sorted by score descending.
    """
    bridges = []
    for a, b in combinations(communities, 2):
        ia_a = set(a["interest_area_ids"])
        ia_b = set(b["interest_area_ids"])
        if not ia_a or not ia_b:
            continue
        if ia_a & ia_b:
            continue  # overlapping domains — not a cross-domain bridge

        score = cosine(a["embedding"], b["embedding"])
        if score >= threshold:
            bridges.append({
                "community_a": a,
                "community_b": b,
                "score": round(float(score), 4),
            })

    return sorted(bridges, key=lambda x: -x["score"])


def write_analogous_to_edges(bridges: list[dict], conn: sqlite3.Connection,
                             max_bridges: int = MAX_BRIDGES) -> list[dict]:
    """
    Write analogous_to edges for the top bridges. Idempotent — updates existing edges.
    Returns list of written/updated edge records.
    """
    written = []
    for bridge in bridges[:max_bridges]:
        a = bridge["community_a"]
        b = bridge["community_b"]

        eid_a, name_a = get_top_entity(a["entity_ids"], conn)
        eid_b, name_b = get_top_entity(b["entity_ids"], conn)
        if not eid_a or not eid_b or eid_a == eid_b:
            continue

        existing = conn.execute(
            """SELECT id FROM relations
               WHERE ((source_entity=? AND target_entity=?) OR (source_entity=? AND target_entity=?))
               AND relation_type='analogous_to'""",
            (eid_a, eid_b, eid_b, eid_a)
        ).fetchone()

        ts = now()
        desc = f"cross-domain bridge (cosine={bridge['score']:.4f})"
        if existing:
            conn.execute(
                "UPDATE relations SET weight=?, description=?, updated_at=? WHERE id=?",
                (bridge["score"], desc, ts, existing[0])
            )
            status = "updated"
        else:
            conn.execute(
                """INSERT INTO relations
                   (id, source_entity, target_entity, relation_type, weight, description,
                    atom_ids, first_seen, last_seen, created_at, updated_at)
                   VALUES (?, ?, ?, 'analogous_to', ?, ?, '[]', ?, ?, ?, ?)""",
                (str(uuid.uuid4()), eid_a, eid_b, bridge["score"], desc,
                 ts, ts, ts, ts)
            )
            status = "created"

        written.append({
            "entity_a": name_a, "entity_b": name_b,
            "community_a": a["label"], "community_b": b["label"],
            "score": bridge["score"],
            "status": status,
        })

    conn.commit()
    return written


def run(graph_db_path: Path = None, threshold: float = THRESHOLD,
        max_bridges: int = MAX_BRIDGES, verbose: bool = True) -> dict:
    """
    Main entry point. Detects cross-domain bridges and writes analogous_to edges.
    Returns summary dict with bridge count and top results.
    """
    db_path = (graph_db_path or GRAPH_DB).resolve()
    if not db_path.exists():
        return {"error": f"graph.db not found at {db_path}"}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    communities = load_communities(conn)
    if len(communities) < 2:
        conn.close()
        return {"bridges_found": 0, "edges_written": 0,
                "reason": f"only {len(communities)} communities with embeddings"}

    if verbose:
        print(f"[bridges] {len(communities)} communities with embeddings, threshold={threshold}")

    bridges = find_bridges(communities, threshold=threshold)
    written = write_analogous_to_edges(bridges, conn, max_bridges=max_bridges)

    conn.close()

    top = [
        {
            "score": b["score"],
            "community_a": b["community_a"]["label"],
            "community_b": b["community_b"]["label"],
        }
        for b in bridges[:5]
    ]

    if verbose:
        print(f"[bridges] {len(bridges)} bridges found, {len(written)} edges written")
        for t in top:
            print(f"  {t['score']:.4f}  {t['community_a']} ↔ {t['community_b']}")

    return {
        "bridges_found": len(bridges),
        "edges_written": len(written),
        "edges_updated": sum(1 for w in written if w["status"] == "updated"),
        "edges_created": sum(1 for w in written if w["status"] == "created"),
        "top": top,
        "all": written,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Detect cross-domain bridges in graph.db")
    p.add_argument("--threshold", type=float, default=THRESHOLD)
    p.add_argument("--top", type=int, default=MAX_BRIDGES)
    p.add_argument("--db", type=str, default=None)
    args = p.parse_args()
    db = Path(args.db) if args.db else None
    result = run(graph_db_path=db, threshold=args.threshold, max_bridges=args.top)
    print(json.dumps({k: v for k, v in result.items() if k != "all"}, indent=2))
