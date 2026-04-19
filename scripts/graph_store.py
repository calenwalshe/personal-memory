"""
graph_store.py — SQLite storage layer for L2 entity graph + community detection.

L2 sits on top of L1 atoms (atoms.db) and produces:
  entities    — canonical entity registry (resolved from L1's free-form strings)
  interest_areas — normalized personal interest taxonomy (from L1 interest_tags)
  relations   — co-occurrence + typed edges between entities
  communities — detected clusters with LLM-generated summaries (the L2 artifact)
  l2_state    — processing cursor and thresholds

graph.db is kept separate from atoms.db so L1 stays immutable and L2 is
independently rebuildable.

Key functions:
  init_graph_db()              — create schema
  upsert_entity()              — add/update a canonical entity
  upsert_interest_area()       — add/update an interest area
  upsert_relation()            — add/update a co-occurrence or typed edge
  get_entity_by_alias()        — look up canonical entity by raw string
  incremental_update(atom_ids) — process new L1 atoms into L2 state
  graph_stats()                — counts for all tables
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

# Suppress HF noise
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")
os.environ.setdefault("TQDM_DISABLE", "1")
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
ATOMS_DB = VAULT / "atoms.db"
GRAPH_DB = VAULT / "graph.db"
EMBED_MODEL = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")
EMBED_DIM = 384

_model = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_model():
    global _model
    if _model is None:
        import io, contextlib
        from sentence_transformers import SentenceTransformer
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _model = SentenceTransformer(EMBED_MODEL)
    return _model


def _embed(texts: list[str]) -> np.ndarray:
    return _get_model().encode(texts, normalize_embeddings=True).astype("float32")


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two normalized vectors."""
    return float(np.dot(a, b))


# ── Schema ──────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL UNIQUE,
    entity_type     TEXT NOT NULL DEFAULT 'concept',
    aliases         TEXT NOT NULL DEFAULT '[]',
    description     TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    atom_count      INTEGER NOT NULL DEFAULT 1,
    atom_ids        TEXT NOT NULL DEFAULT '[]',
    embedding       BLOB,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ent_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_ent_name ON entities(canonical_name);

