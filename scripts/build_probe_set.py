"""
build_probe_set.py — Build a 50-example human-annotated probe set.

Phase 1 (auto): Scan sessions, select candidate windows, pre-label with LLM.
Phase 2 (human): Edit probe_set_candidates.jsonl — set label field to
                 'learning_moment' or 'task_execution' for each example.
Phase 3 (finalize): Run --finalize to validate and write probe_set.jsonl.

Usage:
  python3 build_probe_set.py                    # Phase 1: generate candidates
  python3 build_probe_set.py --finalize         # Phase 3: validate + finalize
  python3 build_probe_set.py --stats            # Show current probe set stats
"""

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
SESSIONS_DIR = Path.home() / ".claude/projects"
CANDIDATES_FILE = VAULT / "scripts/probe_set_candidates.jsonl"
PROBE_SET_FILE = VAULT / "scripts/probe_set.jsonl"
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "claude")

_NVM_BIN = Path.home() / ".nvm/versions/node/v20.20.0/bin"
NODE_BIN = os.environ.get("NODE_BIN") or shutil.which("node") or str(_NVM_BIN / "node")
_claude_sym = shutil.which("claude") or str(_NVM_BIN / "claude")
CLAUDE_CLI = os.environ.get("CLAUDE_CLI") or str(Path(_claude_sym).resolve())
_codex_sym = shutil.which("codex") or str(_NVM_BIN / "codex")
CODEX_CLI = os.environ.get("CODEX_CLI") or str(Path(_codex_sym).resolve())
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or str(_NVM_BIN / "claude")
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or str(_NVM_BIN / "codex")

TARGET_EXAMPLES = 50
TARGET_LM = 25       # learning_moment
TARGET_TE = 25       # task_execution

QUESTION_MARKERS = re.compile(
    r'\?|how |why |what |wait |i don\'t|doesn\'t|so you|that means|'
    r'i see|explain|confused|understand|difference between|what\'s the',
    re.IGNORECASE,
)


def llm(prompt: str) -> str:
    if LLM_PROVIDER == "codex":
        cmd = [NODE_BIN, CODEX_CLI, "exec", "--full-auto", "--profile", "llm",
               "--cd", "/tmp", "--skip-git-repo-check", prompt]
    else:
        cmd = [NODE_BIN, CLAUDE_CLI, "-p", prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return result.stdout.strip()


PRELABEL_PREAMBLE = """You are a training data labeler for a learning moment classifier.
Classify this conversation window as exactly one of:
- learning_moment: user asks a conceptual question, assistant explains, user confirms understanding
- task_execution: user asks Claude to DO something (write code, fix bug, run command, etc.)

Respond with JSON only: {"label": "learning_moment"|"task_execution", "confidence": 0.0-1.0}"""


def load_sessions() -> list[Path]:
    sessions = sorted(SESSIONS_DIR.rglob("*.jsonl"))
    return [s for s in sessions if not any(p in str(s) for p in ["-tmp-", "/-tmp/"])]


def parse_turns(session_path: Path) -> list[dict]:
    turns = []
    try:
        with open(session_path) as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    message = msg.get("message", {})
                    if not isinstance(message, dict):
                        continue
                    role = message.get("role", "")
                    content = message.get("content", "")
                    if role not in ("user", "assistant") or not content:
                        continue
                    if isinstance(content, list):
                        text = " ".join(
                            c.get("text", "") for c in content
                            if isinstance(c, dict) and c.get("type") == "text"
                        )
                    else:
                        text = str(content)
                    if text.strip():
                        turns.append({"role": role, "text": text[:600]})
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception:
        pass
    return turns


def prelabel_window(window: list[dict]) -> dict:
    window_text = "\n".join(f"[{t['role'].upper()}]: {t['text'][:300]}" for t in window)
    raw = llm(PRELABEL_PREAMBLE + f"\n\nWINDOW:\n{window_text}\n\nRespond with JSON only.")
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"label": "task_execution", "confidence": 0.5}


