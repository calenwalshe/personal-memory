# Eval: 002-cross-domain-bridges

**Verdict:** The experiment produces genuine cross-domain analogies at mid-range scores (0.51–0.52), but the top result is contaminated by same-project temporal duplication, and the atom-level evidence layer failed on the most surprising bridge.
**Quality score:** 3.1 / 5
**Eval run:** 2026-04-17T21:24:33

---

## Executive Summary

The experiment works at mid-range scores: the geographic query bridge (0.5227) is a clean proof that the method can surface structural analogies invisible to co-occurrence. However, the top result is a false positive caused by same-project temporal duplication — a chunking gap that will pollute precision metrics if not addressed before production. Hold promotion until (1) a deduplication gate is added to suppress same-entity pairs, and (2) atom coverage is audited for ops/security clusters where the evidence layer returned empty.

## Exhibit A — Why This Works

### Geographic query orchestration surfaces across two unrelated domains
**Claim:** The experiment linked Google Maps API venue lookup and multi-region land parcel search (0.5227) — two workflows built for completely different purposes by the same user — by recognizing shared spatial query orchestration structure.
**Evidence:** score=0.5227 Google Maps API skill & venue lookup ↔ Multi-region land parcel search & enrich — no shared vocabulary forced this; both communities share multi-source API aggregation and location-disambiguation patterns.
**Why it matters:** This is the cleanest proof-of-concept: different problem domains, different user intent, same underlying architectural pattern — exactly the class of latent connection the experiment was designed to find.

### Design cluster evolution traced across community snapshots
**Claim:** Three design-adjacent communities (0.7427, 0.5924, 0.5608) form a coherent lineage: gap recognition → tooling discovery → skill integration — a project arc the embedding space reconstructed without explicit timestamps.
**Evidence:** Design Skill + Image Gen Stack appears as a hub connected to both Nano Banana implementation and AI Design Tools Integration Gap, suggesting the embeddings captured project phase, not just topic.
**Why it matters:** If confirmed, this means the bridge method can reconstruct project timelines and identify where work is stuck or converging — a planning use case beyond pure analogy discovery.

### Google Maps ↔ Google Workspace bridge hints at API-surface clustering
**Claim:** The 0.4801 bridge between Maps venue lookup and Google Workspace assistant bridges suggests the embeddings are finding shared API-orchestration patterns rather than just domain labels.
**Evidence:** score=0.4801 Google Maps API skill & venue lookup ↔ Personal Assistant Google Workspace Bridge — both involve stateless skill dispatch over authenticated Google APIs.
**Why it matters:** If this is structural rather than vocabulary-driven, the method could auto-suggest skill reuse across Google-surface tools — reducing implementation duplication.

## Edge Cases

### Same-project temporal duplicates inflate top scores
The highest-scoring pair (0.7427) documents the same Nano Banana + WeasyPrint project at different implementation stages. The bridge method has no mechanism to distinguish 'same project, different time' from 'different project, same structure.'
*Watch:* Add a deduplication gate: if two communities share >2 named entities or >1 tool reference, suppress the pair from bridge output and flag it as a consolidation candidate instead.

### Atom probe returned empty on the most interesting bridge
Challenge 2 (Server Hardening ↔ ChatGPT Automation, 0.4862) had zero supporting atoms — the most surprising cross-domain bridge has no grounded evidence. This could mean atoms weren't chunked for these communities, or the atom layer isn't indexing security/ops content reliably.
*Watch:* Audit atom coverage by community label; if ops/security clusters are systematically under-atomized, the bridge layer will produce unverifiable claims for exactly the most novel connections.

### Design cluster dominates top-8 results (5 of 8 entries)
Five of the eight top bridges involve a design-related community, suggesting the design cluster has unusually high embedding density relative to other domains — possibly because design work generated more documentation surface area.
*Watch:* Normalize scores by community size or atom count before ranking; raw cosine will always favor dense clusters and produce a skewed leaderboard.

## Open Questions

- Is the atom probe failure on Challenge 2 a systematic gap in ops/security atomization, or a one-off indexing miss? Run vault atoms stats filtered by community label to check coverage distribution.
- Can the community embedding method distinguish 'same project, different phase' from 'different project, same pattern'? This is the difference between a deduplication signal and an analogy signal — the experiment needs a labeled test set to measure this.
- What is the minimum meaningful cosine threshold? The 0.48–0.52 range contains the most interesting cross-domain bridges; below that, how quickly does signal degrade into vocabulary noise?

## Challenge Verdicts

**EDGE-CASE** — Design stack similarity or surface-level noise?
The 0.7427 score is real but misleading: both communities document the *same evolving project* (Nano Banana + WeasyPrint design stack) from different temporal vantage points — Community A is post-implementation, Community B is pre-integration planning. This is temporal duplication masquerading as cross-domain similarity, not a latent analogy the system surfaced. The high cosine is diagnostic of a chunking/deduplication gap, not evidence that the bridge method works.

**INCONCLUSIVE** — Server hardening ↔ ChatGPT automation: meaningful analogy or embedding artifact?
The atom probe returned zero results (atom_count: 0), making it impossible to rule on whether the 0.4862 cosine reflects structural analogy (adversarial session control, credential hygiene) or vocabulary overlap (SSH, tokens, scripts). The evidence layer is absent, not negative — this is a pipeline gap, not a confirmed false positive.

**INCONCLUSIVE** — Memory architecture ↔ session execution: is this a true structural analogy?
The challenge text was truncated and no atom evidence was supplied, so no ruling can be made. The 0.5134 score between Personal Memory Architecture and Persistent session execution is plausible on structural grounds (both are pipeline systems with state management, cursor tracking, and durability concerns), but without atom-level grounding this remains an untested hypothesis.
