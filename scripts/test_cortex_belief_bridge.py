"""
test_cortex_belief_bridge.py — Tests for the Cortex belief bridge.

Covers: scope columns, query, promotion, dependency tracking, soft-fail.
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def temp_vault(tmp_path, monkeypatch):
    """Redirect all vault DBs to temp directory."""
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))

    import source_store
    source_store.VAULT = tmp_path
    source_store.DB_PATH = tmp_path / "sources.db"

    import belief_store
    belief_store.VAULT = tmp_path
    belief_store.DB_PATH = tmp_path / "beliefs.db"

    import cortex_belief_bridge as bridge
    bridge.VAULT = tmp_path
    bridge.BELIEFS_DB = tmp_path / "beliefs.db"
    bridge.SOURCES_DB = tmp_path / "sources.db"

    # Init DBs
    source_store.init_sources_db()
    conn = belief_store.init_beliefs_db()

    # Add scope columns
    conn.execute("ALTER TABLE logical_forms ADD COLUMN scope_type TEXT NOT NULL DEFAULT 'global'")
    conn.execute("ALTER TABLE logical_forms ADD COLUMN scope_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lf_scope ON logical_forms(scope_type, scope_id)")

    # Add derived_dependencies table
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS derived_dependencies (
            derived_object_id TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (derived_object_id, source_kind, source_id, role)
        );
        CREATE INDEX IF NOT EXISTS idx_dd_source ON derived_dependencies(source_kind, source_id);
        CREATE INDEX IF NOT EXISTS idx_dd_derived ON derived_dependencies(derived_object_id);
    """)
    conn.commit()
    conn.close()

    yield tmp_path


class TestScopeColumns:

    def test_global_scope_default(self):
        from belief_store import add_form, get_form
        fid = add_form(form_type="claim", content="Global fact")
        form = get_form(fid)
        assert form is not None

    def test_project_scope(self):
        from belief_store import _conn
        conn = _conn()
        conn.execute(
            "INSERT INTO logical_forms (id, form_type, content, extracted_at, scope_type, scope_id) VALUES (?,?,?,?,?,?)",
            ("test-1", "claim", "Project-scoped belief", "2026-04-20", "project", "test-slug"),
        )
        conn.commit()
        row = conn.execute("SELECT scope_type, scope_id FROM logical_forms WHERE id='test-1'").fetchone()
        conn.close()
        assert row["scope_type"] == "project"
        assert row["scope_id"] == "test-slug"


class TestQueryBeliefs:

    def test_returns_empty_when_no_beliefs(self):
        from cortex_belief_bridge import query_beliefs
        result = query_beliefs(topic="nonexistent")
        assert result["global_stable"] == []
        assert result["formatted"] == ""

    def test_returns_global_stable(self):
        from belief_store import add_form, set_form_status
        fid = add_form(form_type="claim", content="SQLite uses WAL mode")
        set_form_status(fid, "current", "stable", set_by="test")

        from cortex_belief_bridge import query_beliefs
        result = query_beliefs(topic="SQLite")
        assert len(result["global_stable"]) == 1
        assert "WAL" in result["global_stable"][0]["content"]

    def test_returns_project_scoped(self):
        from belief_store import add_form, set_form_status, _conn
        fid = add_form(form_type="decision", content="Use inline extraction")
        set_form_status(fid, "current", "active", set_by="test")

        conn = _conn()
        conn.execute("UPDATE logical_forms SET scope_type='project', scope_id='test-slug' WHERE id=?", (fid,))
        conn.commit()
        conn.close()

        from cortex_belief_bridge import query_beliefs
        result = query_beliefs(slug="test-slug")
        assert len(result["recurring"]) == 1

    def test_format_respects_char_limit(self):
        from belief_store import add_form, set_form_status
        for i in range(20):
            fid = add_form(form_type="claim", content=f"Belief number {i} " * 20)
            set_form_status(fid, "current", "stable", set_by="test")

        from cortex_belief_bridge import query_beliefs
        result = query_beliefs(max_results=20)
        assert len(result["formatted"]) <= 2100  # 2000 + truncation suffix


