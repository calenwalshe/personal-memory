#!/usr/bin/env python3
"""
extract_sessions.py — exchange-level chunker + GraphRAG input writer.

Pipeline:
    raw JSONL sessions → filtered events → exchanges (user→assistant pairs)
        → 400-token chunks with 80-token overlap → graph/input/*.txt

Chunks are atomic at exchange boundary: a single exchange larger than the
target token budget stays in one chunk rather than being split. Overlap is
implemented by reusing trailing exchanges from the previous chunk until the
overlap token budget is reached.

CLI:
    python3 extract_sessions.py [--graph-dir DIR] [--dry-run]
                                [--target-tokens 400] [--overlap-tokens 80]
                                [--project SLUG] [--since ISO8601]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

DEFAULT_GRAPH_DIR = Path.home() / "memory" / "vault" / "graph"
DEFAULT_TARGET_TOKENS = 400
DEFAULT_OVERLAP_TOKENS = 80

# Default models for the GraphRAG workspace
DEFAULT_COMPLETION_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # 23MB, 384-dim, fast local


# --------------------------------------------------------------------------- #
# Token estimation — char/4 heuristic (cheap, deterministic, no tiktoken dep)
# --------------------------------------------------------------------------- #

def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    # Ceil division: shorter texts still count as >=1 token.
    return max(1, (len(text) + 3) // 4)


# --------------------------------------------------------------------------- #
# Exchange grouping
# --------------------------------------------------------------------------- #

def iter_exchanges(events: list[dict]) -> Iterator[dict]:
    """
    Yield user→assistant exchange dicts.

    A user event is only emitted as part of an exchange when the NEXT event
    is an assistant response. Orphan users (followed by another user, or at
    end of stream) are dropped.
    """
    i = 0
    n = len(events)
    while i < n - 1:
        ev = events[i]
        nxt = events[i + 1]
        if ev.get("role") == "user" and nxt.get("role") == "assistant":
            yield {
                "user": ev,
                "assistant": nxt,
                "session_id": ev.get("session_id", "unknown"),
                "timestamp": ev.get("timestamp"),
            }
            i += 2
        else:
            i += 1


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #

def _exchange_id(exchange: dict) -> str:
    h = hashlib.sha1()
    h.update(exchange["user"]["text"].encode("utf-8"))
    h.update(b"\x00")
    h.update(exchange["assistant"]["text"].encode("utf-8"))
    return h.hexdigest()[:16]


def _exchange_tokens(exchange: dict) -> int:
    return estimate_tokens(exchange["user"]["text"]) + estimate_tokens(exchange["assistant"]["text"])


def _render_exchange(exchange: dict) -> str:
    u = exchange["user"]["text"]
    a = exchange["assistant"]["text"]
    return f"User: {u}\nAssistant: {a}"


def _finalize_chunk(indices: list[int], exchanges: list[dict]) -> dict:
    members = [exchanges[i] for i in indices]
    text = "\n\n".join(_render_exchange(m) for m in members)
    exchange_ids = [_exchange_id(m) for m in members]
    session_ids = sorted({m["session_id"] for m in members})
    timestamps = [m.get("timestamp") for m in members if m.get("timestamp")]
    first_ts = min(timestamps).isoformat() if timestamps else None
    last_ts = max(timestamps).isoformat() if timestamps else None
    chunk_id = hashlib.sha1(
        ("|".join(f"{i}:{eid}" for i, eid in zip(indices, exchange_ids)) + "::" + text).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "id": chunk_id,
        "text": text,
        "exchange_ids": exchange_ids,
        "session_ids": session_ids,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "_indices": tuple(indices),
    }


def chunk_exchanges(
    exchanges: list[dict],
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[dict]:
    """
    Greedy pack exchanges into chunks of ~target_tokens. Exchanges are atomic:
    if one alone exceeds target, it still forms a single chunk. Overlap is
    implemented by seeding each new chunk with trailing exchanges (by index)
    of the prior chunk until their combined token count reaches overlap_tokens.
    """
    if not exchanges:
        return []

    tokens = [_exchange_tokens(ex) for ex in exchanges]
    chunks: list[dict] = []
    current_idx: list[int] = []
    current_tokens = 0

    for i, ex_tokens in enumerate(tokens):
        if current_idx and (current_tokens + ex_tokens > target_tokens):
            chunks.append(_finalize_chunk(current_idx, exchanges))
            # Build overlap seed from trailing indices of prior chunk.
            seed: list[int] = []
            seed_tokens = 0
            for prior_i in reversed(current_idx):
                if seed_tokens >= overlap_tokens:
                    break
                seed.insert(0, prior_i)
                seed_tokens += tokens[prior_i]
            current_idx = seed
            current_tokens = seed_tokens
        current_idx.append(i)
        current_tokens += ex_tokens

    # Finalize the trailing chunk only if it contains forward progress
    # beyond the prior chunk (not just overlap residue).
    if current_idx:
        prior_indices = chunks[-1]["_indices"] if chunks else ()
        has_new = any(i not in prior_indices for i in current_idx)
        if has_new or not chunks:
            chunks.append(_finalize_chunk(current_idx, exchanges))

    # Strip internal _indices before returning.
    for c in chunks:
        c.pop("_indices", None)
    return chunks


# --------------------------------------------------------------------------- #
# GraphRAG input writer
# --------------------------------------------------------------------------- #

def write_graphrag_input(chunks: list[dict], graph_dir: Path) -> list[Path]:
    """Write one .txt per chunk under <graph_dir>/input/. Idempotent by filename (chunk id).
    Also writes <graph_dir>/chunk_metadata.json mapping chunk_id → {session_ids, exchange_ids, timestamps}
    so downstream consolidators can trace community reports back to source sessions."""
    graph_dir = Path(graph_dir)
    input_dir = graph_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    metadata: dict[str, dict] = {}
    for c in chunks:
        path = input_dir / f"chunk-{c['id']}.txt"
        path.write_text(c["text"], encoding="utf-8")
        written.append(path)
        metadata[c["id"]] = {
            "session_ids": c.get("session_ids", []),
            "exchange_ids": c.get("exchange_ids", []),
            "first_timestamp": c.get("first_timestamp"),
            "last_timestamp": c.get("last_timestamp"),
        }
    (graph_dir / "chunk_metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True)
    )
    return written


# --------------------------------------------------------------------------- #
# GraphRAG workspace bootstrap
# --------------------------------------------------------------------------- #

_SETTINGS_MARKER = "# autoresearch-memory: customized bootstrap"


def _render_settings_yaml(
    completion_model: str = DEFAULT_COMPLETION_MODEL,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> str:
    """Render the settings.yaml content for this workspace."""
    return f"""{_SETTINGS_MARKER}