CREATE TABLE IF NOT EXISTS interest_areas (
    id              TEXT PRIMARY KEY,
    canonical_tag   TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    description     TEXT,
    raw_tags        TEXT NOT NULL DEFAULT '[]',
    atom_count      INTEGER NOT NULL DEFAULT 0,
    atom_ids        TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relations (
    id              TEXT PRIMARY KEY,
    source_entity   TEXT NOT NULL REFERENCES entities(id),
    target_entity   TEXT NOT NULL REFERENCES entities(id),
    relation_type   TEXT NOT NULL DEFAULT 'related_to',
    weight          REAL NOT NULL DEFAULT 1.0,
    description     TEXT,
    atom_ids        TEXT NOT NULL DEFAULT '[]',
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(source_entity, target_entity, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_rel_src ON relations(source_entity);
CREATE INDEX IF NOT EXISTS idx_rel_tgt ON relations(target_entity);
CREATE INDEX IF NOT EXISTS idx_rel_type ON relations(relation_type);

CREATE TABLE IF NOT EXISTS communities (
    id                TEXT PRIMARY KEY,
    label             TEXT NOT NULL,
    entity_ids        TEXT NOT NULL DEFAULT '[]',
    interest_area_ids TEXT NOT NULL DEFAULT '[]',
    atom_count        INTEGER NOT NULL DEFAULT 0,
    summary           TEXT,
    key_findings      TEXT DEFAULT '[]',
    time_first        TEXT,
    time_last         TEXT,
    generated_at      TEXT NOT NULL,
    stale             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS l2_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def init_graph_db() -> sqlite3.Connection:
    """Create graph.db with schema. Returns open connection."""
    GRAPH_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(GRAPH_DB))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _conn() -> sqlite3.Connection:
    if not GRAPH_DB.exists():
        return init_graph_db()
    conn = sqlite3.connect(str(GRAPH_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── l2_state helpers ────────────────────────────────────────────────────────

def get_state(key: str, default: str = "") -> str:
    conn = _conn()
    row = conn.execute("SELECT value FROM l2_state WHERE key=?", [key]).fetchone()
    conn.close()
    return row["value"] if row else default


def set_state(key: str, value: str):
    conn = _conn()
    now = _now()
    conn.execute(
        "INSERT OR REPLACE INTO l2_state(key, value, updated_at) VALUES(?,?,?)",
        [key, value, now],
    )
    conn.commit()
    conn.close()


# ── Entity CRUD ─────────────────────────────────────────────────────────────

def upsert_entity(
    canonical_name: str,
    entity_type: str = "concept",
    aliases: list[str] = None,
    atom_ids: list[str] = None,
    first_seen: str = None,
    last_seen: str = None,
    description: str = None,
    embedding: np.ndarray = None,
) -> str:
    """Insert or update a canonical entity. Returns entity ID."""
    conn = _conn()
    now = _now()
    aliases = aliases or []
    atom_ids = atom_ids or []
    first_seen = first_seen or now
    last_seen = last_seen or now

    existing = conn.execute(
        "SELECT * FROM entities WHERE canonical_name=?", [canonical_name]
    ).fetchone()

    emb_blob = embedding.tobytes() if embedding is not None else None

    if existing:
        entity_id = existing["id"]
        # Merge aliases and atom_ids
        existing_aliases = json.loads(existing["aliases"] or "[]")
        existing_atoms = json.loads(existing["atom_ids"] or "[]")
        merged_aliases = list(set(existing_aliases) | set(aliases))
        merged_atoms = list(set(existing_atoms) | set(atom_ids))

        conn.execute(
            """UPDATE entities SET
               entity_type=?, aliases=?, atom_count=?, atom_ids=?,
               last_seen=?, description=COALESCE(?,description),
               embedding=COALESCE(?,embedding), updated_at=?
               WHERE id=?""",
            [
                entity_type,
                json.dumps(merged_aliases),
                len(merged_atoms),
                json.dumps(merged_atoms),
                max(last_seen, existing["last_seen"]),
                description,
                emb_blob,
                now,
                entity_id,
            ],
        )
    else:
        entity_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO entities
               (id, canonical_name, entity_type, aliases, description,
                first_seen, last_seen, atom_count, atom_ids, embedding,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                entity_id,
                canonical_name,
                entity_type,
                json.dumps(aliases),
                description,
                first_seen,
                last_seen,
                len(atom_ids),
                json.dumps(atom_ids),
                emb_blob,
                now,
                now,
            ],
        )

    conn.commit()
    conn.close()
    return entity_id


def get_entity_by_alias(raw_string: str) -> Optional[dict]:
    """Find canonical entity by exact alias match (case-insensitive)."""
    conn = _conn()
    raw_lower = raw_string.lower()

    # Try canonical_name first
    row = conn.execute(
        "SELECT * FROM entities WHERE LOWER(canonical_name)=?", [raw_lower]
    ).fetchone()
    if row:
        conn.close()
        return _entity_dict(row)

    # Scan aliases
    rows = conn.execute("SELECT * FROM entities").fetchall()
    for row in rows:
        aliases = json.loads(row["aliases"] or "[]")
        if any(a.lower() == raw_lower for a in aliases):
            conn.close()
            return _entity_dict(row)

    conn.close()
    return None


def get_entity_by_embedding(
    embedding: np.ndarray,
    threshold: float = 0.85,
) -> Optional[dict]:
    """Find canonical entity by embedding similarity."""
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM entities WHERE embedding IS NOT NULL"
    ).fetchall()
    conn.close()

    best_score = -1.0
    best_entity = None
    for row in rows:
        emb = np.frombuffer(row["embedding"], dtype="float32")
        score = _cosine(embedding, emb)
        if score > best_score:
            best_score = score
            best_entity = row

    if best_entity and best_score >= threshold:
        return {**_entity_dict(best_entity), "similarity": best_score}
    return None


def _entity_dict(row) -> dict:
    d = dict(row)
    for f in ("aliases", "atom_ids"):
        try:
            d[f] = json.loads(d[f]) if d[f] else []
        except (json.JSONDecodeError, TypeError):
            d[f] = []
    if d.get("embedding"):
        d["embedding"] = np.frombuffer(d["embedding"], dtype="float32")
    return d


def list_entities(entity_type: str = None, limit: int = 100) -> list[dict]:
    conn = _conn()
    if entity_type:
        rows = conn.execute(
            "SELECT * FROM entities WHERE entity_type=? ORDER BY atom_count DESC LIMIT ?",
            [entity_type, limit],
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM entities ORDER BY atom_count DESC LIMIT ?", [limit]
        ).fetchall()
    conn.close()
    return [_entity_dict(r) for r in rows]


def get_entity(entity_id: str) -> Optional[dict]:
    conn = _conn()
    row = conn.execute("SELECT * FROM entities WHERE id=?", [entity_id]).fetchone()
    conn.close()
    return _entity_dict(row) if row else None


# ── Interest Area CRUD ───────────────────────────────────────────────────────

def upsert_interest_area(
    canonical_tag: str,
    display_name: str,
    raw_tags: list[str] = None,
    atom_ids: list[str] = None,
    description: str = None,
) -> str:
    """Insert or update an interest area. Returns ID."""
    conn = _conn()
    now = _now()
    raw_tags = raw_tags or []
    atom_ids = atom_ids or []

    existing = conn.execute(
        "SELECT * FROM interest_areas WHERE canonical_tag=?", [canonical_tag]
    ).fetchone()

    if existing:
        ia_id = existing["id"]
        existing_tags = json.loads(existing["raw_tags"] or "[]")
        existing_atoms = json.loads(existing["atom_ids"] or "[]")
        merged_tags = list(set(existing_tags) | set(raw_tags))
        merged_atoms = list(set(existing_atoms) | set(atom_ids))

        conn.execute(
            """UPDATE interest_areas SET
               display_name=?, raw_tags=?, atom_count=?, atom_ids=?,
               description=COALESCE(?,description), updated_at=?
               WHERE id=?""",
            [
                display_name,
                json.dumps(merged_tags),
                len(merged_atoms),
                json.dumps(merged_atoms),
                description,
                now,
                ia_id,
            ],
        )
    else:
        ia_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO interest_areas
               (id, canonical_tag, display_name, description, raw_tags,
                atom_count, atom_ids, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                ia_id,
                canonical_tag,
                display_name,
                description,
                json.dumps(raw_tags),
                len(atom_ids),
                json.dumps(atom_ids),
                now,
                now,
            ],
        )

    conn.commit()
    conn.close()
    return ia_id


def get_interest_area_by_raw_tag(raw_tag: str) -> Optional[dict]:
    """Find interest area by raw tag match (case-insensitive)."""
    conn = _conn()
    raw_lower = raw_tag.lower()

    row = conn.execute(
        "SELECT * FROM interest_areas WHERE LOWER(canonical_tag)=?", [raw_lower]
    ).fetchone()
    if row:
        conn.close()
        return _ia_dict(row)

    rows = conn.execute("SELECT * FROM interest_areas").fetchall()
    for row in rows:
        tags = json.loads(row["raw_tags"] or "[]")
        if any(t.lower() == raw_lower for t in tags):
            conn.close()
            return _ia_dict(row)

    conn.close()
    return None


def _ia_dict(row) -> dict:
    d = dict(row)
    for f in ("raw_tags", "atom_ids"):
        try:
            d[f] = json.loads(d[f]) if d[f] else []
        except (json.JSONDecodeError, TypeError):
            d[f] = []
    return d


def list_interest_areas(limit: int = 50) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM interest_areas ORDER BY atom_count DESC LIMIT ?", [limit]
    ).fetchall()
    conn.close()
    return [_ia_dict(r) for r in rows]


# ── Relation CRUD ────────────────────────────────────────────────────────────

def upsert_relation(
    source_entity_id: str,
    target_entity_id: str,
    relation_type: str = "related_to",
    atom_ids: list[str] = None,
    first_seen: str = None,
    last_seen: str = None,
    description: str = None,
) -> str:
    """Insert or update a relation. Weight incremented on update. Returns ID."""
    conn = _conn()
    now = _now()
    atom_ids = atom_ids or []
    first_seen = first_seen or now
    last_seen = last_seen or now

    existing = conn.execute(
        "SELECT * FROM relations WHERE source_entity=? AND target_entity=? AND relation_type=?",
        [source_entity_id, target_entity_id, relation_type],
    ).fetchone()

    if existing:
        rel_id = existing["id"]
        existing_atoms = set(json.loads(existing["atom_ids"] or "[]"))
        new_atoms = set(atom_ids)
        added_atoms = new_atoms - existing_atoms
        merged_atoms = list(existing_atoms | new_atoms)
        # Only increment weight for genuinely new atoms (idempotent on re-run)
        new_weight = existing["weight"] + len(added_atoms)

        conn.execute(
            """UPDATE relations SET weight=?, atom_ids=?, last_seen=?,
               description=COALESCE(?,description), updated_at=?
               WHERE id=?""",
            [new_weight, json.dumps(merged_atoms), max(last_seen, existing["last_seen"]),
             description, now, rel_id],
        )
    else:
        rel_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO relations
               (id, source_entity, target_entity, relation_type, weight, description,
                atom_ids, first_seen, last_seen, created_at, updated_at)
               VALUES (?,?,?,?,1.0,?,?,?,?,?,?)""",
            [
                rel_id, source_entity_id, target_entity_id, relation_type,
                description, json.dumps(atom_ids),
                first_seen, last_seen, now, now,
            ],
        )

    conn.commit()
    conn.close()
    return rel_id


def list_relations(entity_id: str = None, min_weight: float = 1.0) -> list[dict]:
    conn = _conn()
    if entity_id:
        rows = conn.execute(
            """SELECT r.*, e1.canonical_name AS src_name, e2.canonical_name AS tgt_name
               FROM relations r
               JOIN entities e1 ON r.source_entity = e1.id
               JOIN entities e2 ON r.target_entity = e2.id
               WHERE (r.source_entity=? OR r.target_entity=?) AND r.weight>=?
               ORDER BY r.weight DESC""",
            [entity_id, entity_id, min_weight],
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT r.*, e1.canonical_name AS src_name, e2.canonical_name AS tgt_name
               FROM relations r
               JOIN entities e1 ON r.source_entity = e1.id
               JOIN entities e2 ON r.target_entity = e2.id
               WHERE r.weight>=?
               ORDER BY r.weight DESC""",
            [min_weight],
        ).fetchall()
    conn.close()
    result = []
    for row in rows:
        d = dict(row)
        try:
            d["atom_ids"] = json.loads(d["atom_ids"]) if d["atom_ids"] else []
        except (json.JSONDecodeError, TypeError):
            d["atom_ids"] = []
        result.append(d)
    return result


