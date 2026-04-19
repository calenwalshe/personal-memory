# L2 Aggregation Layer — Theoretical Foundations

**Researched:** 2026-04-17
**Domain:** Episodic-to-semantic memory consolidation
**Overall confidence:** MEDIUM (academic theory is well-established; mapping to L2 design is interpretive)
**Sources:** power-search via Perplexity (8 queries, RESEARCH intent)

---

## 1. Complementary Learning Systems (CLS)

### Core mechanism

McClelland, McNaughton & O'Reilly (1995) showed the brain uses two learning systems with opposing properties. The hippocampus learns fast via sparse, pattern-separated representations (one-shot episodic encoding). The neocortex learns slow via distributed, overlapping representations (gradual extraction of statistical structure). Hippocampal traces are *replayed* during sleep/rest, interleaving new memories with old, allowing the neocortex to integrate without catastrophic interference — overwriting prior knowledge.

The computational key: replay interleaves new with old. Without interleaving, the neocortex overwrites. This is why simple "replace old summary with new summary" fails.

In AI/ML, CLS maps directly to experience replay buffers in continual learning. CLEAR (Rolnick et al.) mixes on-policy new data with off-policy replay at ~50/50 ratio, nearly eliminating forgetting. Elastic Weight Consolidation (EWC, Kirkpatrick et al. 2017) protects important parameters from overwriting. Speed-Based Sampling (2025 ICML) selects replay examples by learning speed.

### L2 design implications

- **Never overwrite L2 aggregates in-place.** Generate new versions that interleave old aggregate content with new L1 atoms. The old aggregate is replay; new atoms are the fresh episode.
- **Maintain an interleaving ratio.** When regenerating an L2 summary (e.g., "project X state"), include ~50% prior aggregate content alongside new atoms. This prevents catastrophic forgetting of older context.
- **Keep L1 atoms immutable.** L1 is the hippocampus — fast, specific, never modified. L2 is the neocortex — slow, merged, regenerated. The two layers must remain structurally separate.

### Failure modes

- Replaying everything equally causes "memory saturation" — too many old traces block new learning. Need recency weighting.
- Too little replay causes drift — aggregate loses older knowledge silently.

---

## 2. Memory Indexing Theory

### Core mechanism

Teyler & DiScenna (1986) proposed that the hippocampus stores *indexes* — compact pointers to distributed neocortical activation patterns — not the memories themselves. CA3 acts as an autoassociator: given a partial cue, it completes the index, which then reinstates the full neocortical pattern.

Good indexes have three properties: (1) bind to multiple modalities of the episode (what, where, when, why), (2) are pattern-separated enough to not collide with similar episodes, (3) carry temporal context as an intrinsic dimension. Temporal context is not metadata — it is part of the encoding itself (SCERT theory: encoding-specific oscillatory patterns).

### L2 design implications

- **L2 aggregates must maintain an index back to constituent L1 atoms.** The aggregate is not a replacement — it is a *compression with provenance*. Every claim in an L2 entity card or project summary should trace to specific atom IDs.
- **Index on (entity, temporal_context, causal_role), not just entity name.** An entity appearing in a failure atom vs. a decision atom has different causal roles. The index must distinguish these.
- **Support partial-cue retrieval.** Query "what went wrong with X?" should activate failure-typed atoms for entity X via the index, not require full-text search.
- **Temporal context is a first-class index dimension.** Not just "last updated" metadata, but encoded into the retrieval path: "X in the context of March debugging" vs. "X in the context of April refactor."

### Failure modes

- Indexes that only store entity names lose causal context and return irrelevant matches.
- Over-separation (every atom gets unique index) defeats the purpose — no compression.

---

## 3. Schema Theory and Knowledge Compilation

### Core mechanism

Bartlett (1932), refined by Piaget and 1970s cognitive science. Schemas form when repeated similar experiences decrease in information density — the redundant structure becomes extractable. Three evolution mechanisms: *assimilation* (new data fits existing schema), *accommodation* (schema restructures when data creates dissonance), *tuning* (gradual refinement).

The trigger for abstraction vs. keeping episodes separate is information density. When a new episode adds little new information relative to prior episodes of the same type, it gets assimilated into the schema. When it adds high-density novel information, it remains episodic. Crucially, causal relationships can form a schema from a single episode, while recurring-pattern schemas require multiple episodes.

