"""
Tests for claude_p_query.py — subprocess wrapper around `claude -p`.
"""
from unittest.mock import MagicMock, patch
import pytest

import claude_p_query as cpq


class TestBuildPrompt:
    def test_embeds_question_and_context(self):
        prompt = cpq.build_query_prompt(
            question="What is the default target tokens value?",
            context="## Chunker\nThe default target is 400 tokens with 80 overlap.",
        )
        assert "target tokens" in prompt
        assert "400 tokens" in prompt
        assert "Chunker" in prompt

    def test_instructs_concise_answer(self):
        prompt = cpq.build_query_prompt(question="q", context="c")
        assert "concise" in prompt.lower() or "one" in prompt.lower() or "short" in prompt.lower()


class TestQuerySingle:
    def test_returns_stdout_text(self, monkeypatch):
        fake_result = MagicMock(stdout="  the answer is 42  \n", returncode=0, stderr="")
        monkeypatch.setattr(cpq.subprocess, "run", lambda *a, **k: fake_result)
        ans = cpq.query_single(question="q", context="c")
        assert ans == "the answer is 42"

    def test_returns_empty_string_on_nonzero_exit(self, monkeypatch):
        fake_result = MagicMock(stdout="", returncode=1, stderr="oops")
        monkeypatch.setattr(cpq.subprocess, "run", lambda *a, **k: fake_result)
        ans = cpq.query_single(question="q", context="c")
        assert ans == ""

    def test_returns_empty_on_timeout(self, monkeypatch):
        def _fail(*a, **k):
            raise cpq.subprocess.TimeoutExpired(cmd="claude", timeout=5)
        monkeypatch.setattr(cpq.subprocess, "run", _fail)
        ans = cpq.query_single(question="q", context="c", timeout=5)
        assert ans == ""

    def test_strips_ansi_codes(self, monkeypatch):
        fake = MagicMock(stdout="\x1b[32manswer\x1b[0m\n", returncode=0, stderr="")
        monkeypatch.setattr(cpq.subprocess, "run", lambda *a, **k: fake)
        ans = cpq.query_single(question="q", context="c")
        assert ans == "answer"


class TestQueryBatch:
    def test_batch_returns_list_in_order(self, monkeypatch):
        # Return predictable stdout per-call based on the prompt content.
        def fake_run(cmd, input=None, **kwargs):
            # The prompt is the last element of the cmd list
            prompt = cmd[-1] if isinstance(cmd, list) else str(cmd)
            return MagicMock(
                stdout=f"answer to {'q1' if 'q1' in prompt else 'q2'}\n",
                returncode=0,
                stderr="",
            )
        monkeypatch.setattr(cpq.subprocess, "run", fake_run)
        questions = [
            {"question": "q1", "context": "c1"},
            {"question": "q2", "context": "c2"},
        ]
        results = cpq.query_batch(questions, max_workers=2)
        assert results[0] == "answer to q1"
        assert results[1] == "answer to q2"

    def test_batch_preserves_length(self, monkeypatch):
        fake = MagicMock(stdout="ok", returncode=0, stderr="")
        monkeypatch.setattr(cpq.subprocess, "run", lambda *a, **k: fake)
        questions = [{"question": f"q{i}", "context": "c"} for i in range(10)]
        results = cpq.query_batch(questions, max_workers=4)
        assert len(results) == 10
