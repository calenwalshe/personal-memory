# Spec: knowledge-consolidation-engine

**Slug:** knowledge-consolidation-engine
**Timestamp:** 20260419T033000Z
**Status:** draft

---

## 1. Problem

The SCAPE memory vault captures raw conversation data (L0) and chunks it into atomic memories (L1), but it is structurally limited to chat sessions as input and has no belief-tracking capability. The system can answer "what happened?" (L0/L1) and "what's connected?" (L2 entity graph), but cannot answer "what do I currently believe about X?", "what changed since I last looked at this?", "what's contested?", or "what did I plan but never execute?" Additionally, non-chat knowledge — notes, documents, research dossiers, Cortex briefs, structured data — cannot enter the vault at all. This makes the vault a chat-memory system rather than a knowledge consolidation engine. The user needs a system where any source of evidence feeds into a unified belief engine that tracks truth over time, detects contradictions, and produces derived knowledge.

---

## 2. Acceptance Criteria

- [ ] `vault ingest <file>` accepts a markdown/text document, creates a source record in sources.db with source_segments, and produces L1 evidence units in atoms.db
- [ ] `vault ingest --type note "free text"` accepts stdin/string input and produces evidence units
- [ ] Non-chat atoms have `source_type != 'chat'` and `unit_type` set to a candidate type (claim_candidate, decision_candidate, etc.)
- [ ] Existing 415 atoms are backfilled with `source_type='chat'` and mapped `unit_type` values without data loss
- [ ] `beliefs.db` exists with tables: logical_forms, worlds, form_status, derived_objects, inference_log, l3_state
- [ ] `vault l3 extract` processes L1 evidence units into logical_forms in beliefs.db
- [ ] `vault l3 extract` correctly assigns worlds (current/past/planned/possible/contested) and form_status
- [ ] Conflict detection rule fires when two current claims on the same subject+predicate have different objects
- [ ] Supersession rule fires when a new claim on the same subject moves the old claim to world=past
- [ ] Stable promotion rule fires when a claim appears in 3+ source units with no contradiction
- [ ] Lesson extraction rule fires when a failure evidence unit and a decision claim share entities
- [ ] `vault beliefs [query]` returns current beliefs with world and status, optionally filtered by semantic search
- [ ] `vault conflicts` lists all derived_objects of type=contradiction
- [ ] `vault derived` lists derived objects (stable_belief, lesson, contradiction, open_thread) filterable by namespace and type
- [ ] All inference rule firings are recorded in inference_log with input forms, output id, action, and explanation
- [ ] facts.db 1258 facts are migrated to logical_forms in beliefs.db (semantic→claim, procedural→rule, episodic→observation-linked) OR explicitly frozen with a migration report
- [ ] PersonalMemoryModule is registered as the default L3 module with namespace "personal"
- [ ] All new databases use WAL mode
- [ ] Existing vault commands (`vault chunk`, `vault atoms`, `vault recall`, `vault context`, `vault graph`) continue to work unchanged
- [ ] 10+ pytest tests cover: source creation, evidence extraction, L3 form extraction, each inference rule, world assignment, backfill migration

---

## 3. Scope

### In Scope

- New `sources.db` with `sources` and `source_segments` tables
- Intake adapters: `intake_doc.py` (markdown/text files), `intake_notes.py` (freeform text)
- L1 atom schema extensions: `source_id`, `source_type`, `unit_type`, `observed_labels` columns
- Backfill script for existing 415 atoms
- `evidence_extractor.py` for non-chat source → L1 evidence units (via Haiku)
- New `beliefs.db` with full L3 schema (logical_forms, worlds, form_status, derived_objects, inference_log, l3_state)
- `l3_engine.py` — L3 runtime: form extraction, world assignment, inference rule execution
- `l3_module.py` — module protocol definition
- `personal_memory_module.py` — first L3 module (4 inference rules: conflict, supersede, stable promotion, lesson)
- facts.db migration or freeze
- CLI commands: `vault ingest`, `vault l3 extract`, `vault beliefs`, `vault conflicts`, `vault derived`
- `L2Organizer` protocol definition (no new organizers — just formalize the interface)
- Tests for all new functionality

### Out of Scope

- Intake adapters for PDF, CSV, JSON, code logs, Cortex briefs (future adapters)
- CortexModule and ResearchModule (future L3 modules)
- Additional L2 organizers (TopicMap, Timeline, ResearchDetector)
- L2 run tracking / organizer output tables
- Separate evidence_units table (extend atoms only)
- Full label provenance table (JSON observed_labels sufficient)
- Datalog/Soufflé formal logic engine
- OWL type system
- Event Calculus temporal reasoning
- FAISS index for beliefs.db (semantic search via atoms.db FAISS is sufficient for v1)
- Modifications to existing hooks (PostToolUse, SessionEnd, PreCompact)
- Automatic L3 extraction triggered by chunker (on-demand `vault l3 extract` only for v1)

