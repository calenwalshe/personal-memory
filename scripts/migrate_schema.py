"""
migrate_schema.py — Additive schema migration for cortex-memory-platform.

Adds to facts table:
  - memory_type    TEXT DEFAULT 'semantic'
  - project_scope  TEXT DEFAULT NULL
  - event_time     TEXT DEFAULT NULL
  - ingestion_time TEXT DEFAULT NULL

Creates new tables:
  - entities(id, canonical_name, entity_type, aliases, first_seen, project_scope)
  - fact_entities(fact_id, entity_id)

Idempotent: safe to run multiple times.
Takes a backup before altering.

Usage:
  python3 migrate_schema.py [--dry-run]
"""

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path
import os

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
DB_PATH = VAULT / "facts.db"
BACKUP_PATH = VAULT / "facts.db.pre-migration-backup"


def get_existing_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(facts)").fetchall()}


def get_existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}


def run_migration(dry_run: bool = False) -> dict:
    if not DB_PATH.exists():
        print(f"ERROR: facts.db not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    existing_cols = get_existing_columns(conn)
    existing_tables = get_existing_tables(conn)
    fact_count_before = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    conn.close()

    new_columns = [
        ("memory_type",    "TEXT DEFAULT 'semantic'"),
        ("project_scope",  "TEXT DEFAULT NULL"),
        ("event_time",     "TEXT DEFAULT NULL"),
        ("ingestion_time", "TEXT DEFAULT NULL"),
    ]
    cols_to_add = [(name, defn) for name, defn in new_columns if name not in existing_cols]

    new_tables_needed = []
    if "entities" not in existing_tables:
        new_tables_needed.append("entities")
    if "fact_entities" not in existing_tables:
        new_tables_needed.append("fact_entities")

    if not cols_to_add and not new_tables_needed:
        print("Migration already applied — nothing to do.")
        return {"status": "already_applied", "facts_before": fact_count_before}

    if dry_run:
        print("DRY RUN — would apply:")
        for name, defn in cols_to_add:
            print(f"  ALTER TABLE facts ADD COLUMN {name} {defn}")
        for t in new_tables_needed:
            print(f"  CREATE TABLE {t}")
        return {"status": "dry_run"}

    # Take backup before altering
    shutil.copy2(DB_PATH, BACKUP_PATH)
    print(f"Backup written to {BACKUP_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    for name, defn in cols_to_add:
        conn.execute(f"ALTER TABLE facts ADD COLUMN {name} {defn}")
        print(f"  Added column: {name} {defn}")

    if "entities" in new_tables_needed:
        conn.executescript("""
            CREATE TABLE entities (
                id             TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                entity_type    TEXT,
                aliases        TEXT DEFAULT '[]',
                first_seen     TEXT NOT NULL,
                project_scope  TEXT DEFAULT NULL
            );
            CREATE UNIQUE INDEX idx_entities_canonical_scope
                ON entities(canonical_name, COALESCE(project_scope, ''));
            CREATE INDEX idx_entities_canonical ON entities(canonical_name);
        """)
        print("  Created table: entities")

    if "fact_entities" in new_tables_needed:
        conn.executescript("""
            CREATE TABLE fact_entities (
                fact_id   TEXT NOT NULL REFERENCES facts(id),
                entity_id TEXT NOT NULL REFERENCES entities(id),
                PRIMARY KEY (fact_id, entity_id)
            );
            CREATE INDEX IF NOT EXISTS idx_fact_entities_fact ON fact_entities(fact_id);
            CREATE INDEX IF NOT EXISTS idx_fact_entities_entity ON fact_entities(entity_id);
        """)
        print("  Created table: fact_entities")

    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_facts_memory_type ON facts(memory_type);
        CREATE INDEX IF NOT EXISTS idx_facts_project_scope ON facts(project_scope);
    """)

    conn.commit()

    fact_count_after = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    conn.close()

    assert fact_count_after == fact_count_before, (
        f"MIGRATION ERROR: fact count changed {fact_count_before} → {fact_count_after}"
    )

    print(f"\nMigration complete. Facts: {fact_count_after} (unchanged). ✓")
    return {
        "status": "applied",
        "facts_before": fact_count_before,
        "facts_after": fact_count_after,
        "cols_added": [n for n, _ in cols_to_add],
        "tables_created": new_tables_needed,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Schema migration for cortex-memory-platform")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without applying")
    args = parser.parse_args()
    run_migration(dry_run=args.dry_run)
