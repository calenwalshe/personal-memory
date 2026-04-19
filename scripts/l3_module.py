"""
l3_module.py — L3 domain module protocol + PersonalMemoryModule.

L3 modules sit on top of the belief runtime engine (beliefs.db) and specialize:
  1. What logical forms to extract from evidence units
  2. What inference rules to fire
  3. What derived object types to produce

Modules are namespaced — PersonalMemoryModule produces "personal:*" derived
objects, CortexModule (future) produces "cortex:*", etc. Multiple modules can
be enabled simultaneously without collision.

The 4 v1 inference rules:
  conflict    — two current claims, same subject+predicate, different object → contradiction
  supersede   — new claim on same subject → old moves to world=past, status=superseded
  stable      — claim in 3+ source units, no contradiction → stable_belief
  lesson      — failure evidence + decision on same entities → lesson
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent))

from belief_store import (
    add_derived, set_form_status, expire_form_status,
    supersede_form, log_inference, get_forms_in_world,
    get_contradictions, _conn as beliefs_conn,
)


# ── Inference Rule Protocol ────────────────────────────────────────────────

@dataclass
class RuleFiring:
    """Result of an inference rule evaluation."""
    rule_name: str
    input_form_ids: list[str]
    action: str              # created | updated | superseded | pruned
    output_type: str         # derived object type or "status_change"
    output_content: str
    confidence: float = 0.7
    detail: str = ""


class InferenceRule:
    """Base class for inference rules."""
    name: str = "unnamed"
    module: str = "personal"

    def evaluate(
        self,
        forms: list[dict],
        derived: list[dict],
        statuses: list[dict],
    ) -> list[RuleFiring]:
        """Evaluate the rule against current state. Returns list of firings."""
        raise NotImplementedError


# ── Conflict Detection Rule ────────────────────────────────────────────────

class ConflictDetectionRule(InferenceRule):
    """Two current claims with same subject+predicate but different object → contradiction."""
    name = "conflict_detection"

    def evaluate(self, forms, derived, statuses):
        firings = []

        # Group current/active forms by (subject, predicate)
        by_key: dict[tuple, list[dict]] = {}
        for f in forms:
            if not f.get("subject") or not f.get("predicate"):
                continue
            key = (f["subject"].lower().strip(), f["predicate"].lower().strip())
            by_key.setdefault(key, []).append(f)

        # Check existing contradictions to avoid duplicates
        existing_contradictions = set()
        for d in derived:
            if d["type"] == "contradiction":
                ids = set(json.loads(d.get("source_form_ids", "[]")))
                existing_contradictions.add(frozenset(ids))

        for key, group in by_key.items():
            if len(group) < 2:
                continue

            # Compare objects
            seen_objects: dict[str, dict] = {}
            for f in group:
                obj = (f.get("object") or "").lower().strip()
                if not obj:
                    continue
                if obj in seen_objects:
                    continue

                for prev_obj, prev_f in seen_objects.items():
                    if prev_obj != obj:
                        pair = frozenset([f["id"], prev_f["id"]])
                        if pair not in existing_contradictions:
                            firings.append(RuleFiring(
                                rule_name=self.name,
                                input_form_ids=[prev_f["id"], f["id"]],
                                action="created",
                                output_type="contradiction",
                                output_content=(
                                    f"Conflicting claims on {key[0]}/{key[1]}: "
                                    f'"{prev_f["content"]}" vs "{f["content"]}"'
                                ),
                                confidence=min(prev_f.get("confidence", 0.7),
                                             f.get("confidence", 0.7)),
                                detail=f"subject={key[0]}, predicate={key[1]}",
                            ))
                            existing_contradictions.add(pair)

                seen_objects[obj] = f

        return firings


# ── Supersession Rule ──────────────────────────────────────────────────────

class SupersessionRule(InferenceRule):
    """New claim on same subject → old claim moves to world=past, status=superseded."""
    name = "supersession"

    def evaluate(self, forms, derived, statuses):
        firings = []

        # Group by subject — look for forms with the same subject and type
        # where a newer one should supersede an older one
        by_subject: dict[str, list[dict]] = {}
        for f in forms:
            if not f.get("subject"):
                continue
            subj = f["subject"].lower().strip()
            by_subject.setdefault(subj, []).append(f)

        for subj, group in by_subject.items():
            if len(group) < 2:
                continue

            # Sort by extraction time (newest first)
            sorted_group = sorted(group, key=lambda x: x.get("extracted_at", ""), reverse=True)

            # For same form_type + same predicate, newer supersedes older
            seen: dict[tuple, dict] = {}
            for f in sorted_group:
                key = (f["form_type"], (f.get("predicate") or "").lower().strip())
                if key in seen:
                    # This is an older form — check if already superseded
                    if not f.get("superseded_by"):
                        newer = seen[key]
                        firings.append(RuleFiring(
                            rule_name=self.name,
                            input_form_ids=[newer["id"], f["id"]],
                            action="superseded",
                            output_type="status_change",
                            output_content=(
                                f'Superseded: "{f["content"]}" → "{newer["content"]}"'
                            ),
                            confidence=newer.get("confidence", 0.7),
                            detail=f"subject={subj}, type={key[0]}, predicate={key[1]}",
                        ))
                else:
                    seen[key] = f

        return firings


# ── Stable Promotion Rule ─────────────────────────────────────────────────

class StablePromotionRule(InferenceRule):
    """Claim appearing in 3+ source units with no contradiction → stable_belief."""
    name = "stable_promotion"

    def __init__(self, threshold: int = 3):
        self.threshold = threshold

    def evaluate(self, forms, derived, statuses):
        firings = []

        # Get existing stable beliefs to avoid duplicates
        existing_stable = set()
        for d in derived:
            if d["type"] == "stable_belief":
                ids = json.loads(d.get("source_form_ids", "[]"))
                existing_stable.update(ids)

        # Get contradicted form ids
        contradicted_ids = set()
        for d in derived:
            if d["type"] == "contradiction":
                contradicted_ids.update(json.loads(d.get("source_form_ids", "[]")))

        for f in forms:
            if f["id"] in existing_stable:
                continue
            if f["id"] in contradicted_ids:
                continue

            # Count supporting source units
            unit_ids = json.loads(f.get("source_unit_ids", "[]"))
            # Also count the primary source_unit_id
            if f.get("source_unit_id"):
                unit_ids = list(set(unit_ids + [f["source_unit_id"]]))

            if len(unit_ids) >= self.threshold:
                firings.append(RuleFiring(
                    rule_name=self.name,
                    input_form_ids=[f["id"]],
                    action="created",
                    output_type="stable_belief",
                    output_content=f'Stable: "{f["content"]}" (backed by {len(unit_ids)} evidence units)',
                    confidence=min(f.get("confidence", 0.7) + 0.1, 1.0),
                    detail=f"source_units={len(unit_ids)}, threshold={self.threshold}",
                ))

        return firings


# ── Lesson Extraction Rule ─────────────────────────────────────────────────

class LessonExtractionRule(InferenceRule):
    """Failure evidence unit + decision claim sharing entities → lesson."""
    name = "lesson_extraction"

    def evaluate(self, forms, derived, statuses):
        firings = []

        # Get existing lessons to avoid duplicates
        existing_lessons = set()
        for d in derived:
            if d["type"] == "lesson":
                existing_lessons.add(frozenset(json.loads(d.get("source_form_ids", "[]"))))

        # Find failure-type forms and decision-type forms
        failures = [f for f in forms if f["form_type"] == "warning"
                    and "fail" in (f.get("content") or "").lower()]
        decisions = [f for f in forms if f["form_type"] == "decision"]

        for fail in failures:
            fail_entities = set(json.loads(fail.get("entity_ids", "[]")))
            if not fail_entities:
                # Try subject as entity
                if fail.get("subject"):
                    fail_entities = {fail["subject"].lower()}

            for dec in decisions:
                dec_entities = set(json.loads(dec.get("entity_ids", "[]")))
                if not dec_entities:
                    if dec.get("subject"):
                        dec_entities = {dec["subject"].lower()}

                # Check entity overlap
                overlap = fail_entities & dec_entities
                if overlap:
                    pair = frozenset([fail["id"], dec["id"]])
                    if pair not in existing_lessons:
                        firings.append(RuleFiring(
                            rule_name=self.name,
                            input_form_ids=[fail["id"], dec["id"]],
                            action="created",
                            output_type="lesson",
                            output_content=(
                                f'Lesson from failure: "{fail["content"]}" '
                                f'→ Decision: "{dec["content"]}"'
                            ),
                            confidence=min(fail.get("confidence", 0.7),
                                         dec.get("confidence", 0.7)),
                            detail=f"shared_entities={list(overlap)}",
                        ))
                        existing_lessons.add(pair)

        return firings


# ── PersonalMemoryModule ──────────────────────────────────────────────────

class PersonalMemoryModule:
    """First L3 module — tracks beliefs, preferences, lessons, stable facts."""

    name = "personal"
    namespace = "personal"

    form_types = ["claim", "preference", "decision", "warning", "rule", "event", "plan", "question"]

    derived_types = [
        "stable_belief", "lesson", "contradiction", "open_thread",
        "preference_shift", "design_rule",
    ]

    def inference_rules(self) -> list[InferenceRule]:
        return [
            ConflictDetectionRule(),
            SupersessionRule(),
            StablePromotionRule(threshold=3),
            LessonExtractionRule(),
        ]


# ── Module registry ───────────────────────────────────────────────────────

MODULES: dict[str, type] = {
    "personal": PersonalMemoryModule,
}


def get_module(name: str = "personal") -> PersonalMemoryModule:
    cls = MODULES.get(name)
    if not cls:
        raise ValueError(f"Unknown L3 module '{name}'. Available: {list(MODULES.keys())}")
    return cls()
