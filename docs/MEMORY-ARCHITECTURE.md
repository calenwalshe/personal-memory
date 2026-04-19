# Memory Architecture — Living Design Doc

> Working surface for memory system design. Update this as ideas develop, decisions get made, and things get built.
> Last meaningfully updated: 2026-04-17

---

## System Overview

SCAPE-inspired personal memory vault. Goal: reconstruct any session from fragments. Memory is construction, not retrieval.

Pipeline: **L0 sense data → L1 atoms → L2 entity graph → L3 consolidation (partial)**

---

## Current State — What's Built

### L0: Raw Capture
- **events.db** — per-tool-call telemetry (tool name, input hash, error flag, timing)
- **events.db/turns** — primary L0: full conversation turns (user msg + thinking + tools + response)
- **events.db/session_snapshots** — git state at session start/end
- **JSONL archive** — `raw/event-log/{project}-{date}.jsonl` — append-only per-project per-day
- Hooks: PostToolUse (event logger), SessionStart (snapshot), SessionEnd (turns extractor + summary)

### L1: Atomic Memory
- **atoms.db** — typed atomic memories: decision, discovery, failure, pattern, gotcha, outcome
- **atoms.faiss** — 384-dim all-MiniLM-L6-v2 semantic index
- Fields: content, atom_type, project, entities, topic, confidence, importance, time range, git context, tools_used, files_touched
- Chunker (chunker.py): boundary-agnostic, triggered at PreCompact + SessionEnd
- ~500-1000 atoms currently

### L2: Entity Graph
- **graph.db** — entities, relations, communities, interest_areas, l2_state
- **833 entities** (concept×525, tool×98, service×77, file×67, place×29, project×22, person×10)
- **2195 relations**: 1884 co-occurrence (related_to) + 311 typed semantic
  - Typed: uses×100, part_of×98, depends_on×49, deployed_on×20, configured_by×19, replaced_by×13, built_with×9, analogous_to×3
- **115 communities** — label propagation + Haiku MOC summaries
  - Each community has: label, summary, key_findings, genesis, evolution, current_state, open_threads
  - Summary embeddings stored for semantic search
- **14 interest areas** — normalized taxonomy
- Incremental update: new atoms → entity resolution → co-occurrence edges → mark communities stale
- Typed relation extraction: relation_extractor.py (Haiku, 10 atoms/batch)
- Community rebuild: vault graph rebuild (~20 min, 144 Haiku calls)
- Browser: maps.calenwalshe.com/communities.html

### L2: Retrieval
- `vault recall <topic>` — FAISS semantic search over atoms + community summary semantic search (cosine > 0.45) → Haiku synthesis
- `vault graph search <query>` — community-level semantic search only
- `vault context <project>` — fast project context injection (no LLM)
- SessionStart postclear-recall hook — reconstructs session from L0 turns + L1 atoms

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│  LIVE SESSION                                                       │
│  Claude Code conversation turns                                     │
└────────────────────────────┬────────────────────────────────────────┘
                             │ PostToolUse (async, non-blocking)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  L0 — SENSE DATA                                                    │
│                                                                     │
│  events.db                          raw/event-log/                  │
│  ├─ turns (primary L0)              {project}-{date}.jsonl          │
│  ├─ events (per-tool-call)                                          │
│  ├─ event_content (full I/O)                                        │
│  └─ session_snapshots (git state)                                   │
└────────────────────────────┬────────────────────────────────────────┘
                             │ PreCompact / SessionEnd (chunker.py)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  L1 — ATOMIC MEMORY                                                 │