class TestPromotion:

    def test_promote_lessons_to_global(self):
        from belief_store import add_form, add_derived, set_form_status, _conn

        # Create a project-scoped form
        fid = add_form(form_type="claim", content="Inline is better than async")
        set_form_status(fid, "current", "stable", set_by="test")
        conn = _conn()
        conn.execute("UPDATE logical_forms SET scope_type='project', scope_id='test-slug' WHERE id=?", (fid,))
        conn.commit()
        conn.close()

        # Create a lesson derived from it
        did = add_derived(
            type_="lesson",
            content="Use inline extraction",
            source_form_ids=[fid],
            rule_fired="lesson_extraction",
            namespace="personal",
        )

        from cortex_belief_bridge import promote_on_close
        result = promote_on_close("test-slug")
        assert result.get("promoted", 0) >= 1

        # Verify form scope changed
        conn = _conn()
        row = conn.execute("SELECT scope_type FROM logical_forms WHERE id=?", (fid,)).fetchone()
        conn.close()
        assert row["scope_type"] == "global"


class TestDependencyTracking:

    def test_record_and_cascade(self):
        from belief_store import add_form, add_derived, set_form_status
        from cortex_belief_bridge import record_dependency, invalidate_dependents

        # Create a base form
        fid = add_form(form_type="claim", content="Base claim")
        set_form_status(fid, "current", "active", set_by="test")

        # Create a derived object depending on it
        did = add_derived(
            type_="stable_belief",
            content="Derived from base",
            source_form_ids=[fid],
            rule_fired="stable_promotion",
        )

        # Record the dependency
        assert record_dependency(did, "logical_form", fid, "support")

        # Invalidate the base form → should cascade
        result = invalidate_dependents(fid)
        assert result["invalidated"] == 1

        # Verify derived object is invalidated
        from belief_store import get_derived
        derived = get_derived(type_="stable_belief")
        assert len(derived) == 0  # filtered by active_only=True


class TestSoftFail:

    def test_query_with_missing_db(self, tmp_path, monkeypatch):
        import cortex_belief_bridge as bridge
        bridge.BELIEFS_DB = tmp_path / "nonexistent.db"
        bridge.SOURCES_DB = tmp_path / "nonexistent2.db"

        result = bridge.query_beliefs(topic="anything")
        assert result == {"global_stable": [], "recurring": [], "caution": [], "formatted": ""}

    def test_promote_with_missing_db(self, tmp_path, monkeypatch):
        import cortex_belief_bridge as bridge
        bridge.BELIEFS_DB = tmp_path / "nonexistent.db"

        result = bridge.promote_on_close("any-slug")
        assert result == {}

    def test_invalidate_with_missing_db(self, tmp_path, monkeypatch):
        import cortex_belief_bridge as bridge
        bridge.BELIEFS_DB = tmp_path / "nonexistent.db"

        result = bridge.invalidate_dependents("any-id")
        assert result == {}


class TestCrossProjectQuery:

    def test_three_stage_retrieval(self):
        from belief_store import add_form, set_form_status, _conn

        # Global stable
        fid1 = add_form(form_type="claim", content="Global truth about memory")
        set_form_status(fid1, "current", "stable", set_by="test")

        # Project-scoped
        fid2 = add_form(form_type="decision", content="Project decision about memory")
        set_form_status(fid2, "current", "active", set_by="test")
        conn = _conn()
        conn.execute("UPDATE logical_forms SET scope_type='project', scope_id='proj-a' WHERE id=?", (fid2,))
        conn.commit()
        conn.close()

        # Contested
        fid3 = add_form(form_type="claim", content="Contested memory claim")
        set_form_status(fid3, "contested", "active", set_by="test")

        from cortex_belief_bridge import query_beliefs
        result = query_beliefs(topic="memory", slug="proj-a")
        assert len(result["global_stable"]) >= 1
        assert len(result["recurring"]) >= 1
        assert len(result["caution"]) >= 1
