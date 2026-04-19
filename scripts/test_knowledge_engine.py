"""
test_knowledge_engine.py — Tests for the knowledge consolidation engine.

Covers: source CRUD, intake adapters, atom extensions, belief store,
        inference rules, world assignment.
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Use temp directories for all DBs during tests
@pytest.fixture(autouse=True)
def temp_vault(tmp_path, monkeypatch):
    """Redirect all vault DBs to temp directory."""
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))

    # Patch module-level paths
    import source_store
    source_store.VAULT = tmp_path
    source_store.DB_PATH = tmp_path / "sources.db"

    import belief_store
    belief_store.VAULT = tmp_path
    belief_store.DB_PATH = tmp_path / "beliefs.db"

    # Init DBs
    source_store.init_sources_db()
    belief_store.init_beliefs_db()

    yield tmp_path


# ── Source Store Tests ────────────────────────────────────────────────────

class TestSourceStore:

    def test_create_source(self):
        from source_store import create_source, get_source
        sid = create_source(
            source_type="doc",
            title="Test Document",
            raw_content="Hello world",
            project="test-project",
        )
        assert sid
        src = get_source(sid)
        assert src["source_type"] == "doc"
        assert src["title"] == "Test Document"
        assert src["raw_content"] == "Hello world"
        assert src["project"] == "test-project"

    def test_invalid_source_type(self):
        from source_store import create_source
        with pytest.raises(ValueError, match="Invalid source_type"):
            create_source(source_type="invalid_type")

    def test_create_segment(self):
        from source_store import create_source, create_segment, get_segments
        sid = create_source(source_type="doc", title="Test")
        seg_id = create_segment(
            source_id=sid,
            segment_type="section",
            ordinal=0,
            content="First section content here",
            char_start=0,
            char_end=26,
        )
        assert seg_id
        segs = get_segments(sid)
        assert len(segs) == 1
        assert segs[0]["content"] == "First section content here"
        assert segs[0]["ordinal"] == 0

    def test_batch_segments(self):
        from source_store import create_source, create_segments_batch, get_segments
        sid = create_source(source_type="note", title="Test Note")
        batch = [
            {"segment_type": "paragraph", "ordinal": 0, "content": "Paragraph one with enough text"},
            {"segment_type": "paragraph", "ordinal": 1, "content": "Paragraph two with enough text"},
            {"segment_type": "paragraph", "ordinal": 2, "content": "Paragraph three with enough text"},
        ]
        ids = create_segments_batch(sid, batch)
        assert len(ids) == 3
        segs = get_segments(sid)
        assert len(segs) == 3
        assert segs[0]["ordinal"] == 0
        assert segs[2]["ordinal"] == 2

    def test_list_sources_filter(self):
        from source_store import create_source, list_sources
        create_source(source_type="doc", title="Doc 1", project="proj-a")
        create_source(source_type="note", title="Note 1", project="proj-a")
        create_source(source_type="doc", title="Doc 2", project="proj-b")

        all_sources = list_sources()
        assert len(all_sources) == 3

        docs = list_sources(source_type="doc")
        assert len(docs) == 2

        proj_a = list_sources(project="proj-a")
        assert len(proj_a) == 2

    def test_source_stats(self):
        from source_store import create_source, create_segment, source_stats
        sid = create_source(source_type="doc", title="Test", project="p1")
        create_segment(sid, "section", 0, "Content here for section one")
        create_segment(sid, "section", 1, "Content here for section two")

        stats = source_stats()
        assert stats["total_sources"] == 1
        assert stats["total_segments"] == 2
        assert stats["by_type"]["doc"] == 1


# ── Intake Adapter Tests ─────────────────────────────────────────────────

class TestIntakeDoc:

    def test_ingest_markdown(self, tmp_path):
        from intake_doc import ingest_document
        from source_store import get_source, get_segments

        doc = tmp_path / "test.md"
        doc.write_text("## Section One\n\nSome content here that is long enough.\n\n## Section Two\n\nMore content here that is also long enough.\n\n## Section Three\n\nEven more content here with sufficient length.\n")

        result = ingest_document(str(doc), project="test")
        assert result["segment_count"] >= 3
        assert result["char_count"] > 0

        src = get_source(result["source_id"])
        assert src["source_type"] == "doc"

        segs = get_segments(result["source_id"])
        assert len(segs) >= 3

    def test_ingest_plain_text(self, tmp_path):
        from intake_doc import ingest_document

        doc = tmp_path / "plain.txt"
        doc.write_text("First paragraph with enough text to be kept.\n\nSecond paragraph with enough text to be kept.\n\nThird paragraph with enough text to be kept.\n")

        result = ingest_document(str(doc))
        assert result["segment_count"] == 3


class TestIntakeNotes:

    def test_ingest_string(self):
        from intake_notes import ingest_note
        from source_store import get_source

        result = ingest_note(
            "This is a note about something important that I want to remember.",
            project="test",
            title="Test Note",
        )
        assert result["segment_count"] == 1

        src = get_source(result["source_id"])
        assert src["source_type"] == "note"
        assert src["title"] == "Test Note"

    def test_empty_note_raises(self):
        from intake_notes import ingest_note
        with pytest.raises(ValueError, match="empty"):
            ingest_note("")


# ── Belief Store Tests ───────────────────────────────────────────────────

class TestBeliefStore:

    def test_worlds_seeded(self):
        from belief_store import _conn
        conn = _conn()
        worlds = conn.execute("SELECT id FROM worlds").fetchall()
        conn.close()
        assert len(worlds) == 8

    def test_add_form(self):
        from belief_store import add_form, get_form
        fid = add_form(
            form_type="claim",
            content="SQLite uses WAL mode",
            subject="SQLite",
            predicate="uses",
            object_="WAL mode",
            project="test",
        )
        form = get_form(fid)
        assert form["form_type"] == "claim"
        assert form["content"] == "SQLite uses WAL mode"
        assert form["subject"] == "SQLite"

    def test_form_status(self):
        from belief_store import add_form, set_form_status, get_form_statuses
        fid = add_form(form_type="claim", content="Test claim")
        set_form_status(fid, "current", "active", set_by="test")
        statuses = get_form_statuses(fid)
        assert len(statuses) == 1
        assert statuses[0]["world_id"] == "current"
        assert statuses[0]["status"] == "active"

    def test_derived_object(self):
        from belief_store import add_form, add_derived, get_derived
        fid = add_form(form_type="claim", content="Test")
        did = add_derived(
            type_="stable_belief",
            content="Test is stable",
            source_form_ids=[fid],
            rule_fired="test_rule",
        )
        derived = get_derived(type_="stable_belief")
        assert len(derived) == 1
        assert derived[0]["content"] == "Test is stable"

    def test_explain_belief(self):
        from belief_store import add_form, set_form_status, explain_belief
        fid = add_form(form_type="claim", content="Explain me")
        set_form_status(fid, "current", "active", set_by="test")
        exp = explain_belief(fid)
        assert exp["form"]["content"] == "Explain me"
        assert len(exp["statuses"]) == 1


# ── Inference Rule Tests ─────────────────────────────────────────────────

class TestConflictDetection:

    def test_detects_conflict(self):
        from l3_module import ConflictDetectionRule

        forms = [
            {"id": "a", "form_type": "claim", "subject": "vault", "predicate": "uses",
             "object": "SQLite", "content": "vault uses SQLite", "confidence": 0.8,
             "entity_ids": "[]", "source_unit_ids": "[]"},
            {"id": "b", "form_type": "claim", "subject": "vault", "predicate": "uses",
             "object": "Postgres", "content": "vault uses Postgres", "confidence": 0.7,
             "entity_ids": "[]", "source_unit_ids": "[]"},
        ]
        rule = ConflictDetectionRule()
        firings = rule.evaluate(forms, [], [])
        assert len(firings) == 1
        assert firings[0].output_type == "contradiction"

    def test_no_conflict_same_object(self):
        from l3_module import ConflictDetectionRule

        forms = [
            {"id": "a", "form_type": "claim", "subject": "vault", "predicate": "uses",
             "object": "SQLite", "content": "vault uses SQLite", "confidence": 0.8,
             "entity_ids": "[]", "source_unit_ids": "[]"},
            {"id": "b", "form_type": "claim", "subject": "vault", "predicate": "uses",
             "object": "SQLite", "content": "vault also uses SQLite", "confidence": 0.7,
             "entity_ids": "[]", "source_unit_ids": "[]"},
        ]
        rule = ConflictDetectionRule()
        firings = rule.evaluate(forms, [], [])
        assert len(firings) == 0


class TestSupersession:

    def test_newer_supersedes_older(self):
        from l3_module import SupersessionRule

        forms = [
            {"id": "old", "form_type": "claim", "subject": "vault", "predicate": "version",
             "object": "1.0", "content": "vault is version 1.0", "confidence": 0.8,
             "extracted_at": "2026-04-01T00:00:00Z", "superseded_by": None,
             "entity_ids": "[]", "source_unit_ids": "[]"},
            {"id": "new", "form_type": "claim", "subject": "vault", "predicate": "version",
             "object": "2.0", "content": "vault is version 2.0", "confidence": 0.9,
             "extracted_at": "2026-04-15T00:00:00Z", "superseded_by": None,
             "entity_ids": "[]", "source_unit_ids": "[]"},
        ]
        rule = SupersessionRule()
        firings = rule.evaluate(forms, [], [])
        assert len(firings) == 1
        assert firings[0].action == "superseded"


class TestStablePromotion:

    def test_promotes_with_enough_evidence(self):
        from l3_module import StablePromotionRule

        forms = [
            {"id": "f1", "form_type": "claim", "content": "SQLite is reliable",
             "source_unit_id": "u1", "source_unit_ids": '["u1", "u2", "u3"]',
             "confidence": 0.8, "entity_ids": "[]"},
        ]
        rule = StablePromotionRule(threshold=3)
        firings = rule.evaluate(forms, [], [])
        assert len(firings) == 1
        assert firings[0].output_type == "stable_belief"

    def test_no_promotion_below_threshold(self):
        from l3_module import StablePromotionRule

        forms = [
            {"id": "f1", "form_type": "claim", "content": "maybe true",
             "source_unit_id": "u1", "source_unit_ids": '["u1"]',
             "confidence": 0.5, "entity_ids": "[]"},
        ]
        rule = StablePromotionRule(threshold=3)
        firings = rule.evaluate(forms, [], [])
        assert len(firings) == 0


class TestLessonExtraction:

    def test_extracts_lesson(self):
        from l3_module import LessonExtractionRule

        forms = [
            {"id": "fail1", "form_type": "warning", "content": "Playwright automation failed",
             "subject": "playwright", "entity_ids": '["playwright"]',
             "source_unit_ids": "[]", "confidence": 0.8},
            {"id": "dec1", "form_type": "decision", "content": "Use xdotool instead",
             "subject": "xdotool", "entity_ids": '["playwright", "xdotool"]',
             "source_unit_ids": "[]", "confidence": 0.9},
        ]
        rule = LessonExtractionRule()
        firings = rule.evaluate(forms, [], [])
        assert len(firings) == 1
        assert firings[0].output_type == "lesson"


# ── World Assignment Tests ───────────────────────────────────────────────

class TestWorldAssignment:

    def test_plan_goes_to_planned(self):
        from l3_engine import _assign_initial_world
        world = _assign_initial_world({"form_type": "plan"})
        assert world == "planned"

    def test_claim_goes_to_current(self):
        from l3_engine import _assign_initial_world
        world = _assign_initial_world({"form_type": "claim"})
        assert world == "current"

    def test_question_goes_to_possible(self):
        from l3_engine import _assign_initial_world
        world = _assign_initial_world({"form_type": "question"})
        assert world == "possible"

    def test_preference_goes_to_user_belief(self):
        from l3_engine import _assign_initial_world
        world = _assign_initial_world({"form_type": "preference"})
        assert world == "user_belief"