# Generated by extract_sessions.bootstrap_workspace() — edit via that function.

completion_models:
  default_completion_model:
    type: litellm
    model_provider: anthropic
    model: {completion_model}
    auth_method: api_key
    api_key: ${{ANTHROPIC_API_KEY}}
    # Conservative for Anthropic Tier 1: 10K output TPM, 50 RPM for Haiku 4.5.
    # Stay well under both limits so extract_graph can run 582 chunks without
    # saturating the org bucket. Tokens counted are approximate (prompt+completion).
    rate_limit:
      type: sliding_window
      period_in_seconds: 60
      requests_per_period: 8
      tokens_per_period: 8000
    # Cap per-call output to keep each request small and batch-friendly.
    call_args:
      max_tokens: 1000
    retry:
      type: exponential_backoff

embedding_models:
  default_embedding_model:
    type: local_st
    model_provider: local_st
    model: {embedding_model}
    auth_method: api_key
    api_key: unused

input:
  type: text

chunking:
  # Chunks are already pre-chunked by extract_sessions.py; this is a pass-through.
  type: tokens
  size: 1200
  overlap: 100
  encoding_model: o200k_base

input_storage:
  type: file
  base_dir: input

output_storage:
  type: file
  base_dir: output

reporting:
  type: file
  base_dir: logs

