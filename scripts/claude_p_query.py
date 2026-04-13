"""
claude_p_query.py — subprocess shim around `claude -p`.

Purpose
-------
Run Claude Code in headless (-p/--print) mode to answer grounded-context
questions. When invoked from a shell session already authenticated via
OAuth Max Pro, `claude -p` inherits that auth and its HTTP calls hit the
subscription bucket rather than the API tier. This dodges Tier 1's 10K
output-TPM cap for Haiku 4.5 that blocks bulk use of the Anthropic API.

Threaded parallelism: multiple subprocess invocations can run concurrently.
The bottleneck is Claude Code process cold-start (~5-10s) plus per-call
model latency. Thread-pool concurrency of 6-10 is a reasonable default.

Used by autoresearch_loop.py as the query backend.
"""
from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_TIMEOUT = 120
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def build_query_prompt(*, question: str, context: str) -> str:
    """Format a grounded-context question prompt for claude -p.

    The answer must be drawn only from the provided context so token-level
    F1 against the gold answer is meaningful.
    """
    return (
        "Answer the following question using ONLY the context provided below. "
        "If the context does not contain the answer, say \"unknown\".\n"
        "Be concise — 1-3 sentences maximum. Do not cite the context. "
        "Output only the answer text, nothing else.\n\n"
        "=== CONTEXT ===\n"
        f"{context}\n"
        "=== END CONTEXT ===\n\n"
        f"QUESTION: {question}\n\n"
        "ANSWER:"
    )


def query_single(
    *,
    question: str,
    context: str,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Run a single `claude -p` invocation and return the cleaned text response.
    Returns empty string on error or timeout."""
    prompt = build_query_prompt(question=question, context=context)
    cmd = [
        "claude",
        "-p",
        "--output-format", "text",
        "--no-session-persistence",
        "--disallowedTools", "Bash,Edit,Write,Read,Glob,Grep,Agent,WebFetch,WebSearch,TodoWrite,Task",
        "--model", model,
        prompt,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ""
    if result.returncode != 0:
        return ""
    return _strip_ansi(result.stdout or "").strip()


def query_batch(
    questions: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    max_workers: int = 6,
) -> list[str]:
    """Run a batch of (question, context) pairs in parallel through claude -p.

    Preserves input order. Failed calls return empty strings.

    questions: list of {"question": str, "context": str}
    """
    results: list[str] = [""] * len(questions)

    def _work(i: int, q: dict) -> tuple[int, str]:
        return i, query_single(
            question=q["question"],
            context=q["context"],
            model=model,
            timeout=timeout,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_work, i, q) for i, q in enumerate(questions)]
        for fut in as_completed(futures):
            i, answer = fut.result()
            results[i] = answer

    return results
