#!/usr/bin/env python3
"""
subagent_graphbuilder.py — alternative phase-1 pipeline that routes
graphrag's entity extraction and community reporting through Claude Code
sub-agents instead of direct LiteLLM → Anthropic API calls.

Why: the direct API path is rate-limited to 10K output TPM on Tier 1,
which makes a full 582-chunk index impractical. Sub-agent tool calls
inherit the parent Claude Code session's OAuth (Max Pro subscription),
which has a different rate bucket more suited to bulk conversational work.

What this file does:
  1. Parses graphrag's extract_graph tuple-delimited output format
  2. Deduplicates entities across text units
  3. Clusters the entity graph using graphrag's own Leiden function
     (imported from graphrag.index.operations.cluster_graph.cluster_graph —
     pure-python, no LLM calls)
  4. Assembles parquet files matching graphrag 3.0.8's exact schema so that
     downstream code (deep_consolidate.py, autoresearch_loop query layer)
     works unchanged

What this file does NOT do:
  - Spawn sub-agents. The orchestrator at the bottom is a reference
    skeleton; actual sub-agent invocation is driven by the parent Claude
    Code conversation via the Agent tool. See the README in
    subagent_prompts/ for the prompt contracts.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

TUPLE_DELIMITER = "<|>"
RECORD_DELIMITER = "##"
COMPLETION_DELIMITER = "<|COMPLETE|>"


# --------------------------------------------------------------------------- #
# Parsing extract_graph output (graphrag's native tuple format)
# --------------------------------------------------------------------------- #

def _clean(text: str) -> str:
    """Strip surrounding quotes/whitespace — mirrors graphrag's clean_str."""
    return text.strip().strip('"').strip()


def parse_extract_graph_output(
    text: str, *, source_id: str
) -> tuple[list[dict], list[dict]]:
    """Parse a sub-agent's extract_graph response into entity and relationship lists.

    Format (per graphrag 3.0.8 extract_graph.txt prompt):
        ("entity"<|>NAME<|>TYPE<|>description)
        ##
        ("relationship"<|>SRC<|>TGT<|>description<|>weight)
        ##
        <|COMPLETE|>
    """
    entities: list[dict] = []
    relationships: list[dict] = []

    if not text:
        return entities, relationships

    # Strip completion marker if present.
    text = text.replace(COMPLETION_DELIMITER, "")

    records = [r.strip() for r in text.split(RECORD_DELIMITER)]
    for raw in records:
        record = re.sub(r"^\(|\)$", "", raw.strip())
        if not record:
            continue
        parts = record.split(TUPLE_DELIMITER)
        if not parts:
            continue
        rtype = parts[0].strip().strip('"')

        if rtype == "entity" and len(parts) >= 4:
            entities.append({
                "title": _clean(parts[1]).upper(),
                "type": _clean(parts[2]).upper(),
                "description": _clean(parts[3]),
                "source_id": source_id,
            })
        elif rtype == "relationship" and len(parts) >= 5:
            try:
                weight = float(parts[-1].strip())
            except ValueError:
                weight = 1.0
            relationships.append({
                "source": _clean(parts[1]).upper(),
                "target": _clean(parts[2]).upper(),
                "description": _clean(parts[3]),
                "weight": weight,
                "source_id": source_id,
            })

    return entities, relationships


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #

def deduplicate_entities(raw_entities: list[dict]) -> list[dict]:
    """Merge entities that share a title. Concatenate descriptions, union
    text_unit_ids, count frequency. Type conflicts: keep the first seen."""
    by_title: dict[str, dict] = {}
    for e in raw_entities:
        title = e["title"]
        if title not in by_title:
            by_title[title] = {
                "title": title,
                "type": e.get("type", ""),
                "description": e.get("description", ""),
                "text_unit_ids": [e.get("source_id")],
                "frequency": 1,
            }
        else:
            existing = by_title[title]
            existing["frequency"] += 1
            sid = e.get("source_id")
            if sid and sid not in existing["text_unit_ids"]:
                existing["text_unit_ids"].append(sid)
            new_desc = e.get("description", "")
            if new_desc and new_desc not in existing["description"]:
                if existing["description"]:
                    existing["description"] += "; " + new_desc
                else:
                    existing["description"] = new_desc
    return list(by_title.values())


