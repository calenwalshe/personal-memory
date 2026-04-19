# Contract: knowledge-consolidation-engine — execute

**ID:** knowledge-consolidation-engine-001
**Slug:** knowledge-consolidation-engine
**Phase:** execute
**Created:** 20260419T033000Z
**Status:** draft
**Repair Budget:** max_repair_contracts: 3, cooldown_between_repairs: 1

---

## Objective

Build the universal knowledge consolidation engine: sources.db for multi-format intake, extended atoms as evidence units, and beliefs.db with Kripke worlds, 4 inference rules, and namespaced PersonalMemoryModule — so the vault answers "what do I believe?" not just "what happened?"

---

## Deliverables

- `~/memory/vault/sources.db` — universal source intake database
- `~/memory/vault/beliefs.db` — L3 belief runtime database
- `~/memory/vault/scripts/source_store.py` — sources.db CRUD
- `~/memory/vault/scripts/intake_doc.py` — document intake adapter
- `~/memory/vault/scripts/intake_notes.py` — notes intake adapter
- `~/memory/vault/scripts/evidence_extractor.py` — non-chat → L1 evidence units
- `~/memory/vault/scripts/belief_store.py` — beliefs.db CRUD
- `~/memory/vault/scripts/l3_engine.py` — L3 runtime orchestrator
- `~/memory/vault/scripts/l3_module.py` — L3Module protocol
- `~/memory/vault/scripts/personal_memory_module.py` — PersonalMemoryModule
- `~/memory/vault/scripts/migrate_facts.py` — facts.db migration
- `~/memory/vault/scripts/l2_organizer.py` — L2Organizer protocol
- `~/memory/vault/scripts/test_knowledge_engine.py` — test suite
- `~/memory/vault/bin/vault` — new CLI commands

---

## Scope

### In Scope

- sources.db creation (sources, source_segments, source_state tables)
- Intake adapters for documents and notes
- atoms.db schema extension (4 new nullable columns)
- Backfill of 415 existing atoms
- evidence_extractor.py for non-chat sources
- beliefs.db creation (logical_forms, worlds, form_status, derived_objects, inference_log, l3_state)
- L3 runtime engine (form extraction, world assignment, inference execution)
- L3Module protocol definition
- PersonalMemoryModule with 4 inference rules (conflict, supersede, stable, lesson)
- facts.db migration or freeze
- L2Organizer protocol definition (wrap existing graph_store)
- CLI commands: vault ingest, vault l3 extract, vault beliefs, vault conflicts, vault derived
- Tests (10+ pytest)

### Out of Scope

- PDF/CSV/JSON/code-log intake adapters
- CortexModule and ResearchModule
- Additional L2 organizers
- L2 run tracking tables
- Separate evidence_units table
- Label provenance table
- Datalog/Soufflé engine
- FAISS index for beliefs.db
- Automatic L3 extraction in chunker pipeline
- Hook modifications

---

## Write Roots

- `~/memory/vault/scripts/source_store.py`
- `~/memory/vault/scripts/intake_doc.py`
- `~/memory/vault/scripts/intake_notes.py`
- `~/memory/vault/scripts/evidence_extractor.py`
- `~/memory/vault/scripts/belief_store.py`
- `~/memory/vault/scripts/l3_engine.py`
- `~/memory/vault/scripts/l3_module.py`
- `~/memory/vault/scripts/personal_memory_module.py`
- `~/memory/vault/scripts/migrate_facts.py`
- `~/memory/vault/scripts/l2_organizer.py`
- `~/memory/vault/scripts/test_knowledge_engine.py`
- `~/memory/vault/bin/vault`
- `~/memory/vault/sources.db`
- `~/memory/vault/beliefs.db`
- `~/memory/vault/atoms.db` (ALTER TABLE only)

---

## Done Criteria

