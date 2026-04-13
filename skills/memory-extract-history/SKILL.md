---
name: memory-extract-history
description: "Extracts unprocessed batches from ~/.claude/history.jsonl and writes inbox stubs to ~/memory/vault/inbox/. Idempotent via impressions-index. Trigger when the user says 'extract history', 'process history', 'pull from history', or uses /memory-extract-history."
---

# Memory Extract History — history.jsonl → Inbox Stubs

Parse `~/.claude/history.jsonl`, batch prompts into groups of 50 by session, and write one inbox stub per batch. Safe to re-run — the impressions index tracks the last processed line number and skips already-processed content.

## User-invocable

When the user types `/memory-extract-history`, run this skill.

Also trigger when the user says:
- "extract history"
- "process my history"
- "pull from history into inbox"
- "mine history.jsonl"

## Arguments

- `/memory-extract-history` — process all unprocessed history since last run
- `/memory-extract-history --batch-size <N>` — override batch size (default 50)

## Instructions

### Step 1: Read impressions index

```python
import json, os

INDEX_PATH = os.path.expanduser("~/memory/vault/impressions-index.json")
with open(INDEX_PATH) as f:
    index = json.load(f)

last_line = index["sources"].get("history.jsonl", {}).get("last_line", 0)
```

### Step 2: Read history.jsonl from last_line

`~/.claude/history.jsonl` — each line is a JSON object with at minimum a `display` field containing the user's prompt text. Read lines from `last_line` onward:

```python
HISTORY_PATH = os.path.expanduser("~/.claude/history.jsonl")
new_entries = []
with open(HISTORY_PATH) as f:
    for i, line in enumerate(f):
        if i < last_line:
            continue
        obj = json.loads(line.strip())
        display = obj.get("display", "").strip()
        if display:
            new_entries.append({
                "line": i,
                "display": display,
                "session": obj.get("sessionId", "unknown")
            })
```

If no new entries (len == 0), output:
```
No new history entries since last run (last_line: {last_line}).
```
and exit without writing any stubs.

### Step 3: Batch entries (50 per batch, grouped by session proximity)

Group consecutive entries into batches of `batch_size` (default 50). Do not split a session across batches if avoidable — if a session starts within the last 10 entries of a batch, carry those entries into the next batch:

```python
BATCH_SIZE = 50

batches = []
current_batch = []
for entry in new_entries:
    current_batch.append(entry)
    if len(current_batch) >= BATCH_SIZE:
        batches.append(current_batch)
        current_batch = []
if current_batch:
    batches.append(current_batch)
```

### Step 4: Write one inbox stub per batch

For each batch, write to `~/memory/vault/inbox/inbox-{ts}.md`:

```python
from datetime import datetime, timezone

for seq, batch in enumerate(batches, start=1):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stub_id = f"inbox-{ts}-{seq:03d}"
    
    # Build content: numbered prompts
    lines = [f"{i+1}. {e['display']}" for i, e in enumerate(batch)]
    content = "\n".join(lines)
    
    first_line = batch[0]["line"]
    last_line_num = batch[-1]["line"]
    
    stub = f"""---
id: {stub_id}
type: inbox-stub
status: inbox
source_type: history-batch
source_ref: "history.jsonl lines {first_line}–{last_line_num}"
fetched_at: {datetime.now(timezone.utc).isoformat()}
tags: []
---
{content}
"""
    path = os.path.expanduser(f"~/memory/vault/inbox/inbox-{ts}.md")
    with open(path, "w") as f:
        f.write(stub)
```

### Step 5: Update impressions index (atomic)

```python
TMP_PATH = INDEX_PATH + ".tmp"

new_last_line = new_entries[-1]["line"] + 1

index["sources"]["history.jsonl"] = {
    "type": "history",
    "last_line": new_last_line,
    "last_run": datetime.now(timezone.utc).isoformat(),
    "stubs_written": len(batches)
}

with open(TMP_PATH, "w") as f:
    json.dump(index, f, indent=2)
os.rename(TMP_PATH, INDEX_PATH)
```

### Step 6: Confirm

Output:
```
Extracted {len(new_entries)} history entries → {len(batches)} inbox stubs
Lines processed: {last_line} → {new_last_line}
Run /memory-consolidate to process inbox into candidates.
```

## Rules

- Idempotent: re-running produces no new stubs if no new history lines exist
- Never rewrite existing stubs — index tracks position, not file hashes
- Atomic index writes only (tmp + rename)
- Never modify history.jsonl — read-only source
- batch_size override is for testing only; default 50 is the production value
