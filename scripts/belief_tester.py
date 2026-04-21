"""
belief_tester.py — Passive belief testing engine (L3).

Implements Popperian falsification: beliefs are treated as predictions that
must survive contact with new evidence. Each belief carries explicit
disconfirmation conditions. When new atoms arrive, the engine checks whether
any active belief is confirmed, disconfirmed, or unaffected.

Confidence updates use Beta-Bernoulli:
  α = prior confirmations + 1
  β = prior disconfirmations + 1
  confidence = α / (α + β)

Repair operations (in order of severity):
  narrow      — restrict the scope (add qualifier, reduce coverage)
  split       — break one belief into two more specific ones
  historicize — mark as past-tense (was true, may not be now)
  supersede   — mark as replaced by a newer contradicting belief
  retire      — mark as dead (consistently falsified)

Usage:
  from belief_tester import PassiveTester
  tester = PassiveTester()
  results = tester.test_against_atom(atom_dict)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
BELIEFS_DB = VAULT / "beliefs.db"
ATOMS_DB = VAULT / "atoms.db"

# Memory class labels for atom classification
MEMORY_CLASS_EPISODIC = "episodic"    # specific events, sessions, incidents
MEMORY_CLASS_SEMANTIC = "semantic"    # general facts, patterns, rules
MEMORY_CLASS_PROCEDURAL = "procedural"  # how-to knowledge, workflows


# ── Schema additions ───────────────────────────────────────────────────────

_BELIEF_TEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS belief_tests (
    id                  TEXT PRIMARY KEY,
    form_id             TEXT NOT NULL,
    test_type           TEXT NOT NULL,  -- passive | active
    atom_id             TEXT,           -- evidence that triggered this test
    outcome             TEXT NOT NULL,  -- confirmed | disconfirmed | inconclusive
    detail              TEXT,
    confidence_before   REAL,
    confidence_after    REAL,
    alpha_before        REAL DEFAULT 1.0,
    beta_before         REAL DEFAULT 1.0,
    alpha_after         REAL DEFAULT 1.0,
    beta_after          REAL DEFAULT 1.0,
    tested_at           TEXT NOT NULL,
    FOREIGN KEY (form_id) REFERENCES logical_forms(id)
);

CREATE INDEX IF NOT EXISTS idx_bt_form ON belief_tests(form_id);
CREATE INDEX IF NOT EXISTS idx_bt_outcome ON belief_tests(outcome);
CREATE INDEX IF NOT EXISTS idx_bt_tested ON belief_tests(tested_at);

CREATE TABLE IF NOT EXISTS belief_disconfirmation_conditions (
    id          TEXT PRIMARY KEY,
    form_id     TEXT NOT NULL,
    condition   TEXT NOT NULL,      -- natural language disconfirmation condition
    keywords    TEXT DEFAULT '[]',  -- JSON list of trigger keywords
    created_at  TEXT NOT NULL,
    FOREIGN KEY (form_id) REFERENCES logical_forms(id)
);

CREATE INDEX IF NOT EXISTS idx_bdc_form ON belief_disconfirmation_conditions(form_id);

CREATE TABLE IF NOT EXISTS belief_repair_log (
    id              TEXT PRIMARY KEY,
    form_id         TEXT NOT NULL,
    repair_op       TEXT NOT NULL,  -- narrow | split | historicize | supersede | retire
    reason          TEXT,
    new_form_ids    TEXT DEFAULT '[]',
    performed_at    TEXT NOT NULL,
    FOREIGN KEY (form_id) REFERENCES logical_forms(id)
);

CREATE INDEX IF NOT EXISTS idx_brl_form ON belief_repair_log(form_id);
"""

