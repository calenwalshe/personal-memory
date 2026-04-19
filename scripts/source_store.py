"""
source_store.py — SQLite storage layer for L0 universal source intake.

Sources are the entry point for any knowledge into the vault — not just chat
sessions. A source is an original artifact (a document, a note, a Cortex brief,
a chat session). Source segments are chunks/spans/pages within a source.

sources.db is separate from events.db: events.db is live telemetry (tool calls,
sessions, turns); sources.db is deliberate knowledge intake.

Key functions:
  init_sources_db()    — create schema
  create_source()      — register a new source artifact
  create_segment()     — add a segment (span) within a source
  get_source()         — retrieve source by id
  get_segments()       — retrieve segments for a source
  list_sources()       — list sources with optional filters
  source_stats()       — counts and breakdown
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
DB_PATH = VAULT / "sources.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Schema ──────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
    id              TEXT PRIMARY KEY,
    source_type     TEXT NOT NULL,
    title           TEXT,
    raw_content     TEXT,
    file_path       TEXT,
    actor           TEXT NOT NULL DEFAULT 'user',
    project         TEXT,
    source_time     TEXT,
    captured_at     TEXT NOT NULL,
    capture_quality TEXT DEFAULT 'complete',
    metadata        TEXT DEFAULT '{}',
    session_id      TEXT
);

CREATE INDEX IF NOT EXISTS idx_src_type ON sources(source_type);
CREATE INDEX IF NOT EXISTS idx_src_project ON sources(project);
CREATE INDEX IF NOT EXISTS idx_src_time ON sources(captured_at);

CREATE TABLE IF NOT EXISTS source_segments (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES sources(id),
    segment_type    TEXT NOT NULL,
    ordinal         INTEGER NOT NULL,
    content         TEXT NOT NULL,
    char_start      INTEGER,
    char_end        INTEGER,
    metadata        TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_seg_source ON source_segments(source_id);
CREATE INDEX IF NOT EXISTS idx_seg_ordinal ON source_segments(source_id, ordinal);

CREATE TABLE IF NOT EXISTS source_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Valid source types
SOURCE_TYPES = {
    "chat", "doc", "note", "fact_dump",
    "cortex_brief", "code_log", "research_doc",
}

# Valid segment types
SEGMENT_TYPES = {
    "turn_range", "page", "paragraph",
    "section", "chunk", "line_range",
}


# ── Init ────────────────────────────────────────────────────────────────────

def init_sources_db() -> sqlite3.Connection:
    """Create sources.db with schema. Returns open connection."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        return init_sources_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Source CRUD ─────────────────────────────────────────────────────────────

def create_source(
    source_type: str,
    title: Optional[str] = None,
    raw_content: Optional[str] = None,
    file_path: Optional[str] = None,
    actor: str = "user",
    project: Optional[str] = None,
    source_time: Optional[str] = None,
    capture_quality: str = "complete",
    metadata: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> str:
    """Create a new source record. Returns source_id."""
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"Invalid source_type '{source_type}'. Valid: {SOURCE_TYPES}")

    source_id = str(uuid.uuid4())
    now = _now()

    conn = _conn()
    conn.execute(
        """INSERT INTO sources
           (id, source_type, title, raw_content, file_path, actor, project,
            source_time, captured_at, capture_quality, metadata, session_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            source_id, source_type, title, raw_content, file_path, actor,
            project, source_time, now, capture_quality,
            json.dumps(metadata or {}), session_id,
        ),
    )
    conn.commit()
    conn.close()
    return source_id


def get_source(source_id: str) -> Optional[dict]:
    """Retrieve a source by id."""
    conn = _conn()
    row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def list_sources(
    source_type: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """List sources with optional filters."""
    conn = _conn()
    query = "SELECT * FROM sources WHERE 1=1"
    params: list = []

    if source_type:
        query += " AND source_type=?"
        params.append(source_type)
    if project:
        query += " AND project=?"
        params.append(project)

    query += " ORDER BY captured_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Segment CRUD ────────────────────────────────────────────────────────────

def create_segment(
    source_id: str,
    segment_type: str,
    ordinal: int,
    content: str,
    char_start: Optional[int] = None,
    char_end: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> str:
    """Create a segment within a source. Returns segment_id."""
    if segment_type not in SEGMENT_TYPES:
        raise ValueError(f"Invalid segment_type '{segment_type}'. Valid: {SEGMENT_TYPES}")

    segment_id = str(uuid.uuid4())
    now = _now()

    conn = _conn()
    conn.execute(
        """INSERT INTO source_segments
           (id, source_id, segment_type, ordinal, content,
            char_start, char_end, metadata, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            segment_id, source_id, segment_type, ordinal, content,
            char_start, char_end, json.dumps(metadata or {}), now,
        ),
    )
    conn.commit()
    conn.close()
    return segment_id


