# Spec: hebbian-decay

**Slug:** hebbian-decay
**Timestamp:** 20260418T080000Z
**Status:** draft

---

## 1. Problem

The personal memory graph's Hebbian plasticity implementation only increments edge weights — there is no mechanism to reduce them. This makes `graph.db` a monotonic accumulator: every entity pair ever co-mentioned eventually saturates toward `MAX_WEIGHT`, regardless of whether the association is still active. A user who worked intensively on topic A six months ago and has since moved on will still see A-associated edges dominating the graph, drowning out current interests. The graph is supposed to encode *current* interest topology; without decay it encodes *historical* co-occurrence frequency instead.

---

## 2. Acceptance Criteria

- [ ] `vault chunk <project>` output includes `hebbian_edges_decayed: N` showing how many edges were decremented in the session
- [ ] After one session where entity pair (A, B) does NOT co-appear, their `related_to` edge weight decreases by factor `(1 - 0.03)` — verified by querying `graph.db` before and after
- [ ] Edges with `description='hebbian'` are subject to decay; edges without that description are NOT modified
- [ ] No edge weight drops below `0.0` after decay
- [ ] Edges whose weight drops below `MIN_WEIGHT=0.01` after decay are deleted from `graph.db` (pruned, not left as floating-point noise)
- [ ] Running `vault chunk` twice in sequence (same atoms batch, idempotent path) does not double-apply decay — decay runs once per `update_from_atoms()` call
- [ ] The stale persistence metric is queryable: `vault graph stats` shows "Stale edges (unfired 30d): N" so the health of decay can be monitored
- [ ] All existing tests pass; `graph.db` WAL mode is preserved; no schema changes to `relations` table

---

## 3. Scope

### In Scope

- Add `apply_decay(atom_ids, graph_db_path)` function to `~/memory/vault/scripts/hebbian.py`
- Integrate decay call into `cmd_chunk()` in `~/memory/vault/bin/vault`, running after the Hebbian increment (`update_from_atoms`)
- Decay parameter: `DECAY = 0.03`, `MIN_WEIGHT = 0.01` (prune threshold), constants in `hebbian.py`
- Decay scope: only `relations` rows where `relation_type='related_to'` AND `description='hebbian'`
- Pruning: delete rows where `weight < MIN_WEIGHT` after decrement
- Stale persistence metric: count of hebbian edges unfired in last 30 days, surfaced in `vault graph stats`
- Update `bin/vault` output dict to include `hebbian_edges_decayed` and `hebbian_edges_pruned`
- Update `personal-memory` GitHub repo (`calenwalshe/personal-memory`) with the promoted changes

### Out of Scope

- Competitive depression / LTD-style active penalty (`w -= λ` when A fires without B) — separate experiment (exp-004)
- Multi-timescale decay (fast + slow DECAY constants)
- BCM sliding threshold implementation
- Decay of co-occurrence baseline edges (`description != 'hebbian'`)
- Decay of typed semantic relations (`uses`, `depends_on`, `part_of`, etc.)
- Decay of `analogous_to` edges written by `bridge_detector.py`
- Schema changes to `relations` table (no new columns)
- Oja's rule / lateral inhibition

---

## 4. Architecture Decision

**Chosen approach:** Multiplicative soft-bound decay applied per-session to non-firing hebbian edges.

```python
DECAY = 0.03        # per-session decay constant (half-life ~23 days at 1 session/day)
MIN_WEIGHT = 0.01   # prune threshold — edges below this are deleted

def apply_decay(atom_ids: list[str], graph_db_path: Path = None) -> dict:
    """
    For all hebbian-written related_to edges NOT fired in atom_ids batch:
      w = max(0.0, w * (1 - DECAY))
    Delete edges where w < MIN_WEIGHT.
    Returns {edges_decayed, edges_pruned}.
    """
```

The decay is applied *after* the Hebbian increment in `cmd_chunk`. Fired pairs receive their `+ETA` increment first; non-fired pairs then receive the `*(1-DECAY)` decrement. This order ensures a pair that fires in the current session is never penalized.

**Rationale:** Multiplicative (soft-bound) decay is the correct biological analog (LTD with weight-dependent magnitude per Scholarpedia/Sjöström 2010). It naturally bounds weights away from 0 and produces a unimodal distribution. The steady-state formula `w* = p·η / (d·(1-p))` gives a principled calibration target: with d=0.03, pairs firing 20% of sessions stabilize at ~2.5, pairs firing 50% at ~10.0. No schema changes required — `description='hebbian'` is the existing discriminator.

### Alternatives Considered

- **Constant (additive) decay** `w -= DECAY`: can drive weights negative; requires explicit clamp at 0; produces asymmetric dynamics (increment by 0.3, decrement by 0.03 → net positive even for rarely-firing pairs). Rejected.
- **Competitive depression** `w -= λ * 1[A fires, B doesn't]`: more biologically correct but requires tracking per-entity activity (already available), adds second parameter λ, and should be validated as a separate experiment after passive decay is proven. Deferred to exp-004.
- **Multi-timescale decay** (fast + slow): better fits power-law interest dynamics but doubles the parameter space. Revisit after 60-day production run with stale persistence metrics.
- **Timestamp-based validity** (Zep/Graphiti approach): binary valid/invalid edges rather than continuous weight — loses the gradient entirely. Rejected.

---

## 5. Interfaces

