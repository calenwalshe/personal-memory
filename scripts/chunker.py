"""
chunker.py — L0 → L1 aggregation engine.

Reads L0 turns from events.db, groups them into content-driven clusters,
refines boundaries via Haiku, and produces L1 atoms in atoms.db.

A turn = one cycle of human interaction:
  user message -> model thinking -> tool calls -> model response

Key properties:
  - Boundary-agnostic: chunks span session/compaction/clear boundaries
  - Project-keyed: chunker state is per-project, not per-session
  - Lazy closing: clusters stay open/provisional until a positive close signal
  - Haiku refines, doesn't discover: heuristic pre-clustering does the heavy lifting

Usage:
  from chunker import run_chunker
  result = run_chunker("ctrl", model="claude-haiku-4-5-20251001", dry_run=False)
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VAULT = Path(os.environ.get("MEMORY_VAULT", Path.home() / "memory/vault"))
EVENTS_DB = VAULT / "events.db"

_NVM_BIN = Path.home() / ".nvm/versions/node/v20.20.0/bin"
CLAUDE_BIN = (
    os.environ.get("CLAUDE_BIN")
    or shutil.which("claude")
    or str(_NVM_BIN / "claude")
)

sys.path.insert(0, str(VAULT / "scripts"))

# ── Noise filter ─────────────────────────────────────────────────────────

# Action tools — turns containing these are always kept
ACTION_TOOLS = {"Bash", "Write", "Edit", "NotebookEdit"}

# Plumbing tools — turns where ALL tool calls are these get dropped
PLUMBING_TOOLS = {"Skill", "ScheduleWakeup", "TaskCreate", "TaskUpdate", "TaskGet",
                  "TaskList", "TaskStop", "TaskOutput", "Monitor", "CronCreate",
                  "CronDelete", "CronList", "ExitPlanMode", "EnterPlanMode",
                  "PushNotification", "RemoteTrigger", "EnterWorktree", "ExitWorktree"}

# Max turns in a single cluster before force-closing
MAX_CLUSTER_SIZE = 8

# Time gap (seconds) for hard close
HARD_GAP_SECONDS = 7200  # 2 hours

# Time gap for soft close (different intent likely)
SOFT_GAP_SECONDS = 1800  # 30 minutes


# ── Data types ───────────────────────────────────────────────────────────

class Turn:
    """Lightweight wrapper around an L0 turn row."""
    __slots__ = (
        "turn_id", "session_id", "project", "turn_n", "started_at", "ended_at",
        "duration_ms", "user_message", "user_message_preview",
        "thinking_preview", "response_preview",
        "tool_calls", "tool_call_count", "tool_names",
        "had_error", "error_count", "agentic_loops",
        "cwd", "git_branch", "git_sha",
    )

    def __init__(self, row: dict):
        for s in self.__slots__:
            setattr(self, s, row.get(s))
        self.had_error = bool(self.had_error)
        self.tool_call_count = self.tool_call_count or 0
        self.agentic_loops = self.agentic_loops or 0
        self.error_count = self.error_count or 0

    @property
    def ts(self) -> datetime:
        ts = self.started_at or ""
        ts = ts.replace("+00:00", "Z").rstrip("Z")
        try:
            return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime(2000, 1, 1, tzinfo=timezone.utc)

    def tool_name_set(self) -> set[str]:
        """Set of distinct tool names used in this turn."""
        if not self.tool_names:
            return set()
        try:
            return set(json.loads(self.tool_names))
        except (json.JSONDecodeError, TypeError):
            return set()

    def file_paths(self) -> list[str]:
        """Extract file paths from tool_calls JSON."""
        if not self.tool_calls:
            return []
        try:
            calls = json.loads(self.tool_calls)
        except (json.JSONDecodeError, TypeError):
            return []
        paths = []
        for tc in calls:
            inp = tc.get("input_preview", "")
            try:
                inp_obj = json.loads(inp)
                for key in ("file_path", "path", "pattern", "command"):
                    v = inp_obj.get(key, "")
                    if v and isinstance(v, str) and "/" in v:
                        paths.append(v)
            except (json.JSONDecodeError, TypeError):
                pass
        return paths


class Cluster:
    """A group of turns being accumulated into a potential atom."""

    def __init__(self, status: str = "open"):
        self.turns: list[Turn] = []
        self.turn_ids: list[str] = []
        self.first_timestamp: str = ""
        self.last_timestamp: str = ""
        self.entity_set: set[str] = set()
        self.intent_preview: str = ""
        self.status: str = status  # open | provisional | closed

    def add(self, turn: Turn):
        self.turns.append(turn)
        self.turn_ids.append(turn.turn_id)
        if not self.first_timestamp:
            self.first_timestamp = turn.started_at
        self.last_timestamp = turn.ended_at or turn.started_at
        if not self.intent_preview and turn.user_message_preview:
            self.intent_preview = turn.user_message_preview
        for fp in turn.file_paths():
            self.entity_set.add(fp)

    @property
    def size(self) -> int:
        return len(self.turns)

    def to_dict(self) -> dict:
        return {
            "turn_ids": self.turn_ids,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "entity_set": sorted(self.entity_set),
            "intent_preview": (self.intent_preview or "")[:200],
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Cluster":
        c = cls(status=d.get("status", "provisional"))
        c.turn_ids = d.get("turn_ids", [])
        c.first_timestamp = d.get("first_timestamp", "")
        c.last_timestamp = d.get("last_timestamp", "")
        c.entity_set = set(d.get("entity_set", []))
        c.intent_preview = d.get("intent_preview", "")
        return c


# ── Chunker state persistence ────────────────────────────────────────────

def _ensure_chunker_state_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunker_state (
            project         TEXT PRIMARY KEY,
            cursor_event_id TEXT NOT NULL,
            cursor_timestamp TEXT NOT NULL,
            open_clusters   TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
    """)
    conn.commit()


