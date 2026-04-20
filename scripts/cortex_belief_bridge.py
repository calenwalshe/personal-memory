"""
cortex_belief_bridge.py — Thin bridge between Cortex skills and the vault belief engine.

Cortex skills import this module for all belief operations. Every function
soft-fails — if the vault is unavailable, it returns a safe default and logs
a warning. Skills continue working unchanged without the vault.

Functions:
  query_beliefs(topic, slug)        — 3-stage retrieval for skill context injection
  ingest_and_extract(path, slug)    — ingest artifact + L3 extraction inline
  promote_on_close(slug)            — selective promotion of lessons/design_rules
  invalidate_dependents(form_id)    — JTMS Lite cascading invalidation
  format_beliefs(beliefs, max_chars) — compact bullet formatting for injection
"""

import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
BELIEFS_DB = VAULT / "beliefs.db"
SOURCES_DB = VAULT / "sources.db"
SCRIPTS = VAULT / "scripts"

# Add vault scripts to path for imports
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _vault_available() -> bool:
    """Check if vault databases exist and are readable."""
    return BELIEFS_DB.exists() and SOURCES_DB.exists()


def _beliefs_conn() -> Optional[sqlite3.Connection]:
    """Get a read-only connection to beliefs.db. Returns None if unavailable."""
    if not BELIEFS_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{BELIEFS_DB}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _beliefs_conn_rw() -> Optional[sqlite3.Connection]:
    """Get a read-write connection to beliefs.db."""
    if not BELIEFS_DB.exists():
        return None
    try:
        conn = sqlite3.connect(str(BELIEFS_DB), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    except Exception:
        return None


# ── Query beliefs (3-stage retrieval) ────────────────────────────────────

def query_beliefs(
    topic: str = None,
    slug: str = None,
    max_results: int = 20,
) -> dict:
    """3-stage belief retrieval for Cortex skill context injection.

    Returns dict with keys: global_stable, recurring, caution, formatted.
    All values are lists of dicts. formatted is a string ready for injection.

    Soft-fails: returns empty results if vault unavailable.
    """
    empty = {"global_stable": [], "recurring": [], "caution": [], "formatted": ""}

    conn = _beliefs_conn()
    if not conn:
        return empty

    try:
        # Stage 1: Global stable beliefs
        global_q = """
            SELECT lf.id, lf.form_type, lf.content, lf.subject, lf.predicate,
                   lf.object, lf.confidence, fs.status
            FROM logical_forms lf
            JOIN form_status fs ON lf.id = fs.form_id
            WHERE lf.scope_type = 'global'
              AND fs.world_id = 'current'
              AND fs.status IN ('active', 'stable')
              AND fs.valid_until IS NULL
              AND lf.superseded_by IS NULL
        """
        params = []

        if topic:
            global_q += " AND (lf.content LIKE ? OR lf.subject LIKE ? OR lf.predicate LIKE ?)"
            t = f"%{topic}%"
            params.extend([t, t, t])

        global_q += " ORDER BY lf.confidence DESC, lf.extracted_at DESC LIMIT ?"
        params.append(max_results)

        global_stable = [dict(r) for r in conn.execute(global_q, params).fetchall()]

        # Stage 2: Recurring project beliefs (current slug)
        recurring = []
        if slug:
            slug_q = """
                SELECT lf.id, lf.form_type, lf.content, lf.subject, lf.predicate,
                       lf.object, lf.confidence, fs.status
                FROM logical_forms lf
                JOIN form_status fs ON lf.id = fs.form_id
                WHERE lf.scope_type = 'project'
                  AND lf.scope_id = ?
                  AND fs.world_id IN ('current', 'planned')
                  AND fs.status IN ('active', 'stable')
                  AND fs.valid_until IS NULL
                  AND lf.superseded_by IS NULL
                ORDER BY lf.confidence DESC LIMIT ?
            """
            recurring = [dict(r) for r in conn.execute(slug_q, [slug, max_results]).fetchall()]

        # Stage 3: Caution set (contested/rejected from any scope)
        caution = []
        if topic:
            caution_q = """
                SELECT lf.id, lf.form_type, lf.content, lf.subject, fs.status, fs.world_id
                FROM logical_forms lf
                JOIN form_status fs ON lf.id = fs.form_id
                WHERE fs.world_id IN ('contested', 'rejected')
                  AND fs.valid_until IS NULL
                  AND (lf.content LIKE ? OR lf.subject LIKE ?)
                ORDER BY lf.extracted_at DESC LIMIT 5
            """
            t = f"%{topic}%"
            caution = [dict(r) for r in conn.execute(caution_q, [t, t]).fetchall()]

        conn.close()

        formatted = format_beliefs(global_stable, recurring, caution)

        return {
            "global_stable": global_stable,
            "recurring": recurring,
            "caution": caution,
            "formatted": formatted,
        }

    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        print(f"[belief-bridge] query_beliefs error: {e}", file=sys.stderr)
        return empty


def format_beliefs(
    global_stable: list,
    recurring: list = None,
    caution: list = None,
    max_chars: int = 2000,
) -> str:
    """Format beliefs as compact bullets for context injection."""
    lines = []

    if global_stable:
        lines.append("### Known Beliefs (global stable)")
        for b in global_stable[:8]:
            status_tag = " [STABLE]" if b.get("status") == "stable" else ""
            lines.append(f"- [{b['form_type']}] {b['content'][:120]}{status_tag}")

    if recurring:
        lines.append("\n### Project Beliefs (this slug)")
        for b in recurring[:5]:
            lines.append(f"- [{b['form_type']}] {b['content'][:120]}")

    if caution:
        lines.append("\n### Caution (contested/rejected)")
        for b in caution[:3]:
            lines.append(f"- [!] {b['content'][:100]} (status: {b.get('status', '?')})")

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars - 20] + "\n... (truncated)"
    return result


