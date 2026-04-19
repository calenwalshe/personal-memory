"""
entity_resolver.py — One-time and incremental entity normalization for L2.

Converts L1's free-form entity strings into the canonical entity registry in graph.db.

Two-pass approach:
  Pass 1 (automated, no LLM):
    - Load all unique entity strings from atoms.db
    - Case-fold exact duplicates
    - Embed with all-MiniLM-L6-v2 and cluster by cosine > 0.85
    - Assign canonical names by frequency + heuristics

  Pass 2 (LLM confirmation, ~17 Haiku calls for ~850 strings):
    - Send candidate clusters to Haiku for name + type confirmation
    - Haiku can merge clusters or retype them

Also handles interest tag normalization (single Haiku call).

Usage:
    python3 entity_resolver.py [--dry-run] [--skip-llm] [--batch-size 50]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
ATOMS_DB = VAULT / "atoms.db"
GRAPH_DB = VAULT / "graph.db"

_NVM_BIN = Path.home() / ".nvm/versions/node/v20.20.0/bin"
CLAUDE_BIN = (
    os.environ.get("CLAUDE_BIN")
    or shutil.which("claude")
    or str(_NVM_BIN / "claude")
)

EMBED_MODEL = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")
EMBED_DIM = 384
CLUSTER_THRESHOLD = 0.85   # cosine similarity to merge entity strings
BATCH_SIZE = 50            # entity clusters per Haiku call
HAIKU_TIMEOUT = 90
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Project names from atoms.db — used for heuristic typing
_PROJECT_NAMES: set[str] = set()

# Heuristic entity type rules
def _infer_type(name: str) -> str:
    """Heuristic entity type from name pattern."""
    lower = name.lower()
    # File paths
    if "/" in name or name.startswith(".") or name.endswith((".py", ".sh", ".md", ".json", ".yaml", ".yml", ".js", ".ts", ".toml")):
        return "file"
    # Known tools and services
    tools = {"bash", "python", "python3", "node", "npm", "git", "docker", "caddy",
              "liquidsoap", "ffmpeg", "icecast", "nginx", "redis", "sqlite", "faiss",
              "xdotool", "xclip", "novnc", "vnc", "ssh", "curl", "wget", "jq",
              "claude", "haiku", "claude-code", "mcp", "twilio", "telegram"}
    if lower in tools or any(lower.startswith(t) for t in tools):
        return "tool"
    # Known services / platforms
    services = {"youtube", "chatgpt", "openai", "anthropic", "cloudflare", "github",
                "google", "perplexity", "tavily", "discord", "slack", "stripe",
                "vultr", "digitalocean", "aws", "gcp", "azure", "spotify", "discogs"}
    if lower in services or any(s in lower for s in services):
        return "service"
    # IPs and URLs
    if re.match(r"\d+\.\d+\.\d+\.\d+", name) or name.startswith("http"):
        return "service"
    # If in project names
    if name in _PROJECT_NAMES or lower in _PROJECT_NAMES:
        return "project"
    # Default
    return "concept"


def _load_model():
    import io, contextlib, logging
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")
    os.environ.setdefault("TQDM_DISABLE", "1")
    logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
    logging.getLogger("transformers").setLevel(logging.ERROR)
    from sentence_transformers import SentenceTransformer
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        model = SentenceTransformer(EMBED_MODEL)
    return model


def load_all_entities_from_atoms() -> dict[str, list[str]]:
    """
    Load all entity strings from atoms.db.
    Returns {raw_entity_string: [atom_id, ...]}
    """
    if not ATOMS_DB.exists():
        print("atoms.db not found", file=sys.stderr)
        return {}

    conn = sqlite3.connect(str(ATOMS_DB))
    rows = conn.execute(
        "SELECT id, entities FROM atoms WHERE invalidated_by IS NULL AND entities IS NOT NULL"
    ).fetchall()
    # Also load project names for heuristic typing
    projects = conn.execute("SELECT DISTINCT project FROM atoms").fetchall()
    conn.close()

    global _PROJECT_NAMES
    _PROJECT_NAMES = {r[0] for r in projects}

    entity_to_atoms: dict[str, list[str]] = defaultdict(list)
    for atom_id, entities_json in rows:
        try:
            ents = json.loads(entities_json)
        except (json.JSONDecodeError, TypeError):
            continue
        for e in ents:
            e = e.strip()
            if e:
                entity_to_atoms[e].append(atom_id)

    return dict(entity_to_atoms)


def load_all_interest_tags_from_atoms() -> dict[str, list[str]]:
    """
    Load all interest_tags from atoms.db (interest_signal=1 only).
    Returns {raw_tag: [atom_id, ...]}
    """
    if not ATOMS_DB.exists():
        return {}

    conn = sqlite3.connect(str(ATOMS_DB))
    rows = conn.execute(
        "SELECT id, interest_tags FROM atoms WHERE invalidated_by IS NULL "
        "AND interest_signal=1 AND interest_tags IS NOT NULL"
    ).fetchall()
    conn.close()

    tag_to_atoms: dict[str, list[str]] = defaultdict(list)
    for atom_id, tags_json in rows:
        try:
            tags = json.loads(tags_json)
        except (json.JSONDecodeError, TypeError):
            continue
        for t in tags:
            t = t.strip()
            if t:
                tag_to_atoms[t].append(atom_id)

    return dict(tag_to_atoms)


def pass1_cluster(
    entity_to_atoms: dict[str, list[str]],
    model,
    threshold: float = CLUSTER_THRESHOLD,
    verbose: bool = True,
) -> list[dict]:
    """
    Pass 1: Automated clustering.

    Returns list of candidate clusters:
    [{
        "strings": ["FFmpeg", "ffmpeg", "FFmpeg filter"],
        "frequencies": {"FFmpeg": 4, "ffmpeg": 2, "FFmpeg filter": 1},
        "atom_ids": ["uuid1", "uuid2", ...],
        "candidate_canonical": "FFmpeg",
        "candidate_type": "tool",
    }]
    """
    all_strings = list(entity_to_atoms.keys())
    freq = Counter({s: len(v) for s, v in entity_to_atoms.items()})

    if verbose:
        print(f"Pass 1: {len(all_strings)} unique entity strings")

    # Step 1: Case-fold exact matches first
    case_groups: dict[str, list[str]] = defaultdict(list)
    for s in all_strings:
        case_groups[s.lower()].append(s)

    # Build initial clusters from case-fold groups
    clusters: list[list[str]] = []
    remaining: list[str] = []
    for lower, variants in case_groups.items():
        if len(variants) > 1:
            clusters.append(variants)
        else:
            remaining.append(variants[0])

    if verbose:
        print(f"  Case-fold groups: {len(clusters)} merged, {len(remaining)} remaining")

    # Step 2: Embed remaining strings
    if remaining:
        if verbose:
            print(f"  Embedding {len(remaining)} strings...")
        embeddings = model.encode(remaining, normalize_embeddings=True, show_progress_bar=False).astype("float32")

        # Step 3: Greedy cosine clustering
        n = len(remaining)
        assigned = [False] * n

        for i in range(n):
            if assigned[i]:
                continue
            cluster = [remaining[i]]
            assigned[i] = True
            for j in range(i + 1, n):
                if assigned[j]:
                    continue
                sim = float(np.dot(embeddings[i], embeddings[j]))
                if sim >= threshold:
                    cluster.append(remaining[j])
                    assigned[j] = True
            clusters.append(cluster)

        if verbose:
            multi = sum(1 for c in clusters if len(c) > 1)
            print(f"  After embedding clustering: {len(clusters)} clusters, {multi} multi-string")

    # Step 4: Pick canonical name and infer type
    result = []
    for cluster in clusters:
        # Pick most frequent as canonical
        canonical = max(cluster, key=lambda s: freq.get(s, 0))
        all_atom_ids = []
        for s in cluster:
            all_atom_ids.extend(entity_to_atoms.get(s, []))
        all_atom_ids = list(set(all_atom_ids))

        result.append({
            "strings": cluster,
            "frequencies": {s: freq.get(s, 0) for s in cluster},
            "atom_ids": all_atom_ids,
            "candidate_canonical": canonical,
            "candidate_type": _infer_type(canonical),
        })

    return result


def _call_haiku(prompt: str, timeout: int = HAIKU_TIMEOUT) -> str:
    """Call Haiku via claude -p. Returns raw stdout."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", HAIKU_MODEL, prompt],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"  Haiku call failed: {e}", file=sys.stderr)
        return ""


