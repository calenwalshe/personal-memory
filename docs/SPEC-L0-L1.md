# Memory Pipeline Spec — L0/L1

## Status: DRAFT v2
## Date: 2026-04-16

---

## Overview

The memory pipeline is a layered signal-processing stack. Raw annotated sense
data (L0) flows in continuously per tool call. A chunker aggregates L0 events
upward into atomic memory units (L1). L1 atoms feed multiplexed higher-level
aggregates (L2+, future).

The chunker is boundary-agnostic — it does not respect session, compaction, or
clear boundaries. Chunks are defined by content (intent shifts, entity overlap,
time gaps), not by process lifecycle. Infrastructure events like PreCompact and
/clear are merely processing triggers — opportunistic moments to run the chunker,
not boundaries that constrain it.

---

## L0 — Raw Sense Data

L0 is the annotated event stream. Already built, no changes needed.

### Streams

| Stream | Hook | Fires on | Table(s) |
|--------|------|----------|----------|
| Tool events | `postuse-event-logger.sh` | Every tool call (PostToolUse) | `events`, `event_content` |
| Messages | `sessionend-db-update.sh` | Session end | `messages` |
| Sequences | `sessionend-db-update.sh` | Session end | `sequences` |

### Event-specific captures
- **Git commits**: `postuse-git-episode.sh` — fires on `git commit` Bash calls
- **JSONL archive**: per-project-per-day in `vault/raw/event-log/`

### Annotations per tool event

Every L0 event carries a full SCAPE stimulus compound:

| Field | What it is |
|-------|-----------|
| `tool_name` | What tool was called |
| `tool_input_preview` / `tool_input_full` | The stimulus (what was acted on) |
| `tool_response_preview` / `tool_response_full` | The outcome |
| `purpose_preview` / `purpose_full` | Last user message — the intent driving this call |
| `had_error`, `error_type` | Whether it failed and how |
| `is_retry` | Whether this is a repeat of a previous attempt |
| `sequence_n` | Position within the session |
| `timestamp` | When it happened |
| `project`, `git_branch`, `git_sha`, `cwd` | Spatial context |
| `session_id` | Which session (but NOT a chunking boundary) |

### L0 health
- events.db is WAL-mode, async writes, never blocks responses
- Annotations computed at write time (no deferred processing)
- Full untruncated payloads in `event_content`

---

## L1 — Atomic Memory Units

### What an L1 atom is

The smallest coherent memory unit. One thing happened, was learned, was decided,
or failed. Produced by aggregating 1-N L0 events that belong together.

Properties:
- **Atomic** — cannot be meaningfully split further
- **Self-contained** — readable without joining back to events.db
- **Typed** — tagged with what kind of memory it is
- **Provenanced** — rich metadata package, traceable to exact L0 events
- **Boundary-agnostic** — may span compaction, clear, or session boundaries

### Atom types

| Type | What it captures | Example |
|------|-----------------|---------|
| `decision` | A choice made and why | "Chose flock-based worker over per-session subprocess to prevent concurrent LLM calls" |
| `discovery` | Something learned about how a system works | "FAISS IndexFlatIP requires L2-normalized vectors for cosine similarity" |
| `failure` | Something tried that didn't work | "Playwright automation of ChatGPT blocked by Cloudflare Turnstile" |
| `pattern` | A reusable approach or rule of thumb | "After editing Caddyfile, must restart container because Edit changes inode" |
| `gotcha` | A non-obvious trap or pitfall | "git commit --amend after a hook failure destroys the previous commit" |
| `outcome` | A concrete result or state change | "Deployed memory-viewer to memory.calenwalshe.com with systemd service" |

### Atom schema

