# Contract: hebbian-decay-001

**ID:** hebbian-decay-001
**Slug:** hebbian-decay
**Phase:** execute
**Status:** complete

---

## Objective

Add `apply_decay()` to `hebbian.py` and wire it into `vault chunk` so that hebbian-written entity edges lose `3%` of their weight each session they do not fire — preventing the graph from becoming a monotonic accumulator.

---

## Deliverables

- `~/memory/vault/scripts/hebbian.py` — `apply_decay()` function, `DECAY` and `MIN_WEIGHT` constants, updated `update_from_atoms()` return dict
- `~/memory/vault/bin/vault` — `cmd_chunk()` wired to call `apply_decay()`, result dict extended, `cmd_graph stats` shows stale persistence
- `calenwalshe/personal-memory` GitHub — `scripts/hebbian.py` + `bin/vault` pushed to main

---

## Scope

### In Scope
- `apply_decay()` in `hebbian.py`
- `DECAY = 0.03`, `MIN_WEIGHT = 0.01` constants
- Integration into `cmd_chunk()` in `bin/vault`
- Stale persistence metric in `vault graph stats`
- Tests for all 5 decay behaviours
- GitHub deploy

### Out of Scope
- Competitive depression (exp-004)
- Multi-timescale decay
- Decay of co-occurrence baseline or typed semantic edges
- Schema changes to `relations` table

---

## Write Roots

- `~/memory/vault/scripts/hebbian.py`
- `~/memory/vault/bin/vault`
- `~/memory/vault/graph.db` (via symlink) — UPDATE + DELETE only

---

## Done Criteria

- [x] `vault chunk` output includes `hebbian_edges_decayed: N` and `hebbian_edges_pruned: N`
- [x] Non-firing hebbian edge weight = `pre_weight * (1 - 0.03)` — verified before/after query
- [x] Only `description='hebbian'` edges decayed — co-occurrence baseline weights unchanged
- [x] No weight below `0.0`; edges below `0.01` are deleted
- [x] Decay is idempotent — same-batch re-run does not double-apply
- [x] `vault graph stats` shows stale persistence count (unfired hebbian edges in last 30d)
- [x] All existing vault chunk behaviour preserved; WAL mode intact; no schema changes

---

## Validators

```bash
# Criterion 1 — chunk output has decay keys
vault chunk personal-memory --text 2>&1 | grep hebbian_edges_decayed

# Criterion 2 — non-firing edge decays correctly (manual spot-check)
python3 -c "
import sqlite3
conn = sqlite3.connect('/home/agent/memory/vault/graph.db')
rows = conn.execute(\"SELECT id, weight FROM relations WHERE description='hebbian' ORDER BY weight DESC LIMIT 5\").fetchall()
print('Hebbian edges:', rows)
conn.close()
"
# Run vault chunk, then re-run above and verify weights decreased by ~3%

# Criterion 6 — stale persistence in stats
vault graph stats --text 2>&1 | grep -i stale

# Criterion 7 — WAL mode intact
python3 -c "
import sqlite3
conn = sqlite3.connect('/home/agent/memory/vault/graph.db')
print(conn.execute('PRAGMA journal_mode').fetchone())
conn.close()
"
```

---

## Eval Plan

`docs/cortex/evals/hebbian-decay/eval-plan.md` (pending — create with `/cortex-research --phase evals`)

---

## Repair Budget

- `max_repair_contracts: 3`
- `cooldown_between_repairs: 1`

### Failed Approaches
*(none — initial contract)*

### Why Previous Approach Failed
N/A — initial contract

---

## Approvals

- [x] Spec approved (human)
- [x] Contract approved (human)

---

## Rollback Hints

- Restore `hebbian.py`: `git checkout HEAD -- scripts/hebbian.py` in personal-memory repo
- Restore `bin/vault`: revert the `apply_decay` call block in `cmd_chunk()` and the stats addition in `cmd_graph()`
- No graph.db schema changes to undo — only weight values modified; run `vault lab rollback` to restore prior snapshot if weights are corrupted