_ATOMS_MEMORY_CLASS_MIGRATION = """
ALTER TABLE atoms ADD COLUMN memory_class TEXT DEFAULT 'semantic';
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_belief_test_schema(conn: sqlite3.Connection):
    """Add belief testing tables to beliefs.db. Idempotent."""
    conn.executescript(_BELIEF_TEST_SCHEMA)
    conn.commit()


def ensure_memory_class_column():
    """Add memory_class column to atoms.db if not present. Idempotent."""
    if not ATOMS_DB.exists():
        return
    conn = sqlite3.connect(str(ATOMS_DB))
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(atoms)").fetchall()]
        if "memory_class" not in cols:
            conn.execute(_ATOMS_MEMORY_CLASS_MIGRATION)
            conn.commit()
    finally:
        conn.close()


# ── Atom memory classification ─────────────────────────────────────────────

_EPISODIC_SIGNALS = re.compile(
    r"\b(happened|occurred|deployed|shipped|fixed|broke|discovered|found|"
    r"session|yesterday|today|last week|incident|outage|error|failed|succeeded|"
    r"merged|commit|released|launched|ran|tried|observed)\b",
    re.IGNORECASE,
)
_PROCEDURAL_SIGNALS = re.compile(
    r"\b(always|never|must|should|when you|to do|steps|procedure|workflow|"
    r"pattern|recipe|rule|template|how to|make sure|remember to|approach)\b",
    re.IGNORECASE,
)


def classify_memory_class(atom: dict) -> str:
    """Classify an atom as episodic, semantic, or procedural.

    Episodic: specific past events (incident, session, deployment)
    Procedural: reusable how-to patterns, rules, workflows
    Semantic: general facts, concepts, relationships (default)
    """
    content = (atom.get("content") or "") + " " + (atom.get("topic") or "")
    atom_type = atom.get("atom_type", "")

    if atom_type in ("outcome", "failure") or _EPISODIC_SIGNALS.search(content):
        return MEMORY_CLASS_EPISODIC
    if atom_type in ("pattern", "gotcha") or _PROCEDURAL_SIGNALS.search(content):
        return MEMORY_CLASS_PROCEDURAL
    return MEMORY_CLASS_SEMANTIC


def backfill_memory_class(batch_size: int = 500) -> int:
    """Classify and write memory_class for atoms where it is NULL or missing."""
    ensure_memory_class_column()
    if not ATOMS_DB.exists():
        return 0
    conn = sqlite3.connect(str(ATOMS_DB))
    try:
        rows = conn.execute(
            "SELECT id, content, topic, atom_type FROM atoms WHERE memory_class IS NULL LIMIT ?",
            (batch_size,),
        ).fetchall()
        updated = 0
        for row in rows:
            atom = {"id": row[0], "content": row[1], "topic": row[2], "atom_type": row[3]}
            mc = classify_memory_class(atom)
            conn.execute("UPDATE atoms SET memory_class=? WHERE id=?", (mc, row[0]))
            updated += 1
        conn.commit()
        return updated
    finally:
        conn.close()


# ── Beta-Bernoulli confidence updates ─────────────────────────────────────

def beta_bernoulli_update(
    alpha: float,
    beta: float,
    confirmed: bool,
) -> tuple[float, float, float]:
    """Update Beta-Bernoulli parameters and return (new_alpha, new_beta, new_confidence).

    Prior: Beta(alpha, beta)
    Likelihood: Bernoulli(confirmed)
    Posterior: Beta(alpha + confirmed, beta + (1 - confirmed))
    """
    if confirmed:
        alpha_new = alpha + 1.0
        beta_new = beta
    else:
        alpha_new = alpha
        beta_new = beta + 1.0
    confidence = alpha_new / (alpha_new + beta_new)
    return alpha_new, beta_new, confidence


# ── Disconfirmation condition derivation ───────────────────────────────────

def derive_disconfirmation_conditions(form: dict) -> list[str]:
    """Derive natural-language disconfirmation conditions from a logical form.

    Rules:
    - claim: "X is Y" → disconfirmed if evidence says "X is not Y" or "X is Z ≠ Y"
    - decision: "chose X" → disconfirmed if evidence shows X was reverted or replaced
    - rule: "always do X" → disconfirmed if evidence shows X was skipped with success
    - warning: "X is fragile" → disconfirmed if evidence shows X worked reliably
    - plan: "will do X" → disconfirmed if evidence shows X was abandoned
    """
    form_type = form.get("form_type", "claim")
    content = form.get("content", "")
    subject = form.get("subject") or ""
    predicate = form.get("predicate") or ""
    obj = form.get("object") or ""

    conditions = []

    if form_type == "claim":
        if subject and predicate and obj:
            conditions.append(
                f"Evidence that {subject} {predicate} something other than {obj}"
            )
            conditions.append(f"Evidence that {subject} does not {predicate} {obj}")
        else:
            conditions.append(f"Evidence contradicting: {content}")

    elif form_type == "decision":
        conditions.append(f"Evidence that the decision was reverted or replaced: {content}")
        conditions.append(f"Evidence that an alternative to this decision was adopted")

    elif form_type == "rule":
        conditions.append(f"Evidence that the rule was violated and the outcome was still good")
        conditions.append(f"Evidence that following this rule caused a problem")

    elif form_type == "warning":
        conditions.append(f"Evidence that the warned-about situation occurred without harm")
        conditions.append(f"Evidence that the risk was mitigated and is no longer relevant")

    elif form_type == "plan":
        conditions.append(f"Evidence that this plan was abandoned or superseded")
        conditions.append(f"Evidence that the planned item was completed with a different approach")

    elif form_type == "preference":
        conditions.append(f"Evidence that the preference was overridden or reversed")

    else:
        conditions.append(f"Evidence contradicting: {content}")

    return conditions


def extract_condition_keywords(condition: str) -> list[str]:
    """Extract trigger keywords from a disconfirmation condition for fast matching."""
    # Remove stopwords, keep content words
    stopwords = {"evidence", "that", "the", "a", "an", "of", "or", "and",
                 "is", "was", "were", "be", "been", "being", "this", "something"}
    words = re.findall(r"\b[a-zA-Z_]\w+\b", condition.lower())
    return [w for w in words if w not in stopwords and len(w) > 2]


# ── Passive belief tester ──────────────────────────────────────────────────

@dataclass
class TestResult:
    form_id: str
    outcome: str  # confirmed | disconfirmed | inconclusive
    detail: str
    confidence_before: float
    confidence_after: float
    alpha_before: float
    beta_before: float
    alpha_after: float
    beta_after: float
    repair_op: Optional[str] = None


RETIRE_THRESHOLD = 0.2    # confidence below this → retire
STABLE_THRESHOLD = 0.85   # confidence above this with 5+ tests → stable
MIN_TESTS_FOR_STABLE = 5


class PassiveTester:
    """Tests active beliefs against incoming atoms passively (no external API calls).

    Algorithm per atom:
    1. Classify atom memory_class
    2. Load active beliefs (semantic and procedural focus; episodic ignored)
    3. For each belief, check keyword overlap between atom content and
       disconfirmation conditions
    4. Score match strength; update Beta-Bernoulli if match found
    5. Apply repair operations if confidence crosses thresholds
    """

    def __init__(self, beliefs_db: Path = BELIEFS_DB, atoms_db: Path = ATOMS_DB):
        self.beliefs_db = beliefs_db
        self.atoms_db = atoms_db
        self._ensure_schema()

    def _ensure_schema(self):
        if not self.beliefs_db.exists():
            return
        conn = sqlite3.connect(str(self.beliefs_db))
        try:
            init_belief_test_schema(conn)
        finally:
            conn.close()

    def _beliefs_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.beliefs_db))
        conn.row_factory = sqlite3.Row
        return conn

    def _get_active_forms(self, memory_classes: list[str] = None) -> list[dict]:
        """Load active (non-superseded) logical forms from beliefs.db."""
        conn = self._beliefs_conn()
        try:
            # Active = not superseded, in current world, status active or stable
            rows = conn.execute("""
                SELECT lf.*, COALESCE(fs.confidence, lf.confidence) as effective_confidence
                FROM logical_forms lf
                LEFT JOIN form_status fs ON lf.id = fs.form_id
                    AND fs.world_id = (SELECT id FROM worlds WHERE label='current' LIMIT 1)
                WHERE lf.superseded_by IS NULL
                  AND (fs.status IN ('active', 'stable') OR fs.status IS NULL)
                ORDER BY lf.extracted_at DESC
                LIMIT 500
            """).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            # Schema not yet complete — return empty
            return []
        finally:
            conn.close()

    def _get_or_create_conditions(self, form: dict) -> list[dict]:
        """Get disconfirmation conditions for a form, creating if missing."""
        conn = self._beliefs_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM belief_disconfirmation_conditions WHERE form_id=?",
                (form["id"],),
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]

            # Create conditions for this form
            conditions = derive_disconfirmation_conditions(form)
            created = []
            for cond in conditions:
                keywords = extract_condition_keywords(cond)
                cid = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO belief_disconfirmation_conditions
                       (id, form_id, condition, keywords, created_at)
                       VALUES (?,?,?,?,?)""",
                    (cid, form["id"], cond, json.dumps(keywords), _now()),
                )
                created.append({"id": cid, "form_id": form["id"],
                                 "condition": cond, "keywords": keywords})
            conn.commit()
            return created
        finally:
            conn.close()

    def _get_belief_stats(self, form_id: str) -> tuple[float, float]:
        """Get current alpha, beta for a form from test history."""
        conn = self._beliefs_conn()
        try:
            row = conn.execute(
                """SELECT alpha_after, beta_after FROM belief_tests
                   WHERE form_id=? ORDER BY tested_at DESC LIMIT 1""",
                (form_id,),
            ).fetchone()
            if row:
                return row[0], row[1]
            return 1.0, 1.0  # uniform prior
        finally:
            conn.close()

    def _score_match(self, atom_content: str, conditions: list[dict]) -> tuple[float, str]:
        """Score how strongly an atom matches any disconfirmation condition.

        Returns (score 0.0-1.0, matched condition text).
        Score = fraction of condition keywords found in atom content.
        Threshold 0.3 = inconclusive, 0.5+ = disconfirmed.
        """
        atom_lower = atom_content.lower()
        atom_words = set(re.findall(r"\b\w+\b", atom_lower))

        best_score = 0.0
        best_condition = ""

        for cond in conditions:
            keywords = cond.get("keywords") or []
            if isinstance(keywords, str):
                keywords = json.loads(keywords)
            if not keywords:
                continue
            overlap = len(set(keywords) & atom_words)
            score = overlap / len(keywords)
            if score > best_score:
                best_score = score
                best_condition = cond.get("condition", "")

        return best_score, best_condition

    def _score_confirmation(self, atom_content: str, form: dict) -> float:
        """Score how strongly an atom confirms a belief.

        Uses keyword overlap between atom and form content+subject+predicate.
        Higher overlap = more confirming.
        """
        form_text = " ".join(filter(None, [
            form.get("content", ""),
            form.get("subject", ""),
            form.get("predicate", ""),
            form.get("object", ""),
        ])).lower()
        form_words = set(re.findall(r"\b\w+\b", form_text)) - {
            "the", "a", "an", "is", "was", "are", "were", "be", "been",
            "that", "this", "it", "of", "in", "on", "at", "to", "for",
        }
        atom_words = set(re.findall(r"\b\w+\b", atom_content.lower()))

        if not form_words:
            return 0.0
        overlap = len(form_words & atom_words)
        return overlap / len(form_words)

    def _record_test(self, form_id: str, atom_id: Optional[str],
                     outcome: str, detail: str,
                     conf_before: float, conf_after: float,
                     alpha_before: float, beta_before: float,
                     alpha_after: float, beta_after: float):
        conn = self._beliefs_conn()
        try:
            conn.execute(
                """INSERT INTO belief_tests
                   (id, form_id, test_type, atom_id, outcome, detail,
                    confidence_before, confidence_after,
                    alpha_before, beta_before, alpha_after, beta_after, tested_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), form_id, "passive", atom_id, outcome, detail,
                 conf_before, conf_after,
                 alpha_before, beta_before, alpha_after, beta_after, _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def _apply_repair(self, form: dict, confidence: float, alpha: float, beta: float) -> Optional[str]:
        """Apply a repair operation if confidence crosses a threshold.

        Returns repair_op name if a repair was applied, else None.
        """
        if confidence < RETIRE_THRESHOLD and (alpha + beta) >= 5:
            op = "retire"
            self._log_repair(form["id"], op, f"confidence={confidence:.3f} < {RETIRE_THRESHOLD}")
            # Mark superseded by a sentinel value indicating retirement
            conn = self._beliefs_conn()
            try:
                conn.execute(
                    "UPDATE logical_forms SET superseded_by='RETIRED' WHERE id=?",
                    (form["id"],),
                )
                conn.commit()
            finally:
                conn.close()
            return op

        # Historicize if episodic belief has been contradicted twice
        if form.get("form_type") in ("event", "plan") and confidence < 0.45 and (alpha + beta) >= 3:
            op = "historicize"
            self._log_repair(form["id"], op,
                             f"episodic/plan belief confidence fell to {confidence:.3f}")
            conn = self._beliefs_conn()
            try:
                # Move to world=past if it exists
                past_world = conn.execute(
                    "SELECT id FROM worlds WHERE label='past' LIMIT 1"
                ).fetchone()
                if past_world:
                    conn.execute(
                        """INSERT OR REPLACE INTO form_status
                           (id, form_id, world_id, status, confidence, valid_from,
                            set_by, created_at, updated_at)
                           VALUES (?,?,?,'historicized',?,datetime('now'),
                                   'passive_tester',datetime('now'),datetime('now'))""",
                        (str(uuid.uuid4()), form["id"], past_world[0], confidence),
                    )
                    conn.commit()
            finally:
                conn.close()
            return op

        return None

    def _log_repair(self, form_id: str, op: str, reason: str, new_form_ids: list[str] = None):
        conn = self._beliefs_conn()
        try:
            conn.execute(
                """INSERT INTO belief_repair_log
                   (id, form_id, repair_op, reason, new_form_ids, performed_at)
                   VALUES (?,?,?,?,?,?)""",
                (str(uuid.uuid4()), form_id, op, reason,
                 json.dumps(new_form_ids or []), _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def test_against_atom(self, atom: dict) -> list[TestResult]:
        """Test all active beliefs against a single incoming atom.

        Returns list of TestResult for beliefs that were affected.
        """
        if not self.beliefs_db.exists():
            return []

        atom_id = atom.get("id")
        content = atom.get("content", "")
        if not content.strip():
            return []

        # Classify atom
        mc = classify_memory_class(atom)
        atom["memory_class"] = mc

        # Only semantic and procedural atoms carry general falsification signal
        # Episodic atoms (specific events) don't generalize
        if mc == MEMORY_CLASS_EPISODIC:
            return []

        forms = self._get_active_forms()
        if not forms:
            return []

        results = []
        for form in forms:
            conditions = self._get_or_create_conditions(form)
            alpha, beta = self._get_belief_stats(form["id"])
            conf_before = alpha / (alpha + beta)

            # Check disconfirmation
            disconf_score, matched_condition = self._score_match(content, conditions)

            # Check confirmation (atom reinforces the belief)
            conf_score = self._score_confirmation(content, form)

            if disconf_score >= 0.5 and disconf_score > conf_score:
                outcome = "disconfirmed"
                detail = f"matched disconfirmation condition: {matched_condition} (score={disconf_score:.2f})"
                alpha_new, beta_new, conf_after = beta_bernoulli_update(alpha, beta, confirmed=False)
            elif conf_score >= 0.4:
                outcome = "confirmed"
                detail = f"atom content reinforces belief (overlap={conf_score:.2f})"
                alpha_new, beta_new, conf_after = beta_bernoulli_update(alpha, beta, confirmed=True)
            elif disconf_score >= 0.3:
                outcome = "inconclusive"
                detail = f"weak disconfirmation signal (score={disconf_score:.2f})"
                alpha_new, beta_new, conf_after = alpha, beta, conf_before
            else:
                # No signal — skip
                continue

            self._record_test(
                form["id"], atom_id, outcome, detail,
                conf_before, conf_after,
                alpha, beta, alpha_new, beta_new,
            )

            repair_op = None
            if outcome in ("disconfirmed", "inconclusive"):
                repair_op = self._apply_repair(form, conf_after, alpha_new, beta_new)

            results.append(TestResult(
                form_id=form["id"],
                outcome=outcome,
                detail=detail,
                confidence_before=conf_before,
                confidence_after=conf_after,
                alpha_before=alpha,
                beta_before=beta,
                alpha_after=alpha_new,
                beta_after=beta_new,
                repair_op=repair_op,
            ))

        return results

    def run_batch(self, atom_ids: list[str] = None, limit: int = 100) -> dict:
        """Run passive testing against recent atoms from atoms.db.

        Args:
            atom_ids: specific atom IDs to test (optional)
            limit: max atoms to pull if atom_ids not given

        Returns summary dict with counts.
        """
        ensure_memory_class_column()
        if not self.atoms_db.exists():
            return {"tested": 0, "confirmed": 0, "disconfirmed": 0, "inconclusive": 0, "repairs": 0}

        conn = sqlite3.connect(str(self.atoms_db))
        conn.row_factory = sqlite3.Row
        try:
            if atom_ids:
                placeholders = ",".join("?" * len(atom_ids))
                rows = conn.execute(
                    f"SELECT * FROM atoms WHERE id IN ({placeholders}) AND invalidated_by IS NULL",
                    atom_ids,
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM atoms WHERE invalidated_by IS NULL
                       ORDER BY created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            atoms = [dict(r) for r in rows]
        finally:
            conn.close()

        summary = {"tested": 0, "confirmed": 0, "disconfirmed": 0, "inconclusive": 0, "repairs": 0}
        for atom in atoms:
            results = self.test_against_atom(atom)
            for r in results:
                summary["tested"] += 1
                summary[r.outcome] += 1
                if r.repair_op:
                    summary["repairs"] += 1

        return summary

    def get_belief_health(self, form_id: str) -> dict:
        """Return the test history and current confidence for a single form."""
        conn = self._beliefs_conn()
        try:
            tests = conn.execute(
                """SELECT outcome, confidence_before, confidence_after, alpha_after, beta_after, tested_at
                   FROM belief_tests WHERE form_id=? ORDER BY tested_at ASC""",
                (form_id,),
            ).fetchall()
            repairs = conn.execute(
                "SELECT repair_op, reason, performed_at FROM belief_repair_log WHERE form_id=?",
                (form_id,),
            ).fetchall()

            alpha, beta = self._get_belief_stats(form_id)
            confidence = alpha / (alpha + beta)

            return {
                "form_id": form_id,
                "confidence": confidence,
                "alpha": alpha,
                "beta": beta,
                "test_count": len(tests),
                "confirmed": sum(1 for t in tests if t[0] == "confirmed"),
                "disconfirmed": sum(1 for t in tests if t[0] == "disconfirmed"),
                "inconclusive": sum(1 for t in tests if t[0] == "inconclusive"),
                "repairs": [dict(zip(["op", "reason", "at"], r)) for r in repairs],
            }
        finally:
            conn.close()


if __name__ == "__main__":
    import sys
    ensure_memory_class_column()
    n = backfill_memory_class()
    print(f"Backfilled memory_class for {n} atoms")

    tester = PassiveTester()
    summary = tester.run_batch(limit=50)
    print(f"Passive test run: {summary}")
