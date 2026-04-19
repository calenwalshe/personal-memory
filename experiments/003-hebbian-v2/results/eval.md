# Eval: 003-hebbian-v2

**Verdict:** The uncapped Hebbian update encodes genuine magnitude signal in at least one case (Codex ↔ config.toml) but produces a saturated clique at the top that obscures rather than differentiates the strongest associations.
**Quality score:** 2.8 / 5
**Eval run:** 2026-04-18T05:14:39

---

## Executive Summary

Do not promote 003 as-is. The experiment proves the core hypothesis in the mid-range (Codex ↔ config.toml is a clean, semantically grounded result), but the top of the ranking is dominated by a three-entity saturation clique that tells you nothing except that autoresearch-memory, Cortex, and window_classifier are omnipresent — which you already knew. The 21.5 max_weight anomaly also suggests the 002 graph baseline is contaminating results. The fix is small: frequency normalization (PMI or Jaccard) before the Hebbian update, plus zero-initialization on the next run — if those two changes preserve the mid-range signal while clearing the clique, 004 is promotable.

## Exhibit A — Why This Works

### Codex ↔ config.toml: Semantic Coupling That Earns Its Weight
**Claim:** Uncapped weights allow a mid-tier pair (Δw=13.5, coact=45) to surface a specific, actionable technical relationship that a binary co-presence model would bury under noise.
**Evidence:** Atom: 'Codex batch calls have 5.4x token overhead due to full CLAUDE.md injection; fixable by excluding project documentation via config.toml `project_doc_fallback_filenames` setting.' Topic: codex-optimization. Both atoms confirm a direct causal link.
**Why it matters:** This is exactly the retrieval the system is supposed to enable — a concrete fix, not a generic association — and it only surfaces because magnitude encoding promotes it above threshold.

### Δw Proportionality Holds in the Mid-Range
**Claim:** The coact→Δw mapping is monotone and proportional across the non-saturated range: coact=65→Δw=19.5, coact=49→Δw=14.7, coact=45→Δw=13.5, coact=41→Δw=12.3.
**Evidence:** Top-8 results show a clean linear relationship between shared session count and Δw for all pairs below the saturation ceiling, confirming the hypothesis holds where it matters most.
**Why it matters:** The proportionality gives the graph a meaningful gradient for downstream ranking — retrieval can now distinguish 'strongly correlated' from 'somewhat correlated' rather than treating all co-occurring pairs as equal.

### extract_sessions.py ↔ GraphRAG: Independent Signal in the Top 8
**Claim:** At coact=45 and Δw=13.5, this pair is not part of the omnipresent clique and represents a structurally independent co-activation signal, validating that the uncapped update finds real relationships outside the saturated top.
**Evidence:** Neither extract_sessions.py nor GraphRAG appears in any other top-8 pair, confirming this is not a hub-and-spoke artifact from the autoresearch-memory/Cortex/window_classifier clique.
**Why it matters:** Demonstrates the experiment produces value outside its worst-case pathology — the top is noisy but the mid-range is genuinely informative.

## Edge Cases

### 002 Graph State Contamination
max_weight=21.5 exceeds the theoretical Δw ceiling of 19.5, indicating at least one pair carried a non-zero base weight from the forked 002 graph. Raw weight cannot be treated as a clean signal without knowing which pairs started from zero.
*Watch:* Before promoting, audit all pairs where weight > 19.5 and verify whether the excess derives from inherited 002 state or genuine new co-activation. Consider enforcing a graph-reset (zero initialization) on all future experiments to prevent baseline contamination.

### Omnipresent Entity Saturation
Entities present in 60%+ of sessions (autoresearch-memory, Cortex, window_classifier) will always hit the Δw ceiling with each other regardless of semantic content, crowding the top-N results with clique noise.
*Watch:* Apply a frequency-normalized co-activation score (e.g., PMI or Jaccard) as a pre-filter before Hebbian update, or cap weight contribution from entities exceeding a session-frequency threshold.

### Community Collapse — Unverifiable but Plausible
The community membership payload returned empty for the knowledge-consolidation-engine cluster, making it impossible to confirm or deny that uncapped weights merged formerly distinct communities. The top-8 structure is consistent with hub absorption.
*Watch:* Re-run the community probe with corrected tooling and compare membership lists against 002 before making any production decision. Community structure is a key safety signal for retrieval quality.

## Open Questions

- What is the session-frequency distribution of entities in the full graph — how many entities are in the >60% saturation zone, and what fraction of total edge weight do they account for?
- Does resetting the 003 graph to zero initialization and re-running the update change the max_weight ceiling, and if so by how much — this directly tests whether the 21.5 anomaly is inherited state or a new artifact?
- Would a PMI-normalized variant of the Hebbian update preserve the Codex ↔ config.toml signal while suppressing the autoresearch-memory/Cortex/window_classifier clique — and at what normalization strength does the mid-range signal degrade?

## Challenge Verdicts

**SPURIOUS** — Top trio saturation collision
autoresearch-memory (73 sessions), Cortex (108 sessions), and window_classifier (65 sessions) form a near-complete overlap clique — autoresearch-memory ↔ window_classifier shares 49 of 65 sessions (67%), confirming the Δw=19.5 ceiling is hit because all three entities are near-omnipresent, not because they are semantically coupled. The top two Δw=19.5 results are clique artifacts, not independent co-activation evidence.

**VALID** — Codex ↔ config.toml semantic legitimacy
Both atoms tie Codex and config.toml to a specific technical coupling: CLAUDE.md injection inflating token costs, fixed via config.toml's `project_doc_fallback_filenames` setting. This is a reproducible, semantically coherent relationship — not a boilerplate-touch artifact — making Codex ↔ config.toml the experiment's cleanest signal.

**EDGE-CASE** — Max weight ceiling breach
The max_weight of 21.5 belongs to a pair outside the top-8 list, meaning a numerical artifact — almost certainly a non-zero base weight inherited from the forked 002 graph — pushed it past the theoretical Δw ceiling of 19.5. This is not a co-activation record; it is a graph-state contamination artifact that will silently distort any ranking that treats raw weight as a clean signal.

**EDGE-CASE** — Community collapse under weight amplification
Evidence for the community-collapse claim is inconclusive — the community payload returned empty — but the structural pattern in the top-8 results (autoresearch-memory, Cortex, window_classifier, and knowledge-consolidation-engine appearing in six of eight pairs) is consistent with a dominant hub absorbing formerly distinct clusters. Without community membership data the collapse hypothesis cannot be confirmed, but the risk is real and worth auditing before promotion.
