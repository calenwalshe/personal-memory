"""
extraction_tui.py — Rich live TUI for window_classifier extraction progress.

Usage: instantiate ExtractionTUI(state), call start() before loop, update(state)
each iteration, stop() when done. Handles ctrl+c cleanly.
"""

import threading
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text


class ExtractionTUI:
    def __init__(self, state: dict):
        self._state = dict(state)
        self._recent_events: list[str] = []
        self._lock = threading.Lock()
        self._live: Live | None = None
        self._console = Console()

    # ── public API ──────────────────────────────────────────────────────────

    def start(self):
        layout = self._build_layout()
        self._live = Live(layout, console=self._console, refresh_per_second=1,
                          screen=False)
        self._live.start()

    def update(self, state: dict, event: str = None):
        with self._lock:
            self._state = dict(state)
            if event:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                self._recent_events.append(f"[dim]{ts}[/dim] {event}")
                if len(self._recent_events) > 12:
                    self._recent_events.pop(0)
        if self._live:
            self._live.update(self._build_layout())

    def stop(self):
        if self._live:
            self._live.stop()
            self._live = None

    # ── rendering ────────────────────────────────────────────────────────────

    def _build_layout(self) -> Layout:
        with self._lock:
            state = dict(self._state)
            events = list(self._recent_events)

        layout = Layout()
        layout.split_column(
            Layout(self._header(state), size=3),
            Layout(name="body"),
            Layout(self._events_panel(events), size=14),
        )
        layout["body"].split_row(
            Layout(self._progress_panel(state)),
            Layout(self._stats_panel(state)),
        )
        return layout

    def _header(self, state: dict) -> Panel:
        cur = state.get("current_session") or {}
        session_info = (
            f"[bold cyan]session {cur.get('index', '?')}/{state.get('total_sessions', '?')}[/] "
            f"[dim]{cur.get('id', 'idle')}[/] ({cur.get('size_kb', 0)} KB)"
            if cur else "[dim]initializing…[/dim]"
        )
        return Panel(
            Text.from_markup(f"  [bold white]Knowledge Extraction[/bold white]   {session_info}"),
            style="on #1a1a2e",
        )

    def _progress_panel(self, state: dict) -> Panel:
        total = max(state.get("total_sessions", 1), 1)
        done = len(state.get("processed_sessions", []))
        pct = done / total

        windows_seen = state.get("windows_seen", 0)
        pre_filtered = state.get("windows_prefiltered", 0)
        classified = state.get("windows_classified", 0)
        filter_rate = (pre_filtered / max(windows_seen, 1)) * 100
        lm_rate = (state.get("learning_moments", 0) / max(classified, 1)) * 100

        # ETA
        started = state.get("started_at")
        eta_str = "—"
        if started and done > 0:
            elapsed = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(started)).total_seconds()
            per_session = elapsed / done
            remaining = (total - done) * per_session
            h, m = divmod(int(remaining), 3600)
            m, s = divmod(m, 60)
            eta_str = f"{h:02d}:{m:02d}:{s:02d}"

        bar_width = 38
        filled = int(bar_width * pct)
        bar = "█" * filled + "░" * (bar_width - filled)

        table = Table.grid(padding=(0, 1))
        table.add_column(style="dim", width=18)
        table.add_column()

        table.add_row("Sessions", f"[green]{done}[/] / {total}  [dim]ETA {eta_str}[/]")
        table.add_row("", f"[cyan]{bar}[/] {pct:.0%}")
        table.add_row("Windows seen", f"{windows_seen:,}")
        table.add_row("Pre-filtered", f"[yellow]{pre_filtered:,}[/] [dim]({filter_rate:.0f}% skipped)[/]")
        table.add_row("Classified", f"{classified:,}")
        table.add_row("LM hit rate", f"[magenta]{lm_rate:.1f}%[/]")

        return Panel(table, title="[bold]Progress[/bold]", border_style="blue")

    def _stats_panel(self, state: dict) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="dim", width=18)
        table.add_column()

        table.add_row("Learning moments", f"[green]{state.get('learning_moments', 0):,}[/]")
        table.add_row("Facts written", f"[bold green]{state.get('facts_written', 0):,}[/]")
        table.add_row("Contradictions", (
            f"[red]{state.get('contradictions_auto', 0)}[/] auto  "
            f"[yellow]{state.get('contradictions_queued', 0)}[/] queued"
        ))
        table.add_row("Errors", f"[red]{len(state.get('errors', []))}[/]")

        last = state.get("last_updated")
        if last:
            dt = datetime.fromisoformat(last)
            table.add_row("Last update", dt.strftime("%H:%M:%S UTC"))

        return Panel(table, title="[bold]Fact Store[/bold]", border_style="green")

    def _events_panel(self, events: list[str]) -> Panel:
        content = "\n".join(events) if events else "[dim]waiting for events…[/dim]"
        return Panel(Text.from_markup(content), title="[bold]Recent Events[/bold]",
                     border_style="dim")


# ── standalone smoke test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import time

    state = {
        "total_sessions": 90,
        "processed_sessions": [],
        "facts_written": 0,
        "windows_seen": 0,
        "windows_prefiltered": 0,
        "windows_classified": 0,
        "learning_moments": 0,
        "contradictions_auto": 0,
        "contradictions_queued": 0,
        "errors": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_updated": None,
        "current_session": None,
    }

    tui = ExtractionTUI(state)
    tui.start()

    try:
        for i in range(1, 6):
            time.sleep(1)
            state["current_session"] = {"id": f"session-{i:04d}", "index": i,
                                         "path": f"/sessions/s{i}.jsonl", "size_kb": 42}
            state["windows_seen"] += 30
            state["windows_prefiltered"] += 20
            state["windows_classified"] += 10
            if i % 2 == 0:
                state["learning_moments"] += 2
                state["facts_written"] += 3
            state["processed_sessions"].append(f"session-{i:04d}")
            state["last_updated"] = datetime.now(timezone.utc).isoformat()
            tui.update(state, event=f"session-{i:04d}: {10} windows → 2 learning moments")
    except KeyboardInterrupt:
        pass
    finally:
        tui.stop()

    print("TUI smoke test complete.")