def _load_state(conn: sqlite3.Connection, project: str) -> tuple[str, str, list[dict]]:
    """Returns (cursor_turn_id, cursor_timestamp, open_clusters_dicts)."""
    _ensure_chunker_state_table(conn)
    row = conn.execute(
        "SELECT cursor_event_id, cursor_timestamp, open_clusters FROM chunker_state WHERE project=?",
        [project],
    ).fetchone()
    if row:
        clusters = json.loads(row[2]) if row[2] else []
        return row[0], row[1], clusters
    return "", "", []


def _save_state(conn: sqlite3.Connection, project: str,
                cursor_turn_id: str, cursor_timestamp: str,
                open_clusters: list[dict]):
    _ensure_chunker_state_table(conn)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """INSERT INTO chunker_state (project, cursor_event_id, cursor_timestamp, open_clusters, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(project) DO UPDATE SET
             cursor_event_id=excluded.cursor_event_id,
             cursor_timestamp=excluded.cursor_timestamp,
             open_clusters=excluded.open_clusters,
             updated_at=excluded.updated_at""",
        [project, cursor_turn_id, cursor_timestamp, json.dumps(open_clusters), now],
    )
    conn.commit()


# ── L0 turn reading ─────────────────────────────────────────────────────

TURN_COLS = [
    "turn_id", "session_id", "project", "turn_n", "started_at", "ended_at",
    "duration_ms", "user_message", "user_message_preview",
    "thinking_preview", "response_preview",
    "tool_calls", "tool_call_count", "tool_names",
    "had_error", "error_count", "agentic_loops",
    "cwd", "git_branch", "git_sha",
]
TURN_COL_STR = ", ".join(TURN_COLS)


def _find_project_aliases(conn: sqlite3.Connection, project: str) -> list[str]:
    """Find all project name variants that share session IDs with this project.

    When a session spans a context continuation, the CWD can change and turns
    land under a different project name (e.g. 'spring_austin' vs
    '2026-trips-spring-austin').  This finds all such siblings so the chunker
    can process them together.
    """
    session_ids = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT session_id FROM turns WHERE project = ?", [project]
        ).fetchall()
    ]
    if not session_ids:
        return [project]

    placeholders = ",".join("?" * len(session_ids))
    aliases = [
        r[0] for r in conn.execute(
            f"SELECT DISTINCT project FROM turns WHERE session_id IN ({placeholders})",
            session_ids,
        ).fetchall()
    ]
    return aliases if aliases else [project]