- **`~/memory/vault/scripts/hebbian.py`** — owned by this spec; adds `apply_decay()`, `DECAY`, `MIN_WEIGHT` constants; modifies `update_from_atoms()` return dict to include fired pair set for exclusion
- **`~/memory/vault/bin/vault` `cmd_chunk()`** — reads from `hebbian.apply_decay()`; adds `hebbian_edges_decayed` and `hebbian_edges_pruned` to result dict; calls `apply_decay` after `update_from_atoms`
- **`~/memory/vault/bin/vault` `cmd_graph()` `stats` action** — reads `relations` table to compute stale persistence metric (unfired in 30d); adds to stats output
- **`graph.db` `relations` table** — writes: `UPDATE weight`, `DELETE` rows below MIN_WEIGHT; reads: `WHERE relation_type='related_to' AND description='hebbian'`; no schema changes
- **`calenwalshe/personal-memory` GitHub repo** — deploy target; update `scripts/hebbian.py` and `bin/vault`

---

## 6. Dependencies

- `sqlite3` (stdlib) — graph.db reads/writes
- `hebbian.py` existing `update_from_atoms()` — decay must integrate cleanly; the fired pair set computed during increment is reused to determine which edges to decay
- `graph.db` symlink → active snapshot — must resolve correctly; decay operates on the same db as increment
- No new external dependencies

---

## 7. Risks

- **Decay applied to wrong edges** (co-occurrence baseline eroded) — Mitigation: strict `WHERE description='hebbian'` filter in both UPDATE and DELETE queries; add assertion in test that co-occurrence edge weights are unchanged after decay run
- **Fired pairs not correctly excluded** (pair that fired gets decremented too) — Mitigation: compute `fired_ids` set from `update_from_atoms()` return value; pass to `apply_decay()` as exclusion list; test with a pair that fires and verify weight only goes up
- **Cascading prune deletes bridge edges** — Mitigation: `analogous_to` edges have `relation_type != 'related_to'`; the WHERE clause excludes them; add test to confirm
- **Full-edge-scan performance at scale** — Mitigation: at 2548 current edges, a full scan takes <5ms; add a timing log in debug mode; revisit if edge count exceeds 10k
- **Double-decay on re-run** (idempotency) — Mitigation: decay is gated on `atom_ids` being non-empty; if chunk produces no atoms, neither increment nor decay runs; tested by verifying zero-atom chunk leaves weights unchanged

---

## 8. Sequencing

1. **Add `apply_decay()` to `hebbian.py`** — implement the function, add `DECAY` and `MIN_WEIGHT` constants, update module docstring with new parameters. Verifiable: `python3 -c "from hebbian import apply_decay; print(apply_decay([]))"` returns `{edges_decayed: 0, edges_pruned: 0}`.

2. **Refactor `update_from_atoms()` to return fired pair set** — currently returns `{pairs_found, edges_updated, edges_created}`; extend to also return `fired_entity_ids` (set of entity IDs that appeared in this batch) so `apply_decay()` can exclude them. Verifiable: return dict includes `fired_entity_ids`.

3. **Integrate decay into `cmd_chunk()`** — call `apply_decay(atom_ids, fired_entity_ids=fired_set)` after `update_from_atoms()`; add `hebbian_edges_decayed` and `hebbian_edges_pruned` to result dict. Verifiable: `vault chunk <project>` output includes both keys.

4. **Add stale persistence to `vault graph stats`** — query `relations` for hebbian edges with `last_seen < 30 days ago`; surface count in stats output. Verifiable: `vault graph stats` shows "Stale edges (unfired 30d): N".

5. **Write and run tests** — verify: (a) fired pair weight increases only, (b) non-fired pair weight decreases by factor `(1-0.03)`, (c) edge pruned when weight < 0.01, (d) co-occurrence edge weights unchanged, (e) zero-atom chunk is a no-op. Verifiable: all 5 assertions pass.

6. **Deploy to GitHub** — update `scripts/hebbian.py` and `bin/vault` in `calenwalshe/personal-memory`. Verifiable: commit appears on `main` with correct diff.

---

## 9. Tasks

- [ ] Add `DECAY = 0.03` and `MIN_WEIGHT = 0.01` constants to `hebbian.py`
- [ ] Implement `apply_decay(atom_ids, fired_entity_ids, graph_db_path=None) -> dict` in `hebbian.py`
- [ ] Update `update_from_atoms()` return dict to include `fired_entity_ids: set[str]`
- [ ] Add `hebbian_edges_decayed` and `hebbian_edges_pruned` keys to `cmd_chunk()` result dict in `bin/vault`
- [ ] Wire `apply_decay()` call into `cmd_chunk()` after `update_from_atoms()`, passing `fired_entity_ids`
- [ ] Add stale persistence count to `vault graph stats` output
- [ ] Test: fired pair weight ≥ pre-chunk weight after chunk (increment only, no decay)
- [ ] Test: non-fired hebbian edge weight = `pre_weight * (1 - 0.03)` after chunk
- [ ] Test: edge with weight < 0.01 post-decay is deleted from `graph.db`
- [ ] Test: co-occurrence baseline edge (description != 'hebbian') weight unchanged after chunk
- [ ] Test: chunk with zero atoms produced leaves all weights unchanged
- [ ] Update `docs/MEMORY-ARCHITECTURE.md` to document decay parameters and stale persistence metric
- [ ] Deploy: copy updated `hebbian.py` and `bin/vault` to `/tmp/personal-memory-deploy`, commit, push to `calenwalshe/personal-memory`
