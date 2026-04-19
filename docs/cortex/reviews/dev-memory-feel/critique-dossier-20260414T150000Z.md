# Critique: dossier — dev-memory-feel

**Gate:** dossier
**Slug:** dev-memory-feel
**Timestamp:** 2026-04-14T15:00:00Z
**Artifact:** docs/cortex/research/dev-memory-feel/concept-20260414T150000Z.md
**Engine:** codex
**Overall Severity:** STOP

---

## Summary

This dossier is not ready to drive execution. Its main recommendations rest on weak sources, an unsubstantiated competitive claim, and conclusions that outrun the evidence and traceability provided in the artifact.

---

## Findings (4 total — STOP: 2, CAUTION: 2, GO: 0)

### [STOP] source authority

**Finding:** The dossier builds core product conclusions on low-tier marketing/blog sources instead of high-authority evidence. The central workflow-moment and taxonomy claims are supported by vendor blogs and consultancy content, which is not a defensible basis for a concept dossier that is driving implementation direction.

**Quote from artifact:**
> Sources: super-productivity.com/blog/context-switching-costs-for-developers, basicops.com/blog/the-hidden-cost-of-context-switching

**Impact:** Downstream decisions about where and how to inject memory will be anchored to weak evidence, which makes the MVP design easy to invalidate and likely to waste implementation effort on a false priority.

---

### [STOP] evidence adequacy

**Finding:** The competitive landscape conclusion is asserted without evidence. The dossier declares a market-wide absence of cross-session episodic memory and calls it a "genuine product gap," but provides no competitor citations, feature docs, or comparative evidence anywhere in the artifact.

**Quote from artifact:**
> Finding: No competitor has cross-session episodic memory for developer workflow.

**Impact:** This can send execution into building a supposedly differentiated feature that may already exist, collapsing the product thesis and invalidating prioritization, positioning, and scope.

---

### [CAUTION] traceability

**Finding:** The dossier introduces decisive claims that do not map cleanly to the stated question set and then uses them to drive the terminal recommendation. The trust/consent conclusion is elevated to a "critical constraint" even though it is not one of the answered questions and is only parked as an adjacent finding.

**Quote from artifact:**
> Critical constraint: trust and consent must be solved before phenomenology; 81% of developers report security/privacy concerns about AI tools, and "being known" flips to surveillance anxiety if the developer does not control what is stored.

**Impact:** The dossier mixes research answers with orphan constraints, so downstream teams cannot tell which requirements are actually resolved versus newly introduced, and planning will drift into unscoped UX and governance work.

---

### [CAUTION] assumption backing

**Finding:** The implementation path is treated as settled even though the artifact itself leaves the key mechanism unresolved. It recommends CLAUDE.md injection as the leading path while the open questions still admit uncertainty about file ownership, consent flow, and staleness policy.

**Quote from artifact:**
> The research evidence strongly favors Path A — CLAUDE.md injection with staleness signals and developer-visible/editable output — as the mechanism that can achieve the phenomenological goal within the stated constraints.

**Impact:** Engineering will start wiring a specific mechanism before the artifact has established the operational rules that make that mechanism safe and correct, creating rework and trust failures when consent/staleness details are finally specified.

---
