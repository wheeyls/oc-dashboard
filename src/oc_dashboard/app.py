import os
import signal
import subprocess
import webbrowser
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import DataTable, Static

from .data import BackgroundWorker
from .data import DashboardSnapshot
from .data import SessionSummary
from .data import TodoItem
from .data import build_snapshot
from .data import relative_time
from .data import session_status
from .data import CI_PASS, CI_FAIL, CI_PENDING
from .data import LogEvent, get_wal_mtime, find_latest_log, parse_log_line
from .data import fetch_running_processes


# ── Nerd Font icons ─────────────────────────────────────
# Font Awesome (U+F000-U+F2E0)
_CHECK = chr(0xF00C)
_TIMES = chr(0xF00D)
_COG = chr(0xF013)
_SEARCH = chr(0xF002)
_BOOK = chr(0xF02D)
_LIST = chr(0xF03A)
_PLAY = chr(0xF04B)
_INFO = chr(0xF05A)
_EXCL = chr(0xF06A)
_EYE = chr(0xF06E)
_WARN = chr(0xF071)
_COGS = chr(0xF085)
_SQ_O = chr(0xF096)
_O = chr(0xF10C)
_SPIN = chr(0xF110)
_CIRCLE = chr(0xF111)
_TERM = chr(0xF120)
_FORK = chr(0xF126)
_QUESTION = chr(0xF128)
_ROCKET = chr(0xF135)
_BOLT = chr(0xF0E7)
_SITEMAP = chr(0xF0E8)
_CLIP = chr(0xF0EA)
_DOLLAR = chr(0xF155)
_SIGNAL = chr(0xF012)
_PIE = chr(0xF200)
_CHECK_CIRCLE = chr(0xF058)
_KEYBOARD = chr(0xF11C)
# Hourglass animation
_HG = [chr(0xF250), chr(0xF251), chr(0xF252), chr(0xF253)]
# Powerline (U+E0A0-U+E0D4)
_SEP = chr(0xE0B1)
_BRANCH_ICON = chr(0xE0A0)
# Block elements (standard Unicode - work in all monospace fonts)
_FULL = chr(0x2588)
_SHADE3 = chr(0x2593)
_SHADE2 = chr(0x2592)
_SHADE1 = chr(0x2591)
_SKULL = chr(0xF05E)
_PIPE = chr(0x2502)
_HBAR = chr(0x2500)
_FOLDER = chr(0xF07B)  # nf-fa-folder

STATUS_ICONS = {
    "running": _CIRCLE,
    "waiting": _KEYBOARD,
    "stalled": _WARN,
    "done": _CHECK_CIRCLE,
    "old": _O,
}

TODO_ICONS = {
    "completed": _CHECK,
    "in_progress": _SPIN,
    "pending": _SQ_O,
    "cancelled": _TIMES,
}

AGENT_ICONS = {
    "explore": _SEARCH,
    "librarian": _BOOK,
    "oracle": _SITEMAP,
    "metis": _BOLT,
    "momus": _CLIP,
    "Sisyphus-Junior": _COG,
    "unknown": _QUESTION,
}

PRIORITY_ICONS = {
    "critical": _EXCL,
    "high": _WARN,
    "medium": _INFO,
    "low": _O,
}

# ── Event display mapping for Comms panel ───────────────
EVENT_DISPLAY = {
    "session.status": (_CIRCLE, "session status"),
    "session.updated": (_COG, "session updated"),
    "session.idle": (_KEYBOARD, "agent idle"),
    "session.error": (_EXCL, "ERROR"),
    "session.diff": (_COG, "files changed"),
    "session.compacted": (_COG, "session compacted"),
    "message.updated": (_CHECK, "message complete"),
    "todo.updated": (_SQ_O, "todo updated"),
    "vcs.branch": (_BRANCH_ICON, ""),  # special: shows from->to
    "command.executed": (_TERM, "command executed"),
    "lsp.client.diagnostics": (_WARN, "LSP diagnostics"),
    "file.edited": (_COG, "file edited"),
    "tui.toast.show": (_INFO, "notification"),
    "watchdog.kill": (_SKULL, "KILLED"),
    "ci.failed": (_TIMES, "CI FAILED"),
    }