# ── Community CRUD ───────────────────────────────────────────────────────────

def upsert_community(
    label: str,
    entity_ids: list[str],
    interest_area_ids: list[str] = None,
    atom_count: int = 0,
    summary: str = None,
    key_findings: list[dict] = None,
    time_first: str = None,
    time_last: str = None,
    community_id: str = None,
    summary_embedding: np.ndarray = None,
    # Temporal arc fields
    genesis: str = None,
    evolution: str = None,
    current_state: str = None,
    open_threads: list[str] = None,
) -> str:
    """Insert or update a community. Returns ID."""
    conn = _conn()
    # Ensure extra columns exist (idempotent migrations)
    for col_def in [
        "summary_embedding BLOB",
        "genesis TEXT",
        "evolution TEXT",
        "current_state TEXT",
        "open_threads TEXT",
    ]:
        try:
            conn.execute(f"ALTER TABLE communities ADD COLUMN {col_def}")
            conn.commit()
        except Exception:
            pass

    now = _now()
    cid = community_id or str(uuid.uuid4())
    emb_blob = summary_embedding.tobytes() if summary_embedding is not None else None

    existing = conn.execute(
        "SELECT id FROM communities WHERE id=?", [cid]
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE communities SET
               label=?, entity_ids=?, interest_area_ids=?, atom_count=?,
               summary=?, key_findings=?, time_first=?, time_last=?,
               generated_at=?, stale=0,
               summary_embedding=COALESCE(?,summary_embedding),
               genesis=?, evolution=?, current_state=?, open_threads=?
               WHERE id=?""",
            [
                label,
                json.dumps(entity_ids),
                json.dumps(interest_area_ids or []),
                atom_count,
                summary,
                json.dumps(key_findings or []),
                time_first,
                time_last,
                now,
                emb_blob,
                genesis,
                evolution,
                current_state,
                json.dumps(open_threads or []),
                cid,
            ],
        )
    else:
        conn.execute(
            """INSERT INTO communities
               (id, label, entity_ids, interest_area_ids, atom_count,
                summary, key_findings, time_first, time_last, generated_at, stale,
                summary_embedding, genesis, evolution, current_state, open_threads)
               VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
            [
                cid, label,
                json.dumps(entity_ids),
                json.dumps(interest_area_ids or []),
                atom_count, summary,
                json.dumps(key_findings or []),
                time_first, time_last, now, emb_blob,
                genesis, evolution, current_state,
                json.dumps(open_threads or []),
            ],
        )

    conn.commit()
    conn.close()
    return cid


