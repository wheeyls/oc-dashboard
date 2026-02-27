import os
import signal
import subprocess
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widgets import DataTable, Input, Static

from .data import BackgroundWorker
from .data import DashboardSnapshot
from .data import SessionSummary
from .data import TodoItem
from .data import build_snapshot
from .data import relative_time
from .data import session_status
from .data import CI_FAIL
from .data import LogEvent, get_wal_mtime, find_latest_log, parse_log_line
from .data import fetch_running_processes
from .kanban import (
    STAGES,
    STAGE_LABELS,
    KanbanProject,
    LocalJsonKanban,
)


# ── Nerd Font icons ─────────────────────────────────────
# Font Awesome (U+F000-U+F2E0)
_CHECK = chr(0xF00C)
_TIMES = chr(0xF00D)
_COG = chr(0xF013)
_SEARCH = chr(0xF002)
_BOOK = chr(0xF02D)
_TAG = chr(0xF02B)
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
_FOLDER = chr(0xF07B)

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

STAGE_ICONS = {
    "pending": _SQ_O,
    "in_progress": _SPIN,
    "pr": _FORK,
    "done": _CHECK_CIRCLE,
}

# ── Event display mapping for Comms panel ───────────────
EVENT_DISPLAY = {
    "session.error": (_EXCL, "ERROR"),
    "session.diff": (_COG, "files changed"),
    "session.compacted": (_COG, "session compacted"),
    "vcs.branch": (_BRANCH_ICON, ""),  # special: shows from->to
    "command.executed": (_TERM, "command"),
    "watchdog.kill": (_SKULL, "KILLED"),
    "ci.failed": (_TIMES, "CI FAILED"),
}

ACTIVE_WORKER_WINDOW = timedelta(minutes=3)
COMMS_MAX_EVENTS = 16
MEM_KILL_THRESHOLD_MB = 8192  # 8GB

# ── Kanban input modes ──────────────────────────────────
MODE_NORMAL = "normal"
MODE_ADD_TITLE = "add_title"
MODE_ADD_DESC = "add_desc"
MODE_LINK_SESSION = "link_session"
MODE_LINK_PR = "link_pr"


def _in_tmux():
    # type: () -> bool
    return bool(os.environ.get("TMUX"))


