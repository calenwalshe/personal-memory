"""
Experiment: 003-hebbian-v2
Hypothesis: Removing the MAX_DELTA cap (set to 20.0) allows the Hebbian weight update to
encode co-activation magnitude rather than just co-presence.

Fix from exp-001: MAX_DELTA was 3.0, which saturated at 10 sessions — 85% of qualifying
pairs hit the ceiling, making the update binary. With MAX_DELTA=20.0, the top pair (65
sessions) gets Δw=19.5 vs the weakest (2 sessions) getting Δw=0.6 — a 32x spread that
actually encodes co-activation magnitude.

Mode: official
Run with: vault lab run hebbian-v2
"""
import sys
import json
import sqlite3
import math
from pathlib import Path
from itertools import combinations
from collections import defaultdict
from datetime import datetime

VAULT = Path("/home/agent/memory/vault")
EXP_DIR = Path("/home/agent/memory/vault/experiments/003-hebbian-v2")
GRAPH_DB = EXP_DIR / "graph.db"
ATOMS_DB = VAULT / "atoms.db"

ETA = 0.3               # learning rate
MIN_COACTIVATIONS = 2   # noise filter
MAX_DELTA = 20.0        # raised from 3.0 — must be > ETA × max_count (0.3 × 65 = 19.5)

sys.path.insert(0, str(VAULT / "scripts"))


