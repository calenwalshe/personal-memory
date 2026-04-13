---
name: memory-capture
description: "Captures a thought, idea, or Claude output into the personal memory vault (~/memory/vault/raw/threads/). Trigger when the user says 'save this', 'capture this', 'remember this', 'add to vault', 'file this back', or uses /memory-capture."
---

# Memory Capture — Write a Thread to the Vault

Capture a thought, decision, or Claude output as a vault thread. Generates all frontmatter automatically — the user provides only the summary. Supports bidirectional filing via `--derived` for capturing Claude's own outputs back into the vault.

## User-invocable

When the user types `/memory-capture`, run this skill.

Also trigger when the user says:
- "save this thread: [summary]"
- "capture this: [text]"
- "remember this: [text]"
- "add to vault: [text]"
- "file this back" or "file this back to vault"

## Arguments

- `/memory-capture "<summary>"` — capture a user thought as `type: idea`
- `/memory-capture "<summary>" --type <type>` — override type (idea|task|project|reminder|rd)
- `/memory-capture "<summary>" --derived` — capture a Claude output as `type: derived`
- `/memory-capture "<summary>" --tags <tag1,tag2>` — set tags
- `/memory-capture "<summary>" --project <project>` — set project context
- `/memory-capture "<summary>" --review-due <YYYY-MM-DD>` — set review due date

## Instructions

### Phase 1: Parse input

1. Extract summary from the argument. If no argument provided and a natural language trigger was used, extract the summary from the user's message. If no summary can be inferred, ask once: "What should I capture?"
2. Determine `type`:
   - If `--derived` flag is present: `type = derived`
   - If `--type <type>` is present: use that value
   - Otherwise: `type = idea`
3. Generate `id` as current UTC timestamp in compact format: `YYYYMMDDTHHMMSSz` (no colons, no dashes, Z suffix). Example: `20260410T183000Z`.
4. Slugify the summary for the filename: lowercase, replace spaces and non-alphanumeric with hyphens, collapse consecutive hyphens, strip leading/trailing hyphens, truncate to 40 characters.
5. Construct filename: `${id}-${slug}.md`
6. Check for duplicate: if a file in `${VAULT}/raw/threads/` already has a summary field matching this summary (case-insensitive), warn the user and ask whether to proceed or cancel. Do not silently create duplicates.

### Phase 2: Build frontmatter

Resolve vault root: `VAULT="${MEMORY_VAULT:-$HOME/memory/vault}"`

Generate the full frontmatter block:

```yaml
---
id: "<id>"
type: <type>
status: active
summary: "<summary verbatim>"
created: "<ISO 8601 UTC — e.g. 2026-04-10T18:30:00Z>"
last_touched: "<same as created>"
touch_count: 1
project: <value of --project or null>
tags: [<values of --tags or empty array>]
parent: null
children: []
review_due: <value of --review-due, or for type:derived use 14 days from today, or null>
destination: null
source_thread: <for type:derived: one-sentence description of what Claude output this came from; for all other types: null>
---
```

For `type: derived`, set `source_thread` to a brief description of the originating Claude output (e.g., `"research dossier: memory-layer-improvements implementation phase"`, `"cortex-fit analysis for memory-layer-improvements"`). Ask the user if context is ambiguous.

### Phase 3: Write thread file

Write the full file (frontmatter + blank line + any body text the user provided) to:

```
${VAULT}/raw/threads/${id}-${slug}.md
```

If body text was provided alongside the summary, append it after the frontmatter under a `## Notes` heading. If only a summary was provided, leave the body empty (frontmatter only).

### Phase 4: Update INDEX.md

Append one pointer line to `${VAULT}/INDEX.md` under the `## Active Threads` section:

```
- <id> [<type>] <summary>  (raw/threads/<filename>)
```

Do NOT regenerate the full INDEX — append only. Full regeneration is `/memory-review`'s job (Phase 4).

### Phase 5: Log the capture

Append one line to `${VAULT}/compiled/review-log.md`:

```
<ISO-8601-UTC> capture <id> type=<type>
```

### Phase 6: Confirm

Print a short confirmation:

```
Captured: <id>
File: ~/memory/vault/raw/threads/<filename>
Type: <type>
Summary: <summary>
```

## Rules

- Never prompt the user to write frontmatter manually. Claude generates all fields.
- `type: derived` threads have a 14-day default `review_due` (shorter lifecycle — they're already synthesized).
- Capture does not read or modify `.cortex/state.json`. The vault is independent of Cortex.
- `source_thread` is informational only in v1.1 — no referential integrity enforcement.
- Never create duplicate threads. Check for matching summary before writing; warn and confirm if found.
- Capture is append-only. Never overwrite an existing thread file.
- If the vault root does not exist (`~/memory/vault/` missing), error: "Vault not found at ${VAULT}. Run the memory-layer setup first."
