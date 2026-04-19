# GSD Handoff: knowledge-consolidation-engine

**Slug:** knowledge-consolidation-engine
**Timestamp:** 20260419T033000Z
**Status:** draft

---

## Objective

Build a universal knowledge consolidation engine for the SCAPE memory vault. Add a universal source intake layer (sources.db) that accepts any document type, extend L1 atoms into evidence units with source provenance and candidate labels, and build an L3 belief runtime engine (beliefs.db) with Kripke-inspired worlds, temporal truth tracking, 4 inference rules, and namespaced domain modules — so the vault transitions from "what happened in chat" to "what do I believe, why, and how confident am I."

---

## Deliverables

- `~/memory/vault/sources.db` — new SQLite database with sources + source_segments tables
- `~/memory/vault/beliefs.db` — new SQLite database with L3 schema (logical_forms, worlds, form_status, derived_objects, inference_log, l3_state)
- `~/memory/vault/scripts/source_store.py` — CRUD layer for sources.db
- `~/memory/vault/scripts/intake_doc.py` — document intake adapter
- `~/memory/vault/scripts/intake_notes.py` — notes intake adapter
- `~/memory/vault/scripts/evidence_extractor.py` — non-chat source → L1 evidence units
- `~/memory/vault/scripts/belief_store.py` — CRUD layer for beliefs.db
- `~/memory/vault/scripts/l3_engine.py` — L3 runtime orchestrator
- `~/memory/vault/scripts/l3_module.py` — L3Module protocol
- `~/memory/vault/scripts/personal_memory_module.py` — PersonalMemoryModule with 4 inference rules
- `~/memory/vault/scripts/migrate_facts.py` — facts.db → beliefs.db migration
- `~/memory/vault/scripts/l2_organizer.py` — L2Organizer protocol
- `~/memory/vault/scripts/test_knowledge_engine.py` — pytest test suite
- `~/memory/vault/bin/vault` — extended with vault ingest, vault l3, vault beliefs, vault conflicts, vault derived commands

---

## Requirements

- None formalized

---

## Tasks

- [ ] Create `source_store.py` with sources.db schema (sources, source_segments, source_state), init function, CRUD for source creation and segment creation
- [ ] Create `intake_doc.py` — reads markdown/text file, creates source record, splits into paragraph/section segments
- [ ] Create `intake_notes.py` — reads freeform text from stdin or string, creates source + segments
- [ ] ALTER TABLE atoms.db: add source_id, source_type, unit_type, observed_labels columns (all nullable, backward-compatible)
- [ ] Write and run backfill script: set source_type='chat' for all 415 atoms, map atom_type → unit_type using UNIT_TYPE_MAP dict
- [ ] Create `evidence_extractor.py` — takes source_id, reads segments, calls Haiku to extract typed evidence units, writes to atoms.db with source_type and unit_type set
- [ ] Create `belief_store.py` with beliefs.db schema (logical_forms, worlds, form_status, derived_objects, inference_log, l3_state), init function, CRUD operations
- [ ] Pre-populate worlds table: current, past, planned, possible, contested, rejected, user_belief, system_belief
- [ ] Create `l3_module.py` — define L3Module protocol (name, form_types, derived_types, extract_forms, inference_rules, assign_world)
- [ ] Create `personal_memory_module.py` — PersonalMemoryModule implementing L3Module with 4 inference rules
- [ ] Implement conflict detection rule: two current claims with same subject+predicate but different object → derived_object type=contradiction
- [ ] Implement supersession rule: new claim on same subject → old claim's form_status moves to world=past, status=superseded
- [ ] Implement stable promotion rule: claim appearing in 3+ source units with no contradiction → derived_object type=stable_belief
- [ ] Implement lesson extraction rule: failure evidence unit + decision claim sharing entities → derived_object type=lesson
- [ ] Create `l3_engine.py` — orchestrates: extract_forms (atoms → logical_forms via Haiku), assign_worlds (place forms in worlds), run_inference (fire all rules, write derived_objects + inference_log)
- [ ] Create `migrate_facts.py` — backup facts.db, migrate 1258 facts to logical_forms (semantic→claim, procedural→rule, episodic→linked observation), write migration report, freeze facts.db
- [ ] Run facts.db migration
- [ ] Create `l2_organizer.py` — L2Organizer protocol (ingest, query, rebuild, stats); wrap graph_store.py as EntityGraphOrganizer
- [ ] Add `vault ingest <file> [--type note|doc]` CLI command dispatching to intake adapters
- [ ] Add `vault l3 extract [--all|--new]` CLI command running L3 form extraction
- [ ] Add `vault beliefs [query]` CLI command listing/searching current beliefs
- [ ] Add `vault conflicts` CLI command listing contradictions
- [ ] Add `vault derived [--type T] [--namespace N]` CLI command listing derived objects
- [ ] Write 10+ pytest tests covering: source CRUD, evidence extraction, form extraction, each inference rule, world assignment, backfill, migration
- [ ] End-to-end validation: ingest chatgpt-memory-discussion-deduped.md → extract evidence → extract forms → run inference → query beliefs

---

## Acceptance Criteria

- [ ] `vault ingest <file>` accepts a markdown/text document, creates a source record in sources.db with source_segments, and produces L1 evidence units in atoms.db
- [ ] `vault ingest --type note "free text"` accepts stdin/string input and produces evidence units
- [ ] Non-chat atoms have `source_type != 'chat'` and `unit_type` set to a candidate type
- [ ] Existing 415 atoms are backfilled with `source_type='chat'` and mapped `unit_type` values without data loss
- [ ] `beliefs.db` exists with tables: logical_forms, worlds, form_status, derived_objects, inference_log, l3_state
- [ ] `vault l3 extract` processes L1 evidence units into logical_forms in beliefs.db
- [ ] `vault l3 extract` correctly assigns worlds and form_status
- [ ] Conflict detection rule fires correctly
- [ ] Supersession rule fires correctly
- [ ] Stable promotion rule fires correctly
- [ ] Lesson extraction rule fires correctly
- [ ] `vault beliefs [query]` returns current beliefs with world and status
- [ ] `vault conflicts` lists all contradictions
- [ ] `vault derived` lists derived objects filterable by namespace and type
- [ ] All inference rule firings recorded in inference_log
- [ ] facts.db migrated or frozen with report
- [ ] PersonalMemoryModule registered as default L3 module
- [ ] All new databases use WAL mode
- [ ] Existing vault commands continue to work unchanged
- [ ] 10+ pytest tests pass

---

## Contract Link

docs/cortex/contracts/knowledge-consolidation-engine/contract-001.md