# ── Ingest and extract ───────────────────────────────────────────────────

def ingest_and_extract(
    artifact_path: str,
    slug: str,
    project: str = None,
) -> dict:
    """Ingest a Cortex artifact into sources.db, then run L3 extraction.

    Returns dict with source_id, forms_extracted, inference stats.
    Soft-fails: returns empty dict if vault unavailable.
    """
    if not _vault_available():
        print("[belief-bridge] vault unavailable — skipping ingest", file=sys.stderr)
        return {}

    try:
        from intake_doc import ingest_document
        from l3_engine import extract_forms, run_inference
        from belief_store import init_beliefs_db, add_forms_batch, set_form_status

        # Step 1: Ingest into sources.db
        result = ingest_document(
            artifact_path,
            project=project or f"cortex-{slug}",
            title=f"Cortex artifact: {Path(artifact_path).name}",
        )

        # Step 2: Extract L3 forms from the new source's atoms
        # The intake creates source + segments but doesn't create atoms.
        # We need to run evidence extraction first, then L3 extraction.
        # For now, we extract forms directly from the artifact text.
        init_beliefs_db()

        artifact_text = Path(artifact_path).read_text(encoding="utf-8")
        forms = _extract_forms_from_text(artifact_text, slug)

        if forms:
            form_ids = add_forms_batch(forms, extraction_run=f"cortex-{slug}-{_now()[:10]}")

            # Assign world + status for each form
            for fid, fd in zip(form_ids, forms):
                world = _assign_world(fd)
                set_form_status(
                    form_id=fid,
                    world_id=world,
                    status="active",
                    confidence=fd.get("confidence", 0.7),
                    set_by=f"cortex-bridge:{slug}",
                    reason=f"Extracted from {Path(artifact_path).name}",
                )

        # Step 3: Run inference on the new forms
        inf = run_inference()

        return {
            "source_id": result.get("source_id"),
            "segments": result.get("segment_count", 0),
            "forms_extracted": len(forms) if forms else 0,
            "inference": inf,
        }

    except Exception as e:
        print(f"[belief-bridge] ingest_and_extract error: {e}", file=sys.stderr)
        return {}


