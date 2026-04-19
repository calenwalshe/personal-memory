"""
atom_store.py — SQLite + FAISS storage layer for L1 atoms.

Atoms are the smallest coherent memory units, produced by chunking L0 events.
Each atom is self-contained with denormalized provenance (no joins needed).

Schema:
  atoms(id, content, atom_type, project, source_events, source_count,
        session_ids, time_first, time_last, duration_s, git_branch, git_sha,
        trigger, tools_used, had_errors, retry_count, files_touched,
        entities, topic, confidence, importance, created_at, invalidated_by)
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
DB_PATH = VAULT / "atoms.db"
FAISS_PATH = VAULT / "atoms.faiss"
EMBED_MODEL = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")
EMBED_DIM = 384

_model: Optional[SentenceTransformer] = None
_index: Optional[faiss.Index] = None
_id_map: list[str] = []


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _model = SentenceTransformer(EMBED_MODEL)
    return _model


def _embed(texts: list[str]) -> np.ndarray:
    return _get_model().encode(texts, normalize_embeddings=True).astype("float32")


def init_db():
    """Create atoms table and indices if they don't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS atoms (
            id              TEXT PRIMARY KEY,
            content         TEXT NOT NULL,
            atom_type       TEXT NOT NULL,

            -- Provenance package (denormalized, self-contained)
            project         TEXT NOT NULL,
            source_events   TEXT NOT NULL,
            source_count    INTEGER NOT NULL,
            session_ids     TEXT NOT NULL,
            time_first      TEXT NOT NULL,
            time_last       TEXT NOT NULL,
            duration_s      REAL,
            git_branch      TEXT,
            git_sha         TEXT,
            trigger         TEXT,
            tools_used      TEXT,
            had_errors      INTEGER DEFAULT 0,
            retry_count     INTEGER DEFAULT 0,
            files_touched   TEXT,

            -- Classification
            entities        TEXT,
            topic           TEXT,
            confidence      REAL DEFAULT 0.7,
            importance      REAL DEFAULT 0.5,

            -- Interest graph (L2 feed)
            interest_signal INTEGER DEFAULT 0,
            interest_tags   TEXT,
            user_intent     TEXT,

            -- Lifecycle
            created_at      TEXT NOT NULL,
            invalidated_by  TEXT DEFAULT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_atoms_project ON atoms(project);
        CREATE INDEX IF NOT EXISTS idx_atoms_type ON atoms(atom_type);
        CREATE INDEX IF NOT EXISTS idx_atoms_time ON atoms(time_first);
        CREATE INDEX IF NOT EXISTS idx_atoms_topic ON atoms(topic);
        CREATE INDEX IF NOT EXISTS idx_atoms_valid ON atoms(invalidated_by);
        CREATE INDEX IF NOT EXISTS idx_atoms_interest ON atoms(interest_signal);
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