def _read_new_turns(conn: sqlite3.Connection, project: str,
                    cursor_timestamp: str) -> list[Turn]:
    """Read turns for project (and its aliases) since cursor, ordered by started_at."""

    # Find all project name variants that share sessions
    aliases = _find_project_aliases(conn, project)

    placeholders = ",".join("?" * len(aliases))
    if cursor_timestamp:
        rows = conn.execute(
            f"SELECT {TURN_COL_STR} FROM turns "
            f"WHERE project IN ({placeholders}) AND started_at > ? "
            f"ORDER BY started_at ASC",
            [*aliases, cursor_timestamp],
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {TURN_COL_STR} FROM turns "
            f"WHERE project IN ({placeholders}) "
            f"ORDER BY started_at ASC",
            aliases,
        ).fetchall()

    return [Turn(dict(zip(TURN_COLS, r))) for r in rows]


def _find_unchunked_turns(conn: sqlite3.Connection, project: str) -> list[Turn]:
    """Find turns whose turn_id never appears in any atom's source_events.

    This is the bare-minimum safety net: if a turn exists in L0 but no L1 atom
    references it, the chunker should look at it regardless of cursor position.
    """
    aliases = _find_project_aliases(conn, project)
    placeholders = ",".join("?" * len(aliases))

    # Get all turn_ids for this project family
    all_turns = conn.execute(
        f"SELECT {TURN_COL_STR} FROM turns "
        f"WHERE project IN ({placeholders}) ORDER BY started_at ASC",
        aliases,
    ).fetchall()

    if not all_turns:
        return []

    # Get all turn_ids referenced by atoms for these projects
    atoms_db = VAULT / "atoms.db"
    if not atoms_db.exists():
        # All turns are unchunked if no atoms DB
        return [Turn(dict(zip(TURN_COLS, r))) for r in all_turns]
    atoms_conn = sqlite3.connect(str(atoms_db))
    try:
        referenced = set()
        for row in atoms_conn.execute(
            f"SELECT source_events FROM atoms "
            f"WHERE project IN ({placeholders}) AND invalidated_by IS NULL",
            aliases,
        ).fetchall():
            try:
                for tid in json.loads(row[0] or "[]"):
                    referenced.add(tid)
            except (json.JSONDecodeError, TypeError):
                pass
    finally:
        atoms_conn.close()

    # Return turns not referenced by any atom
    unchunked = []
    for r in all_turns:
        turn_dict = dict(zip(TURN_COLS, r))
        if turn_dict["turn_id"] not in referenced:
            unchunked.append(Turn(turn_dict))

    return unchunked


# ── Noise filtering ──────────────────────────────────────────────────────

def _filter_noise(turns: list[Turn]) -> list[Turn]:
    """
    Filter out noise turns:
    - Keep: turns with action tools, turns with errors, turns with >= 2 agentic loops
    - Keep: pure conversation turns with response > 100 chars (meaningful Q&A)
    - Drop: turns where all tools are plumbing
    - Drop: trivial exchanges (0 tools, short/no response)
    """
    if not turns:
        return []

    kept = []
    for i, t in enumerate(turns):
        tools = t.tool_name_set()

        # Always keep errors
        if t.had_error:
            kept.append(t)
            continue

        # Always keep turns with action tools
        if tools & ACTION_TOOLS:
            kept.append(t)
            continue

        # Drop turns where ALL tools are plumbing
        if tools and tools <= PLUMBING_TOOLS:
            continue

        # Complex agentic turns (>= 2 loops) are interesting
        if t.agentic_loops >= 2:
            kept.append(t)
            continue

        # Pure conversation (no tools): keep if response is meaningful
        if not tools:
            resp = t.response_preview or ""
            if len(resp) > 100:
                kept.append(t)
            continue

        # Orientation-only turns (Read/Glob/Grep): keep if followed by action within 30 min
        orientation_tools = {"Read", "Glob", "Grep", "ToolSearch"}
        if tools <= orientation_tools:
            for j in range(i + 1, min(i + 5, len(turns))):
                nxt_tools = turns[j].tool_name_set()
                gap = abs((turns[j].ts - t.ts).total_seconds())
                if gap > SOFT_GAP_SECONDS:
                    break
                if nxt_tools & ACTION_TOOLS or turns[j].had_error:
                    kept.append(t)
                    break
            continue

        # Unknown tool mix: keep (conservative)
        kept.append(t)

    return kept


