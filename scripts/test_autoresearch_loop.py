"""
Tests for autoresearch_loop.py — F1 measurement + Karpathy loop.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import autoresearch_loop as al


# --------------------------------------------------------------------------- #
# Token-level F1
# --------------------------------------------------------------------------- #

class TestTokenize:
    def test_lowercases(self):
        assert al.tokenize("FOO Bar") == ["foo", "bar"]

    def test_strips_punctuation(self):
        assert al.tokenize("hello, world!") == ["hello", "world"]

    def test_empty_returns_empty_list(self):
        assert al.tokenize("") == []


class TestF1:
    def test_identical_strings_score_one(self):
        assert al.f1_score("the quick brown fox", "the quick brown fox") == 1.0

    def test_zero_overlap_score_zero(self):
        assert al.f1_score("foo bar", "baz qux") == 0.0

    def test_partial_overlap_in_unit_interval(self):
        s = al.f1_score("the quick brown fox", "the fast brown dog")
        assert 0.0 < s < 1.0

    def test_empty_prediction(self):
        assert al.f1_score("", "the truth") == 0.0

    def test_average_f1_is_mean(self):
        pairs = [
            {"question": "q", "answer": "a b c", "prediction": "a b c"},  # 1.0
            {"question": "q", "answer": "a b c", "prediction": "x y z"},  # 0.0
        ]
        avg = al.average_f1(pairs)
        assert avg == 0.5


# --------------------------------------------------------------------------- #
# GOAL.md YAML frontmatter
# --------------------------------------------------------------------------- #

class TestGoalFile:
    def test_load_goal_md_parses_frontmatter(self, tmp_path):
        goal = tmp_path / "GOAL.md"
        goal.write_text(
            "---\n"
            "metric: f1\n"
            "current_best: 0.42\n"
            "current_params:\n"
            "  search_method: local\n"
            "  community_level: 0\n"
            "  top_k: 5\n"
            "search_space:\n"
            "  search_method: [local, global]\n"
            "  community_level: [0, 1]\n"
            "  top_k: [5, 10]\n"
            "---\n\n"
            "# Goal\nBody.\n"
        )
        g = al.load_goal(goal)
        assert g["metric"] == "f1"
        assert g["current_best"] == 0.42
        assert g["current_params"]["top_k"] == 5

    def test_write_goal_updates_current_best(self, tmp_path):
        goal = tmp_path / "GOAL.md"
        al.init_goal(goal, baseline_score=0.0)
        al.update_goal(goal, new_score=0.55, new_params={"search_method": "global", "community_level": 1, "top_k": 10})
        g = al.load_goal(goal)
        assert g["current_best"] == 0.55
        assert g["current_params"]["search_method"] == "global"

    def test_init_goal_creates_file_with_required_keys(self, tmp_path):
        goal = tmp_path / "GOAL.md"
        al.init_goal(goal, baseline_score=0.3)
        g = al.load_goal(goal)
        assert "metric" in g
        assert "current_best" in g
        assert "search_space" in g
        assert g["current_best"] == 0.3


# --------------------------------------------------------------------------- #
# Parameter sampling
# --------------------------------------------------------------------------- #

class TestSampleParams:
    def test_sampled_value_comes_from_space(self):
        space = {"search_method": ["local", "global"], "top_k": [5, 10, 20]}
        current = {"search_method": "local", "top_k": 5}
        import random
        rng = random.Random(0)
        new = al.sample_next_params(current, space, rng)
        # At least one parameter differs from current
        assert new != current
        # And new values are all from the space
        for k, v in new.items():
            assert v in space[k]

    def test_changes_exactly_one_parameter(self):
        space = {"search_method": ["local", "global"], "top_k": [5, 10]}
        current = {"search_method": "local", "top_k": 5}
        import random
        rng = random.Random(42)
        new = al.sample_next_params(current, space, rng)
        diffs = sum(1 for k in current if current[k] != new[k])
        assert diffs == 1


# --------------------------------------------------------------------------- #
# Loop — keeps improvements, discards regressions
# --------------------------------------------------------------------------- #

class TestLoop:
    def test_keeps_improving_params(self, tmp_path, monkeypatch):
        goal = tmp_path / "GOAL.md"
        al.init_goal(goal, baseline_score=0.3)

        # Stub the eval runner: return 0.9 for the first new params, 0.1 thereafter
        scores = iter([0.9, 0.1, 0.1])
        def fake_evaluate(params, eval_pairs, **kwargs):
            return next(scores)
        monkeypatch.setattr(al, "evaluate_params", fake_evaluate)

        import random
        rng = random.Random(0)
        al.run_loop(
            goal_path=goal,
            eval_pairs=[{"question": "q", "answer": "a"}],
            iterations=3,
            rng=rng,
        )
        g = al.load_goal(goal)
        assert g["current_best"] == 0.9  # first improvement stuck, later regressions ignored

    def test_runs_without_error_3_iterations(self, tmp_path, monkeypatch):
        goal = tmp_path / "GOAL.md"
        al.init_goal(goal, baseline_score=0.0)
        monkeypatch.setattr(al, "evaluate_params", lambda p, e, **k: 0.1)
        import random
        al.run_loop(goal, [{"question": "q", "answer": "a"}], iterations=3, rng=random.Random(0))
        # No exception → pass


# --------------------------------------------------------------------------- #
# Eval-only mode (baseline measurement)
# --------------------------------------------------------------------------- #

class TestEvalOnly:
    def test_eval_only_returns_numeric(self, monkeypatch):
        monkeypatch.setattr(al, "query_graphrag", lambda q, params, **k: "a b c")
        pairs = [
            {"question": "q1", "answer": "a b c"},
            {"question": "q2", "answer": "x y z"},
        ]
        params = {"search_method": "local", "community_level": 0, "top_k": 5}
        score = al.evaluate_params(params, pairs)
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0
