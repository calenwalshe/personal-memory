"""
fact_store.py — SQLite + FAISS storage layer for atomic facts.

Schema:
  facts(id, content, topic, entities, confidence, importance, scope,
        valid_from, invalidated_by, session_id, turn_range)
  contradiction_review(id, new_fact_id, old_fact_id, reason, status)
"""

import json
import logging
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

# Suppress noisy sentence-transformers / transformers / HF Hub output
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")
os.environ.setdefault("TQDM_DISABLE", "1")
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
DB_PATH = VAULT / "facts.db"
FAISS_PATH = VAULT / "facts.faiss"
EMBED_MODEL = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")
EMBED_DIM = 384
AUTO_INVALIDATE_THRESHOLD = 0.85

_model: Optional[SentenceTransformer] = None
_index: Optional[faiss.Index] = None
_id_map: list[str] = []  # faiss row → fact_id


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _model = SentenceTransformer(EMBED_MODEL)
    return _model


def _embed(text: str) -> np.ndarray:
    vec = _get_model().encode([text], normalize_embeddings=True)
    return vec.astype("float32")


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS facts (
            id              TEXT PRIMARY KEY,
            content         TEXT NOT NULL,
            topic           TEXT,
            entities        TEXT,
            confidence      REAL,
            importance      REAL,
            scope           TEXT,
            valid_from      TEXT NOT NULL,
            invalidated_by  TEXT DEFAULT NULL,
            session_id      TEXT NOT NULL,
            turn_range      TEXT
        );
        CREATE TABLE IF NOT EXISTS contradiction_review (
            id              TEXT PRIMARY KEY,
            new_fact_id     TEXT,
            old_fact_id     TEXT,
            reason          TEXT,
            status          TEXT DEFAULT 'pending'
        );
        CREATE INDEX IF NOT EXISTS idx_facts_topic ON facts(topic);
        CREATE INDEX IF NOT EXISTS idx_facts_session ON facts(session_id);
        CREATE INDEX IF NOT EXISTS idx_facts_valid ON facts(invalidated_by);
    """)
    conn.commit()
    conn.close()


def _load_index() -> tuple[faiss.Index, list[str]]:
    global _index, _id_map
    if _index is not None:
        return _index, _id_map
    if FAISS_PATH.exists():
        _index = faiss.read_index(str(FAISS_PATH))
        map_path = FAISS_PATH.with_suffix(".map.json")
        _id_map = json.loads(map_path.read_text()) if map_path.exists() else []
    else:
        _index = faiss.IndexFlatIP(EMBED_DIM)
        _id_map = []
    return _index, _id_map


def _save_index():
    idx, id_map = _load_index()
    faiss.write_index(idx, str(FAISS_PATH))
    FAISS_PATH.with_suffix(".map.json").write_text(json.dumps(id_map))


def _resolve_entity(
    conn: sqlite3.Connection,
    name: str,
    entity_type: str = None,
    project_scope: str = None,
    today: str = None,
) -> str:
    """Return entity_id for canonical_name, creating if absent. MVP: exact-match + lowercase."""
    canonical = name.strip().lower()
    scope_val = project_scope or ""
    row = conn.execute(
        "SELECT id FROM entities WHERE canonical_name=? AND COALESCE(project_scope,'')=?",
        (canonical, scope_val),
    ).fetchone()
    if row:
        return row[0]
    entity_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO entities (id, canonical_name, entity_type, aliases, first_seen, project_scope)"
        " VALUES (?,?,?,?,?,?)",
        (entity_id, canonical, entity_type, json.dumps([name]), today or "", project_scope),
    )
    return entity_id


def _insert_fact_row(conn: sqlite3.Connection, fact_id: str, content: str,
                     session_id: str, valid_from: str, topic, entities, confidence,
                     importance, scope, turn_range, memory_type, project_scope,
                     event_time, ingestion_time):
    """Write one fact row + entity links into an open connection. Does not commit."""
    conn.execute(
        """INSERT INTO facts
           (id, content, topic, entities, confidence, importance, scope,
            valid_from, invalidated_by, session_id, turn_range,
            memory_type, project_scope, event_time, ingestion_time)
           VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?)""",
        (
            fact_id, content, topic,
            json.dumps(entities or []),
            confidence, importance, scope,
            valid_from, session_id,
            json.dumps(turn_range or []),
            memory_type, project_scope, event_time, ingestion_time,
        ),
    )
    for name in (entities or []):
        if name and name.strip():
            entity_id = _resolve_entity(conn, name, project_scope=project_scope, today=valid_from)
            conn.execute(
                "INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?,?)",
                (fact_id, entity_id),
            )


def add_fact(
    content: str,
    session_id: str,
    valid_from: str,
    topic: str = None,
    entities: list[str] = None,
    confidence: float = 0.7,
    importance: float = 0.5,
    scope: str = "learning",
    turn_range: list[int] = None,
    memory_type: str = "semantic",
    project_scope: str = None,
    event_time: str = None,
    ingestion_time: str = None,
) -> str:
    """Add a single fact. Returns the new fact_id. Use batch_add_facts() for bulk ingestion."""
    fact_id = str(uuid.uuid4())
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    _insert_fact_row(conn, fact_id, content, session_id, valid_from, topic, entities,
                     confidence, importance, scope, turn_range, memory_type,
                     project_scope, event_time, ingestion_time)
    conn.commit()
    conn.close()

    # Incremental FAISS update — one disk write per call
    idx, id_map = _load_index()
    vec = _embed(content)
    idx.add(vec)
    id_map.append(fact_id)
    _save_index()

    return fact_id


def batch_add_facts(facts: list[dict], skip_faiss: bool = False) -> list[str]:
    """
    Insert multiple facts in a single DB transaction + one FAISS write.
    Each dict has the same kwargs as add_fact().
    Returns list of new fact_ids in the same order.

    Pass skip_faiss=True when the caller will run rebuild_faiss() at the end
    (e.g. vault worker), to avoid stale in-process index races.
    """
    if not facts:
        return []

    ids = [str(uuid.uuid4()) for _ in facts]

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    for fact_id, f in zip(ids, facts):
        _insert_fact_row(
            conn, fact_id,
            f["content"], f["session_id"], f["valid_from"],
            f.get("topic"), f.get("entities"),
            float(f.get("confidence", 0.7)), float(f.get("importance", 0.5)),
            f.get("scope", "learning"), f.get("turn_range"),
            f.get("memory_type", "semantic"), f.get("project_scope"),
            f.get("event_time"), f.get("ingestion_time"),
        )
    conn.commit()
    conn.close()

    if not skip_faiss:
        # One FAISS write for the entire batch
        idx, id_map = _load_index()
        contents = [f["content"] for f in facts]
        vecs = _get_model().encode(contents, normalize_embeddings=True).astype("float32")
        idx.add(vecs)
        id_map.extend(ids)
        _save_index()

    return ids


def invalidate_fact(fact_id: str, invalidated_by: str):
    """Mark a fact as superseded by another fact_id."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE facts SET invalidated_by=? WHERE id=?",
        (invalidated_by, fact_id),
    )
    conn.commit()
    conn.close()


