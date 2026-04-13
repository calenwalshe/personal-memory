"""
Tests for subagent_graphbuilder.py — parse extract_graph tuples, dedupe
entities, assemble parquets in graphrag 3.0.8 schema.
"""
from __future__ import annotations

import pandas as pd
import pytest

import subagent_graphbuilder as sgb


# --------------------------------------------------------------------------- #
# parse_extract_graph_output — graphrag's pipe-delimited tuple format
# --------------------------------------------------------------------------- #

class TestParseExtractGraphOutput:
    def test_parses_single_entity(self):
        text = '("entity"<|>ALICE<|>PERSON<|>Alice is a software engineer)\n<|COMPLETE|>'
        entities, relationships = sgb.parse_extract_graph_output(text, source_id="tu1")
        assert len(entities) == 1
        assert entities[0]["title"] == "ALICE"
        assert entities[0]["type"] == "PERSON"
        assert "software engineer" in entities[0]["description"]
        assert entities[0]["source_id"] == "tu1"
        assert len(relationships) == 0

    def test_parses_entity_and_relationship(self):
        text = (
            '("entity"<|>ALICE<|>PERSON<|>Software engineer)\n'
            '##\n'
            '("entity"<|>BOB<|>PERSON<|>Product manager)\n'
            '##\n'
            '("relationship"<|>ALICE<|>BOB<|>Alice and Bob collaborate on features<|>7)\n'
            '<|COMPLETE|>'
        )
        entities, relationships = sgb.parse_extract_graph_output(text, source_id="tu2")
        assert len(entities) == 2
        assert {e["title"] for e in entities} == {"ALICE", "BOB"}
        assert len(relationships) == 1
        assert relationships[0]["source"] == "ALICE"
        assert relationships[0]["target"] == "BOB"
        assert relationships[0]["weight"] == 7.0
        assert relationships[0]["source_id"] == "tu2"

    def test_tolerates_missing_completion_marker(self):
        text = '("entity"<|>CHARLIE<|>PERSON<|>Researcher)\n##\n'
        entities, relationships = sgb.parse_extract_graph_output(text, source_id="tu3")
        assert len(entities) == 1

    def test_empty_input_returns_empty_lists(self):
        entities, relationships = sgb.parse_extract_graph_output("", source_id="tu4")
        assert entities == [] and relationships == []

    def test_malformed_tuples_skipped(self):
        text = (
            '("entity"<|>INCOMPLETE)\n##\n'  # missing type and description
            '("entity"<|>VALID<|>PERSON<|>Valid entity)\n##\n'
        )
        entities, _ = sgb.parse_extract_graph_output(text, source_id="tu5")
        assert len(entities) == 1
        assert entities[0]["title"] == "VALID"

    def test_relationship_without_numeric_weight_defaults_to_1(self):
        text = '("relationship"<|>A<|>B<|>Related somehow<|>strong)\n<|COMPLETE|>'
        _, relationships = sgb.parse_extract_graph_output(text, source_id="tu6")
        assert len(relationships) == 1
        assert relationships[0]["weight"] == 1.0

    def test_uppercases_entity_names(self):
        text = '("entity"<|>lowercase name<|>person<|>desc)'
        entities, _ = sgb.parse_extract_graph_output(text, source_id="tu7")
        assert entities[0]["title"] == "LOWERCASE NAME"


# --------------------------------------------------------------------------- #
# deduplicate_entities — merge duplicate titles across text units
# --------------------------------------------------------------------------- #

class TestDeduplicateEntities:
    def test_dedupes_by_title(self):
        raw = [
            {"title": "ALICE", "type": "PERSON", "description": "engineer", "source_id": "tu1"},
            {"title": "ALICE", "type": "PERSON", "description": "TDD advocate", "source_id": "tu2"},
            {"title": "BOB", "type": "PERSON", "description": "pm", "source_id": "tu1"},
        ]
        deduped = sgb.deduplicate_entities(raw)
        assert len(deduped) == 2
        alice = next(e for e in deduped if e["title"] == "ALICE")
        assert alice["frequency"] == 2
        assert "tu1" in alice["text_unit_ids"] and "tu2" in alice["text_unit_ids"]
        # Descriptions concatenated (order-independent)
        assert "engineer" in alice["description"] and "TDD advocate" in alice["description"]

    def test_type_conflict_keeps_first(self):
        raw = [
            {"title": "ACME", "type": "ORGANIZATION", "description": "co", "source_id": "tu1"},
            {"title": "ACME", "type": "PERSON", "description": "person?", "source_id": "tu2"},
        ]
        deduped = sgb.deduplicate_entities(raw)
        assert len(deduped) == 1
        assert deduped[0]["type"] == "ORGANIZATION"

    def test_empty_input(self):
        assert sgb.deduplicate_entities([]) == []


