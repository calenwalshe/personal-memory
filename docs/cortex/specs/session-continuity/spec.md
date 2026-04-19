# Spec: session-continuity

**Slug:** session-continuity
**Status:** pending approval
**Informed by:** docs/cortex/research/session-continuity/concept-20260414T170000Z.md

---

## 1. Problem

Developers using Claude Code lose working context whenever the context window compacts or a session ends. At the next session start, they must re-explain where they were, what was being worked on, and what the current state is — a cold restart that costs 10-45 minutes of recovery time. The existing system stores long-term vault facts but has no mechanism to capture and surface short-term session continuity context. Two distinct context losses occur: (1) mid-session loss when the context window auto-compacts (immediate in-session recovery needed), and (2) cross-session loss when a session ends (recovery needed at next session start). A secondary problem discovered in research: `cortex-session-end.sh` is currently misregistered on the `Stop` hook instead of `SessionEnd`, causing it to fire after every agent response turn rather than once at session termination.

---

## 2. Acceptance Criteria

- [ ] After PreCompact fires, `.cortex/session-memory.md` contains a snapshot section with: current task (1 sentence), current state (1-2 sentences), next step (1 sentence), and blocking context (1 sentence if any) — total ≤ 6 lines
- [ ] After SessionEnd fires with matcher != "clear", `.cortex/session-memory.md` contains a summary section with: project area, what was accomplished, key decisions (1-3 bullets), open thread, and status (resolved|paused|blocked)
- [ ] After SessionEnd fires with matcher == "clear", `.cortex/session-memory.md` contains a summary section with status: cleared — open thread and next step fields are absent
- [ ] At SessionStart, if `.cortex/session-memory.md` exists, its content is injected into `additionalContext` (visible in session-start context)
- [ ] Developer can delete `.cortex/session-memory.md` without causing any hook or session-start errors (graceful degradation)
- [ ] `cortex-session-end.sh` fires on `SessionEnd`, not `Stop` — verified by observing that `current-state.md` no longer rewrites on every agent turn
- [ ] No developer-authored CLAUDE.md file is modified by any hook
- [ ] All LLM summarization calls use `claude-haiku-4-5-20251001`, not Sonnet
- [ ] If the Haiku call in PreCompact fails, a raw fallback (last 5 turns verbatim) is written instead — compaction is never blocked indefinitely
- [ ] `.cortex/session-memory.md` is listed in `.gitignore` (or equivalent project-level ignore)

---

## 3. Scope

**In Scope:**
- New hook script: `~/.claude/hooks/precompact-session-snapshot.sh`
- New hook script: `~/.claude/hooks/sessionend-session-summary.sh`
- Modification to `~/.claude/hooks/cortex-session-start.sh` (add session-memory.md read, ≤10 lines)
- Modification to `~/.claude/settings.json` (register two new hooks; move cortex-session-end.sh from Stop → SessionEnd)
- `.gitignore` addition for `.cortex/session-memory.md`
- New system-owned file: `.cortex/session-memory.md` (runtime artifact, not committed)

**Out of Scope:**
- Window classifier changes or improvements
- facts.db schema changes
- FAISS or embedding changes
- Developer-authored CLAUDE.md modifications
- vault/raw/ or vault/compiled/ storage changes
- Cross-project or global memory
- UI for browsing session history
- Improving extraction recall or F1
- Long-term vault storage of session summaries (the window classifier handles that independently)

---

## 4. Architecture Decision

**Chosen approach:** Two dedicated hook scripts write `.cortex/session-memory.md`; the existing SessionStart hook reads and injects it via `additionalContext`.

**Rationale:** Reuses the existing injection surface (SessionStart `additionalContext` mechanism, already reading `.cortex/` files). Isolates LLM calls to write time (PreCompact, SessionEnd) rather than read time (SessionStart) — this is specifically why the vault injection was disabled: it spawned LLM processes at session start. A pre-written file avoids that pattern entirely. PreCompact can block (exit code 2) until the snapshot is written, so compaction doesn't wipe context before the snapshot is captured.

**Alternatives considered:**
- **Read transcript at SessionStart and summarize inline:** Rejected — spawns Haiku process at session start, identical failure mode to the disabled vault injection. Adds latency to every session start.
- **Write to project-root MEMORY.md:** Rejected — MEMORY.md is not auto-read by Claude Code; requires the same SessionStart hook modification anyway, with no advantage over `.cortex/session-memory.md` and higher risk of confusion with developer files.
- **Use window classifier output as session summary:** Rejected — missing temporal narrative, session-level intent, and work distribution. Classifier produces atomic facts, not a session story.
- **Use Stop hook for summary writes:** Rejected — fires after every agent turn. Would rewrite `.cortex/session-memory.md` 50+ times per session.
- **Single script handling both PreCompact and SessionEnd:** Rejected — the content and triggering logic differ enough (snapshot vs summary, blocking vs non-blocking, matcher handling) to warrant separate scripts.

---

## 5. Interfaces

