# /recall — Memory Recall

Query the personal fact store for learning moments extracted from prior Claude Code sessions.

## User-invocable

When the user types `/recall`, run this skill.

Also trigger when the user says:
- "what did I learn about"
- "recall [topic]"
- "what do I know about"
- "remind me about"
- "do I have notes on"

## Arguments

- `/recall <topic>` — semantic search + LLM synthesis (shallow mode, < 3s)
- `/recall <topic> --deep` — query expansion + broader retrieval
- `/recall --pending` — show contradiction review queue

## Instructions

Run the recall script directly:

```bash
VAULT_DIR=~/memory/vault python3 ~/memory/vault/scripts/recall_query.py "<topic>"
```

For deep mode:
```bash
VAULT_DIR=~/memory/vault python3 ~/memory/vault/scripts/recall_query.py "<topic>" --deep
```

For pending contradictions:
```bash
VAULT_DIR=~/memory/vault python3 ~/memory/vault/scripts/recall_query.py --pending-review
```

### Output format

The script returns a 1-3 sentence synthesis referencing session dates:
```
You learned: <fact summary> (session date: YYYY-MM-DD)
```

If no facts found: "No facts found matching '<topic>'."

If contradictions pending: appends a note with count.

### When facts.db is empty

If the extraction hasn't been run yet:
```
No facts found matching '<topic>'.

Tip: Run the extraction first:
  cd ~/memory/vault/scripts && LLM_PROVIDER=claude python3 window_classifier.py
```

## Rules

- Always display the session date from retrieved facts — this is the key provenance signal
- If pending review count > 0, surface it — the user should know their fact store has ambiguous contradictions
- Never fabricate facts not present in the retrieved context
- `/recall` operates in read-only mode — it never writes to the fact store
