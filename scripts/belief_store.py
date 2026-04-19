"""
belief_store.py — SQLite storage layer for L3 belief runtime engine.

beliefs.db tracks what the system believes, why, and how confident it is.
Sits above L1 evidence units (atoms.db) and L2 entity graph (graph.db).

Tables:
  logical_forms    — typed claims extracted from L1 evidence units
  worlds           — Kripke-inspired contexts (current, past, planned, etc.)
  form_status      — which claim lives in which world, with what status
  derived_objects   — output of inference rules (stable beliefs, contradictions, etc.)
  inference_log    — audit trail of every rule firing
  l3_state         — processing cursors and module config

Key functions:
  init_beliefs_db()       — create schema + seed worlds
  add_form()              — insert a logical form
  set_form_status()       — place a form in a world with a status
  add_derived()           — insert a derived object
  log_inference()         — record a rule firing
  get_current_beliefs()   — all forms with world=current, status=active|stable
  get_contradictions()    — all derived objects of type=contradiction
  get_derived()           — derived objects with optional filters
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
DB_PATH = VAULT / "beliefs.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Schema ──────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS logical_forms (
    id              TEXT PRIMARY KEY,
    form_type       TEXT NOT NULL,
    content         TEXT NOT NULL,
    subject         TEXT,
    predicate       TEXT,
    object          TEXT,

    source_unit_id  TEXT,
    source_unit_ids TEXT DEFAULT '[]',
    entity_ids      TEXT DEFAULT '[]',
    project         TEXT,
    confidence      REAL DEFAULT 0.7,

    extracted_at    TEXT NOT NULL,
    extraction_run  TEXT,
    superseded_by   TEXT,

    embedding       BLOB
);

CREATE INDEX IF NOT EXISTS idx_lf_type ON logical_forms(form_type);
CREATE INDEX IF NOT EXISTS idx_lf_subject ON logical_forms(subject);
CREATE INDEX IF NOT EXISTS idx_lf_project ON logical_forms(project);
CREATE INDEX IF NOT EXISTS idx_lf_superseded ON logical_forms(superseded_by);

CREATE TABLE IF NOT EXISTS worlds (
    id              TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    description     TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS form_status (
    id              TEXT PRIMARY KEY,
    form_id         TEXT NOT NULL REFERENCES logical_forms(id),
    world_id        TEXT NOT NULL REFERENCES worlds(id),
    status          TEXT NOT NULL,
    confidence      REAL DEFAULT 0.7,

    valid_from      TEXT NOT NULL,
    valid_until     TEXT,

    set_by          TEXT NOT NULL,
    reason          TEXT,

    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,

    UNIQUE(form_id, world_id)
);

CREATE INDEX IF NOT EXISTS idx_fs_form ON form_status(form_id);
CREATE INDEX IF NOT EXISTS idx_fs_world ON form_status(world_id);
CREATE INDEX IF NOT EXISTS idx_fs_status ON form_status(status);

CREATE TABLE IF NOT EXISTS derived_objects (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    namespace       TEXT NOT NULL DEFAULT 'personal',
    content         TEXT NOT NULL,

    source_form_ids TEXT NOT NULL DEFAULT '[]',
    rule_fired      TEXT NOT NULL,
    confidence      REAL DEFAULT 0.7,

    created_at      TEXT NOT NULL,
    invalidated_at  TEXT,
    invalidated_by  TEXT
);

CREATE INDEX IF NOT EXISTS idx_do_type ON derived_objects(type);
CREATE INDEX IF NOT EXISTS idx_do_ns ON derived_objects(namespace);
CREATE INDEX IF NOT EXISTS idx_do_valid ON derived_objects(invalidated_at);

CREATE TABLE IF NOT EXISTS inference_log (
    id              TEXT PRIMARY KEY,
    rule_name       TEXT NOT NULL,
    module          TEXT NOT NULL,
    input_form_ids  TEXT NOT NULL,
    output_id       TEXT,
    action          TEXT NOT NULL,
    detail          TEXT,
    fired_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_il_rule ON inference_log(rule_name);
CREATE INDEX IF NOT EXISTS idx_il_module ON inference_log(module);

CREATE TABLE IF NOT EXISTS l3_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Canonical Kripke worlds
_SEED_WORLDS = [
    ("current",       "Current",       "What the system currently accepts as true"),
    ("past",          "Past",          "What was true but no longer is"),
    ("planned",       "Planned",       "Intended future state"),
    ("possible",      "Possible",      "Plausible but not confirmed"),
    ("contested",     "Contested",     "Evidence disagrees"),
    ("rejected",      "Rejected",      "Explicitly determined false"),
    ("user_belief",   "User Belief",   "What the user has stated they believe"),
    ("system_belief", "System Belief", "What the system infers independently"),
]

# Valid form types
FORM_TYPES = {
    "claim", "event", "decision", "plan",
    "preference", "warning", "question", "rule",
}

# Valid statuses
STATUSES = {
    "active", "superseded", "contradicted",
    "stable", "decayed", "unknown",
}

# Valid derived object types
DERIVED_TYPES = {
    "stable_belief", "contradiction", "lesson",
    "open_thread", "preference_shift", "design_rule",
    "research_hypothesis", "task", "question",
    "bridge", "profile_update",
}


# ── Init ────────────────────────────────────────────────────────────────────

def init_beliefs_db() -> sqlite3.Connection:
    """Create beliefs.db with schema and seed worlds."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)

    # Seed worlds
    now = _now()
    for wid, label, desc in _SEED_WORLDS:
        conn.execute(
            "INSERT OR IGNORE INTO worlds (id, label, description, created_at) VALUES (?,?,?,?)",
            (wid, label, desc, now),
        )
    conn.commit()
    return conn


