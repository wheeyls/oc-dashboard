import os
import signal
import subprocess
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import DataTable, Input, OptionList, Static
from textual.widgets.option_list import Option

from .data import BackgroundWorker
from .data import DashboardSnapshot
from .data import SessionSummary
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


def _in_tmux():
    # type: () -> bool
    return bool(os.environ.get("TMUX"))


def _launch_session_in_tmux(session_id, project_path=None):
    # type: (str, ...) -> None
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
        a = self._app_ref
        table = self.query_one("#sessions-body", DataTable)
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(a._sessions):
            return
        session = a._sessions[idx]
        if session.is_group_header:
            return
        project_path = session.directory or (
            a._snapshot.project_path if a._snapshot else None
        )
        _launch_session_in_tmux(session.id, project_path)

    def on_data_table_row_selected(self, event_obj):
        # type: (DataTable.RowSelected) -> None
        self.action_open_session()



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
        self.query_one("#picker-topbar", Static).update(
            " %s PICK SESSION" % _TERM
        )
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
        _launch_session_in_tmux(session_id, self._project_path)
        self.app.pop_screen()

    def on_option_list_option_selected(self, event):
        # type: (OptionList.OptionSelected) -> None
        self.action_pick_session()
# ══════════════════════════════════════════════════════════
#  Main App — Kanban board fills the screen
# ══════════════════════════════════════════════════════════


class OCDashboardApp(App[None]):
    CSS_PATH = "app.tcss"
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
        Binding("d", "delete_project", "Delete"),
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
        self._tick_count = 0
        self._total_cost = 0.0
        # Live features
        self._wal_mtime = None  # type: Optional[float]
        self._log_path = None  # type: Optional[str]
        self._log_handle = None  # type: Optional[Any]
        self._log_events = deque(maxlen=COMMS_MAX_EVENTS)  # type: deque
        self._current_branch = ""
        self._error_flash_until = None  # type: Optional[datetime]
        self._watchdog_warned = set()  # type: set
        self._prev_ci_fail_count = 0
        # Kanban
        self._kanban = LocalJsonKanban()
        self._projects_by_stage = {}  # type: Dict[str, List[KanbanProject]]
        self._mode = MODE_NORMAL
        self._pending_title = ""
        self._last_focused_stage = "pending"

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        with Container(id="kanban-row"):
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
        if not project or not project.session_ids:
            return
        project_path = None
        if self._snapshot:
            project_path = self._snapshot.project_path
        if len(project.session_ids) == 1:
            _launch_session_in_tmux(project.session_ids[0], project_path)
        else:
            self.push_screen(
                SessionPickerScreen(
                    project.session_ids, self._sessions, project_path
                )
            )
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

    def action_delete_project(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        self._kanban.delete_project(project.id)
        self._refresh_kanban()

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
                    "Description (optional, enter to skip):", "Brief description..."
                )
            else:
                self._hide_input()
            return

        if self._mode == MODE_ADD_DESC:
            stage = self._last_focused_stage or "pending"
            self._kanban.create_project(
                title=self._pending_title,
                description=value,
                stage=stage,
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

        if self._mode == MODE_UNLINK_SESSION:
            if value:
                project = self._selected_project()
                if project:
                    matched = None
                    for sid in project.session_ids:
                        if sid == value or value in sid:
                            matched = sid
                            break
                    self._kanban.unlink_session(project.id, matched or value)
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
        snapshot = build_snapshot(limit=30)
        self.call_from_thread(self._apply_snapshot, snapshot)

    def _apply_snapshot(self, snapshot):
        # type: (DashboardSnapshot) -> None
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
            title = project.title
            if len(title) > 28:
                title = title[:25] + "..."
            meta_parts = []
            if project.session_ids:
                meta_parts.append("%d sess" % len(project.session_ids))
            if project.pr_numbers:
                meta_parts.append("%d PR" % len(project.pr_numbers))
            meta = " (%s)" % ", ".join(meta_parts) if meta_parts else ""
            ol.add_option(Option("%s%s" % (title, meta), id=project.id))

        # Restore highlight position
        if ol.option_count > 0:
            if old_idx is not None and old_idx < ol.option_count:
                ol.highlighted = old_idx
            else:
                ol.highlighted = max(0, ol.option_count - 1)

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
            " q:quit %s r:refresh %s S:sessions %s tab:columns %s j/k:select"
            " %s enter:open %s m/M:move %s a:add %s s:link %s u:unlink"
            % (_PIPE, _PIPE, _PIPE, _PIPE, _PIPE, _PIPE, _PIPE, _PIPE, _PIPE)
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
