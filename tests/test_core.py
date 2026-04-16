from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from oc_dashboard.core import Dashboard, DashboardState, SearchResult, SessionSeedResult
from oc_dashboard.data import (
    CI_FAIL,
    DashboardSnapshot,
    PullRequestSummary,
    RunningProcess,
    SessionCost,
    SessionSummary,
)
from oc_dashboard.kanban import ALL_STAGES, STAGES, KanbanAdapter, KanbanProject
from oc_dashboard.opencode import OpenCodeClient, SessionInfo


# ── Fake adapters ─────────────────────────────────────────


class FakeKanban(KanbanAdapter):
    def __init__(self):
        self._projects = {}  # type: Dict[str, KanbanProject]
        self._calls = []  # type: List[tuple]
        self._wheel_ids = []  # type: List[str]
        self._wheel_cursor = 0

    def list_projects(self):
        self._calls.append(("list_projects",))
        return list(self._projects.values())

    def get_project(self, project_id):
        self._calls.append(("get_project", project_id))
        return self._projects.get(project_id)

    def create_project(self, title, description="", stage="pending", tags=None):
        pid = "proj_%d" % (len(self._projects) + 1)
        now = datetime.now().isoformat()
        p = KanbanProject(
            id=pid,
            title=title,
            description=description,
            stage=stage,
            created_at=now,
            updated_at=now,
            tags=tags or [],
        )
        self._projects[pid] = p
        self._calls.append(("create_project", title, stage))
        return p

    def update_project(self, project_id, **kwargs):
        p = self._projects.get(project_id)
        if not p:
            return None
        for k, v in kwargs.items():
            setattr(p, k, v)
        self._calls.append(("update_project", project_id, kwargs))
        return p

    def move_project(self, project_id, stage):
        p = self._projects.get(project_id)
        if not p:
            return None
        p.stage = stage
        self._calls.append(("move_project", project_id, stage))
        return p

    def delete_project(self, project_id):
        if project_id in self._projects:
            del self._projects[project_id]
            self._calls.append(("delete_project", project_id))
            return True
        return False

    def link_session(self, project_id, session_id):
        p = self._projects.get(project_id)
        if not p:
            return False
        p.session_ids.append(session_id)
        self._calls.append(("link_session", project_id, session_id))
        return True

    def unlink_session(self, project_id, session_id):
        p = self._projects.get(project_id)
        if not p:
            return False
        if session_id in p.session_ids:
            p.session_ids.remove(session_id)
        self._calls.append(("unlink_session", project_id, session_id))
        return True

    def link_pr(self, project_id, pr_number):
        p = self._projects.get(project_id)
        if not p:
            return False
        p.pr_numbers.append(pr_number)
        self._calls.append(("link_pr", project_id, pr_number))
        return True

    def unlink_pr(self, project_id, pr_number):
        p = self._projects.get(project_id)
        if not p:
            return False
        if pr_number in p.pr_numbers:
            p.pr_numbers.remove(pr_number)
        self._calls.append(("unlink_pr", project_id, pr_number))
        return True

    def wheel_list(self):
        return list(self._wheel_ids)

    def wheel_add(self, project_id):
        if project_id not in self._projects:
            return False
        if project_id in self._wheel_ids:
            return False
        self._wheel_ids.append(project_id)
        return True

    def wheel_remove(self, project_id):
        if project_id not in self._wheel_ids:
            return False
        idx = self._wheel_ids.index(project_id)
        self._wheel_ids.remove(project_id)
        if not self._wheel_ids:
            self._wheel_cursor = 0
        elif idx < self._wheel_cursor:
            self._wheel_cursor -= 1
        elif self._wheel_cursor >= len(self._wheel_ids):
            self._wheel_cursor = 0
        return True

    def wheel_next(self):
        if not self._wheel_ids:
            return None
        self._wheel_cursor = (self._wheel_cursor + 1) % len(self._wheel_ids)
        return self._wheel_ids[self._wheel_cursor]

    def wheel_prev(self):
        if not self._wheel_ids:
            return None
        self._wheel_cursor = (self._wheel_cursor - 1) % len(self._wheel_ids)
        return self._wheel_ids[self._wheel_cursor]

    def wheel_current(self):
        if not self._wheel_ids:
            return None
        if self._wheel_cursor >= len(self._wheel_ids):
            self._wheel_cursor = 0
        return self._wheel_ids[self._wheel_cursor]