| Interface | Path | Owner | This spec reads | This spec writes |
|---|---|---|---|---|
| PreCompact hook | `~/.claude/hooks/precompact-session-snapshot.sh` | system | stdin: `{session_id, transcript_path, cwd}` | new file |
| SessionEnd hook | `~/.claude/hooks/sessionend-session-summary.sh` | system | stdin: `{session_id, transcript_path, cwd, matcher}` | new file |
| SessionStart hook | `~/.claude/hooks/cortex-session-start.sh` | system | `.cortex/session-memory.md` | `additionalContext` output |
| Hook registry | `~/.claude/settings.json` | system | existing hook registrations | PreCompact and SessionEnd entries; Stop→SessionEnd for cortex-session-end.sh |
| Session memory file | `.cortex/session-memory.md` (per-project) | system | — | written by hooks |
| Anthropic API | Haiku (`claude-haiku-4-5-20251001`) | external | — | called for summarization |
| Git ignore | `.gitignore` (per-project) | developer | — | append `.cortex/session-memory.md` |

**Write roots:**
- `~/.claude/hooks/precompact-session-snapshot.sh` (new file)
- `~/.claude/hooks/sessionend-session-summary.sh` (new file)
- `~/.claude/hooks/cortex-session-start.sh` (modify)
- `~/.claude/settings.json` (modify)
- `$CLAUDE_PROJECT_DIR/.cortex/session-memory.md` (runtime, per-project)
- `$CLAUDE_PROJECT_DIR/.gitignore` (append)

---

## 6. Dependencies

| Dependency | Version | Purpose |
|---|---|---|
| Claude Code hook system | current | PreCompact, SessionEnd, SessionStart events and `additionalContext` output |
| Anthropic API — Haiku | `claude-haiku-4-5-20251001` | Summarization in both hook scripts |
| `jq` | ≥1.6 | JSON parsing of hook stdin payload to extract `transcript_path`, `matcher` |
| `claude` CLI | current | Haiku invocation inside hooks (same binary as Claude Code) |
| `~/.claude/hooks/cortex-session-start.sh` | current | Must exist; this spec modifies it |
| `~/.claude/settings.json` | current | Must be writable; this spec modifies it |

---

## 7. Risks

- **Haiku call adds latency to PreCompact, blocking compaction** — Mitigation: write raw last-5-turns fallback if Haiku call exceeds 10s timeout; never block indefinitely; consider making snapshot write async with a post-compact fallback
- **SessionEnd doesn't fire on `/exit` command (known Claude Code bug, issue #35892)** — Mitigation: accept that `/exit` produces no summary for now; document the limitation; revisit when the bug is fixed upstream
- **session-memory.md grows stale if developer doesn't run sessions** — Mitigation: prepend timestamp header so developer sees age at a glance; content is overwritten on every new event so staleness is bounded by session frequency
- **Large transcripts exceed Haiku context window** — Mitigation: PreCompact uses last 30 turns only; SessionEnd uses last 100 turns with hard character-count truncation at 8,000 chars
- **Modifying settings.json breaks other hooks** — Mitigation: read and parse existing settings.json before writing; use atomic tmp+rename write; verify hook array structure before appending

---

## 8. Sequencing

1. **Write `precompact-session-snapshot.sh`** — implement transcript read, Haiku call, raw fallback, and write to `.cortex/session-memory.md`. Checkpoint: manually trigger compaction in a test session, verify file is created with correct schema.

2. **Register on PreCompact in `settings.json`** — add entry to PreCompact hook array. Checkpoint: hook fires on next compaction (verify via supervisor log or direct file check).

3. **Write `sessionend-session-summary.sh`** — implement transcript read, matcher check, Haiku call, and write to `.cortex/session-memory.md`. Checkpoint: end a test session normally, verify summary section written correctly.

4. **Register on SessionEnd in `settings.json`; move `cortex-session-end.sh` from Stop → SessionEnd** — update hook registrations atomically. Checkpoint: observe that `current-state.md` no longer rewrites on every agent turn.

5. **Modify `cortex-session-start.sh`** — add 5-10 lines to read `.cortex/session-memory.md` and inject into `additionalContext`. Checkpoint: start a new session after step 3; verify session-memory content appears in the session-start context (visible in system-reminder output).

6. **Add `.cortex/session-memory.md` to `.gitignore`** — Checkpoint: `git status` shows file as ignored.

---

## 9. Tasks

- [ ] Write `~/.claude/hooks/precompact-session-snapshot.sh` (Haiku call + raw fallback, writes snapshot schema to `.cortex/session-memory.md`)
- [ ] Write `~/.claude/hooks/sessionend-session-summary.sh` (matcher check, Haiku call, writes summary schema; handles `status: cleared` on matcher=="clear")
- [ ] Register `precompact-session-snapshot.sh` on PreCompact in `~/.claude/settings.json`
- [ ] Register `sessionend-session-summary.sh` on SessionEnd in `~/.claude/settings.json`
- [ ] Move `cortex-session-end.sh` from Stop hook to SessionEnd hook in `~/.claude/settings.json`
- [ ] Add session-memory read to `~/.claude/hooks/cortex-session-start.sh` (≤10 lines, budget-guarded at 800 chars)
- [ ] Add `.cortex/session-memory.md` to `.gitignore`
- [ ] Manual smoke test: trigger compaction → verify snapshot in session-memory.md
- [ ] Manual smoke test: exit session → verify summary in session-memory.md
- [ ] Manual smoke test: /clear → verify cleared status, no open thread
- [ ] Manual smoke test: start new session → verify session-memory content in additionalContext
