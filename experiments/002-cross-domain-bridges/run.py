"""
Experiment: 002-cross-domain-bridges
Hypothesis: Embedding community summaries and finding high-cosine pairs across
different interest areas will surface latent analogous_to connections invisible
to co-occurrence alone.

Method:
  1. Load all communities that have a summary and interest_area_ids
  2. Embed each summary with all-MiniLM-L6-v2 (reusing graph_store's model)
  3. For every pair of communities in DIFFERENT interest areas:
       - compute cosine similarity of their summary embeddings
       - if cosine > THRESHOLD, it's a candidate bridge
  4. For each candidate pair, pick one representative entity from each community
     and write an analogous_to edge between them
  5. Write a human-readable bridge report to results/bridges.md

The key question: does the semantic content of community summaries capture
cross-domain analogies that the co-occurrence graph misses?

Mode: official
Run with: vault lab run cross-domain-bridges
"""
import sys
import json
import sqlite3
import uuid
import shutil
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
from itertools import combinations

VAULT = Path("/home/agent/memory/vault")
EXP_DIR = Path("/home/agent/memory/vault/experiments/002-cross-domain-bridges")
GRAPH_DB = EXP_DIR / "graph.db"
ATOMS_DB = VAULT / "atoms.db"

# Cosine similarity threshold for "analogous" community pairs
THRESHOLD = 0.45

# Max bridges to write (ranked by cosine score, keeps output readable)
MAX_BRIDGES = 30