def queue_contradiction_review(new_fact_id: str, old_fact_id: str, reason: str):
    """Queue an ambiguous contradiction for human review."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO contradiction_review (id, new_fact_id, old_fact_id, reason) VALUES (?,?,?,?)",
        (str(uuid.uuid4()), new_fact_id, old_fact_id, reason),
    )
    conn.commit()
    conn.close()


def query_facts(
    query: str,
    top_k: int = 5,
    topic: str = None,
    memory_type: str = None,
    project_scope: str = None,
) -> list[dict]:
    """Retrieve top-K facts by semantic similarity. Excludes invalidated facts."""
    idx, id_map = _load_index()
    if idx.ntotal == 0:
        return []

    vec = _embed(query)
    k = min(top_k * 3, idx.ntotal)  # over-fetch to account for invalidated filtering
    scores, indices = idx.search(vec, k)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    results = []
    for score, i in zip(scores[0], indices[0]):
        if i < 0 or i >= len(id_map):
            continue
        fact_id = id_map[i]
        filters = "AND invalidated_by IS NULL"
        params: list = [fact_id]
        if memory_type:
            filters += " AND memory_type=?"
            params.append(memory_type)
        if project_scope:
            filters += " AND project_scope=?"
            params.append(project_scope)
        row = conn.execute(
            f"SELECT * FROM facts WHERE id=? {filters}",
            params,
        ).fetchone()
        if row:
            results.append({**dict(row), "score": float(score)})
        if len(results) >= top_k:
            break
    conn.close()
    return results


def fact_exists(session_id: str, turn_range: list[int]) -> bool:
    """Check if a fact from this exact window already exists (idempotency)."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id FROM facts WHERE session_id=? AND turn_range=?",
        (session_id, json.dumps(turn_range)),
    ).fetchone()
    conn.close()
    return row is not None


def get_facts_by_entities(entities: list[str]) -> list[dict]:
    """Get all active facts that share entities with the given list."""
    if not entities:
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Simple approach: load all active facts and filter by entity overlap
    rows = conn.execute(
        "SELECT * FROM facts WHERE invalidated_by IS NULL"
    ).fetchall()
    conn.close()
    results = []
    for row in rows:
        row_entities = set(json.loads(row["entities"] or "[]"))
        if row_entities & set(entities):
            results.append(dict(row))
    return results


def pending_review_count() -> int:
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute(
        "SELECT COUNT(*) FROM contradiction_review WHERE status='pending'"
    ).fetchone()[0]
    conn.close()
    return n


def rebuild_faiss():
    """Rebuild FAISS index from all active facts in DB."""
    global _index, _id_map
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, content FROM facts WHERE invalidated_by IS NULL"
    ).fetchall()
    conn.close()

    _index = faiss.IndexFlatIP(EMBED_DIM)
    _id_map = []
    if rows:
        contents = [r[1] for r in rows]
        vecs = _get_model().encode(contents, normalize_embeddings=True).astype("float32")
        _index.add(vecs)
        _id_map = [r[0] for r in rows]
    _save_index()
    return len(rows)


if __name__ == "__main__":
    # Quick unit test
    init_db()
    print("DB initialized")

    fid = add_fact(
        content="FAISS IndexFlatIP requires L2-normalized vectors",
        session_id="test-session",
        valid_from="2026-04-12",
        topic="faiss",
        entities=["FAISS", "IndexFlatIP", "normalization"],
        confidence=0.9,
    )
    print(f"Added fact: {fid}")

    results = query_facts("FAISS vector search normalization")
    print(f"Query results: {len(results)}")
    for r in results:
        print(f"  [{r['score']:.3f}] {r['content']}")

    print(f"Pending review: {pending_review_count()}")
    print("fact_store.py OK")
