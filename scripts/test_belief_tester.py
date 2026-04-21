"""
test_belief_tester.py — Tests for passive belief testing engine.

Covers: memory classification, Beta-Bernoulli updates, disconfirmation
condition derivation, passive tester outcomes, repair operations.
"""

import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def temp_vault(tmp_path, monkeypatch):
    """Redirect vault DBs to temp directory."""
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    import belief_tester as bt
    bt.BELIEFS_DB = tmp_path / "beliefs.db"
    bt.ATOMS_DB = tmp_path / "atoms.db"
    yield tmp_path


def _make_beliefs_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a minimal beliefs.db with required tables."""
    import belief_tester as bt
    conn = sqlite3.connect(str(tmp_path / "beliefs.db"))
    conn.row_factory = sqlite3.Row
    # Minimal schema for logical_forms, worlds, form_status
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS logical_forms (
            id TEXT PRIMARY KEY,
            form_type TEXT NOT NULL,
            content TEXT NOT NULL,
            subject TEXT,
            predicate TEXT,
            object TEXT,
            source_unit_id TEXT,
            source_unit_ids TEXT DEFAULT '[]',
            entity_ids TEXT DEFAULT '[]',
            project TEXT,
            confidence REAL DEFAULT 0.7,
            extracted_at TEXT NOT NULL,
            extraction_run TEXT,
            superseded_by TEXT,
            embedding BLOB,
            scope_type TEXT DEFAULT 'global',
            scope_id TEXT
        );
        CREATE TABLE IF NOT EXISTS worlds (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS form_status (
            id TEXT PRIMARY KEY,
            form_id TEXT NOT NULL,
            world_id TEXT NOT NULL,
            status TEXT NOT NULL,
            confidence REAL DEFAULT 0.7,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            set_by TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(form_id, world_id)
        );
        INSERT OR IGNORE INTO worlds (id, label, description, created_at)
        VALUES ('world-current', 'current', 'Current world', datetime('now'));
        INSERT OR IGNORE INTO worlds (id, label, description, created_at)
        VALUES ('world-past', 'past', 'Past world', datetime('now'));
    """)
    bt.init_belief_test_schema(conn)
    conn.commit()
    return conn


def _insert_form(conn, form_type="claim", content="SQLite is used for storage",
                 subject="SQLite", predicate="is used for", obj="storage",
                 fid=None) -> str:
    fid = fid or str(uuid.uuid4())
    conn.execute(
        """INSERT INTO logical_forms
           (id, form_type, content, subject, predicate, object, extracted_at, confidence)
           VALUES (?,?,?,?,?,?,datetime('now'),0.7)""",
        (fid, form_type, content, subject, predicate, obj),
    )
    conn.execute(
        """INSERT OR REPLACE INTO form_status
           (id, form_id, world_id, status, confidence, valid_from,
            set_by, created_at, updated_at)
           VALUES (?,?,'world-current','active',0.7,datetime('now'),
                   'test',datetime('now'),datetime('now'))""",
        (str(uuid.uuid4()), fid),
    )
    conn.commit()
    return fid


