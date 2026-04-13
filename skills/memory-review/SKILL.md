---
name: memory-review
description: "Walks all active threads in the personal memory vault and forces a per-thread decision (advance/park/kill/promote). Trigger when the user says 'review my memory', 'walk the vault', 'do a memory review', 'review active threads', or uses /memory-review."
---

# Memory Review — The Collector's Fallacy Quality Gate

Walk every active thread in the memory vault and force a per-thread decision:
**advance**, **park**, **kill**, or **promote**. This is the primary quality
gate against the Collector's Fallacy failure mode documented in the concept
research. A vault that captures but is never reviewed becomes an unread
warehouse; this skill is the ritual that prevents that.

## User-invocable

When the user types `/memory-review`, run this skill.

Also trigger when the user says:
- "review my memory"
- "walk the memory vault"
- "do a memory review"
- "review active threads"

The SessionStart nudge hook (`sessionstart-memory-nudge.sh`) also prints a
banner when this review is overdue (>7 days). That banner is informational,
not a trigger.

## Arguments

- `/memory-review` — run the full review pass over all active threads
- `/memory-review --oldest-n <N>` — only review the N oldest-last_touched threads
- `/memory-review --type <type>` — only review threads of a specific type (rd|project|task|idea|reminder)
- `/memory-review --dry-run` — list what would be reviewed without prompting

## Instructions

### Phase 1: Load active threads

1. Vault root is `${MEMORY_VAULT:-$HOME/memory/vault}`.
2. Enumerate all files in `${VAULT}/raw/threads/*.md`.
3. For each file, read the YAML frontmatter.
4. Filter to threads with `status: active` AND `type` not equal to `derived` (unless `--type derived` flag was passed, in which case include ONLY `type: derived` threads).
5. If no active threads, print: "No active threads to review. Vault is at `${VAULT}`. Capture something with a new thread file in `raw/threads/`." and exit 0.

### Phase 2: Order the review queue

Sort active threads by `last_touched` ascending (oldest first). Oldest threads
have decayed the most and need the decision most urgently. Apply any filter
arguments (`--type`, `--oldest-n`).

### Phase 3: Review each thread

For each thread in order:

1. Print a compact header:
   ```
   ────────────────────────────────────────
   Thread: <id>
   Type: <type>  Status: active
   Created: <created>  Last touched: <last_touched> (<N> days ago)
   Tags: <tags>
   Summary: <summary>
   ────────────────────────────────────────
   ```

2. If the user asks to see the body, read the full file and print it. Otherwise, the summary from frontmatter is the default display.

3. Ask the user for exactly one decision:
   ```
   Decision? [advance/park/kill/promote/skip]
   ```

4. Apply the decision:

   - **advance** — The thread is still alive. Ask "What's the next step?" and append the user's answer to the thread body under a `## Next step` heading (create it if not present). Update frontmatter: `last_touched` to now, `touch_count` +1. Leave status=active. File stays in `raw/threads/`.

   - **park** — The thread is not dead but is not progressing. Move the file to `derived/parked/<filename>`. Update frontmatter: `status: parked`, `last_touched` now, `touch_count` +1. Parked threads can be revived by manually moving the file back to `raw/threads/`.

   - **kill** — The thread is over. Move the file to `derived/killed/<filename>`. Update frontmatter: `status: killed`, `last_touched` now. This is a tombstone, NOT a deletion — the file is preserved for history.

   - **promote** — Invoke `/memory-promote <id> <destination>` directly. Ask the user which destination: `cortex:<slug>`, `gsd:<phase>`, `todo`, `calendar`, or `archive`.

   - **skip** — Leave the thread as-is. Do not update `last_touched`. Move to the next thread. (Use sparingly — skipping defeats the purpose of the ritual.)

5. Append one line to `${VAULT}/compiled/review-log.md` for every non-skip decision:
   ```
   <iso-ts> review <id> <decision> [note]
   ```

### Phase 4: Regenerate INDEX

After the review pass completes:

1. Enumerate active threads again (some were parked/killed/promoted during this pass).
2. Rewrite `${VAULT}/INDEX.md` with one pointer line per active thread, sorted by `last_touched` descending (newest first). Format:
   ```
   - <id> [<type>] <summary>  (raw/threads/<filename>)
   ```
3. Keep the INDEX header block and any session-snapshot breadcrumbs appended by the PreCompact hook.

### Phase 5: Summary

Print a short summary of the review pass:
```
Review complete.
  Advanced:  N threads
  Parked:    N threads
  Killed:    N threads
  Promoted:  N threads
  Skipped:   N threads
  Total:     N threads reviewed in <duration>
```

## Rules

- Every active thread in scope must reach a decision. Skipping is allowed but discouraged and recorded (or rather, not recorded — skipped threads produce no review-log entry, which is itself a signal of review fatigue).
- The review ritual is the primary quality gate against the Collector's Fallacy. If this skill stops being run, the vault WILL become an unread warehouse. The `sessionstart-memory-nudge.sh` hook is the backstop that keeps this visible.
- Review never deletes a thread. Kill produces a tombstone in `derived/killed/`, not `/dev/null`.
- Review does not read or modify `.cortex/state.json`. Memory-layer operations are independent of Cortex slug state.
- Weekly cadence is the target but is not enforced mechanically — it is enforced by the roadmap Gate 5 dogfood check (3 days of real usage) and by the SessionStart nudge.
- Never extend the review ritual with additional decisions beyond {advance, park, kill, promote, skip}. The forcing-function value comes from the constrained decision set.
