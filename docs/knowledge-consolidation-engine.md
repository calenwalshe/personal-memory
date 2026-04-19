# Knowledge Consolidation Engine

Automated pipeline that aggregates raw L0 sense data into durable L1 memory atoms. Replaces the old session-extraction → window-classifier → fact-store pipeline.

## Architecture

```
L0 events.db (continuous, per tool call)
    └─ chunker.py           # boundary-agnostic clustering of L0 events
    └─ atom_store.py         # atoms.db + FAISS (embed + store)
                                 ↓
                          vault context     (SessionStart injection)
                          vault recall      (semantic search + synthesis)
                          vault atoms       (list, show, search, stats)
```

## How It Works

1. **L0 collection** — `postuse-event-logger.sh` writes every tool call as a SCAPE stimulus compound to events.db. `sessionend-db-update.sh` writes messages and sequences at session end.

2. **Chunker triggers** — PreCompact (auto) and SessionEnd (productive sessions or /clear) kick the chunker via `vault chunk <project>`.

3. **Noise filtering** — keeps Bash/Write/Edit/errors/retries, drops internal plumbing (TaskCreate, Skill, etc.), conditionally keeps orientation reads (Read/Glob/Grep) only if followed by action within 30s.

4. **Pre-clustering** — groups events by content signals:
   - Hard close: >2h gap, project change, /clear
   - Soft close: intent shift, entity overlap drop, >30min gap
   - Size cap: max 15 events per cluster
   - Lazy closing: clusters stay open/provisional across runs

5. **Haiku refinement** — closed clusters are sent to Haiku in batches. Haiku confirms/adjusts boundaries and produces typed atoms (decision, discovery, failure, pattern, gotcha, outcome).

6. **Provenance assembly** — each atom gets a denormalized metadata package: source event IDs, session IDs, time range, git context, trigger, tools used, files touched, error/retry signals.

7. **Storage** — atoms written to atoms.db (SQLite WAL) + FAISS index (all-MiniLM-L6-v2 embeddings).

## Current Status

| Component | Status |
|---|---|
| L0 event collection | Stable, all hooks active |
| L1 chunker | Live, tested across 7 projects |
| Atom storage + FAISS | Working |
| vault context (SessionStart) | Working, reads atoms.db with facts.db fallback |
| vault recall / /recall | Working, atoms.db preferred |
| L2+ aggregation | Not yet designed |

## Key Design Decisions

| Decision | Rationale |
|---|---|
| Boundary-agnostic chunking | Sessions are noisy boundaries (94% are 0-event subprocesses); content-driven chunks are more meaningful |
| Project-keyed state | Same project across sessions = continuous stream; chunker cursor persists per-project |
| Haiku refines, doesn't discover | Heuristic pre-clustering does the heavy lifting; Haiku only confirms boundaries and produces atom text |
| Denormalized provenance | Each atom is self-contained — no joins to events.db needed for reading |
| env -u ANTHROPIC_API_KEY | Forces claude -p to use subscription instead of depleted API credits |

## Superseded Components

These are from the old pipeline and are no longer used:
- `window_classifier.py` — replaced by chunker.py's noise filter + pre-clustering
- `extract_sessions.py` — replaced by direct L0 event reads
- `deep_consolidate.py` / `subagent_graphbuilder.py` — GraphRAG approach abandoned
- `autoresearch_loop.py` — parameter optimizer no longer needed
- `facts.db` — preserved but superseded by atoms.db