def mark_communities_stale(entity_ids: list[str]):
    """Mark communities containing any of these entities as stale."""
    if not entity_ids:
        return
    conn = _conn()
    rows = conn.execute("SELECT id, entity_ids FROM communities").fetchall()
    for row in rows:
        community_ents = json.loads(row["entity_ids"] or "[]")
        if any(eid in community_ents for eid in entity_ids):
            conn.execute(
                "UPDATE communities SET stale=1 WHERE id=?", [row["id"]]
            )
    conn.commit()
    conn.close()


def list_communities(include_stale: bool = True) -> list[dict]:
    conn = _conn()
    if include_stale:
        rows = conn.execute(
            "SELECT * FROM communities ORDER BY atom_count DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM communities WHERE stale=0 ORDER BY atom_count DESC"
        ).fetchall()
    conn.close()
    return [_community_dict(r) for r in rows]


def get_community(community_id: str) -> Optional[dict]:
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM communities WHERE id=?", [community_id]
    ).fetchone()
    conn.close()
    return _community_dict(row) if row else None


def _community_dict(row) -> dict:
    d = dict(row)
    for f in ("entity_ids", "interest_area_ids", "key_findings"):
        try:
            d[f] = json.loads(d[f]) if d[f] else []
        except (json.JSONDecodeError, TypeError):
            d[f] = []
    # open_threads is a JSON array (may not exist in older rows)
    try:
        d["open_threads"] = json.loads(d["open_threads"]) if d.get("open_threads") else []
    except (json.JSONDecodeError, TypeError):
        d["open_threads"] = []
    d.pop("summary_embedding", None)  # don't expose raw bytes in dicts
    return d