---

## 4. Architecture Decision

**Chosen approach:** Three new stores (sources.db, extended atoms.db, beliefs.db) with a modular L3 runtime engine on top. L3 modules are namespaced and enable/disable per project, not hot-swapped. Inference rules are pure Python functions, not a formal logic engine.

**Rationale:** This preserves all existing infrastructure (events.db, atoms.db, graph.db) while adding universal intake and belief tracking. Separate databases maintain layer immutability — L0 capture never writes to L1 storage, L1 evidence never writes to L3 beliefs. Pure Python rules are sufficient at the current scale (~500 atoms, ~200 entities) and can be replaced with Datalog/Soufflé when the scale demands it.

### Alternatives Considered

- **Extend facts.db into L3:** Rejected because facts.db has a fundamentally different conceptual model (flat facts without worlds, status transitions, or inference). Adding worlds/status to the existing facts schema would require a breaking migration and create a Frankenstein schema mixing two design eras.
- **Single unified database:** Rejected because layer separation is a core architectural invariant. L0 is live telemetry, L1 is evidence, L2 is organization, L3 is belief. Separate databases allow independent rebuild of any layer without affecting others.
- **Datalog/Soufflé for inference:** Rejected for v1 because the scale (~500 evidence units, ~200 entities, 4 rules) doesn't justify the dependency. A Soufflé compilation step adds operational complexity. Pure Python functions are readable, testable, and sufficient. Can swap in later.
- **Hot-swap L3 modules:** Rejected because different modules could produce incompatible realities. Namespaced enable/disable (personal:*, cortex:*, research:*) allows multiple modules to coexist, each producing derived objects in their own namespace.
- **Replace atoms with evidence_units table:** Rejected for v1 because it adds migration complexity with no new capability. Extending atoms with nullable columns gets 95% of the value. Can introduce a renamed table in v2 if naming causes confusion.

---

## 5. Interfaces

### Reads

- `events.db` — L0 turns and sessions (read by intake_chat.py for source record creation)
- `atoms.db` — L1 atoms (read by l3_engine.py for form extraction; schema extended in-place)
- `graph.db` — L2 entities (read by l3_engine.py for entity linking on logical forms)
- `facts.db` — existing 1258 facts (read once during migration, then frozen)

### Writes

- `~/memory/vault/sources.db` — new file, created by source_store.py
- `~/memory/vault/atoms.db` — ALTER TABLE for new columns; backfill existing rows
- `~/memory/vault/beliefs.db` — new file, created by belief_store.py
- `~/memory/vault/scripts/source_store.py` — CRUD for sources.db
- `~/memory/vault/scripts/intake_doc.py` — document intake adapter
- `~/memory/vault/scripts/intake_notes.py` — notes intake adapter
- `~/memory/vault/scripts/evidence_extractor.py` — non-chat source → L1 evidence units
- `~/memory/vault/scripts/belief_store.py` — CRUD for beliefs.db
- `~/memory/vault/scripts/l3_engine.py` — L3 runtime (extraction, world assignment, inference)
- `~/memory/vault/scripts/l3_module.py` — module protocol + PersonalMemoryModule
- `~/memory/vault/scripts/migrate_facts.py` — facts.db → beliefs.db migration
- `~/memory/vault/scripts/l2_organizer.py` — L2Organizer protocol definition
- `~/memory/vault/scripts/test_knowledge_engine.py` — tests
- `~/memory/vault/bin/vault` — new CLI commands (vault ingest, vault l3, vault beliefs, vault conflicts, vault derived)

---

## 6. Dependencies

- `sqlite3` (stdlib) — all database operations, WAL mode
- `sentence-transformers` (installed) — embeddings for logical form similarity in conflict detection
- `faiss` (installed) — reuse existing atoms.db FAISS index for semantic search
- `claude -p` via subscription — Haiku for evidence extraction and logical form extraction
- `json` (stdlib) — observed_labels, source_form_ids, entity_ids stored as JSON arrays
- `uuid` (stdlib) — primary keys for all new records
- `pytest` (installed) — testing

---

## 7. Risks

