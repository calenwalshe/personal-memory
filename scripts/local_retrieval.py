"""
local_retrieval.py — embed community reports once, retrieve top-K per query.

Used by autoresearch_loop.py to build a lightweight local-search substitute
that doesn't require graphrag's query API (which hits Anthropic API rate
limits). All work here is local: sentence-transformers for embeddings,
numpy for cosine similarity. Zero network calls.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

DEFAULT_MODEL = "all-MiniLM-L6-v2"

_MODEL_CACHE: dict[str, Any] = {}


def _load_model(model_name: str):
    """Import and cache a SentenceTransformer by name. Overridden in tests."""
    if model_name in _MODEL_CACHE:
        return _MODEL_CACHE[model_name]
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(model_name)
    _MODEL_CACHE[model_name] = model
    return model


def embed_reports(
    reports_df: pd.DataFrame, *, model_name: str = DEFAULT_MODEL
) -> np.ndarray:
    """Return an (N, d) float32 matrix of L2-normalized embeddings for each
    report in reports_df. Input column priority: full_content → summary → title."""
    if len(reports_df) == 0:
        return np.zeros((0, 1), dtype="float32")
    texts: list[str] = []
    for _, row in reports_df.iterrows():
        text = (
            str(row.get("full_content", ""))
            or str(row.get("summary", ""))
            or str(row.get("title", ""))
        )
        texts.append(text[:8000])
    model = _load_model(model_name)
    vectors = model.encode(
        texts,
        convert_to_numpy=True,
        show_progress_bar=False,
        normalize_embeddings=True,
    )
    return np.asarray(vectors, dtype="float32")


def embed_query(text: str, *, model_name: str = DEFAULT_MODEL) -> np.ndarray:
    """Return a single (d,) normalized embedding for a query string."""
    model = _load_model(model_name)
    vec = model.encode(
        [text],
        convert_to_numpy=True,
        show_progress_bar=False,
        normalize_embeddings=True,
    )[0]
    return np.asarray(vec, dtype="float32")


def retrieve_top_k(
    query_embedding: np.ndarray, report_embeddings: np.ndarray, *, k: int
) -> list[int]:
    """Return the indices of the top-k reports by cosine similarity to the query.

    Both inputs are assumed L2-normalized so dot product = cosine similarity.
    Returns indices in descending similarity order; shorter if fewer than k
    reports exist.
    """
    n = int(report_embeddings.shape[0]) if report_embeddings.ndim == 2 else 0
    if n == 0:
        return []
    k = min(k, n)
    sims = report_embeddings @ query_embedding
    top_idx = np.argsort(-sims)[:k]
    return [int(i) for i in top_idx]


def build_context_string(
    reports_df: pd.DataFrame, indices: list[int], *, max_chars: int = 12000
) -> str:
    """Concatenate full_content of the selected reports into a single context
    string. Truncates to max_chars, adding an ellipsis marker if cut."""
    if not indices or len(reports_df) == 0:
        return ""
    rows = reports_df.iloc[indices]
    parts: list[str] = []
    for _, row in rows.iterrows():
        title = str(row.get("title", ""))
        body = str(row.get("full_content", "") or row.get("summary", ""))
        parts.append(f"## {title}\n{body}")
    joined = "\n\n---\n\n".join(parts)
    marker = "\n[...truncated]"
    if len(joined) > max_chars:
        cut = max(0, max_chars - len(marker))
        joined = joined[:cut] + marker
    return joined
