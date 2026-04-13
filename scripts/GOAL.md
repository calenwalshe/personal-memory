# Autoresearch Goal

## Metric

**Primary**: F1 score on `probe_set.jsonl`
- Target: F1 >= 0.7

## Current Best

| Prompt | Window | Threshold | Precision | Recall | F1 | Date |
|--------|--------|-----------|-----------|--------|----|------|
| v1 | 3 | 0.4 | 0.5 | 0.1667 | **0.25** | 2026-04-12 |

## Parameter Search Space

```
classifier_prompt: ['v1', 'v2', 'v3']
window_size: [3, 5, 7]
confidence_threshold: [0.3, 0.4, 0.5, 0.6, 0.7]
```

## Iteration Log

| Run | Prompt | Window | Threshold | Precision | Recall | F1 | Date |
|-----|--------|--------|-----------|-----------|--------|----|------|
| 1 | v1 | 3 | 0.3 | 0.0 | 0.0 | **0.0** | 2026-04-12 22:30 |
| 2 | v1 | 3 | 0.4 | 0.5 | 0.1667 | **0.25** | 2026-04-12 22:33 |
| 3 | v1 | 3 | 0.5 | 0.5 | 0.1667 | **0.25** | 2026-04-12 22:35 |

---
*Replaces contract-001 GraphRAG loop (prior best F1: 0.074 on contaminated eval set -- architecture abandoned).*