def _launch_session_in_tmux(session_id, project_path=None):
    # type: (str, ...) -> None
    """Open an opencode session in a new tmux pane."""
    oc_cmd = "opencode -s %s" % session_id
    if project_path:
        oc_cmd = "cd %s && %s" % (project_path, oc_cmd)

    if _in_tmux():
        try:
            subprocess.Popen(
                ["tmux", "split-window", "-h", "-l", "50%", oc_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    else:
        apple_script = 'tell application "Terminal" to do script "%s"' % oc_cmd.replace(
            '"', '\\"'
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
        Binding("h", "col_left", "Left col", show=False),
        Binding("l", "col_right", "Right col", show=False),
        Binding("left", "col_left", "Left col", show=False),
        Binding("right", "col_right", "Right col", show=False),
        Binding("j", "item_down", "Down", show=False),
        Binding("k", "item_up", "Up", show=False),
        Binding("down", "item_down", "Down", show=False),
        Binding("up", "item_up", "Up", show=False),
        Binding("m", "move_right", "Move right"),
        Binding("shift+m", "move_left", "Move left", show=False),
        Binding("a", "add_project", "Add"),
        Binding("s", "link_session", "Link session"),
        Binding("p", "link_pr", "Link PR"),
        Binding("d", "delete_project", "Delete"),
        Binding("enter", "activate_selected", "Open"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._snapshot = None  # type: Optional[DashboardSnapshot]
        self._sessions = []  # type: list
        self._todos_by_session = {}  # type: dict
        self._workers_by_session = {}  # type: dict
        self._running_cpu = {}  # type: dict
        self._cost_by_session = {}  # type: dict
        self._daily_spend = []  # type: list
        self._mem_by_session = {}  # type: dict
        self._unattributed_cpu = 0.0
        self._selected_index = 0
        self._tick_count = 0
        self._refreshing = False
        # Live features state
        self._wal_mtime = None  # type: Optional[float]
        self._log_path = None  # type: Optional[str]
        self._log_handle = None  # type: Optional[Any]
        self._log_events = deque(maxlen=COMMS_MAX_EVENTS)  # type: deque
        self._current_branch = ""
        self._error_flash_until = None  # type: Optional[datetime]
        self._total_cost = 0.0
        self._watchdog_warned = set()  # type: set
        self._prev_ci_fail_count = 0
        # Kanban state
        self._kanban = LocalJsonKanban()
        self._col_idx = 0
        self._row_idx = {}  # type: Dict[str, int]
        for s in STAGES:
            self._row_idx[s] = 0
        self._projects_by_stage = {}  # type: Dict[str, List[KanbanProject]]
        self._mode = MODE_NORMAL
        self._pending_title = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        with Container(id="main"):
            with Container(id="top-row"):
                with Container(id="kanban-area"):
                    for stage in STAGES:
                        with Container(id="col-%s" % stage, classes="kanban-column"):
                            yield Static(
                                "", id="title-%s" % stage, classes="column-title"
                            )
                            yield Static(
                                "", id="body-%s" % stage, classes="column-body"
                            )
                with Container(id="detail-panel"):
                    yield Static(" %s PROJECT" % _EYE, classes="panel-title")
                    yield Static("", id="detail-body")
            with Container(id="bottom-row"):
                with Container(id="sessions-panel", classes="panel"):
                    yield Static(" %s SESSIONS" % _LIST, classes="panel-title")
                    yield DataTable(id="sessions-table")
        with Container(id="kanban-input-bar"):
            yield Static("", id="kanban-input-label")
            yield Input(id="kanban-input", placeholder="")
        yield Static("", id="footerbar")

    def on_mount(self) -> None:
        sessions_table = self.query_one("#sessions-table", DataTable)
        _ = sessions_table.add_columns(
            "", "Title", "Todos", "Bots", "Mem", "Cost", "Age"
        )
        sessions_table.cursor_type = "row"
        sessions_table.can_focus = False


        # Initialize WAL mtime for change detection
        self._wal_mtime = get_wal_mtime()

        # Open latest log file and seek to end
        self._open_latest_log()

        # Seed current branch from git
        self._init_branch()

        # Kanban
        self._refresh_kanban()

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
        app = self.app if hasattr(self, "app") else self
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
            return
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
        try:
            fh = open(latest, "r", encoding="utf-8", errors="replace")
            try:
                fh.seek(0, 2)
                end_pos = fh.tell()
                backfill_pos = max(0, end_pos - 8192)
                fh.seek(backfill_pos)
                if backfill_pos > 0:
                    fh.readline()
                for backfill_line in fh:
                    ev = parse_log_line(backfill_line)
                    if ev is not None:
                        self._log_events.append(ev)
                        if ev.event_type == "vcs.branch":
                            to_branch = ev.fields.get("to", "")
                            if to_branch and to_branch != "HEAD":
                                self._current_branch = to_branch
            except Exception:
                fh.seek(0, 2)
            self._log_handle = fh
            self._log_path = latest
        except Exception:
            self._log_handle = None
            self._log_path = None

    def _poll_log(self) -> None:
        """Read new lines from log file, parse events, update comms."""
        latest = find_latest_log()
        if latest and latest != self._log_path:
            self._open_latest_log()

        if self._log_handle is None:
            return

        new_events = False
        try:
            for _ in range(200):
                line = self._log_handle.readline()
                if not line:
                    break
                event = parse_log_line(line)
                if event is None:
                    continue
                self._log_events.append(event)
                new_events = True

                if event.event_type == "vcs.branch":
                    to_branch = event.fields.get("to", "")
                    if to_branch and to_branch != "HEAD":
                        self._current_branch = to_branch

                if event.event_type == "session.error":
                    self._error_flash_until = datetime.now() + timedelta(seconds=5)
        except Exception:
            self._log_handle = None
            self._log_path = None

        if new_events:
            self._render_topbar()
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

            session_label = "unknown session"
            if proc.session_id:
                for s in self._sessions:
                    if s.id == proc.session_id:
                        session_label = s.title[:40]
                        break

            mem_gb = proc.mem_mb / 1024.0

            if proc.pid in self._watchdog_warned:
                try:
                    os.kill(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                msg = "SIGKILL PID %d (%.1fGB) — %s" % (proc.pid, mem_gb, session_label)
                self._watchdog_warned.discard(proc.pid)
            else:
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
            self._render_topbar()
            self._render_topbar()

    def _tick(self) -> None:
        self._tick_count += 1
        self._render_topbar()
        self._render_footerbar()

    # ── Actions ────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._refresh_kanban()
        _ = self.refresh_dashboard()


    def action_col_left(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        if self._col_idx > 0:
            self._col_idx -= 1
            self._render_kanban()

    def action_col_right(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        if self._col_idx < len(STAGES) - 1:
            self._col_idx += 1
            self._render_kanban()

    def action_item_down(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        stage = STAGES[self._col_idx]
        count = len(self._projects_by_stage.get(stage, []))
        if count > 0 and self._row_idx[stage] < count - 1:
            self._row_idx[stage] += 1
            self._render_kanban()

    def action_item_up(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        stage = STAGES[self._col_idx]
        if self._row_idx[stage] > 0:
            self._row_idx[stage] -= 1
            self._render_kanban()

    def action_move_right(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        idx = STAGES.index(project.stage)
        if idx < len(STAGES) - 1:
            self._kanban.move_project(project.id, STAGES[idx + 1])
            self._refresh_kanban()

    def action_move_left(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        idx = STAGES.index(project.stage)
        if idx > 0:
            self._kanban.move_project(project.id, STAGES[idx - 1])
            self._refresh_kanban()

    def action_add_project(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        self._mode = MODE_ADD_TITLE
        self._show_input("Project title:", "e.g. Refactor auth flow")

    def action_link_session(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        self._mode = MODE_LINK_SESSION
        self._show_input("Session ID or search term:", "ses_... or partial title")

    def action_link_pr(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        self._mode = MODE_LINK_PR
        self._show_input("PR number:", "#12345")

    def action_delete_project(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        self._kanban.delete_project(project.id)
        self._refresh_kanban()

    def action_activate_selected(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if project and project.session_ids:
            session_id = project.session_ids[0]
            project_path = self._snapshot.project_path if self._snapshot else None
            _launch_session_in_tmux(session_id, project_path)

    # ── Input bar ──────────────────────────────────────────────────────

    def _show_input(self, label, placeholder=""):
        # type: (str, str) -> None
        bar = self.query_one("#kanban-input-bar", Container)
        bar.add_class("visible")
        lbl = self.query_one("#kanban-input-label", Static)
        lbl.update(" %s" % label)
        inp = self.query_one("#kanban-input", Input)
        inp.value = ""
        inp.placeholder = placeholder
        inp.focus()

    def _hide_input(self) -> None:
        bar = self.query_one("#kanban-input-bar", Container)
        bar.remove_class("visible")
        self._mode = MODE_NORMAL
        self.set_focus(None)

    def on_input_submitted(self, event) -> None:
        # type: (Input.Submitted) -> None
        value = event.value.strip()

        if self._mode == MODE_ADD_TITLE:
            if value:
                self._pending_title = value
                self._mode = MODE_ADD_DESC
                self._show_input(
                    "Description (optional, enter to skip):", "Brief description..."
                )
            else:
                self._hide_input()
            return

        if self._mode == MODE_ADD_DESC:
            self._kanban.create_project(
                title=self._pending_title,
                description=value,
                stage=STAGES[self._col_idx],
            )
            self._pending_title = ""
            self._hide_input()
            self._refresh_kanban()
            return

        if self._mode == MODE_LINK_SESSION:
            if value:
                project = self._selected_project()
                if project:
                    matched = None
                    for s in self._sessions:
                        if (
                            s.id == value
                            or value in s.id
                            or value.lower() in s.title.lower()
                        ):
                            matched = s.id
                            break
                    if matched:
                        self._kanban.link_session(project.id, matched)
                    else:
                        self._kanban.link_session(project.id, value)
            self._hide_input()
            self._refresh_kanban()
            return

        if self._mode == MODE_LINK_PR:
            if value:
                project = self._selected_project()
                if project:
                    try:
                        pr_num = int(value.lstrip("#"))
                        self._kanban.link_pr(project.id, pr_num)
                    except ValueError:
                        pass
            self._hide_input()
            self._refresh_kanban()
            return

        self._hide_input()

    def on_key(self, event) -> None:
        if self._mode != MODE_NORMAL:
            if event.key == "escape":
                self._hide_input()
                event.prevent_default()
            return

    # ── Event handlers ─────────────────────────────────────────────────

    def on_data_table_row_highlighted(
        self, event_obj: DataTable.RowHighlighted
    ) -> None:
        table = event_obj.data_table
        if table.id != "sessions-table":
            return
        if self._refreshing:
            return
        if event_obj.cursor_row is not None:
            self._selected_index = event_obj.cursor_row

    def on_data_table_row_selected(self, event_obj: DataTable.RowSelected) -> None:
        table = event_obj.data_table
        if table.id == "sessions-table":
            self._open_selected_session()

    # ── Data ───────────────────────────────────────────────────────────

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
        self._daily_spend = []
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
        self._daily_spend = snapshot.daily_spend

        if self._selected_index >= len(self._sessions):
            self._selected_index = max(0, len(self._sessions) - 1)

        # Flash topbar if CI failures increased
        ci_fails = sum(1 for p in snapshot.prs if p.ci_status == CI_FAIL)
        if ci_fails > self._prev_ci_fail_count and ci_fails > 0:
            self._error_flash_until = datetime.now() + timedelta(seconds=10)
            self._log_events.append(
                LogEvent(
                    time_str=datetime.now().strftime("%H:%M:%S"),
                    event_type="ci.failed",
                    fields={"detail": "%d PR(s) failing CI" % ci_fails},
                )
            )
        self._prev_ci_fail_count = ci_fails

        self._render_topbar()
        self._render_sessions()
        self._render_detail()

    # ── Kanban data ────────────────────────────────────────────────────

    def _refresh_kanban(self) -> None:
        projects = self._kanban.list_projects()
        self._projects_by_stage = {}
        for s in STAGES:
            self._projects_by_stage[s] = []
        for p in projects:
            stage = p.stage if p.stage in STAGES else "pending"
            self._projects_by_stage[stage].append(p)
        for s in STAGES:
            self._projects_by_stage[s].sort(key=lambda p: p.updated_at, reverse=True)
        for s in STAGES:
            count = len(self._projects_by_stage[s])
            if count == 0:
                self._row_idx[s] = 0
            elif self._row_idx[s] >= count:
                self._row_idx[s] = count - 1
        self._render_kanban()

    def _selected_project(self):
        # type: () -> Optional[KanbanProject]
        stage = STAGES[self._col_idx]
        items = self._projects_by_stage.get(stage, [])
        idx = self._row_idx.get(stage, 0)
        if 0 <= idx < len(items):
            return items[idx]
        return None

    # ── Rendering: Kanban ──────────────────────────────────────────────

    def _render_kanban(self) -> None:
        for stage in STAGES:
            self._render_kanban_column(stage)
        self._highlight_active_column()
        self._render_detail()

    def _render_kanban_column(self, stage):
        # type: (str) -> None
        items = self._projects_by_stage.get(stage, [])
        icon = STAGE_ICONS.get(stage, _O)
        count = len(items)
        is_active = STAGES[self._col_idx] == stage
        row_sel = self._row_idx.get(stage, 0)

        title_widget = self.query_one("#title-%s" % stage, Static)
        title_widget.update(" %s %s (%d)" % (icon, STAGE_LABELS[stage], count))

        lines = []  # type: list
        if not items:
            lines.append("  [dim]empty[/]")
        else:
            for i, project in enumerate(items):
                prefix = "[bold green]>[/] " if (is_active and i == row_sel) else "  "
                title = project.title
                if len(title) > 28:
                    title = title[:25] + "..."
                meta_parts = []
                if project.session_ids:
                    meta_parts.append("%d sess" % len(project.session_ids))
                if project.pr_numbers:
                    meta_parts.append("%d PR" % len(project.pr_numbers))
                meta = " [dim](%s)[/]" % ", ".join(meta_parts) if meta_parts else ""

                if is_active and i == row_sel:
                    lines.append("%s[bold green]%s[/]%s" % (prefix, title, meta))
                else:
                    lines.append("%s%s%s" % (prefix, title, meta))

        body_widget = self.query_one("#body-%s" % stage, Static)
        body_widget.update("\n".join(lines))

    def _highlight_active_column(self) -> None:
        for i, stage in enumerate(STAGES):
            col = self.query_one("#col-%s" % stage, Container)
            if i == self._col_idx:
                col.add_class("active-column")
            else:
                col.remove_class("active-column")

    # ── Rendering: Topbar ──────────────────────────────────────────────

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

        if self._total_cost >= 1:
            parts.append(
                "%s %s $%s" % (_SEP, _DOLLAR, "{:,.0f}".format(self._total_cost))
            )

        if self._current_branch:
            branch_display = self._current_branch
            if len(branch_display) > 28:
                branch_display = branch_display[:25] + "..."
            parts.append("%s %s %s" % (_SEP, _BRANCH_ICON, branch_display))

        ci_fails = sum(
            1
            for p in (self._snapshot.prs if self._snapshot else [])
            if p.ci_status == CI_FAIL
        )
        if ci_fails > 0:
            parts.append("%s [bold red]%s %s CI FAIL[/]" % (_SEP, _TIMES, ci_fails))
        topbar = self.query_one("#topbar", Static)

        if self._error_flash_until and datetime.now() < self._error_flash_until:
            topbar.add_class("error-flash")
        else:
            topbar.remove_class("error-flash")
            self._error_flash_until = None

        topbar.update("  ".join(parts))

    def _render_footerbar(self) -> None:
        self.query_one("#footerbar", Static).update(
            " q:quit %s r:refresh %s h/l:columns %s j/k:select %s m:move %s a:add %s enter:open-sess"
            % (_PIPE, _PIPE, _PIPE, _PIPE, _PIPE, _PIPE)
        )

    # ── Rendering: Detail (kanban project) ─────────────────────────────

    def _render_detail(self) -> None:
        detail = self.query_one("#detail-body", Static)
        project = self._selected_project()
        if not project:
            detail.update(" [dim]No project selected. Press 'a' to create one.[/]")
            return

        lines = []  # type: list
        lines.append(
            " [bold cyan]%s[/]  [dim]%s[/]  [dim]id:%s[/]"
            % (
                project.title,
                STAGE_LABELS.get(project.stage, project.stage),
                project.id,
            )
        )
        if project.description:
            desc = project.description
            if len(desc) > 80:
                desc = desc[:77] + "..."
            lines.append(" [dim]%s[/]" % desc)

        lines.append("")

        # Sessions
        if project.session_ids:
            sess_parts = []
            for sid in project.session_ids[:5]:
                title = sid[:12]
                for s in self._sessions:
                    if s.id == sid:
                        title = s.title[:20]
                        break
                sess_parts.append("[cyan]%s[/]" % title)
            sess_line = " %s Sessions: %s" % (_TERM, "  ".join(sess_parts))
            if len(project.session_ids) > 5:
                sess_line += "  [dim]+%d more[/]" % (len(project.session_ids) - 5)
            lines.append(sess_line)
        else:
            lines.append(" [dim]No sessions linked. Press 's' to link one.[/]")

        # PRs
        if project.pr_numbers:
            pr_parts = []
            prs = self._snapshot.prs if self._snapshot else []
            for num in project.pr_numbers[:5]:
                status = ""
                for pr in prs:
                    if pr.number == num:
                        status = " %s" % pr.ci_status
                        break
                pr_parts.append("[cyan]#%d[/]%s" % (num, status))
            lines.append(" %s PRs: %s" % (_FORK, "  ".join(pr_parts)))

        # Tags
        if project.tags:
            lines.append(
                " %s %s" % (_TAG, "  ".join("[dim]%s[/]" % t for t in project.tags))
            )

        detail.update("\n".join(lines))

    # ── Rendering: Sessions table ──────────────────────────────────────

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

        last_child = {}  # type: dict
        for session in self._sessions:
            if session.depth == 1:
                last_child[session.directory] = session.id
            elif session.depth == 2 and session.parent_id:
                last_child[session.parent_id] = session.id

        for session in self._sessions:
            if session.is_group_header:
                table.add_row(
                    _FOLDER,
                    session.title,
                    "",
                    "",
                    "",
                    "",
                    "",
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
        if self._sessions:
            safe_idx = min(self._selected_index, len(self._sessions) - 1)
            table.move_cursor(row=safe_idx)
        self._refreshing = False


    # ── Helpers ────────────────────────────────────────────────────────

    def _open_selected_session(self) -> None:
        if not self._sessions:
            return
        if self._selected_index >= len(self._sessions):
            return
        session = self._sessions[self._selected_index]
        if session.is_group_header:
            return
        project_path = session.directory or (
            self._snapshot.project_path if self._snapshot else None
        )
        _launch_session_in_tmux(session.id, project_path)


    def _init_branch(self) -> None:
        """Get current git branch from the project path."""
        if self._current_branch:
            return
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
