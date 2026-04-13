# Knowledge Consolidation Engine

Automated pipeline that promotes session-extracted facts into durable vault threads. Replaces manual `/memory-consolidate` for high-volume sessions.

## Architecture

```
Session logs
    └─ extract_sessions.py       # chunk logs into exchange windows
    └─ window_classifier.py      # LLM classifies each window as fact/noise
    └─ fact_store.py             # embed + store in FAISS (facts.db)
    └─ deep_consolidate.py       # GraphRAG: community reports → candidates
    └─ subagent_graphbuilder.py  # builds the community graph
    └─ autoresearch_loop.py      # parameter optimizer (targets F1 ≥ 0.70)
```

## Current Status

| Stage | Status |
|---|---|
| Session extraction | Working |
| Window classification | Working (F1: 0.25, target: 0.70) |
| Fact embedding + retrieval | Working |
| GraphRAG community builder | Bootstrap error — `community_reports.parquet` missing |
| Autoresearch optimizer | Blocked on graph bootstrap |

## Active Work

**Goal:** Push window classifier F1 from 0.25 → 0.70+

Parameter search space:
- `classifier_prompt`: v1, v2, v3
- `window_size`: 3, 5, 7
- `confidence_threshold`: 0.3 – 0.7

Prior architecture (GraphRAG loop) achieved F1: 0.074 on a contaminated eval set — abandoned.

## Auto-Promotion Logic (`deep_consolidate.py`)

A community is auto-promoted to a vault thread if:
- 6-signal score ≥ 0.65
- Sessions ≥ 2
- Week span ≥ 2
- No contradiction with existing threads

Otherwise it lands in `candidates/` for manual `/memory-review-candidates`.

## Known Issues

- `community_reports.parquet` bootstrap failure — graph builder PID exited before completing index
- 4 pending contradictions in `facts.db` need human resolution (`/recall --pending`)