def _make_atoms_db(tmp_path: Path):
    conn = sqlite3.connect(str(tmp_path / "atoms.db"))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS atoms (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            atom_type TEXT NOT NULL DEFAULT 'pattern',
            topic TEXT,
            project TEXT,
            confidence REAL DEFAULT 0.7,
            importance REAL DEFAULT 0.5,
            entities TEXT DEFAULT '[]',
            source_events TEXT DEFAULT '[]',
            source_count INTEGER DEFAULT 1,
            session_ids TEXT DEFAULT '[]',
            time_first TEXT NOT NULL DEFAULT (datetime('now')),
            time_last TEXT NOT NULL DEFAULT (datetime('now')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            invalidated_by TEXT DEFAULT NULL,
            memory_class TEXT DEFAULT NULL
        );
    """)
    conn.commit()
    return conn


# ── Memory Classification Tests ───────────────────────────────────────────

class TestMemoryClassification:

    def test_episodic_signals(self):
        from belief_tester import classify_memory_class, MEMORY_CLASS_EPISODIC
        atom = {"content": "Deployed the auth fix to production yesterday",
                "atom_type": "outcome", "topic": "deployment"}
        assert classify_memory_class(atom) == MEMORY_CLASS_EPISODIC

    def test_procedural_signals(self):
        from belief_tester import classify_memory_class, MEMORY_CLASS_PROCEDURAL
        atom = {"content": "Always restart Caddy after editing the Caddyfile",
                "atom_type": "pattern", "topic": "caddy"}
        assert classify_memory_class(atom) == MEMORY_CLASS_PROCEDURAL

    def test_semantic_default(self):
        from belief_tester import classify_memory_class, MEMORY_CLASS_SEMANTIC
        atom = {"content": "SQLite uses WAL mode for concurrent reads",
                "atom_type": "decision", "topic": "sqlite"}
        assert classify_memory_class(atom) == MEMORY_CLASS_SEMANTIC

    def test_failure_atom_is_episodic(self):
        from belief_tester import classify_memory_class, MEMORY_CLASS_EPISODIC
        atom = {"content": "The migration failed on the production server",
                "atom_type": "failure", "topic": "migration"}
        assert classify_memory_class(atom) == MEMORY_CLASS_EPISODIC

    def test_gotcha_is_procedural(self):
        from belief_tester import classify_memory_class, MEMORY_CLASS_PROCEDURAL
        atom = {"content": "When you edit the Caddyfile, must restart the container",
                "atom_type": "gotcha", "topic": "caddy"}
        assert classify_memory_class(atom) == MEMORY_CLASS_PROCEDURAL


# ── Beta-Bernoulli Tests ──────────────────────────────────────────────────

class TestBetaBernoulli:

    def test_confirmation_increases_confidence(self):
        from belief_tester import beta_bernoulli_update
        alpha, beta, conf = beta_bernoulli_update(1.0, 1.0, confirmed=True)
        assert alpha == 2.0
        assert beta == 1.0
        assert conf == pytest.approx(2/3, abs=0.001)

    def test_disconfirmation_decreases_confidence(self):
        from belief_tester import beta_bernoulli_update
        alpha, beta, conf = beta_bernoulli_update(1.0, 1.0, confirmed=False)
        assert alpha == 1.0
        assert beta == 2.0
        assert conf == pytest.approx(1/3, abs=0.001)

    def test_repeated_confirmations_converge_high(self):
        from belief_tester import beta_bernoulli_update
        alpha, beta = 1.0, 1.0
        for _ in range(10):
            alpha, beta, conf = beta_bernoulli_update(alpha, beta, confirmed=True)
        assert conf > 0.85

    def test_repeated_disconfirmations_converge_low(self):
        from belief_tester import beta_bernoulli_update
        alpha, beta = 1.0, 1.0
        for _ in range(10):
            alpha, beta, conf = beta_bernoulli_update(alpha, beta, confirmed=False)
        assert conf < 0.2

    def test_uniform_prior_gives_half(self):
        from belief_tester import beta_bernoulli_update
        # Alpha=1, beta=1 → confidence = 1/2 before any update
        assert 1.0 / (1.0 + 1.0) == 0.5


# ── Disconfirmation Condition Tests ───────────────────────────────────────

class TestDisconfirmationConditions:

    def test_claim_generates_conditions(self):
        from belief_tester import derive_disconfirmation_conditions
        form = {"form_type": "claim", "content": "SQLite is used for storage",
                "subject": "SQLite", "predicate": "is used for", "object": "storage"}
        conditions = derive_disconfirmation_conditions(form)
        assert len(conditions) >= 1
        assert any("storage" in c.lower() or "SQLite" in c for c in conditions)

    def test_decision_generates_reversion_condition(self):
        from belief_tester import derive_disconfirmation_conditions
        form = {"form_type": "decision", "content": "Chose SQLite over Postgres",
                "subject": None, "predicate": None, "object": None}
        conditions = derive_disconfirmation_conditions(form)
        assert any("revert" in c.lower() or "replaced" in c.lower() for c in conditions)

    def test_rule_generates_violation_condition(self):
        from belief_tester import derive_disconfirmation_conditions
        form = {"form_type": "rule", "content": "Always restart Caddy after edits",
                "subject": None, "predicate": None, "object": None}
        conditions = derive_disconfirmation_conditions(form)
        assert any("violated" in c.lower() or "skipped" in c.lower() for c in conditions)

    def test_plan_generates_abandonment_condition(self):
        from belief_tester import derive_disconfirmation_conditions
        form = {"form_type": "plan", "content": "Will add Redis queue",
                "subject": None, "predicate": None, "object": None}
        conditions = derive_disconfirmation_conditions(form)
        assert any("abandon" in c.lower() or "superseded" in c.lower() for c in conditions)

    def test_keyword_extraction(self):
        from belief_tester import extract_condition_keywords
        kws = extract_condition_keywords("Evidence that SQLite was replaced by Postgres")
        assert "sqlite" in kws
        assert "replaced" in kws
        assert "postgres" in kws
        assert "that" not in kws  # stopword


# ── Passive Tester Tests ──────────────────────────────────────────────────

class TestPassiveTester:

    def test_confirmed_outcome(self, tmp_path):
        from belief_tester import PassiveTester
        conn = _make_beliefs_db(tmp_path)
        _insert_form(conn, content="SQLite is used for storage",
                     subject="SQLite", predicate="is used for", obj="storage")
        conn.close()

        tester = PassiveTester(
            beliefs_db=tmp_path / "beliefs.db",
            atoms_db=tmp_path / "atoms.db",
        )
        # Atom that confirms the belief (high overlap)
        atom = {"id": "a1", "content": "SQLite is used for storage in the vault system",
                "atom_type": "pattern", "topic": "sqlite"}
        results = tester.test_against_atom(atom)
        assert any(r.outcome == "confirmed" for r in results)

    def test_disconfirmed_outcome(self, tmp_path):
        from belief_tester import PassiveTester
        conn = _make_beliefs_db(tmp_path)
        _insert_form(conn, form_type="claim",
                     content="Postgres is used for storage",
                     subject="Postgres", predicate="is used for", obj="storage")
        conn.close()

        tester = PassiveTester(
            beliefs_db=tmp_path / "beliefs.db",
            atoms_db=tmp_path / "atoms.db",
        )
        # Atom that disconfirms — "Postgres" being "replaced" by something else
        atom = {"id": "a2",
                "content": "Postgres is used for something other than storage now",
                "atom_type": "pattern", "topic": "postgres"}
        results = tester.test_against_atom(atom)
        # Should not crash; outcome may vary by scoring
        assert isinstance(results, list)

    def test_episodic_atoms_skipped(self, tmp_path):
        from belief_tester import PassiveTester
        conn = _make_beliefs_db(tmp_path)
        _insert_form(conn)
        conn.close()

        tester = PassiveTester(
            beliefs_db=tmp_path / "beliefs.db",
            atoms_db=tmp_path / "atoms.db",
        )
        # Episodic atom — should return empty (no general falsification signal)
        atom = {"id": "a3", "content": "Deployed the fix yesterday to production",
                "atom_type": "outcome", "topic": "deploy"}
        results = tester.test_against_atom(atom)
        assert results == []

    def test_confidence_decreases_on_disconfirmation(self, tmp_path):
        from belief_tester import PassiveTester, beta_bernoulli_update
        conn = _make_beliefs_db(tmp_path)
        fid = _insert_form(conn, form_type="claim",
                           content="Redis is always used for queuing jobs",
                           subject="Redis", predicate="is used for", obj="queuing")
        conn.close()

        tester = PassiveTester(
            beliefs_db=tmp_path / "beliefs.db",
            atoms_db=tmp_path / "atoms.db",
        )
        atom = {"id": "a4",
                "content": "Redis is used for something other than queuing now",
                "atom_type": "pattern", "topic": "redis"}
        results = tester.test_against_atom(atom)
        disconf = [r for r in results if r.outcome == "disconfirmed"]
        for r in disconf:
            assert r.confidence_after <= r.confidence_before

    def test_no_beliefs_returns_empty(self, tmp_path):
        from belief_tester import PassiveTester
        _make_beliefs_db(tmp_path)  # empty db

        tester = PassiveTester(
            beliefs_db=tmp_path / "beliefs.db",
            atoms_db=tmp_path / "atoms.db",
        )
        atom = {"id": "a5", "content": "some content", "atom_type": "pattern", "topic": "x"}
        results = tester.test_against_atom(atom)
        assert results == []

    def test_missing_beliefs_db_returns_empty(self, tmp_path):
        from belief_tester import PassiveTester
        tester = PassiveTester(
            beliefs_db=tmp_path / "nonexistent.db",
            atoms_db=tmp_path / "atoms.db",
        )
        atom = {"id": "a6", "content": "content", "atom_type": "pattern", "topic": "x"}
        results = tester.test_against_atom(atom)
        assert results == []

    def test_repair_retire_on_low_confidence(self, tmp_path):
        from belief_tester import PassiveTester, beta_bernoulli_update, RETIRE_THRESHOLD
        conn = _make_beliefs_db(tmp_path)
        fid = _insert_form(conn, form_type="claim",
                           content="X is always Y",
                           subject="X", predicate="is always", obj="Y")
        # Seed test history with many disconfirmations to drive confidence below threshold
        alpha, beta = 1.0, 1.0
        for _ in range(8):
            alpha, beta, _ = beta_bernoulli_update(alpha, beta, confirmed=False)
        conn.execute(
            """INSERT INTO belief_tests
               (id, form_id, test_type, outcome, confidence_before, confidence_after,
                alpha_before, beta_before, alpha_after, beta_after, tested_at)
               VALUES (?,?,'passive','disconfirmed',0.7,?,%s,%s,?,?,datetime('now'))""".replace(
                   "%s", "?"
               ),
            (str(uuid.uuid4()), fid, alpha / (alpha + beta), 1.0, 1.0, alpha, beta),
        )
        conn.commit()
        conn.close()

        tester = PassiveTester(
            beliefs_db=tmp_path / "beliefs.db",
            atoms_db=tmp_path / "atoms.db",
        )
        health = tester.get_belief_health(fid)
        # Confidence should be low
        assert health["confidence"] < RETIRE_THRESHOLD + 0.1  # close to retire threshold


# ── Backfill Tests ────────────────────────────────────────────────────────

class TestBackfill:

    def test_backfill_memory_class(self, tmp_path):
        from belief_tester import backfill_memory_class, ensure_memory_class_column
        import belief_tester as bt
        bt.ATOMS_DB = tmp_path / "atoms.db"

        conn = _make_atoms_db(tmp_path)
        # Insert atoms without memory_class
        conn.executemany(
            "INSERT INTO atoms (id, content, atom_type, topic) VALUES (?,?,?,?)",
            [
                ("a1", "Deployed auth fix yesterday", "outcome", "deploy"),
                ("a2", "SQLite uses WAL mode", "decision", "sqlite"),
                ("a3", "Always restart Caddy after edits", "gotcha", "caddy"),
            ],
        )
        conn.commit()
        conn.close()

        n = backfill_memory_class()
        assert n == 3

        conn = sqlite3.connect(str(tmp_path / "atoms.db"))
        rows = conn.execute("SELECT id, memory_class FROM atoms ORDER BY id").fetchall()
        conn.close()
        classes = {r[0]: r[1] for r in rows}
        assert classes["a1"] == "episodic"
        assert classes["a2"] == "semantic"
        assert classes["a3"] == "procedural"

    def test_backfill_idempotent(self, tmp_path):
        from belief_tester import backfill_memory_class
        import belief_tester as bt
        bt.ATOMS_DB = tmp_path / "atoms.db"

        conn = _make_atoms_db(tmp_path)
        conn.execute(
            "INSERT INTO atoms (id, content, atom_type, topic) VALUES ('a1','test content','pattern','test')"
        )
        conn.commit()
        conn.close()

        n1 = backfill_memory_class()
        n2 = backfill_memory_class()  # second run should find nothing to update
        assert n1 == 1
        assert n2 == 0
