# L2 Design Research: Production Memory Aggregation Systems

**Researched:** 2026-04-17
**Method:** power-search (perplexity) x9 queries
**Overall Confidence:** MEDIUM (architectures confirmed via multiple sources; internal implementation details are inferred from papers/docs, not source code)

---

## 1. Zep / Graphiti (Temporal Knowledge Graph)

**Most architecturally relevant to our L2 problem.**

**Atomic unit:** Episodic node -- raw event (message, JSON blob, conversation turn) stored with bitemporal timestamps: event time T (when it happened) and ingestion time T' (when recorded). Directly analogous to our L1 atoms.

**Aggregation layer (L2 equivalent):** Three-tier subgraph hierarchy:
1. **Episodic subgraph** -- raw events with timestamps (our L1)
2. **Semantic entity subgraph** -- entities and facts extracted from episodes via LLM. Entities embedded in 1024D space. Edges form hyperedges for multi-entity relations. Facts carry provenance back to source episodes.
3. **Community subgraph** -- clusters of connected entities detected via dynamic label propagation. Each community gets an LLM-generated summary capturing domain context.

**Trigger mechanism:** Real-time on ingestion. When a new episode arrives:
1. Extract entities/facts into semantic subgraph (LLM call)
2. Assign new entity to existing community via neighbor plurality vote (no LLM, just graph algorithm)
3. Update community summary (LLM call)
4. Periodic full rebuild to correct label propagation drift

**Data model:**
- Nodes: entities with embeddings + metadata
- Edges: typed relations with temporal validity windows
- Communities: sets of connected entities + generated summary text
- All edges traceable to source episodes (provenance chain)

**Key insight for us:** Zep separates entity extraction (per-event, incremental) from community formation (graph algorithm, periodic rebuild). The community summary is the true L2 artifact -- it aggregates across many episodes into a coherent domain description.

**Performance:** Sub-200ms retrieval, 94.8% accuracy on Deep Memory Retrieval benchmark. Community detection uses label propagation (not Leiden) specifically because it supports incremental updates.

---

## 2. Mem0 (Vector + Graph Hybrid)

**Atomic unit:** A "memory" -- an extracted fact from conversation, stored as text + dense embedding in vector DB. Scoped to user, session, or agent.

**Aggregation layer (L2 equivalent):** Two mechanisms:
1. **Vector consolidation** -- on each new extraction, compute embedding, retrieve similar memories by cosine similarity, then LLM classifies: ADD (new), UPDATE (replace if more informative), DELETE (conflicting), or NOOP. This is deduplication/refinement, not true aggregation.
2. **Graph mode** -- memories become nodes with explicit relational edges. Graph traversal enables multi-hop reasoning across related memories. Hybrid retrieval combines vector similarity + graph neighborhood.

**Trigger mechanism:** Synchronous per-conversation-turn. Every message triggers the extract-consolidate pipeline. No batch or time-based triggers -- it is inline.

**Data model:**
- Memory record: text content + embedding + metadata (user_id, agent_id, session_id)
- Graph mode adds: typed edges between memory nodes
- Eviction: LRU deletion or memory decay for storage management

**Key insight for us:** Mem0's "consolidation" is really deduplication -- it prevents duplicate/contradictory memories but does not produce higher-order summaries. The graph mode is closer to true L2 but is bolted on, not the primary path. Their 4-action classification (ADD/UPDATE/DELETE/NOOP) is a clean pattern we could adopt for L1 maintenance.

---

## 3. MemGPT / Letta (Virtual Context Management)

**Atomic unit:** A message or event in the recall memory tier -- full conversation turns stored with timestamps, searchable by text/time/embedding.

**Aggregation layer (L2 equivalent):** Two tiers above raw recall:
1. **Working context (core memory)** -- fixed-size writable scratch space the LLM actively maintains. Contains distilled facts, user preferences, persona details. Updated via explicit tool calls (`core_memory_append`, `core_memory_replace`).
2. **Archival memory** -- unbounded vector DB for overflow. Chunked, embedded, similarity-searchable.

There is no true aggregation layer. The LLM IS the aggregation mechanism -- it decides what to promote from recall to core/archival via its own reasoning. No separate consolidation process.

