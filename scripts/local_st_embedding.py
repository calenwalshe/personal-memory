"""
local_st_embedding — LLMEmbedding adapter for local sentence-transformers.

Wraps a local SentenceTransformer model so graphrag's indexing pipeline can
call it through the same LLMEmbedding interface it uses for LiteLLM-backed
embeddings, but with zero network calls and zero API cost.

Usage:
    from local_st_embedding import register_local_st_embedding
    register_local_st_embedding()

    # then in settings.yaml:
    #   embedding_models:
    #     default_embedding_model:
    #       type: local_st                          # selects our factory
    #       model_provider: local_st                # becomes part of model_id
    #       model: all-MiniLM-L6-v2                 # any sentence-transformers model
    #       auth_method: api_key
    #       api_key: unused
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Unpack

from graphrag_llm.embedding.embedding import LLMEmbedding
from graphrag_llm.embedding.embedding_factory import register_embedding
from graphrag_llm.types.types import (
    LLMEmbedding as LLMEmbeddingDTO,
)
from graphrag_llm.types.types import (
    LLMEmbeddingResponse,
    LLMEmbeddingUsage,
)

if TYPE_CHECKING:
    from graphrag_llm.config import ModelConfig
    from graphrag_llm.metrics import MetricsStore
    from graphrag_llm.tokenizer import Tokenizer
    from graphrag_llm.types import LLMEmbeddingArgs


# --------------------------------------------------------------------------- #
# Module-level cache: one SentenceTransformer instance per model name.
# Keeps the loaded weights resident across embedding calls.
# --------------------------------------------------------------------------- #

_MODEL_CACHE: dict[str, Any] = {}


def _load_model(model_name: str):
    """Import and cache a SentenceTransformer by name. Overridden in tests."""
    if model_name in _MODEL_CACHE:
        return _MODEL_CACHE[model_name]
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(model_name)
    _MODEL_CACHE[model_name] = model
    return model


# --------------------------------------------------------------------------- #
# LLMEmbedding subclass
# --------------------------------------------------------------------------- #

class LocalSTEmbedding(LLMEmbedding):
    """Local sentence-transformers embedding. No network, no API key required."""

    _metrics_store: "MetricsStore"
    _tokenizer: "Tokenizer"
    _model: Any
    _model_name: str

    def __init__(
        self,
        *,
        model_id: str,
        model_config: "ModelConfig",
        tokenizer: "Tokenizer",
        metrics_store: "MetricsStore",
        metrics_processor: Any = None,
        rate_limiter: Any = None,
        retrier: Any = None,
        cache: Any = None,
        cache_key_creator: Any = None,
        **kwargs: Any,
    ):
        self._tokenizer = tokenizer
        self._metrics_store = metrics_store
        # Graphrag passes the model_config.model field as the HF model name.
        self._model_name = model_config.model
        self._model = _load_model(self._model_name)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            texts,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        # numpy arrays → plain python lists (what LLMEmbeddingDTO expects).
        return [[float(x) for x in row] for row in vectors]

    def embedding(
        self, /, **kwargs: Unpack["LLMEmbeddingArgs"]
    ) -> LLMEmbeddingResponse:
        texts: list[str] = list(kwargs.get("input") or [])
        vectors = self._encode(texts)
        data = [
            LLMEmbeddingDTO(object="embedding", embedding=vec, index=i)
            for i, vec in enumerate(vectors)
        ]
        total_chars = sum(len(t) for t in texts)
        return LLMEmbeddingResponse(
            object="list",
            data=data,
            model=f"local_st/{self._model_name}",
            usage=LLMEmbeddingUsage(
                prompt_tokens=total_chars // 4,
                total_tokens=total_chars // 4,
            ),
        )

    async def embedding_async(
        self, /, **kwargs: Unpack["LLMEmbeddingArgs"]
    ) -> LLMEmbeddingResponse:
        # sentence-transformers is synchronous; we just delegate.
        return self.embedding(**kwargs)

    @property
    def metrics_store(self) -> "MetricsStore":
        return self._metrics_store

    @property
    def tokenizer(self) -> "Tokenizer":
        return self._tokenizer


# --------------------------------------------------------------------------- #
# Factory registration
# --------------------------------------------------------------------------- #

_REGISTERED = False


def register_local_st_embedding() -> None:
    """Register LocalSTEmbedding under the 'local_st' type string so that
    settings.yaml entries with `type: local_st` route to this class."""
    global _REGISTERED
    if _REGISTERED:
        return
    register_embedding("local_st", LocalSTEmbedding, scope="singleton")
    _REGISTERED = True
