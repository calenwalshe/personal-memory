"""
Tests for build_eval_set.py — held-out QA generation.

Run: cd ~/memory/vault/scripts && python3 -m pytest test_build_eval_set.py -v
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import build_eval_set as bes


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _session(session_id: str, text: str = "example exchange text") -> dict:
    """Construct a synthetic session (list of events keyed by session_id)."""
    return {
        "session_id": session_id,
        "events": [
            {"role": "user", "text": f"user question in {session_id}",
             "timestamp": datetime(2026, 4, 11, tzinfo=timezone.utc),
             "session_id": session_id},
            {"role": "assistant", "text": f"assistant reply in {session_id}: {text}",
             "timestamp": datetime(2026, 4, 11, tzinfo=timezone.utc),
             "session_id": session_id},
        ],
    }


@pytest.fixture
def twenty_sessions():
    return [_session(f"s{i}") for i in range(20)]


# --------------------------------------------------------------------------- #
# split_sessions
# --------------------------------------------------------------------------- #

class TestSplitSessions:
    def test_deterministic_with_same_seed(self, twenty_sessions):
        a_corpus, a_holdout = bes.split_sessions(twenty_sessions, holdout_ratio=0.25, seed=42)
        b_corpus, b_holdout = bes.split_sessions(twenty_sessions, holdout_ratio=0.25, seed=42)
        assert [s["session_id"] for s in a_holdout] == [s["session_id"] for s in b_holdout]

    def test_different_seed_different_split(self, twenty_sessions):
        _, a = bes.split_sessions(twenty_sessions, holdout_ratio=0.25, seed=1)
        _, b = bes.split_sessions(twenty_sessions, holdout_ratio=0.25, seed=2)
        # Not guaranteed different, but extremely likely with n=20
        assert [s["session_id"] for s in a] != [s["session_id"] for s in b]

    def test_holdout_ratio_respected(self, twenty_sessions):
        corpus, holdout = bes.split_sessions(twenty_sessions, holdout_ratio=0.25, seed=42)
        assert len(holdout) == 5
        assert len(corpus) == 15

    def test_split_is_disjoint(self, twenty_sessions):
        corpus, holdout = bes.split_sessions(twenty_sessions, holdout_ratio=0.25, seed=42)
        corpus_ids = {s["session_id"] for s in corpus}
        holdout_ids = {s["session_id"] for s in holdout}
        assert corpus_ids.isdisjoint(holdout_ids)
        assert len(corpus_ids | holdout_ids) == 20

    def test_rejects_invalid_ratio(self):
        with pytest.raises(ValueError):
            bes.split_sessions([], holdout_ratio=1.5, seed=42)
        with pytest.raises(ValueError):
            bes.split_sessions([], holdout_ratio=-0.1, seed=42)


# --------------------------------------------------------------------------- #
# validate_qa_pair
# --------------------------------------------------------------------------- #

class TestValidateQaPair:
    def test_accepts_valid_pair(self):
        pair = {"question": "why does the chunker keep exchanges atomic?",
                "answer": "because splitting mid-exchange would destroy semantic coherence",
                "session_id": "s1"}
        assert bes.validate_qa_pair(pair) is True

    def test_rejects_missing_question(self):
        assert bes.validate_qa_pair({"answer": "a", "session_id": "s1"}) is False

    def test_rejects_missing_answer(self):
        assert bes.validate_qa_pair({"question": "q", "session_id": "s1"}) is False

    def test_rejects_missing_session_id(self):
        assert bes.validate_qa_pair({"question": "q", "answer": "a"}) is False

    def test_rejects_short_question(self):
        assert bes.validate_qa_pair({"question": "q?", "answer": "yes", "session_id": "s1"}) is False

    def test_rejects_empty_strings(self):
        assert bes.validate_qa_pair({"question": "", "answer": "", "session_id": "s1"}) is False


# --------------------------------------------------------------------------- #
# session_to_prompt_context
# --------------------------------------------------------------------------- #

class TestSessionContext:
    def test_produces_non_empty_string(self):
        s = _session("s1")
        ctx = bes.session_to_prompt_context(s)
        assert isinstance(ctx, str)
        assert len(ctx) > 0

    def test_includes_both_roles(self):
        s = _session("s1")
        ctx = bes.session_to_prompt_context(s)
        assert "user" in ctx.lower()
        assert "assistant" in ctx.lower()


# --------------------------------------------------------------------------- #
# generate_qa_pairs_for_session — uses mocked Anthropic client
# --------------------------------------------------------------------------- #

def _mock_anthropic_client(content_json: str):
    """Build a fake client whose messages.create returns a TextBlock with content_json."""
    client = MagicMock()
    response = MagicMock()
    text_block = MagicMock()
    text_block.text = content_json
    response.content = [text_block]
    client.messages.create.return_value = response
    return client


class TestGenerateQaPairs:
    def test_returns_list_of_valid_pairs(self):
        payload = json.dumps({
            "pairs": [
                {"question": "what is the chunker size?", "answer": "400 tokens with 80 overlap"},
                {"question": "which model provider is used?", "answer": "anthropic via litellm"},
            ]
        })
        client = _mock_anthropic_client(payload)
        pairs = bes.generate_qa_pairs_for_session(
            _session("s1"), client=client, n_pairs=2, model="claude-haiku-4-5-20251001"
        )
        assert len(pairs) == 2
        assert all(p["session_id"] == "s1" for p in pairs)
        assert all(bes.validate_qa_pair(p) for p in pairs)

    def test_parses_json_wrapped_in_prose(self):
        # Haiku often returns text before/after the JSON
        payload = 'Sure, here are pairs:\n```json\n{"pairs":[{"question":"what is the default target tokens value?","answer":"400 tokens"}]}\n```\nDone!'
        client = _mock_anthropic_client(payload)
        pairs = bes.generate_qa_pairs_for_session(
            _session("s1"), client=client, n_pairs=1, model="claude-haiku-4-5-20251001"
        )
        assert len(pairs) == 1

    def test_drops_invalid_pairs_from_model_response(self):
        payload = json.dumps({
            "pairs": [
                {"question": "valid question with enough content?", "answer": "valid answer content"},
                {"question": "", "answer": "a"},  # invalid
                {"question": "another valid query about configuration?", "answer": "a real answer"},
            ]
        })
        client = _mock_anthropic_client(payload)
        pairs = bes.generate_qa_pairs_for_session(
            _session("s1"), client=client, n_pairs=3, model="claude-haiku-4-5-20251001"
        )
        assert len(pairs) == 2

    def test_returns_empty_on_malformed_json(self):
        client = _mock_anthropic_client("not json at all")
        pairs = bes.generate_qa_pairs_for_session(
            _session("s1"), client=client, n_pairs=2, model="claude-haiku-4-5-20251001"
        )
        assert pairs == []


# --------------------------------------------------------------------------- #
# build_eval_set — end-to-end with mocked client
# --------------------------------------------------------------------------- #

class TestBuildEvalSet:
    def test_writes_jsonl_with_valid_pairs(self, tmp_path, twenty_sessions):
        payload = json.dumps({
            "pairs": [
                {"question": "sample question one for testing?", "answer": "sample answer one"},
                {"question": "sample question two for testing?", "answer": "sample answer two"},
            ]
        })
        client = _mock_anthropic_client(payload)
        out = tmp_path / "eval_set.jsonl"
        # holdout_ratio=0.25 → 5 of 20 sessions → 10 pairs
        n = bes.build_eval_set(
            twenty_sessions, output_path=out, client=client,
            holdout_ratio=0.25, pairs_per_session=2, seed=42,
            model="claude-haiku-4-5-20251001",
        )
        assert n == 10
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 10
        for line in lines:
            rec = json.loads(line)
            assert "question" in rec and "answer" in rec and "session_id" in rec

    def test_idempotent_rerun_adds_no_duplicates(self, tmp_path, twenty_sessions):
        payload = json.dumps({"pairs": [
            {"question": "repeatable question for this session?", "answer": "repeatable answer"},
        ]})
        client = _mock_anthropic_client(payload)
        out = tmp_path / "eval_set.jsonl"
        bes.build_eval_set(twenty_sessions, output_path=out, client=client,
                           holdout_ratio=0.25, pairs_per_session=1, seed=42,
                           model="claude-haiku-4-5-20251001")
        first_count = len(out.read_text().strip().split("\n"))
        bes.build_eval_set(twenty_sessions, output_path=out, client=client,
                           holdout_ratio=0.25, pairs_per_session=1, seed=42,
                           model="claude-haiku-4-5-20251001")
        second_count = len(out.read_text().strip().split("\n"))
        assert second_count == first_count

    def test_each_pair_carries_session_id(self, tmp_path, twenty_sessions):
        payload = json.dumps({"pairs": [
            {"question": "a question with sufficient length?", "answer": "an answer"},
        ]})
        client = _mock_anthropic_client(payload)
        out = tmp_path / "eval_set.jsonl"
        bes.build_eval_set(twenty_sessions, output_path=out, client=client,
                           holdout_ratio=0.25, pairs_per_session=1, seed=42,
                           model="claude-haiku-4-5-20251001")
        lines = [json.loads(l) for l in out.read_text().strip().split("\n")]
        session_ids = {l["session_id"] for l in lines}
        # 25% of 20 = 5 unique sessions
        assert len(session_ids) == 5
