# Contract: session-continuity-001

**ID:** session-continuity-001
**Slug:** session-continuity
**Phase:** execute
**Status:** approved

---

## Objective

Build session continuity hooks so that a developer who exits any Claude Code session — through compaction, /clear, or normal exit — finds their prior working context automatically present at the next session start, without asking for it.

---

## Deliverables

| # | Artifact | Path |
|---|---|---|
| 1 | PreCompact snapshot hook | `~/.claude/hooks/precompact-session-snapshot.sh` |
| 2 | SessionEnd summary hook | `~/.claude/hooks/sessionend-session-summary.sh` |
| 3 | SessionStart hook (modified) | `~/.claude/hooks/cortex-session-start.sh` |
| 4 | Hook registry (modified) | `~/.claude/settings.json` |
| 5 | Git ignore entry | `.gitignore` in target project |

---

## Scope

**In Scope:**
- Two new hook scripts (precompact-session-snapshot.sh, sessionend-session-summary.sh)
- Modification to cortex-session-start.sh (session-memory read, ≤10 lines)
- Modification to settings.json (register hooks, move cortex-session-end.sh to SessionEnd)
- .gitignore entry for .cortex/session-memory.md

**Out of Scope:**
- Window classifier, facts.db, FAISS, vault changes
- CLAUDE.md modifications
- Cross-project or global memory
- Long-term vault storage of session summaries

---

## Write Roots

- `~/.claude/hooks/precompact-session-snapshot.sh`
- `~/.claude/hooks/sessionend-session-summary.sh`
- `~/.claude/hooks/cortex-session-start.sh`
- `~/.claude/settings.json`
- `$CLAUDE_PROJECT_DIR/.cortex/session-memory.md` (runtime, not committed)
- `$CLAUDE_PROJECT_DIR/.gitignore`

---

## Done Criteria

1. After PreCompact fires, `.cortex/session-memory.md` contains snapshot section ≤6 lines with current task, state, next step
2. After SessionEnd (non-clear), `.cortex/session-memory.md` contains summary section with project area, accomplishments, decisions, open thread, status
3. After SessionEnd (matcher=="clear"), summary present with `status: cleared`, no open thread
4. At SessionStart, session-memory content appears in additionalContext
5. Deleting `.cortex/session-memory.md` causes no hook or session-start errors
6. `cortex-session-end.sh` no longer fires on every agent turn (moved to SessionEnd)
7. No developer-authored CLAUDE.md is modified
8. All LLM calls in hooks use `claude-haiku-4-5-20251001`
9. Haiku timeout fallback writes raw turns — compaction never hangs indefinitely
10. `.cortex/session-memory.md` listed in `.gitignore`

---

## Validators

```bash
# 1. Snapshot exists and has correct schema after compaction
grep -q "## Session Snapshot" "$CLAUDE_PROJECT_DIR/.cortex/session-memory.md"
grep -q "What we're doing:" "$CLAUDE_PROJECT_DIR/.cortex/session-memory.md"

# 2. Summary exists after session end
grep -q "## Session Summary" "$CLAUDE_PROJECT_DIR/.cortex/session-memory.md"
grep -q "Open thread:" "$CLAUDE_PROJECT_DIR/.cortex/session-memory.md"

# 3. Cleared status on /clear
grep -q "status: cleared" "$CLAUDE_PROJECT_DIR/.cortex/session-memory.md"

# 4. cortex-session-end.sh no longer on Stop hook
jq '.hooks.Stop[].command' ~/.claude/settings.json | grep -v "cortex-session-end"

# 5. New hooks registered
jq '.hooks.PreCompact[].command' ~/.claude/settings.json | grep "precompact-session-snapshot"
jq '.hooks.SessionEnd[].command' ~/.claude/settings.json | grep "sessionend-session-summary"

# 6. Gitignore contains session-memory.md
grep -q "session-memory.md" "$CLAUDE_PROJECT_DIR/.gitignore"

# 7. Haiku used, not Sonnet
grep -v "claude-sonnet" ~/.claude/hooks/precompact-session-snapshot.sh
grep -v "claude-sonnet" ~/.claude/hooks/sessionend-session-summary.sh
```

---

## Eval Plan

docs/cortex/evals/session-continuity/eval-plan.md (pending)

---

## Repair Budget

- `max_repair_contracts: 3`
- `cooldown_between_repairs: 1`

## Failed Approaches

*(none — initial contract)*

## Why Previous Approach Failed

N/A — initial contract

---

## Approvals

- [ ] Contract approved for execution
- [ ] Evals approved
