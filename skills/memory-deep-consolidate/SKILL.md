---
name: memory-deep-consolidate
description: "Reads GraphRAG community reports from ~/memory/vault/graph/output/, computes the 6-signal quality score for each, runs a Haiku contradiction check against existing vault threads, and writes candidates to ~/memory/vault/candidates/. Auto-promotes candidates passing all 3 gates (score ≥ 0.65, session_count ≥ 2, week_span ≥ 2) to ~/memory/vault/raw/threads/ and appends to INDEX.md. Trigger when the user says 'deep consolidate', 'mine the graph', 'promote graph communities', or uses /memory-deep-consolidate."
---

# Memory Deep Consolidate — GraphRAG Communities → Vault Candidates

Thin wrapper around `~/memory/vault/scripts/deep_consolidate.py`. Runs after `/memory-extract-sessions` has produced a GraphRAG index. This skill:

1. Loads `graph/output/community_reports.parquet`
2. Traces each community → entities → text_units → documents → `chunk_metadata.json` to compute `session_count` and `week_span` for that community
3. Computes the 6-signal weighted score (rank, size, session_count, week_span, entity specificity, relationship density)
4. For candidates passing all three auto-promote gates (score ≥ 0.65 AND session_count ≥ 2 AND week_span ≥ 2), runs a Haiku contradiction check vs existing `raw/threads/*.md`
5. Writes `cand-*.md` files to `~/memory/vault/candidates/` with frontmatter matching the contract-001 schema (`source: graphrag-corpus`, `score`, `session_count`, `week_span`)
6. Auto-promoted candidates are also copied to `~/memory/vault/raw/threads/` and appended to `INDEX.md`
7. Updates `~/memory/vault/impressions-index.json` under the `graphrag-corpus` source key

## User-invocable

When the user types `/memory-deep-consolidate`, run this skill. Also trigger on:
- "deep consolidate"
- "mine the graph"
- "promote graph communities"
- "run deep consolidation"

## Arguments

- `/memory-deep-consolidate` — full run
- `/memory-deep-consolidate --dry-run` — process reports, compute scores, skip all writes and Haiku calls
- `/memory-deep-consolidate --graph-dir <path>` — override graph workspace location

## Instructions

### Step 1: Verify prerequisites

```bash
ls ~/memory/vault/graph/output/community_reports.parquet 2>/dev/null
ls ~/memory/vault/graph/chunk_metadata.json 2>/dev/null
```

If either is missing, stop and tell the user to run `/memory-extract-sessions --index` first.

### Step 2: Run the consolidator

```bash
cd ~/memory/vault/scripts
/home/agent/claude-stack-env/bin/python3 deep_consolidate.py "$@"
```

### Step 3: Report

The script prints a JSON stats block — surface the key numbers:
- `reports_processed`
- `candidates_written`
- `auto_promoted`
- `for_review` (user needs to triage via `/memory-review-candidates`)
- `contradicted` (auto-promote blocked by existing contradictory thread)

If `for_review > 0`, suggest the user run `/memory-review-candidates`.

## Not in scope

- Creating or updating the GraphRAG index (use `/memory-extract-sessions`)
- Walking pending candidates for human review (use `/memory-review-candidates` — it already handles graphrag-corpus candidates without modification)
- Parameter optimization against eval_set.jsonl (use `autoresearch_loop.py`)
