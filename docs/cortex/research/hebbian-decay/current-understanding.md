---
slug: hebbian-decay
brief_iteration: 1
last_updated: 2026-04-18
---

# Current Understanding — hebbian-decay

## Possible Terminals

| Terminal | Status | Ruled-Out Reason | Evidence |
|----------|--------|-----------------|----------|
| commit-to-build | live | — | — |
| kill-with-learning | live | — | — |
| experiment-required | live | — | — |
| decompose | ruled-out | Problem is well-scoped: single decay parameter, single code module | Pre-research |
| already-exists | ruled-out | No decay in current hebbian.py; graph is monotonic accumulator | Code inspection |
| hold-on-dependency | ruled-out | No external blockers; pure Python modification to hebbian.py | Pre-research |

## Iteration History

| Iteration | Brief | Dossier | Reframe Reason |
|-----------|-------|---------|----------------|
| 1 | docs/cortex/clarify/hebbian-decay/20260418T063000Z-clarify-brief.md | TBD | (initial) |