- [ ] `vault ingest <file>` creates source + segments + evidence units from a markdown file
- [ ] `vault ingest --type note "text"` creates source + segments + evidence units from stdin
- [ ] Non-chat atoms have source_type and unit_type set correctly
- [ ] 415 existing atoms backfilled without data loss (row count preserved)
- [ ] beliefs.db has all 6 tables with correct schema
- [ ] `vault l3 extract` produces logical_forms from evidence units
- [ ] World assignment produces form_status records with correct world and status
- [ ] Conflict detection rule produces contradiction derived_objects
- [ ] Supersession rule moves old claims to world=past
- [ ] Stable promotion rule produces stable_belief derived_objects
- [ ] Lesson extraction rule produces lesson derived_objects
- [ ] `vault beliefs` returns current beliefs with world/status
- [ ] `vault conflicts` lists contradictions
- [ ] `vault derived` lists derived objects with filtering
- [ ] inference_log has entries for all rule firings
- [ ] facts.db migrated or explicitly frozen with report
- [ ] PersonalMemoryModule registered as default module
- [ ] All databases use WAL mode
- [ ] Existing vault commands unchanged
- [ ] 10+ pytest tests pass

---

## Validators

- [ ] [external] `sqlite3 ~/memory/vault/sources.db ".tables"` shows sources, source_segments, source_state
- [ ] [external] `sqlite3 ~/memory/vault/beliefs.db ".tables"` shows logical_forms, worlds, form_status, derived_objects, inference_log, l3_state
- [ ] [external] `sqlite3 ~/memory/vault/atoms.db "SELECT COUNT(*) FROM atoms WHERE source_type IS NOT NULL"` returns 415+
- [ ] [external] `sqlite3 ~/memory/vault/atoms.db "SELECT COUNT(*) FROM atoms WHERE unit_type IS NOT NULL"` returns 415+
- [ ] [external] `sqlite3 ~/memory/vault/beliefs.db "SELECT COUNT(*) FROM worlds"` returns 8
- [ ] [external] `cd ~/memory/vault/scripts && python3 -m pytest test_knowledge_engine.py -v` passes with 10+ tests
- [ ] [external] `vault ingest ~/projects/personal-memory/docs/chatgpt-memory-discussion-deduped.md && vault l3 extract --new && vault beliefs` produces output
- [ ] [external] `vault atoms list --limit 5` still works (backward compat)
- [ ] [external] `vault graph stats` still works (backward compat)
- [ ] [external] `vault recall "memory system"` still works (backward compat)
- [ ] [judgment] Derived objects (beliefs, contradictions, lessons) are semantically meaningful — not noise

---

## Eval Plan

docs/cortex/evals/knowledge-consolidation-engine/eval-plan.md

---

## Approvals

- [ ] Contract approval
- [ ] Evals approval

---

## Completion Promise

<!-- CORTEX_PROMISE: knowledge-consolidation-engine-001 COMPLETE -->

---

## Failed Approaches

N/A — initial contract

---

## Why Previous Approach Failed

N/A — initial contract

---

## Rollback Hints

- Delete `~/memory/vault/sources.db` (new file, no dependencies)
- Delete `~/memory/vault/beliefs.db` (new file, no dependencies)
- Revert atoms.db: `ALTER TABLE atoms DROP COLUMN source_id; ALTER TABLE atoms DROP COLUMN source_type; ALTER TABLE atoms DROP COLUMN unit_type; ALTER TABLE atoms DROP COLUMN observed_labels;` (SQLite 3.35+ supports DROP COLUMN)
- Restore facts.db from backup (created by migrate_facts.py before migration)
- Remove new scripts: source_store.py, intake_doc.py, intake_notes.py, evidence_extractor.py, belief_store.py, l3_engine.py, l3_module.py, personal_memory_module.py, migrate_facts.py, l2_organizer.py, test_knowledge_engine.py
- Revert vault CLI changes (git checkout ~/memory/vault/bin/vault)

---

## Repair Budget

**max_repair_contracts:** 3
**cooldown_between_repairs:** 1
