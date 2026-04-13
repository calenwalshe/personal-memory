"""
autoresearch_loop.py — Karpathy-style autoresearch loop for window classifier.

Sweeps {classifier_prompt, window_size, confidence_threshold} against probe_set.jsonl.
Updates GOAL.md with best params and F1 history after each iteration.

Usage:
  python3 autoresearch_loop.py                  # full sweep (requires probe_set.jsonl)
  python3 autoresearch_loop.py --eval-only      # evaluate current best params only
  python3 autoresearch_loop.py --iterations N   # limit sweep to N iterations
"""

import argparse
import shutil
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
PROBE_SET_FILE = VAULT / "scripts/probe_set.jsonl"
GOAL_FILE = VAULT / "scripts/GOAL.md"
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "claude")

_NVM_BIN = Path.home() / ".nvm/versions/node/v20.20.0/bin"
NODE_BIN = os.environ.get("NODE_BIN") or shutil.which("node") or str(_NVM_BIN / "node")
_claude_sym = shutil.which("claude") or str(_NVM_BIN / "claude")
CLAUDE_CLI = os.environ.get("CLAUDE_CLI") or str(Path(_claude_sym).resolve())
_codex_sym = shutil.which("codex") or str(_NVM_BIN / "codex")
CODEX_CLI = os.environ.get("CODEX_CLI") or str(Path(_codex_sym).resolve())

sys.path.insert(0, str(VAULT / "scripts"))

# ── Parameter search space ────────────────────────────────────────────────────

CLASSIFIER_PROMPTS = {
    "v1": """Analyze this conversation window. A learning moment has ALL THREE:
1. User question showing ignorance or confusion about a concept
2. Assistant explanation that addresses the confusion
3. User comprehension confirmation (verbal or follow-up showing understanding)

Task-execution windows (user asks to DO something) are NOT learning moments.""",

    "v2": """Analyze this conversation window. A learning moment has ALL THREE:
1. User question showing ignorance or confusion about a concept
2. Assistant explanation that addresses the confusion
3. User comprehension confirmation -- MUST be explicit (e.g., "I see", "that makes sense",
   "got it", "ok so", "ah I understand") -- a follow-up question does NOT count

Task-execution windows are NOT learning moments.""",

    "v3": """Analyze this conversation window. A learning moment has ALL THREE:
1. User question showing ignorance or confusion about a NAMED technical concept
   (library, API, algorithm, tool, pattern -- not general tasks)
2. Assistant explanation of that specific concept
3. User comprehension confirmation -- explicit verbal signal required

Task-execution windows (debugging, code writing, file edits, running commands) are NOT
learning moments even if they contain technical terms.""",
}

SEARCH_SPACE = {
    "classifier_prompt": ["v1", "v2", "v3"],
    "window_size": [3, 5, 7],
    "confidence_threshold": [0.3, 0.4, 0.5, 0.6, 0.7],
}


# ── LLM call ─────────────────────────────────────────────────────────────────

def llm(prompt: str) -> str:
    if LLM_PROVIDER == "codex":
        cmd = [NODE_BIN, CODEX_CLI, "exec", "--full-auto", "--profile", "llm",
               "--cd", "/tmp", "--skip-git-repo-check", prompt]
    else:
        cmd = [NODE_BIN, CLAUDE_CLI, "-p", prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return result.stdout.strip()


def classify_window_with_prompt(window_text: str, prompt_version: str,
                                 confidence_threshold: float) -> dict:
    preamble = CLASSIFIER_PROMPTS[prompt_version]
    full_prompt = f"""{preamble}

WINDOW:
{window_text}

Respond with JSON only:
{{
  "is_learning_moment": true/false,
  "confidence": 0.0-1.0
}}"""

    raw = llm(full_prompt)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
    try:
        result = json.loads(raw)
        is_lm = (
            result.get("is_learning_moment", False) and
            result.get("confidence", 0) >= confidence_threshold
        )
        return {"prediction": "learning_moment" if is_lm else "task_execution",
                "confidence": result.get("confidence", 0)}
    except json.JSONDecodeError:
        return {"prediction": "task_execution", "confidence": 0.0}


# ── Evaluation ────────────────────────────────────────────────────────────────

def evaluate(params: dict, probe_examples: list[dict]) -> dict:
    """Run classifier on all probe examples, compute P/R/F1."""
    prompt_v = params["classifier_prompt"]
    threshold = params["confidence_threshold"]

    tp = fp = fn = tn = 0
    for ex in probe_examples:
        pred = classify_window_with_prompt(
            ex["window_text"], prompt_v, threshold
        )
        actual = ex["label"]
        predicted = pred["prediction"]

        if actual == "learning_moment" and predicted == "learning_moment":
            tp += 1
        elif actual == "task_execution" and predicted == "learning_moment":
            fp += 1
        elif actual == "learning_moment" and predicted == "task_execution":
            fn += 1
        else:
            tn += 1

    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "n": len(probe_examples),
    }


# ── GOAL.md update ────────────────────────────────────────────────────────────