# ── Pre-clustering ───────────────────────────────────────────────────────

def _time_gap(t1: Turn, t2: Turn) -> float:
    return abs((t2.ts - t1.ts).total_seconds())


def _intent_changed(t1: Turn, t2: Turn) -> bool:
    """Did the user intent change between turns?"""
    p1 = (t1.user_message_preview or "").strip()[:80]
    p2 = (t2.user_message_preview or "").strip()[:80]
    if not p1 or not p2:
        return False
    # Different first 60 chars = different intent
    return p1[:60] != p2[:60]


def _entity_overlap(cluster: Cluster, turn: Turn) -> float:
    turn_files = set(turn.file_paths())
    if not turn_files or not cluster.entity_set:
        return 0.5
    overlap = turn_files & cluster.entity_set
    return len(overlap) / len(turn_files)


def _pre_cluster(turns: list[Turn], open_clusters: list[Cluster]) -> tuple[list[Cluster], list[Cluster]]:
    """Group turns into clusters. Returns (closed, still_open)."""
    closed = []

    if open_clusters:
        current = open_clusters[-1]
        for c in open_clusters[:-1]:
            c.status = "closed"
            closed.append(c)
    else:
        current = Cluster()

    for turn in turns:
        # Hard close: long gap
        if current.turns and _time_gap(current.turns[-1], turn) > HARD_GAP_SECONDS:
            if current.turns:
                current.status = "closed"
                closed.append(current)
            current = Cluster()

        # Soft close: new intent
        if current.turns and _intent_changed(current.turns[-1], turn):
            if current.turns:
                current.status = "provisional"
                closed.append(current)
            current = Cluster()

        # Soft close: moderate gap + low entity overlap
        elif (current.turns
              and _time_gap(current.turns[-1], turn) > SOFT_GAP_SECONDS
              and _entity_overlap(current, turn) < 0.3):
            if current.turns:
                current.status = "provisional"
                closed.append(current)
            current = Cluster()

        # Size guard
        if current.size >= MAX_CLUSTER_SIZE:
            current.status = "closed"
            closed.append(current)
            current = Cluster()

        current.add(turn)

    still_open = [current] if current.turns else []
    return closed, still_open


# ── Merge check ──────────────────────────────────────────────────────────

def _merge_adjacent(closed: list[Cluster]) -> list[Cluster]:
    """Merge adjacent provisional clusters that share entities/intent."""
    if len(closed) < 2:
        return closed

    merged = [closed[0]]
    for c in closed[1:]:
        prev = merged[-1]
        if prev.status != "provisional" or c.status not in ("provisional", "closed"):
            merged.append(c)
            continue
        if prev.turns and c.turns:
            gap = _time_gap(prev.turns[-1], c.turns[0])
            if gap > SOFT_GAP_SECONDS:
                merged.append(c)
                continue
        if prev.entity_set and c.entity_set:
            if not (prev.entity_set & c.entity_set):
                merged.append(c)
                continue
        # Merge
        for t in c.turns:
            prev.add(t)
        if c.status == "closed":
            prev.status = "closed"
    return merged


# ── Haiku refinement ─────────────────────────────────────────────────────