cache:
  type: json
  storage:
    type: file
    base_dir: cache

vector_store:
  type: lancedb
  db_uri: output/lancedb

embed_text:
  embedding_model_id: default_embedding_model

extract_graph:
  completion_model_id: default_completion_model
  prompt: prompts/extract_graph.txt
  entity_types: [organization, person, concept, decision, tool, file, skill, topic]
  max_gleanings: 1

summarize_descriptions:
  completion_model_id: default_completion_model
  prompt: prompts/summarize_descriptions.txt
  max_length: 500

extract_graph_nlp:
  text_analyzer:
    extractor_type: regex_english

cluster_graph:
  max_cluster_size: 10

extract_claims:
  enabled: false
  completion_model_id: default_completion_model
  prompt: prompts/extract_claims.txt
  description: "Any claims or facts that could be relevant to information discovery."
  max_gleanings: 1

community_reports:
  completion_model_id: default_completion_model
  graph_prompt: prompts/community_report_graph.txt
  text_prompt: prompts/community_report_text.txt
  max_length: 2000
  max_input_length: 8000

snapshots:
  graphml: false
  embeddings: false

local_search:
  completion_model_id: default_completion_model
  embedding_model_id: default_embedding_model
  prompt: prompts/local_search_system_prompt.txt

global_search:
  completion_model_id: default_completion_model
  map_prompt: prompts/global_search_map_system_prompt.txt
  reduce_prompt: prompts/global_search_reduce_system_prompt.txt
  knowledge_prompt: prompts/global_search_knowledge_system_prompt.txt

drift_search:
  completion_model_id: default_completion_model
  embedding_model_id: default_embedding_model
  prompt: prompts/drift_search_system_prompt.txt
  reduce_prompt: prompts/drift_reduce_prompt.txt

basic_search:
  completion_model_id: default_completion_model
  embedding_model_id: default_embedding_model
  prompt: prompts/basic_search_system_prompt.txt
