"""
Tests for extract_sessions.py — exchange-level chunker + GraphRAG input writer.

Run: cd ~/memory/vault/scripts && python3 -m pytest test_extract_sessions.py -v
"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

import extract_sessions as es


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _event(role: str, text: str, minute: int = 0, session_id: str = "sess-a") -> dict:
    return {
        "role": role,
        "text": text,
        "timestamp": datetime(2026, 4, 11, 10, minute, tzinfo=timezone.utc),
        "session_id": session_id,
    }


@pytest.fixture
def simple_exchange_pair():
    # Two clean user→assistant exchanges
    return [
        _event("user", "how do I run the pipeline", minute=0),
        _event("assistant", "call extract_sessions.py with --input-dir", minute=1),
        _event("user", "what about overlap", minute=2),
        _event("assistant", "use --overlap-tokens 80", minute=3),
    ]


@pytest.fixture
def orphan_user_event():
    # User message with no assistant reply — should not form an exchange
    return [
        _event("user", "dangling question", minute=0),
        _event("user", "followup before any reply", minute=1),
        _event("assistant", "here is a reply to the followup", minute=2),
    ]


# --------------------------------------------------------------------------- #
# estimate_tokens
# --------------------------------------------------------------------------- #

class TestEstimateTokens:
    def test_empty_string_is_zero(self):
        assert es.estimate_tokens("") == 0

    def test_short_text_rounds_up(self):
        # "hello world" = 11 chars → roughly 2-3 tokens with /4 heuristic
        assert es.estimate_tokens("hello world") >= 2

    def test_monotonic(self):
        assert es.estimate_tokens("a" * 400) < es.estimate_tokens("a" * 800)

    def test_roughly_matches_char_quarter(self):
        # 1600 chars → ~400 tokens (±20%)
        est = es.estimate_tokens("x" * 1600)
        assert 320 <= est <= 480


# --------------------------------------------------------------------------- #
# iter_exchanges
# --------------------------------------------------------------------------- #

class TestIterExchanges:
    def test_produces_two_exchanges_from_two_pairs(self, simple_exchange_pair):
        exchanges = list(es.iter_exchanges(simple_exchange_pair))
        assert len(exchanges) == 2

    def test_each_exchange_has_user_and_assistant(self, simple_exchange_pair):
        exchanges = list(es.iter_exchanges(simple_exchange_pair))
        for ex in exchanges:
            assert ex["user"]["role"] == "user"
            assert ex["assistant"]["role"] == "assistant"

    def test_drops_orphan_user_keeps_paired(self, orphan_user_event):
        # First user has no assistant (next event is another user) → orphan, dropped.
        # Second user has an assistant reply → kept.
        exchanges = list(es.iter_exchanges(orphan_user_event))
        assert len(exchanges) == 1
        assert "followup" in exchanges[0]["user"]["text"]

    def test_empty_input_returns_empty(self):
        assert list(es.iter_exchanges([])) == []

    def test_exchange_carries_session_id_and_start_timestamp(self, simple_exchange_pair):
        exchanges = list(es.iter_exchanges(simple_exchange_pair))
        assert exchanges[0]["session_id"] == "sess-a"
        assert exchanges[0]["timestamp"] == simple_exchange_pair[0]["timestamp"]


# --------------------------------------------------------------------------- #
# chunk_exchanges (the core 400/80 chunker)
# --------------------------------------------------------------------------- #

def _make_exchange(user_text: str, asst_text: str, minute: int = 0, sid: str = "s1") -> dict:
    return {
        "user": _event("user", user_text, minute=minute, session_id=sid),
        "assistant": _event("assistant", asst_text, minute=minute + 1, session_id=sid),
        "session_id": sid,
        "timestamp": datetime(2026, 4, 11, 10, minute, tzinfo=timezone.utc),
    }


class TestChunkExchanges:
    def test_single_small_exchange_one_chunk(self):
        ex = [_make_exchange("hi", "hello", minute=0)]
        chunks = es.chunk_exchanges(ex, target_tokens=400, overlap_tokens=80)
        assert len(chunks) == 1
        assert "hi" in chunks[0]["text"]
        assert "hello" in chunks[0]["text"]

    def test_many_small_exchanges_merged_under_target(self):
        # 10 tiny exchanges ~5 tokens each → should fit in 1 chunk ≤ 400
        exchanges = [_make_exchange(f"q{i}", f"a{i}", minute=i) for i in range(10)]
        chunks = es.chunk_exchanges(exchanges, target_tokens=400, overlap_tokens=80)
        assert len(chunks) == 1

    def test_respects_target_tokens_boundary(self):
        # Each exchange ~100 tokens (400 chars user + 400 chars assistant ≈ 200 tokens).
        # With target 400, ~2 exchanges per chunk.
        big = [_make_exchange("u" * 400, "a" * 400, minute=i) for i in range(6)]
        chunks = es.chunk_exchanges(big, target_tokens=400, overlap_tokens=80)
        assert len(chunks) >= 2
        for c in chunks:
            # Each chunk may slightly exceed target since exchanges are atomic,
            # but should not be >2x target.
            assert es.estimate_tokens(c["text"]) <= 800

    def test_never_splits_mid_exchange(self):
        # A single massive exchange (2000 tokens) should stay in one chunk
        # even though it exceeds target — exchanges are atomic.
        huge = [_make_exchange("u" * 4000, "a" * 4000, minute=0)]
        chunks = es.chunk_exchanges(huge, target_tokens=400, overlap_tokens=80)
        assert len(chunks) == 1
        assert "u" * 4000 in chunks[0]["text"]
        assert "a" * 4000 in chunks[0]["text"]

    def test_overlap_reuses_trailing_exchange(self):
        # ~200 token exchanges. With target 400 and overlap 80, consecutive chunks
        # should share at least the last exchange of the prior chunk.
        exchanges = [_make_exchange("u" * 400, "a" * 400, minute=i) for i in range(6)]
        chunks = es.chunk_exchanges(exchanges, target_tokens=400, overlap_tokens=80)
        assert len(chunks) >= 2
        # Overlap: last exchange of chunk[0] should appear at start of chunk[1]
        last_of_first = chunks[0]["exchange_ids"][-1]
        assert last_of_first in chunks[1]["exchange_ids"]

    def test_no_overlap_when_only_one_chunk(self):
        ex = [_make_exchange("hi", "hello")]
        chunks = es.chunk_exchanges(ex, target_tokens=400, overlap_tokens=80)
        assert len(chunks) == 1

    def test_chunk_has_stable_id(self):
        ex = [_make_exchange("hi", "hello", minute=0)]
        chunks1 = es.chunk_exchanges(ex, target_tokens=400, overlap_tokens=80)
        chunks2 = es.chunk_exchanges(ex, target_tokens=400, overlap_tokens=80)
        assert chunks1[0]["id"] == chunks2[0]["id"]

    def test_chunk_id_changes_with_content(self):
        ex1 = [_make_exchange("hi", "hello", minute=0)]
        ex2 = [_make_exchange("hi", "goodbye", minute=0)]
        c1 = es.chunk_exchanges(ex1, 400, 80)
        c2 = es.chunk_exchanges(ex2, 400, 80)
        assert c1[0]["id"] != c2[0]["id"]

    def test_chunks_carry_session_ids(self):
        exchanges = [
            _make_exchange("u" * 400, "a" * 400, minute=0, sid="sA"),
            _make_exchange("u" * 400, "a" * 400, minute=5, sid="sB"),
        ]
        chunks = es.chunk_exchanges(exchanges, target_tokens=400, overlap_tokens=80)
        all_sids = set()
        for c in chunks:
            all_sids.update(c["session_ids"])
        assert {"sA", "sB"} <= all_sids


# --------------------------------------------------------------------------- #
# write_graphrag_input — writes one .txt per chunk under graph/input/
# --------------------------------------------------------------------------- #

class TestWriteGraphragInput:
    def test_creates_input_dir_and_files(self, tmp_path):
        chunks = [
            {"id": "c1", "text": "chunk one content", "session_ids": ["s1"], "exchange_ids": ["e1"]},
            {"id": "c2", "text": "chunk two content", "session_ids": ["s2"], "exchange_ids": ["e2"]},
        ]
        es.write_graphrag_input(chunks, tmp_path / "graph")
        input_dir = tmp_path / "graph" / "input"
        assert input_dir.is_dir()
        written = sorted(input_dir.glob("*.txt"))
        assert len(written) == 2

    def test_idempotent_on_rerun(self, tmp_path):
        chunks = [{"id": "c1", "text": "same content", "session_ids": ["s1"], "exchange_ids": ["e1"]}]
        es.write_graphrag_input(chunks, tmp_path / "graph")
        es.write_graphrag_input(chunks, tmp_path / "graph")
        written = list((tmp_path / "graph" / "input").glob("*.txt"))
        assert len(written) == 1

    def test_file_contains_chunk_text(self, tmp_path):
        chunks = [{"id": "c1", "text": "distinctive marker XYZ", "session_ids": ["s1"], "exchange_ids": ["e1"]}]
        es.write_graphrag_input(chunks, tmp_path / "graph")
        f = next((tmp_path / "graph" / "input").glob("*.txt"))
        assert "distinctive marker XYZ" in f.read_text()

    def test_writes_chunk_metadata_json(self, tmp_path):
        import json as _json
        chunks = [
            {"id": "c1", "text": "alpha", "session_ids": ["s1", "s2"], "exchange_ids": ["e1"], "first_timestamp": "2026-04-10T10:00:00Z"},
            {"id": "c2", "text": "beta", "session_ids": ["s3"], "exchange_ids": ["e2"], "first_timestamp": "2026-04-11T10:00:00Z"},
        ]
        es.write_graphrag_input(chunks, tmp_path / "graph")
        meta_path = tmp_path / "graph" / "chunk_metadata.json"
        assert meta_path.exists()
        meta = _json.loads(meta_path.read_text())
        assert "c1" in meta and "c2" in meta
        assert meta["c1"]["session_ids"] == ["s1", "s2"]
        assert meta["c2"]["session_ids"] == ["s3"]


# --------------------------------------------------------------------------- #
# bootstrap_workspace — writes settings.yaml and prompts dir
# --------------------------------------------------------------------------- #

class TestBootstrapWorkspace:
    def test_creates_settings_yaml(self, tmp_path):
        es.bootstrap_workspace(tmp_path / "graph")
        assert (tmp_path / "graph" / "settings.yaml").exists()

    def test_settings_has_anthropic_completion_provider(self, tmp_path):
        import yaml
        es.bootstrap_workspace(tmp_path / "graph")
        data = yaml.safe_load((tmp_path / "graph" / "settings.yaml").read_text())
        cm = data["completion_models"]["default_completion_model"]
        assert cm["model_provider"] == "anthropic"
        assert "claude-haiku" in cm["model"]

    def test_settings_has_local_st_embedding(self, tmp_path):
        import yaml
        es.bootstrap_workspace(tmp_path / "graph")
        data = yaml.safe_load((tmp_path / "graph" / "settings.yaml").read_text())
        em = data["embedding_models"]["default_embedding_model"]
        assert em["type"] == "local_st"
        # Using a small, popular ST model so first run doesn't download 1GB+
        assert em["model"]  # non-empty

    def test_settings_has_input_and_output_storage(self, tmp_path):
        import yaml
        es.bootstrap_workspace(tmp_path / "graph")
        data = yaml.safe_load((tmp_path / "graph" / "settings.yaml").read_text())
        assert data["input_storage"]["base_dir"] == "input"
        assert data["output_storage"]["base_dir"] == "output"

    def test_idempotent(self, tmp_path):
        es.bootstrap_workspace(tmp_path / "graph")
        first = (tmp_path / "graph" / "settings.yaml").read_text()
        es.bootstrap_workspace(tmp_path / "graph")
        second = (tmp_path / "graph" / "settings.yaml").read_text()
        assert first == second

    def test_creates_input_dir(self, tmp_path):
        es.bootstrap_workspace(tmp_path / "graph")
        assert (tmp_path / "graph" / "input").is_dir()

    def test_creates_prompts_dir_with_default_prompts(self, tmp_path):
        # graphrag needs the prompts/ dir populated with prompt files
        # referenced by settings.yaml; bootstrap should provide defaults.
        es.bootstrap_workspace(tmp_path / "graph")
        prompts_dir = tmp_path / "graph" / "prompts"
        assert prompts_dir.is_dir()
        # At least the extract_graph + community report prompts
        assert (prompts_dir / "extract_graph.txt").exists()
