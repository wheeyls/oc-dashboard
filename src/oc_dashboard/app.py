import os
import signal
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import DataTable, Input, OptionList, Static
from textual.widgets.option_list import Option

from .core import Dashboard, SearchResult, _launch_session_interactive
from .data import (
    CI_FAIL,
    DashboardSnapshot,
    LogEvent,
    SessionSummary,
    fetch_running_processes,
    find_latest_log,
    get_wal_mtime,
    parse_log_line,
    relative_time,
    session_status,
)
from .kanban import (
    STAGES,
    STAGE_LABELS,
    KanbanProject,
    LocalJsonKanban,
)
from .opencode import HttpOpenCodeClient, ensure_server


# ── Nerd Font icons ─────────────────────────────────────
_CHECK = chr(0xF00C)
_TIMES = chr(0xF00D)
_COG = chr(0xF013)
_SEARCH = chr(0xF002)
_TAG = chr(0xF02B)
_LIST = chr(0xF03A)
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
_ROCKET = chr(0xF135)
_BOLT = chr(0xF0E7)
_SKULL = chr(0xF05E)
_DOLLAR = chr(0xF155)
_SIGNAL = chr(0xF012)
_CHECK_CIRCLE = chr(0xF058)
_KEYBOARD = chr(0xF11C)
_HG = [chr(0xF250), chr(0xF251), chr(0xF252), chr(0xF253)]
_SEP = chr(0xE0B1)
_BRANCH_ICON = chr(0xE0A0)
_PIPE = chr(0x2502)
_FOLDER = chr(0xF07B)

STATUS_ICONS = {
    "running": _CIRCLE,
    "waiting": _KEYBOARD,
    "stalled": _WARN,
    "done": _CHECK_CIRCLE,
    "old": _O,
}

STAGE_ICONS = {
    "pending": _SQ_O,
    "in_progress": _SPIN,
    "done": _CHECK_CIRCLE,
}

ACTIVE_WORKER_WINDOW = timedelta(minutes=3)
COMMS_MAX_EVENTS = 16
MEM_KILL_THRESHOLD_MB = 8192

MODE_NORMAL = "normal"
MODE_ADD_TITLE = "add_title"
MODE_ADD_DESC = "add_desc"
MODE_LINK_SESSION = "link_session"
MODE_UNLINK_SESSION = "unlink_session"
MODE_LINK_PR = "link_pr"


# ══════════════════════════════════════════════════════════
#  KanbanList — OptionList with vim keys
# ══════════════════════════════════════════════════════════


