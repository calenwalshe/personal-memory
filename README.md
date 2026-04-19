# personal-memory

Cross-session persistent memory for Claude Code — capture, chunk, embed, and recall across sessions and projects.

**SCAPE-inspired architecture** (Selective Construction And Preservation of Experiences). Memory is reconstruction, not retrieval. Every hook captures the full stimulus compound — stimulus + purpose + context + processing operations + fluency signals — so any session can be reconstructed from fragments later.

## Architecture

```
L0 sense data (per tool call, continuous)
    postuse-event-logger.sh   →  events.db (events + event_content)
    sessionend-extract-turns.py →  events.db (turns table — primary L0)

L1 atomic memory (on PreCompact / SessionEnd)
    chunker.py                →  atoms.db (typed atoms with provenance)
    atom_store.py             →  atoms.faiss (semantic search index)

L2 entity graph (incremental, after every chunk)
    graph_store.py            →  graph.db (entities, relations, communities)
    entity_resolver.py        →  entity normalization + alias resolution
    hebbian.py                →  live Hebbian weight updates ("fire together, wire together")
```

### L2 Lab — experiment platform

`experiments/` contains reproducible experiments that run against forked graph.db snapshots:

| Experiment | What it tests |
|-----------|--------------|
| `001-hebbian-weights` | Batch Hebbian update: does co-activation boost reshape community structure? |
| `002-cross-domain-bridges` | Community embedding similarity: can we surface latent analogies across domains? |
| `003-hebbian-v2` | Fix MAX_DELTA saturation: raised cap from 3.0→20.0, unblocks gradient |

## Vault CLI

```bash
vault chunk <project>              # L0 → L1: run chunker for a project
vault atoms [list|show|search|stats]
vault recall <topic>               # semantic search + Haiku synthesis
vault context <project>            # fast session context injection
vault events [project]             # raw L0 tool events
vault sessions [project]

vault graph stats                  # L2 entity graph overview
vault graph entity <name>          # show entity + relations
vault graph communities            # list detected communities
vault graph rebuild                # re-run community detection + LLM summaries

vault snap [ls|create|checkout|info]   # graph.db snapshot management
vault lab [ls|new|run|eval|export]     # experiment platform
```

## Hooks (Claude Code)

Wire these into `.claude/settings.json`:

| Event | Hook | Purpose |
|-------|------|---------|
| PostToolUse | `postuse-event-logger.sh` | L0: log every tool call to events.db |
| PostToolUse | `postuse-git-episode.sh` | L0: git commit episodes |
| PreCompact | `precompact-session-snapshot.sh` | L1: snapshot + kick chunker |
| SessionEnd | `sessionend-session-summary.sh` | L1: episode .md + kick chunker |
| SessionEnd | `sessionend-db-update.sh` | L0: close session, write snapshots |
| SessionEnd | `sessionend-extract-turns.py` | L0: extract turns to DB (primary L0) |
| SessionStart | `sessionstart-snapshot.sh` | L0: open session row |
| SessionStart | `sessionstart-postclear-recall.sh` | Reconstruct context after /clear |

## Key Scripts

| Script | Purpose |
|--------|---------|
| `chunker.py` | Boundary-agnostic L1 chunker — Haiku classifies intent shifts into typed atoms |
| `atom_store.py` | atoms.db CRUD + FAISS index management |
| `graph_store.py` | graph.db: entity resolution, co-occurrence relations, community detection, LLM summaries |
| `entity_resolver.py` | Two-pass normalization: embedding clusters + Haiku confirmation |
| `hebbian.py` | Live Hebbian plasticity — ETA=0.1 weight increment per co-appearing entity pair |
| `recall_query.py` | FAISS semantic search over atoms |
| `local_st_embedding.py` | Local sentence-transformers embeddings (all-MiniLM-L6-v2) |

## Atom Types

Each atom has a type that guides recall and synthesis:

- `decision` — choices made, options considered
- `discovery` — things learned, insights
- `failure` — what broke, error patterns
- `pattern` — reusable approaches
- `gotcha` — traps and edge cases
- `outcome` — results, metrics, completions

## Hebbian Plasticity

The live Hebbian hook runs after every `vault chunk` call. For each new atom batch, it finds entity pairs that co-appeared and increments their `related_to` edge weight by ETA=0.1. Over many sessions, strongly co-activated pairs naturally accumulate higher weights — the graph learns which concepts genuinely cluster together, not just which co-occurred once.

This is distinct from the batch co-occurrence baseline (which runs once at graph build time). The Hebbian hook is the continuous learning layer.

## Setup

1. Clone and configure vault path in `bin/vault` (`VAULT` variable)
2. Initialize databases: `vault graph stats` (auto-creates graph.db on first run)
3. Wire hooks into `.claude/settings.json`
4. Run initial chunk: `vault chunk <your-project>`
5. Build L2: `vault graph rebuild`

## Docs

- [`docs/SPEC-L0-L1.md`](docs/SPEC-L0-L1.md) — full pipeline design
- [`docs/MEMORY-ARCHITECTURE.md`](docs/MEMORY-ARCHITECTURE.md) — architecture overview