def create_segments_batch(
    source_id: str,
    segments: list[dict],
) -> list[str]:
    """Create multiple segments in a single transaction. Returns segment_ids.

    Each dict: {segment_type, ordinal, content, char_start?, char_end?, metadata?}
    """
    if not segments:
        return []

    ids = []
    now = _now()
    conn = _conn()

    for seg in segments:
        seg_id = str(uuid.uuid4())
        ids.append(seg_id)
        conn.execute(
            """INSERT INTO source_segments
               (id, source_id, segment_type, ordinal, content,
                char_start, char_end, metadata, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                seg_id, source_id, seg["segment_type"], seg["ordinal"],
                seg["content"], seg.get("char_start"), seg.get("char_end"),
                json.dumps(seg.get("metadata", {})), now,
            ),
        )

    conn.commit()
    conn.close()
    return ids


def get_segments(
    source_id: str,
    segment_type: Optional[str] = None,
) -> list[dict]:
    """Get all segments for a source, ordered by ordinal."""
    conn = _conn()
    query = "SELECT * FROM source_segments WHERE source_id=?"
    params: list = [source_id]

    if segment_type:
        query += " AND segment_type=?"
        params.append(segment_type)

    query += " ORDER BY ordinal"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_segment(segment_id: str) -> Optional[dict]:
    """Retrieve a single segment by id."""
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM source_segments WHERE id=?", (segment_id,)
    ).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


# ── State helpers ───────────────────────────────────────────────────────────

def get_state(key: str, default: str = "") -> str:
    conn = _conn()
    row = conn.execute(
        "SELECT value FROM source_state WHERE key=?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_state(key: str, value: str):
    conn = _conn()
    conn.execute(
        """INSERT INTO source_state (key, value, updated_at) VALUES (?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, _now()),
    )
    conn.commit()
    conn.close()


# ── Stats ───────────────────────────────────────────────────────────────────

def source_stats() -> dict:
    """Counts and breakdown of sources and segments."""
    conn = _conn()

    total = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]

    by_type = conn.execute(
        "SELECT source_type, COUNT(*) as n FROM sources GROUP BY source_type ORDER BY n DESC"
    ).fetchall()

    by_project = conn.execute(
        "SELECT project, COUNT(*) as n FROM sources GROUP BY project ORDER BY n DESC LIMIT 10"
    ).fetchall()

    total_segments = conn.execute(
        "SELECT COUNT(*) FROM source_segments"
    ).fetchone()[0]

    conn.close()

    return {
        "total_sources": total,
        "total_segments": total_segments,
        "by_type": {r["source_type"]: r["n"] for r in by_type},
        "by_project": {r["project"]: r["n"] for r in by_project},
    }


if __name__ == "__main__":
    init_sources_db()
    print(f"sources.db initialized at {DB_PATH}")
    stats = source_stats()
    print(f"  Sources: {stats['total_sources']}")
    print(f"  Segments: {stats['total_segments']}")