# --------------------------------------------------------------------------- #
# build_entity_dataframe — graphrag schema
# --------------------------------------------------------------------------- #

class TestBuildEntityDataframe:
    def test_schema_matches_graphrag(self):
        deduped = [
            {"title": "ALICE", "type": "PERSON", "description": "eng", "text_unit_ids": ["tu1"], "frequency": 1},
        ]
        df = sgb.build_entity_dataframe(deduped)
        required = {"id", "human_readable_id", "title", "type", "description",
                    "text_unit_ids", "frequency", "degree"}
        assert required <= set(df.columns)

    def test_assigns_sequential_human_readable_ids(self):
        deduped = [
            {"title": f"E{i}", "type": "PERSON", "description": "d",
             "text_unit_ids": ["tu1"], "frequency": 1}
            for i in range(5)
        ]
        df = sgb.build_entity_dataframe(deduped)
        assert sorted(df["human_readable_id"].tolist()) == [0, 1, 2, 3, 4]

    def test_ids_are_unique_stable_strings(self):
        deduped = [
            {"title": "ALICE", "type": "PERSON", "description": "d", "text_unit_ids": ["tu1"], "frequency": 1},
            {"title": "BOB", "type": "PERSON", "description": "d", "text_unit_ids": ["tu1"], "frequency": 1},
        ]
        df1 = sgb.build_entity_dataframe(deduped)
        df2 = sgb.build_entity_dataframe(deduped)
        assert df1["id"].tolist() == df2["id"].tolist()  # stable
        assert df1["id"].nunique() == 2


# --------------------------------------------------------------------------- #
# build_relationship_dataframe
# --------------------------------------------------------------------------- #

class TestBuildRelationshipDataframe:
    def test_schema_and_degree_computation(self):
        entities_df = pd.DataFrame([
            {"title": "A", "id": "a-id"},
            {"title": "B", "id": "b-id"},
            {"title": "C", "id": "c-id"},
        ])
        raw_rels = [
            {"source": "A", "target": "B", "description": "ab", "weight": 5.0, "source_id": "tu1"},
            {"source": "B", "target": "C", "description": "bc", "weight": 3.0, "source_id": "tu2"},
        ]
        df = sgb.build_relationship_dataframe(raw_rels, entities_df)
        required = {"id", "human_readable_id", "source", "target", "description",
                    "weight", "combined_degree", "text_unit_ids"}
        assert required <= set(df.columns)
        assert len(df) == 2

    def test_drops_relationships_with_unknown_entities(self):
        entities_df = pd.DataFrame([{"title": "A", "id": "a-id"}])
        raw_rels = [
            {"source": "A", "target": "GHOST", "description": "x", "weight": 1.0, "source_id": "tu1"},
        ]
        df = sgb.build_relationship_dataframe(raw_rels, entities_df)
        assert len(df) == 0


# --------------------------------------------------------------------------- #
# parse_community_report_output — accepts JSON (possibly in prose)
# --------------------------------------------------------------------------- #

class TestParseCommunityReport:
    def test_parses_clean_json(self):
        import json
        payload = json.dumps({
            "title": "Test Community",
            "summary": "Summary text",
            "rating": 6.5,
            "rating_explanation": "because",
            "findings": [{"summary": "s1", "explanation": "e1"}],
        })
        report = sgb.parse_community_report_output(payload)
        assert report["title"] == "Test Community"
        assert report["rating"] == 6.5
        assert len(report["findings"]) == 1

    def test_parses_json_in_prose(self):
        text = 'Sure! Here you go:\n```json\n{"title": "X", "summary": "Y", "rating": 5, "rating_explanation": "z", "findings": []}\n```\nDone!'
        report = sgb.parse_community_report_output(text)
        assert report["title"] == "X"

    def test_returns_none_on_malformed(self):
        assert sgb.parse_community_report_output("total garbage") is None


# --------------------------------------------------------------------------- #
# build_community_reports_dataframe
# --------------------------------------------------------------------------- #

