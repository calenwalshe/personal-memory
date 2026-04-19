---
slug: session-continuity
brief_iteration: 1
last_updated: 2026-04-14
---

# Current Understanding: session-continuity

---

## Possible Terminals

| Terminal | Status | Ruled-Out Reason | Evidence |
|---|---|---|---|
| commit-to-build | live | | |
| kill-with-learning | live | | |
| decompose | live | | |
| experiment-required | live | | |
| already-exists | live | | |
| hold-on-dependency | live | | |

---

## Durable Findings

*(Inherited from dev-memory-feel dossier)*

- Session start after a break is the highest-ROI injection moment — peak interruption-recovery event (23-45 min recovery time). Source: docs/cortex/research/dev-memory-feel/concept-20260414T150000Z.md
- MEMORY.md (system-owned, CLAUDE.md-adjacent) is the right surface — readable/editable by developer, not mixed with developer-authored instructions. Source: ibid.
- Two distinct artifacts needed: continuity snapshot (pre-compaction, working memory) vs session summary (session-exit, episodic). Same surface, different content and triggers.
- Confident wrong memory is the worst failure mode — staleness signals required. Source: docs/cortex/research/memory-phenomenology/concept-20260414T130000Z.md

---

## Provisional Thoughts

- **[PROVISIONAL]** Claude Code likely has a PreCompact hook and a Stop hook — needs codebase verification before spec can proceed.
- **[PROVISIONAL]** The window classifier's 120 episodic facts are likely sufficient raw material for session summaries without a dedicated summarization pass.

---

## Open Questions

- Does Claude Code expose a PreCompact hook that fires before context is wiped? — *Revisit when:* q1 codebase audit completed
- Is MEMORY.md at project root auto-read by Claude Code at session start? — *Revisit when:* q2 codebase audit completed
- What is the right content schema for each artifact type? — *Revisit when:* q4 factual research completed
- Does /clear intent differ from compaction intent, and should the system respect that? — *Revisit when:* q6 factual research completed

---

## Iteration History

| Iteration | Brief | Dossier | Reframe Reason |
|---|---|---|---|
| 1 | docs/cortex/clarify/session-continuity/20260414T160000Z-clarify-brief.md | TBD | (initial — derived from dev-memory-feel concept research) |