ACTIVE_WORKER_WINDOW = timedelta(minutes=3)
COMMS_MAX_EVENTS = 40
MEM_KILL_THRESHOLD_MB = 8192  # 8GB — processes above this get killed

def _in_tmux():
    # type: () -> bool
    return bool(os.environ.get("TMUX"))


def _launch_session_in_tmux(session_id, project_path=None):
    # type: (str, ...) -> None
    """Open an opencode session in a new tmux pane.

    Uses 'remain-on-exit off' so the pane auto-closes when opencode exits
    normally (user presses q), but opencode itself survives pane death
    because we run it under setsid to give it its own process group.
    """
    oc_cmd = "opencode -s %s" % session_id
    if project_path:
        oc_cmd = "cd %s && %s" % (project_path, oc_cmd)

    if _in_tmux():
        try:
            subprocess.Popen(
                ["tmux", "split-window", "-h", "-l", "25%", oc_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    else:
        # Fallback: open in a new Terminal.app window (macOS)
        apple_script = (
            'tell application "Terminal" to do script "%s"'
            % cmd.replace('"', '\\"')
        )
        try:
            subprocess.Popen(
                ["osascript", "-e", apple_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


class OCDashboardApp(App[None]):
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("o", "open_pr", "Open PR"),
        Binding("enter", "open_session", "Open Session"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._snapshot = None  # type: Optional[DashboardSnapshot]
        self._sessions = []  # type: list
        self._todos_by_session = {}  # type: dict
        self._workers_by_session = {}  # type: dict
        self._running_cpu = {}  # type: dict
        self._cost_by_session = {}  # type: dict
        self._mem_by_session = {}  # type: dict
        self._unattributed_cpu = 0.0
        self._selected_index = 0
        self._tick_count = 0
        self._refreshing = False
        # Live features state
        self._wal_mtime = None  # type: Optional[float]
        self._log_path = None  # type: Optional[str]
        self._log_handle = None  # type: Optional[object]
        self._log_events = deque(maxlen=COMMS_MAX_EVENTS)  # type: deque
        self._current_branch = ""
        self._error_flash_until = None  # type: Optional[datetime]
        self._total_cost = 0.0
        self._watchdog_warned = set()  # type: set
        self._prev_ci_fail_count = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        with Container(id="main"):
            with Container(id="top-row"):
                with Container(id="sessions-panel", classes="panel"):
                    yield Static(" %s SESSIONS" % _LIST, classes="panel-title")
                    yield DataTable(id="sessions-table")
                with Container(id="detail-panel", classes="panel"):
                    yield Static(" %s INTEL" % _EYE, classes="panel-title")
                    yield Static("", id="detail-body")
            with Container(id="bottom-row"):
                with Container(id="comms-panel", classes="panel"):
                    yield Static(" %s COMMS" % _SIGNAL, classes="panel-title")
                    yield Static("", id="comms-body")
                with Container(id="next-panel", classes="panel"):
                    yield Static(" %s NEXT OPS" % _ROCKET, classes="panel-title")
                    yield Static("", id="next-body")
                with Container(id="pr-panel", classes="panel"):
                    yield Static(" %s PULL REQUESTS" % _FORK, classes="panel-title")
                    yield DataTable(id="pr-table")
        yield Static("", id="footerbar")

    def on_mount(self) -> None:
        sessions_table = self.query_one("#sessions-table", DataTable)
        _ = sessions_table.add_columns(
            "", "Title", "Todos", "Bots", "Mem", "Cost", "Age"
        )
        sessions_table.cursor_type = "row"

        pr_table = self.query_one("#pr-table", DataTable)
        _ = pr_table.add_columns("PR", "CI", "Title", "Review")
        pr_table.cursor_type = "row"

        # Initialize WAL mtime for change detection
        self._wal_mtime = get_wal_mtime()

        # Open latest log file and seek to end
        self._open_latest_log()

        # Seed current branch from git
        self._init_branch()

        self._render_topbar()
        self._render_footerbar()
        _ = self.refresh_dashboard()

        # Timers
        self.set_interval(2, self._tick)
        self.set_interval(0.5, self._poll_log)
        self.set_interval(3, self._check_wal)

        self.set_interval(5, self._watchdog)

    def on_resize(self, event) -> None:
        """Toggle responsive CSS classes based on terminal width."""
        w = event.size.width
        app = self.app if hasattr(self, 'app') else self
        screen = app.screen
        if w < 100:
            screen.add_class("compact")
            screen.add_class("narrow")
        elif w < 160:
            screen.remove_class("compact")
            screen.add_class("narrow")
        else:
            screen.remove_class("compact")
            screen.remove_class("narrow")
    # ── Live features ──────────────────────────────────────────────────

    def _open_latest_log(self) -> None:
        """Open the most recently modified log file, backfill recent events."""
        latest = find_latest_log()
        if not latest:
            return
        if self._log_path == latest and self._log_handle is not None:
            return  # already tailing this file
        # Close old handle
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
        try:
            fh = open(latest, "r", encoding="utf-8", errors="replace")
            # Backfill: read last ~8KB to seed Comms panel
            try:
                fh.seek(0, 2)  # seek to end
                end_pos = fh.tell()
                backfill_pos = max(0, end_pos - 8192)
                fh.seek(backfill_pos)
                if backfill_pos > 0:
                    fh.readline()  # discard partial line
                for backfill_line in fh:
                    ev = parse_log_line(backfill_line)
                    if ev is not None:
                        self._log_events.append(ev)
                        if ev.event_type == "vcs.branch":
                            to_branch = ev.fields.get("to", "")
                            if to_branch and to_branch != "HEAD":
                                self._current_branch = to_branch
            except Exception:
                fh.seek(0, 2)  # fallback: just go to end
            self._log_handle = fh
            self._log_path = latest
        except Exception:
            self._log_handle = None
            self._log_path = None

    def _poll_log(self) -> None:
        """Read new lines from log file, parse events, update comms."""
        # Check if log file rotated
        latest = find_latest_log()
        if latest and latest != self._log_path:
            self._open_latest_log()

        if self._log_handle is None:
            return

        new_events = False
        try:
            for _ in range(200):  # cap reads per poll
                line = self._log_handle.readline()
                if not line:
                    break
                event = parse_log_line(line)
                if event is None:
                    continue
                self._log_events.append(event)
                new_events = True

                # Track branch changes
                if event.event_type == "vcs.branch":
                    to_branch = event.fields.get("to", "")
                    if to_branch and to_branch != "HEAD":
                        self._current_branch = to_branch

                # Error flash
                if event.event_type == "session.error":
                    self._error_flash_until = datetime.now() + timedelta(seconds=5)
        except Exception:
            # File might have been truncated/removed
            self._log_handle = None
            self._log_path = None

        if new_events:
            self._render_comms()
            self._render_topbar()

    def _check_wal(self) -> None:
        """Auto-refresh when DB changes detected via WAL mtime."""
        new_mtime = get_wal_mtime()
        if new_mtime is None:
            return
        if self._wal_mtime is None or new_mtime > self._wal_mtime:
            self._wal_mtime = new_mtime
            _ = self.refresh_dashboard()

    def _watchdog(self) -> None:
        """Kill runaway opencode processes exceeding memory threshold."""
        try:
            processes = fetch_running_processes()
        except Exception:
            return

        killed_any = False
        for proc in processes:
            if proc.mem_mb < MEM_KILL_THRESHOLD_MB:
                self._watchdog_warned.discard(proc.pid)
                continue

            # Resolve session info
            session_label = "unknown session"
            if proc.session_id:
                for s in self._sessions:
                    if s.id == proc.session_id:
                        session_label = s.title[:40]
                        break

            mem_gb = proc.mem_mb / 1024.0

            if proc.pid in self._watchdog_warned:
                # Already warned — escalate to SIGKILL
                try:
                    os.kill(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                msg = "SIGKILL PID %d (%.1fGB) — %s" % (proc.pid, mem_gb, session_label)
                self._watchdog_warned.discard(proc.pid)
            else:
                # First offense — SIGTERM
                try:
                    os.kill(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
                msg = "SIGTERM PID %d (%.1fGB) — %s" % (proc.pid, mem_gb, session_label)
                self._watchdog_warned.add(proc.pid)

            event = LogEvent(
                time_str=datetime.now().strftime("%H:%M:%S"),
                event_type="watchdog.kill",
                fields={"detail": msg},
            )
            self._log_events.append(event)
            killed_any = True

        if killed_any:
            self._error_flash_until = datetime.now() + timedelta(seconds=10)
            self._render_comms()
            self._render_topbar()
    def _tick(self) -> None:
        self._tick_count += 1
        self._render_topbar()
        self._render_footerbar()

    def action_refresh(self) -> None:
        _ = self.refresh_dashboard()

    def action_open_pr(self) -> None:
        self._open_selected_pr()

    def action_cursor_down(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        table.action_cursor_down()

    def action_cursor_up(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        table.action_cursor_up()

    def action_open_session(self) -> None:
        self._open_selected_session()

    def _open_selected_session(self) -> None:
        if not self._sessions:
            return
        if self._selected_index >= len(self._sessions):
            return
        session = self._sessions[self._selected_index]
        if session.is_group_header:
            return
        project_path = session.directory or (self._snapshot.project_path if self._snapshot else None)
        _launch_session_in_tmux(session.id, project_path)
    def _open_selected_pr(self) -> None:
        if not self._snapshot:
            return
        pr_table = self.query_one("#pr-table", DataTable)
        row_idx = pr_table.cursor_row
        if row_idx is None or row_idx < 0:
            return
        prs = self._snapshot.prs[:10]
        if row_idx >= len(prs):
            return
        pr = prs[row_idx]
        if pr.url:
            webbrowser.open(pr.url)

    def on_data_table_row_highlighted(
        self, event_obj: DataTable.RowHighlighted
    ) -> None:
        table = event_obj.data_table
        if table.id != "sessions-table":
            return
        if self._refreshing:
            return  # ignore cursor resets during refresh
        if event_obj.cursor_row is not None:
            self._selected_index = event_obj.cursor_row
            self._render_detail()

    def on_data_table_row_selected(self, event_obj: DataTable.RowSelected) -> None:
        table = event_obj.data_table
        if table.id == "pr-table":
            self._open_selected_pr()
        elif table.id == "sessions-table":
            self._open_selected_session()

    @work(thread=True, exclusive=True)
    def refresh_dashboard(self) -> None:
        snapshot = build_snapshot(limit=30)
        self.call_from_thread(self._apply_snapshot, snapshot)

    def _apply_snapshot(self, snapshot: DashboardSnapshot) -> None:
        self._snapshot = snapshot
        self._sessions = snapshot.sessions
        self._todos_by_session = snapshot.todos_by_session
        self._workers_by_session = snapshot.workers_by_session
        self._running_cpu = {}
        self._cost_by_session = {}
        self._unattributed_cpu = 0.0
        for process in snapshot.running_processes:
            if process.session_id:
                current = self._running_cpu.get(process.session_id, 0.0)
                self._running_cpu[process.session_id] = max(
                    current, process.cpu_percent
                )
            else:
                self._unattributed_cpu += process.cpu_percent

        self._mem_by_session = {}
        for process in snapshot.running_processes:
            if process.session_id:
                current = self._mem_by_session.get(process.session_id, 0)
                self._mem_by_session[process.session_id] = current + process.mem_mb
        self._total_cost = 0.0
        for cost in snapshot.session_costs:
            self._cost_by_session[cost.session_id] = cost.total_cost
            self._total_cost += cost.total_cost

        if self._selected_index >= len(self._sessions):
            self._selected_index = max(0, len(self._sessions) - 1)

        # Flash topbar if CI failures increased
        ci_fails = sum(1 for p in snapshot.prs if p.ci_status == CI_FAIL)
        if ci_fails > self._prev_ci_fail_count and ci_fails > 0:
            self._error_flash_until = datetime.now() + timedelta(seconds=10)
            self._log_events.append(LogEvent(
                timestamp=datetime.now().strftime("%H:%M:%S"),
                event_type="ci.failed",
                fields={},
                raw="%d PR(s) failing CI" % ci_fails,
            ))
        self._prev_ci_fail_count = ci_fails

        self._render_topbar()
        self._render_sessions()
        self._render_detail()
        self._render_prs()
        self._render_comms()
        self._render_next_ops()
    # ── Topbar ──────────────────────────────────────────

    def _render_topbar(self) -> None:
        spinner = _HG[self._tick_count % len(_HG)]
        now_text = datetime.now().strftime("%H:%M:%S")

        live = sum(1 for s in self._sessions if s.id in self._running_cpu)
        stalled = sum(
            1
            for s in self._sessions
            if (s.pending > 0 or s.in_progress > 0) and s.id not in self._running_cpu
        )
        total_cpu = sum(
            p.cpu_percent
            for p in (self._snapshot.running_processes if self._snapshot else [])
        )

        parts = [
            " %s %s OC//DASH" % (spinner, _TERM),
            "%s %s" % (_SEP, now_text),
            "%s %s %s LIVE" % (_SEP, _CIRCLE, live),
            "%s %s %s STALLED" % (_SEP, _WARN, stalled),
        ]
        if total_cpu >= 1:
            parts.append("%s %s %d%%" % (_SEP, _COGS, int(round(total_cpu))))

        # Cumulative cost
        if self._total_cost >= 1:
            parts.append(
                "%s %s $%s" % (_SEP, _DOLLAR, "{:,.0f}".format(self._total_cost))
            )

        # Current branch
        if self._current_branch:
            branch_display = self._current_branch
            if len(branch_display) > 28:
                branch_display = branch_display[:25] + "..."
            parts.append("%s %s %s" % (_SEP, _BRANCH_ICON, branch_display))

        # CI failure badge — persistent red alert in topbar
        ci_fails = sum(
            1 for p in (self._snapshot.prs if self._snapshot else []) if p.ci_status == CI_FAIL
        )
        if ci_fails > 0:
            parts.append("%s [bold red]%s %s CI FAIL[/]" % (_SEP, _TIMES, ci_fails))
        topbar = self.query_one("#topbar", Static)

        # Error flash: red background for 5s after session.error
        if self._error_flash_until and datetime.now() < self._error_flash_until:
            topbar.add_class("error-flash")
        else:
            topbar.remove_class("error-flash")
            self._error_flash_until = None

        topbar.update("  ".join(parts))

    def _render_footerbar(self) -> None:
        self.query_one("#footerbar", Static).update(
            " q:quit %s r:refresh %s o:pr %s enter:session %s j/k:nav" % (_PIPE, _PIPE, _PIPE, _PIPE)
        )

    # ── Sessions table ──────────────────────────────────

    def _format_todos(self, session: SessionSummary) -> str:
        done = session.completed
        total = session.total
        if total == 0:
            return "-"
        if session.pending == 0 and session.in_progress == 0:
            return "%s/%s %s" % (done, total, _CHECK)
        return "%s/%s" % (done, total)

    def _is_worker_active(self, session_id: str, worker: BackgroundWorker) -> bool:
        session_is_running = session_id in self._running_cpu
        if not session_is_running:
            return False
        return (datetime.now() - worker.updated) <= ACTIVE_WORKER_WINDOW

    def _worker_counts(self, session_id: str):
        # type: (...) -> tuple
        workers = self._workers_by_session.get(session_id, [])
        if not workers:
            return 0, 0
        active = sum(
            1 for worker in workers if self._is_worker_active(session_id, worker)
        )
        return active, len(workers)

    def _render_sessions(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        self._refreshing = True
        table.clear()

        # Pre-compute last-child at each depth for └─ vs ├─
        # We track: for each "parent context" what is the last child id
        # depth=1 sessions are children of their group header
        # depth=2 sessions (forks) are children of a depth=1 session
        last_child = {}  # type: dict  # parent_key -> last_child_id
        for session in self._sessions:
            if session.depth == 1:
                # parent is the preceding group header
                last_child[session.directory] = session.id
            elif session.depth == 2 and session.parent_id:
                last_child[session.parent_id] = session.id

        for session in self._sessions:
            if session.is_group_header:
                # Repo group header row
                table.add_row(
                    _FOLDER,
                    session.title,
                    "", "", "", "", "",
                    key=session.id,
                )
                continue

            cpu = self._running_cpu.get(session.id)
            status = session_status(session, cpu)
            icon = STATUS_ICONS.get(status, _O)
            active_workers, total_workers = self._worker_counts(session.id)
            workers_text = (
                "%s/%s" % (active_workers, total_workers) if total_workers > 0 else "-"
            )
            mem = self._mem_by_session.get(session.id, 0)
            if mem >= 1024:
                mem_text = "%.1fGB" % (mem / 1024.0)
            elif mem > 0:
                mem_text = "%dMB" % mem
            else:
                mem_text = "-"
            cost = self._cost_by_session.get(session.id)
            cost_text = "$%.0f" % cost if cost and cost >= 1 else "-"

            # Git-style tree prefix
            if session.depth == 1:
                is_last = last_child.get(session.directory) == session.id
                branch = "\u2514\u2500 " if is_last else "\u251c\u2500 "
                display_icon = branch + icon
            elif session.depth == 2:
                is_last = last_child.get(session.parent_id) == session.id
                branch = "\u2502  \u2514\u2500 " if is_last else "\u2502  \u251c\u2500 "
                display_icon = branch + icon
            else:
                display_icon = icon

            table.add_row(
                display_icon,
                session.title,
                self._format_todos(session),
                workers_text,
                mem_text,
                cost_text,
                relative_time(session.updated),
                key=session.id,
            )
        # Restore cursor position
        if self._sessions:
            safe_idx = min(self._selected_index, len(self._sessions) - 1)
            table.move_cursor(row=safe_idx)
        self._refreshing = False

    # ── Detail / Intel panel ────────────────────────────

    def _format_worker_line(self, worker: BackgroundWorker, running: bool) -> str:
        agent_icon = AGENT_ICONS.get(worker.agent_type, _QUESTION)
        delta = worker.updated - worker.created
        secs = max(1, int(delta.total_seconds()))
        if secs < 60:
            duration = "%ss" % secs
        elif secs < 3600:
            duration = "%sm" % (secs // 60)
        else:
            duration = "%sh" % (secs // 3600)
        if running:
            return "  [green]%s %s %s:[/] %s [dim](%s, %s msgs)[/]" % (
                _PLAY, agent_icon, worker.agent_type,
                worker.description[:40], duration, worker.message_count,
            )
        return "  [dim]- %s %s: %s (%s, %s msgs)[/]" % (
            agent_icon, worker.agent_type,
            worker.description[:40], duration, worker.message_count,
        )

    def _render_detail(self) -> None:
        detail = self.query_one("#detail-body", Static)
        if not self._sessions:
            detail.update(" [dim]No sessions[/]")
            return
        if self._selected_index >= len(self._sessions):
            return

        session = self._sessions[self._selected_index]
        lines = []  # type: list

        # Group header: show repo path only
        if session.is_group_header:
            lines.append(" [bold cyan]%s[/]  %s" % (_FOLDER, session.title))
            lines.append(" [dim]%s[/]" % session.directory)
            detail.update("\n".join(lines))
            return

        # Session header with colored status
        cpu = self._running_cpu.get(session.id)
        status = session_status(session, cpu)
        status_rich = {
            "running": "[bold green]%s LIVE[/]" % _CIRCLE,
            "waiting": "[bold yellow]%s WAITING[/]" % _KEYBOARD,
            "stalled": "[bold red]%s STALLED[/]" % _WARN,
            "done": "[dim green]%s DONE[/]" % _CHECK_CIRCLE,
            "old": "[dim]%s OLD[/]" % _O,
        }
        status_label = status_rich.get(status, status)
        lines.append(" %s  [dim]%s[/]  %s" % (status_label, _PIPE, session.title))
        lines.append(" [dim]%s[/]" % (_HBAR * 40))

        # Memory
        mem = self._mem_by_session.get(session.id, 0)
        if mem > 0:
            if mem >= 1024:
                mem_str = "%.1fGB" % (mem / 1024.0)
            else:
                mem_str = "%dMB" % mem
            lines.append(" [dim]mem[/] %s" % mem_str)

        # Todos
        todos = self._todos_by_session.get(session.id, [])
        if todos:
            lines.append("")
            lines.append(" [cyan]%s Todos (%s/%s) %s[/]" % (
                _HBAR * 2, session.completed, session.total, _HBAR * 20))
            for todo in todos:
                icon = TODO_ICONS.get(todo.status, _SQ_O)
                lines.append("  %s %s" % (icon, todo.content))

        # Workers
        workers = self._workers_by_session.get(session.id, [])
        active_workers = [w for w in workers if self._is_worker_active(session.id, w)]
        recent_workers = [
            w
            for w in workers
            if w not in active_workers
            and (datetime.now() - w.updated) <= timedelta(hours=6)
        ]
        if active_workers or recent_workers:
            lines.append("")
            lines.append(
                " [cyan]%s Agents (%s live / %s total) %s[/]"
                % (_HBAR * 2, len(active_workers), len(workers), _HBAR * 16)
            )
            for worker in active_workers:
                lines.append(self._format_worker_line(worker, running=True))
            for worker in recent_workers[:8]:
                lines.append(self._format_worker_line(worker, running=False))
            if len(recent_workers) > 8:
                lines.append("  [dim]... %s more historical[/]" % (len(recent_workers) - 8))

        if len(lines) <= 2:
            lines.append("")
            lines.append("  [dim]No active todos or agents[/]")

        detail.update("\n".join(lines))

    # ── PR table ────────────────────────────────────────

    def _render_prs(self) -> None:
        table = self.query_one("#pr-table", DataTable)
        table.clear()
        if not self._snapshot:
            return

        # Sort: failures first, then pending, then passing
        ci_order = {CI_FAIL: 0, CI_PENDING: 1, CI_PASS: 2}
        prs = sorted(
            self._snapshot.prs[:10],
            key=lambda p: ci_order.get(p.ci_status, 1),
        )

        fail_count = sum(1 for p in prs if p.ci_status == CI_FAIL)

        # Update panel title to show failure count
        pr_title_widget = self.query_one("#pr-panel .panel-title", Static)
        if fail_count > 0:
            pr_title_widget.update(
                " %s PULL REQUESTS  [bold red]%s %s FAILING[/]"
                % (_FORK, _TIMES, fail_count)
            )
        else:
            pr_title_widget.update(" %s PULL REQUESTS" % _FORK)

        for pr in prs:
            title = pr.title if len(pr.title) <= 30 else pr.title[:27] + "..."

            if pr.ci_status == CI_FAIL:
                # RED everything for failures — this needs to be unmissable
                pr_col = Text("#%s" % pr.number, style="bold red")
                ci_col = Text("%s FAIL" % _TIMES, style="bold red")
                title_col = Text(title, style="bold red")
                review_col = Text(pr.review_status, style="red")
            elif pr.ci_status == CI_PASS:
                pr_col = Text("#%s" % pr.number)
                ci_col = Text("%s" % _CHECK, style="green")
                title_col = Text(title)
                review_col = Text(pr.review_status)
            else:
                pr_col = Text("#%s" % pr.number)
                ci_col = Text("%s" % _SPIN, style="yellow")
                title_col = Text(title)
                review_col = Text(pr.review_status)

            table.add_row(pr_col, ci_col, title_col, review_col)
    # ── Next Ops panel ──────────────────────────────────

    def _render_next_ops(self) -> None:
        body = self.query_one("#next-body", Static)
        if not self._snapshot:
            body.update(" No data")
            return

        recs = self._snapshot.recommendations[:8]
        if not recs:
            body.update(" %s All clear -- no action items" % _CHECK)
            return

        lines = []  # type: list
        for rec in recs:
            pri_icon = PRIORITY_ICONS.get(rec.priority, _O)
            pc = {"critical": "#ff2a6d", "high": "#ffb000", "medium": "#00d4ff", "low": "#3d5040"}.get(rec.priority, "#c9a8ff")
            lines.append(" [%s]%s %s[/] %s" % (pc, pri_icon, rec.icon, rec.text))
        body.update("\n".join(lines))

    # ── Comms panel (live event feed) ───────────────────

    def _render_comms(self) -> None:
        body = self.query_one("#comms-body", Static)
        if not self._log_events:
            body.update(" %s Listening..." % _SIGNAL)
            return

        lines = []  # type: list
        # Show most recent events, newest first
        events = list(self._log_events)
        events.reverse()
        for ev in events[:20]:
            display = EVENT_DISPLAY.get(ev.event_type)
            if display is None:
                icon = _INFO
                label = ev.event_type
            else:
                icon, label = display

            ts = "[dim]%s[/]" % ev.time_str

            if ev.event_type == "vcs.branch":
                from_b = ev.fields.get("from", "?")
                to_b = ev.fields.get("to", "?")
                if len(from_b) > 20:
                    from_b = from_b[:17] + "..."
                if len(to_b) > 20:
                    to_b = to_b[:17] + "..."
                text = " %s %s  %s [bold]%s[/]" % (ts, icon, from_b, to_b)
            elif ev.event_type == "session.error":
                text = " %s %s  [bold red]%s %s[/]" % (ts, icon, _EXCL, label)
            elif ev.event_type == "watchdog.kill":
                detail = ev.fields.get("detail", "process killed")
                text = " %s %s  [bold red]%s[/]" % (ts, icon, detail)
            elif ev.event_type == "ci.failed":
                text = " %s %s  [bold red]%s %s[/]" % (ts, icon, _EXCL, ev.raw)
            else:
                text = " %s %s  %s" % (ts, icon, label)

            lines.append(text)
        body.update("\n".join(lines))


    def _init_branch(self) -> None:
        """Get current git branch from the project path."""
        if self._current_branch:
            return  # already set from log backfill
        try:
            from .data import discover_project_path
            pp = discover_project_path()
            if pp:
                result = subprocess.run(
                    ["git", "-C", pp, "rev-parse", "--abbrev-ref", "HEAD"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if result.returncode == 0:
                    branch = result.stdout.strip()
                    if branch and branch != "HEAD":
                        self._current_branch = branch
        except Exception:
            pass


def main() -> None:
    app = OCDashboardApp()
    app.run()
