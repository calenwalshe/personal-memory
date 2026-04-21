"""
test_active_tester.py — Tests for active belief testing engine.

Covers: probe construction, probe execution (grep/path_exists/content_match),
outcome derivation, ActiveTester integration.
"""

import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def temp_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    import active_tester as at
    at.BELIEFS_DB = tmp_path / "beliefs.db"
    yield tmp_path


def _make_beliefs_db(tmp_path: Path) -> sqlite3.Connection:
    import belief_tester as bt
    conn = sqlite3.connect(str(tmp_path / "beliefs.db"))
    conn.row_factory = sqlite3.Row
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
    """)
    bt.init_belief_test_schema(conn)
    conn.commit()
    return conn


def _insert_form(conn, form_type="claim", content="SQLite is used for storage",
                 subject="SQLite", predicate="is used for", obj="storage",
                 project=None, fid=None) -> str:
    fid = fid or str(uuid.uuid4())
    conn.execute(
        """INSERT INTO logical_forms
           (id, form_type, content, subject, predicate, object, project, extracted_at, confidence)
           VALUES (?,?,?,?,?,?,?,datetime('now'),0.7)""",
        (fid, form_type, content, subject, predicate, obj, project),
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


# ── Probe construction tests ───────────────────────────────────────────────

class TestProbeConstruction:

    def test_claim_generates_subject_probe(self, tmp_path):
        from active_tester import build_probes_for_form
        form = {"form_type": "claim", "content": "SQLite is used for storage",
                "subject": "SQLite", "predicate": "is used for", "object": "storage",
                "project": None, "id": "f1"}
        probes = build_probes_for_form(form, [tmp_path])
        labels = " ".join(p.label for p in probes)
        assert "SQLite" in labels or "sqlite" in labels.lower()

    def test_claim_generates_object_probe(self, tmp_path):
        from active_tester import build_probes_for_form
        form = {"form_type": "claim", "content": "SQLite is used for storage",
                "subject": "SQLite", "predicate": "is used for", "object": "storage",
                "project": None, "id": "f1"}
        probes = build_probes_for_form(form, [tmp_path])
        labels = " ".join(p.label for p in probes)
        assert "storage" in labels

    def test_claim_generates_replacement_probe(self, tmp_path):
        from active_tester import build_probes_for_form
        form = {"form_type": "claim", "content": "SQLite is used for storage",
                "subject": "SQLite", "predicate": "is used for", "object": "storage",
                "project": None, "id": "f1"}
        probes = build_probes_for_form(form, [tmp_path])
        # Should include a probe looking for replacement/removal signals
        patterns = " ".join(p.pattern for p in probes)
        assert "replace" in patterns.lower() or "remov" in patterns.lower() or "migrat" in patterns.lower()

    def test_decision_generates_keyword_probes(self, tmp_path):
        from active_tester import build_probes_for_form
        form = {"form_type": "decision", "content": "Chose Redis for job queuing",
                "subject": None, "predicate": None, "object": None,
                "project": None, "id": "f2"}
        probes = build_probes_for_form(form, [tmp_path])
        assert len(probes) >= 1
        patterns = " ".join(p.pattern for p in probes)
        # Should include meaningful keywords like Redis or queuing
        assert "Redis" in patterns or "queuing" in patterns or "job" in patterns

    def test_no_probes_for_empty_form(self, tmp_path):
        from active_tester import build_probes_for_form
        form = {"form_type": "claim", "content": "",
                "subject": "", "predicate": "", "object": "",
                "project": None, "id": "f3"}
        probes = build_probes_for_form(form, [tmp_path])
        # Should still get some probes or none, but not crash
        assert isinstance(probes, list)


# ── Probe execution tests ──────────────────────────────────────────────────

class TestProbeExecution:

    def test_grep_finds_keyword_in_file(self, tmp_path):
        from active_tester import Probe, run_probe
        (tmp_path / "test.py").write_text("# SQLite is used here\nconn = sqlite3.connect(db)\n")
        probe = Probe(probe_type="grep", target=str(tmp_path),
                      pattern="SQLite", label="find SQLite")
        result = run_probe(probe)
        assert result.found is True
        assert result.match_count >= 1
        assert "SQLite" in result.sample or "sqlite" in result.sample.lower()

    def test_grep_misses_absent_keyword(self, tmp_path):
        from active_tester import Probe, run_probe
        (tmp_path / "test.py").write_text("# Postgres is used here\n")
        probe = Probe(probe_type="grep", target=str(tmp_path),
                      pattern="SQLite", label="find SQLite")
        result = run_probe(probe)
        assert result.found is False
        assert result.match_count == 0

    def test_grep_missing_root_returns_error(self, tmp_path):
        from active_tester import Probe, run_probe
        probe = Probe(probe_type="grep", target=str(tmp_path / "nonexistent"),
                      pattern="anything", label="probe")
        result = run_probe(probe)
        assert result.error is not None

    def test_path_exists_found(self, tmp_path):
        from active_tester import Probe, run_probe
        fp = tmp_path / "beliefs.db"
        fp.write_text("x")
        probe = Probe(probe_type="path_exists", target=str(fp),
                      pattern="", label="check file exists")
        result = run_probe(probe)
        assert result.found is True

    def test_path_exists_not_found(self, tmp_path):
        from active_tester import Probe, run_probe
        probe = Probe(probe_type="path_exists", target=str(tmp_path / "missing.db"),
                      pattern="", label="check file exists")
        result = run_probe(probe)
        assert result.found is False

    def test_content_match_found(self, tmp_path):
        from active_tester import Probe, run_probe
        fp = tmp_path / "config.py"
        fp.write_text("DB_ENGINE = 'sqlite'\nWAL_MODE = True\n")
        probe = Probe(probe_type="content_match", target=str(fp),
                      pattern="sqlite", label="sqlite in config")
        result = run_probe(probe)
        assert result.found is True

    def test_content_match_missing_file(self, tmp_path):
        from active_tester import Probe, run_probe
        probe = Probe(probe_type="content_match", target=str(tmp_path / "nofile.py"),
                      pattern="sqlite", label="probe")
        result = run_probe(probe)
        assert result.error is not None

    def test_unknown_probe_type_returns_error(self, tmp_path):
        from active_tester import Probe, run_probe
        probe = Probe(probe_type="magic", target=str(tmp_path),
                      pattern="x", label="probe")
        result = run_probe(probe)
        assert result.error is not None


# ── Outcome derivation tests ───────────────────────────────────────────────

class TestOutcomeDerivation:

    def _form(self) -> dict:
        return {"id": "f1", "form_type": "claim", "content": "SQLite used for storage",
                "subject": "SQLite", "predicate": "used for", "object": "storage",
                "confidence": 0.7}

    def test_confirmed_when_non_negate_probe_found(self):
        from active_tester import Probe, ProbeResult, derive_outcome
        probe = Probe(probe_type="grep", target="/", pattern="SQLite", label="found SQLite")
        r = ProbeResult(probe=probe, found=True, match_count=3, sample="import sqlite3")
        result = derive_outcome(self._form(), [r], conf_before=0.7)
        assert result.outcome == "confirmed"
        assert result.confidence_after >= result.confidence_before

    def test_disconfirmed_when_negate_probe_found(self):
        from active_tester import Probe, ProbeResult, derive_outcome
        probe = Probe(probe_type="grep", target="/", pattern="replace.*SQLite",
                      label="SQLite replaced", negate=True)
        r = ProbeResult(probe=probe, found=True, match_count=1, sample="replace SQLite with Postgres")
        result = derive_outcome(self._form(), [r], conf_before=0.7)
        assert result.outcome == "disconfirmed"
        assert result.confidence_after <= result.confidence_before

    def test_inconclusive_when_no_probe_matches(self):
        from active_tester import Probe, ProbeResult, derive_outcome
        probe = Probe(probe_type="grep", target="/", pattern="SQLite", label="find SQLite")
        r = ProbeResult(probe=probe, found=False, match_count=0, sample="")
        result = derive_outcome(self._form(), [r], conf_before=0.7)
        assert result.outcome == "inconclusive"
        assert result.confidence_after == result.confidence_before

    def test_error_when_all_probes_fail(self):
        from active_tester import Probe, ProbeResult, derive_outcome
        probe = Probe(probe_type="grep", target="/", pattern="x", label="probe")
        r = ProbeResult(probe=probe, found=False, match_count=0,
                        sample="", error="root not found")
        result = derive_outcome(self._form(), [r], conf_before=0.7)
        assert result.outcome == "error"

    def test_no_probes_returns_inconclusive(self):
        from active_tester import derive_outcome
        result = derive_outcome(self._form(), [], conf_before=0.7)
        assert result.outcome == "inconclusive"


# ── ActiveTester integration tests ────────────────────────────────────────

class TestActiveTester:

    def test_test_belief_returns_result(self, tmp_path):
        from active_tester import ActiveTester
        (tmp_path / "code.py").write_text("import sqlite3\nconn = sqlite3.connect('vault.db')\n")

        conn = _make_beliefs_db(tmp_path)
        fid = _insert_form(conn, content="SQLite is used for storage",
                           subject="SQLite", predicate="is used for", obj="storage")
        conn.close()

        tester = ActiveTester(
            beliefs_db=tmp_path / "beliefs.db",
            search_roots=[tmp_path],
        )
        form = {"id": fid, "form_type": "claim",
                "content": "SQLite is used for storage",
                "subject": "SQLite", "predicate": "is used for", "object": "storage",
                "project": None, "confidence": 0.7}
        result = tester.test_belief(form)
        assert result.outcome in ("confirmed", "disconfirmed", "inconclusive", "error")
        assert result.form_id == fid

    def test_missing_db_still_runs_probes(self, tmp_path):
        from active_tester import ActiveTester
        (tmp_path / "src.py").write_text("# test\n")
        tester = ActiveTester(
            beliefs_db=tmp_path / "nonexistent.db",
            search_roots=[tmp_path],
        )
        form = {"id": "x", "form_type": "claim", "content": "Redis is used",
                "subject": "Redis", "predicate": "is used", "object": None,
                "project": None, "confidence": 0.7}
        result = tester.test_belief(form)
        # No crash — result may be inconclusive if Redis not in files
        assert result.outcome in ("confirmed", "disconfirmed", "inconclusive", "error")

    def test_run_batch_empty_db(self, tmp_path):
        from active_tester import ActiveTester
        _make_beliefs_db(tmp_path)  # no forms
        tester = ActiveTester(
            beliefs_db=tmp_path / "beliefs.db",
            search_roots=[tmp_path],
        )
        summary = tester.run_batch(limit=5)
        assert summary["tested"] == 0

    def test_run_batch_processes_forms(self, tmp_path):
        from active_tester import ActiveTester
        (tmp_path / "app.py").write_text("import sqlite3\nUSE_REDIS = False\n")
        conn = _make_beliefs_db(tmp_path)
        _insert_form(conn, content="SQLite is used for storage",
                     subject="SQLite", predicate="used for", obj="storage")
        _insert_form(conn, form_type="decision", content="Redis chosen for queuing",
                     subject="Redis", predicate="chosen for", obj="queuing")
        conn.close()

        tester = ActiveTester(
            beliefs_db=tmp_path / "beliefs.db",
            search_roots=[tmp_path],
        )
        summary = tester.run_batch(limit=10)
        assert summary["tested"] == 2
        total = summary["confirmed"] + summary["disconfirmed"] + summary["inconclusive"] + summary.get("error", 0)
        assert total == 2
