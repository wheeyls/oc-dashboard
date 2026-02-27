"""Kanban board screen for oc-dashboard.

A full-screen Textual screen showing projects in a 4-column Kanban layout:
  Pending | In Progress | In PR | Done

Keybindings:
  h/l or left/right  - move between columns
  j/k or up/down     - move between projects within a column
  m                   - move project to next stage (right)
  M                   - move project to previous stage (left)
  a                   - add new project
  s                   - link current session to selected project
  p                   - link a PR to selected project
  d                   - delete project
  enter               - show project detail / jump into sessions
  escape / b          - back to main dashboard
"""

import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from .kanban import (
    STAGES,
    STAGE_LABELS,
    KanbanAdapter,
    KanbanProject,
    LocalJsonKanban,
)

# ── Nerd Font icons ──────────────────────────────────────
_CHECK = chr(0xF00C)
_TIMES = chr(0xF00D)
_COG = chr(0xF013)
_LIST = chr(0xF03A)
_PLUS = chr(0xF067)
_WARN = chr(0xF071)
_SQ_O = chr(0xF096)
_O = chr(0xF10C)
_SPIN = chr(0xF110)
_CIRCLE = chr(0xF111)
_TERM = chr(0xF120)
_FORK = chr(0xF126)
_ROCKET = chr(0xF135)
_BOLT = chr(0xF0E7)
_PIPE = chr(0x2502)
_HBAR = chr(0x2500)
_FOLDER = chr(0xF07B)
_ARROW_R = chr(0xF061)
_ARROW_L = chr(0xF060)
_TAG = chr(0xF02B)
_CLIPBOARD = chr(0xF0EA)
_PLAY = chr(0xF04B)
_KEYBOARD = chr(0xF11C)
_CHECK_CIRCLE = chr(0xF058)

STAGE_ICONS = {
    "pending": _SQ_O,
    "in_progress": _SPIN,
    "pr": _FORK,
    "done": _CHECK_CIRCLE,
}

# Mode for input prompts
MODE_NORMAL = "normal"
MODE_ADD_TITLE = "add_title"
MODE_ADD_DESC = "add_desc"
MODE_LINK_SESSION = "link_session"
MODE_LINK_PR = "link_pr"


