#!/usr/bin/env python3
"""
doc_pipeline.py — Three-step pipeline for processing a large research doc.

Step 1: dedupe   — remove duplicate paragraphs/blocks by rolling hash
Step 2: index    — Haiku pass to build a navigable index with char offsets
Step 3: section  — pull a specific indexed section by ID (for Opus deep-dive)

Usage:
  python3 doc_pipeline.py dedupe  <input.md> <output-deduped.md>
  python3 doc_pipeline.py index   <deduped.md> <output-index.md>
  python3 doc_pipeline.py section <deduped.md> <index.md> <section_id>
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path

# ── Step 1: Dedupe ────────────────────────────────────────────────────────────

NOISE_PATTERNS = [
    re.compile(r'^Called tool\s*$', re.MULTILINE),
    re.compile(r'^I\'m (?:checking|opening|pulling|reading|looking|grounding|fetching).*?\.\s*$', re.MULTILINE),
    re.compile(r'^I\'ve got .*?\.\s*$', re.MULTILINE),
    re.compile(r'^One more (?:quick )?pass.*?\.\s*$', re.MULTILINE),
    re.compile(r'^\s*─+\s*$', re.MULTILINE),     # separator lines
]


def _block_hash(text: str) -> str:
    normalized = re.sub(r'\s+', ' ', text.strip().lower())
    return hashlib.md5(normalized.encode()).hexdigest()


def _split_blocks(text: str) -> list[str]:
    """Split on double newlines, treating each paragraph as a block."""
    blocks = re.split(r'\n{2,}', text)
    return [b.strip() for b in blocks if b.strip()]


def _strip_noise(block: str) -> str:
    for pat in NOISE_PATTERNS:
        block = pat.sub('', block)
    return block.strip()


def dedupe(input_path: Path, output_path: Path):
    raw = input_path.read_text(encoding='utf-8')

    # Split into blocks
    blocks = _split_blocks(raw)

    seen_hashes: set[str] = set()
    kept: list[str] = []
    stats = {'total': len(blocks), 'noise_stripped': 0, 'dupes_removed': 0, 'kept': 0}

    for block in blocks:
        cleaned = _strip_noise(block)

        # Skip if nothing left after noise strip
        if len(cleaned) < 20:
            stats['noise_stripped'] += 1
            continue

        h = _block_hash(cleaned)
        if h in seen_hashes:
            stats['dupes_removed'] += 1
            continue

        seen_hashes.add(h)
        kept.append(cleaned)
        stats['kept'] += 1

    deduped = '\n\n'.join(kept)
    output_path.write_text(deduped, encoding='utf-8')

    print(f"Dedupe complete:")
    print(f"  Input blocks : {stats['total']}")
    print(f"  Noise stripped: {stats['noise_stripped']}")
    print(f"  Dupes removed : {stats['dupes_removed']}")
    print(f"  Kept          : {stats['kept']}")
    print(f"  Input size    : {len(raw):,} chars")
    print(f"  Output size   : {len(deduped):,} chars")
    print(f"  Reduction     : {100*(1-len(deduped)/len(raw)):.1f}%")
    print(f"  Written to    : {output_path}")


# ── Step 2: Index ─────────────────────────────────────────────────────────────

CHUNK_SIZE = 3000   # chars per Haiku call
CHUNK_OVERLAP = 200 # overlap to avoid cutting mid-thought

INDEX_SYSTEM = """You are indexing sections of a research conversation about AI memory systems.
The conversation discusses: a personal memory vault system (SCAPE/vault), comparisons with other systems (Mem0, Letta, Graphiti, Cognee), gap analysis, and ideas for future development.

For each chunk, return a JSON object with:
- "topic": short label (3-6 words, e.g. "Hebbian decay gap analysis")
- "summary": 1-2 sentence description of what this chunk contains
- "type": one of: gap | idea | comparison | recommendation | description | question | other
- "systems_mentioned": list of system names mentioned (e.g. ["Mem0", "Letta", "vault"])
- "actionable": true if this chunk contains something worth acting on for the personal-memory system

