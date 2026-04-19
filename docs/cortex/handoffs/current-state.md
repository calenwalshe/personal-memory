# Current State

**slug:** knowledge-consolidation-engine

**mode:** execute

**approval_status:** pending

**active_contract_path:** docs/cortex/contracts/knowledge-consolidation-engine/contract-001.md

**recent_artifacts:**
- docs/cortex/clarify/knowledge-consolidation-engine/20260419T030000Z-clarify-brief.md
- docs/cortex/research/knowledge-consolidation-engine/current-understanding.md
- docs/cortex/specs/knowledge-consolidation-engine/spec.md
- docs/cortex/specs/knowledge-consolidation-engine/gsd-handoff.md
- docs/cortex/contracts/knowledge-consolidation-engine/contract-001.md

**open_questions:**
- How should the existing 1258 facts in facts.db be migrated? (semantic→logical_forms, episodic→evidence-linked observations, procedural→rule_candidate forms, contradictions→derived_objects)
- What is the right extraction trigger for L3? (v1: on-demand via `vault l3 extract`)
- Should L3 extraction run on ALL existing atoms (415 backfill) or only new atoms going forward?

**blockers:**
- (none)

**next_action:** Approve contract-001, then begin execution (Phase 1: sources.db + source_store.py)