class FakeOpenCode(OpenCodeClient):
    def __init__(self):
        self._sessions = {}  # type: Dict[str, SessionInfo]
        self._messages = []  # type: List[tuple]
        self._healthy = True
        self._next_session_id = 1

    def healthy(self):
        return self._healthy

    def list_sessions(self, limit=30):
        return list(self._sessions.values())[:limit]

    def create_session(self, title=None):
        sid = "ses_%d" % self._next_session_id
        self._next_session_id += 1
        info = SessionInfo(id=sid, title=title or "Untitled")
        self._sessions[sid] = info
        return info

    def get_session_todos(self, session_id):
        return []

    def get_session_children(self, session_id):
        return []

    def send_message_async(self, session_id, text):
        self._messages.append((session_id, text))
        return True

    def select_session_in_tui(self, session_id):
        return True

    def get_project_path(self):
        return None


# ── Helpers ───────────────────────────────────────────────


def _make_session(sid, title="Test", pending=0, in_progress=0, completed=0):
    return SessionSummary(
        id=sid,
        title=title,
        updated=datetime.now(),
        pending=pending,
        in_progress=in_progress,
        completed=completed,
        cancelled=0,
        parent_id=None,
        depth=0,
    )


def _make_snapshot(sessions=None, processes=None, costs=None, prs=None):
    return DashboardSnapshot(
        sessions=sessions or [],
        todos_by_session={},
        running_processes=processes or [],
        worktrees=[],
        workers_by_session={},
        prs=prs or [],
        recommendations=[],
        session_costs=costs or [],
        daily_spend=[],
        project_path="/tmp/fake",
        errors=[],
    )


def _make_dashboard():
    kanban = FakeKanban()
    oc = FakeOpenCode()
    return Dashboard(kanban, oc), kanban, oc


# ── Tests: Dashboard initialization ──────────────────────


class TestDashboardInit:
    def test_initial_state_has_all_stages(self):
        dash, _, _ = _make_dashboard()
        for stage in STAGES:
            assert stage in dash.state.projects_by_stage

    def test_properties_expose_adapters(self):
        dash, kanban, oc = _make_dashboard()
        assert dash.kanban is kanban
        assert dash.opencode is oc


# ── Tests: Snapshot processing ────────────────────────────