def build_candidates(target: int = TARGET_EXAMPLES * 2, window_size: int = 5):
    """Phase 1: scan sessions, select candidate windows, pre-label."""
    sessions = load_sessions()
    random.shuffle(sessions)

    candidates = []
    lm_count = 0
    te_count = 0

    print(f"Scanning {len(sessions)} sessions for probe set candidates...")
    print(f"Target: {target} total candidates ({TARGET_LM} LM + {TARGET_TE} TE minimum)")

    for session_path in sessions:
        if lm_count >= TARGET_LM and te_count >= TARGET_TE:
            break

        turns = parse_turns(session_path)
        if len(turns) < window_size:
            continue

        session_id = session_path.stem
        total_windows = len(turns) - window_size + 1

        # Sample a few windows per session (not all — diversity)
        sample_indices = random.sample(range(total_windows), min(3, total_windows))

        for w in sample_indices:
            window = turns[w: w + window_size]

            # Only consider windows with question markers (likely interesting)
            has_question = any(
                t["role"] == "user" and QUESTION_MARKERS.search(t["text"])
                for t in window
            )
            if not has_question:
                continue

            window_text = "\n".join(
                f"[{t['role'].upper()}]: {t['text'][:300]}" for t in window
            )

            print(f"  Pre-labeling window {w} from {session_id[:16]}...", end=" ", flush=True)
            result = prelabel_window(window)
            label = result.get("label", "task_execution")
            confidence = result.get("confidence", 0.5)
            print(f"{label} ({confidence:.2f})")

            candidate = {
                "session_id": session_id,
                "window_start": w,
                "window_size": window_size,
                "window_text": window_text,
                "llm_label": label,
                "llm_confidence": confidence,
                # Human fills this in during Phase 2:
                "label": label,   # starts as llm_label; human overrides if wrong
            }
            candidates.append(candidate)

            if label == "learning_moment":
                lm_count += 1
            else:
                te_count += 1

    # Write candidates
    CANDIDATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CANDIDATES_FILE, "w") as f:
        for c in candidates:
            f.write(json.dumps(c) + "\n")

    print(f"\nWrote {len(candidates)} candidates to {CANDIDATES_FILE}")
    print(f"  LLM-labeled: {lm_count} learning_moment, {te_count} task_execution")
    print(f"\nNext step: Review and correct 'label' field in {CANDIDATES_FILE}")
    print("  Then run: python3 build_probe_set.py --finalize")


def finalize():
    """Phase 3: validate human-annotated candidates → write probe_set.jsonl."""
    if not CANDIDATES_FILE.exists():
        print(f"ERROR: {CANDIDATES_FILE} not found. Run without --finalize first.")
        sys.exit(1)

    with open(CANDIDATES_FILE) as f:
        candidates = [json.loads(line) for line in f if line.strip()]

    valid_labels = {"learning_moment", "task_execution"}
    errors = []
    for i, c in enumerate(candidates):
        if c.get("label") not in valid_labels:
            errors.append(f"  Line {i+1}: invalid label '{c.get('label')}'")

    if errors:
        print(f"Validation errors ({len(errors)}):")
        for e in errors:
            print(e)
        print("Fix labels and re-run --finalize.")
        sys.exit(1)

    lm = [c for c in candidates if c["label"] == "learning_moment"]
    te = [c for c in candidates if c["label"] == "task_execution"]

    if len(lm) < 20:
        print(f"WARNING: Only {len(lm)} learning_moment examples (need ≥20 for contract)")
    if len(te) < 20:
        print(f"WARNING: Only {len(te)} task_execution examples (need ≥20 for contract)")

    # Write final probe set (select balanced subset)
    final = (
        random.sample(lm, min(TARGET_LM, len(lm))) +
        random.sample(te, min(TARGET_TE, len(te)))
    )
    random.shuffle(final)

    with open(PROBE_SET_FILE, "w") as f:
        for ex in final:
            f.write(json.dumps(ex) + "\n")

    print(f"Probe set finalized: {len(final)} examples")
    print(f"  {sum(1 for e in final if e['label']=='learning_moment')} learning_moment")
    print(f"  {sum(1 for e in final if e['label']=='task_execution')} task_execution")
    print(f"Written to: {PROBE_SET_FILE}")


def show_stats():
    """Show current probe set stats."""
    for path, label in [(CANDIDATES_FILE, "Candidates"), (PROBE_SET_FILE, "Probe set")]:
        if not path.exists():
            print(f"{label}: not found")
            continue
        with open(path) as f:
            examples = [json.loads(line) for line in f if line.strip()]
        lm = sum(1 for e in examples if e.get("label") == "learning_moment")
        te = sum(1 for e in examples if e.get("label") == "task_execution")
        print(f"{label}: {len(examples)} examples ({lm} LM, {te} TE)")


def main():
    parser = argparse.ArgumentParser(description="Build probe set for autoresearch eval")
    parser.add_argument("--finalize", action="store_true",
                        help="Validate candidates and write probe_set.jsonl")
    parser.add_argument("--stats", action="store_true", help="Show current probe set stats")
    parser.add_argument("--target", type=int, default=TARGET_EXAMPLES * 2,
                        help="Target candidate count to generate")
    args = parser.parse_args()

    if args.stats:
        show_stats()
    elif args.finalize:
        finalize()
    else:
        build_candidates(target=args.target)


if __name__ == "__main__":
    main()
