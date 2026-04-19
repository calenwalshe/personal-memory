"""
l3_engine.py — L3 runtime orchestrator.

Coordinates the three-phase L3 pipeline:
  1. Extract logical forms from L1 evidence units (via Haiku)
  2. Assign forms to Kripke worlds with statuses
  3. Run inference rules (fixed-point loop) to produce derived objects

Usage:
  from l3_engine import extract_forms, run_inference, run_full_pipeline

  # Extract forms from new atoms
  extract_forms(project="personal-memory")

  # Run inference on all current forms
  run_inference()

  # Full pipeline
  run_full_pipeline(project="personal-memory")
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from belief_store import (
    add_form, add_forms_batch, set_form_status,
    get_forms_in_world, get_derived, add_derived,
    log_inference, supersede_form, expire_form_status,
    get_state, set_state, belief_stats,
    _conn as beliefs_conn, init_beliefs_db,
)
from l3_module import get_module, RuleFiring

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
ATOMS_DB = VAULT / "atoms.db"

_NVM_BIN = Path.home() / ".nvm/versions/node/v20.20.0/bin"
CLAUDE_BIN = (
    os.environ.get("CLAUDE_BIN")
    or shutil.which("claude")
    or str(_NVM_BIN / "claude")
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Phase 1: Extract logical forms from atoms ─────────────────────────────

EXTRACTION_PROMPT = """You are extracting structured logical forms from memory evidence units.

For each evidence unit, extract 1-5 logical forms. Each form is a typed claim.

Form types:
  claim      — something said to be true ("Graphiti tracks facts over time")
  event      — something that happened ("Deployed memory viewer to production")
  decision   — a choice made ("Chose SQLite over Postgres for atoms")
  plan       — something intended ("Add a fact-over-time layer")
  preference — user tends to like something ("User prefers compact tables")
  warning    — something risky or fragile ("Do not replace atoms with facts")
  question   — something unresolved ("Should facts live in graph.db?")
  rule       — reusable pattern ("After editing Caddyfile, restart container")

For each form, return JSON:
{
  "forms": [
    {
      "form_type": "claim",
      "content": "the natural language statement",
      "subject": "main entity or topic",
      "predicate": "verb or relation",
      "object": "target entity or value",
      "confidence": 0.8
    }
  ]
}

Return ONLY valid JSON. Extract the most important forms — quality over quantity."""


def _haiku_extract(atom_content: str, atom_type: str, entities: str) -> list[dict]:
    """Call Haiku to extract logical forms from an atom."""
    prompt = f"""{EXTRACTION_PROMPT}