HAIKU_PROMPT = """You are bundling conversation turns into atomic memory units.

Each turn shows: what the user asked, what the model thought, what tools were used,
and what the model responded. A turn is one complete interaction cycle.

For each cluster of turns:
1. Decide: is this one coherent memory, or should it be split/merged/dropped?
2. If it's a memory, produce ONE atom.

Atom types: decision, discovery, failure, pattern, gotcha, outcome

Rules:
- An atom is ONE thing. If a cluster contains two distinct things, split it.
- Adjacent clusters that are clearly one thing (debug->fix, explore->decide)
  should be merged.
- Drop clusters that contain no memorable content: routine navigation,
  asking about project status, simple lookups.
- Be specific: name the tool, file, project, service, or outcome.
- 1-2 sentences max per atom.

For each atom, also assess:
- interest_signal: Did the USER initiate this by choice (true) or was it reactive —
  fixing an error, responding to a system requirement, following instructions (false)?
  Look at the user's words: "let's build...", "I want to...", "can we research..." = true.
  "fix this", "it's broken", "why is this failing" = false.
- interest_tags: What personal interests does this reveal about the user? Not technical
  entities (those go in "entities"), but interest-level concepts: family-travel,
  music-curation, creative-projects, health-tracking, home-automation, cooking,
  real-estate, finance, etc. Empty list if no personal interest signal.
- user_intent: What was the user trying to do? One of: explore, build, fix, plan,
  research, decide, organize. This describes the user's goal, not the outcome.

Respond with a JSON array:
[
  {
    "content": "one sentence describing what happened or was learned",
    "atom_type": "decision|discovery|failure|pattern|gotcha|outcome",
    "source_cluster_indices": [0, 1],
    "entities": ["Entity1", "Entity2"],
    "topic": "short-label",
    "confidence": 0.7,
    "importance": 0.5,
    "interest_signal": true,
    "interest_tags": ["family-travel", "trip-planning"],
    "user_intent": "build"
  }
]

Return empty array [] if no clusters contain memorable content.

CLUSTERS:
"""


def _format_cluster_for_haiku(cluster: Cluster, index: int) -> str:
    """Format a cluster's turns for the Haiku prompt."""
    lines = [f"--- Cluster {index} ({cluster.size} turns) ---"]
    for t in cluster.turns:
        parts = [f"  [{t.started_at}] User: {(t.user_message_preview or '')[:150]}"]
        if t.thinking_preview:
            parts.append(f"    Thinking: {(t.thinking_preview or '')[:200]}")
        if t.tool_names:
            parts.append(f"    Tools: {t.tool_names} ({t.agentic_loops} loops)")
        if t.response_preview:
            parts.append(f"    Response: {(t.response_preview or '')[:200]}")
        flags = []
        if t.had_error:
            flags.append(f"ERRORS({t.error_count})")
        if flags:
            parts.append(f"    Flags: {', '.join(flags)}")
        lines.append("\n".join(parts))
    return "\n".join(lines)


HAIKU_BATCH_SIZE = 8  # clusters per Haiku call
HAIKU_TIMEOUT = 45    # seconds per call


def _haiku_refine_batch(clusters: list[Cluster], model: str,
                        index_offset: int = 0) -> list[dict]:
    """Send a batch of clusters to Haiku. Returns atom dicts with corrected indices."""
    if not clusters:
        return []

    cluster_text = "\n\n".join(
        _format_cluster_for_haiku(c, i) for i, c in enumerate(clusters)
    )
    prompt = HAIKU_PROMPT + cluster_text

    try:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        result = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", model, prompt],
            capture_output=True, text=True, timeout=HAIKU_TIMEOUT, env=env,
        )
        raw = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    raw = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "")
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        atoms = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []

    # Remap cluster indices to global positions
    for a in atoms:
        a["source_cluster_indices"] = [
            idx + index_offset for idx in a.get("source_cluster_indices", [0])
        ]
    return atoms


def _haiku_refine(clusters: list[Cluster], model: str) -> list[dict]:
    """Send clusters to Haiku in batches for boundary refinement + atom production."""
    if not clusters:
        return []

    all_atoms = []
    for batch_start in range(0, len(clusters), HAIKU_BATCH_SIZE):
        batch = clusters[batch_start:batch_start + HAIKU_BATCH_SIZE]
        atoms = _haiku_refine_batch(batch, model, index_offset=batch_start)
        all_atoms.extend(atoms)

    return all_atoms