### L2 design implications

- **Use information density as the consolidation trigger.** When a new L1 atom overlaps >70% with an existing L2 aggregate (entity overlap, topic match), assimilate it — update the aggregate. When overlap is <30%, keep the atom as a standalone episode reference. The 30-70% middle zone is accommodation territory: update the aggregate AND flag the atom as a notable deviation.
- **Track schema confidence by repetition count.** An L2 pattern derived from 1 atom is a hypothesis. From 3+ atoms it is a schema. From 10+ it is stable knowledge. Expose this count.
- **Separate causal schemas from pattern schemas.** A single decision atom establishing "we chose X because Y" is a causal schema — immediately promotable to L2. A pattern like "tests tend to fail on Mondays" needs multiple atoms before it becomes L2.

### Failure modes

- Premature schema formation: abstracting too early loses important episodic detail (the exception that proves the rule).
- Schema rigidity: once formed, schemas resist disconfirming evidence. Need explicit accommodation triggers.

---

## 4. GraphRAG Community Detection as Consolidation Analog

### Core mechanism

Microsoft's GraphRAG (Edge et al. 2024) extracts entities and relationships from text into a graph, then applies the Hierarchical Leiden algorithm to detect communities — groups of densely connected entities. Each community gets an LLM-generated summary at multiple hierarchy levels (C0 root, C1-C3 intermediate). Intermediate levels (C1-C3) outperform root-level summaries in comprehensiveness with 26-97% fewer tokens.

The biological analog: community detection parallels how the neocortex groups related concepts. The hierarchy parallels levels of abstraction. The summary generation parallels schema compilation. But unlike biological consolidation, GraphRAG is stateless — it rebuilds from scratch each time, with no incremental replay.

### L2 design implications

- **Build an entity-relationship graph from L1 atoms.** Each atom mentions entities and files — these form nodes. Co-occurrence within an atom forms edges. Apply community detection to find natural clusters.
- **Generate L2 summaries at intermediate granularity, not root level.** Per-project or per-entity-cluster summaries. Root-level "everything" summaries lose too much.
- **Make consolidation incremental, not batch.** GraphRAG's rebuild-from-scratch approach is too expensive for a personal memory system. Instead: maintain a persistent entity graph, add new atoms' entities/edges incrementally, re-run community detection only when graph structure changes meaningfully (>N new edges since last run).
- **Use hierarchy levels as retrieval scopes.** Global query ("what's the state of everything?") hits C1. Specific query ("what happened with auth tokens?") hits leaf communities.

### Failure modes

- **Information loss during summarization.** LLM-generated community summaries hallucinate or omit details. Mitigation: summaries link back to constituent atoms (per indexing theory above).
- **Context window truncation.** Large communities exceed LLM context. Mitigation: cap community size, split large communities.
- **Entity resolution failures.** "auth module" vs "authentication system" vs "the auth code" — same entity, different strings. Need entity normalization before graph construction.
- **Stale communities.** Graph structure changes but communities are not recomputed. Need change-detection triggers.

---

## 5. Temporal Decay and Spaced Repetition

### Core mechanism

Ebbinghaus (1885, replicated Murre & Dros 2015): retention follows R = e^(-t/S) where S is memory stability and t is time since encoding. Each successful retrieval increases S, pushing the next forgetting threshold further out. Anki's FSRS algorithm (v23+) models this with machine-learned weights over (stability, retrievability, difficulty) per item, scheduling reviews at predicted forgetting points.

Key insight: decay is not deletion — it is *reduced retrievability*. The memory trace still exists but becomes harder to activate. Spaced retrieval at expanding intervals is 200%+ more effective than massed review (Cepeda et al. 2006 meta-analysis, 184 studies).

### L2 design implications

- **Decay-weighted retrieval, not static aggregation.** When querying L2 aggregates for context injection, weight by recency: recent atoms contribute more to the aggregate surface. But do not delete old atoms — they remain available for deep retrieval.
- **Track retrieval count per L2 aggregate.** Each time an aggregate is surfaced (e.g., in SessionStart context), increment a counter and timestamp. Frequently retrieved aggregates gain stability (higher S). Unused aggregates decay in retrieval priority but not in storage.
- **Use access patterns to prioritize consolidation.** Atoms/aggregates that are retrieved often should be consolidated more aggressively (higher quality summaries, more cross-links). Rarely accessed ones can remain as raw atom collections.
- **Implement expanding intervals for aggregate refresh.** First refresh after 1 day of new atoms, then 3 days, then 1 week. If no new atoms arrive for a topic, stop refreshing — the aggregate is stable.

