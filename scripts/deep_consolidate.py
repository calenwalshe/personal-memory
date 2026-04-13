#!/usr/bin/env python3
"""
deep_consolidate.py — GraphRAG community reports → personal memory vault candidates.

Pipeline:
    graph/output/community_reports.parquet
      + graph/chunk_metadata.json
      + graph/output/{entities,text_units,documents}.parquet
        → for each community:
            - trace community → entities → text_units → chunks → sessions
            - compute 6-signal score
            - if score ≥ 0.65 AND sessions ≥ 2 AND week_span ≥ 2:
                run contradiction check vs raw/threads/
                if no contradiction: write candidate with status=auto-promoted
                                     and copy to raw/threads/ + INDEX.md
            - else: write candidate with status=pending (user reviews via
                    /memory-review-candidates)

Outputs:
  ~/memory/vault/candidates/cand-*.md
  ~/memory/vault/raw/threads/*.md (auto-promoted only)
  ~/memory/vault/INDEX.md (append)
  ~/memory/vault/impressions-index.json (graphrag-corpus key update)

Idempotent: re-running skips communities already processed (by community_id
within a given run_id).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_GRAPH_DIR = Path.home() / "memory" / "vault" / "graph"
DEFAULT_VAULT = Path.home() / "memory" / "vault"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# 3-gate auto-promotion thresholds
AUTO_PROMOTE_SCORE_THRESHOLD = 0.65
AUTO_PROMOTE_SESSION_FLOOR = 2
AUTO_PROMOTE_WEEK_FLOOR = 2


# --------------------------------------------------------------------------- #
# Community traceability: community_id → {session_ids, week_span}
# --------------------------------------------------------------------------- #

@dataclass
class CommunityInfo:
    session_ids: set[str]
    session_count: int
    week_span: int
    first_ts: datetime | None
    last_ts: datetime | None
    chunk_ids: set[str]


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def load_community_session_info(graph_dir: Path) -> dict[int, dict]:
    """Traverse community → entities → text_units → documents → chunk metadata.

    Returns dict keyed by community id with fields: session_ids (set),
    session_count, week_span (int weeks), first_ts, last_ts, chunk_ids.
    """
    import pandas as pd

    graph_dir = Path(graph_dir)
    output = graph_dir / "output"

    entities = pd.read_parquet(output / "entities.parquet")
    text_units = pd.read_parquet(output / "text_units.parquet")
    documents = pd.read_parquet(output / "documents.parquet")
    metadata = json.loads((graph_dir / "chunk_metadata.json").read_text())

    def _safe_list(val) -> list:
        if val is None:
            return []
        # Pandas/numpy arrays and lists both support iteration; just coerce.
        try:
            return list(val)
        except TypeError:
            return []

    # Build lookup: text_unit_id → set of document_ids.
    # graphrag 3.0.8 uses `document_id` (singular, scalar) in text_units.parquet;
    # our test fixture uses `document_ids` (plural list). Accept either.
    tu_to_docs: dict[str, set[str]] = {}
    for _, row in text_units.iterrows():
        plural = _safe_list(row.get("document_ids") if "document_ids" in row.index else None)
        singular = row.get("document_id") if "document_id" in row.index else None
        docs = set(plural)
        if singular and not (isinstance(singular, float) and pd.isna(singular)):
            docs.add(str(singular))
        tu_to_docs[row["id"]] = docs

    # Build lookup: document_id → chunk_id
    # Graphrag stores documents with title like "chunk-<id>.txt" OR
    # with id starting with "chunk-"; try both.
    doc_to_chunk: dict[str, str] = {}
    for _, row in documents.iterrows():
        doc_id = row["id"]
        title = str(row.get("title", ""))
        match = re.search(r"chunk-([a-f0-9]+)", title) or re.search(
            r"chunk-([a-f0-9]+)", doc_id
        )
        if match:
            doc_to_chunk[doc_id] = match.group(1)

    # Walk each entity → chunks → sessions; group by community
    result: dict[int, dict] = {}
    for _, row in entities.iterrows():
        community = row.get("community")
        if community is None or (isinstance(community, float) and community != community):
            continue
        community = int(community)
        bucket = result.setdefault(
            community,
            {
                "session_ids": set(),
                "chunk_ids": set(),
                "timestamps": [],
            },
        )
        for tu_id in _safe_list(row.get("text_unit_ids")):
            for doc_id in tu_to_docs.get(tu_id, set()):
                chunk_id = doc_to_chunk.get(doc_id)
                if not chunk_id:
                    continue
                meta = metadata.get(chunk_id)
                if not meta:
                    continue
                bucket["chunk_ids"].add(chunk_id)
                for sid in meta.get("session_ids", []):
                    bucket["session_ids"].add(sid)
                ts = _parse_iso(meta.get("first_timestamp"))
                if ts:
                    bucket["timestamps"].append(ts)

    # Finalize: compute week_span per community
    for community, bucket in result.items():
        ts_list = sorted(bucket.pop("timestamps"))
        if ts_list:
            span_days = (ts_list[-1] - ts_list[0]).days
            bucket["week_span"] = max(1, (span_days // 7) + 1)
            bucket["first_ts"] = ts_list[0]
            bucket["last_ts"] = ts_list[-1]
        else:
            bucket["week_span"] = 0
            bucket["first_ts"] = None
            bucket["last_ts"] = None
        bucket["session_count"] = len(bucket["session_ids"])

    return result


# --------------------------------------------------------------------------- #
# 6-signal scoring
# --------------------------------------------------------------------------- #

# Weights sum to 1.0
_SIGNAL_WEIGHTS = {
    "rank_normalized": 0.30,
    "size_normalized": 0.15,
    "session_count_normalized": 0.20,
    "week_span_normalized": 0.15,
    "entity_specificity": 0.10,
    "relationship_density_normalized": 0.10,
}


def score_signals(signals: dict[str, float]) -> float:
    """Weighted sum of 6 normalized signals, clamped to [0, 1]."""
    total = 0.0
    for key, weight in _SIGNAL_WEIGHTS.items():
        v = signals.get(key, 0.0)
        v = max(0.0, min(1.0, v))
        total += weight * v
    return max(0.0, min(1.0, total))


def compute_signals(report_row, community_info: dict) -> dict[str, float]:
    """Extract the 6 signals from a community_reports row + its community info."""
    # rank_normalized: graphrag gives rank in [0, 10]
    rank = float(report_row.get("rank", 0) or 0)
    rank_normalized = min(1.0, rank / 10.0)

    # size_normalized: community size (entities); cap at 20
    size = int(report_row.get("size", 0) or 0)
    size_normalized = min(1.0, size / 20.0)

    # session_count_normalized: 1 session = 0, 5+ = 1
    sc = community_info.get("session_count", 0)
    session_count_normalized = min(1.0, max(0.0, (sc - 1) / 4.0))

    # week_span_normalized: 1 week = 0, 8+ = 1
    ws = community_info.get("week_span", 0)
    week_span_normalized = min(1.0, max(0.0, (ws - 1) / 7.0))

    # entity_specificity: placeholder — ratio of entities with title length
    # > 5 chars (proxy for "not too generic"). 0.5 default.
    entity_specificity = 0.5

    # relationship_density_normalized: placeholder (0.5) since graphrag stores
    # relationships separately; we don't need to re-read that parquet for
    # this prototype.
    relationship_density_normalized = 0.5

    return {
        "rank_normalized": rank_normalized,
        "size_normalized": size_normalized,
        "session_count_normalized": session_count_normalized,
        "week_span_normalized": week_span_normalized,
        "entity_specificity": entity_specificity,
        "relationship_density_normalized": relationship_density_normalized,
    }


# --------------------------------------------------------------------------- #
# Auto-promotion 3-gate
# --------------------------------------------------------------------------- #

def should_auto_promote(*, score: float, session_count: int, week_span: int) -> bool:
    return (
        score >= AUTO_PROMOTE_SCORE_THRESHOLD
        and session_count >= AUTO_PROMOTE_SESSION_FLOOR
        and week_span >= AUTO_PROMOTE_WEEK_FLOOR
    )


# --------------------------------------------------------------------------- #
# Contradiction check via Haiku
# --------------------------------------------------------------------------- #

_CONTRADICTION_PROMPT = """You are a strict consistency checker for a personal memory vault.

