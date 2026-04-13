---
name: memory-consolidate
description: "Reads unprocessed inbox stubs and new history lines since last run, calls Haiku consolidation, and writes structured candidate files to ~/memory/vault/candidates/. Idempotent via impressions-index. Trigger when the user says 'consolidate', 'run consolidation', 'process inbox', or uses /memory-consolidate."
---

# Memory Consolidate — Inbox + History → Haiku → Candidates

Read all unprocessed inbox stubs and new history since the last run, send batches to Haiku for pattern-finding, and write structured candidate files to `~/memory/vault/candidates/`. Safe to re-run — the impressions index prevents reprocessing.

## User-invocable

When the user types `/memory-consolidate`, run this skill.

Also trigger when the user says:
- "consolidate my memory"
- "run consolidation"
- "process inbox"
- "mine the inbox"
- "find patterns in my history"

## Arguments

- `/memory-consolidate` — process all unprocessed inbox stubs + new history
- `/memory-consolidate --inbox-only` — skip new history lines, only process inbox stubs
- `/memory-consolidate --dry-run` — print what would be processed without calling Haiku or writing files

## Instructions

### Step 1: Load impressions index

```python
import json, os

INDEX_PATH = os.path.expanduser("~/memory/vault/impressions-index.json")
with open(INDEX_PATH) as f:
    index = json.load(f)
```

### Step 2: Collect unprocessed inbox stubs

Read all `.md` files in `~/memory/vault/inbox/` where `status: inbox` (not already processed). Check that stub IDs are not already recorded in the index:

```python
import glob, re

INBOX_DIR = os.path.expanduser("~/memory/vault/inbox/")
processed_ids = set(index["sources"].keys())

stubs = []
for path in sorted(glob.glob(INBOX_DIR + "inbox-*.md")):
    stub_id = os.path.basename(path).replace(".md", "")
    if stub_id in processed_ids:
        continue  # already consolidated
    with open(path) as f:
        content = f.read()
    stubs.append({"id": stub_id, "path": path, "content": content})
```

### Step 3: Collect new history lines (unless --inbox-only)

```python
HISTORY_PATH = os.path.expanduser("~/.claude/history.jsonl")
history_state = index["sources"].get("history.jsonl", {})
last_consolidated_line = history_state.get("last_consolidated_line", 0)

new_history_entries = []
with open(HISTORY_PATH) as f:
    for i, line in enumerate(f):
        if i < last_consolidated_line:
            continue
        obj = json.loads(line.strip())
        display = obj.get("display", "").strip()
        if display and len(display) > 5:
            project = obj.get("project", "").split("/")[-1]
            new_history_entries.append(f"[{project}] {display}")
```

### Step 4: Prepare consolidation batches

Combine inbox content + new history into batches of ~1500 tokens (~6000 characters):

```python
MAX_BATCH_CHARS = 6000

all_text_items = []

# Add inbox stubs
for stub in stubs:
    all_text_items.append(("inbox", stub["id"], stub["content"]))

# Add history entries as a single block
if new_history_entries:
    history_text = "\n".join(f"{i+1}. {e}" for i, e in enumerate(new_history_entries))
    all_text_items.append(("history", "new-history-batch", history_text))

# Chunk into batches by character count
batches = []
current_batch = []
current_chars = 0
for item in all_text_items:
    item_chars = len(item[2])
    if current_chars + item_chars > MAX_BATCH_CHARS and current_batch:
        batches.append(current_batch)
        current_batch = []
        current_chars = 0
    current_batch.append(item)
    current_chars += item_chars

if current_batch:
    batches.append(current_batch)
```

If no stubs and no new history: output `Nothing to consolidate. Inbox is empty and no new history since last run.` and exit.

### Step 5: Call Haiku for each batch