### Failure modes

- Pure recency bias loses important but old knowledge (the "foundational decision" problem). Mitigation: atoms tagged as `decision` or `architecture` type get a stability floor — they never fully decay.
- Over-refreshing wastes LLM budget on stable aggregates. Need change-detection: only refresh when new atoms actually change the picture.

---

## Synthesis: L2 Architecture Recommendations

Combining all five theories into concrete architectural guidance:

### 1. Dual-store architecture (from CLS)
L1 atoms are the fast store (hippocampus). L2 aggregates are the slow store (neocortex). Never modify L1. Regenerate L2 by interleaving prior aggregate with new atoms.

### 2. Entity graph as index structure (from Indexing Theory + GraphRAG)
Maintain a persistent entity-relationship graph built incrementally from L1 atoms. Entities indexed by (name, type, causal_role, temporal_window). Community detection groups entities into natural clusters. Each cluster gets an L2 summary.

### 3. Information density as consolidation trigger (from Schema Theory)
New atoms are scored for overlap with existing L2 aggregates. High overlap = assimilate (update aggregate). Low overlap = keep episodic (just index). Medium overlap = accommodate (update + flag deviation).

### 4. Decay-weighted retrieval with stability floors (from Spaced Repetition)
Retrieval ranking uses R = e^(-t/S) where S increases with retrieval frequency. Decision/architecture atoms get a stability floor (S_min = 30 days). Access patterns drive consolidation priority.

### 5. Hierarchical summaries with provenance (from GraphRAG + Indexing)
L2 summaries exist at multiple granularity levels. Every claim traces to constituent L1 atom IDs. Intermediate granularity (per-project, per-entity-cluster) outperforms both too-specific and too-general.

### Processing model
```
Trigger: New L1 atoms arrive (from chunker at PreCompact/SessionEnd)

1. Entity extraction: Pull entities/relationships from new atoms
2. Graph update: Add to persistent entity-relationship graph
3. Overlap scoring: Compare new atoms against existing L2 aggregates
4. Route:
   - High overlap (>70%): Assimilate — schedule aggregate refresh
   - Medium overlap (30-70%): Accommodate — refresh + flag deviation
   - Low overlap (<30%): Index only — atom stays episodic
5. Community check: If graph structure changed significantly, re-run Leiden
6. Refresh: For scheduled aggregates, regenerate by interleaving
   old aggregate + new atoms (50/50 ratio)
7. Decay update: Adjust retrievability scores based on access patterns
```

---

## Sources

All findings from power-search (Perplexity, RESEARCH intent), 2026-04-17:

- McClelland, McNaughton & O'Reilly (1995). "Why there are complementary learning systems in the hippocampus and neocortex." *Psychological Review* 102(3).
- Teyler & DiScenna (1986, revised 2007). Hippocampal indexing theory.
- Bartlett (1932). *Remembering: A Study in Experimental and Social Psychology.*
- Edge et al. (2024). "From Local to Global: A Graph RAG Approach to Query-Focused Summarization." Microsoft Research. arXiv:2404.16130.
- Ebbinghaus (1885). *Memory: A Contribution to Experimental Psychology.* Replicated by Murre & Dros (2015).
- Kirkpatrick et al. (2017). "Overcoming catastrophic forgetting in neural networks." *PNAS* 114(13).
- Rolnick et al. (2019). "Experience Replay for Continual Learning." *NeurIPS*.
- Cepeda et al. (2006). "Distributed practice in verbal recall tasks." *Psychological Bulletin* 132(3).
- FSRS algorithm documentation, Anki 23+.

**Confidence note:** The academic theories are HIGH confidence (well-replicated, foundational). The L2 design implications are MEDIUM confidence — they are my interpretive mapping from theory to architecture, not established practice. The specific thresholds (70%, 50/50 ratio, 30-day floor) are starting points requiring empirical tuning.