def _conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        return init_beliefs_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Logical Form CRUD ──────────────────────────────────────────────────────

def add_form(
    form_type: str,
    content: str,
    subject: str = None,
    predicate: str = None,
    object_: str = None,
    source_unit_id: str = None,
    source_unit_ids: list[str] = None,
    entity_ids: list[str] = None,
    project: str = None,
    confidence: float = 0.7,
    extraction_run: str = None,
) -> str:
    """Insert a logical form. Returns form_id."""
    if form_type not in FORM_TYPES:
        raise ValueError(f"Invalid form_type '{form_type}'. Valid: {FORM_TYPES}")

    form_id = str(uuid.uuid4())
    now = _now()

    conn = _conn()
    conn.execute(
        """INSERT INTO logical_forms
           (id, form_type, content, subject, predicate, object,
            source_unit_id, source_unit_ids, entity_ids, project,
            confidence, extracted_at, extraction_run)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            form_id, form_type, content, subject, predicate, object_,
            source_unit_id, json.dumps(source_unit_ids or []),
            json.dumps(entity_ids or []), project, confidence,
            now, extraction_run,
        ),
    )
    conn.commit()
    conn.close()
    return form_id


def add_forms_batch(forms: list[dict], extraction_run: str = None) -> list[str]:
    """Insert multiple logical forms in one transaction."""
    if not forms:
        return []

    ids = []
    now = _now()
    conn = _conn()

    for f in forms:
        form_id = str(uuid.uuid4())
        ids.append(form_id)
        conn.execute(
            """INSERT INTO logical_forms
               (id, form_type, content, subject, predicate, object,
                source_unit_id, source_unit_ids, entity_ids, project,
                confidence, extracted_at, extraction_run)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                form_id, f["form_type"], f["content"],
                f.get("subject"), f.get("predicate"), f.get("object"),
                f.get("source_unit_id"),
                json.dumps(f.get("source_unit_ids", [])),
                json.dumps(f.get("entity_ids", [])),
                f.get("project"), f.get("confidence", 0.7),
                now, extraction_run,
            ),
        )

    conn.commit()
    conn.close()
    return ids


