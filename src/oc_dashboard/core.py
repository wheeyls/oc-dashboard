import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .data import (
    DashboardSnapshot,
    SessionSummary,
    TodoItem,
    BackgroundWorker,
    build_snapshot,
    fetch_running_processes,
    session_status,
    CI_FAIL,
)
from .kanban import (
    ALL_STAGES,
    STAGES,
    STAGE_LABELS,
    KanbanAdapter,
    KanbanProject,
)
from .opencode import OpenCodeClient, opencode_env_prefix


@dataclass
class DashboardState:
    snapshot: Optional[DashboardSnapshot] = None
    sessions: List[SessionSummary] = field(default_factory=list)
    todos_by_session: Dict[str, List[TodoItem]] = field(default_factory=dict)
    workers_by_session: Dict[str, List[BackgroundWorker]] = field(default_factory=dict)
    running_cpu: Dict[str, float] = field(default_factory=dict)
    cost_by_session: Dict[str, float] = field(default_factory=dict)
    mem_by_session: Dict[str, int] = field(default_factory=dict)
    unattributed_cpu: float = 0.0
    total_cost: float = 0.0
    current_branch: str = ""
    projects_by_stage: Dict[str, List[KanbanProject]] = field(default_factory=dict)
    prev_ci_fail_count: int = 0
    project_path: Optional[str] = None


@dataclass
class SessionSeedResult:
    session_id: Optional[str] = None
    linked: bool = False
    error: Optional[str] = None


@dataclass
class SearchResult:
    """A single search hit — either a kanban project or a session."""

    kind: str  # "project" or "session"
    id: str
    title: str
    detail: str  # match context line
    stage: str = ""  # for projects: pending/in_progress/done/archived
    session_ids: List[str] = field(default_factory=list)
    pr_numbers: List[int] = field(default_factory=list)


