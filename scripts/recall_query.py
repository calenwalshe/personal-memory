"""
recall_query.py — FAISS retrieval + LLM synthesis for /recall [topic].

Usage:
  python3 recall_query.py "FAISS embeddings"
  python3 recall_query.py "FAISS embeddings" --deep
  python3 recall_query.py --pending-review

Modes:
  shallow (default): FAISS top-K → Haiku synthesis (target < 3s)
  deep (--deep): FAISS + topic/entity expansion → Haiku synthesis
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "claude")

_NVM_BIN = Path.home() / ".nvm/versions/node/v20.20.0/bin"
NODE_BIN = os.environ.get("NODE_BIN") or shutil.which("node") or str(_NVM_BIN / "node")
_claude_sym = shutil.which("claude") or str(_NVM_BIN / "claude")
CLAUDE_CLI = os.environ.get("CLAUDE_CLI") or str(Path(_claude_sym).resolve())
_codex_sym = shutil.which("codex") or str(_NVM_BIN / "codex")
CODEX_CLI = os.environ.get("CODEX_CLI") or str(Path(_codex_sym).resolve())
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or str(_NVM_BIN / "claude")
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or str(_NVM_BIN / "codex")

sys.path.insert(0, str(VAULT / "scripts"))


def llm(prompt: str) -> str:
    if LLM_PROVIDER == "codex":
        cmd = [NODE_BIN, CODEX_CLI, "exec", "--full-auto", "--profile", "llm",
               "--cd", "/tmp", "--skip-git-repo-check", prompt]
    else:
        cmd = [NODE_BIN, CLAUDE_CLI, "-p", prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


SYNTHESIS_PREAMBLE = """You are a memory recall assistant. Given retrieved facts from a developer's
learning history, synthesize a concise answer (1-3 sentences). Always reference the session date
when available. Format: "You learned: [fact summary] (session date: YYYY-MM-DD)".
If multiple facts exist, synthesize them into a coherent answer. If contradictory facts exist,
surface the most recent non-invalidated one. Never fabricate facts not present in the retrieved context."""


def recall_shallow(query: str, top_k: int = 5, memory_type: str = None, project_scope: str = None) -> str:
    """FAISS semantic search → LLM synthesis."""
    from fact_store import query_facts, pending_review_count

    facts = query_facts(query, top_k=top_k, memory_type=memory_type, project_scope=project_scope)
    if not facts:
        return f"No facts found matching '{query}'."

    # Format retrieved facts for synthesis
    facts_text = "\n".join(
        f"[{i+1}] ({f.get('valid_from', 'unknown date')}) {f['content']}  "
        f"[score={f.get('score', 0):.3f}, topic={f.get('topic', 'n/a')}]"
        for i, f in enumerate(facts)
    )

    prompt = f"""{SYNTHESIS_PREAMBLE}

Query: {query}

Retrieved facts:
{facts_text}

Synthesize a concise answer (1-3 sentences) that directly addresses the query.
Reference session dates. If facts conflict, prefer the most recent."""

    answer = llm(prompt)

    # Append pending review notice
    pending = pending_review_count()
    if pending > 0:
        answer += f"\n\n[Note: {pending} contradiction(s) pending human review — run with --pending-review to see them]"

    return answer


def recall_deep(query: str, top_k: int = 10, memory_type: str = None, project_scope: str = None) -> str:
    """Query expansion + broader retrieval → LLM synthesis."""
    from fact_store import query_facts, pending_review_count

    # Step 1: Expand the query using LLM
    expand_prompt = f"""Given this recall query: "{query}"
Generate 2-3 semantically related search phrases that might surface relevant facts.
Respond with one phrase per line, no numbering or bullets."""

    expanded_raw = llm(expand_prompt)
    expanded_queries = [query] + [q.strip() for q in expanded_raw.splitlines() if q.strip()][:2]

    # Step 2: Retrieve facts for all queries, deduplicate
    seen_ids = set()
    all_facts = []
    for q in expanded_queries:
        for f in query_facts(q, top_k=top_k // len(expanded_queries) + 2,
                             memory_type=memory_type, project_scope=project_scope):
            if f["id"] not in seen_ids:
                seen_ids.add(f["id"])
                all_facts.append(f)

    # Sort by score, take top_k
    all_facts.sort(key=lambda f: f.get("score", 0), reverse=True)
    all_facts = all_facts[:top_k]

    if not all_facts:
        return f"No facts found matching '{query}'."

    facts_text = "\n".join(
        f"[{i+1}] ({f.get('valid_from', 'unknown date')}) {f['content']}  "
        f"[score={f.get('score', 0):.3f}, topic={f.get('topic', 'n/a')}]"
        for i, f in enumerate(all_facts)
    )

    prompt = f"""{SYNTHESIS_PREAMBLE}

Query: {query}

Retrieved facts (from expanded search):
{facts_text}

Synthesize a comprehensive answer referencing specific session dates.
Surface any patterns or evolution of understanding across sessions."""

    answer = llm(prompt)

    pending = pending_review_count()
    if pending > 0:
        answer += f"\n\n[Note: {pending} contradiction(s) pending human review]"

    return answer


def show_pending_review() -> str:
    """Display all pending contradiction review items."""
    import json
    import sqlite3

    db_path = VAULT / "facts.db"
    if not db_path.exists():
        return "No facts database found."

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT cr.*,
               n.content as new_content, n.valid_from as new_date,
               o.content as old_content, o.valid_from as old_date
        FROM contradiction_review cr
        JOIN facts n ON cr.new_fact_id = n.id
        JOIN facts o ON cr.old_fact_id = o.id
        WHERE cr.status = 'pending'
        ORDER BY cr.id
    """).fetchall()
    conn.close()

    if not rows:
        return "No pending contradiction reviews."

    lines = [f"Pending contradiction reviews ({len(rows)}):"]
    for r in rows:
        lines.append(f"\n  ID: {r['id']}")
        lines.append(f"  New ({r['new_date']}): {r['new_content']}")
        lines.append(f"  Old ({r['old_date']}): {r['old_content']}")
        lines.append(f"  Reason: {r['reason']}")
        lines.append(f"  Status: {r['status']}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Recall facts from memory vault")
    parser.add_argument("query", nargs="?", help="Topic to recall")
    parser.add_argument("--deep", action="store_true", help="Use deep query expansion mode")
    parser.add_argument("--top-k", type=int, default=5, help="Max facts to retrieve")
    parser.add_argument("--pending-review", action="store_true",
                        help="Show pending contradiction reviews")
    parser.add_argument("--type", dest="memory_type", default=None,
                        help="Filter by memory type: episodic, procedural, semantic")
    parser.add_argument("--project", dest="project_scope", default=None,
                        help="Filter by project scope (e.g. cortex-memory-platform)")
    args = parser.parse_args()

    if args.pending_review:
        print(show_pending_review())
        return

    if not args.query:
        parser.error("query is required (or use --pending-review)")

    if args.deep:
        answer = recall_deep(args.query, top_k=args.top_k,
                             memory_type=args.memory_type, project_scope=args.project_scope)
    else:
        answer = recall_shallow(args.query, top_k=args.top_k,
                                memory_type=args.memory_type, project_scope=args.project_scope)

    print(answer)


if __name__ == "__main__":
    main()