```sql
CREATE TABLE atoms (
    id              TEXT PRIMARY KEY,       -- uuid
    content         TEXT NOT NULL,          -- 1-2 sentence description
    atom_type       TEXT NOT NULL,          -- decision|discovery|failure|pattern|gotcha|outcome

    -- Provenance package (denormalized, self-contained)
    project         TEXT NOT NULL,          -- chunking key (NOT session_id)
    source_events   TEXT NOT NULL,          -- JSON array of L0 event_ids
    source_count    INTEGER NOT NULL,       -- number of L0 events bundled
    session_ids     TEXT NOT NULL,          -- JSON array — atom may span sessions
    time_first      TEXT NOT NULL,          -- ISO timestamp of earliest source event
    time_last       TEXT NOT NULL,          -- ISO timestamp of latest source event
    duration_s      REAL,                   -- time_last - time_first in seconds
    git_branch      TEXT,                   -- branch at time of earliest event
    git_sha         TEXT,                   -- sha at time of earliest event
    trigger         TEXT,                   -- user intent that started this work
    tools_used      TEXT,                   -- JSON array of distinct tool names
    had_errors      INTEGER DEFAULT 0,      -- 1 if any source event had errors
    retry_count     INTEGER DEFAULT 0,      -- total retries across source events
    files_touched   TEXT,                   -- JSON array of file paths involved

    -- Classification
    entities        TEXT,                   -- JSON array of entity names
    topic           TEXT,                   -- short label
    confidence      REAL DEFAULT 0.7,       -- Haiku's confidence in this atom
    importance      REAL DEFAULT 0.5,       -- Haiku's importance rating

    -- Lifecycle
    created_at      TEXT NOT NULL,          -- when this atom was produced
    invalidated_by  TEXT DEFAULT NULL       -- id of atom that supersedes this
);

CREATE INDEX idx_atoms_project ON atoms(project);
CREATE INDEX idx_atoms_type ON atoms(atom_type);
CREATE INDEX idx_atoms_time ON atoms(time_first);
CREATE INDEX idx_atoms_topic ON atoms(topic);
```

Note: `session_id` is not a singular field — an atom can span multiple sessions.
The `session_ids` array records all sessions whose events contributed.
The chunking key is `project`, not `session_id`.

### Provenance assembly

For each atom, the provenance package is assembled from L0 annotations.
No inference needed — every field is a direct lookup or aggregation:

```python
def assemble_provenance(source_events: list[Event]) -> dict:
    return {
        "project":       source_events[0].project,
        "source_events": [e.event_id for e in source_events],
        "source_count":  len(source_events),
        "session_ids":   dedupe([e.session_id for e in source_events]),
        "time_first":    min(e.timestamp for e in source_events),
        "time_last":     max(e.timestamp for e in source_events),
        "duration_s":    (max_ts - min_ts).total_seconds(),
        "git_branch":    source_events[0].git_branch,
        "git_sha":       source_events[0].git_sha,
        "trigger":       first_purpose_message(source_events),
        "tools_used":    dedupe([e.tool_name for e in source_events]),
        "had_errors":    any(e.had_error for e in source_events),
        "retry_count":   sum(e.is_retry for e in source_events),
        "files_touched": extract_file_paths(source_events),
    }
```

---

## Chunker

### Design principles

1. **Chunks are content-driven, not boundary-driven.** Session ends,
   compactions, and /clear events are processing triggers — they tell the
   chunker "now is a good time to run." They are NOT chunk boundaries. A
   single atom may span compaction windows or even session boundaries if
   the same coherent work continued.

2. **The chunking key is project, not session.** If the user closes a session
   on `yt_dj` mid-task and opens a new one on the same project, the chunker
   sees those events as a continuous stream. Different session_ids, same project,
   same work.

3. **Clusters stay open by default.** The chunker is lazy about closing clusters.
   A cluster is only closed when there is a positive close signal. Ambiguous
   clusters are carried forward to the next run as open state.

4. **Haiku refines, it doesn't discover.** Heuristic pre-clustering does the
   heavy lifting. Haiku confirms or adjusts boundaries and produces the atom
   content. This keeps Haiku calls small and fast.

### Hard close signals

These always close the current cluster:

| Signal | Why |
|--------|-----|
| **Project change** | User switched to a different project directory |
| **Long time gap** | > 2 hours between consecutive events — context has shifted |
| **Explicit /clear** | User declared "I'm done with this topic" |

### Soft close signals

These suggest a boundary but the chunker holds the cluster open as provisional.
If the next run's events continue the same work, the cluster reopens and merges.

| Signal | Why |
|--------|-----|
| **New user intent** | New user message with different topic — but might be a sub-task |
| **Entity set shift** | Files/tools touched change — but might be exploring |
| **Compaction occurred** | Context was compressed — but work likely continues |

### Chunker state

The chunker maintains persistent state between runs, stored in events.db:

```sql
CREATE TABLE chunker_state (
    project         TEXT PRIMARY KEY,
    cursor_event_id TEXT NOT NULL,       -- last L0 event_id processed
    cursor_timestamp TEXT NOT NULL,      -- timestamp of that event
    open_clusters   TEXT NOT NULL,       -- JSON: provisional clusters not yet closed
    updated_at      TEXT NOT NULL
);
```