**Trigger mechanism:** Memory pressure. When context queue hits ~70% capacity, system alert fires. The LLM then:
1. Receives "memory pressure" notification
2. Decides what to preserve (writes to working context or archival)
3. Oldest messages evict via recursive summarization

**Data model:**
- Core memory: key-value blocks (persona, human, custom sections), size-limited
- Recall memory: timestamped message log with embeddings
- Archival memory: chunked text passages with embeddings in vector DB

**Key insight for us:** MemGPT has no offline aggregation -- it relies entirely on the LLM's in-context reasoning to manage memory hierarchy. This is elegant for single-agent but does not scale to our case (multi-session, no persistent agent). The memory-pressure trigger pattern is interesting but our chunker already handles the L0-to-L1 boundary more systematically.

---

## 4. LangMem (LangChain)

**Atomic unit:** A typed memory record defined by a Pydantic schema. Schemas are user-defined (e.g., FoodPreference with fields: food_name, cuisine, preference_score, description). Stored with embeddings in a BaseStore.

**Aggregation layer (L2 equivalent):** No explicit aggregation layer. LangMem's "consolidation" means:
1. Accept conversation + current memory state
2. LLM determines: create new memories, update existing, delete invalidated
3. Return updated memory state

Operations: INSERT, UPDATE (replace if info changed), DELETE (via RemoveDoc). Organized in hierarchical namespaces like `("memories", "{user_id}")`.

**Trigger mechanism:** Explicit invocation via `manager.invoke()` or `manager.ainvoke()`. No automatic triggers -- the application calls consolidation when it wants. Typically after each conversation turn or at session end.

**Data model:**
- Memory: Pydantic model instance + embedding + namespace tuple + key
- Retrieval: by key, by semantic similarity, or by metadata filter
- Namespace: hierarchical tuple for scoping (org, user, app)

**Key insight for us:** LangMem is the most explicit about its data model (Pydantic schemas) but the least sophisticated about aggregation. It is really a typed memory CRUD layer with LLM-driven merge logic. The namespace hierarchy is useful -- our atoms already have project scoping but could benefit from finer namespacing. The p95 search latency of 59.82 seconds on LOCOMO benchmark is a cautionary data point.

---

## 5. Microsoft Recall

**Atomic unit:** A screenshot ("snapshot") of the active window, taken every few seconds. Processed on-device via NPU to extract text, images, URLs.

**Aggregation layer (L2 equivalent):** Semantic vector index. Extracted content from each snapshot is embedded into vectors that cluster by semantic similarity. The index is the aggregation -- it implicitly groups related captures without explicit entity/topic extraction.

**Trigger mechanism:** Continuous time-based capture (every few seconds). Indexing happens on-device asynchronously. No consolidation in the memory-system sense -- it is pure append + index.

**Data model:**
- Snapshot: image + extracted text + URL + timestamp
- Vector index: embeddings of extracted content, encrypted via BitLocker
- Retrieval: natural language query converted to vector, similarity search returns matching snapshots in visual timeline

**Key insight for us:** Recall is a pure capture+index system with no aggregation layer at all. It relies entirely on retrieval-time search rather than write-time consolidation. This is the opposite architectural choice from what we need -- our atoms are already indexed, the gap is producing higher-order summaries.

---

## 6. Obsidian Ecosystem (PKM Community Patterns)

**Atomic unit:** A note (markdown file). In Zettelkasten practice, one idea per note.

**Aggregation layer (L2 equivalent):** Multiple community-built approaches:
1. **Dataview** -- treats notes as a queryable database. Aggregation is query-time: `LIST FROM #tag WHERE length(file.inlinks) > 5` surfaces highly-connected notes. No persistent aggregation artifacts.
2. **Smart Connections** -- vector embeddings of notes, AI-suggested links between semantically similar notes. Surfaces implicit relationships. Still query-time, no persistent L2.
3. **Cognee** -- LLM-powered entity/relation extraction from vault, exports a knowledge graph. This is the closest to write-time L2: ingests notes, produces entity graph as a persistent artifact.
4. **Manual MOCs (Maps of Content)** -- human-curated index notes that aggregate related atomics. The original L2 pattern -- a note that references and summarizes a cluster of notes.

