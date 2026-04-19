"""
extend_atoms_l1.py — Extend atoms.db with L1 evidence unit columns.

Adds 4 nullable columns to atoms table:
  source_id       — links to sources.db (NULL for existing chat atoms)
  source_type     — chat | doc | note | fact_dump | cortex_brief | code_log | research_doc
  unit_type       — observation | claim_candidate | decision_candidate | plan_candidate |
                    preference_candidate | question_candidate | warning_candidate |
                    rule_candidate | source_excerpt
  observed_labels — JSON array of obs: labels (e.g. ["obs:plan", "obs:decision"])

Then backfills existing atoms:
  source_type = 'chat' for all
  unit_type mapped from atom_type via UNIT_TYPE_MAP

Safe to run multiple times (idempotent).
"""

import sqlite3
import sys
from pathlib import Path

VAULT = Path(__file__).parent.parent
ATOMS_DB = VAULT / "atoms.db"

# Mapping from old atom_type to new unit_type
UNIT_TYPE_MAP = {
    "decision": "decision_candidate",
    "discovery": "claim_candidate",
    "gotcha": "warning_candidate",
    "outcome": "observation",
    "pattern": "rule_candidate",
    "failure": "warning_candidate",
}


def extend_schema(conn: sqlite3.Connection) -> list[str]:
    """Add new columns if they don't exist. Returns list of columns added."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(atoms)").fetchall()}
    added = []

    new_columns = [
        ("source_id", "TEXT"),
        ("source_type", "TEXT DEFAULT 'chat'"),
        ("unit_type", "TEXT"),
        ("observed_labels", "TEXT DEFAULT '[]'"),
    ]

    for col_name, col_def in new_columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE atoms ADD COLUMN {col_name} {col_def}")
            added.append(col_name)

    # Add indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_atoms_source_type ON atoms(source_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_atoms_unit_type ON atoms(unit_type)")

    conn.commit()
    return added


def backfill(conn: sqlite3.Connection) -> dict:
    """Backfill source_type and unit_type for existing atoms."""
    stats = {"total": 0, "backfilled_source_type": 0, "backfilled_unit_type": 0}

    rows = conn.execute(
        "SELECT id, atom_type, source_type, unit_type FROM atoms"
    ).fetchall()
    stats["total"] = len(rows)

    for row in rows:
        atom_id = row[0]
        atom_type = row[1]
        current_source_type = row[2]
        current_unit_type = row[3]

        updates = []
        params = []

        if not current_source_type:
            updates.append("source_type = ?")
            params.append("chat")
            stats["backfilled_source_type"] += 1

        if not current_unit_type:
            mapped = UNIT_TYPE_MAP.get(atom_type, "observation")
            updates.append("unit_type = ?")
            params.append(mapped)
            stats["backfilled_unit_type"] += 1

        if updates:
            params.append(atom_id)
            conn.execute(
                f"UPDATE atoms SET {', '.join(updates)} WHERE id = ?",
                params,
            )

    conn.commit()
    return stats


def verify(conn: sqlite3.Connection) -> dict:
    """Verify the migration succeeded."""
    total = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    with_source_type = conn.execute(
        "SELECT COUNT(*) FROM atoms WHERE source_type IS NOT NULL"
    ).fetchone()[0]
    with_unit_type = conn.execute(
        "SELECT COUNT(*) FROM atoms WHERE unit_type IS NOT NULL"
    ).fetchone()[0]

    by_unit_type = conn.execute(
        "SELECT unit_type, COUNT(*) as n FROM atoms GROUP BY unit_type ORDER BY n DESC"
    ).fetchall()

    return {
        "total": total,
        "with_source_type": with_source_type,
        "with_unit_type": with_unit_type,
        "all_backfilled": with_source_type == total and with_unit_type == total,
        "by_unit_type": {r[0]: r[1] for r in by_unit_type},
    }


def run():
    if not ATOMS_DB.exists():
        print("ERROR: atoms.db not found", file=sys.stderr)
        sys.exit(1)

    # Backup first
    import shutil
    backup = ATOMS_DB.with_suffix(".db.bak-l1-extend")
    if not backup.exists():
        shutil.copy2(ATOMS_DB, backup)
        print(f"Backup: {backup}")

    conn = sqlite3.connect(str(ATOMS_DB))
    conn.execute("PRAGMA journal_mode=WAL")

    # Step 1: pre-migration count
    pre_count = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    print(f"Pre-migration: {pre_count} atoms")

    # Step 2: extend schema
    added = extend_schema(conn)
    if added:
        print(f"Added columns: {', '.join(added)}")
    else:
        print("Schema already extended (idempotent)")

    # Step 3: backfill
    stats = backfill(conn)
    print(f"Backfill: {stats['backfilled_source_type']} source_type, "
          f"{stats['backfilled_unit_type']} unit_type")

    # Step 4: verify
    v = verify(conn)
    print(f"Post-migration: {v['total']} atoms")
    print(f"  source_type set: {v['with_source_type']}/{v['total']}")
    print(f"  unit_type set:   {v['with_unit_type']}/{v['total']}")
    print(f"  All backfilled:  {v['all_backfilled']}")
    print(f"  Unit type breakdown:")
    for ut, count in v["by_unit_type"].items():
        print(f"    {ut}: {count}")

    # Step 5: final row count check
    post_count = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    if post_count != pre_count:
        print(f"ERROR: Row count changed! {pre_count} → {post_count}", file=sys.stderr)
        sys.exit(1)
    print(f"Row count verified: {post_count} (no data loss)")

    conn.close()


if __name__ == "__main__":
    run()