# ── Incremental L2 Update ────────────────────────────────────────────────────

def incremental_update(atom_ids: list[str]) -> dict:
    """
    Process new L1 atoms into L2 state (Phase 1+2: entity resolution + co-occurrence).

    For each new atom:
    - Map its entity strings to canonical entities (alias or embedding match)
    - Map its interest_tags to interest_areas
    - Build co-occurrence relations between resolved entities
    - Mark affected communities as stale

    Returns summary dict with counts and whether community rebuild is needed.
    """
    if not atom_ids:
        return {"entities_updated": 0, "relations_updated": 0, "should_rebuild_communities": False}

    # Load atoms from atoms.db
    if not ATOMS_DB.exists():
        return {"error": "atoms.db not found"}

    # graph.db must exist and have entities populated to do resolution
    if not GRAPH_DB.exists():
        return {"skipped": "graph.db not initialized — run vault graph normalize first"}

    atoms_conn = sqlite3.connect(str(ATOMS_DB))
    atoms_conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(atom_ids))
    atom_rows = atoms_conn.execute(
        f"SELECT * FROM atoms WHERE id IN ({placeholders})", atom_ids
    ).fetchall()
    atoms_conn.close()

    if not atom_rows:
        return {"entities_updated": 0, "relations_updated": 0, "should_rebuild_communities": False}

    entities_updated = 0
    relations_updated = 0
    interest_areas_updated = 0
    affected_entity_ids: set[str] = set()
    unresolved: list[str] = []

    for atom in atom_rows:
        atom_id = atom["id"]
        time_first = atom["time_first"]
        time_last = atom["time_last"]
        raw_entities: list[str] = []
        raw_tags: list[str] = []

        try:
            raw_entities = json.loads(atom["entities"] or "[]")
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            raw_tags = json.loads(atom["interest_tags"] or "[]")
        except (json.JSONDecodeError, TypeError):
            pass

        # --- Resolve entities ---
        resolved_entity_ids: list[str] = []
        for raw in raw_entities:
            if not raw or not raw.strip():
                continue
            # Try exact alias match
            entity = get_entity_by_alias(raw)
            if entity:
                # Update atom_ids and last_seen
                eid = upsert_entity(
                    canonical_name=entity["canonical_name"],
                    entity_type=entity["entity_type"],
                    aliases=entity["aliases"],
                    atom_ids=[atom_id],
                    last_seen=time_last,
                )
                resolved_entity_ids.append(eid)
                affected_entity_ids.add(eid)
                entities_updated += 1
            else:
                # Queue for normalization batch
                unresolved.append(raw)

        # --- Build co-occurrence relations ---
        for i, eid_a in enumerate(resolved_entity_ids):
            for eid_b in resolved_entity_ids[i + 1:]:
                if eid_a == eid_b:
                    continue
                # Always use consistent ordering (lower id first)
                src, tgt = (eid_a, eid_b) if eid_a < eid_b else (eid_b, eid_a)
                upsert_relation(
                    source_entity_id=src,
                    target_entity_id=tgt,
                    relation_type="related_to",
                    atom_ids=[atom_id],
                    first_seen=time_first,
                    last_seen=time_last,
                )
                relations_updated += 1

        # --- Resolve interest tags ---
        if atom["interest_signal"]:
            for raw_tag in raw_tags:
                if not raw_tag:
                    continue
                ia = get_interest_area_by_raw_tag(raw_tag)
                if ia:
                    upsert_interest_area(
                        canonical_tag=ia["canonical_tag"],
                        display_name=ia["display_name"],
                        raw_tags=[raw_tag],
                        atom_ids=[atom_id],
                    )
                    interest_areas_updated += 1
                # Unresolved tags are queued during normalize phase

    # Mark affected communities stale
    if affected_entity_ids:
        mark_communities_stale(list(affected_entity_ids))

    # Update cursor and counter
    now = _now()
    cursor = get_state("last_atom_cursor", "")
    # Use max created_at of processed atoms as new cursor
    atoms_conn2 = sqlite3.connect(str(ATOMS_DB))
    max_created = atoms_conn2.execute(
        f"SELECT MAX(created_at) FROM atoms WHERE id IN ({placeholders})", atom_ids
    ).fetchone()[0]
    atoms_conn2.close()
    if max_created and max_created > cursor:
        set_state("last_atom_cursor", max_created)

    since_rebuild = int(get_state("atoms_since_rebuild", "0")) + len(atom_rows)
    set_state("atoms_since_rebuild", str(since_rebuild))

    # Queue unresolved
    if unresolved:
        existing_queue = json.loads(get_state("unresolved_entities", "[]"))
        merged_queue = list(set(existing_queue) | set(unresolved))
        set_state("unresolved_entities", json.dumps(merged_queue))

    REBUILD_THRESHOLD = 30
    should_rebuild = since_rebuild >= REBUILD_THRESHOLD

    return {
        "atoms_processed": len(atom_rows),
        "entities_updated": entities_updated,
        "relations_updated": relations_updated,
        "interest_areas_updated": interest_areas_updated,
        "unresolved_entities": len(unresolved),
        "atoms_since_rebuild": since_rebuild,
        "should_rebuild_communities": should_rebuild,
    }


