# Critique: dossier — session-continuity

**Gate:** dossier
**Slug:** session-continuity
**Timestamp:** 2026-04-14T17:00:00Z
**Artifact:** docs/cortex/research/session-continuity/concept-20260414T170000Z.md
**Engine:** codex
**Overall Severity:** STOP

---

## Summary

This dossier is not decision-ready. Its main conclusion is internally inconsistent, its most important recommendation is unsupported by evidence, it relies on weak sources for normative claims, and it drifts beyond the stated research questions into orphan implementation assertions.

---

## Findings (4 total — STOP: 2, CAUTION: 2, GO: 0)

### [STOP] assumption backing

**Finding:** The dossier asserts a core architectural conclusion without evidence: it says no new platform capability is needed and the gap is purely what hooks write, then immediately makes the design depend on Haiku summarization in PreCompact and SessionEnd. That is a new runtime dependency and an unvalidated capability assumption, not "just file I/O."

**Quote from artifact:**
> "No new platform capability is needed. The gap is purely in what the hooks write." ... "No FAISS, no LLM at session start, no vault lookup — just file I/O." ... "Calls Haiku"

**Impact:** The central recommendation is internally contradictory. The proposed solution depends on an LLM call at hook time, with latency, reliability, exit-code, and lifecycle implications that were never established. The dossier's main implementation claim cannot be trusted.

---

### [STOP] evidence adequacy

**Finding:** The strongest prescriptive claim in the dossier is unsupported: it declares that the window classifier is insufficient and that a dedicated Haiku summarization pass is required, but provides no experiment, no output comparison, no failure examples, and no measurable criteria showing that the classifier cannot satisfy the continuity use case.

**Quote from artifact:**
> "Finding: No. A dedicated Haiku summarization pass is required."

**Impact:** This turns a preference into a fake finding. The implementation is being driven by an unevidenced conclusion, so the team could end up adding unnecessary LLM complexity and hook-time failure modes for a problem that may already be solvable with existing outputs.

---

### [CAUTION] source authority

**Finding:** The dossier leans on low-authority sources for major design guidance, including a GitHub issue and a third-party repository used as a normative source for the artifact schema. Those are not high-tier evidence for product behavior or minimum schema requirements.

**Quote from artifact:**
> Source: code.claude.com/docs/en/hooks, github.com/anthropics/claude-code/issues/34954
> Source: session-handoff framework patterns, github.com/duke-of-beans/CONTINUITY

**Impact:** The dossier overstates confidence in behavior and design choices not grounded in official docs or validated internal evidence. Weakens reliability of hook-behavior claims and the proposed snapshot/summary schema.

---

### [CAUTION] traceability

**Finding:** The dossier includes orphan material not cleanly tied to the six stated research questions — especially the "Adjacent Findings" section and implementation-level prescriptions in "Synthesis." It drifts from answering the clarify brief into solutioning and bug triage without maintaining question-level traceability.

**Quote from artifact:**
> ## Adjacent Findings (2 — passed filter pipeline) ... ## Synthesis: The Implementation

**Impact:** The artifact is no longer a disciplined answer set to q1-q6. Harder to audit whether the research actually resolved the brief versus accumulating extra claims that bypassed the stated question structure.

---
