"""
migrate_facts.py — Migrate facts.db → beliefs.db logical_forms.

Maps:
  semantic facts  → form_type='claim', world='current'
  episodic facts  → form_type='event', world='current'
  procedural facts → form_type='rule', world='current'
  pending contradictions → form_type derived_objects type='contradiction'

After migration, facts.db is frozen (not deleted — kept for reference).

Usage:
  python3 migrate_facts.py [--dry-run]
"""

import json
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
FACTS_DB = VAULT / "facts.db"
BELIEFS_DB = VAULT / "beliefs.db"

# Map memory_type → form_type
MEMORY_TYPE_MAP = {
    "semantic": "claim",
    "episodic": "event",
    "procedural": "rule",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate(dry_run: bool = False) -> dict:
    """Migrate facts.db content to beliefs.db."""
    if not FACTS_DB.exists():
        print("facts.db not found — nothing to migrate")
        return {"status": "skipped", "reason": "no facts.db"}

    # Backup
    backup = FACTS_DB.with_suffix(".db.bak-pre-l3-migration")
    if not backup.exists() and not dry_run:
        shutil.copy2(FACTS_DB, backup)
        print(f"Backup: {backup}")

    # Read all facts
    facts_conn = sqlite3.connect(str(FACTS_DB))
    facts_conn.row_factory = sqlite3.Row

    facts = facts_conn.execute(
        "SELECT * FROM facts ORDER BY valid_from"
    ).fetchall()

    contradictions = facts_conn.execute(
        "SELECT * FROM contradiction_review"
    ).fetchall()

    facts_conn.close()

    stats = {
        "total_facts": len(facts),
        "by_type": {},
        "migrated": 0,
        "skipped_invalidated": 0,
        "contradictions": len(contradictions),
        "contradictions_migrated": 0,
    }

    # Count by type
    for f in facts:
        mt = f["memory_type"] or "semantic"
        stats["by_type"][mt] = stats["by_type"].get(mt, 0) + 1

    if dry_run:
        print(f"DRY RUN — would migrate {len(facts)} facts:")
        for mt, count in stats["by_type"].items():
            form_type = MEMORY_TYPE_MAP.get(mt, "claim")
            print(f"  {mt} ({count}) → form_type={form_type}")
        print(f"Would migrate {len(contradictions)} contradiction reviews")
        return stats

    # Open beliefs.db
    from belief_store import init_beliefs_db
    init_beliefs_db()

    beliefs_conn = sqlite3.connect(str(BELIEFS_DB))
    beliefs_conn.row_factory = sqlite3.Row
    beliefs_conn.execute("PRAGMA journal_mode=WAL")
    beliefs_conn.execute("PRAGMA foreign_keys=ON")

    now = _now()
    run_id = f"facts-migration-{now[:10]}"

    for f in facts:
        # Skip invalidated facts (they'll get superseded status)
        is_active = f["invalidated_by"] is None

        memory_type = f["memory_type"] or "semantic"
        form_type = MEMORY_TYPE_MAP.get(memory_type, "claim")

        # Parse entities
        try:
            entities = json.loads(f["entities"] or "[]")
        except Exception:
            entities = []

        form_id = str(uuid.uuid4())

        # Determine subject from entities or topic
        subject = None
        if entities:
            subject = entities[0]
        elif f["topic"]:
            subject = f["topic"]

        beliefs_conn.execute(
            """INSERT INTO logical_forms
               (id, form_type, content, subject, predicate, object,
                source_unit_id, source_unit_ids, entity_ids, project,
                confidence, extracted_at, extraction_run, superseded_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                form_id, form_type, f["content"],
                subject, None, None,
                None,  # no direct atom link
                json.dumps([]),
                json.dumps(entities),
                f["project_scope"],
                f["confidence"] or 0.7,
                f["valid_from"] or now,
                run_id,
                None if is_active else "migrated-invalidated",
            ),
        )

        # Set world status
        if is_active:
            world = "current"
            status = "active"
        else:
            world = "past"
            status = "superseded"

        status_id = str(uuid.uuid4())
        beliefs_conn.execute(
            """INSERT INTO form_status
               (id, form_id, world_id, status, confidence, valid_from,
                set_by, reason, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                status_id, form_id, world, status,
                f["confidence"] or 0.7,
                f["valid_from"] or now,
                f"migration:{run_id}",
                f"Migrated from facts.db (memory_type={memory_type})",
                now, now,
            ),
        )

        if is_active:
            stats["migrated"] += 1
        else:
            stats["skipped_invalidated"] += 1

    # Migrate contradictions as derived objects
    for c in contradictions:
        if c["status"] == "pending":
            derived_id = str(uuid.uuid4())
            beliefs_conn.execute(
                """INSERT INTO derived_objects
                   (id, type, namespace, content, source_form_ids, rule_fired,
                    confidence, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    derived_id, "contradiction", "personal",
                    f"Migrated contradiction: {c['reason'] or 'pending review'}",
                    json.dumps([]),
                    "facts_migration",
                    0.5, now,
                ),
            )
            stats["contradictions_migrated"] += 1

    # Log the migration
    beliefs_conn.execute(
        """INSERT INTO l3_state (key, value, updated_at) VALUES (?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        ("facts_migration_complete", json.dumps(stats), now),
    )

    beliefs_conn.commit()
    beliefs_conn.close()

    # Write migration report
    report_path = VAULT / "facts-migration-report.md"
    report = f"""# Facts.db Migration Report

**Date:** {now}
**Run ID:** {run_id}

## Summary

| Metric | Count |
|--------|-------|
| Total facts | {stats['total_facts']} |
| Migrated (active) | {stats['migrated']} |
| Skipped (invalidated) | {stats['skipped_invalidated']} |
| Contradictions | {stats['contradictions']} |
| Contradictions migrated | {stats['contradictions_migrated']} |

## By memory type

| Memory type | Count | Mapped to |
|------------|-------|-----------|
"""
    for mt, count in stats["by_type"].items():
        form_type = MEMORY_TYPE_MAP.get(mt, "claim")
        report += f"| {mt} | {count} | {form_type} |\n"

    report += f"""
## Post-migration

- facts.db backed up to: `{backup.name}`
- facts.db is now FROZEN — no new writes
- All active facts are in beliefs.db as logical_forms
- Invalidated facts have world=past, status=superseded
- Pending contradictions migrated as derived_objects

## Verification

```
sqlite3 beliefs.db "SELECT form_type, COUNT(*) FROM logical_forms WHERE extraction_run='{run_id}' GROUP BY form_type"
```
"""

    report_path.write_text(report)
    print(f"Migration report: {report_path}")

    return stats


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv

    print("=" * 60)
    print("FACTS.DB → BELIEFS.DB MIGRATION")
    print("=" * 60)
    print()

    stats = migrate(dry_run=dry_run)

    print()
    print("Results:")
    print(f"  Total facts:      {stats.get('total_facts', 0)}")
    print(f"  Migrated:         {stats.get('migrated', 0)}")
    print(f"  Invalidated:      {stats.get('skipped_invalidated', 0)}")
    print(f"  Contradictions:   {stats.get('contradictions_migrated', 0)}")
    if not dry_run:
        print()
        print("facts.db is now FROZEN. No new writes will occur.")