class TestBuildCommunityReportsDataframe:
    def test_schema(self):
        communities_df = pd.DataFrame([
            {"id": "c0-id", "community": 0, "level": 0, "parent": -1, "children": [],
             "title": "", "entity_ids": ["a-id"], "relationship_ids": [],
             "text_unit_ids": ["tu1"], "period": "2026-04-11", "size": 1,
             "human_readable_id": 0},
        ])
        raw_reports = {
            0: {
                "title": "Community Zero",
                "summary": "sum",
                "rating": 7.0,
                "rating_explanation": "x",
                "findings": [{"summary": "s1", "explanation": "e1"}],
            }
        }
        df = sgb.build_community_reports_dataframe(raw_reports, communities_df)
        required = {"id", "human_readable_id", "community", "level", "parent", "children",
                    "title", "summary", "full_content", "rank", "rating_explanation",
                    "findings", "full_content_json", "period", "size"}
        assert required <= set(df.columns)
        assert df.iloc[0]["title"] == "Community Zero"
        assert df.iloc[0]["rank"] == 7.0


# --------------------------------------------------------------------------- #
# Batch iteration
# --------------------------------------------------------------------------- #

class TestBatchTextUnits:
    def test_batches_text_units_by_size(self):
        tu_df = pd.DataFrame([{"id": f"tu{i}", "text": f"text {i}"} for i in range(25)])
        batches = list(sgb.batch_text_units(tu_df, batch_size=10))
        assert len(batches) == 3
        assert len(batches[0]) == 10
        assert len(batches[2]) == 5

    def test_empty_input(self):
        tu_df = pd.DataFrame(columns=["id", "text"])
        assert list(sgb.batch_text_units(tu_df, batch_size=10)) == []


# --------------------------------------------------------------------------- #
# build_extract_prompt — prompt passed to Agent tool
# --------------------------------------------------------------------------- #

class TestBuildExtractPrompt:
    def test_contains_tuple_format_instructions(self):
        batch = [{"id": "tu1", "text": "Sample text about Alice and Bob."}]
        prompt = sgb.build_extract_prompt(batch)
        assert "<|>" in prompt  # tuple delimiter mentioned
        assert "<|COMPLETE|>" in prompt
        assert "===TU:" in prompt  # delimiter between units in the response

    def test_contains_every_text_unit(self):
        batch = [
            {"id": "tu1", "text": "Alpha content"},
            {"id": "tu2", "text": "Beta content"},
        ]
        prompt = sgb.build_extract_prompt(batch)
        assert "tu1" in prompt
        assert "tu2" in prompt
        assert "Alpha content" in prompt
        assert "Beta content" in prompt


# --------------------------------------------------------------------------- #
# parse_agent_extract_response — splits an agent's response into per-TU blocks
# --------------------------------------------------------------------------- #

class TestParseAgentExtractResponse:
    def test_splits_by_tu_marker(self):
        resp = (
            "===TU:tu1===\n"
            '("entity"<|>ALICE<|>PERSON<|>Engineer)\n'
            "##\n<|COMPLETE|>\n"
            "===TU:tu2===\n"
            '("entity"<|>BOB<|>PERSON<|>Designer)\n'
            "##\n<|COMPLETE|>\n"
        )
        blocks = sgb.parse_agent_extract_response(resp)
        assert set(blocks.keys()) == {"tu1", "tu2"}
        assert "ALICE" in blocks["tu1"]
        assert "BOB" in blocks["tu2"]

    def test_ignores_prose_before_first_marker(self):
        resp = "Sure! Here are the extractions:\n===TU:tu1===\n(\"entity\"<|>X<|>T<|>d)\n"
        blocks = sgb.parse_agent_extract_response(resp)
        assert set(blocks.keys()) == {"tu1"}

    def test_empty_response(self):
        assert sgb.parse_agent_extract_response("") == {}


# --------------------------------------------------------------------------- #
# build_community_report_prompt
# --------------------------------------------------------------------------- #

class TestBuildCommunityReportPrompt:
    def test_includes_entity_and_relationship_tables(self):
        entities = [
            {"human_readable_id": 5, "title": "ALICE", "description": "engineer"},
            {"human_readable_id": 6, "title": "BOB", "description": "designer"},
        ]
        relationships = [
            {"human_readable_id": 37, "source": "ALICE", "target": "BOB",
             "description": "collaborate"},
        ]
        prompt = sgb.build_community_report_prompt(
            community_id=0, entities=entities, relationships=relationships
        )
        assert "ALICE" in prompt
        assert "BOB" in prompt
        assert "collaborate" in prompt
        assert '"title"' in prompt  # JSON output format mentioned
