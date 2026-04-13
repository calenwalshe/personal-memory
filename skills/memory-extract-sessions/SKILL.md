---
name: memory-extract-sessions
description: "Extracts exchange-level chunks from the ~/.claude/projects/-home-agent/*.jsonl session corpus, writes them to ~/memory/vault/graph/input/, and runs GraphRAG indexing with local sentence-transformers embeddings and Anthropic Haiku completions. Trigger when the user says 'extract sessions', 'reindex graph', 'run graphrag index', or uses /memory-extract-sessions."
---

# Memory Extract Sessions — Corpus → Chunks → GraphRAG Index

Thin wrapper around `~/memory/vault/scripts/extract_sessions.py`. This skill:

1. Parses every `~/.claude/projects/-home-agent/*.jsonl` into events (read-only)
2. Groups events into exchanges (user → assistant pairs), packs them into ~400-token chunks with 80-token overlap (exchanges are atomic)
3. Writes one `chunk-<id>.txt` per chunk under `~/memory/vault/graph/input/` + a `chunk_metadata.json` for downstream session traceability
4. Bootstraps the GraphRAG workspace (settings.yaml with `model_provider: anthropic`, `model: claude-haiku-4-5-20251001`, and the custom `local_st` embedding type pointing at `all-MiniLM-L6-v2`)
5. Runs `graphrag.api.build_index()` in-process with the custom embedding registered — all embedding calls run locally, only completions call Anthropic

## User-invocable

When the user types `/memory-extract-sessions`, run this skill. Also trigger on:
- "extract sessions"
- "reindex the graph"
- "rebuild graphrag index"
- "run graphrag indexing"

## Arguments

- `/memory-extract-sessions` — full run: parse → chunk → write → bootstrap → index
- `/memory-extract-sessions --dry-run` — parse → chunk → print counts; no writes, no index
- `/memory-extract-sessions --update` — incremental index (`graphrag update`) instead of full build
- `/memory-extract-sessions --no-index` — write chunks and bootstrap only, skip indexing

## Instructions

### Step 1: Sanity check

Verify the corpus exists:

```bash
ls ~/.claude/projects/-home-agent/*.jsonl | wc -l
```

If zero, stop and report: no session corpus to process.

### Step 2: Run the extractor

```bash
cd ~/memory/vault/scripts
/home/agent/claude-stack-env/bin/python3 extract_sessions.py "$@"
```

Pass through any args the user supplied. For the default (full build + index):

```bash
/home/agent/claude-stack-env/bin/python3 extract_sessions.py --index
```

For incremental:

```bash
/home/agent/claude-stack-env/bin/python3 extract_sessions.py --index --update
```

### Step 3: Report

Echo the final counts from the script's stdout:
- `events: N  exchanges: M  chunks: K`
- `wrote K files → <graph-dir>/input/`
- Community report count: `ls ~/memory/vault/graph/output/community_reports/ 2>/dev/null | wc -l` (if `--index` was used)

## Cost note

With `--index`, indexing the full corpus (~600 chunks) makes hundreds of Haiku API calls (entity extraction, description summarization, community reports). Expect ~$1–3 in API spend and 5–15 minutes of wall time. Use `--dry-run` first if the user wants to preview.

## Not in scope

This skill is a thin subprocess wrapper — no parsing of the output, no contradiction checks, no candidate writing. For community reports → vault candidates, use `/memory-deep-consolidate`.
