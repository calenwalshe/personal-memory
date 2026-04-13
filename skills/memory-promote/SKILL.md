---
name: memory-promote
description: "Routes a vault thread to its graduation destination. Trigger when the user says 'promote this thread', 'graduate thread', 'route thread to cortex', 'archive thread', or uses /memory-promote."
---

# Memory Promote — Route a Thread to its Destination

Move a thread from `~/memory/vault/raw/threads/` to its graduation destination.
Updates frontmatter, moves the file to `derived/promoted/` (or `derived/archive/`),
and writes a routing record to the review log.

## User-invocable

When the user types `/memory-promote`, run this skill.

Also trigger when the user says:
- "promote this thread"
- "graduate thread <id>"
- "route thread <id> to cortex"
- "archive thread <id>"

## Arguments

- `/memory-promote <id> <destination>` — route to one of:
  - `cortex:<slug>` — emit a `/cortex-clarify` invocation with the thread body; does NOT run it
  - `gsd:<phase>` — append thread body to the target GSD phase NOTES.md
  - `todo` — emit a single task line the user can pipe into their todo system
  - `calendar` — emit a `g calendar add` invocation using the thread's `review_due` field
  - `archive` — move file to `derived/archive/` and set `status: parked`

## Instructions

### Phase 1: Resolve the thread

1. Vault root is `${MEMORY_VAULT:-$HOME/memory/vault}`.
2. Find the thread file by prefix-matching `<id>` against filenames in `raw/threads/`.
3. If zero matches, error: "No thread matching '<id>'. Run `ls ~/memory/vault/raw/threads/` to list active threads."
4. If multiple matches, print the list and ask the user to disambiguate with a longer id prefix.

### Phase 2: Update frontmatter

Edit the thread file's YAML frontmatter in place:

1. Set `status: promoted` (or `status: parked` for the `archive` destination).
2. Set `destination: "<destination>"` verbatim.
3. Set `last_touched` to the current ISO 8601 UTC timestamp.
4. Increment `touch_count` by 1.

Do not modify the body of the thread. Frontmatter only.

### Phase 3: Destination-specific side effects

- **`cortex:<slug>`** — print to stdout:
  ```
  /cortex-clarify "<first paragraph of thread body>"

  Source: ~/memory/vault/derived/promoted/<filename>
  ```
  Do NOT execute `/cortex-clarify` from this skill. The user runs it. This preserves the Cortex clarify-is-human-gated contract.

- **`gsd:<phase>`** — append the thread body (frontmatter stripped) to `.planning/phases/<phase>/NOTES.md`. Create the file with a `# Notes` header if missing.

- **`todo`** — print a single-line task to stdout:
  ```
  - [ ] <thread summary>  // src: <filename>
  ```
  The user pipes this into their todo system.

- **`calendar`** — if the thread's `review_due` frontmatter field is set, emit:
  ```
  g calendar add "<summary>" --date <review_due>
  ```
  Do NOT execute it. If `review_due` is unset, error: "Thread <id> has no review_due date. Edit the thread to set one, or use a different destination."

- **`archive`** — no side effects beyond the move.

### Phase 4: Move the file

1. `mv ${VAULT}/raw/threads/<filename> ${VAULT}/derived/promoted/<filename>` for all non-archive destinations.
2. `mv ${VAULT}/raw/threads/<filename> ${VAULT}/derived/archive/<filename>` for the archive destination.
3. Append a line to `${VAULT}/compiled/review-log.md`:
   ```
   <iso-ts> promoted <id> -> <destination>
   ```
4. Regenerate `${VAULT}/INDEX.md`: drop the promoted thread from the active-thread pointer list. Do not rewrite the whole file; remove only the pointer line matching the thread id.

## Rules

- Promotion is one-way in v1. To un-promote, the user manually moves the file back.
- Never delete a thread on promote. The file moves to `derived/`, never to `/dev/null`.
- Promotion does not read or modify `.cortex/state.json` — memory-layer operations are independent of Cortex slug state.
- `cortex:<slug>` promotion emits the `/cortex-clarify` invocation, does not run it.
- Failure after the frontmatter update but before the file move leaves the thread with an updated frontmatter but still in `raw/threads/`. This is recoverable: re-run the promote, which is idempotent by destination-already-matches check.