class TestApplySnapshot:
    def test_sessions_propagated(self):
        dash, _, _ = _make_dashboard()
        s1 = _make_session("s1", "Alpha")
        snapshot = _make_snapshot(sessions=[s1])
        dash._apply_snapshot(snapshot)
        assert len(dash.state.sessions) == 1
        assert dash.state.sessions[0].title == "Alpha"

    def test_cpu_aggregation_takes_max(self):
        dash, _, _ = _make_dashboard()
        procs = [
            RunningProcess(
                pid=1, cpu_percent=30.0, mem_mb=100, terminal="", session_id="s1"
            ),
            RunningProcess(
                pid=2, cpu_percent=50.0, mem_mb=200, terminal="", session_id="s1"
            ),
        ]
        snapshot = _make_snapshot(processes=procs)
        dash._apply_snapshot(snapshot)
        assert dash.state.running_cpu["s1"] == 50.0

    def test_mem_aggregation_sums(self):
        dash, _, _ = _make_dashboard()
        procs = [
            RunningProcess(
                pid=1, cpu_percent=10.0, mem_mb=100, terminal="", session_id="s1"
            ),
            RunningProcess(
                pid=2, cpu_percent=20.0, mem_mb=300, terminal="", session_id="s1"
            ),
        ]
        snapshot = _make_snapshot(processes=procs)
        dash._apply_snapshot(snapshot)
        assert dash.state.mem_by_session["s1"] == 400

    def test_unattributed_cpu(self):
        dash, _, _ = _make_dashboard()
        procs = [
            RunningProcess(
                pid=1, cpu_percent=25.0, mem_mb=100, terminal="", session_id=None
            ),
            RunningProcess(
                pid=2, cpu_percent=15.0, mem_mb=50, terminal="", session_id=None
            ),
        ]
        snapshot = _make_snapshot(processes=procs)
        dash._apply_snapshot(snapshot)
        assert dash.state.unattributed_cpu == 40.0

    def test_cost_aggregation(self):
        dash, _, _ = _make_dashboard()
        costs = [
            SessionCost(session_id="s1", title="A", direct_cost=5.0, child_cost=2.0),
            SessionCost(session_id="s2", title="B", direct_cost=10.0, child_cost=3.0),
        ]
        snapshot = _make_snapshot(costs=costs)
        dash._apply_snapshot(snapshot)
        assert dash.state.cost_by_session["s1"] == 7.0
        assert dash.state.cost_by_session["s2"] == 13.0
        assert dash.state.total_cost == 20.0

    def test_ci_fail_count(self):
        dash, _, _ = _make_dashboard()
        prs = [
            PullRequestSummary(
                number=1,
                title="PR1",
                head_ref="a",
                state="open",
                updated_at="",
                ci_status=CI_FAIL,
                review_status="",
                url="",
            ),
            PullRequestSummary(
                number=2,
                title="PR2",
                head_ref="b",
                state="open",
                updated_at="",
                ci_status="pass",
                review_status="",
                url="",
            ),
            PullRequestSummary(
                number=3,
                title="PR3",
                head_ref="c",
                state="open",
                updated_at="",
                ci_status=CI_FAIL,
                review_status="",
                url="",
            ),
        ]
        snapshot = _make_snapshot(prs=prs)
        dash._apply_snapshot(snapshot)
        assert dash.state.prev_ci_fail_count == 2

    def test_project_path_propagated(self):
        dash, _, _ = _make_dashboard()
        snapshot = _make_snapshot()
        dash._apply_snapshot(snapshot)
        assert dash.state.project_path == "/tmp/fake"


# ── Tests: Kanban operations ─────────────────────────────