class Dashboard:
    def __init__(self, kanban, opencode_client):
        # type: (KanbanAdapter, OpenCodeClient) -> None
        self._kanban = kanban
        self._oc = opencode_client
        self.state = DashboardState()
        for stage in STAGES:
            self.state.projects_by_stage[stage] = []

    @property
    def kanban(self):
        # type: () -> KanbanAdapter
        return self._kanban

    @property
    def opencode(self):
        # type: () -> OpenCodeClient
        return self._oc

    def refresh_snapshot(self):
        # type: () -> DashboardSnapshot
        snapshot = build_snapshot(limit=30)
        self._apply_snapshot(snapshot)
        return snapshot

    def _apply_snapshot(self, snapshot):
        # type: (DashboardSnapshot) -> None
        s = self.state
        s.snapshot = snapshot
        s.sessions = snapshot.sessions
        s.todos_by_session = snapshot.todos_by_session
        s.workers_by_session = snapshot.workers_by_session
        s.project_path = snapshot.project_path

        s.running_cpu = {}
        s.unattributed_cpu = 0.0
        s.mem_by_session = {}
        for proc in snapshot.running_processes:
            if proc.session_id:
                s.running_cpu[proc.session_id] = max(
                    s.running_cpu.get(proc.session_id, 0.0), proc.cpu_percent
                )
                s.mem_by_session[proc.session_id] = (
                    s.mem_by_session.get(proc.session_id, 0) + proc.mem_mb
                )
            else:
                s.unattributed_cpu += proc.cpu_percent

        s.cost_by_session = {}
        s.total_cost = 0.0
        for cost in snapshot.session_costs:
            s.cost_by_session[cost.session_id] = cost.total_cost
            s.total_cost += cost.total_cost

        s.prev_ci_fail_count = sum(1 for p in snapshot.prs if p.ci_status == CI_FAIL)

    def refresh_kanban(self):
        # type: () -> Dict[str, List[KanbanProject]]
        projects = self._kanban.list_projects()
        by_stage = {}  # type: Dict[str, List[KanbanProject]]
        for stage in STAGES:
            by_stage[stage] = []
        for p in projects:
            if p.stage not in STAGES:
                continue
            by_stage[p.stage].append(p)
        for stage in STAGES:
            by_stage[stage].sort(key=lambda proj: proj.updated_at, reverse=True)
        self.state.projects_by_stage = by_stage
        return by_stage

    # ── Session lifecycle ─────────────────────────────────

    def create_project_and_seed(self, title, description="", stage="pending"):
        # type: (str, str, str) -> SessionSeedResult
        project = self._kanban.create_project(
            title=title, description=description, stage=stage
        )
        return self.seed_session_for_project(project)

    def seed_session_for_project(self, project):
        # type: (KanbanProject) -> SessionSeedResult
        session = self._oc.create_session(title=project.title)
        if not session:
            return SessionSeedResult(error="Failed to create session")

        prompt = project.title
        if project.description:
            prompt = "%s\n\n%s" % (project.title, project.description)
        self._oc.send_message_async(session.id, prompt)
        self._kanban.link_session(project.id, session.id)
        return SessionSeedResult(session_id=session.id, linked=True)

    def open_session_interactive(self, session_id, project_path=None):
        # type: (str, Optional[str]) -> None
        _launch_session_interactive(session_id, project_path)

    def open_session_for_project(self, project):
        # type: (KanbanProject) -> Optional[SessionSeedResult]
        project_path = self.state.project_path
        if not project.session_ids:
            result = self.seed_session_for_project(project)
            if result.session_id:
                _launch_session_interactive(result.session_id, project_path)
            return result
        elif len(project.session_ids) == 1:
            _launch_session_interactive(project.session_ids[0], project_path)
            return None
        else:
            return None

    # ── Kanban operations (delegated) ─────────────────────

    def move_project(self, project_id, direction):
        # type: (str, str) -> bool
        project = self._kanban.get_project(project_id)
        if not project:
            return False
        idx = STAGES.index(project.stage) if project.stage in STAGES else 0
        if direction == "right" and idx < len(STAGES) - 1:
            self._kanban.move_project(project_id, STAGES[idx + 1])
            return True
        if direction == "left" and idx > 0:
            self._kanban.move_project(project_id, STAGES[idx - 1])
            return True
        return False

    def link_session(self, project_id, session_id_or_query):
        # type: (str, str) -> bool
        matched = None
        for s in self.state.sessions:
            if (
                s.id == session_id_or_query
                or session_id_or_query in s.id
                or session_id_or_query.lower() in s.title.lower()
            ):
                matched = s.id
                break
        self._kanban.link_session(project_id, matched or session_id_or_query)
        return True

    def unlink_session(self, project_id, session_id_or_query):
        # type: (str, str) -> bool
        project = self._kanban.get_project(project_id)
        if not project:
            return False
        matched = None
        for sid in project.session_ids:
            if sid == session_id_or_query or session_id_or_query in sid:
                matched = sid
                break
        self._kanban.unlink_session(project_id, matched or session_id_or_query)
        return True

    def link_pr(self, project_id, value):
        # type: (str, str) -> bool
        try:
            pr_num = int(value.lstrip("#"))
            self._kanban.link_pr(project_id, pr_num)
            return True
        except ValueError:
            return False

    def delete_project(self, project_id):
        # type: (str) -> bool
        return self._kanban.delete_project(project_id)

    def archive_project(self, project_id):
        # type: (str) -> bool
        project = self._kanban.get_project(project_id)
        if not project:
            return False
        self._kanban.update_project(project_id, previous_stage=project.stage)
        result = self._kanban.move_project(project_id, "archived")
        return result is not None

    def restore_project(self, project_id, stage=None):
        # type: (str, Optional[str]) -> bool
        project = self._kanban.get_project(project_id)
        if not project:
            return False
        target = stage or project.previous_stage or "done"
        if target not in STAGES:
            target = "done"
        self._kanban.update_project(project_id, previous_stage=None)
        result = self._kanban.move_project(project_id, target)
        return result is not None

    def list_archived(self):
        # type: () -> List[KanbanProject]
        projects = self._kanban.list_projects()
        archived = [p for p in projects if p.stage == "archived"]
        archived.sort(key=lambda p: p.updated_at, reverse=True)
        return archived

    # ── Wheel operations ────────────────────────────────

    def wheel_list(self):
        # type: () -> List[KanbanProject]
        ids = self._kanban.wheel_list()
        result = []  # type: List[KanbanProject]
        for pid in ids:
            p = self._kanban.get_project(pid)
            if p:
                result.append(p)
        return result

    def wheel_add(self, project_id):
        # type: (str) -> bool
        return self._kanban.wheel_add(project_id)

    def wheel_remove(self, project_id):
        # type: (str) -> bool
        return self._kanban.wheel_remove(project_id)

    def wheel_next(self):
        # type: () -> Optional[KanbanProject]
        pid = self._kanban.wheel_next()
        if not pid:
            return None
        return self._kanban.get_project(pid)

    def wheel_prev(self):
        # type: () -> Optional[KanbanProject]
        pid = self._kanban.wheel_prev()
        if not pid:
            return None
        return self._kanban.get_project(pid)

    def wheel_current(self):
        # type: () -> Optional[KanbanProject]
        pid = self._kanban.wheel_current()
        if not pid:
            return None
        return self._kanban.get_project(pid)

    # ── Search ────────────────────────────────────────────

    def search(self, query):
        # type: (str) -> List[SearchResult]
        if not query or not query.strip():
            return []
        q = query.strip().lower()
        results = []  # type: List[SearchResult]
        seen_session_ids = set()  # type: set

        for project in self._kanban.list_projects():
            matched = self._match_project(project, q)
            if matched:
                results.append(matched)
                seen_session_ids.update(project.session_ids)

        for session in self.state.sessions:
            if session.is_group_header:
                continue
            if session.id in seen_session_ids:
                continue
            if q in session.title.lower() or q in session.id.lower():
                results.append(
                    SearchResult(
                        kind="session",
                        id=session.id,
                        title=session.title,
                        detail=session.id[:16],
                    )
                )

        return results

    def _match_project(self, project, q):
        # type: (KanbanProject, str) -> Optional[SearchResult]
        detail = ""
        if q in project.title.lower():
            detail = STAGE_LABELS.get(project.stage, project.stage)
        elif q in project.description.lower():
            detail = self._excerpt(project.description, q)
        elif any(q in sid.lower() for sid in project.session_ids):
            matched_sid = next(s for s in project.session_ids if q in s.lower())
            detail = "session: %s" % matched_sid[:20]
        elif any(q in str(pr) for pr in project.pr_numbers):
            detail = "PR #%s" % next(
                str(pr) for pr in project.pr_numbers if q in str(pr)
            )
        elif any(q in t.lower() for t in project.tags):
            detail = "tag: %s" % next(t for t in project.tags if q in t.lower())
        else:
            return None

        return SearchResult(
            kind="project",
            id=project.id,
            title=project.title,
            detail=detail,
            stage=project.stage,
            session_ids=list(project.session_ids),
            pr_numbers=list(project.pr_numbers),
        )

    @staticmethod
    def _excerpt(text, q, width=50):
        # type: (str, str, int) -> str
        lower = text.lower()
        idx = lower.find(q)
        if idx < 0:
            return text[:width]
        start = max(0, idx - width // 4)
        end = min(len(text), idx + len(q) + width)
        snippet = text[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
        return snippet

    # ── Computed values ───────────────────────────────────

    def session_status(self, session):
        # type: (SessionSummary) -> str
        cpu = self.state.running_cpu.get(session.id)
        return session_status(session, cpu)

    def live_session_count(self):
        # type: () -> int
        return sum(1 for s in self.state.sessions if s.id in self.state.running_cpu)

    def stalled_session_count(self):
        # type: () -> int
        return sum(
            1
            for s in self.state.sessions
            if (s.pending > 0 or s.in_progress > 0)
            and s.id not in self.state.running_cpu
        )

    def total_cpu(self):
        # type: () -> float
        if not self.state.snapshot:
            return 0.0
        return sum(p.cpu_percent for p in self.state.snapshot.running_processes)

    def ci_fail_count(self):
        # type: () -> int
        if not self.state.snapshot:
            return 0
        return sum(1 for p in self.state.snapshot.prs if p.ci_status == CI_FAIL)

    def init_branch(self):
        # type: () -> None
        if self.state.current_branch:
            return
        project_path = self.state.project_path
        if not project_path:
            from .data import discover_project_path

            project_path = discover_project_path()
        if not project_path:
            return
        try:
            result = subprocess.run(
                ["git", "-C", project_path, "rev-parse", "--abbrev-ref", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                branch = result.stdout.strip()
                if branch and branch != "HEAD":
                    self.state.current_branch = branch
        except Exception:
            pass

    # ── Watchdog ──────────────────────────────────────────

    def check_memory_watchdog(self, threshold_mb=8192):
        # type: (int) -> List[Dict[str, Any]]
        killed = []
        try:
            processes = fetch_running_processes()
        except Exception:
            return killed
        for proc in processes:
            mem_gb = proc.mem_mb / 1024.0
            if proc.mem_mb <= threshold_mb:
                continue
            try:
                import signal

                os.kill(proc.pid, signal.SIGTERM)
                killed.append(
                    {
                        "pid": proc.pid,
                        "mem_gb": mem_gb,
                        "session_id": proc.session_id,
                    }
                )
            except OSError:
                pass
        return killed


def _in_tmux():
    # type: () -> bool
    return bool(os.environ.get("TMUX"))


def _launch_session_interactive(session_id=None, project_path=None):
    # type: (Optional[str], Optional[str]) -> None
    env_prefix = opencode_env_prefix()
    oc_cmd = (
        "%sopencode -s %s" % (env_prefix, session_id)
        if session_id
        else "%sopencode" % env_prefix
    )
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