- **atoms.db ALTER TABLE corruption** — Mitigation: backup atoms.db before migration; ALTER TABLE ADD COLUMN is safe in SQLite (documented atomic operation); validate row count before and after.
- **facts.db migration data loss** — Mitigation: migration script creates a full backup first; migration is idempotent (can re-run safely); write a migration report with counts.
- **Haiku extraction quality for logical forms** — Mitigation: validate extraction prompt against 20 sample atoms before running full extraction; include confidence scores; forms with confidence < 0.5 get world=possible instead of current.
- **Inference rule false positives** — Mitigation: all rule firings logged to inference_log with full provenance; derived objects can be invalidated; start with high-confidence rules only (conflict and supersession are deterministic).
- **Performance at scale** — Mitigation: current scale is small (~500 atoms, ~200 entities). All queries are indexed. Beliefs.db uses WAL mode. If scale becomes an issue, add FAISS index for beliefs.db in v2.
- **L3 extraction latency** — Mitigation: L3 extraction is on-demand (`vault l3 extract`), not inline with chunker. No impact on existing pipeline latency.

---

## 8. Sequencing

1. **sources.db + source_store.py** — Create new database with sources and source_segments tables. Write CRUD module. Verify with unit tests.

2. **Intake adapters** — Build intake_doc.py and intake_notes.py. Wire to source_store. Test: `vault ingest docs/chatgpt-memory-discussion-deduped.md` creates source + segments.

3. **L1 atom extensions** — ALTER TABLE atoms ADD COLUMN for source_id, source_type, unit_type, observed_labels. Backfill existing 415 atoms. Verify no data loss (row count, spot check).

4. **evidence_extractor.py** — Build non-chat evidence extraction pipeline (source_segments → Haiku → atoms with unit_type and observed_labels). Test with a small document.

5. **beliefs.db + belief_store.py** — Create new database with full L3 schema. Write CRUD module. Pre-populate worlds table with 8 canonical worlds.

6. **l3_engine.py + l3_module.py** — Build L3 runtime: form extraction from atoms, world assignment, inference rule framework. Define L3Module protocol. Build PersonalMemoryModule with 4 rules.

7. **facts.db migration** — Migrate 1258 facts to logical_forms in beliefs.db. Freeze facts.db as read-only. Write migration report.

8. **CLI commands** — Wire vault ingest, vault l3, vault beliefs, vault conflicts, vault derived into vault CLI.

9. **L2Organizer protocol** — Define protocol in l2_organizer.py. Wrap existing graph_store.py as EntityGraphOrganizer (interface only, no behavioral change).

10. **Tests + validation** — 10+ pytest tests covering all new functionality. Run full pipeline end-to-end: ingest doc → extract evidence → extract logical forms → run inference → query beliefs.

---

## 9. Tasks

- [ ] Create `source_store.py` with sources.db schema, init, CRUD functions
- [ ] Create `intake_doc.py` — markdown/text file → source + segments
- [ ] Create `intake_notes.py` — freeform text → source + segments
- [ ] ALTER TABLE atoms.db: add source_id, source_type, unit_type, observed_labels columns
- [ ] Write and run backfill script for existing 415 atoms (source_type='chat', unit_type mapping)
- [ ] Create `evidence_extractor.py` — source_segments → L1 evidence units via Haiku
- [ ] Create `belief_store.py` with beliefs.db schema (logical_forms, worlds, form_status, derived_objects, inference_log, l3_state)
- [ ] Pre-populate worlds table with 8 canonical Kripke worlds
- [ ] Create `l3_module.py` — L3Module protocol definition
- [ ] Create `personal_memory_module.py` — PersonalMemoryModule with form_types, derived_types, 4 inference rules
- [ ] Create `l3_engine.py` — runtime orchestrator: extract_forms(), assign_worlds(), run_inference()
- [ ] Implement conflict detection rule (two current claims, same subject+predicate, different object → contradiction)
- [ ] Implement supersession rule (new claim on same subject → old moves to world=past)
- [ ] Implement stable promotion rule (claim in 3+ source units, no contradiction → stable_belief)
- [ ] Implement lesson extraction rule (failure + decision on shared entities → lesson)
- [ ] Create `migrate_facts.py` — facts.db → beliefs.db migration with backup and report
- [ ] Run facts.db migration; freeze facts.db as read-only
- [ ] Create `l2_organizer.py` — L2Organizer protocol; wrap graph_store as EntityGraphOrganizer
- [ ] Add `vault ingest` CLI command (dispatch to intake adapters by file type or --type flag)
- [ ] Add `vault l3 extract` CLI command (run L3 extraction on new/all evidence units)
- [ ] Add `vault beliefs [query]` CLI command (list/search current beliefs)
- [ ] Add `vault conflicts` CLI command (list contradiction derived objects)
- [ ] Add `vault derived [--type T] [--namespace N]` CLI command
- [ ] Write 10+ pytest tests: source CRUD, evidence extraction, form extraction, each inference rule, world assignment, backfill validation
- [ ] End-to-end validation: ingest a document → extract evidence → extract forms → run inference → query beliefs
