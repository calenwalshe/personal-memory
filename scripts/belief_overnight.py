#!/usr/bin/env python3
"""
belief_overnight.py — Overnight belief adjudication using Gemma 4.

Loops through untested logical forms (lowest-confidence first), finds the
most overlapping recent atom, and sends inconclusive pairs to local Gemma 4
at localhost:8770 for LLM adjudication. Updates confidence in beliefs.db.

Safe to interrupt — state persisted after each form.

Usage:
    python3 belief_overnight.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
BELIEFS_DB = VAULT / "beliefs.db"
ATOMS_DB = VAULT / "atoms.db"
GEMMA_URL = os.environ.get("GEMMA_URL", "http://localhost:8770")
POLL_INTERVAL = 2.0
POLL_TIMEOUT = 120
ATOM_BATCH = 50


ADJUDICATION_PROMPT = """\
You are a belief adjudicator. Determine whether a piece of evidence (an "atom") \
confirms, disconfirms, or is inconclusive with respect to a stated belief.

Belief ({form_type}): {belief}

Evidence: {atom_content}

Respond with ONLY a JSON object:
{{"verdict": "confirmed"|"disconfirmed"|"inconclusive", "reason": "<one sentence>"}}
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _gemma_adjudicate(belief: str, form_type: str, atom: str, dry_run: bool) -> dict:
    if dry_run:
        return {"verdict": "inconclusive", "reason": "dry-run"}

    prompt = ADJUDICATION_PROMPT.format(
        form_type=form_type,
        belief=belief[:500],
        atom_content=atom[:500],
    )
    try:
        resp = requests.post(f"{GEMMA_URL}/api/task",
                             json={"prompt": prompt, "model": "local"}, timeout=10)
        resp.raise_for_status()
        job_id = resp.json()["id"]
    except Exception as e:
        return {"verdict": "inconclusive", "reason": f"submit error: {e}"}

    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            r = requests.get(f"{GEMMA_URL}/api/task/{job_id}", timeout=10)
            data = r.json()
            if data.get("status") == "done":
                raw = data.get("result", "")
                m = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
                if m:
                    parsed = json.loads(m.group())
                    if parsed.get("verdict") in ("confirmed", "disconfirmed", "inconclusive"):
                        return parsed
                return {"verdict": "inconclusive", "reason": f"unparseable: {raw[:80]}"}
        except Exception as e:
            return {"verdict": "inconclusive", "reason": f"poll error: {e}"}

    return {"verdict": "inconclusive", "reason": "timeout"}


def _beta_update(alpha: float, beta: float, confirmed: bool) -> tuple[float, float, float]:
    a = alpha + (1 if confirmed else 0)
    b = beta + (0 if confirmed else 1)
    return a, b, a / (a + b)


def _record(conn, form_id, atom_id, outcome, detail,
            conf_before, conf_after, alpha, beta, alpha_new, beta_new):
    conn.execute("""
        INSERT OR IGNORE INTO belief_tests
        (id, form_id, test_type, atom_id, outcome, detail,
         confidence_before, confidence_after,
         alpha_before, beta_before, alpha_after, beta_after, tested_at)
        VALUES (?,?,  'gemma_adjudication', ?,?,?,  ?,?,?,?,?,?, ?)
    """, (str(uuid.uuid4()), form_id, atom_id, outcome, detail,
          conf_before, conf_after, alpha, beta, alpha_new, beta_new, _now()))


def run_overnight(limit: int = 500, dry_run: bool = False) -> dict:
    if not BELIEFS_DB.exists():
        print("beliefs.db not found")
        return {}

    bcon = sqlite3.connect(str(BELIEFS_DB))
    bcon.row_factory = sqlite3.Row
    acon = sqlite3.connect(str(ATOMS_DB))
    acon.row_factory = sqlite3.Row

    forms = bcon.execute("""
        SELECT lf.id, lf.content, lf.form_type, lf.confidence
        FROM logical_forms lf
        LEFT JOIN form_status fs ON fs.form_id = lf.id
        WHERE (fs.status IS NULL OR fs.status = 'active')
          AND lf.id NOT IN (
              SELECT DISTINCT form_id FROM belief_tests
              WHERE test_type = 'gemma_adjudication'
          )
        ORDER BY lf.confidence ASC
        LIMIT ?
    """, (limit,)).fetchall()

    print(f"[{_now()}] Starting overnight run — {len(forms)} forms to adjudicate")
    stats = {"forms": len(forms), "confirmed": 0, "disconfirmed": 0,
             "inconclusive": 0, "skipped": 0}

    atoms = acon.execute("""
        SELECT id, content, atom_type FROM atoms
        WHERE atom_type IN ('pattern', 'decision', 'outcome', 'discovery')
          AND content IS NOT NULL
        ORDER BY time_first DESC
        LIMIT ?
    """, (ATOM_BATCH,)).fetchall()

    for i, form in enumerate(forms):
        form_id = form["id"]
        content = form["content"] or ""
        form_type = form["form_type"] or "claim"
        conf = float(form["confidence"] or 0.7)
        # Derive pseudo alpha/beta from confidence for Beta-Bernoulli update
        alpha = conf * 10
        beta = (1.0 - conf) * 10

        # Find most overlapping atom
        belief_words = set(content.lower().split())
        best_atom = None
        best_overlap = 0
        for atom in atoms:
            atom_words = set((atom["content"] or "").lower().split())
            overlap = len(belief_words & atom_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_atom = atom

        if not best_atom or best_overlap < 2:
            stats["skipped"] += 1
            continue

        print(f"  [{i+1}/{len(forms)}] {form_id[:8]}… overlap={best_overlap} → Gemma… ", end="", flush=True)

        verdict = _gemma_adjudicate(content, form_type, best_atom["content"] or "", dry_run)
        v = verdict.get("verdict", "inconclusive")
        reason = verdict.get("reason", "")

        if v == "confirmed":
            alpha_new, beta_new, conf_after = _beta_update(alpha, beta, True)
            stats["confirmed"] += 1
        elif v == "disconfirmed":
            alpha_new, beta_new, conf_after = _beta_update(alpha, beta, False)
            stats["disconfirmed"] += 1
        else:
            alpha_new, beta_new, conf_after = alpha, beta, conf
            stats["inconclusive"] += 1

        print(f"{v} ({conf:.2f}→{conf_after:.2f})")

        if not dry_run:
            bcon.execute("UPDATE logical_forms SET confidence = ? WHERE id = ?",
                         (conf_after, form_id))
            _record(bcon, form_id, best_atom["id"], v, reason,
                    conf, conf_after, alpha, beta, alpha_new, beta_new)
            bcon.commit()

    bcon.close()
    acon.close()
    print(f"\n[{_now()}] Done — {stats}")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Overnight Gemma 4 belief adjudication")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_overnight(limit=args.limit, dry_run=args.dry_run)