│                                                                     │
│  atoms.db                           atoms.faiss                     │
│  ├─ typed atoms                     384-dim MiniLM                  │
│  │   decision / discovery /         semantic index                  │
│  │   failure / pattern /                                            │
│  │   gotcha / outcome                                               │
│  └─ fields: content, project,                                       │
│     entities, confidence,                                           │
│     importance, time range                                          │
└────────────────────────────┬────────────────────────────────────────┘
                             │ incremental_update() + relation_extractor.py
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  L2 — ENTITY GRAPH                                                  │
│                                                                     │
│  graph.db                                                           │
│  ├─ entities (833)          canonical names, types, aliases         │
│  ├─ relations (2195)        co-occurrence + 9 typed semantic types  │
│  ├─ communities (115)       label propagation clusters              │
│  │   + temporal arc         genesis / evolution / now / open        │
│  │   + embeddings           semantic search over summaries          │
│  └─ interest_areas (14)     normalized interest taxonomy            │
│                                                                     │
│  Browser: maps.calenwalshe.com/communities.html                     │
└────────────────────────────┬────────────────────────────────────────┘
                             │ ← NOT YET BUILT
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  L3 — CONSOLIDATION  [PLANNED]                                      │
│                                                                     │
│  Stable semantic knowledge, decoupled from episodic L1 instances   │
│  Cross-community pattern detection                                  │
│  Distilled "facts" that survive atom rotation                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Known Gaps — Architectural

### GAP-01: No Decay Model
**What's missing:** Everything exists at equal weight forever. `importance` and `confidence` on atoms are set at write time, never updated. No forgetting curve.
**Why it matters:** Can't distinguish things you remember well vs. things sitting unaccessed for months. Graph is only additive.
**Possible approach:** Access-time decay on atoms (exponential halflife, reset on retrieval). Weight = base_importance × decay(last_accessed).
**Status:** Identified, not designed.

### GAP-02: Retrieval Doesn't Reinforce
**What's missing:** `vault recall` reads atoms, that's it. No trace left. System doesn't know retrieval history.
**Why it matters:** Human memory is shaped by retrieval. Frequently recalled things should be stronger. Currently: no feedback loop.
**Possible approach:** Write a `retrieval_log` table (atom_id, retrieved_at, query). Update atom weight on access. Feeds GAP-01.
**Status:** Identified, not designed.

### GAP-03: Episodic/Semantic Conflation in L1
**What's missing:** L1 atoms mix two fundamentally different memory types:
  - **Episodic**: "on April 15 we found the race condition" — specific, timestamped, should decay
  - **Semantic**: "analyze_library.py has a concurrency bug" — general, timeless, should persist and generalize
**Why it matters:** In CLS theory these need different treatment. Episodic feeds into semantic via consolidation. Here they're identical schema rows.
**Possible approach:** Add `memory_class: episodic | semantic` to atoms. Semantic atoms get promoted to stable L2 facts. Episodic atoms decay normally.
**Status:** Identified, not designed. This is probably the deepest architectural gap.

### GAP-04: L2 Communities Are Computed, Not Consolidated
**What's missing:** Communities are re-derived from atoms on every rebuild. They're not *stable knowledge* — they're cached aggregates. If you rotated out old atoms, communities would degrade.
**Why it matters:** True semantic memory persists even when source episodes fade. L2 should eventually be independent of L1.
**Possible approach:** After community reaches stability (N rebuilds without structural change), "lock" its core knowledge. Allow it to accrete new atoms without full rewrite. Versioned community summaries.
**Status:** Identified, not designed.

### GAP-05: No Contradiction Surface
**What's missing:** `invalidated_by` exists on atoms and `replaced_by` as a relation type, but no active contradiction detection. Two atoms can assert opposite things with no flag.
**Why it matters:** Knowledge graph consistency. "yt_dj runs on systemd" and "yt_dj runs in Docker" both exist as valid atoms.
**Possible approach:** On L2 rebuild, run contradiction scan: for each entity, find atoms that make contradictory claims about it. Flag for review. Maybe auto-resolve with recency (newer wins).
**Status:** Identified, not designed.

### GAP-06: Retrieval Path Is Fragmented
**What's missing:** Three separate retrieval mechanisms (vault recall, vault context, SessionStart hook) that aren't composed. No single "what's relevant right now?" interface.
**Why it matters:** Context injection is inconsistent. Which one you use depends on knowing which one to use.
**Possible approach:** Unified `vault relevant <query> [--project P]` that combines: FAISS atom search + community semantic search + temporal arc state + recent session atoms. Single ranked result set.
**Status:** Identified. Partial work exists (vault recall does some of this).