# ── Provenance assembly ──────────────────────────────────────────────────

def _assemble_provenance(turns: list[Turn]) -> dict:
    """Build denormalized provenance from source turns."""
    if not turns:
        return {}

    session_ids = list(dict.fromkeys(t.session_id for t in turns if t.session_id))
    timestamps = [t.ts for t in turns]
    min_ts = min(timestamps)
    max_ts = max(timestamps)

    files = []
    tools = []
    for t in turns:
        files.extend(t.file_paths())
        tools.extend(t.tool_name_set())
    files = list(dict.fromkeys(files))
    tools = list(dict.fromkeys(tools))

    trigger = ""
    for t in turns:
        if t.user_message_preview and t.user_message_preview.strip():
            trigger = t.user_message_preview.strip()[:200]
            break

    return {
        "project": turns[0].project,
        "source_events": [t.turn_id for t in turns],
        "source_count": len(turns),
        "session_ids": session_ids,
        "time_first": min_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "time_last": max_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_s": round((max_ts - min_ts).total_seconds(), 1),
        "git_branch": turns[0].git_branch,
        "git_sha": turns[0].git_sha,
        "trigger": trigger,
        "tools_used": tools,
        "had_errors": any(t.had_error for t in turns),
        "retry_count": sum(t.error_count for t in turns),
        "files_touched": files,
    }


# ── Main orchestrator ────────────────────────────────────────────────────

