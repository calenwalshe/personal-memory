#!/bin/bash
# run_weekly.sh — autoresearch-memory weekly pipeline
#
# Runs the full memory consolidation chain:
#   1. extract_sessions.py --index --update  (incremental graphrag reindex)
#   2. build_eval_set.py                     (add QA pairs for new holdout sessions)
#   3. deep_consolidate.py                   (community reports → vault candidates)
#   4. autoresearch_loop.py --iterations 3   (F1-guided parameter search)
#
# Installed via crontab:
#   0 4 * * 0 /home/agent/memory/vault/scripts/run_weekly.sh
#
# Logs to run.log in the same directory. set -e halts the chain on any failure
# so the autoresearch loop never runs against a stale or broken index.

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
PY=/home/agent/claude-stack-env/bin/python3
LOG="$SCRIPTS_DIR/run.log"

# Source Anthropic API key from call-agent env (same location used by the
# pipeline's other scripts).
if [[ -f /home/agent/.config/call-agent/.env ]]; then
    # shellcheck disable=SC1091
    source /home/agent/.config/call-agent/.env
    export ANTHROPIC_API_KEY
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "[$(date -Iseconds)] ERROR: ANTHROPIC_API_KEY not set" >&2
    exit 1
fi

echo "=================================================================" >> "$LOG"
echo "[$(date -Iseconds)] run_weekly.sh start" >> "$LOG"

cd "$SCRIPTS_DIR"

echo "[$(date -Iseconds)] phase 1: extract_sessions --index --update" >> "$LOG"
"$PY" extract_sessions.py --index --update >> "$LOG" 2>&1

echo "[$(date -Iseconds)] phase 2: build_eval_set" >> "$LOG"
"$PY" build_eval_set.py --pairs-per-session 15 >> "$LOG" 2>&1

echo "[$(date -Iseconds)] phase 3: deep_consolidate" >> "$LOG"
"$PY" deep_consolidate.py >> "$LOG" 2>&1

echo "[$(date -Iseconds)] phase 4: autoresearch_loop --iterations 3 (via claude -p)" >> "$LOG"
# Phase 4 uses local retrieval + `claude -p` subprocesses for answer generation,
# routing LLM calls through Claude Code OAuth (Max Pro subscription) to avoid
# Anthropic API Tier 1 rate limits. Requires `claude` CLI on PATH with an
# OAuth-authenticated session.
"$PY" autoresearch_loop.py --iterations 3 --max-workers 6 >> "$LOG" 2>&1

echo "[$(date -Iseconds)] run_weekly.sh done" >> "$LOG"