```python
import anthropic

client = anthropic.Anthropic()

CONSOLIDATION_PROMPT = """You are a personal knowledge consolidation agent. You are given a batch of prompts and notes that a user sent to Claude Code across multiple sessions, plus any URL inbox stubs they staged.

Your task: identify 2-5 distinct insights, patterns, decisions, or recurring themes from this batch that would be worth remembering as personal knowledge. Ignore one-off tasks. Focus on patterns that reveal how this person thinks, what they're building, or decisions they've made.

For each insight, output EXACTLY this format (no preamble, no extra text):

---CANDIDATE---
pattern_type: decision|pattern|contradiction|open-loop
confidence: high|medium|low
suggested_vault_type: idea|project|task|decision
suggested_tags: [tag1, tag2]
what_was_found: [1-2 sentences: the pattern or insight]
evidence: [quoted fragment(s) from the batch that support this]
proposed_thread: [2-4 sentences ready to save as a vault note]
---END---

Batch:
{batch_content}"""

all_candidates = []

for batch in batches:
    batch_content = "\n\n".join(f"[SOURCE: {item[0]} / {item[1]}]\n{item[2]}" for item in batch)
    
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": CONSOLIDATION_PROMPT.format(batch_content=batch_content)
        }]
    )
    
    all_candidates.extend(parse_candidates(response.content[0].text))
```

### Step 5a: Parse candidate blocks

```python
import re

def parse_candidates(text: str) -> list[dict]:
    candidates = []
    blocks = re.findall(r"---CANDIDATE---(.*?)---END---", text, re.DOTALL)
    for block in blocks:
        c = {}
        for line in block.strip().splitlines():
            if ": " in line:
                key, _, val = line.partition(": ")
                c[key.strip()] = val.strip()
        if "what_was_found" in c:
            candidates.append(c)
    return candidates
```

### Step 6: Write candidate files

For each parsed candidate, write to `~/memory/vault/candidates/cand-{ts}-{seq:03d}.md`:

```python
from datetime import datetime, timezone

ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
ts_full = datetime.now(timezone.utc).isoformat()

for seq, cand in enumerate(all_candidates, start=1):
    cand_id = f"cand-{ts}-{seq:03d}"
    path = os.path.expanduser(f"~/memory/vault/candidates/{cand_id}.md")
    
    tags_raw = cand.get("suggested_tags", "[]")
    
    content = f"""---
id: {cand_id}
type: candidate
status: pending-review
consolidated_at: {ts_full}
reviewed_at: null
sources:
  - type: batch
    ref: "consolidation run {ts}"
pattern_type: {cand.get("pattern_type", "pattern")}
confidence: {cand.get("confidence", "medium")}
suggested_vault_type: {cand.get("suggested_vault_type", "idea")}
suggested_tags: {tags_raw}
---

## What was found
{cand.get("what_was_found", "")}

## Evidence
{cand.get("evidence", "")}

## Proposed thread
{cand.get("proposed_thread", "")}
"""
    with open(path, "w") as f:
        f.write(content)
```

### Step 7: Update impressions index (atomic)

```python
TMP_PATH = INDEX_PATH + ".tmp"

# Mark inbox stubs as consolidated
for stub in stubs:
    index["sources"][stub["id"]] = {
        **index["sources"].get(stub["id"], {}),
        "status": "consolidated",
        "consolidated_at": ts_full
    }

# Update history consolidation pointer
if new_history_entries:
    index["sources"].setdefault("history.jsonl", {})
    index["sources"]["history.jsonl"]["last_consolidated_line"] = (
        last_consolidated_line + len(new_history_entries)
    )

index["last_run"] = ts_full

with open(TMP_PATH, "w") as f:
    json.dump(index, f, indent=2)
os.rename(TMP_PATH, INDEX_PATH)
```

### Step 8: Confirm

```
Consolidation complete:
  Inbox stubs processed: {len(stubs)}
  New history entries:   {len(new_history_entries)}
  Candidates written:    {len(all_candidates)}
  
Run /memory-review-candidates to review.
```

## Rules

- Use `claude-haiku-4-5-20251001` — never Sonnet for consolidation
- Idempotent: re-running with no new inbox stubs and no new history exits without API calls
- Atomic index writes only (tmp + rename)
- Never modify inbox stubs — read-only source
- If Haiku returns zero candidates for a batch, log a warning but continue to next batch
- `--dry-run` prints batch sizes and skips all Haiku calls and file writes