# ── Label Propagation ────────────────────────────────────────────────────────

def label_propagation(
    entity_ids: list[str],
    edges: list[tuple[str, str, float]],
    max_iter: int = 20,
    seed: int = 42,
) -> dict[str, int]:
    """
    Label propagation community detection.

    Args:
        entity_ids: list of entity IDs (nodes)
        edges: list of (src_id, tgt_id, weight) tuples
        max_iter: maximum iterations
        seed: random seed for reproducibility

    Returns:
        {entity_id: community_label} mapping
    """
    import random
    random.seed(seed)

    if not entity_ids:
        return {}

    # Initialize: each node is its own community
    labels: dict[str, int] = {e: i for i, e in enumerate(entity_ids)}
    neighbors: dict[str, list[tuple[str, float]]] = {e: [] for e in entity_ids}

    for src, tgt, weight in edges:
        if src in neighbors and tgt in neighbors:
            neighbors[src].append((tgt, weight))
            neighbors[tgt].append((src, weight))

    for iteration in range(max_iter):
        changed = False
        order = list(entity_ids)
        random.shuffle(order)

        for node in order:
            nbrs = neighbors[node]
            if not nbrs:
                continue
            # Weighted vote from neighbors
            votes: dict[int, float] = {}
            for nbr, w in nbrs:
                lbl = labels[nbr]
                votes[lbl] = votes.get(lbl, 0.0) + w
            best = max(votes, key=votes.get)
            if best != labels[node]:
                labels[node] = best
                changed = True

        if not changed:
            break

    return labels