class KanbanScreen(Screen):
    CSS = """
    KanbanScreen {
        background: #000000;
    }

    #kanban-topbar {
        height: 3;
        padding: 1 2;
        background: #010409;
        color: #00ff41;
        text-style: bold;
        border-bottom: heavy #0a3d1a;
    }

    #kanban-main {
        layout: horizontal;
        height: 1fr;
    }

    .kanban-column {
        width: 1fr;
        border: heavy #0a3d1a;
        padding: 0 1;
    }

    .kanban-column.active-column {
        border: heavy #00ff41;
    }

    .column-title {
        height: 1;
        color: #00d4ff;
        text-style: bold;
    }

    .column-body {
        height: 1fr;
        color: #a8b1ba;
        padding: 0 0 1 0;
    }

    #kanban-detail {
        height: 10;
        border-top: heavy #0a3d1a;
        padding: 0 2;
        color: #7a8490;
    }

    #kanban-footer {
        height: 1;
        padding: 0 1;
        background: #010409;
        color: #3d444d;
        border-top: heavy #0a3d1a;
    }

    #kanban-input-bar {
        height: 3;
        padding: 0 1;
        background: #001a00;
        border-top: heavy #0a3d1a;
        display: none;
    }

    #kanban-input-bar.visible {
        display: block;
    }

    #kanban-input-bar Input {
        background: #000000;
        color: #00ff41;
        border: heavy #0a3d1a;
    }

    #kanban-input-bar Input:focus {
        border: heavy #00ff41;
    }

    #kanban-input-label {
        height: 1;
        color: #00d4ff;
    }
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("b", "back", "Back", show=False),
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
        Binding("enter", "open_sessions", "Open sessions"),
    ]

    def __init__(self, adapter=None, sessions=None, prs=None, project_path=None):
        # type: (Optional[KanbanAdapter], Optional[list], Optional[list], Optional[str]) -> None
        super().__init__()
        self._adapter = adapter or LocalJsonKanban()
        self._sessions = sessions or []
        self._prs = prs or []
        self._project_path = project_path
        self._col_idx = 0  # which STAGES column is active
        self._row_idx = {}  # type: Dict[str, int]  # stage -> selected row
        for s in STAGES:
            self._row_idx[s] = 0
        self._projects_by_stage = {}  # type: Dict[str, List[KanbanProject]]
        self._mode = MODE_NORMAL
        self._pending_title = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="kanban-topbar")
        with Horizontal(id="kanban-main"):
            for stage in STAGES:
                with Container(id="col-%s" % stage, classes="kanban-column"):
                    yield Static("", id="title-%s" % stage, classes="column-title")
                    yield Static("", id="body-%s" % stage, classes="column-body")
        yield Static("", id="kanban-detail")
        with Container(id="kanban-input-bar"):
            yield Static("", id="kanban-input-label")
            yield Input(id="kanban-input", placeholder="")
        yield Static("", id="kanban-footer")

    def on_mount(self) -> None:
        self._refresh_board()
        self._render_footer()

    # ── data ──────────────────────────────────────────────

    def _refresh_board(self) -> None:
        projects = self._adapter.list_projects()
        self._projects_by_stage = {}
        for s in STAGES:
            self._projects_by_stage[s] = []
        for p in projects:
            stage = p.stage if p.stage in STAGES else "pending"
            self._projects_by_stage[stage].append(p)
        # Sort each column: most recently updated first
        for s in STAGES:
            self._projects_by_stage[s].sort(key=lambda p: p.updated_at, reverse=True)
        # Clamp row indices
        for s in STAGES:
            count = len(self._projects_by_stage[s])
            if count == 0:
                self._row_idx[s] = 0
            elif self._row_idx[s] >= count:
                self._row_idx[s] = count - 1
        self._render_all()

    def _selected_project(self):
        # type: () -> Optional[KanbanProject]
        stage = STAGES[self._col_idx]
        items = self._projects_by_stage.get(stage, [])
        idx = self._row_idx.get(stage, 0)
        if 0 <= idx < len(items):
            return items[idx]
        return None

    # ── rendering ─────────────────────────────────────────

    def _render_all(self) -> None:
        self._render_topbar()
        for stage in STAGES:
            self._render_column(stage)
        self._render_detail()
        self._highlight_active_column()

    def _render_topbar(self) -> None:
        total = sum(len(v) for v in self._projects_by_stage.values())
        active = len(self._projects_by_stage.get("in_progress", []))
        in_pr = len(self._projects_by_stage.get("pr", []))
        topbar = self.query_one("#kanban-topbar", Static)
        topbar.update(
            " %s %s KANBAN BOARD  %s  %d projects  %s  %d active  %s  %d in PR"
            % (_CLIPBOARD, _TERM, _PIPE, total, _PIPE, active, _PIPE, in_pr)
        )

    def _render_column(self, stage):
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
                # Show session/PR counts inline
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

    def _render_detail(self) -> None:
        detail = self.query_one("#kanban-detail", Static)
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
            lines.append(" [dim]%s[/]" % project.description[:80])

        # Sessions
        if project.session_ids:
            sess_parts = []
            for sid in project.session_ids[:5]:
                # Try to find title from dashboard sessions
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
            for num in project.pr_numbers[:5]:
                # Try to find PR status from dashboard PRs
                status = ""
                for pr in self._prs:
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

    def _highlight_active_column(self) -> None:
        for i, stage in enumerate(STAGES):
            col = self.query_one("#col-%s" % stage, Container)
            if i == self._col_idx:
                col.add_class("active-column")
            else:
                col.remove_class("active-column")

    def _render_footer(self) -> None:
        footer = self.query_one("#kanban-footer", Static)
        footer.update(
            " esc:back %s h/l:columns %s j/k:projects %s m:move %s a:add %s s:link-session %s p:link-pr %s d:delete %s enter:open"
            % (_PIPE, _PIPE, _PIPE, _PIPE, _PIPE, _PIPE, _PIPE, _PIPE)
        )

    # ── input bar ─────────────────────────────────────────

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
            self._adapter.create_project(
                title=self._pending_title,
                description=value,
                stage=STAGES[self._col_idx],
            )
            self._pending_title = ""
            self._hide_input()
            self._refresh_board()
            return

        if self._mode == MODE_LINK_SESSION:
            if value:
                project = self._selected_project()
                if project:
                    # Accept full session id or partial match
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
                        self._adapter.link_session(project.id, matched)
                    else:
                        # Use raw value as session id
                        self._adapter.link_session(project.id, value)
            self._hide_input()
            self._refresh_board()
            return

        if self._mode == MODE_LINK_PR:
            if value:
                project = self._selected_project()
                if project:
                    try:
                        pr_num = int(value.lstrip("#"))
                        self._adapter.link_pr(project.id, pr_num)
                    except ValueError:
                        pass
            self._hide_input()
            self._refresh_board()
            return

        self._hide_input()

    def on_key(self, event) -> None:
        # If input is focused, let it handle keys except escape
        if self._mode != MODE_NORMAL:
            if event.key == "escape":
                self._hide_input()
                event.prevent_default()
            return

    # ── actions ───────────────────────────────────────────

    def action_back(self) -> None:
        if self._mode != MODE_NORMAL:
            self._hide_input()
            return
        self.app.pop_screen()

    def action_col_left(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        if self._col_idx > 0:
            self._col_idx -= 1
            self._render_all()

    def action_col_right(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        if self._col_idx < len(STAGES) - 1:
            self._col_idx += 1
            self._render_all()

    def action_item_down(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        stage = STAGES[self._col_idx]
        count = len(self._projects_by_stage.get(stage, []))
        if count > 0 and self._row_idx[stage] < count - 1:
            self._row_idx[stage] += 1
            self._render_all()

    def action_item_up(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        stage = STAGES[self._col_idx]
        if self._row_idx[stage] > 0:
            self._row_idx[stage] -= 1
            self._render_all()

    def action_move_right(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        idx = STAGES.index(project.stage)
        if idx < len(STAGES) - 1:
            self._adapter.move_project(project.id, STAGES[idx + 1])
            self._refresh_board()

    def action_move_left(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project:
            return
        idx = STAGES.index(project.stage)
        if idx > 0:
            self._adapter.move_project(project.id, STAGES[idx - 1])
            self._refresh_board()

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
        self._adapter.delete_project(project.id)
        self._refresh_board()

    def action_open_sessions(self) -> None:
        if self._mode != MODE_NORMAL:
            return
        project = self._selected_project()
        if not project or not project.session_ids:
            return
        # Open first linked session in tmux
        session_id = project.session_ids[0]
        _launch_session_in_tmux(session_id, self._project_path)


def _in_tmux():
    # type: () -> bool
    return bool(__import__("os").environ.get("TMUX"))


def _launch_session_in_tmux(session_id, project_path=None):
    # type: (str, Optional[str]) -> None
    import os

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
