# Critique: clarify — session-continuity

**Gate:** clarify
**Slug:** session-continuity
**Timestamp:** 2026-04-14T16:00:00Z
**Artifact:** docs/cortex/clarify/session-continuity/20260414T160000Z-clarify-brief.md
**Engine:** codex
**Overall Severity:** STOP

---

## Summary

The brief is structurally unsound because it commits to behavior for `/clear` that it simultaneously treats as unresolved, and it assumes critical platform capabilities before verifying they exist. It also frames the problem around a preselected `MEMORY.md` solution, which biases research and leaves the actual acceptance criteria too vague to implement safely.

---

## Findings (4 total — STOP: 2, CAUTION: 2, GO: 0)

### [STOP] consistency

**Finding:** The brief hard-commits to preserving continuity after `/clear`, then later admits `/clear` may intentionally mean the opposite. That is a direct contradiction in core product behavior, so an implementation team could build the exact behavior the owner does not want.

**Quote from artifact:**
> A developer who exits a Claude Code session — whether through /clear, context compaction, or normal exit — finds a MEMORY.md in the project root at their next session start that tells them exactly where they were and what mattered, without having to ask for it.
>
> - Compaction and /clear are different events with different intent: compaction = context management mid-session (keep working), /clear = deliberate reset (may or may not want continuity)
>
> - q6 (factual): Should the system behave differently on explicit /clear vs compaction vs normal session exit — does the intent behind each event change what gets written?

**Impact:** Downstream work will encode the wrong reset semantics. The team can easily ship a system that restores context after a deliberate reset, violating user intent and forcing rework at the behavior-definition level.

---

### [STOP] verifiability

**Finding:** The entire solution path depends on two unverified platform behaviors, yet the brief states the product as if those behaviors already exist: a pre-wipe hook and automatic `MEMORY.md` ingestion. This is not a testable plan; it is a speculative implementation disguised as a requirement.

**Quote from artifact:**
> - Claude Code exposes a hook that fires before or at compaction (PreCompact or equivalent) — needs verification
> - MEMORY.md at the project root will be read by Claude at session start because it is in the same directory as CLAUDE.md — needs verification against Claude Code's context loading behavior

**Impact:** If either hook availability or `MEMORY.md` auto-read is false, the proposed architecture collapses and the MVP is impossible in its current form. Engineering will waste cycles refining behavior around a mechanism that may not exist.

---

### [CAUTION] framing attack

**Finding:** The brief locks in `MEMORY.md` injection and classifier-only summarization before the research is done, which forecloses better options such as explicit resume artifacts, hook-driven handoff files, or a dedicated summary pass. It is framed as validating a chosen solution rather than solving the continuity problem.

**Quote from artifact:**
> parent context: dev-memory-feel dossier establishes that session-start is the highest-ROI injection moment and CLAUDE.md-adjacent MEMORY.md is the right surface.
>
> - Must work with the existing window classifier output — no new extraction infrastructure required for MVP

**Impact:** Research becomes biased toward proving the preselected architecture instead of finding the least risky implementation.

---

### [CAUTION] unambiguity

**Finding:** The core success condition is not specifiable because "exactly where they were" and "what mattered" are undefined, while q4 leaves the minimum content schema unresolved. Multiple incompatible implementations would satisfy the brief on paper and still be rejected by the owner.

**Quote from artifact:**
> finds a MEMORY.md in the project root at their next session start that tells them exactly where they were and what mattered

**Impact:** Teams cannot derive a stable artifact schema, acceptance test, or evaluation rubric. Implementation will drift into subjective summary-writing.

---
