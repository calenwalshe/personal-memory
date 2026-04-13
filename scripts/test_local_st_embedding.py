"""
Tests for local_st_embedding.LocalSTEmbedding — local sentence-transformers
adapter that satisfies graphrag_llm.LLMEmbedding.

Run: cd ~/memory/vault/scripts && python3 -m pytest test_local_st_embedding.py -v
"""
from unittest.mock import MagicMock

import pytest


# --------------------------------------------------------------------------- #
# A small fake sentence-transformers model — avoids downloading weights during
# unit tests and guarantees deterministic output.
# --------------------------------------------------------------------------- #

class _FakeST:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.dim = 8

    def encode(self, texts, convert_to_numpy=True, show_progress_bar=False, **_):
        # Deterministic fingerprint per text: first `dim` byte values, normalized.
        import numpy as np
        vectors = []
        for t in texts:
            raw = (t.encode("utf-8") + b"\x00" * self.dim)[: self.dim]
            v = np.array([b / 255.0 for b in raw], dtype="float32")
            vectors.append(v)
        return np.stack(vectors) if convert_to_numpy else vectors

    def get_sentence_embedding_dimension(self):
        return self.dim


@pytest.fixture
def fake_model(monkeypatch):
    """Patch SentenceTransformer to return our fake model, no network."""
    import local_st_embedding as mod
    monkeypatch.setattr(mod, "_load_model", lambda name: _FakeST(name))
    return _FakeST("fake")


# --------------------------------------------------------------------------- #
# Minimal model_config stub matching what graphrag_llm passes in.
# --------------------------------------------------------------------------- #

def _stub_model_config(model_name: str = "fake-st"):
    cfg = MagicMock()
    cfg.model_provider = "local_st"
    cfg.model = model_name
    cfg.type = "local_st"
    cfg.model_extra = {}
    return cfg


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

class TestLocalSTEmbedding:
    def test_instantiates_without_error(self, fake_model):
        from local_st_embedding import LocalSTEmbedding
        emb = LocalSTEmbedding(
            model_id="local_st/fake-st",
            model_config=_stub_model_config(),
            tokenizer=MagicMock(),
            metrics_store=MagicMock(),
            cache_key_creator=MagicMock(),
        )
        assert emb is not None

    def test_embedding_returns_response_with_correct_shape(self, fake_model):
        from local_st_embedding import LocalSTEmbedding
        emb = LocalSTEmbedding(
            model_id="local_st/fake-st",
            model_config=_stub_model_config(),
            tokenizer=MagicMock(),
            metrics_store=MagicMock(),
            cache_key_creator=MagicMock(),
        )
        resp = emb.embedding(input=["hello", "world", "foo"])
        assert len(resp.data) == 3
        # Each vector has the fake model's dim (8)
        for item in resp.data:
            assert len(item.embedding) == 8
        # Indices are correct
        assert [item.index for item in resp.data] == [0, 1, 2]

    def test_different_inputs_produce_different_vectors(self, fake_model):
        from local_st_embedding import LocalSTEmbedding
        emb = LocalSTEmbedding(
            model_id="local_st/fake-st",
            model_config=_stub_model_config(),
            tokenizer=MagicMock(),
            metrics_store=MagicMock(),
            cache_key_creator=MagicMock(),
        )
        resp = emb.embedding(input=["apple", "banana"])
        assert resp.data[0].embedding != resp.data[1].embedding

    def test_embedding_is_deterministic(self, fake_model):
        from local_st_embedding import LocalSTEmbedding
        emb = LocalSTEmbedding(
            model_id="local_st/fake-st",
            model_config=_stub_model_config(),
            tokenizer=MagicMock(),
            metrics_store=MagicMock(),
            cache_key_creator=MagicMock(),
        )
        r1 = emb.embedding(input=["same text"])
        r2 = emb.embedding(input=["same text"])
        assert r1.data[0].embedding == r2.data[0].embedding

    def test_async_method_returns_same_result_as_sync(self, fake_model):
        import asyncio
        from local_st_embedding import LocalSTEmbedding
        emb = LocalSTEmbedding(
            model_id="local_st/fake-st",
            model_config=_stub_model_config(),
            tokenizer=MagicMock(),
            metrics_store=MagicMock(),
            cache_key_creator=MagicMock(),
        )
        sync = emb.embedding(input=["x", "y"])
        async_resp = asyncio.run(emb.embedding_async(input=["x", "y"]))
        assert sync.data[0].embedding == async_resp.data[0].embedding
        assert sync.data[1].embedding == async_resp.data[1].embedding

    def test_metrics_store_property_accessible(self, fake_model):
        from local_st_embedding import LocalSTEmbedding
        sentinel = MagicMock(name="metrics_store_sentinel")
        emb = LocalSTEmbedding(
            model_id="local_st/fake-st",
            model_config=_stub_model_config(),
            tokenizer=MagicMock(),
            metrics_store=sentinel,
            cache_key_creator=MagicMock(),
        )
        assert emb.metrics_store is sentinel

    def test_registers_with_graphrag_factory(self, fake_model):
        import local_st_embedding as mod
        from graphrag_llm.embedding.embedding_factory import embedding_factory
        mod.register_local_st_embedding()
        assert "local_st" in embedding_factory
