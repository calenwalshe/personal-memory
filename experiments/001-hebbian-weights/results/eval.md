# Eval: 001-hebbian-weights

**Verdict:** The Hebbian update rule is saturated at Δw=3.0 across all pairs regardless of co-activation magnitude, meaning the experiment produced a binary presence detector rather than a weighted associative memory — the core hypothesis is not proven.
**Quality score:** 2.0 / 5
**Eval run:** 2026-04-17T21:49:59

---

## Executive Summary

Do not promote this to production. The update rule is saturated — every pair gets the same weight increment regardless of how often they co-occur, which means the system is encoding co-presence (a binary signal) rather than co-activation strength (the gradient the hypothesis requires). The measurement infrastructure is solid and the failure mode is precisely diagnosed, so the fix is targeted: change the update function to scale Δw proportionally to co-activation count, then re-run. Before approving that follow-on experiment, verify that the session-window mismatch in the coactivation probes is resolved, or the next evaluation will have the same blind spot.

## Exhibit A — Why This Works

### Co-activation tracking infrastructure works
**Claim:** The pipeline correctly counts shared session appearances across entity pairs at scale
**Evidence:** coact=65 for 0fed0bd4 ↔ 1d9f3163 vs coact=17 for other pairs — meaningful variance captured across 8 top pairs
**Why it matters:** The measurement substrate is sound; the failure is in the update rule, not the observation layer, so fixing saturation does not require rebuilding the pipeline.

### Hub entity detection is a side-product
**Claim:** High-degree hub entities (01cd72b7 in 4/8 top pairs) are automatically surfaced by weight accumulation
**Evidence:** 01cd72b7 appears in 4 of 8 top-weighted pairs — a structural signal that would be invisible in pairwise static weights
**Why it matters:** Even a broken Hebbian rule exposes hub topology, which is independently useful for identifying over-connected entities that may be degrading community quality.

### Saturation failure is precisely diagnosable
**Claim:** The experiment produced a clear, falsifiable finding rather than ambiguous results
**Evidence:** Δw=3.0 uniformly across coact 17–65 is a crisp diagnostic: the update function hits a ceiling before encoding magnitude
**Why it matters:** A precisely diagnosed failure is cheaper to fix than a vague underperformance — the required change is a single update-rule modification, not an architectural rethink.

## Edge Cases

### Zero-session coactivation for probed pairs
Both coactivation probes for the explicit challenge entities returned sessions_a=0, sessions_b=0, shared=0 — the entities exist in the weight table but have no session history in the query window, suggesting the probe window and the training window are misaligned.
*Watch:* Verify that the session window used to compute coact in the weight table matches the window used by the evaluation probe; a mismatch would silently invalidate all coactivation evidence.

### Empty atom store for hub entity pair
The atom query for 01cd72b7 ↔ 5b0117e3 returned zero atoms despite both entities appearing in top-weighted pairs, meaning hub-inflation cannot be ruled in or out.
*Watch:* Check whether atom provenance is correctly linking entities to atoms; if the join is broken, community coherence scores have no semantic grounding and cannot be evaluated.

### Community size explosion without community data
The 36→69 member growth is reported in the summary but the community probe returned empty — the most structurally significant event in the experiment has no inspectable evidence.
*Watch:* Ensure community snapshots are persisted before and after each Hebbian update cycle, not just at experiment end, so merge events can be reconstructed post-hoc.

## Open Questions

- Is the Δw=3.0 ceiling a hard cap in the update function, or is it an artifact of the weight initialization plus a single update cycle — and if the latter, would multiple cycles produce differentiated weights?
- Are the zero coactivation scores in the probes a query-window mismatch, a missing join, or evidence that the named entities genuinely had no session overlap during the experiment period?

## Challenge Verdicts

**VALID** — Weight ceiling masks coactivation signal
The evidence confirms saturation: a pair with coact=65 and a pair with coact=17 both receive identical Δw=3.0, meaning the update rule encodes co-presence, not co-activation magnitude. The weight ceiling is masking the signal the experiment was designed to produce.

**INCONCLUSIVE** — Hub entity 01cd72b7 inflating community cohesion
The atom query for the 01cd72b7 ↔ 5b0117e3 pair returned zero atoms, so the hub-inflation claim cannot be confirmed or denied from this evidence. The structural signature (01cd72b7 appearing in 4 of 8 top pairs) remains suspicious but unverified.

**INCONCLUSIVE** — Max community size explosion (36→69) is a merge, not growth
The community probe returned empty data for both hub entities, so the 36→69 merge cannot be attributed to a specific bridge collapse. The growth pattern is consistent with hub-driven merger but the evidence does not confirm it.

**VALID** — Δw uniformity invalidates the core hypothesis
Every top-8 pair shows Δw=3.0 despite coact ranging 17–65, a 3.8× spread that the weight distribution completely fails to reflect. The Hebbian update adds no rank-order information beyond what raw co-occurrence counts already encode, falsifying the differentiating-power claim.