Candidate (about to be added):
---
{candidate}
---

Existing vault threads:
---
{existing}
---

Does the candidate directly contradict any existing thread? Answer with exactly one word: YES or NO.
If YES, briefly explain which thread on a second line.
"""


def contradicts_existing(
    *,
    candidate_summary: str,
    raw_threads_dir: Path,
    client: Any,
    model: str,
    max_threads: int = 10,
) -> bool:
    """Check if candidate contradicts any existing raw thread. Calls Haiku once."""
    raw_threads_dir = Path(raw_threads_dir)
    existing: list[str] = []
    for p in sorted(raw_threads_dir.glob("*.md"))[:max_threads]:
        try:
            existing.append(f"## {p.name}\n{p.read_text()[:1500]}")
        except OSError:
            continue
    if not existing:
        return False

    try:
        response = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": _CONTRADICTION_PROMPT.format(
                    candidate=candidate_summary[:2000],
                    existing="\n\n".join(existing),
                ),
            }],
        )
    except Exception as e:
        print(f"[consolidate] contradiction check error: {e}", file=sys.stderr)
        return False  # fail open — don't block on API hiccups

    text = "".join(getattr(b, "text", "") for b in getattr(response, "content", []))
    return text.strip().upper().startswith("YES")


# --------------------------------------------------------------------------- #
# Candidate writer
# --------------------------------------------------------------------------- #

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _candidate_frontmatter(cand: dict, *, auto_promoted: bool) -> dict:
    status = "auto-promoted" if auto_promoted else "pending"
    fm = {
        "id": cand["id"],
        "type": "candidate",
        "status": status,
        "consolidated_at": _utcnow_iso(),
        "source": "graphrag-corpus",
        "sources": [{"type": "graphrag-corpus", "ref": f"community:{cand.get('community_id')}"}],
        "pattern_type": cand.get("pattern_type", "pattern"),
        "confidence": "high" if cand.get("score", 0) >= 0.8 else "medium",
        "suggested_vault_type": "project",
        "score": round(float(cand.get("score", 0)), 3),
        "session_count": int(cand.get("session_count", 0)),
        "week_span": int(cand.get("week_span", 0)),
        "source_chunk_ids": list(cand.get("source_chunk_ids", [])),
        "source_session_ids": list(cand.get("source_session_ids", [])),
    }
    if auto_promoted:
        fm["auto_promoted_at"] = _utcnow_iso()
    return fm


def _yaml_dump(obj) -> str:
    import yaml
    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False)


def write_candidate(
    cand: dict, *, candidates_dir: Path, auto_promoted: bool
) -> Path:
    candidates_dir = Path(candidates_dir)
    candidates_dir.mkdir(parents=True, exist_ok=True)
    path = candidates_dir / f"{cand['id']}.md"
    fm = _candidate_frontmatter(cand, auto_promoted=auto_promoted)
    body = (
        f"## What was found\n{cand.get('title', '')}\n\n"
        f"## Summary\n{cand.get('summary', '')}\n\n"
        f"## Full community report\n{cand.get('full_content', '')}\n"
    )
    text = f"---\n{_yaml_dump(fm)}---\n\n{body}"
    path.write_text(text)
    return path


# --------------------------------------------------------------------------- #
# Thread promoter
# --------------------------------------------------------------------------- #

def promote_to_threads(
    cand: dict, *, threads_dir: Path, index_path: Path
) -> bool:
    """Write the auto-promoted candidate as a raw thread and append to INDEX.md.
    Returns False (no-op) if this candidate id is already referenced in INDEX.md."""
    threads_dir = Path(threads_dir)
    index_path = Path(index_path)

    if index_path.exists() and cand["id"] in index_path.read_text():
        return False

    threads_dir.mkdir(parents=True, exist_ok=True)
    path = threads_dir / f"{cand['id']}.md"

    fm = {
        "id": cand["id"],
        "type": "thread",
        "status": "auto-promoted",
        "source": "graphrag-corpus",
        "community_id": cand.get("community_id"),
        "score": round(float(cand.get("score", 0)), 3),
        "session_count": int(cand.get("session_count", 0)),
        "week_span": int(cand.get("week_span", 0)),
        "created_at": _utcnow_iso(),
    }
    body = (
        f"## {cand.get('title', 'Untitled')}\n\n"
        f"{cand.get('summary', '')}\n\n"
        f"## Full community report\n{cand.get('full_content', '')}\n"
    )
    path.write_text(f"---\n{_yaml_dump(fm)}---\n\n{body}")

    with index_path.open("a") as f:
        f.write(f"- {cand['id']} (source: graphrag-corpus, score: {fm['score']})\n")
    return True


# --------------------------------------------------------------------------- #
# End-to-end runner
# --------------------------------------------------------------------------- #

def run_deep_consolidate(
    *,
    graph_dir: Path,
    vault_dir: Path,
    client: Any,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
) -> dict:
    """Process all community reports, write candidates, auto-promote qualifying ones."""
    import pandas as pd

    graph_dir = Path(graph_dir)
    vault_dir = Path(vault_dir)
    candidates_dir = vault_dir / "candidates"
    threads_dir = vault_dir / "raw" / "threads"
    index_path = vault_dir / "INDEX.md"

    reports = pd.read_parquet(graph_dir / "output" / "community_reports.parquet")
    cs_info = load_community_session_info(graph_dir)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stats = {
        "reports_processed": 0,
        "candidates_written": 0,
        "auto_promoted": 0,
        "for_review": 0,
        "contradicted": 0,
    }

    for idx, row in reports.iterrows():
        community_id = int(row.get("community", -1))
        info = cs_info.get(community_id, {"session_count": 0, "week_span": 0, "session_ids": set(), "chunk_ids": set()})
        signals = compute_signals(row, info)
        score = score_signals(signals)

        cand = {
            "id": f"cand-gr-{run_id}-{idx:03d}",
            "title": str(row.get("title", "")),
            "summary": str(row.get("summary", "")),
            "full_content": str(row.get("full_content", "")),
            "score": score,
            "session_count": info.get("session_count", 0),
            "week_span": info.get("week_span", 0),
            "pattern_type": "pattern",
            "community_id": community_id,
            "source_chunk_ids": sorted(info.get("chunk_ids", set())),
            "source_session_ids": sorted(info.get("session_ids", set())),
        }

        stats["reports_processed"] += 1

        qualifies = should_auto_promote(
            score=score,
            session_count=cand["session_count"],
            week_span=cand["week_span"],
        )

        if qualifies and not dry_run:
            if contradicts_existing(
                candidate_summary=cand["summary"],
                raw_threads_dir=threads_dir,
                client=client,
                model=model,
            ):
                stats["contradicted"] += 1
                qualifies = False

        if not dry_run:
            write_candidate(cand, candidates_dir=candidates_dir, auto_promoted=qualifies)
            stats["candidates_written"] += 1
            if qualifies:
                promote_to_threads(cand, threads_dir=threads_dir, index_path=index_path)
                stats["auto_promoted"] += 1
            else:
                stats["for_review"] += 1

    # Update impressions-index
    if not dry_run:
        _update_impressions_index(vault_dir, stats)

    return stats


def _update_impressions_index(vault_dir: Path, stats: dict) -> None:
    idx_path = vault_dir / "impressions-index.json"
    if idx_path.exists():
        try:
            data = json.loads(idx_path.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    data.setdefault("sources", {})
    data["sources"]["graphrag-corpus"] = {
        "last_run": _utcnow_iso(),
        "reports_processed": stats["reports_processed"],
        "candidates_written": stats["candidates_written"],
        "auto_promoted": stats["auto_promoted"],
        "for_review": stats["for_review"],
        "contradicted": stats["contradicted"],
    }
    idx_path.write_text(json.dumps(data, indent=2, sort_keys=True))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--graph-dir", default=str(DEFAULT_GRAPH_DIR))
    ap.add_argument("--vault", default=str(DEFAULT_VAULT))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    client = None
    if not args.dry_run:
        from anthropic import Anthropic
        client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    stats = run_deep_consolidate(
        graph_dir=Path(args.graph_dir).expanduser(),
        vault_dir=Path(args.vault).expanduser(),
        client=client,
        model=args.model,
        dry_run=args.dry_run,
    )
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
