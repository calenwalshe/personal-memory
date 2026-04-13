"""
Tests for local_retrieval.py — embed community reports + top-K cosine retrieval.
"""
from unittest.mock import MagicMock
import numpy as np
import pandas as pd
import pytest

import local_retrieval as lr


class _FakeST:
    """Deterministic fake — fingerprint per-text, no network, 8-dim vectors."""
    def __init__(self, model_name=""):
        self.dim = 8
    def encode(self, texts, convert_to_numpy=True, show_progress_bar=False, normalize_embeddings=False, **_):
        vectors = []
        for t in texts:
            raw = (t.encode("utf-8") + b"\x00" * self.dim)[: self.dim]
            v = np.array([b / 255.0 for b in raw], dtype="float32")
            if normalize_embeddings:
                n = np.linalg.norm(v) or 1.0
                v = v / n
            vectors.append(v)
        return np.stack(vectors)


@pytest.fixture
def fake_model(monkeypatch):
    monkeypatch.setattr(lr, "_load_model", lambda name: _FakeST(name))
    return _FakeST()


@pytest.fixture
def sample_reports():
    return pd.DataFrame([
        {"id": "r0", "community": 0, "title": "Alpha", "summary": "about alpha", "full_content": "# Alpha\nalpha content apples"},
        {"id": "r1", "community": 1, "title": "Beta", "summary": "about beta", "full_content": "# Beta\nbeta content bananas"},
        {"id": "r2", "community": 2, "title": "Gamma", "summary": "about gamma", "full_content": "# Gamma\ngamma content grapes"},
        {"id": "r3", "community": 3, "title": "Delta", "summary": "about delta", "full_content": "# Delta\ndelta content dates"},
    ])


class TestEmbedReports:
    def test_returns_matrix_shape(self, fake_model, sample_reports):
        embeddings = lr.embed_reports(sample_reports, model_name="fake")
        assert embeddings.shape == (4, 8)

    def test_empty_df_returns_empty_matrix(self, fake_model):
        df = pd.DataFrame(columns=["id", "full_content"])
        embeddings = lr.embed_reports(df, model_name="fake")
        assert embeddings.shape == (0,) or embeddings.shape[0] == 0

    def test_embeddings_are_normalized(self, fake_model, sample_reports):
        embeddings = lr.embed_reports(sample_reports, model_name="fake")
        # Each row has unit norm (within fp tolerance)
        norms = np.linalg.norm(embeddings, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-5)


class TestRetrieveTopK:
    def test_returns_k_indices(self, fake_model, sample_reports):
        embs = lr.embed_reports(sample_reports, model_name="fake")
        q = lr.embed_query("alpha content", model_name="fake")
        idx = lr.retrieve_top_k(q, embs, k=2)
        assert len(idx) == 2
        assert all(0 <= i < 4 for i in idx)

    def test_top_k_ordered_by_similarity(self, fake_model, sample_reports):
        embs = lr.embed_reports(sample_reports, model_name="fake")
        q = lr.embed_query("# Alpha\nalpha content apples", model_name="fake")
        idx = lr.retrieve_top_k(q, embs, k=4)
        # The exact match for Alpha (index 0) should be first
        assert idx[0] == 0

    def test_k_larger_than_corpus_returns_all(self, fake_model, sample_reports):
        embs = lr.embed_reports(sample_reports, model_name="fake")
        q = lr.embed_query("anything", model_name="fake")
        idx = lr.retrieve_top_k(q, embs, k=100)
        assert len(idx) == 4

    def test_empty_corpus_returns_empty(self, fake_model):
        empty = np.zeros((0, 8), dtype="float32")
        q = lr.embed_query("anything", model_name="fake")
        idx = lr.retrieve_top_k(q, empty, k=5)
        assert len(idx) == 0


class TestBuildContextString:
    def test_concatenates_selected_reports(self, sample_reports):
        ctx = lr.build_context_string(sample_reports, [0, 2], max_chars=10000)
        assert "Alpha" in ctx
        assert "Gamma" in ctx
        assert "Beta" not in ctx

    def test_respects_max_chars(self, sample_reports):
        ctx = lr.build_context_string(sample_reports, [0, 1, 2, 3], max_chars=50)
        assert len(ctx) <= 60  # some slack for truncation marker

    def test_empty_indices_returns_empty_string(self, sample_reports):
        assert lr.build_context_string(sample_reports, [], max_chars=1000) == ""
