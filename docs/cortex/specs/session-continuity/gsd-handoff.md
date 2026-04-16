# GSD Handoff: session-continuity

**Contract:** docs/cortex/contracts/session-continuity/contract-001.md
**Spec:** docs/cortex/specs/session-continuity/spec.md

---

## Objective

Build two Claude Code hook scripts that capture working context before compaction and session state at exit, writing both to `.cortex/session-memory.md`. Modify the SessionStart hook to inject that file into session-start context. Fix the existing `cortex-session-end.sh` misregistration (Stop → SessionEnd). Result: a developer who exits any session finds their prior context automatically present at the next session start, without asking for it.

---

## Deliverables

1. `~/.claude/hooks/precompact-session-snapshot.sh` — new PreCompact hook
2. `~/.claude/hooks/sessionend-session-summary.sh` — new SessionEnd hook
3. `~/.claude/hooks/cortex-session-start.sh` — modified (add session-memory read)
4. `~/.claude/settings.json` — modified (new hooks + Stop→SessionEnd fix)
5. `$CLAUDE_PROJECT_DIR/.gitignore` — `.cortex/session-memory.md` added

---

## Requirements

None formalized.

---

## Tasks

1. - [ ] Write `precompact-session-snapshot.sh`: read last 30 turns from `transcript_path`, call Haiku (`claude-haiku-4-5-20251001`) with prompt "In 4-5 lines: current task, current state, next step, blocking context", write output under `## Session Snapshot — {timestamp}` in `.cortex/session-memory.md`. If Haiku fails or times out (>10s), write raw last-5-turns verbatim as fallback. Exit 0 always (never block compaction indefinitely).
2. - [ ] Write `sessionend-session-summary.sh`: read full transcript from `transcript_path` (cap at 8,000 chars from end), call Haiku with prompt "In 8-12 lines: project area, what was accomplished, key decisions (1-3 bullets), open thread, status (resolved|paused|blocked)". If `matcher == "clear"`, use prompt variant that produces `status: cleared` and omits open thread. Write/replace content under `## Session Summary — {timestamp}` in `.cortex/session-memory.md`.
3. - [ ] Register `precompact-session-snapshot.sh` in `~/.claude/settings.json` under `PreCompact` hooks array.
4. - [ ] Register `sessionend-session-summary.sh` in `~/.claude/settings.json` under `SessionEnd` hooks array.
5. - [ ] Move `cortex-session-end.sh` from `Stop` hooks array to `SessionEnd` hooks array in `~/.claude/settings.json`.
6. - [ ] Add to `cortex-session-start.sh`: after existing current-state.md read block, add: if `.cortex/session-memory.md` exists, read it and append to additionalContext output (cap at 800 chars, truncate with "…" if over).
7. - [ ] Add `.cortex/session-memory.md` to `.gitignore`.

---

## Acceptance Criteria

- [ ] After PreCompact: `.cortex/session-memory.md` contains snapshot section ≤6 lines with current task, state, next step
- [ ] After SessionEnd (non-clear): `.cortex/session-memory.md` contains summary section with project area, accomplishments, decisions, open thread, status
- [ ] After SessionEnd (clear): summary section present, `status: cleared`, no open thread
- [ ] At SessionStart: session-memory content visible in additionalContext
- [ ] Deleting `.cortex/session-memory.md` causes no errors
- [ ] `cortex-session-end.sh` no longer fires on every agent turn
- [ ] No CLAUDE.md files modified
- [ ] All LLM calls use Haiku
- [ ] Haiku timeout fallback writes raw turns (compaction never hangs)
- [ ] `.cortex/session-memory.md` in `.gitignore`
