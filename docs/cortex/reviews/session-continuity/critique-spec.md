# Critique: spec — session-continuity

**Gate:** spec
**Slug:** session-continuity
**Timestamp:** 2026-04-14T17:30:00Z
**Artifact:** docs/cortex/specs/session-continuity/spec.md
**Engine:** codex
**Overall Severity:** STOP

---

## Summary

This spec is not ready. Its acceptance criteria are not mechanically verifiable, its scope boundaries are contradictory around project-file ownership and ignore behavior, and its core latency risk mitigation is unresolved and internally inconsistent.

---

## Findings (4 total — STOP: 2, CAUTION: 2, GO: 0)

### [STOP] ac_testability

**Finding:** The acceptance criteria rely on subjective or undefined observations instead of deterministic checks. "visible in session-start context" and "verified by observing" are not testable protocols. The spec never defines how to inspect `additionalContext` or what exact output constitutes success.

**Quote from artifact:**
> At SessionStart, if `.cortex/session-memory.md` exists, its content is injected into `additionalContext` (visible in session-start context)
> `cortex-session-end.sh` fires on `SessionEnd`, not `Stop` — verified by observing that `current-state.md` no longer rewrites on every agent turn

**Impact:** Implementers cannot write reliable automated checks for core behavior, which guarantees inconsistent validation and approval disputes.

---

### [STOP] scope_coherence

**Finding:** The scope is internally contradictory about ownership and modification rights for project files. The spec declares `.gitignore` developer-owned while also requiring the system to append to it, and it claims the runtime artifact is "not committed" while only optionally requiring ignore handling via `.gitignore` or an undefined "equivalent project-level ignore."

**Quote from artifact:**
> `.gitignore` addition for `.cortex/session-memory.md`
> `New system-owned file: .cortex/session-memory.md (runtime artifact, not committed)`
> `.cortex/session-memory.md` is listed in `.gitignore` (or equivalent project-level ignore)`

**Impact:** Creates direct scope ambiguity about whether the system is allowed to mutate developer-owned repo files and what exact ignore mechanism is acceptable, inviting scope creep and implementation divergence.

---

### [CAUTION] risk_completeness

**Finding:** The primary latency mitigation is vague and self-contradictory. It says PreCompact should block until the snapshot is written, then proposes making the write async with a "post-compact fallback," which defeats the stated reason for blocking. "Consider making" is not a mitigation plan.

**Quote from artifact:**
> PreCompact can block (exit code 2) until the snapshot is written, so compaction doesn't wipe context before the snapshot is captured.
> `Mitigation: write raw last-5-turns fallback if Haiku call exceeds 10s timeout; never block indefinitely; consider making snapshot write async with a post-compact fallback`

**Impact:** The most critical failure mode has no settled mitigation, so implementers can choose incompatible behaviors that either reintroduce context loss or stall compaction unpredictably.

---

### [CAUTION] ac_testability

**Finding:** Several criteria specify content shape but not exact formatting or merge behavior. The spec never defines whether the snapshot and summary replace each other, append as separate sections, or how section boundaries are identified for verification.

**Quote from artifact:**
> After PreCompact fires, `.cortex/session-memory.md` contains a snapshot section ...
> After SessionEnd fires ... `.cortex/session-memory.md` contains a summary section ...

**Impact:** Tests cannot unambiguously assert correct file state after multiple events, and competing implementations will produce incompatible file layouts.

---
