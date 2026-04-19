# GSD Handoff: hebbian-decay

**Slug:** hebbian-decay
**Contract:** docs/cortex/contracts/hebbian-decay/contract-001.md
**Status:** pending approval

---

## Objective

Add multiplicative per-session decay (`w *= (1 - 0.03)`) to hebbian-written entity edges in `graph.db`, so the personal memory graph encodes current interest topology rather than historical accumulation. Non-firing edges fade; edges below `MIN_WEIGHT=0.01` are pruned.

---

## Deliverables

| Artifact | Path |
|----------|------|
| Updated Hebbian module | `~/memory/vault/scripts/hebbian.py` |
| Updated vault CLI | `~/memory/vault/bin/vault` |
| GitHub deploy | `calenwalshe/personal-memory` — scripts/hebbian.py + bin/vault |

---

## Requirements

None formalized. Derived from clarify brief `hebbian-decay` and research dossier `concept-20260418T073000Z.md`.

---

## Tasks

- [ ] Add `DECAY = 0.03` and `MIN_WEIGHT = 0.01` constants to `hebbian.py`
- [ ] Implement `apply_decay(atom_ids, fired_entity_ids, graph_db_path=None) -> dict` in `hebbian.py`
- [ ] Update `update_from_atoms()` return dict to include `fired_entity_ids: set[str]`
- [ ] Wire `apply_decay()` into `cmd_chunk()` in `bin/vault` after `update_from_atoms()`; add `hebbian_edges_decayed` and `hebbian_edges_pruned` to result dict
- [ ] Add stale persistence count to `vault graph stats` output
- [ ] Test: fired pair weight ≥ pre-chunk weight (no decay on firing pairs)
- [ ] Test: non-fired hebbian edge weight = `pre * (1 - 0.03)` after chunk
- [ ] Test: edge with weight < 0.01 post-decay is deleted
- [ ] Test: co-occurrence baseline edge weight unchanged
- [ ] Test: zero-atom chunk is a no-op
- [ ] Update `docs/MEMORY-ARCHITECTURE.md` with decay parameters
- [ ] Deploy to `calenwalshe/personal-memory` (push to main)

---

## Acceptance Criteria

- [ ] `vault chunk` output includes `hebbian_edges_decayed: N` and `hebbian_edges_pruned: N`
- [ ] Non-firing hebbian edge weight decreases by factor `(1 - 0.03)` per session
- [ ] Only edges with `description='hebbian'` are decayed — co-occurrence baseline unchanged
- [ ] No edge weight drops below `0.0`; edges below `MIN_WEIGHT=0.01` are deleted
- [ ] Decay is idempotent: running `vault chunk` with same atoms does not double-apply decay
- [ ] `vault graph stats` shows stale persistence count
- [ ] All existing behaviour preserved; WAL mode intact; no schema changes