class TestKanbanOps:
    def test_refresh_kanban_groups_by_stage(self):
        dash, kanban, _ = _make_dashboard()
        kanban.create_project("A", stage="pending")
        kanban.create_project("B", stage="in_progress")
        kanban.create_project("C", stage="done")
        result = dash.refresh_kanban()
        assert len(result["pending"]) == 1
        assert len(result["in_progress"]) == 1
        assert len(result["done"]) == 1

    def test_refresh_kanban_excludes_invalid_stage(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("X", stage="pending")
        p.stage = "nonexistent"
        result = dash.refresh_kanban()
        total = sum(len(result[s]) for s in STAGES)
        assert total == 0

    def test_move_right(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A", stage="pending")
        assert dash.move_project(p.id, "right")
        assert kanban.get_project(p.id).stage == "in_progress"

    def test_move_left(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A", stage="in_progress")
        assert dash.move_project(p.id, "left")
        assert kanban.get_project(p.id).stage == "pending"

    def test_move_right_at_end_returns_false(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A", stage="done")
        assert not dash.move_project(p.id, "right")

    def test_move_left_at_start_returns_false(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A", stage="pending")
        assert not dash.move_project(p.id, "left")

    def test_move_nonexistent_project(self):
        dash, _, _ = _make_dashboard()
        assert not dash.move_project("nonexistent", "right")

    def test_delete_project(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A")
        assert dash.delete_project(p.id)
        assert kanban.get_project(p.id) is None

    def test_link_pr_valid(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A")
        assert dash.link_pr(p.id, "#42")
        assert 42 in kanban.get_project(p.id).pr_numbers

    def test_link_pr_invalid(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A")
        assert not dash.link_pr(p.id, "not-a-number")


# ── Tests: Session lifecycle ──────────────────────────────


class TestSessionLifecycle:
    def test_seed_session_creates_and_links(self):
        dash, kanban, oc = _make_dashboard()
        p = kanban.create_project("My Project", description="Build something")
        result = dash.seed_session_for_project(p)
        assert result.session_id is not None
        assert result.linked
        assert result.error is None
        assert result.session_id in kanban.get_project(p.id).session_ids
        assert len(oc._messages) == 1
        assert "My Project" in oc._messages[0][1]
        assert "Build something" in oc._messages[0][1]

    def test_seed_session_title_only(self):
        dash, kanban, oc = _make_dashboard()
        p = kanban.create_project("Just a title")
        result = dash.seed_session_for_project(p)
        assert result.session_id is not None
        assert oc._messages[0][1] == "Just a title"

    def test_seed_session_failure(self):
        dash, kanban, oc = _make_dashboard()
        oc.create_session = lambda title=None: None  # type: ignore
        p = kanban.create_project("Fail")
        result = dash.seed_session_for_project(p)
        assert result.session_id is None
        assert result.error == "Failed to create session"
        assert not result.linked

    def test_create_project_and_seed(self):
        dash, kanban, oc = _make_dashboard()
        result = dash.create_project_and_seed("New Feature", "Description here")
        assert result.session_id is not None
        assert result.linked
        projects = kanban.list_projects()
        assert len(projects) == 1
        assert projects[0].title == "New Feature"


# ── Tests: Session link/unlink ────────────────────────────


class TestSessionLinking:
    def test_link_session_by_exact_id(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A")
        dash.state.sessions = [_make_session("ses_abc", "Test Session")]
        dash.link_session(p.id, "ses_abc")
        assert "ses_abc" in kanban.get_project(p.id).session_ids

    def test_link_session_by_partial_id(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A")
        dash.state.sessions = [_make_session("ses_abc123", "Test Session")]
        dash.link_session(p.id, "abc123")
        assert "ses_abc123" in kanban.get_project(p.id).session_ids

    def test_link_session_by_title_search(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A")
        dash.state.sessions = [_make_session("ses_x", "Auth Refactor")]
        dash.link_session(p.id, "auth")
        assert "ses_x" in kanban.get_project(p.id).session_ids

    def test_link_session_no_match_uses_raw_value(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A")
        dash.state.sessions = []
        dash.link_session(p.id, "unknown_id")
        assert "unknown_id" in kanban.get_project(p.id).session_ids

    def test_unlink_session_by_exact_id(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A")
        kanban.link_session(p.id, "ses_abc")
        assert dash.unlink_session(p.id, "ses_abc")
        assert "ses_abc" not in kanban.get_project(p.id).session_ids

    def test_unlink_session_by_partial_id(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A")
        kanban.link_session(p.id, "ses_abc123")
        assert dash.unlink_session(p.id, "abc123")
        assert "ses_abc123" not in kanban.get_project(p.id).session_ids

    def test_unlink_nonexistent_project(self):
        dash, _, _ = _make_dashboard()
        assert not dash.unlink_session("fake", "ses_abc")


# ── Tests: Computed values ────────────────────────────────


class TestComputedValues:
    def test_live_session_count(self):
        dash, _, _ = _make_dashboard()
        dash.state.sessions = [
            _make_session("s1"),
            _make_session("s2"),
            _make_session("s3"),
        ]
        dash.state.running_cpu = {"s1": 10.0, "s3": 20.0}
        assert dash.live_session_count() == 2

    def test_stalled_session_count(self):
        dash, _, _ = _make_dashboard()
        dash.state.sessions = [
            _make_session("s1", pending=2),
            _make_session("s2", in_progress=1),
            _make_session("s3", completed=5),
        ]
        dash.state.running_cpu = {}
        assert dash.stalled_session_count() == 2

    def test_stalled_excludes_running(self):
        dash, _, _ = _make_dashboard()
        dash.state.sessions = [_make_session("s1", pending=2)]
        dash.state.running_cpu = {"s1": 50.0}
        assert dash.stalled_session_count() == 0

    def test_total_cpu(self):
        dash, _, _ = _make_dashboard()
        procs = [
            RunningProcess(
                pid=1, cpu_percent=30.0, mem_mb=100, terminal="", session_id="s1"
            ),
            RunningProcess(
                pid=2, cpu_percent=20.0, mem_mb=200, terminal="", session_id=None
            ),
        ]
        snapshot = _make_snapshot(processes=procs)
        dash._apply_snapshot(snapshot)
        assert dash.total_cpu() == 50.0

    def test_total_cpu_no_snapshot(self):
        dash, _, _ = _make_dashboard()
        assert dash.total_cpu() == 0.0

    def test_ci_fail_count_method(self):
        dash, _, _ = _make_dashboard()
        prs = [
            PullRequestSummary(
                number=1,
                title="",
                head_ref="",
                state="",
                updated_at="",
                ci_status=CI_FAIL,
                review_status="",
                url="",
            ),
            PullRequestSummary(
                number=2,
                title="",
                head_ref="",
                state="",
                updated_at="",
                ci_status="pass",
                review_status="",
                url="",
            ),
        ]
        snapshot = _make_snapshot(prs=prs)
        dash._apply_snapshot(snapshot)
        assert dash.ci_fail_count() == 1

    def test_ci_fail_count_no_snapshot(self):
        dash, _, _ = _make_dashboard()
        assert dash.ci_fail_count() == 0


# ── Tests: Init branch ───────────────────────────────────


class TestInitBranch:
    def test_skips_if_already_set(self):
        dash, _, _ = _make_dashboard()
        dash.state.current_branch = "main"
        dash.init_branch()
        assert dash.state.current_branch == "main"

    @patch("oc_dashboard.core.subprocess.run")
    def test_sets_branch_from_git(self, mock_run):
        dash, _, _ = _make_dashboard()
        dash.state.project_path = "/tmp/repo"
        mock_run.return_value = MagicMock(returncode=0, stdout="feature/auth\n")
        dash.init_branch()
        assert dash.state.current_branch == "feature/auth"

    @patch("oc_dashboard.core.subprocess.run")
    def test_ignores_head(self, mock_run):
        dash, _, _ = _make_dashboard()
        dash.state.project_path = "/tmp/repo"
        mock_run.return_value = MagicMock(returncode=0, stdout="HEAD\n")
        dash.init_branch()
        assert dash.state.current_branch == ""

    @patch("oc_dashboard.core.subprocess.run")
    def test_handles_git_failure(self, mock_run):
        dash, _, _ = _make_dashboard()
        dash.state.project_path = "/tmp/repo"
        mock_run.return_value = MagicMock(returncode=128, stdout="")
        dash.init_branch()
        assert dash.state.current_branch == ""

    @patch("oc_dashboard.core.subprocess.run")
    def test_handles_exception(self, mock_run):
        dash, _, _ = _make_dashboard()
        dash.state.project_path = "/tmp/repo"
        mock_run.side_effect = OSError("not found")
        dash.init_branch()
        assert dash.state.current_branch == ""


# ── Tests: Archive / Restore ─────────────────────────────


class TestArchive:
    def test_archive_moves_to_archived_stage(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A", stage="done")
        assert dash.archive_project(p.id)
        assert kanban.get_project(p.id).stage == "archived"

    def test_archive_saves_previous_stage(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A", stage="in_progress")
        dash.archive_project(p.id)
        assert kanban.get_project(p.id).previous_stage == "in_progress"

    def test_archive_nonexistent_returns_false(self):
        dash, _, _ = _make_dashboard()
        assert not dash.archive_project("nope")

    def test_restore_uses_previous_stage(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A", stage="in_progress")
        dash.archive_project(p.id)
        assert dash.restore_project(p.id)
        assert kanban.get_project(p.id).stage == "in_progress"

    def test_restore_clears_previous_stage(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A", stage="pending")
        dash.archive_project(p.id)
        dash.restore_project(p.id)
        assert kanban.get_project(p.id).previous_stage is None

    def test_restore_falls_back_to_done_without_previous(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A", stage="pending")
        kanban.move_project(p.id, "archived")
        assert dash.restore_project(p.id)
        assert kanban.get_project(p.id).stage == "done"

    def test_restore_explicit_stage_overrides_previous(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A", stage="done")
        dash.archive_project(p.id)
        assert dash.restore_project(p.id, stage="pending")
        assert kanban.get_project(p.id).stage == "pending"

    def test_restore_invalid_explicit_stage_falls_back(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("A", stage="pending")
        dash.archive_project(p.id)
        assert dash.restore_project(p.id, stage="nonexistent")
        assert kanban.get_project(p.id).stage == "done"

    def test_restore_nonexistent_returns_false(self):
        dash, _, _ = _make_dashboard()
        assert not dash.restore_project("nope")

    def test_list_archived(self):
        dash, kanban, _ = _make_dashboard()
        kanban.create_project("Active", stage="pending")
        p2 = kanban.create_project("Old", stage="done")
        dash.archive_project(p2.id)
        p3 = kanban.create_project("Older", stage="in_progress")
        dash.archive_project(p3.id)
        archived = dash.list_archived()
        assert len(archived) == 2
        assert all(p.stage == "archived" for p in archived)

    def test_list_archived_empty(self):
        dash, kanban, _ = _make_dashboard()
        kanban.create_project("Active", stage="pending")
        assert dash.list_archived() == []

    def test_archived_excluded_from_board(self):
        dash, kanban, _ = _make_dashboard()
        kanban.create_project("Active", stage="pending")
        p2 = kanban.create_project("Archived", stage="done")
        dash.archive_project(p2.id)
        by_stage = dash.refresh_kanban()
        all_board_projects = []
        for stage in STAGES:
            all_board_projects.extend(by_stage[stage])
        assert len(all_board_projects) == 1
        assert all_board_projects[0].title == "Active"

    def test_round_trip_preserves_stage(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("Feature X", stage="in_progress")
        dash.archive_project(p.id)
        assert kanban.get_project(p.id).stage == "archived"
        assert kanban.get_project(p.id).previous_stage == "in_progress"
        assert len(dash.list_archived()) == 1
        dash.restore_project(p.id)
        assert kanban.get_project(p.id).stage == "in_progress"
        assert kanban.get_project(p.id).previous_stage is None
        assert len(dash.list_archived()) == 0


class TestSearch:
    def test_empty_query_returns_empty(self):
        dash, _, _ = _make_dashboard()
        assert dash.search("") == []
        assert dash.search("   ") == []

    def test_match_project_by_title(self):
        dash, kanban, _ = _make_dashboard()
        kanban.create_project("Ad waterfall refactor", stage="in_progress")
        kanban.create_project("Fix login bug", stage="pending")
        results = dash.search("waterfall")
        assert len(results) == 1
        assert results[0].kind == "project"
        assert "waterfall" in results[0].title.lower()

    def test_match_project_by_description(self):
        dash, kanban, _ = _make_dashboard()
        kanban.create_project(
            "Refactor", description="Unify ad chooser preresolved", stage="done"
        )
        results = dash.search("preresolved")
        assert len(results) == 1
        assert results[0].kind == "project"
        assert "preresolved" in results[0].detail.lower()

    def test_match_project_by_session_id(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("My project", stage="pending")
        kanban.link_session(p.id, "ses_2dffd0f22ffeL175")
        results = dash.search("2dffd0f22ffe")
        assert len(results) == 1
        assert results[0].kind == "project"
        assert "session" in results[0].detail.lower()

    def test_match_project_by_pr_number(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("PR work", stage="done")
        kanban.link_pr(p.id, 38213)
        results = dash.search("38213")
        assert len(results) == 1
        assert results[0].kind == "project"
        assert "38213" in results[0].detail

    def test_match_project_by_tag(self):
        dash, kanban, _ = _make_dashboard()
        kanban.create_project("Tagged work", stage="pending", tags=["frontend"])
        results = dash.search("frontend")
        assert len(results) == 1
        assert "tag" in results[0].detail.lower()

    def test_match_session_by_title(self):
        dash, _, _ = _make_dashboard()
        dash.state.sessions = [
            _make_session("ses_1", title="Help me troubleshoot kafka"),
            _make_session("ses_2", title="Fix styling on header"),
        ]
        results = dash.search("kafka")
        assert len(results) == 1
        assert results[0].kind == "session"
        assert results[0].id == "ses_1"

    def test_match_session_by_id(self):
        dash, _, _ = _make_dashboard()
        dash.state.sessions = [
            _make_session("ses_abc123", title="Some work"),
        ]
        results = dash.search("abc123")
        assert len(results) == 1
        assert results[0].kind == "session"

    def test_case_insensitive(self):
        dash, kanban, _ = _make_dashboard()
        kanban.create_project("Kafka Streaming Fix", stage="pending")
        results = dash.search("kafka")
        assert len(results) == 1
        results_upper = dash.search("KAFKA")
        assert len(results_upper) == 1

    def test_mixed_results_projects_and_sessions(self):
        dash, kanban, _ = _make_dashboard()
        kanban.create_project("Kafka consumer lag", stage="in_progress")
        dash.state.sessions = [
            _make_session("ses_kafka_1", title="Kafka debug session"),
        ]
        results = dash.search("kafka")
        assert len(results) == 2
        kinds = {r.kind for r in results}
        assert kinds == {"project", "session"}

    def test_session_deduped_when_linked_to_matching_project(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("Kafka work", stage="pending")
        kanban.link_session(p.id, "ses_kafka_linked")
        dash.state.sessions = [
            _make_session("ses_kafka_linked", title="Kafka debug"),
        ]
        results = dash.search("kafka")
        assert len(results) == 1
        assert results[0].kind == "project"

    def test_no_matches(self):
        dash, kanban, _ = _make_dashboard()
        kanban.create_project("Something else", stage="pending")
        dash.state.sessions = [_make_session("ses_1", title="Unrelated")]
        assert dash.search("xyznonexistent") == []

    def test_searches_archived_projects_too(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("Old feature", stage="done")
        dash.archive_project(p.id)
        results = dash.search("old feature")
        assert len(results) == 1
        assert results[0].stage == "archived"

    def test_skips_group_header_sessions(self):
        dash, _, _ = _make_dashboard()
        header = _make_session("dir_header", title="kafka-repo")
        header.is_group_header = True
        dash.state.sessions = [header]
        assert dash.search("kafka") == []

    def test_result_has_project_metadata(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("Test project", stage="in_progress")
        kanban.link_session(p.id, "ses_abc")
        kanban.link_pr(p.id, 12345)
        results = dash.search("test project")
        assert len(results) == 1
        r = results[0]
        assert r.stage == "in_progress"
        assert "ses_abc" in r.session_ids
        assert 12345 in r.pr_numbers

    def test_excerpt_shows_context_around_match(self):
        dash, kanban, _ = _make_dashboard()
        long_desc = "This is a very long description about the ad waterfall refactor that was done"
        kanban.create_project("Refactor", description=long_desc, stage="done")
        results = dash.search("waterfall")
        assert len(results) == 1
        assert "waterfall" in results[0].detail.lower()


class TestWheel:
    def test_wheel_empty_initially(self):
        dash, _, _ = _make_dashboard()
        assert dash.wheel_list() == []
        assert dash.wheel_current() is None

    def test_wheel_add_and_list(self):
        dash, kanban, _ = _make_dashboard()
        p1 = kanban.create_project("Alpha", stage="pending")
        p2 = kanban.create_project("Beta", stage="in_progress")
        assert dash.wheel_add(p1.id)
        assert dash.wheel_add(p2.id)
        items = dash.wheel_list()
        assert len(items) == 2
        assert items[0].id == p1.id
        assert items[1].id == p2.id

    def test_wheel_add_duplicate_returns_false(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("Alpha", stage="pending")
        assert dash.wheel_add(p.id)
        assert not dash.wheel_add(p.id)
        assert len(dash.wheel_list()) == 1

    def test_wheel_add_nonexistent_returns_false(self):
        dash, _, _ = _make_dashboard()
        assert not dash.wheel_add("nonexistent_id")
        assert dash.wheel_list() == []

    def test_wheel_remove(self):
        dash, kanban, _ = _make_dashboard()
        p1 = kanban.create_project("Alpha", stage="pending")
        p2 = kanban.create_project("Beta", stage="pending")
        dash.wheel_add(p1.id)
        dash.wheel_add(p2.id)
        assert dash.wheel_remove(p1.id)
        items = dash.wheel_list()
        assert len(items) == 1
        assert items[0].id == p2.id

    def test_wheel_remove_adjusts_cursor(self):
        dash, kanban, _ = _make_dashboard()
        p1 = kanban.create_project("A", stage="pending")
        p2 = kanban.create_project("B", stage="pending")
        p3 = kanban.create_project("C", stage="pending")
        dash.wheel_add(p1.id)
        dash.wheel_add(p2.id)
        dash.wheel_add(p3.id)
        dash.wheel_next()
        dash.wheel_next()
        current_before = dash.wheel_current()
        assert current_before is not None
        assert current_before.id == p3.id
        dash.wheel_remove(p1.id)
        current_after = dash.wheel_current()
        assert current_after is not None
        assert current_after.id == p3.id

    def test_wheel_next_cycles(self):
        dash, kanban, _ = _make_dashboard()
        p1 = kanban.create_project("A", stage="pending")
        p2 = kanban.create_project("B", stage="pending")
        p3 = kanban.create_project("C", stage="pending")
        dash.wheel_add(p1.id)
        dash.wheel_add(p2.id)
        dash.wheel_add(p3.id)
        r1 = dash.wheel_next()
        assert r1 is not None and r1.id == p2.id
        r2 = dash.wheel_next()
        assert r2 is not None and r2.id == p3.id
        r3 = dash.wheel_next()
        assert r3 is not None and r3.id == p1.id

    def test_wheel_prev_cycles(self):
        dash, kanban, _ = _make_dashboard()
        p1 = kanban.create_project("A", stage="pending")
        p2 = kanban.create_project("B", stage="pending")
        p3 = kanban.create_project("C", stage="pending")
        dash.wheel_add(p1.id)
        dash.wheel_add(p2.id)
        dash.wheel_add(p3.id)
        r1 = dash.wheel_prev()
        assert r1 is not None and r1.id == p3.id
        r2 = dash.wheel_prev()
        assert r2 is not None and r2.id == p2.id
        r3 = dash.wheel_prev()
        assert r3 is not None and r3.id == p1.id

    def test_wheel_next_empty_returns_none(self):
        dash, _, _ = _make_dashboard()
        assert dash.wheel_next() is None

    def test_wheel_current(self):
        dash, kanban, _ = _make_dashboard()
        p1 = kanban.create_project("A", stage="pending")
        p2 = kanban.create_project("B", stage="pending")
        dash.wheel_add(p1.id)
        dash.wheel_add(p2.id)
        current = dash.wheel_current()
        assert current is not None
        assert current.id == p1.id
        dash.wheel_next()
        current = dash.wheel_current()
        assert current is not None
        assert current.id == p2.id

    def test_wheel_list_returns_full_projects(self):
        dash, kanban, _ = _make_dashboard()
        p = kanban.create_project("Full Project", description="desc", stage="done")
        kanban.link_session(p.id, "ses_abc")
        kanban.link_pr(p.id, 42)
        dash.wheel_add(p.id)
        items = dash.wheel_list()
        assert len(items) == 1
        assert items[0].title == "Full Project"
        assert items[0].description == "desc"
        assert "ses_abc" in items[0].session_ids
        assert 42 in items[0].pr_numbers

    def test_wheel_list_skips_deleted_projects(self):
        dash, kanban, _ = _make_dashboard()
        p1 = kanban.create_project("A", stage="pending")
        p2 = kanban.create_project("B", stage="pending")
        dash.wheel_add(p1.id)
        dash.wheel_add(p2.id)
        kanban.delete_project(p1.id)
        items = dash.wheel_list()
        assert len(items) == 1
        assert items[0].id == p2.id
