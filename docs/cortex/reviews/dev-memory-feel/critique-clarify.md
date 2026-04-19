# Critique: clarify — dev-memory-feel

**Gate:** clarify
**Slug:** dev-memory-feel
**Timestamp:** 2026-04-14T14:00:00Z
**Artifact:** docs/cortex/clarify/dev-memory-feel/20260414T140000Z-clarify-brief.md
**Engine:** codex
**Overall Severity:** STOP

---

## Summary

This brief is structurally unsound: it defines success in terms of a proactive felt-memory experience that its own constraints say cannot happen, and it never makes "feeling known" measurable. It also biases its own research by pre-answering the taxonomy and prematurely locking the solution space to injection formats.

---

## Findings (4 total — STOP: 2, CAUTION: 2, GO: 0)

### [STOP] consistency

**Finding:** The brief hard-contradicts itself on the core delivery mechanism. It says the felt experience depends on proactive memory surfacing, but also states the current system cannot surface anything unless the user explicitly invokes `/recall`. That means the mechanism the brief claims is necessary is ruled out by its own constraints, so the research target cannot be achieved under the stated operating conditions.

**Quote from artifact:**
> The current entry point is entirely opt-in (`/recall`) — felt experience cannot currently happen without user initiation
>
> Proactive surfacing (assistant mentions memory without being asked) is the mechanism that produces felt continuity — passive retrieval via `/recall` produces information access, not felt recognition

**Impact:** Downstream work will either define an MVP that is impossible to implement in the current system, or quietly violate the stated constraints. That guarantees wasted research cycles and a spec the owner will reject once implementation reality is checked.

---

### [STOP] verifiability

**Finding:** The brief claims it will produce a "concrete, testable definition" of "feeling known," but never defines a falsifiable evaluation method, decision rule, or observer. "Felt," "impactful," and "minimum viable memory moment" are all subjective labels with no operational measurement, so the team cannot tell whether the goal has been met or not.

**Quote from artifact:**
> Produce a concrete, testable definition of what "feeling known by Claude Code" looks like for a developer — expressed as 3-5 specific workflow moments where memory would be felt, not just retrieved
>
> What are the 3-5 concrete moments in a developer's workflow where memory would feel most impactful — and what does "impactful" mean in each case?

**Impact:** Different researchers can produce incompatible outputs that all appear compliant. Execution will drift into opinionated examples instead of a usable spec, and evaluation will collapse into taste-based arguments rather than evidence.

---

### [CAUTION] consistency

**Finding:** The brief presents the taxonomy and priority ordering as an open research question, then pre-answers it in the research plan. That contaminates the inquiry and makes the supposed discovery exercise circular.

**Quote from artifact:**
> q3 (factual): What is the taxonomy of developer-specific memory categories and their relative importance to felt experience?
>
> 4. (q3/factual) Define a taxonomy of developer-specific memory categories ordered by their impact on felt experience — project context > architectural decisions > debugging patterns > code style preferences > tool preferences.

**Impact:** Research will be biased toward confirming a predetermined ordering instead of testing alternatives. Any resulting spec will look evidence-based while actually encoding an assumption the owner may reject.

---

### [CAUTION] framing attack

**Finding:** The brief narrows the solution space too early to session-start injection formats and proactive recall behavior, even though the actual problem statement is about phenomenology. That frames the work around a preferred implementation pattern before establishing whether that pattern is the right lever at all.

**Quote from artifact:**
> What is the right format for session-start memory injection — CLAUDE.md-style static file, runtime prompt prefix, or explicit /recall surface?
>
> Proactive surfacing (assistant mentions memory without being asked) is the mechanism that produces felt continuity

**Impact:** Better alternatives such as task-triggered retrieval, workflow-state reconstruction, or artifact-grounded context carryover can be missed entirely. The project risks optimizing an interface pattern instead of the actual experience problem.

---
