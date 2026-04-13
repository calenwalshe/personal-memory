# personal-memory

Cross-session persistent memory system for Claude Code. Captures thoughts, decisions, and Claude outputs into a structured vault — queryable across sessions, projects, and working directories.

## Architecture

```
scripts/          # Python pipeline (extraction, embedding, retrieval, graph)
skills/           # Claude Code skill definitions (capture, recall, review, etc.)
vault-template/   # Empty vault scaffold to bootstrap a new instance
```

## Skills

| Skill | Trigger | Purpose |
|---|---|---|
| `/memory-capture` | "save this", "remember this" | Write a thread to the vault |
| `/recall` | "what did I learn about X" | Semantic search + synthesis |
| `/memory-review` | manual | Review + triage active threads |
| `/memory-consolidate` | manual | Extract facts from session history |
| `/memory-extract-sessions` | manual | Chunk session logs into exchange windows |
| `/memory-compile` | manual | Compile threads into linked concept pages |
| `/memory-promote` | manual | Graduate a thread to its destination |
| `/memory-ingest` | "ingest this URL" | Fetch URL → vault inbox stub |
| `/memory-review-candidates` | manual | Walk pending candidate threads |
| `/memory-deep-consolidate` | manual | GraphRAG community report builder |

## Pipeline

```
Session logs → extract_sessions.py → window_classifier.py → fact_store.py
                                                                    ↓
                                                             recall_query.py
```

Facts are embedded locally (sentence-transformers) and stored in FAISS. `recall_query.py` does semantic search + LLM synthesis with session-date provenance.

## Setup

1. Copy `vault-template/` to `~/memory/vault/`
2. Copy `skills/` contents to `~/.claude/skills/`
3. Install deps: `pip install sentence-transformers faiss-cpu anthropic`
4. Set `VAULT_DIR=~/memory/vault` in your environment

## Environment Variables

| Var | Default | Purpose |
|---|---|---|
| `MEMORY_VAULT` | `~/memory/vault` | Vault root path |
| `VAULT_DIR` | same | Used by scripts directly |
| `LLM_PROVIDER` | `claude` | LLM backend for classification |
| `ANTHROPIC_API_KEY` | — | Required for Claude-backed steps |
