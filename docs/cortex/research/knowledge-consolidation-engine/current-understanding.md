---
slug: knowledge-consolidation-engine
brief_iteration: 1
last_updated: 2026-04-19
---

# Current Understanding: knowledge-consolidation-engine

## Possible Terminals

| Terminal | Status | Ruled-Out Reason | Evidence |
|----------|--------|------------------|----------|
| commit-to-build | **live** | — | Full architecture designed, agreed scope, critique-validated |
| kill-with-learning | ruled-out | Problem is real (chat-only memory is limiting) | 233K-char research doc confirms gap |
| decompose | live | — | Could split into L0-intake + L3-beliefs, but v1 scope is manageable |
| experiment-required | ruled-out | Existing 415 atoms + 1258 facts sufficient for testing | — |
| already-exists | ruled-out | No existing system combines universal intake + Kripke belief tracking | Compared Mem0, Letta, Graphiti, Cognee, TeleMem |
| hold-on-dependency | ruled-out | All dependencies (SQLite, sentence-transformers, FAISS, Haiku) available | — |

## Research Sources

1. **ChatGPT memory system discussion** — 233K chars, deduped to 139K, indexed into 53 chunks (52 actionable). Covers: vault architecture review, comparison with 5 external systems, gap analysis, L3 logical inference design, universal intake architecture, modular L3 modules.
   - Local: `docs/chatgpt-memory-discussion-deduped.md`
   - Index: `docs/chatgpt-memory-discussion-index.md`

2. **Adversarial critique** — ChatGPT reviewed the full proposal. 8 critique points, 5 accepted, 3 pushed back (L2 run tracking premature, evidence_units table deferred, atoms extension sufficient).

3. **Existing codebase** — atom_store.py, graph_store.py, fact_store.py, chunker.py, hebbian.py fully read and analyzed.

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Separate sources.db from events.db | events.db = live telemetry; sources.db = deliberate knowledge intake |
| source_segments table | Sources and chunks/spans are different things; prevents re-chunking from creating duplicate sources |
| Extend atoms.db, don't create evidence_units table | 95% of value, fraction of migration cost |
| New beliefs.db (not extending facts.db) | facts.db has wrong conceptual model (flat facts, no worlds/status/inference) |
| Namespaced modules (enable/disable) not hot-swap | Multiple modules can coexist (personal:*, cortex:*, research:*) without incompatible realities |
| Pure Python inference rules, not Datalog/Soufflé | Covers 80% of value; swap in Soufflé later if needed |
| 4 starter rules (conflict, supersede, stable, lesson) | Scale rules as use cases emerge; premature rules add noise |

## Iteration History

| Iter | Brief | Dossier | Reframe Reason |
|------|-------|---------|----------------|
| 1 | 20260419T030000Z | this document | (initial — synthesized from extensive ChatGPT research + codebase analysis) |
