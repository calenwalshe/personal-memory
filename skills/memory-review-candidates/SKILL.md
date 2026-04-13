---
name: memory-review-candidates
description: "Walks pending candidates in ~/memory/vault/candidates/ one at a time. Approve writes a vault thread; reject marks it rejected; edit lets the user modify before writing; skip defers to next session; quit preserves state. Hard cap: 10 per session. Trigger when the user says 'review candidates', 'review my candidates', or uses /memory-review-candidates."
---

# Memory Review Candidates — Human Gate for Vault Promotion

Walk all `status: pending-review` candidates one at a time. The user approves, rejects, edits, skips, or quits. Approved candidates become vault threads. Hard cap: 10 candidates per session to respect cognitive load.

## User-invocable

When the user types `/memory-review-candidates`, run this skill.

Also trigger when the user says:
- "review candidates"
- "review my candidates"
- "go through candidates"
- "what candidates do I have?"
- "show me the candidates"

## Arguments

- `/memory-review-candidates` — review up to 10 pending candidates
- `/memory-review-candidates --all` — show count of all pending candidates (does not start review)

## Instructions

### Step 1: Load pending candidates

```python
import glob, re, os

CANDIDATES_DIR = os.path.expanduser("~/memory/vault/candidates/")
candidates = []

for path in sorted(glob.glob(CANDIDATES_DIR + "cand-*.md")):
    with open(path) as f:
        content = f.read()
    
    # Parse frontmatter
    fm_match = re.match(r"^---\n(.*?)\n---\n(.*)", content, re.DOTALL)
    if not fm_match:
        continue
    
    import yaml
    try:
        fm = yaml.safe_load(fm_match.group(1))
    except Exception:
        continue
    
    if fm.get("status") != "pending-review":
        continue
    
    # Parse body sections
    body = fm_match.group(2)
    sections = {}
    for section_name in ["What was found", "Evidence", "Proposed thread"]:
        m = re.search(rf"## {section_name}\n(.*?)(?=\n## |\Z)", body, re.DOTALL)
        sections[section_name] = m.group(1).strip() if m else ""
    
    candidates.append({
        "path": path,
        "id": fm.get("id", ""),
        "frontmatter": fm,
        "sections": sections,
        "raw_content": content
    })
```

If `--all` flag:
```
{len(candidates)} pending candidates in ~/memory/vault/candidates/
Run /memory-review-candidates to begin review.
```
Exit.

If no pending candidates:
```
No pending candidates. Run /memory-consolidate to generate candidates from your inbox.
```
Exit.

### Step 2: Apply session cap

```python
session_cap = 10
review_batch = candidates[:session_cap]
total_pending = len(candidates)
```

If `total_pending > session_cap`:
```
{total_pending} pending candidates — reviewing first {session_cap} this session.
```

### Step 3: Review loop (one candidate at a time)

For each candidate (index `i` from 0):

**Display the candidate:**

```
════════════════════════════════════════
Candidate {i+1} of {len(review_batch)} (total pending: {total_pending})
ID: {cand['id']}
Pattern: {fm['pattern_type']} | Confidence: {fm['confidence']} | Type: {fm['suggested_vault_type']}
Tags: {fm['suggested_tags']}
════════════════════════════════════════

## What was found
{sections['What was found']}

## Evidence
{sections['Evidence']}

## Proposed thread
{sections['Proposed thread']}

────────────────────────────────────────
[approve] [reject] [edit] [skip] [quit]
```

**Wait for user response. Handle these decisions:**

#### approve
Write the proposed thread as a vault thread:

```python
from datetime import datetime, timezone

now = datetime.now(timezone.utc)
ts = now.strftime("%Y%m%dT%H%M%SZ")
ts_full = now.isoformat()

# Build vault thread slug from what_was_found (first 6 words, hyphenated)
what = sections['What was found']
words = re.sub(r'[^\w\s]', '', what).lower().split()[:6]
slug = "-".join(words)

thread_filename = f"{ts}-{slug}.md"
thread_path = os.path.expanduser(f"~/memory/vault/raw/threads/{thread_filename}")

thread_content = f"""---
id: "{ts}"
type: {fm.get("suggested_vault_type", "idea")}
status: active
created: "{ts_full}"
last_touched: "{ts_full}"
touch_count: 1
project: null
tags: {fm.get("suggested_tags", [])}
destination: null
summary: "{what[:100].replace(chr(34), chr(39))}"
parent: null
children: []
review_due: null
decay_after_days: 90
source: candidate
source_candidate: {fm.get("id", "")}
---

# {what[:80]}

{sections['Proposed thread']}

## Evidence
{sections['Evidence']}
"""

with open(thread_path, "w") as f:
    f.write(thread_content)
```

Append to INDEX.md:
```python
INDEX_PATH = os.path.expanduser("~/memory/vault/INDEX.md")
pointer = f"- {ts} [{fm.get('suggested_vault_type', 'idea')}] {what[:80]}  ({thread_filename})\n"
with open(INDEX_PATH, "a") as f:
    f.write(pointer)
```

Mark candidate as approved:
```python
update_candidate_status(cand["path"], "approved", reviewed_at=ts_full)
```

Output: `✓ Written: ~/memory/vault/raw/threads/{thread_filename}`

#### reject
Mark candidate as rejected. No vault write.

```python
update_candidate_status(cand["path"], "rejected", reviewed_at=ts_full)
```

Output: `✗ Rejected: {cand['id']}`

#### edit \<instruction\>
Accept natural language edit instruction (e.g., "trim to the first sentence", "change the type to decision", "add tag: cortex").

Apply the edit instruction to the proposed thread section (or frontmatter field if the instruction targets one). Show the modified proposed thread:

```
Modified proposed thread:
─────────────────────────
{modified_text}
─────────────────────────
Write this? [yes / no / edit again]
```

If `yes`: write vault thread with modified content (same approve flow as above).
If `no`: return to the candidate display without writing.
If `edit again`: accept another natural language edit instruction.

#### skip
Leave candidate `status: pending-review`. It will appear in the next session.

Output: `→ Skipped: {cand['id']} (will reappear next session)`

#### quit
Exit the review loop. All remaining candidates stay `status: pending-review`.

Output:
```
Review session ended.
Reviewed: {i} of {len(review_batch)}
Remaining pending: {total_pending - i} candidates
Run /memory-review-candidates to continue.
```
Exit.

### Step 4: Update candidate status helper

```python
def update_candidate_status(path: str, status: str, reviewed_at: str = None):
    with open(path) as f:
        content = f.read()
    
    content = re.sub(r"^status: .*$", f"status: {status}", content, flags=re.MULTILINE)
    if reviewed_at:
        content = re.sub(r"^reviewed_at: .*$", f"reviewed_at: {reviewed_at}", content, flags=re.MULTILINE)
    
    with open(path, "w") as f:
        f.write(content)
```

### Step 5: End-of-session summary

After all candidates in the batch are reviewed (or quit):

```
════════════════════════════════════════
Session complete.
  Approved:  {approved_count}
  Rejected:  {rejected_count}
  Skipped:   {skipped_count}
  Remaining: {total_pending - reviewed_count} candidates pending
════════════════════════════════════════
```

## Rules

- Hard cap: never present more than 10 candidates in one session
- One candidate at a time — never batch display
- Always show progress counter ("Candidate N of M")
- Edit requires confirmation before writing to vault — never write without user seeing the modified text
- Quit preserves all state — no data loss
- Approved candidates write to `raw/threads/` AND append to `INDEX.md`
- Rejected candidates set `status: rejected` only — never deleted
- Never modify existing vault threads from this skill
- `suggested_vault_type` from the candidate becomes the thread's `type` field
