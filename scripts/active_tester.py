"""
active_tester.py — Active belief testing via read-only repo/file inspection.

Complements passive_tester.py (observation-driven) with deliberate probes:
given a belief, construct grep/path/content probes against the local filesystem
and return a test outcome.

Active tests are more expensive than passive (they do I/O per belief) but can
confirm or disconfirm beliefs that haven't appeared in recent atom stream.

Probe types (read-only, no side effects):
  grep       — search for keywords/patterns in files under a directory
  path_exists — check whether a specific file/directory exists
  content_match — read a file and check for pattern presence

Outcome semantics:
  confirmed    — probe found evidence consistent with the belief
  disconfirmed — probe found evidence contradicting the belief
  inconclusive — probe ran but couldn't determine either way
  error        — probe itself failed (path inaccessible, regex error, etc.)

Usage:
  from active_tester import ActiveTester
  tester = ActiveTester(search_roots=[Path("/home/agent/projects/myrepo")])
  results = tester.test_belief(form)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VAULT = Path(os.environ.get("VAULT_DIR", Path.home() / "memory/vault"))
BELIEFS_DB = VAULT / "beliefs.db"

# Default roots to search when a belief has no project context
DEFAULT_SEARCH_ROOTS: list[Path] = [
    Path.home() / "projects",
    Path.home() / "memory" / "vault" / "scripts",
]

# File extensions to probe during grep (avoid binaries)
_GREP_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs",
    ".sh", ".bash", ".json", ".yaml", ".yml", ".toml",
    ".md", ".txt", ".sql", ".html", ".css",
    "Dockerfile", "Makefile",
}

# Max files to grep per probe (safety limit)
_MAX_FILES_PER_PROBE = 200

# Max line output to include in detail (chars)
_MAX_DETAIL_CHARS = 400


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Probe construction ─────────────────────────────────────────────────────

@dataclass
class Probe:
    """A single read-only inspection against the filesystem."""
    probe_type: str       # grep | path_exists | content_match
    target: str           # search root (grep) | file path (path_exists/content_match)
    pattern: str          # regex pattern to search for
    label: str            # human-readable description of what we're looking for
    negate: bool = False  # True → confirmed if pattern NOT found


def build_probes_for_form(form: dict, search_roots: list[Path]) -> list[Probe]:
    """Construct probes for a belief based on its form_type and content.

    Strategy:
    - Extract subject/predicate/object or fallback to content keywords
    - Build confirming probes (grep for technology, path exists for claimed files)
    - Build disconfirming probes (grep for contradicting patterns)
    """
    form_type = form.get("form_type", "claim")
    content = form.get("content", "")
    subject = form.get("subject") or ""
    predicate = form.get("predicate") or ""
    obj = form.get("object") or ""
    project = form.get("project") or ""

    # Determine which roots to search
    roots = _resolve_roots(project, search_roots)

    probes: list[Probe] = []

    if form_type == "claim":
        # "X is used for Y" → grep for X, grep for Y in same project
        if subject:
            probes.append(Probe(
                probe_type="grep",
                target=str(roots[0]) if roots else str(Path.home()),
                pattern=re.escape(subject),
                label=f"evidence of subject '{subject}' in codebase",
            ))
        if obj and obj != subject:
            probes.append(Probe(
                probe_type="grep",
                target=str(roots[0]) if roots else str(Path.home()),
                pattern=re.escape(obj),
                label=f"evidence of object '{obj}' in codebase",
            ))
        # Disconfirmation: look for subject being replaced
        if subject:
            probes.append(Probe(
                probe_type="grep",
                target=str(roots[0]) if roots else str(Path.home()),
                pattern=rf"(?i)(replace|replac|remov|drop|migrat|switch)\w*\s+{re.escape(subject)}",
                label=f"evidence that '{subject}' was replaced or removed",
                negate=False,  # finding this DISCONFIRMS the belief
            ))

    elif form_type == "decision":
        # "Chose X" → grep for X still being used
        keywords = _keywords_from_content(content)[:3]
        for kw in keywords:
            probes.append(Probe(
                probe_type="grep",
                target=str(roots[0]) if roots else str(Path.home()),
                pattern=re.escape(kw),
                label=f"evidence that '{kw}' is still in use",
            ))

    elif form_type == "rule":
        # "Always do X" → grep for the rule being followed
        keywords = _keywords_from_content(content)[:2]
        for kw in keywords:
            probes.append(Probe(
                probe_type="grep",
                target=str(roots[0]) if roots else str(Path.home()),
                pattern=re.escape(kw),
                label=f"evidence of rule application involving '{kw}'",
            ))

    elif form_type == "plan":
        # "Will add X" → check if X now exists (plan completed) or if there's
        # evidence it was abandoned
        keywords = _keywords_from_content(content)[:2]
        for kw in keywords:
            probes.append(Probe(
                probe_type="grep",
                target=str(roots[0]) if roots else str(Path.home()),
                pattern=re.escape(kw),
                label=f"evidence that planned item '{kw}' was implemented",
            ))

    else:
        # Generic: grep for content keywords
        keywords = _keywords_from_content(content)[:3]
        for kw in keywords:
            probes.append(Probe(
                probe_type="grep",
                target=str(roots[0]) if roots else str(Path.home()),
                pattern=re.escape(kw),
                label=f"evidence related to '{kw}'",
            ))

    return probes


def _resolve_roots(project: str, search_roots: list[Path]) -> list[Path]:
    """Pick search roots: prefer project-specific directory, then defaults."""
    if project:
        candidate = Path.home() / "projects" / project
        if candidate.is_dir():
            return [candidate]
        candidate2 = Path.home() / project
        if candidate2.is_dir():
            return [candidate2]
    return [r for r in search_roots if r.is_dir()] or [Path.home() / "projects"]


def _keywords_from_content(content: str) -> list[str]:
    """Extract meaningful keywords from free-form content."""
    stopwords = {
        "the", "a", "an", "is", "was", "are", "were", "be", "been",
        "that", "this", "it", "of", "in", "on", "at", "to", "for",
        "will", "would", "should", "could", "always", "never", "not",
        "and", "or", "but", "with", "by", "from", "as", "if", "so",
        "do", "does", "did", "have", "has", "had", "we", "i", "you",
        "chose", "use", "used", "add", "after", "before", "when", "then",
    }
    words = re.findall(r"\b[a-zA-Z_]\w{2,}\b", content)
    seen = set()
    result = []
    for w in words:
        lw = w.lower()
        if lw not in stopwords and lw not in seen:
            seen.add(lw)
            result.append(w)
    return result


# ── Probe execution ────────────────────────────────────────────────────────

@dataclass
class ProbeResult:
    probe: Probe
    found: bool          # True = pattern found (or path exists)
    match_count: int
    sample: str          # short excerpt of first match
    error: Optional[str] = None


def run_probe(probe: Probe) -> ProbeResult:
    """Execute a single probe. Read-only — no writes."""
    try:
        if probe.probe_type == "grep":
            return _run_grep(probe)
        elif probe.probe_type == "path_exists":
            return _run_path_exists(probe)
        elif probe.probe_type == "content_match":
            return _run_content_match(probe)
        else:
            return ProbeResult(probe=probe, found=False, match_count=0,
                               sample="", error=f"unknown probe type: {probe.probe_type}")
    except Exception as e:
        return ProbeResult(probe=probe, found=False, match_count=0,
                           sample="", error=str(e))


def _run_grep(probe: Probe) -> ProbeResult:
    root = Path(probe.target)
    if not root.is_dir():
        return ProbeResult(probe=probe, found=False, match_count=0,
                           sample="", error=f"search root not found: {root}")

    # Collect candidate files
    files = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in _GREP_EXTENSIONS or p.name in _GREP_EXTENSIONS:
            files.append(p)
        if len(files) >= _MAX_FILES_PER_PROBE:
            break

    if not files:
        return ProbeResult(probe=probe, found=False, match_count=0,
                           sample="", error="no searchable files found")

    try:
        compiled = re.compile(probe.pattern, re.IGNORECASE)
    except re.error as e:
        return ProbeResult(probe=probe, found=False, match_count=0,
                           sample="", error=f"regex error: {e}")

    match_count = 0
    sample = ""
    for fp in files:
        try:
            text = fp.read_text(errors="ignore")
        except OSError:
            continue
        for m in compiled.finditer(text):
            match_count += 1
            if not sample:
                # Get surrounding line as sample
                start = max(0, m.start() - 40)
                end = min(len(text), m.end() + 80)
                sample = text[start:end].replace("\n", " ").strip()[:_MAX_DETAIL_CHARS]
            if match_count >= 50:
                break
        if match_count >= 50:
            break

    return ProbeResult(probe=probe, found=match_count > 0,
                       match_count=match_count, sample=sample)


def _run_path_exists(probe: Probe) -> ProbeResult:
    p = Path(probe.target)
    exists = p.exists()
    return ProbeResult(probe=probe, found=exists, match_count=1 if exists else 0,
                       sample=str(p) if exists else "")


def _run_content_match(probe: Probe) -> ProbeResult:
    fp = Path(probe.target)
    if not fp.is_file():
        return ProbeResult(probe=probe, found=False, match_count=0,
                           sample="", error=f"file not found: {fp}")
    try:
        text = fp.read_text(errors="ignore")
        compiled = re.compile(probe.pattern, re.IGNORECASE)
        matches = list(compiled.finditer(text))
        if matches:
            m = matches[0]
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 80)
            sample = text[start:end].replace("\n", " ").strip()[:_MAX_DETAIL_CHARS]
        else:
            sample = ""
        return ProbeResult(probe=probe, found=bool(matches),
                           match_count=len(matches), sample=sample)
    except Exception as e:
        return ProbeResult(probe=probe, found=False, match_count=0,
                           sample="", error=str(e))


# ── Outcome derivation ─────────────────────────────────────────────────────

@dataclass
class ActiveTestResult:
    form_id: str
    outcome: str          # confirmed | disconfirmed | inconclusive | error
    detail: str
    probes_run: int
    probes_confirmed: int
    probes_disconfirmed: int
    confidence_before: float
    confidence_after: float


def derive_outcome(form: dict, probe_results: list[ProbeResult],
                   conf_before: float) -> ActiveTestResult:
    """Aggregate probe results into a single test outcome."""
    from belief_tester import beta_bernoulli_update

    if not probe_results:
        return ActiveTestResult(
            form_id=form["id"],
            outcome="inconclusive",
            detail="no probes generated",
            probes_run=0,
            probes_confirmed=0,
            probes_disconfirmed=0,
            confidence_before=conf_before,
            confidence_after=conf_before,
        )

    errors = [r for r in probe_results if r.error]
    non_error = [r for r in probe_results if not r.error]

    if not non_error:
        error_msgs = "; ".join(r.error for r in errors[:3])
        return ActiveTestResult(
            form_id=form["id"],
            outcome="error",
            detail=f"all probes failed: {error_msgs}",
            probes_run=len(probe_results),
            probes_confirmed=0,
            probes_disconfirmed=0,
            confidence_before=conf_before,
            confidence_after=conf_before,
        )

    # Separate confirming vs. disconfirming probes
    confirmed_probes = []
    disconfirmed_probes = []
    for r in non_error:
        if r.error:
            continue
        # A negate probe disconfirms if found; non-negate disconfirms if NOT found
        # (But we only use negate for explicitly disconfirming probes here)
        if r.probe.negate:
            if r.found:
                disconfirmed_probes.append(r)
            else:
                confirmed_probes.append(r)
        else:
            if r.found:
                confirmed_probes.append(r)
            # not found → no signal (skip, don't count as disconfirmation)

    n_conf = len(confirmed_probes)
    n_disconf = len(disconfirmed_probes)

    if n_disconf > n_conf and n_disconf >= 1:
        outcome = "disconfirmed"
        sample = disconfirmed_probes[0].sample or disconfirmed_probes[0].probe.label
        detail = f"disconfirming probe matched: {sample[:200]}"
        _, _, conf_after = beta_bernoulli_update(
            conf_before * 8, (1 - conf_before) * 8, confirmed=False
        )
    elif n_conf >= 1:
        outcome = "confirmed"
        sample = confirmed_probes[0].sample or confirmed_probes[0].probe.label
        detail = f"confirming probe matched ({n_conf} probes): {sample[:200]}"
        _, _, conf_after = beta_bernoulli_update(
            conf_before * 8, (1 - conf_before) * 8, confirmed=True
        )
    else:
        outcome = "inconclusive"
        detail = f"no probes matched ({len(non_error)} probes ran, 0 matched)"
        conf_after = conf_before

    return ActiveTestResult(
        form_id=form["id"],
        outcome=outcome,
        detail=detail,
        probes_run=len(probe_results),
        probes_confirmed=n_conf,
        probes_disconfirmed=n_disconf,
        confidence_before=conf_before,
        confidence_after=conf_after,
    )


# ── ActiveTester class ─────────────────────────────────────────────────────

class ActiveTester:
    """Probe beliefs against the local filesystem.

    Unlike PassiveTester (which waits for atoms), ActiveTester actively
    inspects files and directories to confirm or disconfirm beliefs on demand.

    All probes are read-only — no modifications to any file.
    """

    def __init__(
        self,
        beliefs_db: Path = BELIEFS_DB,
        search_roots: Optional[list[Path]] = None,
    ):
        self._beliefs_db = beliefs_db
        self._search_roots = search_roots or DEFAULT_SEARCH_ROOTS

    def _beliefs_conn(self) -> Optional[sqlite3.Connection]:
        if not self._beliefs_db.exists():
            return None
        conn = sqlite3.connect(str(self._beliefs_db))
        conn.row_factory = sqlite3.Row
        return conn

    def _get_active_forms(self) -> list[dict]:
        conn = self._beliefs_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                """SELECT lf.*, fs.confidence as fs_confidence
                   FROM logical_forms lf
                   JOIN form_status fs ON fs.form_id = lf.id
                   WHERE fs.world_id = 'world-current'
                     AND fs.status IN ('active', 'stable')
                     AND (lf.superseded_by IS NULL OR lf.superseded_by = '')
                   ORDER BY lf.extracted_at DESC""",
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _get_belief_confidence(self, form: dict) -> float:
        """Get current confidence from form_status or fallback to form confidence."""
        conn = self._beliefs_conn()
        if conn is None:
            return form.get("confidence", 0.7)
        try:
            row = conn.execute(
                """SELECT confidence FROM form_status
                   WHERE form_id = ? AND world_id = 'world-current'""",
                (form["id"],),
            ).fetchone()
            if row:
                return row["confidence"]
            return form.get("confidence", 0.7)
        finally:
            conn.close()

    def _record_active_test(self, result: ActiveTestResult):
        conn = self._beliefs_conn()
        if conn is None:
            return
        try:
            conn.execute(
                """INSERT INTO belief_tests
                   (id, form_id, test_type, outcome, detail,
                    confidence_before, confidence_after,
                    alpha_before, beta_before, alpha_after, beta_after, tested_at)
                   VALUES (?,?,'active',?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()),
                    result.form_id,
                    result.outcome,
                    result.detail,
                    result.confidence_before,
                    result.confidence_after,
                    result.confidence_before * 8,   # alpha proxy
                    (1 - result.confidence_before) * 8,  # beta proxy
                    result.confidence_after * 8,
                    (1 - result.confidence_after) * 8,
                    _now(),
                ),
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass  # belief_tests table may not exist yet in this connection
        finally:
            conn.close()

    def test_belief(self, form: dict) -> ActiveTestResult:
        """Run active probes for a single belief form.

        Args:
            form: A dict with at least 'id', 'form_type', 'content';
                  optionally 'subject', 'predicate', 'object', 'project'

        Returns:
            ActiveTestResult with outcome and confidence delta.
        """
        conf_before = self._get_belief_confidence(form)
        probes = build_probes_for_form(form, self._search_roots)
        probe_results = [run_probe(p) for p in probes]
        result = derive_outcome(form, probe_results, conf_before)
        self._record_active_test(result)
        return result

    def run_batch(self, form_ids: Optional[list[str]] = None,
                  limit: int = 20) -> dict:
        """Run active tests for a batch of beliefs.

        Args:
            form_ids: If provided, test only these form IDs. Otherwise test
                      up to `limit` active forms (oldest-tested first).
            limit:    Max forms to test in one batch.

        Returns:
            Summary dict with counts by outcome.
        """
        forms = self._get_active_forms()
        if form_ids:
            forms = [f for f in forms if f["id"] in set(form_ids)]
        forms = forms[:limit]

        summary = {"tested": 0, "confirmed": 0, "disconfirmed": 0,
                   "inconclusive": 0, "error": 0}
        for form in forms:
            result = self.test_belief(form)
            summary["tested"] += 1
            summary[result.outcome] = summary.get(result.outcome, 0) + 1

        return summary