def load_session_entities() -> dict[str, list[str]]:
    """
    Load atoms from atoms.db grouped by session_id.
    Returns {session_id: [entity1, entity2, ...]} — deduplicated per session.
    """
    conn = sqlite3.connect(str(ATOMS_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT session_ids, entities FROM atoms WHERE invalidated_by IS NULL"
    ).fetchall()
    conn.close()

    session_entities: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        try:
            sessions = json.loads(row["session_ids"] or "[]")
            entities = json.loads(row["entities"] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue

        for sid in sessions:
            for ent in entities:
                if ent:
                    session_entities[sid].add(ent.lower().strip())

    return {sid: list(ents) for sid, ents in session_entities.items()}


def resolve_entities_to_ids(entity_names: list[str], graph_conn: sqlite3.Connection) -> list[str]:
    """
    Resolve raw entity name strings to canonical entity IDs via aliases.
    Returns a list of entity IDs (may be shorter than input if some names don't resolve).
    """
    resolved = set()
    for name in entity_names:
        name_lower = name.lower().strip()
        # Try canonical name match
        row = graph_conn.execute(
            "SELECT id FROM entities WHERE lower(canonical_name) = ?", (name_lower,)
        ).fetchone()
        if row:
            resolved.add(row[0])
            continue
        # Try alias match
        rows = graph_conn.execute("SELECT id, aliases FROM entities").fetchall()
        for r in rows:
            try:
                aliases = [a.lower().strip() for a in json.loads(r[1] or "[]")]
                if name_lower in aliases:
                    resolved.add(r[0])
                    break
            except (json.JSONDecodeError, TypeError):
                continue
    return list(resolved)


def count_coactivations(session_entities: dict[str, list[str]],
                        graph_conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    """
    For each session, find entity pairs that co-appeared.
    Resolve raw strings to canonical IDs.
    Returns {(entity_id_a, entity_id_b): session_count} — sorted tuple keys, no duplicates.
    """
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)

    for sid, raw_entities in session_entities.items():
        entity_ids = resolve_entities_to_ids(raw_entities, graph_conn)
        if len(entity_ids) < 2:
            continue
        for a, b in combinations(sorted(entity_ids), 2):
            pair_counts[(a, b)] += 1

    return dict(pair_counts)


def apply_hebbian_updates(pair_counts: dict[tuple[str, str], int],
                          graph_conn: sqlite3.Connection) -> list[dict]:
    """
    For each co-activating pair above MIN_COACTIVATIONS:
      - Find the existing related_to edge (co-occurrence edge)
      - Apply Δw = min(ETA × count, MAX_DELTA)
      - Update in experimental graph.db
    Returns list of update records for metrics.
    """
    updates = []
    now = datetime.utcnow().isoformat()

    for (a, b), count in pair_counts.items():
        if count < MIN_COACTIVATIONS:
            continue

        delta = min(ETA * count, MAX_DELTA)

        # Check for existing edge (either direction)
        row = graph_conn.execute(
            """SELECT id, weight FROM relations
               WHERE ((source_entity=? AND target_entity=?) OR (source_entity=? AND target_entity=?))
               AND relation_type='related_to'""",
            (a, b, b, a)
        ).fetchone()

        if row:
            old_weight = row[1]
            new_weight = old_weight + delta
            graph_conn.execute(
                "UPDATE relations SET weight=?, updated_at=? WHERE id=?",
                (new_weight, now, row[0])
            )
            updates.append({
                "entity_a": a, "entity_b": b,
                "coactivations": count,
                "delta": round(delta, 4),
                "old_weight": old_weight,
                "new_weight": round(new_weight, 4),
                "edge_existed": True,
            })
        else:
            # No existing co-occurrence edge — create one (they co-activated but were in different atoms)
            import uuid
            edge_id = str(uuid.uuid4())
            graph_conn.execute(
                """INSERT INTO relations
                   (id, source_entity, target_entity, relation_type, weight, atom_ids,
                    first_seen, last_seen, created_at, updated_at)
                   VALUES (?, ?, ?, 'related_to', ?, '[]', ?, ?, ?, ?)""",
                (edge_id, a, b, round(delta, 4), now, now, now, now)
            )
            updates.append({
                "entity_a": a, "entity_b": b,
                "coactivations": count,
                "delta": round(delta, 4),
                "old_weight": 0,
                "new_weight": round(delta, 4),
                "edge_existed": False,
            })

    graph_conn.commit()
    return updates


def get_community_metrics(graph_conn: sqlite3.Connection) -> dict:
    """Snapshot community structure for before/after comparison. Includes stale communities."""
    communities = graph_conn.execute(
        "SELECT id, label, entity_ids, atom_count FROM communities"
    ).fetchall()

    sizes = []
    for c in communities:
        try:
            entity_ids = json.loads(c[2] or "[]")
            sizes.append(len(entity_ids))
        except (json.JSONDecodeError, TypeError):
            sizes.append(0)

    return {
        "community_count": len(communities),
        "total_entities_in_communities": sum(sizes),
        "avg_community_size": round(sum(sizes) / len(sizes), 2) if sizes else 0,
        "max_community_size": max(sizes) if sizes else 0,
        "singleton_count": sum(1 for s in sizes if s == 1),
    }


def write_communities_to_db(communities: list[dict], graph_conn: sqlite3.Connection):
    """
    Write detected community structures to graph.db (no LLM summaries — structural only).
    Clears existing communities first for clean comparison.
    """
    import uuid
    now = datetime.utcnow().isoformat()

    graph_conn.execute("DELETE FROM communities")
    for c in communities:
        entity_ids_json = json.dumps(c["entity_ids"])
        # detect_communities returns {label_id, entity_ids, entity_names, size}
        # Use first entity name as label if available, else label_id
        entity_names = c.get("entity_names", [])
        label = ", ".join(entity_names[:3]) if entity_names else str(c.get("label_id", "?"))
        graph_conn.execute(
            """INSERT INTO communities
               (id, label, entity_ids, interest_area_ids, atom_count,
                summary, time_first, time_last, generated_at, stale)
               VALUES (?, ?, ?, '[]', ?, NULL, ?, ?, ?, 0)""",
            (str(uuid.uuid4()), label, entity_ids_json,
             len(c["entity_ids"]), now, now, now)
        )
    graph_conn.commit()


def get_relation_metrics(graph_conn: sqlite3.Connection) -> dict:
    """Snapshot relation weight distribution."""
    rows = graph_conn.execute(
        "SELECT weight FROM relations WHERE relation_type='related_to'"
    ).fetchall()
    weights = [r[0] for r in rows]
    if not weights:
        return {}
    return {
        "edge_count": len(weights),
        "mean_weight": round(sum(weights) / len(weights), 3),
        "max_weight": max(weights),
        "weight_gt_5": sum(1 for w in weights if w > 5),
        "weight_gt_10": sum(1 for w in weights if w > 10),
    }


def run():
    import shutil
    import graph_store

    print(f"[exp-001] Hebbian weight update experiment")
    print(f"[exp-001] ETA={ETA}, MIN_COACTIVATIONS={MIN_COACTIVATIONS}, MAX_DELTA={MAX_DELTA}")
    print()

    # --- Phase 0: Fresh fork from production graph.db ---
    # Always start from a clean copy so repeated runs are idempotent.
    prod_db = VAULT / "graph.db"
    print(f"[exp-001] Forking fresh copy from production graph.db...")
    shutil.copy2(str(prod_db), str(GRAPH_DB))
    print(f"[exp-001]   Forked → {GRAPH_DB}")
    print()

    # Monkey-patch graph_store to use experimental graph.db
    original_db = graph_store.GRAPH_DB
    graph_store.GRAPH_DB = GRAPH_DB

    try:
        _run_experiment(graph_store)
    finally:
        graph_store.GRAPH_DB = original_db


def _run_experiment(graph_store):
    """Core experiment logic — runs against the experimental graph.db."""

    # --- Phase 1: Pre-Hebbian community detection (baseline snapshot) ---
    print("[exp-001] Phase 1: Detecting communities on pre-Hebbian weights...")
    pre_communities = graph_store.detect_communities()
    print(f"[exp-001]   {len(pre_communities)} communities detected")

    graph_conn = sqlite3.connect(str(GRAPH_DB))
    graph_conn.row_factory = sqlite3.Row
    write_communities_to_db(pre_communities, graph_conn)

    baseline_rels = get_relation_metrics(graph_conn)
    baseline_comms = get_community_metrics(graph_conn)
    print(f"[exp-001]   Baseline: {baseline_comms['community_count']} communities, "
          f"mean_weight={baseline_rels.get('mean_weight', '?')}, "
          f"edges>5={baseline_rels.get('weight_gt_5', '?')}")
    print()

    # --- Phase 2: Load session-entity co-activations ---
    print("[exp-001] Phase 2: Loading session entity co-activations from atoms.db...")
    session_entities = load_session_entities()
    print(f"[exp-001]   {len(session_entities)} sessions with entity data")

    pair_counts = count_coactivations(session_entities, graph_conn)
    above_threshold = {p: c for p, c in pair_counts.items() if c >= MIN_COACTIVATIONS}
    print(f"[exp-001]   {len(pair_counts)} unique pairs total, "
          f"{len(above_threshold)} above threshold={MIN_COACTIVATIONS}")

    if above_threshold:
        top = sorted(above_threshold.items(), key=lambda x: -x[1])[:5]
        print(f"[exp-001]   Top co-activating pairs:")
        for (a, b), cnt in top:
            a_name = graph_conn.execute("SELECT canonical_name FROM entities WHERE id=?", (a,)).fetchone()
            b_name = graph_conn.execute("SELECT canonical_name FROM entities WHERE id=?", (b,)).fetchone()
            a_str = a_name[0] if a_name else a[:8]
            b_str = b_name[0] if b_name else b[:8]
            print(f"    {a_str} ↔ {b_str}: {cnt} sessions")
    print()

    # --- Phase 3: Apply Hebbian weight updates ---
    print("[exp-001] Phase 3: Applying Hebbian weight updates...")
    updates = apply_hebbian_updates(above_threshold, graph_conn)
    updated_existing = sum(1 for u in updates if u["edge_existed"])
    new_edges = sum(1 for u in updates if not u["edge_existed"])
    print(f"[exp-001]   {updated_existing} existing edges updated, {new_edges} new edges created")

    if updates:
        top_delta = sorted(updates, key=lambda u: -u["delta"])[:5]
        print(f"[exp-001]   Largest weight changes:")
        for u in top_delta:
            a_name = graph_conn.execute("SELECT canonical_name FROM entities WHERE id=?", (u["entity_a"],)).fetchone()
            b_name = graph_conn.execute("SELECT canonical_name FROM entities WHERE id=?", (u["entity_b"],)).fetchone()
            a_str = a_name[0] if a_name else u["entity_a"][:8]
            b_str = b_name[0] if b_name else u["entity_b"][:8]
            print(f"    {a_str} ↔ {b_str}: {u['old_weight']} → {u['new_weight']} (+{u['delta']})")
    graph_conn.close()
    print()

    # --- Phase 4: Post-Hebbian community detection ---
    print("[exp-001] Phase 4: Detecting communities on post-Hebbian weights...")
    post_community_list = graph_store.detect_communities()
    print(f"[exp-001]   {len(post_community_list)} communities detected")

    graph_conn2 = sqlite3.connect(str(GRAPH_DB))
    graph_conn2.row_factory = sqlite3.Row
    write_communities_to_db(post_community_list, graph_conn2)

    post_rels = get_relation_metrics(graph_conn2)
    post_comms = get_community_metrics(graph_conn2)
    graph_conn2.close()

    # --- Phase 5: Summary ---
    print()
    print("[exp-001] Results:")
    for k in baseline_rels:
        bv = baseline_rels[k]
        pv = post_rels.get(k, "?")
        delta_str = f" (Δ{round(pv - bv, 3):+})" if isinstance(pv, (int, float)) else ""
        print(f"  relations.{k}: {bv} → {pv}{delta_str}")
    for k in baseline_comms:
        bv = baseline_comms[k]
        pv = post_comms.get(k, "?")
        delta_str = f" (Δ{round(pv - bv, 3):+})" if isinstance(pv, (int, float)) else ""
        print(f"  communities.{k}: {bv} → {pv}{delta_str}")

    # --- Write metrics.json ---
    metrics = {
        "experiment": "003-hebbian-v2",
        "run_at": datetime.utcnow().isoformat(),
        "params": {"eta": ETA, "min_coactivations": MIN_COACTIVATIONS, "max_delta": MAX_DELTA},
        "sessions_processed": len(session_entities),
        "total_pairs": len(pair_counts),
        "pairs_above_threshold": len(above_threshold),
        "edges_updated": updated_existing,
        "edges_created": new_edges,
        "baseline": {"relations": baseline_rels, "communities": baseline_comms},
        "post": {"relations": post_rels, "communities": post_comms},
        "top_updates": sorted(updates, key=lambda u: -u["delta"])[:20],
    }

    results_dir = EXP_DIR / "results"
    results_dir.mkdir(exist_ok=True)
    (results_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print()
    print(f"[exp-001] Metrics written to {results_dir}/metrics.json")
    print(f"[exp-001] Done. Run `vault lab compare hebbian-weights` to see full diff.")


if __name__ == "__main__":
    run()