Evidence unit:
  Type: {atom_type}
  Content: {atom_content}
  Entities: {entities}"""

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
        # Extract JSON from response
        json_match = re.search(r'\{[^{}]*"forms"\s*:\s*\[.*?\]\s*\}', raw, re.DOTALL)
        if not json_match:
            # Try simpler pattern
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            return data.get("forms", [])
    except Exception:
        pass
    return []


def extract_forms(
    project: str = None,
    all_atoms: bool = False,
    dry_run: bool = False,
    batch_size: int = 20,
) -> dict:
    """Extract logical forms from L1 atoms.

    By default, only processes atoms not yet extracted (based on l3_state cursor).
    Use all_atoms=True to re-extract everything.
    """
    # Get cursor
    cursor = "" if all_atoms else get_state("last_extraction_cursor", "")
    run_id = str(uuid.uuid4())[:8]

    # Query atoms
    conn = sqlite3.connect(str(ATOMS_DB))
    conn.row_factory = sqlite3.Row

    query = """SELECT id, content, atom_type, project, entities, topic,
                      confidence, importance, created_at
               FROM atoms WHERE invalidated_by IS NULL"""
    params: list = []

    if cursor and not all_atoms:
        query += " AND created_at > ?"
        params.append(cursor)
    if project:
        query += " AND project = ?"
        params.append(project)

    query += " ORDER BY created_at ASC"
    atoms = conn.execute(query, params).fetchall()
    conn.close()

    if not atoms:
        return {"atoms_scanned": 0, "forms_extracted": 0, "run_id": run_id}

    print(f"Extracting forms from {len(atoms)} atoms (run={run_id})...")

    total_forms = 0
    last_created_at = ""

    for i, atom in enumerate(atoms):
        if dry_run:
            print(f"  [{i+1}/{len(atoms)}] {atom['atom_type']:10s} {atom['topic'] or '?'}")
            continue

        raw_forms = _haiku_extract(
            atom["content"],
            atom["atom_type"],
            atom["entities"] or "[]",
        )

        if raw_forms:
            form_dicts = []
            for rf in raw_forms:
                form_dicts.append({
                    "form_type": rf.get("form_type", "claim"),
                    "content": rf.get("content", atom["content"]),
                    "subject": rf.get("subject"),
                    "predicate": rf.get("predicate"),
                    "object": rf.get("object"),
                    "source_unit_id": atom["id"],
                    "source_unit_ids": [atom["id"]],
                    "entity_ids": json.loads(atom["entities"] or "[]"),
                    "project": atom["project"],
                    "confidence": rf.get("confidence", atom["confidence"] or 0.7),
                })

            form_ids = add_forms_batch(form_dicts, extraction_run=run_id)

            # Assign initial world status for each form
            for fid, fd in zip(form_ids, form_dicts):
                world = _assign_initial_world(fd)
                set_form_status(
                    form_id=fid,
                    world_id=world,
                    status="active",
                    confidence=fd["confidence"],
                    set_by=f"extractor:run-{run_id}",
                    reason=f"Initial extraction from atom {atom['id'][:8]}",
                )

            total_forms += len(form_ids)

        last_created_at = atom["created_at"]
        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(atoms)}] {total_forms} forms so far...")

    # Update cursor
    if last_created_at and not dry_run:
        set_state("last_extraction_cursor", last_created_at)
        set_state("last_extraction_run", run_id)

    print(f"Extraction complete: {total_forms} forms from {len(atoms)} atoms")
    return {
        "atoms_scanned": len(atoms),
        "forms_extracted": total_forms,
        "run_id": run_id,
    }


def _assign_initial_world(form_dict: dict) -> str:
    """Determine the initial Kripke world for a new form based on its type."""
    form_type = form_dict.get("form_type", "claim")

    if form_type == "plan":
        return "planned"
    elif form_type == "question":
        return "possible"
    elif form_type == "warning":
        return "current"
    elif form_type == "preference":
        return "user_belief"
    else:
        # claim, event, decision, rule → current
        return "current"


# ── Phase 3: Run inference rules ──────────────────────────────────────────

def run_inference(
    module_name: str = "personal",
    max_passes: int = 5,
    dry_run: bool = False,
) -> dict:
    """Run inference rules until fixed point (no new derivations).

    Returns stats about what was produced.
    """
    module = get_module(module_name)
    rules = module.inference_rules()

    total_firings = 0
    total_derived = 0
    total_status_changes = 0
    passes = 0

    for pass_n in range(max_passes):
        passes = pass_n + 1

        # Load current state
        current_forms = get_forms_in_world("current")
        all_derived = get_derived(namespace=module.namespace)

        # Also load forms from other worlds for supersession checks
        planned_forms = get_forms_in_world("planned")
        user_forms = get_forms_in_world("user_belief")
        all_active_forms = current_forms + planned_forms + user_forms

        # Build status list
        conn = beliefs_conn()
        statuses = conn.execute(
            "SELECT * FROM form_status WHERE valid_until IS NULL"
        ).fetchall()
        statuses = [dict(r) for r in statuses]
        conn.close()

        # Run each rule
        new_firings: list[RuleFiring] = []
        for rule in rules:
            firings = rule.evaluate(all_active_forms, all_derived, statuses)
            new_firings.extend(firings)

        if not new_firings:
            break  # Fixed point reached

        # Apply firings
        for firing in new_firings:
            if dry_run:
                print(f"  [dry-run] {firing.rule_name}: {firing.output_type} — {firing.output_content[:80]}")
                continue

            if firing.output_type == "status_change":
                # Supersession: move old form to past
                if len(firing.input_form_ids) == 2:
                    newer_id, older_id = firing.input_form_ids
                    expire_form_status(older_id, "current",
                                      reason=f"Superseded by {newer_id[:8]}")
                    set_form_status(older_id, "past", "superseded",
                                   set_by=f"rule:{firing.rule_name}",
                                   reason=firing.detail)
                    supersede_form(older_id, newer_id)

                log_inference(
                    rule_name=firing.rule_name,
                    module=module.namespace,
                    input_form_ids=firing.input_form_ids,
                    action=firing.action,
                    detail=firing.detail,
                )
                total_status_changes += 1
            else:
                # Create derived object
                derived_id = add_derived(
                    type_=firing.output_type,
                    content=firing.output_content,
                    source_form_ids=firing.input_form_ids,
                    rule_fired=firing.rule_name,
                    namespace=module.namespace,
                    confidence=firing.confidence,
                )

                log_inference(
                    rule_name=firing.rule_name,
                    module=module.namespace,
                    input_form_ids=firing.input_form_ids,
                    output_id=derived_id,
                    action=firing.action,
                    detail=firing.detail,
                )
                total_derived += 1

            total_firings += 1

        if dry_run:
            break

        print(f"  Pass {passes}: {len(new_firings)} firings "
              f"({total_derived} derived, {total_status_changes} status changes)")

    print(f"Inference complete: {passes} passes, {total_firings} firings")
    return {
        "passes": passes,
        "total_firings": total_firings,
        "derived_created": total_derived,
        "status_changes": total_status_changes,
        "module": module_name,
        "fixed_point": passes < max_passes,
    }


# ── Full pipeline ─────────────────────────────────────────────────────────

def run_full_pipeline(
    project: str = None,
    module_name: str = "personal",
    all_atoms: bool = False,
    dry_run: bool = False,
) -> dict:
    """Run the full L3 pipeline: extract → assign worlds → run inference."""
    # Ensure beliefs.db exists
    init_beliefs_db()

    print("=" * 60)
    print("L3 PIPELINE")
    print("=" * 60)

    # Phase 1: Extract forms
    print("\n--- Phase 1: Extract logical forms ---")
    extraction = extract_forms(project=project, all_atoms=all_atoms, dry_run=dry_run)

    # Phase 2: World assignment happens during extraction (inline)

    # Phase 3: Run inference
    print("\n--- Phase 3: Run inference rules ---")
    inference = run_inference(module_name=module_name, dry_run=dry_run)

    # Stats
    print("\n--- Results ---")
    stats = belief_stats()
    print(f"Logical forms: {stats['logical_forms']['active']} active "
          f"({stats['logical_forms']['by_type']})")
    print(f"Worlds: {stats['form_status']['by_world']}")
    print(f"Statuses: {stats['form_status']['by_status']}")
    print(f"Derived: {stats['derived_objects']['active']} active "
          f"({stats['derived_objects']['by_type']})")
    print(f"Inference log: {stats['inference_log']['total']} entries")

    return {
        "extraction": extraction,
        "inference": inference,
        "stats": stats,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="L3 belief runtime engine")
    parser.add_argument("command", choices=["extract", "infer", "pipeline", "stats"],
                        help="Command to run")
    parser.add_argument("--project", help="Project scope")
    parser.add_argument("--module", default="personal", help="L3 module (default: personal)")
    parser.add_argument("--all", action="store_true", help="Process all atoms, not just new")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    if args.command == "extract":
        extract_forms(project=args.project, all_atoms=args.all, dry_run=args.dry_run)
    elif args.command == "infer":
        run_inference(module_name=args.module, dry_run=args.dry_run)
    elif args.command == "pipeline":
        run_full_pipeline(project=args.project, module_name=args.module,
                         all_atoms=args.all, dry_run=args.dry_run)
    elif args.command == "stats":
        init_beliefs_db()
        stats = belief_stats()
        print(json.dumps(stats, indent=2))