**Trigger mechanism:** Varies. Dataview/Smart Connections are query-time. Cognee is batch (explicit run). MOCs are manual.

**Key insight for us:** The Obsidian community has converged on two L2 patterns: (a) query-time aggregation via embeddings/Dataview, and (b) explicit MOC notes that are human- or LLM-authored summaries of note clusters. MOCs are exactly what our L2 should produce -- a persistent document that synthesizes a cluster of atoms into a coherent narrative. The question is whether to generate them at write-time, query-time, or batch.

---

## Synthesis: Patterns for Our L2

### What the field converges on

| Pattern | Systems Using It | Our Applicability |
|---------|-----------------|-------------------|
| LLM-driven extract on ingest | Zep, Mem0, LangMem | Already doing this at L1 (chunker) |
| Entity/fact extraction into graph | Zep, Mem0 (graph mode), Cognee | Strong candidate for L2 entity layer |
| Community detection / clustering | Zep (label propagation) | Strong candidate for L2 topic clusters |
| LLM-generated cluster summaries | Zep (community summaries) | Direct analog to what L2 should produce |
| Dedup/merge on similar memories | Mem0 (ADD/UPDATE/DELETE/NOOP) | Useful for L1 maintenance, not L2 |
| Query-time aggregation only | Recall, Dataview, Smart Connections | Insufficient alone -- we need persistent artifacts |
| Human/LLM-authored MOCs | Obsidian community | Target output format for L2 |

### Recommended L2 architecture (evidence-based)

**Closest production analog: Zep's three-tier subgraph.**

1. **Entity extraction** from L1 atoms (already partially done -- atoms have `entities` field). Formalize into entity nodes with embeddings.
2. **Relation extraction** between entities across atoms. Build lightweight graph.
3. **Community detection** via label propagation on entity graph. Incremental on new atoms, periodic full rebuild.
4. **Community summaries** via LLM. Each community gets a generated summary = our L2 artifact. Equivalent to Obsidian MOCs but auto-generated.

**Trigger:** Hybrid. Incremental entity/relation extraction when chunker produces new atoms. Community rebuild + summary generation on schedule (daily) or threshold (N new atoms since last rebuild).

**Data model for L2:**
```
entities table:
  id, name, entity_type, embedding, first_seen, last_seen, atom_ids (JSON), metadata

relations table:
  id, source_entity_id, target_entity_id, relation_type, weight, atom_ids (JSON), time_range

communities table:
  id, entity_ids (JSON), summary, generated_at, atom_count, time_range, confidence

-- Plus: community_history for tracking evolution over time
```

### What NOT to copy

- MemGPT's "LLM as memory manager" -- requires persistent agent, does not work for our multi-session case
- Recall's "capture everything, aggregate nothing" -- we already have L1, need to go up not sideways
- LangMem's explicit schema approach -- too rigid for our diverse atom types; better to extract entities dynamically
- Mem0's inline consolidation -- our atoms are already written; L2 should be a separate async process

---

## Confidence Assessment

| Finding | Confidence | Reason |
|---------|-----------|--------|
| Zep/Graphiti architecture | HIGH | Multiple detailed sources, open-source, consistent descriptions |
| Mem0 pipeline (ADD/UPDATE/DELETE/NOOP) | HIGH | Published paper + multiple sources agree |
| MemGPT memory pressure trigger | MEDIUM | Described in original paper, details sparse on exact thresholds |
| LangMem data model | MEDIUM | API docs consistent but limited architectural depth |
| Recall internals | LOW | Closed source, all details inferred from marketing + press |
| Obsidian patterns | MEDIUM | Community-documented but no single authoritative source |
| L2 architecture recommendation | MEDIUM | Synthesized from verified patterns, not battle-tested |

## Sources

All findings from perplexity research queries (9 queries, 2026-04-17). No Context7 or official doc fetches -- these are infrastructure/AI systems not typically in Context7. Cross-verified by requiring 2+ sources to agree on architectural claims.