# --------------------------------------------------------------------------- #
# Parquet assembly — entities, relationships, communities, community_reports
# --------------------------------------------------------------------------- #

def _stable_id(*parts: str) -> str:
    """128-bit hex ID derived from the joined parts — deterministic across runs."""
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return h[:32]


def build_entity_dataframe(deduped: list[dict]) -> pd.DataFrame:
    """Schema: id, human_readable_id, title, type, description, text_unit_ids,
    frequency, degree. Matches graphrag 3.0.8 entities.parquet exactly."""
    if not deduped:
        return pd.DataFrame(columns=[
            "id", "human_readable_id", "title", "type", "description",
            "text_unit_ids", "frequency", "degree",
        ])
    rows: list[dict] = []
    for i, e in enumerate(deduped):
        rows.append({
            "id": _stable_id("entity", e["title"]),
            "human_readable_id": i,
            "title": e["title"],
            "type": e.get("type", ""),
            "description": e.get("description", ""),
            "text_unit_ids": e.get("text_unit_ids", []),
            "frequency": int(e.get("frequency", 1)),
            "degree": 0,  # filled in after relationships are built
        })
    return pd.DataFrame(rows)


def build_relationship_dataframe(
    raw_relationships: list[dict], entities_df: pd.DataFrame
) -> pd.DataFrame:
    """Schema: id, human_readable_id, source, target, description, weight,
    combined_degree, text_unit_ids. Drops relationships whose endpoints
    don't exist in entities_df."""
    known_titles = set(entities_df["title"].tolist()) if len(entities_df) else set()

    # Aggregate duplicates (same source, target) with summed weight and merged text_units.
    aggregated: dict[tuple[str, str], dict] = {}
    for r in raw_relationships:
        src, tgt = r["source"], r["target"]
        if src not in known_titles or tgt not in known_titles:
            continue
        key = (src, tgt)
        if key not in aggregated:
            aggregated[key] = {
                "source": src,
                "target": tgt,
                "description": r.get("description", ""),
                "weight": float(r.get("weight", 1.0)),
                "text_unit_ids": [r.get("source_id")] if r.get("source_id") else [],
            }
        else:
            ag = aggregated[key]
            ag["weight"] += float(r.get("weight", 1.0))
            new_desc = r.get("description", "")
            if new_desc and new_desc not in ag["description"]:
                ag["description"] += "; " + new_desc if ag["description"] else new_desc
            sid = r.get("source_id")
            if sid and sid not in ag["text_unit_ids"]:
                ag["text_unit_ids"].append(sid)

    if not aggregated:
        return pd.DataFrame(columns=[
            "id", "human_readable_id", "source", "target", "description",
            "weight", "combined_degree", "text_unit_ids",
        ])

    rows: list[dict] = []
    for i, ag in enumerate(aggregated.values()):
        rows.append({
            "id": _stable_id("rel", ag["source"], ag["target"]),
            "human_readable_id": i,
            "source": ag["source"],
            "target": ag["target"],
            "description": ag["description"],
            "weight": float(ag["weight"]),
            "combined_degree": 0,  # filled in later
            "text_unit_ids": ag["text_unit_ids"],
        })
    return pd.DataFrame(rows)


def assign_entities_to_communities(
    entities_df: pd.DataFrame, leiden_result
) -> pd.DataFrame:
    """Write a `community` column onto entities_df using the LEAF (lowest-level)
    Leiden partition for each entity. Matches graphrag 3.0.8's shape where
    entities carry a single community field referenced by deep_consolidate."""
    entities_df = entities_df.copy()
    if not leiden_result or len(entities_df) == 0:
        entities_df["community"] = -1
        return entities_df
    # Find the highest level (leaf) for each node — Leiden level increases
    # as clusters subdivide, so the leaf level is the most-specific assignment.
    title_to_community: dict[str, int] = {}
    title_to_level: dict[str, int] = {}
    for (level, cluster_id, _parent, nodes) in leiden_result:
        for title in nodes:
            if title not in title_to_level or level > title_to_level[title]:
                title_to_level[title] = level
                title_to_community[title] = cluster_id
    entities_df["community"] = entities_df["title"].map(
        lambda t: title_to_community.get(t, -1)
    )
    return entities_df