"""


def bootstrap_workspace(
    graph_dir: Path,
    *,
    completion_model: str = DEFAULT_COMPLETION_MODEL,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> Path:
    """
    Create a GraphRAG workspace at graph_dir:
      - graph/settings.yaml         custom config (anthropic + local_st)
      - graph/prompts/*.txt         graphrag's default prompt set (via `graphrag init`)
      - graph/input/                chunk files land here
      - graph/output/               graphrag writes artifacts here

    Idempotent: re-running is a no-op if settings.yaml already has our marker.
    """
    graph_dir = Path(graph_dir).expanduser()
    graph_dir.mkdir(parents=True, exist_ok=True)
    (graph_dir / "input").mkdir(exist_ok=True)

    settings_path = graph_dir / "settings.yaml"
    prompts_dir = graph_dir / "prompts"

    # Idempotency short-circuit.
    if settings_path.exists() and _SETTINGS_MARKER in settings_path.read_text():
        return settings_path

    # Run graphrag init to scaffold prompts/ (side-benefit: writes its own
    # settings.yaml which we immediately overwrite).
    if not prompts_dir.exists() or not any(prompts_dir.glob("*.txt")):
        import subprocess
        subprocess.run(
            [
                sys.executable, "-m", "graphrag", "init",
                "--root", str(graph_dir),
                "--model", completion_model,
                "--embedding", embedding_model,
                "--force",
            ],
            check=True,
            capture_output=True,
        )

    # Overwrite settings.yaml with our customized version.
    settings_path.write_text(
        _render_settings_yaml(completion_model, embedding_model)
    )
    return settings_path


# --------------------------------------------------------------------------- #
# Session corpus parsing — reuses preprocess-sessions.py via importlib
# (filename has a dash so we can't plain-import)
# --------------------------------------------------------------------------- #

def _load_preprocess_module():
    path = Path(__file__).parent / "preprocess-sessions.py"
    spec = importlib.util.spec_from_file_location("preprocess_sessions", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gather_events_from_corpus(
    project_filter: str | None = None, since: datetime | None = None
) -> list[dict]:
    """Parse every JSONL in the corpus into a flat, chronologically-sorted event list."""
    pp = _load_preprocess_module()
    projects_dir = pp.PROJECTS_DIR
    all_events: list[dict] = []
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        slug = pp.get_project_slug(project_dir)
        if project_filter and project_filter.lower() not in slug.lower():
            continue
        for jsonl_path in sorted(project_dir.glob("*.jsonl")):
            if since:
                mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=timezone.utc)
                if mtime < since:
                    continue
            all_events.extend(pp.parse_session_file(jsonl_path))
    all_events.sort(key=lambda e: e["timestamp"])
    return all_events


# --------------------------------------------------------------------------- #
# Indexing — run graphrag's Python API with our custom embedding registered
# --------------------------------------------------------------------------- #

def run_graphrag_index(graph_dir: Path, *, is_update: bool = False) -> None:
    """Invoke graphrag's build_index with local_st embedding registered.

    Must be called after bootstrap_workspace() and write_graphrag_input().
    """
    import asyncio

    # Import here so `--dry-run` / tests that don't need graphrag still work
    # without triggering the heavy import chain.
    from graphrag.api import build_index
    from graphrag.config.load_config import load_config

    import local_st_embedding
    local_st_embedding.register_local_st_embedding()

    graph_dir = Path(graph_dir).expanduser()
    config = load_config(root_dir=graph_dir)
    results = asyncio.run(build_index(config=config, is_update_run=is_update))
    # Surface any workflow errors instead of silently succeeding.
    errors = [r for r in results if getattr(r, "errors", None)]
    if errors:
        for r in errors:
            print(f"[index] workflow {getattr(r, 'workflow', '?')} errors: {r.errors}", file=sys.stderr)
        raise RuntimeError(f"graphrag index failed: {len(errors)} workflows had errors")

    # graphrag 3.0.8 sometimes logs workflow failures without surfacing them
    # via .errors on the result objects (rate-limit-induced giveup is one such
    # case). Positively verify the critical artifacts exist.
    required = ["community_reports.parquet", "entities.parquet"]
    missing = [f for f in required if not (graph_dir / "output" / f).exists()]
    if missing:
        log_path = graph_dir / "logs" / "indexing-engine.log"
        hint = f" — check {log_path} for workflow errors" if log_path.exists() else ""
        raise RuntimeError(
            f"graphrag index produced no {', '.join(missing)}; pipeline failed silently{hint}"
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--graph-dir", default=str(DEFAULT_GRAPH_DIR))
    ap.add_argument("--target-tokens", type=int, default=DEFAULT_TARGET_TOKENS)
    ap.add_argument("--overlap-tokens", type=int, default=DEFAULT_OVERLAP_TOKENS)
    ap.add_argument("--project", default=None)
    ap.add_argument("--since", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--index", action="store_true", help="Run graphrag index after writing chunks")
    ap.add_argument("--update", action="store_true", help="Incremental update mode for --index")
    args = ap.parse_args()

    since = None
    if args.since:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))

    events = gather_events_from_corpus(project_filter=args.project, since=since)
    exchanges = list(iter_exchanges(events))
    chunks = chunk_exchanges(
        exchanges,
        target_tokens=args.target_tokens,
        overlap_tokens=args.overlap_tokens,
    )

    print(f"events: {len(events)}  exchanges: {len(exchanges)}  chunks: {len(chunks)}")

    if args.dry_run:
        print(f"[dry-run] would write {len(chunks)} chunk files under {args.graph_dir}/input/")
        return

    graph_dir = Path(args.graph_dir).expanduser()
    bootstrap_workspace(graph_dir)
    written = write_graphrag_input(chunks, graph_dir)
    print(f"wrote {len(written)} files → {graph_dir}/input/")

    if args.index:
        print(f"[index] running graphrag {'update' if args.update else 'build'}...")
        run_graphrag_index(graph_dir, is_update=args.update)
        print("[index] done")


if __name__ == "__main__":
    main()