def detect_communities() -> list[dict]:
    """
    Run label propagation on all entities + relations.
    Typed semantic relations (depends_on, part_of, built_with, deployed_on)
    get a weight multiplier to produce tighter, more semantically meaningful clusters.
    Returns list of community dicts: {label, entity_ids, entity_names}
    """
    # Weight multipliers for typed semantic relations.
    # Higher values pull these pairs together more strongly during propagation.
    RELATION_WEIGHT_MULT = {
        "depends_on":    3.0,  # strong structural coupling
        "part_of":       3.0,  # explicit containment
        "built_with":    2.5,  # tool/library pairing
        "deployed_on":   2.5,  # infrastructure coupling
        "configured_by": 2.0,  # management relationship
        "uses":          1.5,  # invocation (moderate boost)
        "replaced_by":   1.5,  # lineage
        "analogous_to":  1.0,  # cross-domain bridge — no pull boost
        "related_to":    1.0,  # co-occurrence baseline
    }

    conn = _conn()
    entities = conn.execute("SELECT id, canonical_name FROM entities").fetchall()
    rels = conn.execute(
        "SELECT source_entity, target_entity, weight, relation_type FROM relations"
    ).fetchall()
    conn.close()

    entity_ids = [r["id"] for r in entities]
    entity_names = {r["id"]: r["canonical_name"] for r in entities}
    edges = [
        (
            r["source_entity"],
            r["target_entity"],
            r["weight"] * RELATION_WEIGHT_MULT.get(r["relation_type"], 1.0),
        )
        for r in rels
    ]

    if not entity_ids:
        return []

    labels = label_propagation(entity_ids, edges)

    # Group by label
    from collections import defaultdict
    groups: dict[int, list[str]] = defaultdict(list)
    for eid, lbl in labels.items():
        groups[lbl].append(eid)

    communities = []
    for lbl, eids in groups.items():
        communities.append({
            "label_id": lbl,
            "entity_ids": eids,
            "entity_names": [entity_names[eid] for eid in eids],
            "size": len(eids),
        })

    # Sort by size descending
    return sorted(communities, key=lambda c: -c["size"])