def _parse_json_response(raw: str) -> list | dict | None:
    raw = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    # Try to find JSON array or object
    m = re.search(r"(\[.*\]|\{.*\})", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


ENTITY_CLUSTER_PROMPT = """You are normalizing entity mentions from a personal memory system.

For each numbered cluster of similar entity strings, confirm or adjust:
1. canonical_name: the best display name (keep existing capitalization conventions)
2. entity_type: exactly one of: tool, project, service, person, concept, file, place
3. merge: if this cluster should be merged with another cluster (provide cluster number), else null

Entity type guidance:
- tool: CLI tools, libraries, scripts (FFmpeg, Liquidsoap, Bash, FAISS)
- project: named software projects or repos (yt_dj, openclaw-fresh, personal-memory)
- service: hosted platforms or APIs (YouTube, ChatGPT, Cloudflare, GitHub)
- person: human names
- file: file paths or specific config/code files (CLAUDE.md, atoms.db)
- place: physical locations, countries, cities
- concept: abstract ideas, techniques, patterns (label propagation, SCAPE theory)

CLUSTERS:
"""

INTEREST_TAG_PROMPT = """You are organizing personal interest tags into a coherent taxonomy.

Below are interest tags extracted from a personal memory system, with their frequency counts.
Group them into 12-18 canonical interest areas.

Rules:
- Each area should have a canonical_tag (kebab-case, concise) and display_name (Title Case)
- Each area should have a 1-sentence description of what it covers
- List all source tags it absorbs in "absorbs"
- Err toward fewer, broader areas over many narrow ones

Tags (frequency):
"""


def pass2_llm_normalize(
    clusters: list[dict],
    batch_size: int = BATCH_SIZE,
    verbose: bool = True,
    dry_run: bool = False,
) -> list[dict]:
    """
    Pass 2: LLM confirmation and typing of entity clusters.
    Sends batches to Haiku. Updates candidate_canonical and candidate_type.
    Returns updated clusters.
    """
    if verbose:
        print(f"\nPass 2 (LLM): {len(clusters)} clusters, batch_size={batch_size}")
        if dry_run:
            print("  [dry-run] Would send clusters to Haiku")
            return clusters

    total_batches = (len(clusters) + batch_size - 1) // batch_size
    updated = 0

    for batch_start in range(0, len(clusters), batch_size):
        batch = clusters[batch_start:batch_start + batch_size]
        batch_num = batch_start // batch_size + 1

        # Format batch for Haiku
        lines = []
        for i, cluster in enumerate(batch):
            global_idx = batch_start + i
            strings_with_freq = ", ".join(
                f'"{s}" ({cluster["frequencies"].get(s, 0)}x)'
                for s in cluster["strings"]
            )
            lines.append(
                f"[{global_idx}] candidate: {cluster['candidate_canonical']!r} "
                f"({cluster['candidate_type']}) — {strings_with_freq}"
            )

        prompt = ENTITY_CLUSTER_PROMPT + "\n".join(lines) + (
            "\n\nReturn a JSON array matching the input order:\n"
            '[{"cluster": 0, "canonical_name": "FFmpeg", "entity_type": "tool", "merge_with": null}, ...]'
        )

        if verbose:
            print(f"  Batch {batch_num}/{total_batches} ({len(batch)} clusters)...", end=" ", flush=True)

        raw = _call_haiku(prompt)
        result = _parse_json_response(raw)

        if not result or not isinstance(result, list):
            print(f"FAILED (no JSON)", file=sys.stderr)
            continue

        # Apply results
        applied = 0
        for item in result:
            idx = item.get("cluster")
            if idx is None or idx >= len(clusters):
                continue
            clusters[idx]["candidate_canonical"] = item.get("canonical_name", clusters[idx]["candidate_canonical"])
            clusters[idx]["candidate_type"] = item.get("entity_type", clusters[idx]["candidate_type"])
            clusters[idx]["merge_with"] = item.get("merge_with")
            applied += 1
            updated += 1

        if verbose:
            print(f"applied {applied}/{len(batch)}")

        if batch_start + batch_size < len(clusters):
            time.sleep(0.5)

    if verbose:
        print(f"\nPass 2 complete: {updated} clusters updated")

    # Apply merges
    merges = [(i, c["merge_with"]) for i, c in enumerate(clusters) if c.get("merge_with") is not None]
    if merges:
        if verbose:
            print(f"  Applying {len(merges)} merges...")
        for src_idx, tgt_idx in merges:
            if tgt_idx is None or src_idx >= len(clusters) or tgt_idx >= len(clusters):
                continue
            tgt = clusters[tgt_idx]
            src = clusters[src_idx]
            tgt["strings"] = list(set(tgt["strings"]) | set(src["strings"]))
            tgt["frequencies"].update(src["frequencies"])
            tgt["atom_ids"] = list(set(tgt["atom_ids"]) | set(src["atom_ids"]))
            # Canonical is kept as target's
            clusters[src_idx] = None  # mark for removal

        clusters = [c for c in clusters if c is not None]

    return clusters


def normalize_interest_tags(
    tag_to_atoms: dict[str, list[str]],
    verbose: bool = True,
    dry_run: bool = False,
) -> list[dict]:
    """
    Single Haiku call to group interest tags into canonical interest areas.

    Returns list of interest area dicts:
    [{canonical_tag, display_name, description, absorbs, atom_ids}]
    """
    if not tag_to_atoms:
        return []

    freq = Counter({t: len(v) for t, v in tag_to_atoms.items()})
    tag_list = ", ".join(f"{t} ({n})" for t, n in freq.most_common())

    prompt = (
        INTEREST_TAG_PROMPT + tag_list +
        "\n\nReturn JSON array:\n"
        '[{"canonical_tag": "music-curation", "display_name": "Music Curation", '
        '"description": "...", "absorbs": ["music-streaming", "music-curation"]}]'
    )

    if verbose:
        print(f"\nInterest tag normalization: {len(tag_to_atoms)} tags → Haiku...", end=" ", flush=True)
        if dry_run:
            print("[dry-run]")
            return []

    raw = _call_haiku(prompt, timeout=120)
    result = _parse_json_response(raw)

    if not result or not isinstance(result, list):
        print(f"FAILED (no JSON): {raw[:200]}", file=sys.stderr)
        return []

    if verbose:
        print(f"got {len(result)} interest areas")

    # Attach atom_ids from absorbed tags
    interest_areas = []
    covered_tags = set()
    for ia in result:
        ct = ia.get("canonical_tag", "")
        absorbs = ia.get("absorbs", [])
        all_atom_ids = []
        for tag in absorbs:
            all_atom_ids.extend(tag_to_atoms.get(tag, []))
            covered_tags.add(tag)
        # Also include atoms tagged directly with canonical_tag
        all_atom_ids.extend(tag_to_atoms.get(ct, []))
        covered_tags.add(ct)
        interest_areas.append({
            "canonical_tag": ct,
            "display_name": ia.get("display_name", ct.replace("-", " ").title()),
            "description": ia.get("description", ""),
            "raw_tags": absorbs,
            "atom_ids": list(set(all_atom_ids)),
        })

    # Any uncovered tags become their own interest area
    uncovered = set(tag_to_atoms.keys()) - covered_tags
    for tag in uncovered:
        interest_areas.append({
            "canonical_tag": tag,
            "display_name": tag.replace("-", " ").title(),
            "description": "",
            "raw_tags": [tag],
            "atom_ids": list(set(tag_to_atoms.get(tag, []))),
        })

    return interest_areas


def write_to_graph_db(
    clusters: list[dict],
    interest_areas: list[dict],
    verbose: bool = True,
) -> dict:
    """
    Write normalized entities and interest areas to graph.db.
    Also extracts co-occurrence relations from atoms.

    Returns counts.
    """
    sys.path.insert(0, str(VAULT / "scripts"))
    from graph_store import (
        init_graph_db, upsert_entity, upsert_interest_area,
        upsert_relation, _embed, get_entity_by_alias,
    )

    init_graph_db()

    if verbose:
        print(f"\nWriting {len(clusters)} entities to graph.db...")

    # Write entities
    entities_written = 0
    for cluster in clusters:
        canonical = cluster["candidate_canonical"]
        etype = cluster["candidate_type"]
        aliases = [s for s in cluster["strings"] if s != canonical]
        atom_ids = cluster["atom_ids"]

        # Get time range from atoms
        time_first, time_last = _get_atom_time_range(atom_ids)

        # Embed canonical name
        emb = _embed([canonical])[0]

        upsert_entity(
            canonical_name=canonical,
            entity_type=etype,
            aliases=cluster["strings"],  # include canonical itself
            atom_ids=atom_ids,
            first_seen=time_first,
            last_seen=time_last,
            embedding=emb,
        )
        entities_written += 1

    if verbose:
        print(f"  Entities: {entities_written} written")
        print(f"Writing {len(interest_areas)} interest areas...")

    # Write interest areas
    ia_written = 0
    for ia in interest_areas:
        if not ia["canonical_tag"] or not ia["atom_ids"]:
            continue
        upsert_interest_area(
            canonical_tag=ia["canonical_tag"],
            display_name=ia["display_name"],
            raw_tags=ia.get("raw_tags", []),
            atom_ids=ia["atom_ids"],
            description=ia.get("description", ""),
        )
        ia_written += 1

    if verbose:
        print(f"  Interest areas: {ia_written} written")

    # Build co-occurrence relations from atoms
    if verbose:
        print("Building co-occurrence relations...")
    rel_count = _build_cooccurrence_relations(verbose=verbose)
    if verbose:
        print(f"  Relations: {rel_count} written")

    return {
        "entities_written": entities_written,
        "interest_areas_written": ia_written,
        "relations_written": rel_count,
    }


def _get_atom_time_range(atom_ids: list[str]) -> tuple[str, str]:
    """Get first and last time from a set of atom_ids."""
    if not atom_ids or not ATOMS_DB.exists():
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        return now, now
    placeholders = ",".join("?" * len(atom_ids))
    conn = sqlite3.connect(str(ATOMS_DB))
    row = conn.execute(
        f"SELECT MIN(time_first), MAX(time_last) FROM atoms WHERE id IN ({placeholders})",
        atom_ids,
    ).fetchone()
    conn.close()
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    return (row[0] or now, row[1] or now)


def _build_cooccurrence_relations(verbose: bool = True) -> int:
    """
    For every atom, build co-occurrence edges between its resolved entities.
    Returns number of relation rows written/updated.
    """
    sys.path.insert(0, str(VAULT / "scripts"))
    from graph_store import get_entity_by_alias, upsert_relation

    if not ATOMS_DB.exists():
        return 0

    conn = sqlite3.connect(str(ATOMS_DB))
    conn.row_factory = sqlite3.Row
    atoms = conn.execute(
        "SELECT id, entities, time_first, time_last FROM atoms "
        "WHERE invalidated_by IS NULL AND entities IS NOT NULL"
    ).fetchall()
    conn.close()

    rel_count = 0
    for atom in atoms:
        atom_id = atom["id"]
        try:
            raw_ents = json.loads(atom["entities"] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue

        # Resolve entity strings to canonical IDs
        resolved_ids = []
        for raw in raw_ents:
            ent = get_entity_by_alias(raw)
            if ent:
                resolved_ids.append(ent["id"])

        resolved_ids = list(set(resolved_ids))

        # Create co-occurrence edges for every pair
        for i, eid_a in enumerate(resolved_ids):
            for eid_b in resolved_ids[i + 1:]:
                src, tgt = (eid_a, eid_b) if eid_a < eid_b else (eid_b, eid_a)
                upsert_relation(
                    source_entity_id=src,
                    target_entity_id=tgt,
                    relation_type="related_to",
                    atom_ids=[atom_id],
                    first_seen=atom["time_first"],
                    last_seen=atom["time_last"],
                )
                rel_count += 1

    return rel_count


def main():
    dry_run = "--dry-run" in sys.argv
    skip_llm = "--skip-llm" in sys.argv
    batch_size = BATCH_SIZE

    for i, arg in enumerate(sys.argv):
        if arg == "--batch-size" and i + 1 < len(sys.argv):
            batch_size = int(sys.argv[i + 1])

    print("=== Entity Resolver: L1 → L2 Normalization ===")
    print(f"dry_run={dry_run}, skip_llm={skip_llm}, batch_size={batch_size}")

    # Load data from atoms.db
    entity_to_atoms = load_all_entities_from_atoms()
    tag_to_atoms = load_all_interest_tags_from_atoms()

    print(f"\nLoaded {len(entity_to_atoms)} unique entity strings")
    print(f"Loaded {len(tag_to_atoms)} unique interest tags")

    if not entity_to_atoms:
        print("No entities found in atoms.db. Nothing to do.")
        return

    # Load embedding model
    print("\nLoading embedding model...")
    model = _load_model()
    print("  Model ready")

    # Pass 1: automated clustering
    start = time.monotonic()
    clusters = pass1_cluster(entity_to_atoms, model, verbose=True)
    print(f"\nPass 1 complete: {len(clusters)} clusters in {round(time.monotonic()-start, 1)}s")

    if dry_run:
        multi = [(c["candidate_canonical"], c["strings"]) for c in clusters if len(c["strings"]) > 1]
        print(f"\n[dry-run] Multi-string clusters (would merge): {len(multi)}")
        for name, strings in multi[:20]:
            print(f"  {name!r}: {strings}")
        print("\n[dry-run] Exiting.")
        return

    # Pass 2: LLM confirmation
    if not skip_llm:
        clusters = pass2_llm_normalize(clusters, batch_size=batch_size, verbose=True)

    # Interest tag normalization
    interest_areas = normalize_interest_tags(tag_to_atoms, verbose=True, dry_run=skip_llm)

    # Write to graph.db
    counts = write_to_graph_db(clusters, interest_areas, verbose=True)

    elapsed = round(time.monotonic() - start, 1)
    print(f"\n=== Done in {elapsed}s ===")
    print(f"  Entities: {counts['entities_written']}")
    print(f"  Interest areas: {counts['interest_areas_written']}")
    print(f"  Co-occurrence relations: {counts['relations_written']}")

    # Show stats
    sys.path.insert(0, str(VAULT / "scripts"))
    from graph_store import graph_stats
    print(f"\nGraph stats: {json.dumps(graph_stats(), indent=2)}")


if __name__ == "__main__":
    main()