def get_form(form_id: str) -> Optional[dict]:
    conn = _conn()
    row = conn.execute("SELECT * FROM logical_forms WHERE id=?", (form_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_forms(
    form_type: str = None,
    project: str = None,
    subject: str = None,
    limit: int = 100,
) -> list[dict]:
    """Query logical forms with optional filters."""
    conn = _conn()
    query = "SELECT * FROM logical_forms WHERE superseded_by IS NULL"
    params: list = []

    if form_type:
        query += " AND form_type=?"
        params.append(form_type)
    if project:
        query += " AND project=?"
        params.append(project)
    if subject:
        query += " AND subject=?"
        params.append(subject)

    query += " ORDER BY extracted_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def supersede_form(old_form_id: str, new_form_id: str):
    """Mark a form as superseded by another."""
    conn = _conn()
    conn.execute(
        "UPDATE logical_forms SET superseded_by=? WHERE id=?",
        (new_form_id, old_form_id),
    )
    conn.commit()
    conn.close()


# ── Form Status CRUD ──────────────────────────────────────────────────────

def set_form_status(
    form_id: str,
    world_id: str,
    status: str,
    confidence: float = 0.7,
    set_by: str = "extractor",
    reason: str = None,
) -> str:
    """Place a form in a world with a status. Upserts on (form_id, world_id)."""
    if status not in STATUSES:
        raise ValueError(f"Invalid status '{status}'. Valid: {STATUSES}")

    status_id = str(uuid.uuid4())
    now = _now()
    conn = _conn()

    # Check if exists
    existing = conn.execute(
        "SELECT id FROM form_status WHERE form_id=? AND world_id=?",
        (form_id, world_id),
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE form_status SET status=?, confidence=?, set_by=?,
               reason=?, updated_at=?, valid_until=NULL
               WHERE form_id=? AND world_id=?""",
            (status, confidence, set_by, reason, now, form_id, world_id),
        )
        status_id = existing["id"]
    else:
        conn.execute(
            """INSERT INTO form_status
               (id, form_id, world_id, status, confidence, valid_from,
                set_by, reason, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (status_id, form_id, world_id, status, confidence,
             now, set_by, reason, now, now),
        )

    conn.commit()
    conn.close()
    return status_id


def expire_form_status(form_id: str, world_id: str, reason: str = None):
    """Set valid_until on a form's status in a world."""
    now = _now()
    conn = _conn()
    conn.execute(
        """UPDATE form_status SET valid_until=?, reason=COALESCE(?, reason),
           updated_at=? WHERE form_id=? AND world_id=? AND valid_until IS NULL""",
        (now, reason, now, form_id, world_id),
    )
    conn.commit()
    conn.close()


def get_form_statuses(form_id: str) -> list[dict]:
    """Get all world statuses for a form."""
    conn = _conn()
    rows = conn.execute(
        """SELECT fs.*, w.label as world_label
           FROM form_status fs JOIN worlds w ON fs.world_id = w.id
           WHERE fs.form_id=? ORDER BY w.id""",
        (form_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_forms_in_world(
    world_id: str,
    status: str = None,
    active_only: bool = True,
) -> list[dict]:
    """Get all forms in a given world, optionally filtered by status."""
    conn = _conn()
    query = """SELECT lf.*, fs.status, fs.confidence as status_confidence,
                      fs.valid_from, fs.valid_until, fs.set_by, fs.reason
               FROM logical_forms lf
               JOIN form_status fs ON lf.id = fs.form_id
               WHERE fs.world_id=?"""
    params: list = [world_id]

    if active_only:
        query += " AND fs.valid_until IS NULL"
    if status:
        query += " AND fs.status=?"
        params.append(status)

    query += " ORDER BY lf.extracted_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Derived Objects CRUD ───────────────────────────────────────────────────

def add_derived(
    type_: str,
    content: str,
    source_form_ids: list[str],
    rule_fired: str,
    namespace: str = "personal",
    confidence: float = 0.7,
) -> str:
    """Insert a derived object. Returns derived_id."""
    derived_id = str(uuid.uuid4())
    now = _now()

    conn = _conn()
    conn.execute(
        """INSERT INTO derived_objects
           (id, type, namespace, content, source_form_ids, rule_fired,
            confidence, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            derived_id, type_, namespace, content,
            json.dumps(source_form_ids), rule_fired,
            confidence, now,
        ),
    )
    conn.commit()
    conn.close()
    return derived_id


def invalidate_derived(derived_id: str, invalidated_by: str = None):
    """Mark a derived object as no longer valid."""
    now = _now()
    conn = _conn()
    conn.execute(
        "UPDATE derived_objects SET invalidated_at=?, invalidated_by=? WHERE id=?",
        (now, invalidated_by, derived_id),
    )
    conn.commit()
    conn.close()


def get_derived(
    type_: str = None,
    namespace: str = None,
    active_only: bool = True,
    limit: int = 100,
) -> list[dict]:
    """Query derived objects with optional filters."""
    conn = _conn()
    query = "SELECT * FROM derived_objects WHERE 1=1"
    params: list = []

    if active_only:
        query += " AND invalidated_at IS NULL"
    if type_:
        query += " AND type=?"
        params.append(type_)
    if namespace:
        query += " AND namespace=?"
        params.append(namespace)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_contradictions(namespace: str = None) -> list[dict]:
    """Shortcut: get all active contradictions."""
    return get_derived(type_="contradiction", namespace=namespace)


# ── Inference Log ──────────────────────────────────────────────────────────

def log_inference(
    rule_name: str,
    module: str,
    input_form_ids: list[str],
    output_id: str = None,
    action: str = "created",
    detail: str = None,
) -> str:
    """Record an inference rule firing."""
    log_id = str(uuid.uuid4())
    now = _now()

    conn = _conn()
    conn.execute(
        """INSERT INTO inference_log
           (id, rule_name, module, input_form_ids, output_id, action, detail, fired_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (log_id, rule_name, module, json.dumps(input_form_ids),
         output_id, action, detail, now),
    )
    conn.commit()
    conn.close()
    return log_id


def get_inference_log(
    rule_name: str = None,
    module: str = None,
    limit: int = 50,
) -> list[dict]:
    conn = _conn()
    query = "SELECT * FROM inference_log WHERE 1=1"
    params: list = []

    if rule_name:
        query += " AND rule_name=?"
        params.append(rule_name)
    if module:
        query += " AND module=?"
        params.append(module)

    query += " ORDER BY fired_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── State helpers ──────────────────────────────────────────────────────────

def get_state(key: str, default: str = "") -> str:
    conn = _conn()
    row = conn.execute("SELECT value FROM l3_state WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_state(key: str, value: str):
    conn = _conn()
    conn.execute(
        """INSERT INTO l3_state (key, value, updated_at) VALUES (?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, _now()),
    )
    conn.commit()
    conn.close()


# ── Stats ──────────────────────────────────────────────────────────────────

def belief_stats() -> dict:
    conn = _conn()

    total_forms = conn.execute("SELECT COUNT(*) FROM logical_forms").fetchone()[0]
    active_forms = conn.execute(
        "SELECT COUNT(*) FROM logical_forms WHERE superseded_by IS NULL"
    ).fetchone()[0]

    by_type = conn.execute(
        "SELECT form_type, COUNT(*) as n FROM logical_forms WHERE superseded_by IS NULL GROUP BY form_type ORDER BY n DESC"
    ).fetchall()

    total_statuses = conn.execute("SELECT COUNT(*) FROM form_status").fetchone()[0]
    active_statuses = conn.execute(
        "SELECT COUNT(*) FROM form_status WHERE valid_until IS NULL"
    ).fetchone()[0]

    by_world = conn.execute(
        """SELECT w.label, COUNT(*) as n FROM form_status fs
           JOIN worlds w ON fs.world_id = w.id
           WHERE fs.valid_until IS NULL
           GROUP BY w.label ORDER BY n DESC"""
    ).fetchall()

    by_status = conn.execute(
        "SELECT status, COUNT(*) as n FROM form_status WHERE valid_until IS NULL GROUP BY status ORDER BY n DESC"
    ).fetchall()

    total_derived = conn.execute("SELECT COUNT(*) FROM derived_objects").fetchone()[0]
    active_derived = conn.execute(
        "SELECT COUNT(*) FROM derived_objects WHERE invalidated_at IS NULL"
    ).fetchone()[0]

    by_derived_type = conn.execute(
        "SELECT type, COUNT(*) as n FROM derived_objects WHERE invalidated_at IS NULL GROUP BY type ORDER BY n DESC"
    ).fetchall()

    total_inferences = conn.execute("SELECT COUNT(*) FROM inference_log").fetchone()[0]

    worlds = conn.execute("SELECT COUNT(*) FROM worlds").fetchone()[0]

    conn.close()

    return {
        "logical_forms": {"total": total_forms, "active": active_forms,
                          "by_type": {r[0]: r[1] for r in by_type}},
        "form_status": {"total": total_statuses, "active": active_statuses,
                        "by_world": {r[0]: r[1] for r in by_world},
                        "by_status": {r[0]: r[1] for r in by_status}},
        "derived_objects": {"total": total_derived, "active": active_derived,
                           "by_type": {r[0]: r[1] for r in by_derived_type}},
        "inference_log": {"total": total_inferences},
        "worlds": worlds,
    }


# ── Convenience ────────────────────────────────────────────────────────────

def get_current_beliefs(project: str = None, limit: int = 100) -> list[dict]:
    """Get all forms currently believed to be true (world=current, status active|stable)."""
    conn = _conn()
    query = """SELECT lf.*, fs.status, fs.confidence as status_confidence,
                      fs.valid_from, fs.set_by
               FROM logical_forms lf
               JOIN form_status fs ON lf.id = fs.form_id
               WHERE fs.world_id='current'
                 AND fs.status IN ('active', 'stable')
                 AND fs.valid_until IS NULL
                 AND lf.superseded_by IS NULL"""
    params: list = []

    if project:
        query += " AND lf.project=?"
        params.append(project)

    query += " ORDER BY lf.extracted_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def explain_belief(form_id: str) -> dict:
    """Explain why a belief exists: form + statuses + supporting evidence + inference history."""
    form = get_form(form_id)
    if not form:
        return {"error": "form not found"}

    statuses = get_form_statuses(form_id)

    conn = _conn()
    # Derived objects that reference this form
    derived = conn.execute(
        "SELECT * FROM derived_objects WHERE source_form_ids LIKE ? AND invalidated_at IS NULL",
        (f'%{form_id}%',),
    ).fetchall()

    # Inference log entries involving this form
    inferences = conn.execute(
        "SELECT * FROM inference_log WHERE input_form_ids LIKE ? ORDER BY fired_at DESC",
        (f'%{form_id}%',),
    ).fetchall()
    conn.close()

    return {
        "form": form,
        "statuses": statuses,
        "derived_objects": [dict(r) for r in derived],
        "inference_history": [dict(r) for r in inferences],
    }


if __name__ == "__main__":
    conn = init_beliefs_db()
    conn.close()
    print(f"beliefs.db initialized at {DB_PATH}")
    stats = belief_stats()
    print(f"  Worlds: {stats['worlds']}")
    print(f"  Logical forms: {stats['logical_forms']['total']}")
    print(f"  Derived objects: {stats['derived_objects']['total']}")
    print(f"  Inference log: {stats['inference_log']['total']}")