### GAP-07: Cross-Domain Connections Weak
**What's missing:** `analogous_to` has only 3 edges. Communities are mostly single-domain clusters. Lateral connections between domains aren't being captured.
**Why it matters:** The most interesting memory is the unexpected bridge — "your radio streaming system and your memory vault are both solving the same problem." These are invisible right now.
**Possible approach:** Dedicated cross-community pass after rebuild: embed community summaries, find pairs with cosine > 0.75 in different interest areas, generate `analogous_to` candidates for review.
**Status:** Identified. The embedding infrastructure already exists. ~50 lines of Python.

---

## Ideas Backlog

### IDEA-A: `memory_class` field on atoms (episodic/semantic split)
Prerequisite for GAP-03 and GAP-04. Small schema change, large downstream impact.
Could be auto-classified by Haiku at chunk time: "is this a specific event or a general fact?"

### IDEA-B: Retrieval log + atom weight decay
Two tables: `retrieval_log(atom_id, query, retrieved_at)` + decay function on atom weight.
vault recall becomes a read-write operation. Enables spaced repetition surface eventually.

### IDEA-C: Cross-community analogous_to pass
After rebuild, embed all community summaries, find high-cosine pairs in different interest areas, surface as candidate `analogous_to` bridges. Human review step before writing to graph.

### IDEA-D: Contradiction scanner
After L2 rebuild: for each entity with 5+ atoms, check for temporal contradictions in atom content. Flag pairs where claims are incompatible. Lightweight — just a Haiku pass over entity atom sets.

### IDEA-E: Stable L2 facts table
`facts(id, claim, entities, confidence, source_atom_ids, first_seen, last_confirmed, invalidated_by)`
Populated by promoting high-confidence semantic atoms. Survives atom rotation. Replaces the current community-as-knowledge-store approach for stable facts.

### IDEA-F: Unified retrieval interface
`vault relevant <query> [--project P] [--k N]`
Returns: matched atoms + matched communities + open_threads touching query + recent session context.
One call, composed result, ranked by relevance + recency.

### IDEA-G: Prospective memory layer
Forward-looking: commitments, intentions, "said I'd do" items. Currently `open_threads` captures some of this but they're buried in community summaries, not first-class objects.
`intentions(id, description, project, created_at, due, resolved_at, source_atom_id)`

---

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-17 | Separate graph.db from atoms.db | L1 stays immutable; L2 independently rebuildable |
| 2026-04-17 | Co-occurrence first, typed relations second | Captures 80% of graph structure at zero LLM cost |
| 2026-04-17 | Label propagation over Leiden | Simpler, incremental, no dependencies, sufficient at this scale |
| 2026-04-17 | Typed relation weight=1, no increment | Relations are semantic facts, not frequency counts. Multi-atom backing tracked separately via atom_ids |
| 2026-04-17 | Communities cleared on rebuild | Fully derived; safe to regenerate. Idempotency over versioning for now. |
| 2026-04-17 | Temporal arc added to communities | genesis/evolution/current_state/open_threads as first-class fields, not buried in summary prose |

---

## What to Build Next — Rough Priority

1. **GAP-07 (cross-domain analogous_to pass)** — highest leverage, infrastructure already exists, ~50 lines
2. **IDEA-A (memory_class field)** — schema change that unlocks GAP-03/04; low cost, high downstream value
3. **IDEA-C + GAP-05 (contradiction scanner)** — data quality improvement; real knowledge graph needs this
4. **IDEA-F (unified retrieval)** — UX improvement; makes the system usable as actual working memory
5. **IDEA-B (retrieval log + decay)** — architecturally important but operationally complex; tackle after schema is stable

---

## Open Questions

- Should L3 be a separate DB or live in graph.db as additional tables?
- What's the right decay halflife for episodic atoms? (30 days? 90 days? project-dependent?)
- Should `vault recall` be read-write by default or opt-in (`--reinforce`)?
- How do we handle contradictions that are both true at different times (systemd → Docker migration is valid history, not a bug)?
- Is a `facts` table the right abstraction or should stable semantic memory just be atoms with `memory_class=semantic` and no decay?