def update_goal_md(best_params: dict, best_metrics: dict, history: list[dict]):
    """Write updated current_best and iteration log to GOAL.md."""
    rows = []
    for h in history[-20:]:
        p = h.get("params", {})
        m = h.get("metrics", {})
        rows.append(
            f"| {h.get('run', '?')} | {p.get('classifier_prompt', 'v1')} | "
            f"{p.get('window_size', 5)} | {p.get('confidence_threshold', 0.5)} | "
            f"{m.get('precision', 0)} | {m.get('recall', 0)} | "
            f"**{m.get('f1', 0)}** | {h.get('date', '')} |"
        )

    content = f"""# Autoresearch Goal

## Metric

**Primary**: F1 score on `probe_set.jsonl`
- Target: F1 >= 0.7

## Current Best

| Prompt | Window | Threshold | Precision | Recall | F1 | Date |
|--------|--------|-----------|-----------|--------|----|------|
| {best_params.get('classifier_prompt', 'v1')} | {best_params.get('window_size', 5)} | {best_params.get('confidence_threshold', 0.5)} | {best_metrics.get('precision', 0)} | {best_metrics.get('recall', 0)} | **{best_metrics.get('f1', 0)}** | {datetime.now(timezone.utc).strftime('%Y-%m-%d')} |

## Parameter Search Space

```
classifier_prompt: {list(CLASSIFIER_PROMPTS.keys())}
window_size: {SEARCH_SPACE['window_size']}
confidence_threshold: {SEARCH_SPACE['confidence_threshold']}
```

## Iteration Log

| Run | Prompt | Window | Threshold | Precision | Recall | F1 | Date |
|-----|--------|--------|-----------|-----------|--------|----|------|
{chr(10).join(rows)}

---
*Replaces contract-001 GraphRAG loop (prior best F1: 0.074 on contaminated eval set -- architecture abandoned).*
"""
    GOAL_FILE.write_text(content)


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_loop(max_iterations=None, eval_only=False):
    if not PROBE_SET_FILE.exists():
        print("probe_set.jsonl not found.")
        print("Run: python3 build_probe_set.py")
        print("Then annotate and run: python3 build_probe_set.py --finalize")
        sys.exit(0)

    with open(PROBE_SET_FILE) as f:
        probe_examples = [json.loads(line) for line in f if line.strip()]

    lm_count = sum(1 for e in probe_examples if e.get("label") == "learning_moment")
    te_count = sum(1 for e in probe_examples if e.get("label") == "task_execution")
    print(f"Probe set: {len(probe_examples)} examples ({lm_count} LM, {te_count} TE)")

    best_params = {"classifier_prompt": "v1", "window_size": 5, "confidence_threshold": 0.5}
    best_f1 = 0.0
    best_metrics = {}
    history = []

    if eval_only:
        print(f"Evaluating params: {best_params}")
        metrics = evaluate(best_params, probe_examples)
        print(f"F1={metrics['f1']:.4f}  P={metrics['precision']:.4f}  R={metrics['recall']:.4f}")
        print(f"TP={metrics['tp']} FP={metrics['fp']} FN={metrics['fn']} TN={metrics['tn']}")
        return metrics

    from itertools import product
    param_grid = [
        {"classifier_prompt": cp, "window_size": ws, "confidence_threshold": ct}
        for cp, ws, ct in product(
            SEARCH_SPACE["classifier_prompt"],
            SEARCH_SPACE["window_size"],
            SEARCH_SPACE["confidence_threshold"],
        )
    ]

    print(f"Search space: {len(param_grid)} combinations")
    if max_iterations:
        param_grid = param_grid[:max_iterations]
        print(f"Limited to {max_iterations} iterations")

    for run_n, params in enumerate(param_grid, 1):
        print(f"\n[{run_n}/{len(param_grid)}] prompt={params['classifier_prompt']} "
              f"window={params['window_size']} threshold={params['confidence_threshold']}")
        t0 = time.monotonic()
        metrics = evaluate(params, probe_examples)
        elapsed = time.monotonic() - t0

        print(f"  F1={metrics['f1']:.4f}  P={metrics['precision']:.4f}  "
              f"R={metrics['recall']:.4f}  ({elapsed:.1f}s)")

        history.append({
            "run": run_n,
            "params": params,
            "metrics": metrics,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        })

        if metrics["f1"] > best_f1:
            best_f1 = metrics["f1"]
            best_params = params
            best_metrics = metrics
            print(f"  *** New best! F1={best_f1:.4f} ***")
            update_goal_md(best_params, best_metrics, history)

    print(f"\n=== Loop complete ===")
    print(f"Best params: {best_params}")
    print(f"Best F1: {best_f1:.4f}")
    update_goal_md(best_params, best_metrics or {"f1": best_f1}, history)
    return {"best_f1": best_f1, "best_params": best_params}


def main():
    parser = argparse.ArgumentParser(description="Autoresearch loop for window classifier")
    parser.add_argument("--eval-only", action="store_true",
                        help="Evaluate current best params only (no sweep)")
    parser.add_argument("--iterations", type=int, default=None,
                        help="Max iterations to run")
    args = parser.parse_args()

    run_loop(max_iterations=args.iterations, eval_only=args.eval_only)


if __name__ == "__main__":
    main()