`open_clusters` is a JSON array of cluster objects:

```json
[
  {
    "event_ids": ["sess1:14", "sess1:15", "sess1:16"],
    "first_timestamp": "2026-04-16T03:21:00Z",
    "last_timestamp": "2026-04-16T03:24:30Z",
    "entity_set": ["Caddyfile", "docker"],
    "purpose_preview": "fix the caddy deploy hook",
    "status": "provisional"
  }
]
```

### Algorithm

#### On trigger (PreCompact, /clear, or real session end):

```
1. LOAD chunker state for this project
   - cursor: where we left off
   - open_clusters: provisional clusters from last run

2. READ new L0 events since cursor, ordered by timestamp
   - Source: events table WHERE project = ? AND timestamp > cursor
   - Filter out noise: skip events where tool_name IN (Read, Glob, Grep)
     AND no follow-up action within 30s (orientation reads, not productive work)

3. PRE-CLUSTER new events
   for each new event:
       if HARD CLOSE signal detected:
           close all open clusters → finalize queue
           start fresh cluster with this event
       elif SOFT CLOSE signal detected:
           mark current cluster as provisional
           start new cluster with this event
       else:
           append event to current open cluster

       # Size guard: if cluster exceeds 15 events, force close
       if current cluster size >= 15:
           close current cluster → finalize queue
           start new cluster

4. MERGE check: examine tail of last finalized cluster against
   head of first open cluster. If they share entities/purpose and
   time gap < 30 min, merge them back into one cluster.

5. FINALIZE closed clusters via Haiku
   Batch closed clusters in groups of 3-5. For each batch:

   Present to Haiku:
   - The events in each cluster (tool_name, input_preview, response_preview,
     purpose_preview, had_error, is_retry, timestamp)
   - Adjacent cluster context (so Haiku can suggest merges/splits)

   Haiku returns:
   - Confirmed/adjusted cluster boundaries
   - One atom per cluster (content, type, entities, confidence, importance)
   - Or: "drop" for clusters with no memorable content

6. ASSEMBLE provenance for each atom from source L0 events

7. WRITE atoms to atoms.db + update FAISS index

8. SAVE chunker state
   - Advance cursor to latest processed event
   - Save remaining open/provisional clusters
```

#### Noise filtering (Step 2)

Not all L0 events are worth chunking. Filter before pre-clustering:

| Keep | Drop |
|------|------|
| Bash commands (real actions) | Lone Read/Glob/Grep with no follow-up write/bash |
| Write/Edit (code changes) | ToolSearch (internal plumbing) |
| Tool calls that error (signal!) | Skill invocations that are just prompt injection |
| Retries (signal!) | System message tool_results |

The filter is conservative — when in doubt, keep the event. Better to let
Haiku drop a noisy cluster than to silently lose a meaningful one.

#### Haiku prompt (Step 5)

```
You are bundling raw tool-call events into atomic memory units.

Each event shows: tool_name, input (truncated), response (truncated),
user intent (purpose), error/retry status, timestamp.

For each cluster of events:
1. Decide: is this one coherent memory, or should it be split/merged/dropped?
2. If it's a memory, produce ONE atom.

Atom types: decision, discovery, failure, pattern, gotcha, outcome

Rules:
- An atom is ONE thing. If a cluster contains two distinct things, split it.
- Adjacent clusters that are clearly one thing (retry→success, explore→decide)
  should be merged.
- Drop clusters that contain no memorable content: routine navigation,
  re-reading files for orientation, context-loading skill prompts.
- Be specific in the content: name the tool, file, project, service, or outcome.
- 1-2 sentences max per atom.

Respond with a JSON array:
[
  {
    "content": "one sentence describing what happened or was learned",
    "atom_type": "decision|discovery|failure|pattern|gotcha|outcome",
    "source_cluster_indices": [0, 1],   // which input clusters this covers
    "entities": ["Entity1", "Entity2"],
    "topic": "short-label",
    "confidence": 0.0-1.0,
    "importance": 0.0-1.0
  }
]

Return empty array [] if no clusters contain memorable content.
```

---

## Processing triggers

### What triggers the chunker

| Event | Hook | Condition | Why it's a good trigger |
|-------|------|-----------|------------------------|
| PreCompact (auto) | `precompact-session-snapshot.sh` | Always | Context window full = real work happened |
| /clear | `sessionend-session-summary.sh` | `matcher == "clear"` | Explicit user intent boundary |
| Real session end | `sessionend-session-summary.sh` | `event_count > 5` for this session | Productive session finishing |