sys.path.insert(0, str(VAULT / "scripts"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0


def load_communities(conn: sqlite3.Connection) -> list[dict]:
    """Load all communities that have a summary."""
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


def embed_missing(communities: list[dict], conn: sqlite3.Connection) -> list[dict]:
    """
    For communities whose summary_embedding is null (shouldn't happen after rebuild,
    but just in case), generate embeddings inline.
    Returns the same list with embeddings filled in.
    """
    missing = [c for c in communities if c["embedding"] is None]
    if not missing:
        return communities

    print(f"[exp-002] Generating {len(missing)} missing embeddings...")
    from graph_store import _embed
    texts = [c["summary"] for c in missing]
    embs = _embed(texts)
    for c, emb in zip(missing, embs):
        c["embedding"] = emb
        # Write back to db
        conn.execute(
            "UPDATE communities SET summary_embedding=? WHERE id=?",
            (emb.tobytes(), c["id"])
        )
    conn.commit()
    return communities


def get_interest_area_name(ia_id: str, conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT display_name FROM interest_areas WHERE id=?", (ia_id,)
    ).fetchone()
    return row[0] if row else ia_id[:12]


def get_top_entity(entity_ids: list[str], conn: sqlite3.Connection) -> tuple[str, str]:
    """Return (entity_id, canonical_name) for the highest atom_count entity in a community."""
    if not entity_ids:
        return None, None
    placeholders = ",".join("?" * len(entity_ids))
    row = conn.execute(
        f"SELECT id, canonical_name FROM entities WHERE id IN ({placeholders}) "
        f"ORDER BY atom_count DESC LIMIT 1",
        entity_ids
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def find_bridges(communities: list[dict]) -> list[dict]:
    """
    Find all community pairs in different interest areas with cosine > THRESHOLD.
    Returns list of bridge dicts sorted by score descending.
    """
    bridges = []
    for a, b in combinations(communities, 2):
        # Must be in different interest areas
        ia_a = set(a["interest_area_ids"])
        ia_b = set(b["interest_area_ids"])
        if not ia_a or not ia_b:
            continue
        if ia_a & ia_b:
            # Overlapping interest areas — not a cross-domain bridge
            continue

        score = cosine(a["embedding"], b["embedding"])
        if score >= THRESHOLD:
            bridges.append({
                "community_a": a,
                "community_b": b,
                "score": round(float(score), 4),
            })

    return sorted(bridges, key=lambda x: -x["score"])


def write_analogous_to_edges(bridges: list[dict], conn: sqlite3.Connection) -> list[dict]:
    """
    For each bridge, write an analogous_to edge between the top entity
    from each community. Idempotent — skip if edge already exists.
    Returns list of written edge records.
    """
    written = []
    for bridge in bridges[:MAX_BRIDGES]:
        a = bridge["community_a"]
        b = bridge["community_b"]

        eid_a, name_a = get_top_entity(a["entity_ids"], conn)
        eid_b, name_b = get_top_entity(b["entity_ids"], conn)
        if not eid_a or not eid_b or eid_a == eid_b:
            continue

        # Check for existing analogous_to edge (either direction)
        existing = conn.execute(
            """SELECT id FROM relations
               WHERE ((source_entity=? AND target_entity=?) OR (source_entity=? AND target_entity=?))
               AND relation_type='analogous_to'""",
            (eid_a, eid_b, eid_b, eid_a)
        ).fetchone()

        ts = now()
        if existing:
            # Update description with latest score
            conn.execute(
                "UPDATE relations SET description=?, updated_at=? WHERE id=?",
                (f"cross-domain bridge (cosine={bridge['score']:.4f})", ts, existing[0])
            )
            status = "updated"
        else:
            edge_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO relations
                   (id, source_entity, target_entity, relation_type, weight, description,
                    atom_ids, first_seen, last_seen, created_at, updated_at)
                   VALUES (?, ?, ?, 'analogous_to', ?, ?, '[]', ?, ?, ?, ?)""",
                (edge_id, eid_a, eid_b, bridge["score"],
                 f"cross-domain bridge (cosine={bridge['score']:.4f})",
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


def write_bridge_report(bridges: list[dict], written: list[dict],
                        conn: sqlite3.Connection) -> str:
    """Write a human-readable bridge report to results/bridges.md."""
    lines = [
        "# Cross-Domain Bridge Report",
        f"\n**Run:** {now()[:19]}",
        f"**Threshold:** cosine ≥ {THRESHOLD}",
        f"**Bridges found:** {len(bridges)}",
        f"**Edges written:** {len(written)} (top {MAX_BRIDGES})",
        "\n---\n",
        "## Top Bridges\n",
    ]

    for i, b in enumerate(bridges[:MAX_BRIDGES], 1):
        a = b["community_a"]
        c = b["community_b"]
        ia_a_names = [get_interest_area_name(ia, conn) for ia in a["interest_area_ids"]]
        ia_b_names = [get_interest_area_name(ia, conn) for ia in c["interest_area_ids"]]
        eid_a, name_a = get_top_entity(a["entity_ids"], conn)
        eid_b, name_b = get_top_entity(c["entity_ids"], conn)

        lines.append(f"### {i}. {a['label']}  ↔  {c['label']}")
        lines.append(f"**Cosine:** {b['score']:.4f}  |  "
                     f"**Domains:** {', '.join(ia_a_names)} ↔ {', '.join(ia_b_names)}")
        lines.append(f"**Bridge entities:** `{name_a}` ↔ `{name_b}`")
        lines.append(f"\n**A:** {(a['summary'] or '')[:300]}...")
        lines.append(f"\n**B:** {(c['summary'] or '')[:300]}...")
        lines.append("")

    if len(bridges) > MAX_BRIDGES:
        lines.append(f"\n*... {len(bridges) - MAX_BRIDGES} more bridges below threshold (not written)*")

    report = "\n".join(lines)
    (EXP_DIR / "results" / "bridges.md").write_text(report)
    return report


def run():
    import graph_store

    print("[exp-002] Cross-domain bridge detection")
    print(f"[exp-002] Threshold: cosine ≥ {THRESHOLD}")
    print()

    # Fork from the snapshot with the best summary coverage.
    # The Hebbian snapshot has communities but no LLM summaries (detect_communities
    # only does label propagation). We need a snapshot where vault graph rebuild
    # has been run. Find the one with the most non-null summary_embeddings.
    snap_dir = VAULT / "snapshots"
    best_db = None
    best_count = -1
    prod_db = VAULT / "graph.db"

    candidates = list(snap_dir.glob("*.db")) if snap_dir.exists() else []
    candidates.append(prod_db.resolve())  # always include current production

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            c = sqlite3.connect(str(candidate))
            n = c.execute(
                "SELECT COUNT(*) FROM communities WHERE summary_embedding IS NOT NULL"
            ).fetchone()[0]
            c.close()
            if n > best_count:
                best_count = n
                best_db = candidate
        except Exception:
            continue

    if best_db is None or best_count == 0:
        print("[exp-002] No snapshots with community summaries found.")
        print("[exp-002] Run: vault graph rebuild  (generates LLM summaries)")
        return

    print(f"[exp-002] Forking from: {best_db.name} ({best_count} summaries)")
    shutil.copy2(str(best_db), str(GRAPH_DB))

    # Point graph_store at experimental db
    original_db = graph_store.GRAPH_DB
    graph_store.GRAPH_DB = GRAPH_DB
    conn = sqlite3.connect(str(GRAPH_DB))
    conn.row_factory = sqlite3.Row

    try:
        # --- Phase 1: Baseline ---
        baseline_analogous = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE relation_type='analogous_to'"
        ).fetchone()[0]
        total_communities = conn.execute("SELECT COUNT(*) FROM communities").fetchone()[0]
        print(f"[exp-002] Baseline: {total_communities} communities, "
              f"{baseline_analogous} analogous_to edges")

        # --- Phase 2: Load communities with embeddings ---
        print("[exp-002] Loading community summaries + embeddings...")
        communities = load_communities(conn)
        print(f"[exp-002]   {len(communities)} communities with summary embeddings")

        if len(communities) < 2:
            print("[exp-002] Not enough communities with summaries to find bridges.")
            print("[exp-002] Run: vault graph rebuild  (to generate summaries)")
            return

        # Check interest area coverage
        with_ia = [c for c in communities if c["interest_area_ids"]]
        print(f"[exp-002]   {len(with_ia)} communities have interest area tags")

        # --- Phase 3: Find bridges ---
        print(f"[exp-002] Scanning {len(communities)*(len(communities)-1)//2} pairs "
              f"for cross-domain bridges...")
        bridges = find_bridges(communities)
        print(f"[exp-002]   {len(bridges)} candidate bridges at threshold={THRESHOLD}")

        if not bridges:
            print(f"[exp-002] No bridges found at threshold {THRESHOLD}.")
            print(f"[exp-002] Try lowering THRESHOLD (currently {THRESHOLD}).")
        else:
            # Show top 10
            print(f"\n[exp-002] Top bridges:")
            for b in bridges[:10]:
                a_label = b["community_a"]["label"][:35]
                c_label = b["community_b"]["label"][:35]
                print(f"  {b['score']:.4f}  {a_label}  ↔  {c_label}")
            if len(bridges) > 10:
                print(f"  ... {len(bridges)-10} more")

        # --- Phase 4: Write analogous_to edges ---
        print(f"\n[exp-002] Writing analogous_to edges (top {MAX_BRIDGES})...")
        written = write_analogous_to_edges(bridges, conn)
        created = sum(1 for w in written if w["status"] == "created")
        updated = sum(1 for w in written if w["status"] == "updated")
        print(f"[exp-002]   {created} new edges, {updated} updated")

        # --- Phase 5: Post metrics ---
        post_analogous = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE relation_type='analogous_to'"
        ).fetchone()[0]
        print(f"\n[exp-002] analogous_to edges: {baseline_analogous} → {post_analogous} "
              f"(+{post_analogous - baseline_analogous})")

        # --- Phase 6: Write bridge report ---
        print("[exp-002] Writing bridge report...")
        write_bridge_report(bridges, written, conn)
        print(f"[exp-002]   Written to results/bridges.md")

        # --- Write metrics.json ---
        metrics = {
            "experiment": "002-cross-domain-bridges",
            "run_at": now(),
            "params": {"threshold": THRESHOLD, "max_bridges": MAX_BRIDGES},
            "forked_from": prod_db.resolve().name,
            "baseline": {
                "communities": total_communities,
                "analogous_to_edges": baseline_analogous,
            },
            "post": {
                "analogous_to_edges": post_analogous,
                "bridges_found": len(bridges),
                "edges_created": created,
                "edges_updated": updated,
            },
            "top_bridges": [
                {
                    "score": b["score"],
                    "community_a": b["community_a"]["label"],
                    "community_b": b["community_b"]["label"],
                }
                for b in bridges[:20]
            ],
            "written_edges": written,
        }
        (EXP_DIR / "results").mkdir(exist_ok=True)
        (EXP_DIR / "results" / "metrics.json").write_text(json.dumps(metrics, indent=2))
        print(f"\n[exp-002] Done.")
        print(f"  metrics:  {EXP_DIR}/results/metrics.json")
        print(f"  bridges:  {EXP_DIR}/results/bridges.md")
        print(f"  next:     vault lab compare cross-domain-bridges")

    finally:
        conn.close()
        graph_store.GRAPH_DB = original_db


if __name__ == "__main__":
    run()
