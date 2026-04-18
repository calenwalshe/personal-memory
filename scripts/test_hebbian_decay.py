"""
Tests for apply_decay() in hebbian.py — 5 decay behaviour criteria.

Criterion mapping (from contract-001):
  (a) fired pair weight unchanged (no decay on firing pairs)
  (b) non-fired hebbian edge weight = pre * (1 - 0.03) after chunk
  (c) edge pruned (deleted) when post-decay weight < 0.01
  (d) co-occurrence baseline edge weights unchanged by decay
  (e) zero-atom chunk is a no-op
"""

import json
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent))

import hebbian as hb


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_graph_db(tmp_path: Path) -> Path:
    """Create a minimal graph.db at tmp_path/graph.db with required schema."""
    db_path = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE entities (
            id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL UNIQUE,
            entity_type TEXT NOT NULL DEFAULT 'concept',
            aliases TEXT DEFAULT '[]',
            description TEXT,
            first_seen TEXT NOT NULL DEFAULT '',
            last_seen TEXT NOT NULL DEFAULT '',
            atom_count INTEGER DEFAULT 1,
            atom_ids TEXT DEFAULT '[]',
            embedding BLOB,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE relations (
            id TEXT PRIMARY KEY,
            source_entity TEXT NOT NULL,
            target_entity TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            description TEXT,
            atom_ids TEXT DEFAULT '[]',
            first_seen TEXT NOT NULL DEFAULT '',
            last_seen TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            UNIQUE(source_entity, target_entity, relation_type)
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def _add_entity(conn, eid, name):
    conn.execute(
        "INSERT OR IGNORE INTO entities (id, canonical_name, entity_type, first_seen, last_seen, created_at, updated_at) VALUES (?, ?, 'concept', '', '', '', '')",
        (eid, name),
    )


def _add_hebbian_edge(conn, eid_a, eid_b, weight):
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO relations
           (id, source_entity, target_entity, relation_type, weight,
            description, atom_ids, first_seen, last_seen, created_at, updated_at)
           VALUES (?, ?, ?, 'related_to', ?, 'hebbian', '[]', '', '', '', '')""",
        (rid, eid_a, eid_b, weight),
    )
    return rid


def _add_cooccurrence_edge(conn, eid_a, eid_b, weight):
    """Non-hebbian baseline edge — description is NOT 'hebbian'."""
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO relations
           (id, source_entity, target_entity, relation_type, weight,
            description, atom_ids, first_seen, last_seen, created_at, updated_at)
           VALUES (?, ?, ?, 'related_to', ?, 'co-occurrence', '[]', '', '', '', '')""",
        (rid, eid_a, eid_b, weight),
    )
    return rid


def _get_weight(conn, rid):
    row = conn.execute("SELECT weight FROM relations WHERE id=?", (rid,)).fetchone()
    return row[0] if row else None


# ── tests ─────────────────────────────────────────────────────────────────────

class TestApplyDecay:

    def test_a_fired_entity_pair_not_decayed(self, tmp_path):
        """(a) Edges where either entity fired this session are excluded from decay."""
        db = _make_graph_db(tmp_path)
        conn = sqlite3.connect(str(db))

        eid_a = str(uuid.uuid4())
        eid_b = str(uuid.uuid4())
        _add_entity(conn, eid_a, "alpha")
        _add_entity(conn, eid_b, "beta")
        pre_weight = 5.0
        rid = _add_hebbian_edge(conn, eid_a, eid_b, pre_weight)
        conn.commit()
        conn.close()

        # Both entities fired — edge should NOT be decayed
        fired = {eid_a, eid_b}
        result = hb.apply_decay(fired, graph_db_path=db)

        conn = sqlite3.connect(str(db))
        post_weight = _get_weight(conn, rid)
        conn.close()

        assert post_weight == pytest.approx(pre_weight), (
            f"Fired edge weight changed: {pre_weight} → {post_weight}"
        )
        assert result["edges_decayed"] == 0
        assert result["edges_pruned"] == 0

    def test_b_non_fired_edge_decays_correctly(self, tmp_path):
        """(b) Non-firing hebbian edge weight = pre_weight * (1 - DECAY)."""
        db = _make_graph_db(tmp_path)
        conn = sqlite3.connect(str(db))

        eid_a = str(uuid.uuid4())
        eid_b = str(uuid.uuid4())
        _add_entity(conn, eid_a, "gamma")
        _add_entity(conn, eid_b, "delta")
        pre_weight = 4.0
        rid = _add_hebbian_edge(conn, eid_a, eid_b, pre_weight)
        conn.commit()
        conn.close()

        # Neither entity fired
        result = hb.apply_decay(set(), graph_db_path=db)

        conn = sqlite3.connect(str(db))
        post_weight = _get_weight(conn, rid)
        conn.close()

        expected = pre_weight * (1.0 - hb.DECAY)
        assert post_weight == pytest.approx(expected, rel=1e-6), (
            f"Expected {expected}, got {post_weight}"
        )
        assert result["edges_decayed"] == 1
        assert result["edges_pruned"] == 0

    def test_c_edge_pruned_when_below_min_weight(self, tmp_path):
        """(c) Edge with post-decay weight < MIN_WEIGHT=0.01 is deleted."""
        db = _make_graph_db(tmp_path)
        conn = sqlite3.connect(str(db))

        eid_a = str(uuid.uuid4())
        eid_b = str(uuid.uuid4())
        _add_entity(conn, eid_a, "epsilon")
        _add_entity(conn, eid_b, "zeta")
        # weight just above threshold — after 3% decay it will drop below 0.01
        pre_weight = hb.MIN_WEIGHT * 0.99  # 0.0099 — already below threshold before decay
        rid = _add_hebbian_edge(conn, eid_a, eid_b, pre_weight)
        conn.commit()
        conn.close()

        result = hb.apply_decay(set(), graph_db_path=db)

        conn = sqlite3.connect(str(db))
        post = _get_weight(conn, rid)
        count = conn.execute("SELECT COUNT(*) FROM relations WHERE id=?", (rid,)).fetchone()[0]
        conn.close()

        assert count == 0, "Edge should have been deleted (pruned)"
        assert result["edges_pruned"] == 1
        assert result["edges_decayed"] == 0

    def test_d_cooccurrence_baseline_unchanged(self, tmp_path):
        """(d) Non-hebbian (co-occurrence) edges are not touched by apply_decay."""
        db = _make_graph_db(tmp_path)
        conn = sqlite3.connect(str(db))

        eid_a = str(uuid.uuid4())
        eid_b = str(uuid.uuid4())
        _add_entity(conn, eid_a, "eta")
        _add_entity(conn, eid_b, "theta")
        baseline_weight = 7.0
        rid = _add_cooccurrence_edge(conn, eid_a, eid_b, baseline_weight)
        conn.commit()
        conn.close()

        # Run decay — no entities fired
        hb.apply_decay(set(), graph_db_path=db)

        conn = sqlite3.connect(str(db))
        post_weight = _get_weight(conn, rid)
        conn.close()

        assert post_weight == pytest.approx(baseline_weight), (
            f"Co-occurrence baseline edge should not be decayed: {baseline_weight} → {post_weight}"
        )

    def test_e_zero_atom_chunk_is_noop(self, tmp_path):
        """(e) Zero-atom chunk: update_from_atoms([]) returns empty fired set; apply_decay still runs correctly."""
        db = _make_graph_db(tmp_path)
        conn = sqlite3.connect(str(db))

        eid_a = str(uuid.uuid4())
        eid_b = str(uuid.uuid4())
        _add_entity(conn, eid_a, "iota")
        _add_entity(conn, eid_b, "kappa")
        pre_weight = 2.0
        rid = _add_hebbian_edge(conn, eid_a, eid_b, pre_weight)
        conn.commit()
        conn.close()

        # Simulate zero-atom chunk: update_from_atoms returns empty fired_entity_ids
        update_result = hb.update_from_atoms([])
        fired = update_result.pop("fired_entity_ids", set())
        assert fired == set(), "Zero-atom chunk should produce empty fired set"

        # apply_decay with empty fired set — edge should still decay
        decay_result = hb.apply_decay(fired, graph_db_path=db)

        conn = sqlite3.connect(str(db))
        post_weight = _get_weight(conn, rid)
        conn.close()

        expected = pre_weight * (1.0 - hb.DECAY)
        assert post_weight == pytest.approx(expected, rel=1e-6), (
            f"Expected decay to {expected}, got {post_weight}"
        )
        assert decay_result["edges_decayed"] == 1