### What does NOT trigger the chunker

| Event | Why not |
|-------|---------|
| SessionEnd with 0 events | Subprocess noise — 94% of all sessions |
| SessionEnd with < 5 events | Too little data to produce meaningful atoms |
| PostToolUse | Too frequent, would be per-call. L0 logger handles this layer. |
| SessionStart | Nothing has happened yet |

### Trigger implementation

The existing hooks add one line to invoke the chunker:

```bash
# In precompact-session-snapshot.sh and sessionend-session-summary.sh:
# After existing logic, kick the chunker if there's data

VAULT_BIN="${HOME}/.local/bin/vault"
[[ ! -x "$VAULT_BIN" ]] && VAULT_BIN="${HOME}/memory/vault/bin/vault"
if [[ -x "$VAULT_BIN" ]]; then
    PROJECT=$(basename "${CLAUDE_PROJECT_DIR:-}" 2>/dev/null || echo "")
    if [[ -n "$PROJECT" ]]; then
        nohup "$VAULT_BIN" chunk "$PROJECT" >> /tmp/vault-chunker.log 2>&1 &
        disown $!
    fi
fi
```

The chunker uses flock internally (same pattern as the promotion worker) so
concurrent triggers are safe — the second invocation exits immediately.

---

## Relationship to existing pipeline

### What stays unchanged
- All L0 hooks (event logger, git episode writer, JSONL archiver)
- L1 episode .md files (Stage 2 hooks still write them — useful for human reading)
- `vault` CLI (gains `chunk` and `atoms` subcommands)
- FAISS index infrastructure (rebuilt from atoms instead of facts)
- `/recall` skill (queries atoms via FAISS)
- `vault context` command (reads atoms instead of facts)

### What is replaced
- `extractor.py` → `chunker.py` (reads L0 events directly, not episode summaries)
- `fact_store.py` → `atom_store.py` (new schema, batch writes, same FAISS pattern)
- `facts.db` → `atoms.db` (new file; old facts.db preserved, not deleted)
- Promotion queue/worker → chunk queue/worker (same flock pattern, different input)

### What is removed
- `EPISODIC_PROMPT` / `PROCEDURAL_PROMPT` (the two-pass session-summary extraction)
- The distinction between "episodic from episodes" and "procedural from messages"
- Session-bounded processing assumption

---

## Vault CLI changes

New subcommands:

```
vault chunk <project>           # run chunker for project (reads L0, produces atoms)
vault atoms [project] [-n N]    # list recent atoms
vault atoms show <atom-id>      # full atom with provenance
vault atoms search <query>      # FAISS semantic search over atoms
vault atoms stats               # atom counts by project, type, time
```

The `vault context`, `vault recall`, and `vault search` commands switch
from reading `facts.db` to reading `atoms.db`.

---

## L2+ (future, not in this refactor)

L1 atoms feed multiplexed L2 aggregates along different dimensions:

| Dimension | Aggregation | Output |
|-----------|-------------|--------|
| Project | All atoms for project X | Project state model |
| Entity | All atoms mentioning entity Y | Entity knowledge card |
| Pattern | All `pattern` + `gotcha` atoms | Operational playbook |
| Time | Recent atoms (decay-weighted) | Working memory set |
| Recurrence | Atoms whose content recurs across N+ sessions | Durable knowledge |

The same L1 atom participates in multiple L2 aggregates simultaneously.
L2 design is deferred. This spec covers L0 → L1 only.

---

## Acceptance criteria

1. **Direct provenance**: L1 atoms trace to exact L0 event_ids, not intermediate summaries
2. **Content-driven boundaries**: chunks are defined by intent/entity/time, not session/compaction boundaries
3. **Cross-boundary spanning**: atoms may span compaction windows and session boundaries within a project
4. **Lazy closing**: clusters stay open/provisional until a positive close signal is confirmed by the next run
5. **Rich provenance**: every atom carries time range, git context, trigger, tools used, files touched, error/retry signals — all denormalized, self-contained
6. **Project-keyed**: chunker state is per-project, not per-session
7. **Noise filtering**: orientation reads, subprocess sessions, and context-loading events are filtered before clustering
8. **Safe concurrency**: chunker uses flock, concurrent triggers are harmless
9. **Backward compatibility**: existing L0 hooks unchanged, old facts.db preserved, episode .md files still written
10. **Consumer integration**: `vault context`, `/recall`, `vault search` read from atoms.db