def batch_add_atoms(atoms: list[dict], skip_faiss: bool = False) -> list[str]:
    """
    Insert multiple atoms in a single DB transaction + one FAISS write.

    Each dict must have:
      content, atom_type, project, source_events, source_count, session_ids,
      time_first, time_last

    Optional fields default sanely. Returns list of atom IDs.
    Pass skip_faiss=True when caller will rebuild_faiss() at the end.
    """
    if not atoms:
        return []

    init_db()
    ids = [str(uuid.uuid4()) for _ in atoms]
    now = atoms[0].get("created_at", "")

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    for atom_id, a in zip(ids, atoms):
        conn.execute(
            """INSERT INTO atoms
               (id, content, atom_type, project, source_events, source_count,
                session_ids, time_first, time_last, duration_s, git_branch,
                git_sha, trigger, tools_used, had_errors, retry_count,
                files_touched, entities, topic, confidence, importance,
                interest_signal, interest_tags, user_intent,
                created_at, invalidated_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (
                atom_id,
                a["content"],
                a["atom_type"],
                a["project"],
                json.dumps(a["source_events"]),
                a["source_count"],
                json.dumps(a["session_ids"]),
                a["time_first"],
                a["time_last"],
                a.get("duration_s"),
                a.get("git_branch"),
                a.get("git_sha"),
                a.get("trigger"),
                json.dumps(a.get("tools_used", [])),
                1 if a.get("had_errors") else 0,
                a.get("retry_count", 0),
                json.dumps(a.get("files_touched", [])),
                json.dumps(a.get("entities", [])),
                a.get("topic"),
                float(a.get("confidence", 0.7)),
                float(a.get("importance", 0.5)),
                1 if a.get("interest_signal") else 0,
                json.dumps(a.get("interest_tags", [])),
                a.get("user_intent", ""),
                a.get("created_at", now),
            ),
        )

    conn.commit()
    conn.close()

    if not skip_faiss:
        idx, id_map = _load_index()
        contents = [a["content"] for a in atoms]
        vecs = _embed(contents)
        idx.add(vecs)
        id_map.extend(ids)
        _save_index()

    return ids


def query_atoms(
    query: str,
    top_k: int = 5,
    project: str = None,
    atom_type: str = None,
) -> list[dict]:
    """Retrieve top-K atoms by semantic similarity. Excludes invalidated."""
    idx, id_map = _load_index()
    if idx.ntotal == 0:
        return []

    vec = _embed([query])
    k = min(top_k * 3, idx.ntotal)
    scores, indices = idx.search(vec, k)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    results = []

    for score, i in zip(scores[0], indices[0]):
        if i < 0 or i >= len(id_map):
            continue
        atom_id = id_map[i]
        filters = "AND invalidated_by IS NULL"
        params: list = [atom_id]
        if project:
            filters += " AND project=?"
            params.append(project)
        if atom_type:
            filters += " AND atom_type=?"
            params.append(atom_type)
        row = conn.execute(
            f"SELECT * FROM atoms WHERE id=? {filters}", params
        ).fetchone()
        if row:
            d = dict(row)
            d["score"] = float(score)
            # Parse JSON fields for consumer convenience
            for jf in ("source_events", "session_ids", "tools_used",
                        "files_touched", "entities"):
                try:
                    d[jf] = json.loads(d[jf]) if d[jf] else []
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        if len(results) >= top_k:
            break

    conn.close()
    return results


def get_atom(atom_id: str) -> Optional[dict]:
    """Fetch a single atom by ID."""
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM atoms WHERE id=?", [atom_id]).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    for jf in ("source_events", "session_ids", "tools_used",
                "files_touched", "entities"):
        try:
            d[jf] = json.loads(d[jf]) if d[jf] else []
        except (json.JSONDecodeError, TypeError):
            pass
    return d


def list_atoms(
    project: str = None,
    limit: int = 20,
    atom_type: str = None,
) -> list[dict]:
    """List recent atoms, optionally filtered by project/type."""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    where = ["invalidated_by IS NULL"]
    params = []
    if project:
        where.append("project=?")
        params.append(project)
    if atom_type:
        where.append("atom_type=?")
        params.append(atom_type)
    clause = " AND ".join(where)
    rows = conn.execute(
        f"SELECT id, content, atom_type, project, source_count, time_first, "
        f"time_last, topic, confidence, importance, created_at "
        f"FROM atoms WHERE {clause} ORDER BY time_first DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def atom_stats() -> dict:
    """Aggregate counts by project and atom_type."""
    if not DB_PATH.exists():
        return {"total": 0, "by_project": {}, "by_type": {}}
    conn = sqlite3.connect(str(DB_PATH))
    total = conn.execute(
        "SELECT COUNT(*) FROM atoms WHERE invalidated_by IS NULL"
    ).fetchone()[0]
    by_project = {
        r[0]: r[1] for r in conn.execute(
            "SELECT project, COUNT(*) FROM atoms WHERE invalidated_by IS NULL "
            "GROUP BY project ORDER BY COUNT(*) DESC"
        ).fetchall()
    }
    by_type = {
        r[0]: r[1] for r in conn.execute(
            "SELECT atom_type, COUNT(*) FROM atoms WHERE invalidated_by IS NULL "
            "GROUP BY atom_type ORDER BY COUNT(*) DESC"
        ).fetchall()
    }
    conn.close()
    return {"total": total, "by_project": by_project, "by_type": by_type}


def rebuild_faiss() -> int:
    """Rebuild FAISS index from all active atoms in DB."""
    global _index, _id_map
    if not DB_PATH.exists():
        return 0
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT id, content FROM atoms WHERE invalidated_by IS NULL"
    ).fetchall()
    conn.close()

    _index = faiss.IndexFlatIP(EMBED_DIM)
    _id_map = []
    if rows:
        contents = [r[1] for r in rows]
        vecs = _embed(contents)
        _index.add(vecs)
        _id_map = [r[0] for r in rows]
    _save_index()
    return len(rows)


if __name__ == "__main__":
    init_db()
    print(f"atoms.db initialized at {DB_PATH}")
    stats = atom_stats()
    print(f"Stats: {json.dumps(stats, indent=2)}")
    print("atom_store.py OK")