# ── Stats ────────────────────────────────────────────────────────────────────

def graph_stats() -> dict:
    """Aggregate counts for all L2 tables."""
    if not GRAPH_DB.exists():
        return {"initialized": False}

    conn = _conn()
    entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    entity_by_type = {
        r[0]: r[1] for r in conn.execute(
            "SELECT entity_type, COUNT(*) FROM entities GROUP BY entity_type ORDER BY COUNT(*) DESC"
        ).fetchall()
    }
    relation_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    relation_by_type = {
        r[0]: r[1] for r in conn.execute(
            "SELECT relation_type, COUNT(*) FROM relations GROUP BY relation_type ORDER BY COUNT(*) DESC"
        ).fetchall()
    }
    ia_count = conn.execute("SELECT COUNT(*) FROM interest_areas").fetchone()[0]
    community_count = conn.execute("SELECT COUNT(*) FROM communities").fetchone()[0]
    stale_communities = conn.execute(
        "SELECT COUNT(*) FROM communities WHERE stale=1"
    ).fetchone()[0]
    conn.close()

    state_keys = ["last_atom_cursor", "atoms_since_rebuild", "last_community_rebuild"]
    state = {k: get_state(k) for k in state_keys}
    unresolved_queue = json.loads(get_state("unresolved_entities", "[]"))

    return {
        "initialized": True,
        "entities": entity_count,
        "entity_by_type": entity_by_type,
        "relations": relation_count,
        "relation_by_type": relation_by_type,
        "interest_areas": ia_count,
        "communities": community_count,
        "stale_communities": stale_communities,
        "unresolved_entity_queue": len(unresolved_queue),
        "state": state,
    }


def embed_communities() -> int:
    """Embed all community summaries that don't have embeddings yet. Returns count embedded."""
    conn = _conn()
    # Add summary_embedding column if missing (graceful migration)
    try:
        conn.execute("ALTER TABLE communities ADD COLUMN summary_embedding BLOB")
        conn.commit()
    except Exception:
        pass  # column already exists

    rows = conn.execute(
        "SELECT id, summary FROM communities WHERE summary IS NOT NULL AND summary_embedding IS NULL"
    ).fetchall()
    conn.close()

    if not rows:
        return 0

    model = _get_model()
    summaries = [r["summary"] for r in rows]
    embeddings = model.encode(summaries, normalize_embeddings=True, show_progress_bar=False)

    conn = _conn()
    for row, emb in zip(rows, embeddings):
        conn.execute(
            "UPDATE communities SET summary_embedding=? WHERE id=?",
            [emb.astype(np.float32).tobytes(), row["id"]],
        )
    conn.commit()
    conn.close()
    return len(rows)


def query_communities(query: str, top_k: int = 3) -> list[dict]:
    """Semantic search over community summaries. Returns top_k matches with score."""
    conn = _conn()
    # Ensure column exists
    try:
        conn.execute("ALTER TABLE communities ADD COLUMN summary_embedding BLOB")
        conn.commit()
    except Exception:
        pass

    rows = conn.execute(
        "SELECT id, label, entity_ids, interest_area_ids, summary, key_findings, "
        "atom_count, time_first, time_last, generated_at, stale, summary_embedding, "
        "genesis, evolution, current_state, open_threads "
        "FROM communities WHERE summary IS NOT NULL AND summary_embedding IS NOT NULL"
    ).fetchall()
    conn.close()

    if not rows:
        return []

    model = _get_model()
    q_emb = model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]

    scored = []
    for row in rows:
        emb = np.frombuffer(row["summary_embedding"], dtype=np.float32)
        score = float(np.dot(q_emb, emb))
        scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)

    results = []
    for score, row in scored[:top_k]:
        d = _community_dict(row)
        d["score"] = round(score, 4)
        results.append(d)
    return results


if __name__ == "__main__":
    init_graph_db()
    print(f"graph.db initialized at {GRAPH_DB}")
    print(json.dumps(graph_stats(), indent=2))
    print("graph_store.py OK")
