# personal-memory

Cross-session persistent memory system for Claude Code. Captures raw tool-call sense data, aggregates it into atomic memory units, and makes them queryable across sessions, projects, and working directories.

## Architecture

```
L0 — Raw sense data (per tool call, continuous)
  hooks/postuse-event-logger.sh  →  events.db (events + event_content)
  hooks/sessionend-db-update.sh  →  events.db (messages, sequences, snapshots)

L1 — Atomic memory units (on PreCompact / SessionEnd / /clear)
  scripts/chunker.py             →  atoms.db (typed atoms with provenance)
  scripts/atom_store.py          →  atoms.db + FAISS index

Consumers
  vault context <project>        →  SessionStart injection (no LLM)
  vault atoms search -q <query>  →  FAISS semantic search
  vault recall <topic>           →  Haiku synthesis with provenance
  /recall                        →  User-facing query skill
```

## Key Concepts

- **L0 events** carry the full SCAPE stimulus compound: tool input/output, purpose (user intent), context (git, cwd), fluency signals (errors, retries)
- **L1 atoms** are the smallest coherent memory unit. Types: decision, discovery, failure, pattern, gotcha, outcome
- **Boundary-agnostic chunking**: atoms can span session and compaction boundaries — chunks are defined by content (intent shifts, entity overlap, time gaps), not process lifecycle
- **Project-keyed**: chunker state is per-project. Same project across different sessions = continuous stream

## Vault CLI

```bash
vault chunk <project>              # run L1 chunker (L0 → atoms)
vault atoms list [--project P]     # list recent atoms
vault atoms show <atom-id>         # full atom with provenance
vault atoms search -q <query>      # FAISS semantic search
vault atoms stats                  # counts by project and type
vault context <project>            # fast SessionStart injection
vault recall <topic> [--deep]      # synthesized recall with provenance
vault events [project] [-n N]      # list raw L0 events
vault sessions [project]           # list sessions
vault status                       # pipeline health
```

## Setup

1. Vault lives at `~/memory/vault/`
2. Hooks in `~/.claude/hooks/` (installed via settings.json)
3. Deps: `pip install sentence-transformers faiss-cpu`
4. `claude -p` calls use subscription (env -u ANTHROPIC_API_KEY)

## Environment Variables

| Var | Default | Purpose |
|---|---|---|
| `MEMORY_VAULT` | `~/memory/vault` | Vault root path |
| `VAULT_DIR` | same | Used by scripts directly |
| `EMBED_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformers model |
