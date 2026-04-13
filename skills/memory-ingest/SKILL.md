---
name: memory-ingest
description: "Fetches a URL and writes an inbox stub to ~/memory/vault/inbox/ with provenance frontmatter. Trigger when the user says 'ingest this URL', 'add to inbox', 'stage this article', or uses /memory-ingest."
---

# Memory Ingest — URL → Inbox Stub

Fetch a URL, extract its content, and write a staged inbox stub with provenance. The stub sits in the inbox until `/memory-consolidate` processes it into candidates.

## User-invocable

When the user types `/memory-ingest`, run this skill.

Also trigger when the user says:
- "ingest this URL: [url]"
- "add to inbox: [url]"
- "stage this article: [url]"
- "capture this link: [url]"
- "queue this for later: [url]"

## Arguments

- `/memory-ingest <url>` — fetch URL and write inbox stub

## Instructions

### Step 1: Validate input

Extract the URL from the user's message. If no URL is provided, respond:
> No URL provided. Usage: `/memory-ingest <url>`

### Step 2: Fetch and extract content

Use the `search` skill (power_search) with `Intent.READ_URL` to fetch and extract the URL:

```python
from power_search import search
from power_search.base import Intent

result = search(url, intent=Intent.READ_URL)
content = result.content  # extracted text
```

If the fetch fails (network error, 404, paywall), write an inbox stub with `status: fetch-failed` and `content: ""`. Do not abort — a failed-fetch stub still records provenance.

### Step 3: Generate timestamp and ID

```python
from datetime import datetime, timezone
ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
stub_id = f"inbox-{ts}"
```

### Step 4: Write inbox stub

Write to `~/memory/vault/inbox/inbox-{ts}.md`:

```markdown
---
id: inbox-{ts}
type: inbox-stub
status: inbox
source_type: url
source_url: {url}
fetched_at: {ISO8601 full, e.g. 2026-04-11T00:00:00Z}
tags: []
---
{extracted content}
```

### Step 5: Update impressions index (atomic write)

```python
import json, os

INDEX_PATH = os.path.expanduser("~/memory/vault/impressions-index.json")
TMP_PATH = INDEX_PATH + ".tmp"

with open(INDEX_PATH) as f:
    index = json.load(f)

index["sources"][stub_id] = {
    "type": "url",
    "url": url,
    "ingested_at": ts,
    "stub_path": f"vault/inbox/inbox-{ts}.md"
}

with open(TMP_PATH, "w") as f:
    json.dump(index, f, indent=2)
os.rename(TMP_PATH, INDEX_PATH)
```

### Step 6: Confirm

Output:
```
Ingested: {url}
Stub:     ~/memory/vault/inbox/inbox-{ts}.md
Run /memory-consolidate to process inbox into candidates.
```

## Rules

- One stub per invocation — never batch multiple URLs in a single call
- Atomic index writes only (tmp + rename)
- Failed fetches write a stub, not an error — provenance is preserved
- Never modify existing stubs
- Never write to vault/raw/threads/ — that is the review skill's domain