def run_chunker(
    project: str,
    model: str = "claude-haiku-4-5-20251001",
    dry_run: bool = False,
    skip_faiss: bool = False,
) -> dict:
    """
    Run the full chunker pipeline for a project:
    1. Load state -> 2. Read new turns -> 3. Filter noise -> 4. Pre-cluster ->
    5. Merge check -> 6. Haiku refine -> 7. Assemble provenance -> 8. Write atoms -> 9. Save state
    """
    import time
    start = time.monotonic()

    if not EVENTS_DB.exists():
        return {"project": project, "status": "no_events_db", "atoms_produced": 0}

    conn = sqlite3.connect(str(EVENTS_DB))
    conn.row_factory = sqlite3.Row

    # Step 1: Load state
    cursor_id, cursor_ts, open_cluster_dicts = _load_state(conn, project)

    # Step 2: Read new turns (cursor-based, includes project aliases)
    raw_turns = _read_new_turns(conn, project, cursor_ts)

    # Step 2b: Safety net — check for unchunked turns that the cursor missed.
    # This catches project-name changes mid-session, cursor drift, and any
    # turns that fell through the cracks.  Fires when cursor finds nothing new,
    # even if there are leftover open clusters (which may be stale).
    unchunked_backfill = False
    if not raw_turns:
        unchunked = _find_unchunked_turns(conn, project)
        if unchunked:
            raw_turns = unchunked
            unchunked_backfill = True
            # Clear stale open clusters — we're reprocessing from scratch
            open_cluster_dicts = []
        elif not open_cluster_dicts:
            conn.close()
            return {
                "project": project,
                "status": "no_new_turns",
                "atoms_produced": 0,
                "duration_s": round(time.monotonic() - start, 2),
            }

    # Step 3: Filter noise
    turns = _filter_noise(raw_turns)

    # Rebuild open clusters from state
    open_clusters = [Cluster.from_dict(d) for d in open_cluster_dicts] if open_cluster_dicts else []

    # Step 4: Pre-cluster
    closed_clusters, still_open = _pre_cluster(turns, open_clusters)

    # Step 5: Merge check
    closed_clusters = _merge_adjacent(closed_clusters)

    # Filter out tiny clusters
    meaningful = []
    for c in closed_clusters:
        if c.size == 0:
            continue
        if c.size >= 2:
            meaningful.append(c)
        elif c.turns and (c.turns[0].had_error or c.turns[0].tool_name_set() & ACTION_TOOLS):
            meaningful.append(c)

    if not meaningful:
        if raw_turns:
            last = raw_turns[-1]
            if not dry_run:
                _save_state(conn, project, last.turn_id, last.started_at,
                           [c.to_dict() for c in still_open])
        conn.close()
        return {
            "project": project,
            "status": "no_meaningful_clusters",
            "raw_turns": len(raw_turns),
            "filtered_turns": len(turns),
            "clusters_formed": len(closed_clusters),
            "atoms_produced": 0,
            "duration_s": round(time.monotonic() - start, 2),
        }

    # Step 6: Haiku refinement
    if dry_run:
        haiku_atoms = [
            {
                "content": f"[dry-run] cluster {i}: {c.size} turns, intent={c.intent_preview[:60]}",
                "atom_type": "outcome",
                "source_cluster_indices": [i],
                "entities": sorted(c.entity_set)[:5],
                "topic": "dry-run",
                "confidence": 0.5,
                "importance": 0.5,
            }
            for i, c in enumerate(meaningful)
        ]
    else:
        haiku_atoms = _haiku_refine(meaningful, model)

    if not haiku_atoms:
        if raw_turns:
            last = raw_turns[-1]
            if not dry_run:
                _save_state(conn, project, last.turn_id, last.started_at,
                           [c.to_dict() for c in still_open])
        conn.close()
        return {
            "project": project,
            "status": "haiku_produced_nothing",
            "raw_turns": len(raw_turns),
            "filtered_turns": len(turns),
            "clusters_sent": len(meaningful),
            "atoms_produced": 0,
            "duration_s": round(time.monotonic() - start, 2),
        }

    # Step 7: Assemble provenance + build atom dicts
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    atom_dicts = []

    for ha in haiku_atoms:
        content = ha.get("content", "").strip()
        if not content:
            continue

        atom_type = ha.get("atom_type", "outcome")
        if atom_type not in ("decision", "discovery", "failure", "pattern", "gotcha", "outcome"):
            atom_type = "outcome"

        indices = ha.get("source_cluster_indices", [0])
        source_turns = []
        for idx in indices:
            if 0 <= idx < len(meaningful):
                source_turns.extend(meaningful[idx].turns)

        if not source_turns and meaningful:
            source_turns = meaningful[0].turns

        if not source_turns:
            continue

        prov = _assemble_provenance(source_turns)

        # Validate user_intent
        user_intent = ha.get("user_intent", "")
        if user_intent not in ("explore", "build", "fix", "plan", "research", "decide", "organize"):
            user_intent = ""

        atom_dicts.append({
            "content": content,
            "atom_type": atom_type,
            "entities": ha.get("entities", []),
            "topic": ha.get("topic", ""),
            "confidence": float(ha.get("confidence", 0.7)),
            "importance": float(ha.get("importance", 0.5)),
            "interest_signal": bool(ha.get("interest_signal", False)),
            "interest_tags": ha.get("interest_tags", []),
            "user_intent": user_intent,
            "created_at": now,
            **prov,
        })

    # Step 8: Write atoms
    atom_ids = []
    if atom_dicts and not dry_run:
        from atom_store import batch_add_atoms
        atom_ids = batch_add_atoms(atom_dicts, skip_faiss=skip_faiss)

    # Step 9: Save state
    if raw_turns and not dry_run:
        last = raw_turns[-1]
        _save_state(conn, project, last.turn_id, last.started_at,
                   [c.to_dict() for c in still_open])

    conn.close()
    duration = round(time.monotonic() - start, 2)

    return {
        "project": project,
        "status": "ok" if atom_ids else ("dry_run" if dry_run else "no_atoms"),
        "raw_turns": len(raw_turns),
        "filtered_turns": len(turns),
        "clusters_formed": len(closed_clusters),
        "clusters_sent": len(meaningful),
        "haiku_returned": len(haiku_atoms),
        "atoms_produced": len(atom_ids) if atom_ids else len(atom_dicts),
        "atom_ids": atom_ids,
        "still_open_clusters": len(still_open),
        "unchunked_backfill": unchunked_backfill,
        "duration_s": duration,
        "dry_run": dry_run,
    }