class KanbanList(OptionList):
    """Kanban column list. j/k navigate, tab/shift-tab cycle columns."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def on_focus(self) -> None:
        if self.highlighted is None and self.option_count > 0:
            self.action_first()
        # Update detail panel after focus settles
        self.call_later(self._update_detail)

    def _update_detail(self) -> None:
        render_detail = getattr(self.app, "_render_detail", None)
        if callable(render_detail):
            render_detail()


# ══════════════════════════════════════════════════════════
#  Sessions Screen (pushed via Shift+S)
# ══════════════════════════════════════════════════════════


class SessionsScreen(Screen):
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("S", "pop_screen", "Back", show=False),
        Binding("q", "pop_screen", "Back", show=False),
        Binding("enter", "open_session", "Open"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def __init__(self, app_ref):
        # type: (OCDashboardApp) -> None
        super().__init__()
        self._app_ref = app_ref

    def compose(self) -> ComposeResult:
        yield Static("", id="sessions-topbar")
        yield DataTable(id="sessions-body")
        yield Static("", id="sessions-footer")

    def on_mount(self) -> None:
        table = self.query_one("#sessions-body", DataTable)
        _ = table.add_columns("", "Title", "Todos", "Bots", "Mem", "Cost", "Age")
        table.cursor_type = "row"
        self._render_sessions()
        self.query_one("#sessions-topbar", Static).update(
            " %s %s SESSIONS" % (_LIST, _TERM)
        )
        self.query_one("#sessions-footer", Static).update(
            " esc/S:back %s j/k:nav %s enter:open" % (_PIPE, _PIPE)
        )

    def _render_sessions(self) -> None:
        a = self._app_ref
        table = self.query_one("#sessions-body", DataTable)
        table.clear()

        last_child = {}  # type: dict
        for session in a._sessions:
            if session.depth == 1:
                last_child[session.directory] = session.id
            elif session.depth == 2 and session.parent_id:
                last_child[session.parent_id] = session.id

        for session in a._sessions:
            if session.is_group_header:
                table.add_row(
                    _FOLDER, session.title, "", "", "", "", "", key=session.id
                )
                continue

            cpu = a._running_cpu.get(session.id)
            status = session_status(session, cpu)
            icon = STATUS_ICONS.get(status, _O)
            workers = a._workers_by_session.get(session.id, [])
            active_w = sum(
                1
                for w in workers
                if session.id in a._running_cpu
                and (datetime.now() - w.updated) <= ACTIVE_WORKER_WINDOW
            )
            workers_text = "%s/%s" % (active_w, len(workers)) if workers else "-"
            mem = a._mem_by_session.get(session.id, 0)
            if mem >= 1024:
                mem_text = "%.1fGB" % (mem / 1024.0)
            elif mem > 0:
                mem_text = "%dMB" % mem
            else:
                mem_text = "-"
            cost = a._cost_by_session.get(session.id)
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
                self._fmt_todos(session),
                workers_text,
                mem_text,
                cost_text,
                relative_time(session.updated),
                key=session.id,
            )

    def _fmt_todos(self, session):
        # type: (SessionSummary) -> str
        if session.total == 0:
            return "-"
        if session.pending == 0 and session.in_progress == 0:
            return "%s/%s %s" % (session.completed, session.total, _CHECK)
        return "%s/%s" % (session.completed, session.total)

    def action_pop_screen(self) -> None:
        self.app.pop_screen()

    def action_cursor_down(self) -> None:
        self.query_one("#sessions-body", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#sessions-body", DataTable).action_cursor_up()

    def action_open_session(self) -> None:
        table = self.query_one("#sessions-body", DataTable)
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return
        self._open_by_key(str(cell_key.row_key.value))

    def on_data_table_row_selected(self, event_obj):
        # type: (DataTable.RowSelected) -> None
        self._open_by_key(str(event_obj.row_key.value))

    def _open_by_key(self, session_id):
        # type: (str) -> None
        a = self._app_ref
        session = None
        for s in a._sessions:
            if s.id == session_id:
                session = s
                break
        if not session or session.is_group_header:
            return
        project_path = session.directory or (
            a._snapshot.project_path if a._snapshot else None
        )
        _launch_session_interactive(session.id, project_path)


# ══════════════════════════════════════════════════════════
#  Session Picker Screen (for opening linked sessions)
# ══════════════════════════════════════════════════════════


class SessionPickerScreen(Screen):
    """Pick a session from a project's linked sessions to open in tmux."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back", show=False),
        Binding("enter", "pick_session", "Open"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def __init__(self, session_ids, sessions, project_path=None):
        # type: (List[str], list, Optional[str]) -> None
        super().__init__()
        self._session_ids = session_ids
        self._sessions = sessions
        self._project_path = project_path

    def compose(self) -> ComposeResult:
        yield Static("", id="picker-topbar")
        yield OptionList(id="picker-list")
        yield Static("", id="picker-footer")

    def on_mount(self) -> None:
        self.query_one("#picker-topbar", Static).update(" %s PICK SESSION" % _TERM)
        self.query_one("#picker-footer", Static).update(
            " esc:back %s j/k:nav %s enter:open" % (_PIPE, _PIPE)
        )
        ol = self.query_one("#picker-list", OptionList)
        for sid in self._session_ids:
            title = sid[:16]
            for s in self._sessions:
                if s.id == sid:
                    title = s.title[:40]
                    break
            ol.add_option(Option("%s  [dim]%s[/]" % (title, sid[:16]), id=sid))
        if ol.option_count > 0:
            ol.highlighted = 0
        ol.focus()

    def action_pop_screen(self) -> None:
        self.app.pop_screen()

    def action_cursor_down(self) -> None:
        self.query_one("#picker-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#picker-list", OptionList).action_cursor_up()

    def action_pick_session(self) -> None:
        ol = self.query_one("#picker-list", OptionList)
        idx = ol.highlighted
        if idx is None or idx < 0 or idx >= len(self._session_ids):
            return
        session_id = self._session_ids[idx]
        _launch_session_interactive(session_id, self._project_path)
        self.app.pop_screen()

    def on_option_list_option_selected(self, event):
        # type: (OptionList.OptionSelected) -> None
        event.stop()
        self.action_pick_session()


# ══════════════════════════════════════════════════════════
#  Archive Screen (pushed via Shift+A)
# ══════════════════════════════════════════════════════════


class ArchiveScreen(Screen):
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("A", "pop_screen", "Back", show=False),
        Binding("q", "pop_screen", "Back", show=False),
        Binding("enter", "restore_project", "Restore"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def __init__(self, app_ref):
        # type: (OCDashboardApp) -> None
        super().__init__()
        self._app_ref = app_ref
        self._archived = []  # type: List[KanbanProject]

    def compose(self) -> ComposeResult:
        yield Static("", id="archive-topbar")
        yield OptionList(id="archive-list")
        yield Static("", id="archive-footer")

    def on_mount(self) -> None:
        self._archived = self._app_ref._dashboard.list_archived()
        self.query_one("#archive-topbar", Static).update(
            " %s ARCHIVE  [dim]%d project(s)[/]" % (_LIST, len(self._archived))
        )
        self.query_one("#archive-footer", Static).update(
            " esc/A:back %s j/k:nav %s enter:restore" % (_PIPE, _PIPE)
        )
        ol = self.query_one("#archive-list", OptionList)
        for project in self._archived:
            label = Text()
            title = project.title
            if len(title) > 40:
                title = title[:37] + "..."
            label.append(title, style="bold")
            if project.description:
                desc = project.description
                if len(desc) > 50:
                    desc = desc[:47] + "..."
                label.append("\n")
                label.append(desc, style="dim")
            restore_to = project.previous_stage or "done"
            restore_label = STAGE_LABELS.get(restore_to, restore_to)
            label.append("\n")
            label.append(
                "archived %s  \u2192 %s" % (project.updated_at[:10], restore_label),
                style="dim italic",
            )
            ol.add_option(Option(label, id=project.id))
        if ol.option_count > 0:
            ol.highlighted = 0
        elif ol.option_count == 0:
            ol.add_option(Option(Text("(empty)", style="dim")))
        ol.focus()

    def action_pop_screen(self) -> None:
        self.app.pop_screen()

    def action_cursor_down(self) -> None:
        self.query_one("#archive-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#archive-list", OptionList).action_cursor_up()

    def action_restore_project(self) -> None:
        ol = self.query_one("#archive-list", OptionList)
        idx = ol.highlighted
        if idx is None or idx < 0 or idx >= len(self._archived):
            return
        project = self._archived[idx]
        self._app_ref._dashboard.restore_project(project.id)
        self._app_ref._refresh_kanban()
        self.app.pop_screen()

    def on_option_list_option_selected(self, event):
        # type: (OptionList.OptionSelected) -> None
        event.stop()
        self.action_restore_project()


# ══════════════════════════════════════════════════════════
#  Search Screen (pushed via /)
# ══════════════════════════════════════════════════════════


class SearchScreen(Screen):
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back", show=False),
    ]

    def __init__(self, app_ref):
        # type: (OCDashboardApp) -> None
        super().__init__()
        self._app_ref = app_ref
        self._results = []  # type: List[SearchResult]

    def compose(self) -> ComposeResult:
        yield Static("", id="search-topbar")
        yield Input(id="search-input", placeholder="Search projects, sessions, PRs...")
        yield OptionList(id="search-results")
        yield Static("", id="search-footer")

    def on_mount(self) -> None:
        self.query_one("#search-topbar", Static).update(" %s SEARCH" % _SEARCH)
        self.query_one("#search-footer", Static).update(
            " esc:back %s enter:search %s j/k:nav %s enter:open" % (_PIPE, _PIPE, _PIPE)
        )
        self.query_one("#search-input", Input).focus()

    def on_input_submitted(self, event):
        # type: (Input.Submitted) -> None
        query = event.value.strip()
        if not query:
            return
        self._results = self._app_ref._dashboard.search(query)
        self._render_results()
        if self._results:
            self.query_one("#search-results", OptionList).focus()

    def _render_results(self):
        # type: () -> None
        ol = self.query_one("#search-results", OptionList)
        ol.clear_options()
        if not self._results:
            ol.add_option(Option(Text("(no results)", style="dim")))
            return
        for result in self._results:
            label = Text()
            if result.kind == "project":
                icon = STAGE_ICONS.get(result.stage, _O)
                stage_text = STAGE_LABELS.get(result.stage, result.stage)
                label.append("%s " % icon, style="bold")
                title = result.title
                if len(title) > 50:
                    title = title[:47] + "..."
                label.append(title, style="bold")
                label.append("  ")
                label.append(stage_text, style="dim italic")
                if result.pr_numbers:
                    prs = ", ".join("#%d" % pr for pr in result.pr_numbers)
                    label.append("  %s" % prs, style="dim")
                if result.detail and result.detail != stage_text:
                    label.append("\n")
                    detail = result.detail
                    if len(detail) > 60:
                        detail = detail[:57] + "..."
                    label.append("  %s" % detail, style="dim")
            else:
                label.append("%s " % _TERM, style="bold")
                title = result.title
                if len(title) > 50:
                    title = title[:47] + "..."
                label.append(title, style="bold")
                label.append("\n")
                label.append("  %s" % result.id[:24], style="dim")
            ol.add_option(Option(label, id=result.id))
        if ol.option_count > 0:
            ol.highlighted = 0

    def on_option_list_option_selected(self, event):
        # type: (OptionList.OptionSelected) -> None
        event.stop()
        self._open_selected()

    def _open_selected(self):
        # type: () -> None
        ol = self.query_one("#search-results", OptionList)
        idx = ol.highlighted
        if idx is None or idx < 0 or idx >= len(self._results):
            return
        result = self._results[idx]
        if result.kind == "session":
            project_path = None
            snap = self._app_ref._snapshot
            if snap:
                project_path = snap.project_path
            _launch_session_interactive(result.id, project_path)
        elif result.kind == "project":
            self._app_ref._focus_project_after_search = result.id
            self.app.pop_screen()

    def action_pop_screen(self) -> None:
        self.app.pop_screen()


# ══════════════════════════════════════════════════════════
#  Main App — Kanban board fills the screen
# ══════════════════════════════════════════════════════════


class OCDashboardApp(App[None]):
    CSS_PATH = "app.tcss"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (80, "-wide")]
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("S", "show_sessions", "Sessions"),
        Binding("m", "move_right", "Move \u2192"),
        Binding("M", "move_left", "Move \u2190", show=False),
        Binding("a", "add_project", "Add"),
        Binding("s", "link_session", "Link sess"),
        Binding("u", "unlink_session", "Unlink sess"),
        Binding("p", "link_pr", "Link PR"),
        Binding("d", "archive_project", "Archive"),
        Binding("A", "show_archive", "Archive \u2630"),
        Binding("/", "show_search", "Search"),
        Binding("n", "wheel_next", "Next \u27f3"),
        Binding("N", "wheel_prev", "Prev \u27f3", show=False),
        Binding("w", "wheel_add", "Wheel+"),
        Binding("W", "wheel_remove", "Wheel-", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        # ── Core domain (all state lives here) ─────────────
        server_url = ensure_server()
        oc_client = HttpOpenCodeClient(server_url or "http://127.0.0.1:4096")
        kanban = LocalJsonKanban()
        self._dashboard = Dashboard(kanban, oc_client)
        # ── UI-only state ──────────────────────────────────
        self._tick_count = 0
        self._wal_mtime = None  # type: Optional[float]
        self._log_path = None  # type: Optional[str]
        self._log_handle = None  # type: Optional[Any]
        self._log_events = deque(maxlen=COMMS_MAX_EVENTS)  # type: deque[LogEvent]
        self._error_flash_until = None  # type: Optional[datetime]
        self._watchdog_warned = set()  # type: set[int]
        self._mode = MODE_NORMAL
        self._pending_title = ""
        self._last_focused_stage = "pending"
        self._focus_project_after_search = None  # type: Optional[str]

    # ── State accessors (delegate to Dashboard) ────────────

    @property
    def _sessions(self):
        # type: () -> List[SessionSummary]
        return self._dashboard.state.sessions

    @property
    def _snapshot(self):
        # type: () -> Optional[DashboardSnapshot]
        return self._dashboard.state.snapshot

    @property
    def _running_cpu(self):
        # type: () -> Dict[str, float]
        return self._dashboard.state.running_cpu

    @property
    def _workers_by_session(self):
        # type: () -> Dict[str, Any]
        return self._dashboard.state.workers_by_session

    @property
    def _mem_by_session(self):
        # type: () -> Dict[str, int]
        return self._dashboard.state.mem_by_session

    @property
    def _cost_by_session(self):
        # type: () -> Dict[str, float]
        return self._dashboard.state.cost_by_session

    @property
    def _total_cost(self):
        # type: () -> float
        return self._dashboard.state.total_cost

    @property
    def _current_branch(self):
        # type: () -> str
        return self._dashboard.state.current_branch

    @_current_branch.setter
    def _current_branch(self, value):
        # type: (str) -> None
        self._dashboard.state.current_branch = value

    @property
    def _projects_by_stage(self):
        # type: () -> Dict[str, List[KanbanProject]]
        return self._dashboard.state.projects_by_stage

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        with Container(id="kanban-row"):
            yield Static("", id="wheel-pane")
            with Container(id="kanban-area"):
                for stage in STAGES:
                    with Container(id="col-%s" % stage, classes="kanban-column"):
                        yield Static("", id="title-%s" % stage, classes="column-title")
                        yield KanbanList(id="list-%s" % stage)
            with Container(id="detail-panel"):
                yield Static(" %s PROJECT" % _EYE, classes="panel-title")
                yield Static("", id="detail-body")
        with Container(id="bottom-bar"):
            with Container(id="kanban-input-bar"):
                yield Static("", id="kanban-input-label")
                yield Input(id="kanban-input", placeholder="")
            yield Static("", id="footerbar")

    def on_mount(self) -> None:
        self._wal_mtime = get_wal_mtime()
        self._open_latest_log()
        self._init_branch()
        self._refresh_kanban()
        self._render_topbar()
        self._render_footerbar()
        _ = self.refresh_dashboard()
        self.set_interval(2, self._tick)
        self.set_interval(0.5, self._poll_log)
        self.set_interval(3, self._check_wal)
        self.set_interval(5, self._watchdog)
        # Focus first column after mount settles
        self.set_timer(0.05, self._init_kanban_focus)
        self._apply_compact_mode(self.size.width, self.size.height)

    def _init_kanban_focus(self) -> None:
        """Set initial focus + highlight on the first non-empty column."""
        for stage in STAGES:
            ol = self.query_one("#list-%s" % stage, KanbanList)
            if ol.option_count > 0:
                ol.focus()
                ol.highlighted = 0
                self._render_detail()
                return
        # All columns empty, focus first anyway
        self.query_one("#list-pending", KanbanList).focus()

    def on_resize(self, event) -> None:
        self._apply_compact_mode(event.size.width, event.size.height)

    def _apply_compact_mode(self, width, height):
        # type: (int, int) -> None
        try:
            panel = self.query_one("#detail-panel", Container)
        except Exception:
            return
        if height < 20 or width < 60:
            panel.add_class("hidden")
        else:
            panel.remove_class("hidden")

    # ── Live features ──────────────────────────────────────────────────

    def _open_latest_log(self) -> None:
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

    def _check_wal(self) -> None:
        new_mtime = get_wal_mtime()
        if new_mtime is None:
            return
        if self._wal_mtime is None or new_mtime > self._wal_mtime:
            self._wal_mtime = new_mtime
            _ = self.refresh_dashboard()

    def _watchdog(self) -> None:
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
                msg = "SIGKILL PID %d (%.1fGB) \u2014 %s" % (
                    proc.pid,
                    mem_gb,
                    session_label,
                )
                self._watchdog_warned.discard(proc.pid)
            else:
                try:
                    os.kill(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
                msg = "SIGTERM PID %d (%.1fGB) \u2014 %s" % (
                    proc.pid,
                    mem_gb,
                    session_label,
                )
                self._watchdog_warned.add(proc.pid)
            self._log_events.append(
                LogEvent(
                    time_str=datetime.now().strftime("%H:%M:%S"),
                    event_type="watchdog.kill",
                    fields={"detail": msg},
                )
            )
            killed_any = True
        if killed_any:
            self._error_flash_until = datetime.now() + timedelta(seconds=10)
            self._render_topbar()

    def _tick(self) -> None:
        self._tick_count += 1
        self._render_topbar()

    # ── Actions ────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._refresh_kanban()
        _ = self.refresh_dashboard()

    def action_show_sessions(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        self.push_screen(SessionsScreen(self))

    def action_open_session(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        project_path = None
        if self._snapshot:
            project_path = self._snapshot.project_path
        if not project.session_ids:
            self._seed_and_open_session(project, project_path)
        elif len(project.session_ids) == 1:
            _launch_session_interactive(project.session_ids[0], project_path)
        else:
            self.push_screen(
                SessionPickerScreen(project.session_ids, self._sessions, project_path)
            )

    @work(thread=True, exclusive=True, group="seed_session")
    def _seed_and_open_session(self, project, project_path):
        # type: (KanbanProject, Optional[str]) -> None
        result = self._dashboard.seed_session_for_project(project)
        self.call_from_thread(self._refresh_kanban)
        if result.session_id:
            _launch_session_interactive(result.session_id, project_path)
        else:
            _launch_session_interactive(None, project_path)

    def action_move_right(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        idx = STAGES.index(project.stage)
        if idx < len(STAGES) - 1:
            self._dashboard.kanban.move_project(project.id, STAGES[idx + 1])
            self._refresh_kanban()

    def action_move_left(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        idx = STAGES.index(project.stage)
        if idx > 0:
            self._dashboard.kanban.move_project(project.id, STAGES[idx - 1])
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

    def action_unlink_session(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project or not project.session_ids:
            return
        self._mode = MODE_UNLINK_SESSION
        self._show_input(
            "Unlink session ID:",
            "ses_...",
            initial_value=project.session_ids[0],
        )

    def action_link_pr(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        self._mode = MODE_LINK_PR
        self._show_input("PR number:", "#12345")

    def action_archive_project(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        self._dashboard.archive_project(project.id)
        self._refresh_kanban()

    def action_show_archive(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        self.push_screen(ArchiveScreen(self))

    def action_show_search(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        self._focus_project_after_search = None
        self.push_screen(SearchScreen(self), callback=self._on_search_dismissed)

    def action_wheel_next(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._dashboard.wheel_next()
        self._render_wheel()
        if project and project.session_ids:
            project_path = self._snapshot.project_path if self._snapshot else None
            _launch_session_interactive(project.session_ids[0], project_path)

    def action_wheel_prev(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._dashboard.wheel_prev()
        self._render_wheel()
        if project and project.session_ids:
            project_path = self._snapshot.project_path if self._snapshot else None
            _launch_session_interactive(project.session_ids[0], project_path)

    def action_wheel_add(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        self._dashboard.wheel_add(project.id)
        self._render_wheel()

    def action_wheel_remove(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        self._dashboard.wheel_remove(project.id)
        self._render_wheel()

    def _render_wheel(self) -> None:
        try:
            pane = self.query_one("#wheel-pane", Static)
        except Exception:
            return
        wheel_items = self._dashboard.wheel_list()
        if not wheel_items:
            pane.add_class("hidden")
            return
        pane.remove_class("hidden")
        current = self._dashboard.wheel_current()
        current_id = current.id if current else None
        parts = []  # type: list
        for project in wheel_items:
            title = project.title
            if len(title) > 24:
                title = title[:21] + "..."
            if project.id == current_id:
                parts.append("[bold #00ff41]\u25b8 %s \u25c2[/]" % title)
            else:
                parts.append("[dim]%s[/]" % title)
        text = " \u25c0  %s  \u25b6" % ("  %s  " % _PIPE).join(parts)
        pane.update(text)

    def _on_search_dismissed(self, _result=None) -> None:
        target_id = self._focus_project_after_search
        self._focus_project_after_search = None
        if not target_id:
            return
        project = self._dashboard.kanban.get_project(target_id)
        if not project or project.stage not in STAGES:
            return
        self._refresh_kanban()
        ol = self.query_one("#list-%s" % project.stage, KanbanList)
        ol.focus()
        items = self._projects_by_stage.get(project.stage, [])
        for i, p in enumerate(items):
            if p.id == target_id:
                ol.highlighted = i
                break

    # ── Input bar ──────────────────────────────────────────────────────

    def _show_input(self, label, placeholder="", initial_value=""):
        # type: (str, str, str) -> None
        # Remember which column had focus
        focused = self.focused
        if isinstance(focused, KanbanList):
            if focused.id:
                self._last_focused_stage = focused.id.replace("list-", "")
        bar = self.query_one("#kanban-input-bar", Container)
        bar.add_class("visible")
        lbl = self.query_one("#kanban-input-label", Static)
        lbl.update(" %s" % label)
        inp = self.query_one("#kanban-input", Input)
        inp.value = initial_value
        inp.placeholder = placeholder
        inp.focus()

    def _hide_input(self) -> None:
        bar = self.query_one("#kanban-input-bar", Container)
        bar.remove_class("visible")
        self._mode = MODE_NORMAL
        # Restore focus to the column that had it
        stage = self._last_focused_stage or "pending"
        self.query_one("#list-%s" % stage, KanbanList).focus()

    def on_input_submitted(self, event):
        # type: (Input.Submitted) -> None
        value = event.value.strip()

        if self._mode == MODE_ADD_TITLE:
            if value:
                self._pending_title = value
                self._mode = MODE_ADD_DESC
                self._show_input(
                    "Description (becomes the agent's initial prompt):",
                    "What to build, context, constraints...",
                )
            else:
                self._hide_input()
            return

        if self._mode == MODE_ADD_DESC:
            stage = self._last_focused_stage or "pending"
            project = self._dashboard.kanban.create_project(
                title=self._pending_title,
                description=value,
                stage=stage,
            )
            self._pending_title = ""
            self._hide_input()
            self._refresh_kanban()
            project_path = None
            if self._snapshot:
                project_path = self._snapshot.project_path
            self._seed_and_open_session(project, project_path)
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
                        self._dashboard.kanban.link_session(project.id, matched)
                    else:
                        self._dashboard.kanban.link_session(project.id, value)
            self._hide_input()
            self._refresh_kanban()
            return

        if self._mode == MODE_UNLINK_SESSION:
            if value:
                project = self._selected_project()
                if project:
                    matched = None
                    for sid in project.session_ids:
                        if sid == value or value in sid:
                            matched = sid
                            break
                    self._dashboard.kanban.unlink_session(project.id, matched or value)
            self._hide_input()
            self._refresh_kanban()
            return

        if self._mode == MODE_LINK_PR:
            if value:
                project = self._selected_project()
                if project:
                    try:
                        pr_num = int(value.lstrip("#"))
                        self._dashboard.kanban.link_pr(project.id, pr_num)
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

    # ── OptionList events ─────────────────────────────────────────────

    def on_option_list_option_highlighted(self, event):
        # type: (OptionList.OptionHighlighted) -> None
        self._render_detail()

    def on_option_list_option_selected(self, event):
        # type: (OptionList.OptionSelected) -> None
        self.action_open_session()

    # ── Data ───────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True)
    def refresh_dashboard(self) -> None:
        self._dashboard.refresh_snapshot()
        self.call_from_thread(self._on_snapshot_applied)

    def _on_snapshot_applied(self) -> None:
        prev = self._dashboard.state.prev_ci_fail_count
        ci_fails = self._dashboard.ci_fail_count()
        if ci_fails > prev and ci_fails > 0:
            self._error_flash_until = datetime.now() + timedelta(seconds=10)
            self._log_events.append(
                LogEvent(
                    time_str=datetime.now().strftime("%H:%M:%S"),
                    event_type="ci.failed",
                    fields={"detail": "%d PR(s) failing CI" % ci_fails},
                )
            )
        self._render_topbar()
        self._render_detail()

    # ── Kanban data ────────────────────────────────────────────────────

    def _refresh_kanban(self) -> None:
        self._dashboard.refresh_kanban()
        self._render_kanban()

    def _selected_project(self):
        # type: () -> Optional[KanbanProject]
        focused = self.focused
        if isinstance(focused, KanbanList):
            stage = focused.id.replace("list-", "") if focused.id else "pending"
        elif self._last_focused_stage:
            stage = self._last_focused_stage
        else:
            return None
        items = self._projects_by_stage.get(stage, [])
        # Get highlighted index from the correct list
        ol = self.query_one("#list-%s" % stage, KanbanList)
        idx = ol.highlighted
        if idx is not None and 0 <= idx < len(items):
            return items[idx]
        return None

    # ── Rendering ──────────────────────────────────────────────────────

    def _render_kanban(self) -> None:
        for stage in STAGES:
            self._render_kanban_column(stage)
        self._render_detail()
        self._render_wheel()

    def _render_kanban_column(self, stage):
        # type: (str) -> None
        items = self._projects_by_stage.get(stage, [])
        icon = STAGE_ICONS.get(stage, _O)
        count = len(items)

        self.query_one("#title-%s" % stage, Static).update(
            " %s %s (%d)" % (icon, STAGE_LABELS[stage], count)
        )

        ol = self.query_one("#list-%s" % stage, KanbanList)
        old_idx = ol.highlighted
        ol.clear_options()
        for project in items:
            ol.add_option(Option(self._build_card(project), id=project.id))

        # Restore highlight position
        if ol.option_count > 0:
            if old_idx is not None and old_idx < ol.option_count:
                ol.highlighted = old_idx
            else:
                ol.highlighted = max(0, ol.option_count - 1)

    def _build_card(self, project):
        # type: (KanbanProject) -> Text
        card = Text()
        # Title line
        title = project.title
        if len(title) > 30:
            title = title[:27] + "..."
        card.append(title, style="bold")
        # Description line
        if project.description:
            desc = project.description
            if len(desc) > 34:
                desc = desc[:31] + "..."
            card.append("\n")
            card.append(desc, style="dim")
        # Meta line — sessions, PRs, tags
        meta_parts = []  # type: list
        if project.session_ids:
            meta_parts.append("%s %d" % (_TERM, len(project.session_ids)))
        if project.pr_numbers:
            pr_labels = ["#%d" % n for n in project.pr_numbers[:3]]
            meta_parts.append("%s %s" % (_FORK, " ".join(pr_labels)))
        if project.tags:
            tag_labels = project.tags[:3]
            meta_parts.append("%s %s" % (_TAG, " ".join(tag_labels)))
        if meta_parts:
            card.append("\n")
            card.append("  ".join(meta_parts), style="dim italic")
        return card

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
            " q:quit %s r:refresh %s S:sessions %s A:archive %s /:search"
            " %s n/N:wheel %s w/W:wheel+/- %s tab:columns %s j/k:select"
            " %s enter:open %s m/M:move %s a:add %s d:archive %s s:link"
            % (
                _PIPE,
                _PIPE,
                _PIPE,
                _PIPE,
                _PIPE,
                _PIPE,
                _PIPE,
                _PIPE,
                _PIPE,
                _PIPE,
                _PIPE,
                _PIPE,
                _PIPE,
            )
        )

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

        if project.tags:
            lines.append(
                " %s %s" % (_TAG, "  ".join("[dim]%s[/]" % t for t in project.tags))
            )

        detail.update("\n".join(lines))

    # ── Helpers ────────────────────────────────────────────────────────

    def _init_branch(self) -> None:
        self._dashboard.init_branch()


def main() -> None:
    app = OCDashboardApp()
    app.run()
