---
name: memory-compile
description: "Compiles active vault threads into linked concept pages in compiled/. Trigger when the user says 'compile the vault', 'compile my memory', 'run a compile pass', or uses /memory-compile. GATED: do not run before Gate 5 dogfood verdict (2026-04-13)."
---

# Memory Compile — Synthesize Threads into Linked Wiki Pages

Incrementally compiles active threads from `raw/threads/` into structured concept pages in `compiled/`. Produces summaries, tag-based concept pages, and backlinks — the Karpathy compile step. Knowledge compounds rather than just accumulates.

**Gate 5 required.** This skill should not be run until the dogfood verdict (on or after 2026-04-13). Use `--force` to override if Gate 5 has passed.

## User-invocable

When the user types `/memory-compile`, run this skill.

Also trigger when the user says:
- "compile the vault"
- "compile my memory"
- "run a compile pass"
- "synthesize my threads"

## Arguments

- `/memory-compile` — incremental compile (only threads newer than last compile)
- `/memory-compile --full` — full recompile of all active threads
- `/memory-compile --force` — bypass Gate 5 gate notice and proceed

## Instructions

### Phase 1: Gate check

1. Print the gate notice unless `--force` was passed:

   ```
   ⚠  Gate 5 required
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   /memory-compile should not run before the dogfood
   verdict (Gate 5, fires on or after 2026-04-13).

   Running compile before Gate 5 biases the measurement:
   it makes the vault more useful before you've answered
   whether you actually reach for it during real work.

   If Gate 5 has passed, re-run with --force to proceed.
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   ```

   If `--force` was NOT passed: stop here. Do not proceed.
   If `--force` was passed: continue to Phase 2.

2. Resolve vault root: `VAULT="${MEMORY_VAULT:-$HOME/memory/vault}"`
3. Verify vault exists. If `${VAULT}/INDEX.md` is absent: error "Vault not found."

### Phase 2: Determine compile scope

1. Read `${VAULT}/compiled/.last-compile` if it exists (plain ISO 8601 UTC timestamp string).
2. If `--full` flag or no `.last-compile` file: compile scope = all active threads.
3. Otherwise: compile scope = threads where `last_touched` > `.last-compile` timestamp.
4. Enumerate `${VAULT}/raw/threads/*.md`. Filter to `status: active`. Apply scope filter.
5. If zero threads in scope: print "Nothing to compile — all threads are up to date." and exit 0.

### Phase 3: Compile each thread

For each thread in scope:

1. Read the full thread file. Extract: `id`, `type`, `tags`, `summary`, and body text.

2. **Write summary page:**
   Write or overwrite `${VAULT}/compiled/summaries/${id}-summary.md`:
   ```yaml
   ---
   title: "<summary>"
   type: source-summary
   sources: ["raw/threads/<filename>"]
   related: []
   created: <today>
   updated: <today>
   confidence: medium
   ---
   ```
   Body: one paragraph synthesizing the thread body into a third-person summary. If thread body is empty, use the summary field as the synthesis.

3. **Update concept pages (by-tag):**
   For each tag in the thread's `tags` array:
   - If `${VAULT}/compiled/by-tag/<tag>.md` does not exist: create it with frontmatter (`title: <tag>`, `type: concept`, `sources: []`, `related: []`, `created: today`, `updated: today`, `confidence: medium`) and an empty `## Threads` section.
   - Append a backlink line under `## Threads` if not already present:
     ```
     - [[<id>]] — <summary>
     ```
   - Update the `sources` frontmatter field to include `raw/threads/<filename>` if not already present.
   - Update `updated` to today.

### Phase 4: Update compiled index

Write `${VAULT}/compiled/INDEX-compiled.md`:

```markdown
# Compiled Index

Last compiled: <ISO 8601 UTC>

## Concept Pages (by-tag)

<one line per file in compiled/by-tag/, sorted alphabetically>
- [[<tag>]] — <N> threads, last updated <date>

## Summaries

<count> thread summaries in compiled/summaries/
```

### Phase 5: Write .last-compile timestamp

Write current UTC timestamp (ISO 8601) to `${VAULT}/compiled/.last-compile`.

### Phase 6: Print compile report

```
Compile complete.
  Threads processed: N
  Summaries written: N
  Concept pages updated: N
  Concept pages created: N
  Index updated: compiled/INDEX-compiled.md
```

## Rules

- Never modify files in `raw/` — compilation is read-only on the raw layer.
- Never edit thread frontmatter during compile — summaries and concept pages are the output layer.
- Compilation is idempotent: running twice produces the same result (overwrite summaries, deduplicate backlinks).
- The `--force` flag bypasses the Gate 5 notice but does not disable it permanently. The notice reappears on the next invocation without `--force`.
- `compiled/` files are LLM-owned. Do not manually edit them — they will be overwritten on the next compile pass.
- Zero threads to compile is a valid and expected outcome (exit 0, no error).