Return ONLY valid JSON, no explanation."""

INDEX_USER = """Chunk {chunk_id} (chars {start}–{end}):

{text}"""


def _haiku(system: str, user: str) -> str:
    import subprocess
    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "system": system,
        "messages": [{"role": "user", "content": user}]
    })
    result = subprocess.run(
        ["env", "-u", "ANTHROPIC_API_KEY", "claude", "-p", user,
         "--system", system, "--model", "claude-haiku-4-5-20251001",
         "--max-tokens", "300", "--output-format", "text"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"Haiku call failed: {result.stderr[:200]}")
    return result.stdout.strip()


def _haiku_api(system: str, user: str) -> str:
    """Call Haiku via `claude -p` (subscription billing, no API credits needed)."""
    import subprocess
    # Bake system prompt into the user message since --system flag isn't supported
    combined = f"{system}\n\n---\n\n{user}"
    result = subprocess.run(
        ["env", "-u", "ANTHROPIC_API_KEY",
         "claude", "-p", combined,
         "--model", "claude-haiku-4-5-20251001"],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed: {result.stderr[:200]}")
    return result.stdout.strip()


def build_index(deduped_path: Path, index_path: Path):
    text = deduped_path.read_text(encoding='utf-8')
    total = len(text)

    chunks = []
    pos = 0
    chunk_id = 0
    while pos < total:
        end = min(pos + CHUNK_SIZE, total)
        # Try to break at a paragraph boundary
        if end < total:
            boundary = text.rfind('\n\n', pos, end)
            if boundary > pos + CHUNK_SIZE // 2:
                end = boundary
        chunk_text = text[pos:end].strip()
        if chunk_text:
            chunks.append((chunk_id, pos, end, chunk_text))
            chunk_id += 1
        pos = max(end - CHUNK_OVERLAP, end)  # no overlap if at end
        if pos >= total:
            break

    print(f"Indexing {len(chunks)} chunks via Haiku...")

    entries = []
    for cid, start, end, chunk_text in chunks:
        prompt = INDEX_USER.format(chunk_id=cid, start=start, end=end, text=chunk_text[:CHUNK_SIZE])
        try:
            raw = _haiku_api(INDEX_SYSTEM, prompt)
            # Extract JSON from response
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}', raw, re.DOTALL) or re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                entry = json.loads(json_match.group())
            else:
                entry = {"topic": "unknown", "summary": raw[:100], "type": "other",
                         "systems_mentioned": [], "actionable": False}
        except Exception as e:
            entry = {"topic": f"chunk-{cid}", "summary": f"[parse error: {e}]",
                     "type": "other", "systems_mentioned": [], "actionable": False}

        entry["chunk_id"] = cid
        entry["char_start"] = start
        entry["char_end"] = end
        entry["char_count"] = end - start
        entries.append(entry)
        print(f"  [{cid:3d}/{len(chunks)-1}] {entry.get('type','?'):14s}  {entry.get('topic','?')[:50]}")

    # Write index as markdown with embedded JSON per entry
    lines = [
        "# Document Index",
        f"\nSource: `{deduped_path.name}`  ",
        f"Total chunks: {len(entries)}  ",
        f"Total chars: {total:,}  ",
        f"Actionable chunks: {sum(1 for e in entries if e.get('actionable'))}",
        "\n---\n",
        "## Chunks\n",
        "| ID | Type | Topic | Chars | Actionable |",
        "|-----|------|-------|-------|------------|",
    ]

    for e in entries:
        actionable = "✓" if e.get("actionable") else ""
        lines.append(
            f"| {e['chunk_id']} | {e.get('type','?')} | {e.get('topic','?')} "
            f"| {e['char_count']} | {actionable} |"
        )

    lines.append("\n---\n")
    lines.append("## Full Entries\n")
    lines.append("```json")
    lines.append(json.dumps(entries, indent=2))
    lines.append("```")

    index_path.write_text('\n'.join(lines), encoding='utf-8')
    actionable_count = sum(1 for e in entries if e.get('actionable'))
    print(f"\nIndex written to {index_path}")
    print(f"  {len(entries)} chunks, {actionable_count} actionable")


# ── Step 3: Section pull ──────────────────────────────────────────────────────

def pull_section(deduped_path: Path, index_path: Path, chunk_id: int, context: int = 0):
    """
    Print the raw text of chunk_id (plus optional surrounding context chunks).
    Pipe this to Opus or paste into a prompt.
    """
    text = deduped_path.read_text(encoding='utf-8')
    index_text = index_path.read_text(encoding='utf-8')

    # Parse entries from index
    json_match = re.search(r'```json\n(.*?)\n```', index_text, re.DOTALL)
    if not json_match:
        print("ERROR: could not parse index JSON", file=sys.stderr)
        sys.exit(1)

    entries = json.loads(json_match.group(1))
    entry_map = {e['chunk_id']: e for e in entries}

    ids_to_fetch = list(range(
        max(0, chunk_id - context),
        min(len(entries), chunk_id + context + 1)
    ))

    for cid in ids_to_fetch:
        e = entry_map.get(cid)
        if not e:
            continue
        section_text = text[e['char_start']:e['char_end']]
        print(f"\n{'='*60}")
        print(f"CHUNK {cid} | {e.get('type','?').upper()} | {e.get('topic','?')}")
        print(f"Chars {e['char_start']}–{e['char_end']} | actionable={e.get('actionable')}")
        if e.get('systems_mentioned'):
            print(f"Systems: {', '.join(e['systems_mentioned'])}")
        print(f"{'='*60}\n")
        print(section_text)


def list_index(index_path: Path, type_filter: str = None, actionable_only: bool = False):
    """Print the index table, optionally filtered."""
    index_text = index_path.read_text(encoding='utf-8')
    json_match = re.search(r'```json\n(.*?)\n```', index_text, re.DOTALL)
    if not json_match:
        print("ERROR: could not parse index JSON", file=sys.stderr)
        sys.exit(1)

    entries = json.loads(json_match.group(1))

    if type_filter:
        entries = [e for e in entries if e.get('type') == type_filter]
    if actionable_only:
        entries = [e for e in entries if e.get('actionable')]

    print(f"{'ID':>4}  {'Type':14}  {'A':1}  {'Systems':25}  Topic")
    print('-' * 90)
    for e in entries:
        a = '✓' if e.get('actionable') else ' '
        sys_str = ', '.join(e.get('systems_mentioned', []))[:24]
        print(f"{e['chunk_id']:>4}  {e.get('type','?'):14}  {a}  {sys_str:25}  {e.get('topic','?')}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'help'

    if cmd == 'dedupe':
        inp = Path(sys.argv[2])
        out = Path(sys.argv[3]) if len(sys.argv) > 3 else inp.with_stem(inp.stem + '-deduped')
        dedupe(inp, out)

    elif cmd == 'index':
        inp = Path(sys.argv[2])
        out = Path(sys.argv[3]) if len(sys.argv) > 3 else inp.with_stem(inp.stem + '-index')
        build_index(inp, out)

    elif cmd == 'section':
        deduped = Path(sys.argv[2])
        index = Path(sys.argv[3])
        cid = int(sys.argv[4])
        ctx = int(sys.argv[5]) if len(sys.argv) > 5 else 0
        pull_section(deduped, index, cid, context=ctx)

    elif cmd == 'ls':
        index = Path(sys.argv[2])
        type_filter = sys.argv[3] if len(sys.argv) > 3 else None
        actionable = '--actionable' in sys.argv
        list_index(index, type_filter=type_filter, actionable_only=actionable)

    else:
        print(__doc__)
