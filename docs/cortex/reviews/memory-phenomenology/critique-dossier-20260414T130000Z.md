# Critique: dossier — memory-phenomenology

**Gate:** dossier
**Slug:** memory-phenomenology
**Timestamp:** 2026-04-14T13:00:00Z
**Artifact:** docs/cortex/research/memory-phenomenology/concept-20260414T130000Z.md
**Engine:** codex
**Overall Severity:** STOP

---

## Summary

The dossier has a structural evidence problem: it makes hard product conclusions from weak, low-authority, and poorly traceable sources. It also recommends concrete direction before resolving its own central open question, so the guidance is not execution-safe.

---

## Findings (4 total — STOP: 2, CAUTION: 2, GO: 0)

### [STOP] source authority

**Finding:** The dossier bases multiple core conclusions on low-tier or opaque sources instead of high-authority evidence. Blog posts, Reddit, vendor marketing summaries, and LLM aggregators are used to support central claims about trust failure modes, benchmark validity, and architectural direction.

**Quote from artifact:**
> - **Confident wrong memory is the primary trust-damaging failure mode, not forgetting.** ... [Tavily, chrislema.com]
> ...
> - **Benchmark fragmentation makes external comparison meaningless:** ... Source: reddit.com/r/MachineLearning + emergentmind.com/topics/locomo
> ...
> ## Sources
> - Perplexity: Letta/Mem0/Zep UX comparison (primary q4 research)
> - Gemini: Phenomenological gap challenge (adversarial cross-reference of q4 findings)

**Impact:** Downstream decisions will be anchored to unverified or non-authoritative material, which makes the recommended metric changes and architecture priorities unreliable and likely to waste implementation effort.

---

### [STOP] evidence adequacy

**Finding:** The dossier presents sweeping universal claims as established findings without supplying direct evidence, citations, or traceable comparisons. It asserts field-wide absence of phenomenological goals and declares proactive surfacing to be the mechanism of feeling known, but never shows the underlying evidence chain.

**Quote from artifact:**
> - **No phenomenological design goal exists anywhere in the field.** Letta, Mem0, and Zep describe architecture and integration benefits (token reduction, developer control, temporal accuracy); none has a documented statement of what the target user experience should feel like. This is not a gap in the literature — it is a gap in industry practice. [Perplexity + Gemini challenge]
> ...
> The three design principles that survive the research: precision over recall, temporal metadata is load-bearing, and proactive surfacing (the assistant mentions memory unprompted) is the mechanism through which "being known" is experienced.

**Impact:** Unsubstantiated absolutes will push the project toward premature conclusions and hard-coded design bets that may not be true, especially when replacing existing metrics or changing retrieval behavior.

---

### [CAUTION] traceability

**Finding:** Major recommendations do not cleanly map back to answered research questions; the dossier itself admits the central question remains unresearched, yet it still issues implementation recommendations as if the concept were resolved. That creates orphan findings and recommendations disconnected from the clarify gap.

**Quote from artifact:**
> - **What's still open:** What does "feeling known" mean specifically for developer/technical work — and what is the minimum viable, testable expression of that phenomenology in a Claude Code context?
> ...
> - **Adopt a testable phenomenological design goal:** "The user should occasionally feel their assistant understood something about them without being asked."
> ...
> - What does "feeling known" mean specifically for developer/technical work in Claude Code — and what is the minimum viable, testable expression of that phenomenology? (Not researched — self-check gap; likely the next clarify iteration)

**Impact:** The team can start building against a definition that the research explicitly did not answer, creating rework when the actual Claude Code use case turns out to need different behavior.

---

### [CAUTION] assumption backing

**Finding:** The dossier treats its core thesis as proven when it is still an assumption: that phenomenological success is best captured by spontaneous cross-session reuse and that precision should dominate recall. Those are design hypotheses, not demonstrated conclusions from the cited material.

**Quote from artifact:**
> And success should ultimately be measured by whether facts extracted in one session spontaneously shape another session, not by extraction accuracy alone.
> ...
> - **Raising recall naively is the wrong direction.** ... Precision should be the primary optimization target.

**Impact:** If these assumptions are wrong, the project will optimize the wrong metric and tune the classifier in the wrong direction, undermining both recall coverage and actual user value.

---