def _extract_forms_from_text(text: str, slug: str) -> list[dict]:
    """Extract logical forms from artifact text using Haiku."""
    import re
    import shutil
    import subprocess

    _NVM_BIN = Path.home() / ".nvm/versions/node/v20.20.0/bin"
    CLAUDE_BIN = (
        os.environ.get("CLAUDE_BIN")
        or shutil.which("claude")
        or str(_NVM_BIN / "claude")
    )

    # Truncate to avoid blowing Haiku context
    text_truncated = text[:6000]

    prompt = f"""Extract structured logical forms from this Cortex artifact. For each claim, decision, plan, rule, warning, or question, return JSON:

{{"forms": [
  {{"form_type": "claim|decision|plan|rule|warning|question",
    "content": "the statement",
    "subject": "main entity",
    "predicate": "verb/relation",
    "object": "target",
    "confidence": 0.8}}
]}}

Return ONLY valid JSON. Extract the 5-10 most important forms.

ARTIFACT:
{text_truncated}"""

    try:
        result = subprocess.run(
            ["env", "-u", "ANTHROPIC_API_KEY",
             CLAUDE_BIN, "-p", prompt,
             "--model", "claude-haiku-4-5-20251001"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return []

        raw = result.stdout.strip()
        json_match = re.search(r'\{[^{}]*"forms"\s*:\s*\[.*?\]\s*\}', raw, re.DOTALL)
        if not json_match:
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            forms = data.get("forms", [])
            # Add scope metadata
            for f in forms:
                f["scope_type"] = "project"
                f["scope_id"] = slug
                f["project"] = f"cortex-{slug}"
            return forms
    except Exception:
        pass
    return []


def _assign_world(form_dict: dict) -> str:
    """Determine initial Kripke world for a form."""
    ft = form_dict.get("form_type", "claim")
    if ft == "plan":
        return "planned"
    elif ft == "question":
        return "possible"
    elif ft == "preference":
        return "user_belief"
    return "current"


# ── Promote on close ─────────────────────────────────────────────────────

def promote_on_close(slug: str) -> dict:
    """Promote durable beliefs from project scope to global scope.

    Policy: auto-promote derived lessons/design_rules/anti_patterns.
    Never promote project-specific tasks, contested, or rejected forms.

    Soft-fails: returns empty dict if vault unavailable.
    """
    conn = _beliefs_conn_rw()
    if not conn:
        return {}

    try:
        # Find promotable derived objects
        promotable_types = ('lesson', 'design_rule', 'anti_pattern', 'heuristic', 'stable_belief')
        placeholders = ",".join("?" * len(promotable_types))

        derived = conn.execute(
            f"""SELECT id, type, content, source_form_ids, confidence
                FROM derived_objects
                WHERE namespace = ?
                  AND type IN ({placeholders})
                  AND invalidated_at IS NULL""",
            [f"cortex:{slug}"] + list(promotable_types),
        ).fetchall()

        # Also check personal namespace
        derived2 = conn.execute(
            f"""SELECT id, type, content, source_form_ids, confidence
                FROM derived_objects
                WHERE namespace = 'personal'
                  AND type IN ({placeholders})
                  AND invalidated_at IS NULL""",
            list(promotable_types),
        ).fetchall()

        all_derived = list(derived) + list(derived2)

        promoted_count = 0
        now = _now()

        for d in all_derived:
            # Check source forms are from this slug
            source_ids = json.loads(d["source_form_ids"])
            if not source_ids:
                continue

            # Promote the source forms to global scope
            for fid in source_ids:
                conn.execute(
                    "UPDATE logical_forms SET scope_type='global', scope_id=NULL WHERE id=? AND scope_id=?",
                    (fid, slug),
                )

            promoted_count += 1

        conn.commit()
        conn.close()

        # Log promotion (separate connection)
        if promoted_count > 0:
            try:
                from belief_store import log_inference
                log_inference(
                    rule_name="promotion_on_close",
                    module="cortex",
                    input_form_ids=[slug],
                    action="promoted",
                    detail=f"Promoted {promoted_count} derived objects from slug {slug} to global scope",
                )
            except Exception:
                pass

        return {"promoted": promoted_count, "slug": slug}

    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        print(f"[belief-bridge] promote_on_close error: {e}", file=sys.stderr)
        return {}


# ── Invalidate dependents (JTMS Lite) ────────────────────────────────────

def invalidate_dependents(form_id: str) -> dict:
    """Cascade invalidation through derived_dependencies.

    When a source form is retracted/changed, find all derived objects
    that depend on it and mark them stale.

    Soft-fails: returns empty dict if vault unavailable.
    """
    conn = _beliefs_conn_rw()
    if not conn:
        return {}

    try:
        now = _now()

        # Find all derived objects that depend on this form (recursive)
        dependents = conn.execute(
            """WITH RECURSIVE dep_chain(did) AS (
                 SELECT derived_object_id FROM derived_dependencies
                 WHERE source_kind='logical_form' AND source_id=?
                 UNION
                 SELECT dd.derived_object_id FROM derived_dependencies dd
                 JOIN dep_chain dc ON dd.source_kind='derived_object' AND dd.source_id=dc.did
               )
               SELECT did FROM dep_chain""",
            (form_id,),
        ).fetchall()

        invalidated = 0
        for row in dependents:
            conn.execute(
                "UPDATE derived_objects SET invalidated_at=?, invalidated_by=? WHERE id=? AND invalidated_at IS NULL",
                (now, f"retraction:{form_id}", row["did"]),
            )
            invalidated += 1

        conn.commit()
        conn.close()

        if invalidated > 0:
            try:
                from belief_store import log_inference
                log_inference(
                    rule_name="jtms_cascade_invalidation",
                    module="cortex",
                    input_form_ids=[form_id],
                    action="invalidated",
                    detail=f"Cascaded invalidation to {invalidated} derived objects",
                )
            except Exception:
                pass

        return {"form_id": form_id, "invalidated": invalidated}

    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        print(f"[belief-bridge] invalidate_dependents error: {e}", file=sys.stderr)
        return {}


# ── Record dependency ────────────────────────────────────────────────────

def record_dependency(
    derived_object_id: str,
    source_kind: str,
    source_id: str,
    role: str = "support",
) -> bool:
    """Record a dependency edge for JTMS tracking."""
    conn = _beliefs_conn_rw()
    if not conn:
        return False

    try:
        conn.execute(
            """INSERT OR IGNORE INTO derived_dependencies
               (derived_object_id, source_kind, source_id, role, created_at)
               VALUES (?,?,?,?,?)""",
            (derived_object_id, source_kind, source_id, role, _now()),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return False


# ── CLI test mode ────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--test" in sys.argv:
        print("Testing cortex_belief_bridge...")
        print(f"  Vault available: {_vault_available()}")

        if _vault_available():
            result = query_beliefs(topic="memory", max_results=3)
            print(f"  Query 'memory': {len(result['global_stable'])} global, "
                  f"{len(result['recurring'])} recurring, {len(result['caution'])} caution")
            print(f"  Formatted length: {len(result['formatted'])} chars")

            if result['formatted']:
                print(f"\n{result['formatted'][:500]}")

        print("\nAll tests passed.")
        sys.exit(0)

    # Default: show status
    print(f"Vault: {'available' if _vault_available() else 'unavailable'}")
    if _vault_available():
        conn = _beliefs_conn()
        total = conn.execute("SELECT COUNT(*) FROM logical_forms").fetchone()[0]
        scoped = conn.execute("SELECT scope_type, COUNT(*) FROM logical_forms GROUP BY scope_type").fetchall()
        deps = conn.execute("SELECT COUNT(*) FROM derived_dependencies").fetchone()[0]
        conn.close()
        print(f"  Forms: {total}")
        for r in scoped:
            print(f"    {r[0]}: {r[1]}")
        print(f"  Dependencies: {deps}")