def compute_degrees(
    entities_df: pd.DataFrame, relationships_df: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compute per-entity degree (count of incident relationships) and per-edge
    combined_degree (source_degree + target_degree). Mutates copies."""
    entities_df = entities_df.copy()
    relationships_df = relationships_df.copy()

    if len(relationships_df) == 0 or len(entities_df) == 0:
        return entities_df, relationships_df

    degree_counts: dict[str, int] = {}
    for _, row in relationships_df.iterrows():
        degree_counts[row["source"]] = degree_counts.get(row["source"], 0) + 1
        degree_counts[row["target"]] = degree_counts.get(row["target"], 0) + 1

    entities_df["degree"] = entities_df["title"].map(lambda t: degree_counts.get(t, 0))

    def _combined(row):
        return degree_counts.get(row["source"], 0) + degree_counts.get(row["target"], 0)

    relationships_df["combined_degree"] = relationships_df.apply(_combined, axis=1)
    return entities_df, relationships_df


# --------------------------------------------------------------------------- #
# Leiden clustering — delegated to graphrag's own function
# --------------------------------------------------------------------------- #

def run_leiden_clustering(
    relationships_df: pd.DataFrame, *, max_cluster_size: int = 10, seed: int = 42
):
    """Call graphrag's cluster_graph() on our edges. Returns list of
    (level, cluster_id, parent, node_titles) tuples."""
    from graphrag.index.operations.cluster_graph import cluster_graph
    if len(relationships_df) == 0:
        return []
    return cluster_graph(
        edges=relationships_df[["source", "target", "weight"]].copy(),
        max_cluster_size=max_cluster_size,
        use_lcc=False,
        seed=seed,
    )


def build_communities_dataframe(
    leiden_result, entities_df: pd.DataFrame, relationships_df: pd.DataFrame
) -> pd.DataFrame:
    """Schema: id, human_readable_id, community, level, parent, children,
    title, entity_ids, relationship_ids, text_unit_ids, period, size."""
    if not leiden_result:
        return pd.DataFrame(columns=[
            "id", "human_readable_id", "community", "level", "parent", "children",
            "title", "entity_ids", "relationship_ids", "text_unit_ids", "period", "size",
        ])

    title_to_id = dict(zip(entities_df["title"], entities_df["id"])) if len(entities_df) else {}

    # Build reverse maps for relationships
    rel_by_pair: dict[tuple[str, str], str] = {}
    rel_tus: dict[tuple[str, str], list] = {}
    if len(relationships_df):
        for _, row in relationships_df.iterrows():
            rel_by_pair[(row["source"], row["target"])] = row["id"]
            rel_tus[(row["source"], row["target"])] = list(row.get("text_unit_ids", []))

    ent_tus = dict(zip(entities_df["title"], entities_df["text_unit_ids"])) if len(entities_df) else {}

    # children map: community_id → list of child community_ids
    children_by_parent: dict[int, list[int]] = {}
    for (level, cluster_id, parent, _nodes) in leiden_result:
        if parent >= 0:
            children_by_parent.setdefault(parent, []).append(cluster_id)

    rows: list[dict] = []
    for i, (level, cluster_id, parent, node_titles) in enumerate(leiden_result):
        entity_ids = [title_to_id[t] for t in node_titles if t in title_to_id]
        rel_ids: list[str] = []
        text_unit_ids: list = []
        title_set = set(node_titles)
        for (src, tgt), rid in rel_by_pair.items():
            if src in title_set and tgt in title_set:
                rel_ids.append(rid)
                text_unit_ids.extend(rel_tus.get((src, tgt), []))
        for t in node_titles:
            text_unit_ids.extend(ent_tus.get(t, []))
        # Deduplicate text unit ids preserving insertion order
        seen = set()
        unique_tus = []
        for tu in text_unit_ids:
            if tu not in seen:
                seen.add(tu)
                unique_tus.append(tu)

        rows.append({
            "id": _stable_id("community", str(level), str(cluster_id)),
            "human_readable_id": i,
            "community": int(cluster_id),
            "level": int(level),
            "parent": int(parent),
            "children": children_by_parent.get(cluster_id, []),
            "title": f"Community {cluster_id}",
            "entity_ids": entity_ids,
            "relationship_ids": rel_ids,
            "text_unit_ids": unique_tus,
            "period": "",
            "size": len(entity_ids),
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Community reports
# --------------------------------------------------------------------------- #

def parse_community_report_output(text: str) -> dict | None:
    """Extract the JSON body from a community-report agent response."""
    if not text:
        return None
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    start = t.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(t)):
        c = t[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def build_community_reports_dataframe(
    raw_reports: dict[int, dict], communities_df: pd.DataFrame
) -> pd.DataFrame:
    """Schema: id, human_readable_id, community, level, parent, children, title,
    summary, full_content, rank, rating_explanation, findings, full_content_json,
    period, size."""
    if len(communities_df) == 0:
        return pd.DataFrame(columns=[
            "id", "human_readable_id", "community", "level", "parent", "children",
            "title", "summary", "full_content", "rank", "rating_explanation",
            "findings", "full_content_json", "period", "size",
        ])

    rows: list[dict] = []
    for i, (_, c) in enumerate(communities_df.iterrows()):
        cid = int(c["community"])
        report = raw_reports.get(cid, {})
        findings = report.get("findings", []) or []
        full_content = _render_report_full_content(report)
        rows.append({
            "id": _stable_id("report", str(c["level"]), str(cid)),
            "human_readable_id": i,
            "community": cid,
            "level": int(c["level"]),
            "parent": int(c["parent"]),
            "children": c.get("children", []),
            "title": report.get("title", c.get("title", f"Community {cid}")),
            "summary": report.get("summary", ""),
            "full_content": full_content,
            "rank": float(report.get("rating", 0.0)),
            "rating_explanation": report.get("rating_explanation", ""),
            "findings": findings,
            "full_content_json": json.dumps(report) if report else "",
            "period": c.get("period", ""),
            "size": int(c["size"]),
        })
    return pd.DataFrame(rows)


def _render_report_full_content(report: dict) -> str:
    if not report:
        return ""
    parts = [
        f"# {report.get('title', '')}",
        "",
        report.get("summary", ""),
        "",
    ]
    for f in report.get("findings", []) or []:
        parts.append(f"## {f.get('summary', '')}")
        parts.append(f.get("explanation", ""))
        parts.append("")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Batching
# --------------------------------------------------------------------------- #

def batch_text_units(tu_df: pd.DataFrame, *, batch_size: int) -> Iterator[list[dict]]:
    """Yield batches of text unit rows as lists of dicts (id, text)."""
    if len(tu_df) == 0:
        return
    rows = tu_df[["id", "text"]].to_dict(orient="records")
    for i in range(0, len(rows), batch_size):
        yield rows[i : i + batch_size]


# --------------------------------------------------------------------------- #
# Workspace assembly
# --------------------------------------------------------------------------- #

def build_extract_prompt(text_units: list[dict]) -> str:
    """Render the prompt passed to an Agent for extract_graph batch work.

    Embeds graphrag's pipe-delimited tuple format contract and asks the
    agent to produce one block per text unit, separated by `===TU:<id>===`
    markers so the response is trivially parseable.
    """
    blocks = []
    for tu in text_units:
        blocks.append(f"### Text Unit {tu['id']}\n\n{tu['text']}")
    joined = "\n\n---\n\n".join(blocks)

    entity_types = "person, organization, concept, tool, file, project, decision, skill, topic"

    return f"""You are performing entity and relationship extraction on a batch of conversation text units from a personal AI assistant session corpus. The output format is strict and machine-parsed — follow it exactly.

For EACH text unit below, produce an extraction block. Between blocks, output a delimiter on its own line: `===TU:<text_unit_id>===`. Start the very first block with the delimiter too.

Within each block, use this format (copied verbatim from Microsoft GraphRAG's extract_graph prompt):

Entity types allowed: [{entity_types}]

For each entity, output:
("entity"<|>ENTITY_NAME<|>ENTITY_TYPE<|>short description of the entity's attributes and activities)

For each pair of clearly related entities, output:
("relationship"<|>SOURCE_ENTITY<|>TARGET_ENTITY<|>why they are related<|>numeric_weight_1_to_10)

Separate records with `##` on its own line. End each block with `<|COMPLETE|>` on its own line.

Entity names must be UPPERCASE. Weights must be integers 1–10 where 10 is strongest. Skip text units that have no extractable entities (just emit the delimiter and `<|COMPLETE|>`).

Return ONLY the extraction blocks — no prose, no summaries, no explanations, no code fences. Do not use tools — just output the text directly in your final message.

==========
TEXT UNITS
==========

{joined}

==========

Remember: start with `===TU:<first_id>===`, then the block, then `===TU:<next_id>===`, etc. End each block with `<|COMPLETE|>`. No extra text anywhere.
"""


def parse_agent_extract_response(response: str) -> dict[str, str]:
    """Split an agent's multi-block extract response into {text_unit_id: block_text}."""
    if not response:
        return {}
    blocks: dict[str, str] = {}
    # Split on the TU marker. Keep the captured id alongside the block body.
    parts = re.split(r"===TU:([a-zA-Z0-9_\-]+)===", response)
    # parts = [prose_before_first, id1, body1, id2, body2, ...]
    for i in range(1, len(parts) - 1, 2):
        tu_id = parts[i].strip()
        body = parts[i + 1].strip()
        if tu_id and body:
            blocks[tu_id] = body
    return blocks


def build_community_report_prompt(
    *,
    community_id: int,
    entities: list[dict],
    relationships: list[dict],
    max_report_length: int = 2000,
) -> str:
    """Render a prompt that asks the agent to produce a single community report
    in JSON, matching the shape of graphrag's community_report_graph.txt output.
    """
    ent_lines = ["human_readable_id,title,description"]
    for e in entities:
        desc = str(e.get("description", "")).replace("\n", " ").replace(",", ";")
        ent_lines.append(f"{e.get('human_readable_id', 0)},{e['title']},{desc[:200]}")
    ent_csv = "\n".join(ent_lines)

    rel_lines = ["human_readable_id,source,target,description"]
    for r in relationships:
        desc = str(r.get("description", "")).replace("\n", " ").replace(",", ";")
        rel_lines.append(
            f"{r.get('human_readable_id', 0)},{r['source']},{r['target']},{desc[:200]}"
        )
    rel_csv = "\n".join(rel_lines)

    return f"""You are writing an information-discovery community report. Produce ONLY a valid JSON object (no code fences, no prose, no preamble) matching this shape exactly:

{{
    "title": "<short specific title with representative entities>",
    "summary": "<executive summary paragraph>",
    "rating": <float 0.0-10.0, severity/importance of this community>,
    "rating_explanation": "<one sentence>",
    "findings": [
        {{"summary": "<short insight title>", "explanation": "<multi-paragraph detail>"}},
        ...5-10 findings total...
    ]
}}

Keep the total output under {max_report_length} words. Ground statements in the provided entities and relationships only — do not invent facts.

==========
Community id: {community_id}

Entities
--------
{ent_csv}

Relationships
-------------
{rel_csv}
==========

Output JSON only.
"""


def assemble_workspace(
    graph_dir: Path,
    *,
    entities_df: pd.DataFrame,
    relationships_df: pd.DataFrame,
    communities_df: pd.DataFrame,
    community_reports_df: pd.DataFrame,
) -> None:
    """Write the four parquet files into graph_dir/output/."""
    output_dir = Path(graph_dir).expanduser() / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    entities_df.to_parquet(output_dir / "entities.parquet")
    relationships_df.to_parquet(output_dir / "relationships.parquet")
    communities_df.to_parquet(output_dir / "communities.parquet")
    community_reports_df.to_parquet(output_dir / "community_reports.parquet")
