"""
Tests for deep_consolidate.py — GraphRAG community reports → vault candidates.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

import deep_consolidate as dc


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def fake_graph_workspace(tmp_path):
    """Build a minimal graphrag output directory with the columns deep_consolidate reads."""
    root = tmp_path / "graph"
    output = root / "output"
    output.mkdir(parents=True)

    # 2 communities → 2 reports
    reports = pd.DataFrame([
        {
            "id": "rep1",
            "community": 0,
            "level": 0,
            "title": "TDD discipline pattern",
            "summary": "User consistently demands red-green-refactor TDD before implementation.",
            "full_content": "# TDD\n\nFull report on TDD discipline.",
            "rank": 8.5,
            "size": 7,
            "findings": [],
            "period": "2026-04-10",
        },
        {
            "id": "rep2",
            "community": 1,
            "level": 0,
            "title": "Weak one-shot community",
            "summary": "A community with low evidence.",
            "full_content": "# Weak",
            "rank": 3.0,
            "size": 2,
            "findings": [],
            "period": "2026-04-10",
        },
    ])
    reports.to_parquet(output / "community_reports.parquet")

    entities = pd.DataFrame([
        {"id": "ent1", "title": "TDD", "community": 0, "text_unit_ids": ["tu1", "tu2"]},
        {"id": "ent2", "title": "RED-GREEN", "community": 0, "text_unit_ids": ["tu1"]},
        {"id": "ent3", "title": "WEAK_THING", "community": 1, "text_unit_ids": ["tu3"]},
    ])
    entities.to_parquet(output / "entities.parquet")

    # text_units link back to chunk documents
    text_units = pd.DataFrame([
        {"id": "tu1", "document_ids": ["chunk-c1"]},
        {"id": "tu2", "document_ids": ["chunk-c2"]},
        {"id": "tu3", "document_ids": ["chunk-c3"]},
    ])
    text_units.to_parquet(output / "text_units.parquet")

    documents = pd.DataFrame([
        {"id": "chunk-c1", "title": "chunk-c1.txt"},
        {"id": "chunk-c2", "title": "chunk-c2.txt"},
        {"id": "chunk-c3", "title": "chunk-c3.txt"},
    ])
    documents.to_parquet(output / "documents.parquet")

    # chunk metadata: c1+c2 span 2 sessions × 3 weeks → passes gates
    # c3 is a single-session, single-day → fails gates
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    metadata = {
        "c1": {
            "session_ids": ["sA"],
            "exchange_ids": ["e1"],
            "first_timestamp": base.isoformat(),
            "last_timestamp": base.isoformat(),
        },
        "c2": {
            "session_ids": ["sB"],
            "exchange_ids": ["e2"],
            "first_timestamp": (base + timedelta(days=21)).isoformat(),
            "last_timestamp": (base + timedelta(days=21)).isoformat(),
        },
        "c3": {
            "session_ids": ["sC"],
            "exchange_ids": ["e3"],
            "first_timestamp": (base + timedelta(days=1)).isoformat(),
            "last_timestamp": (base + timedelta(days=1)).isoformat(),
        },
    }
    (root / "chunk_metadata.json").write_text(json.dumps(metadata))

    return root


# --------------------------------------------------------------------------- #
# Traceability: report → chunks → sessions → week span
# --------------------------------------------------------------------------- #

class TestTraceability:
    def test_loads_community_session_map(self, fake_graph_workspace):
        cs = dc.load_community_session_info(fake_graph_workspace)
        # Community 0 touches chunks c1, c2 → sessions sA, sB
        assert cs[0]["session_ids"] == {"sA", "sB"}
        # Community 1 touches chunk c3 → session sC
        assert cs[1]["session_ids"] == {"sC"}

    def test_week_span_across_communities(self, fake_graph_workspace):
        cs = dc.load_community_session_info(fake_graph_workspace)
        # Community 0: c1 at day 0, c2 at day 21 → 3-week span
        assert cs[0]["week_span"] >= 3
        # Community 1: single day
        assert cs[1]["week_span"] == 1


# --------------------------------------------------------------------------- #
# 6-signal scoring
# --------------------------------------------------------------------------- #

class TestScoring:
    def test_score_returns_float_in_unit_interval(self):
        signals = {
            "rank_normalized": 0.85,
            "size_normalized": 0.5,
            "session_count_normalized": 0.4,
            "week_span_normalized": 0.3,
            "entity_specificity": 0.6,
            "relationship_density_normalized": 0.5,
        }
        s = dc.score_signals(signals)
        assert 0.0 <= s <= 1.0

    def test_score_is_monotonic_in_rank(self):
        base = {
            "rank_normalized": 0.3,
            "size_normalized": 0.5,
            "session_count_normalized": 0.5,
            "week_span_normalized": 0.5,
            "entity_specificity": 0.5,
            "relationship_density_normalized": 0.5,
        }
        low = dc.score_signals(base)
        high = dc.score_signals({**base, "rank_normalized": 0.9})
        assert high > low

    def test_compute_signals_from_report(self, fake_graph_workspace):
        cs = dc.load_community_session_info(fake_graph_workspace)
        reports = pd.read_parquet(fake_graph_workspace / "output" / "community_reports.parquet")
        strong = reports[reports["community"] == 0].iloc[0]
        signals = dc.compute_signals(strong, cs[0])
        assert 0 <= signals["rank_normalized"] <= 1
        assert signals["session_count_normalized"] > 0


# --------------------------------------------------------------------------- #
# Gate: auto-promotion requires all three (score, session_count, week_span)
# --------------------------------------------------------------------------- #

class TestAutoPromoteGate:
    def test_passes_when_all_gates_met(self):
        assert dc.should_auto_promote(score=0.7, session_count=3, week_span=3) is True

    def test_fails_on_low_score(self):
        assert dc.should_auto_promote(score=0.5, session_count=3, week_span=3) is False

    def test_fails_on_single_session(self):
        assert dc.should_auto_promote(score=0.9, session_count=1, week_span=3) is False

    def test_fails_on_single_week(self):
        assert dc.should_auto_promote(score=0.9, session_count=3, week_span=1) is False

    def test_boundary_values(self):
        # score exactly 0.65, session_count 2, week_span 2 → passes
        assert dc.should_auto_promote(score=0.65, session_count=2, week_span=2) is True


# --------------------------------------------------------------------------- #
# Contradiction check (mocked Haiku)
# --------------------------------------------------------------------------- #

def _mock_client_saying(verdict: str):
    client = MagicMock()
    response = MagicMock()
    block = MagicMock()
    block.text = verdict
    response.content = [block]
    client.messages.create.return_value = response
    return client


class TestContradictionCheck:
    def test_no_contradiction_when_no_existing_threads(self, tmp_path):
        client = _mock_client_saying("NO")
        result = dc.contradicts_existing(
            candidate_summary="TDD is always required.",
            raw_threads_dir=tmp_path,
            client=client,
            model="test",
        )
        assert result is False
        # No threads → no API call
        assert client.messages.create.call_count == 0

    def test_detects_contradiction(self, tmp_path):
        thread = tmp_path / "thread.md"
        thread.write_text("TDD is optional for prototypes.")
        client = _mock_client_saying("YES - contradicts thread.md")
        result = dc.contradicts_existing(
            candidate_summary="TDD is mandatory always.",
            raw_threads_dir=tmp_path,
            client=client,
            model="test",
        )
        assert result is True

    def test_no_contradiction_when_model_says_no(self, tmp_path):
        thread = tmp_path / "thread.md"
        thread.write_text("Other unrelated content.")
        client = _mock_client_saying("NO")
        result = dc.contradicts_existing(
            candidate_summary="TDD.",
            raw_threads_dir=tmp_path,
            client=client,
            model="test",
        )
        assert result is False


# --------------------------------------------------------------------------- #
# Candidate writer — frontmatter schema + idempotency
# --------------------------------------------------------------------------- #

class TestWriteCandidate:
    def _cand(self):
        return {
            "id": "cand-gr-20260411-001",
            "title": "TDD discipline",
            "summary": "Red-green-refactor is mandatory.",
            "score": 0.72,
            "session_count": 3,
            "week_span": 4,
            "pattern_type": "behavior",
            "community_id": 0,
            "source_chunk_ids": ["c1", "c2"],
            "source_session_ids": ["sA", "sB", "sC"],
            "full_content": "# TDD\n\nFull body content.",
        }

    def test_writes_valid_frontmatter(self, tmp_path):
        import yaml
        dc.write_candidate(self._cand(), candidates_dir=tmp_path, auto_promoted=False)
        out = list(tmp_path.glob("cand-*.md"))
        assert len(out) == 1
        text = out[0].read_text()
        fm = yaml.safe_load(text.split("---")[1])
        # Contract-required fields
        for key in ("id", "type", "status", "pattern_type", "confidence", "suggested_vault_type"):
            assert key in fm, f"missing {key}"

    def test_auto_promoted_status_and_extra_fields(self, tmp_path):
        import yaml
        dc.write_candidate(self._cand(), candidates_dir=tmp_path, auto_promoted=True)
        out = next(tmp_path.glob("cand-*.md"))
        fm = yaml.safe_load(out.read_text().split("---")[1])
        assert fm["status"] == "auto-promoted"
        assert "auto_promoted_at" in fm
        assert fm["score"] == 0.72
        # source tag for grep in contract validator #4
        assert fm["source"] == "graphrag-corpus"

    def test_idempotent_rewrite(self, tmp_path):
        dc.write_candidate(self._cand(), candidates_dir=tmp_path, auto_promoted=False)
        dc.write_candidate(self._cand(), candidates_dir=tmp_path, auto_promoted=False)
        # Should still be one file (same id)
        assert len(list(tmp_path.glob("cand-*.md"))) == 1


# --------------------------------------------------------------------------- #
# Thread promoter — writes to raw/threads/ + appends to INDEX.md
# --------------------------------------------------------------------------- #

class TestPromoteToThreads:
    def test_writes_thread_with_source_tag(self, tmp_path):
        cand = {
            "id": "cand-gr-20260411-002",
            "title": "Sample",
            "summary": "body",
            "score": 0.8,
            "session_count": 2,
            "week_span": 2,
            "pattern_type": "behavior",
            "full_content": "full",
            "source_chunk_ids": [],
            "source_session_ids": [],
            "community_id": 0,
        }
        threads = tmp_path / "raw" / "threads"
        threads.mkdir(parents=True)
        index = tmp_path / "INDEX.md"
        index.write_text("# INDEX\n\n")
        dc.promote_to_threads(cand, threads_dir=threads, index_path=index)
        out = list(threads.glob("*.md"))
        assert len(out) == 1
        assert "source: graphrag-corpus" in out[0].read_text()
        assert "cand-gr-20260411-002" in index.read_text()

    def test_skips_if_already_in_index(self, tmp_path):
        cand = {
            "id": "cand-gr-20260411-003",
            "title": "Dup",
            "summary": "body",
            "score": 0.8,
            "session_count": 2,
            "week_span": 2,
            "pattern_type": "behavior",
            "full_content": "full",
            "source_chunk_ids": [],
            "source_session_ids": [],
            "community_id": 0,
        }
        threads = tmp_path / "raw" / "threads"
        threads.mkdir(parents=True)
        index = tmp_path / "INDEX.md"
        index.write_text("# INDEX\n\n- cand-gr-20260411-003\n")
        result = dc.promote_to_threads(cand, threads_dir=threads, index_path=index)
        assert result is False  # skipped
        assert len(list(threads.glob("*.md"))) == 0
