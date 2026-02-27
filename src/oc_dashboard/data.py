from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import glob as globmod
import os
import re
import sqlite3
import subprocess
from typing import Any, List, Optional, Tuple


DB_PATH = os.path.expanduser("~/.local/share/opencode/opencode.db")
WAL_PATH = DB_PATH + "-wal"
LOG_DIR = os.path.expanduser("~/.local/share/opencode/log")

# Nerd Font icons (Private Use Area) - requires Nerd Font patched terminal font
NF_CHECK = chr(0xF00C)  # nf-fa-check
NF_TIMES = chr(0xF00D)  # nf-fa-times
NF_SEARCH = chr(0xF002)  # nf-fa-search
NF_EYE = chr(0xF06E)  # nf-fa-eye
NF_EXCL_CIRCLE = chr(0xF06A)  # nf-fa-exclamation_circle
NF_WARNING = chr(0xF071)  # nf-fa-warning
NF_SPINNER = chr(0xF110)  # nf-fa-spinner
NF_BOLT = chr(0xF0E7)  # nf-fa-bolt
NF_BRANCH = chr(0xE0A0)  # nf-pl-branch

# CI status constants (exported for app.py comparisons)
CI_PASS = NF_CHECK
CI_FAIL = NF_TIMES
CI_PENDING = NF_SPINNER


@dataclass
class SessionSummary:
    id: str
    title: str
    updated: datetime
    pending: int
    in_progress: int
    completed: int
    cancelled: int
    parent_id: Optional[str]
    depth: int
    directory: str = ""
    is_group_header: bool = False

    @property
    def total(self) -> int:
        return self.pending + self.in_progress + self.completed + self.cancelled


@dataclass
class TodoItem:
    status: str
    content: str


@dataclass
class RunningProcess:
    pid: int
    cpu_percent: float
    mem_mb: int
    terminal: str
    session_id: Optional[str]


@dataclass
class WorktreeStatus:
    path: str
    branch: str
    dirty: bool


@dataclass
class BackgroundWorker:
    id: str
    parent_id: str
    agent_type: str
    description: str
    created: datetime
    updated: datetime
    message_count: int


@dataclass
class PullRequestSummary:
    number: int
    title: str
    head_ref: str
    state: str
    updated_at: str
    ci_status: str
    review_status: str
    url: str


@dataclass
class Recommendation:
    priority: str  # critical, high, medium, low
    icon: str
    text: str
    action: Optional[str]  # "pr:37410" or "session:ses_xxx" or None


@dataclass
class SessionCost:
    session_id: str
    title: str
    direct_cost: float
    child_cost: float

    @property
    def total_cost(self):
        # type: () -> float
        return self.direct_cost + self.child_cost


@dataclass
class DailySpend:
    day: str
    total_cost: float


@dataclass
class LogEvent:
    time_str: str
    event_type: str
    fields: dict  # type: dict


@dataclass
class DashboardSnapshot:
    sessions: List[SessionSummary]
    todos_by_session: dict
    running_processes: List[RunningProcess]
    worktrees: List[WorktreeStatus]
    workers_by_session: dict
    prs: List[PullRequestSummary]
    recommendations: List[Recommendation]
    session_costs: List[SessionCost]
    daily_spend: List[DailySpend]
    project_path: Optional[str]
    errors: List[str]


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _parse_db_datetime(value):
    # type: (str) -> datetime
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def discover_project_path():
    # type: () -> Optional[str]
    if not os.path.exists(DB_PATH):
        return None

    query = (
        "SELECT worktree FROM project WHERE id = ("
        "  SELECT project_id FROM session WHERE time_archived IS NULL AND parent_id IS NULL "
        "  ORDER BY time_updated DESC LIMIT 1"
        ");"
    )

    try:
        connection = _connect()
        try:
            row = connection.execute(query).fetchone()
        finally:
            connection.close()
        if not row:
            return None
        worktree = row["worktree"]
        return str(worktree) if worktree else None
    except Exception:
        return None


def fetch_sessions(limit):
    # type: (int) -> List[SessionSummary]
    """Fetch root sessions grouped by repo, with forks nested under originals.

    Tree structure:
      Group header (repo name)          depth=0, is_group_header=True
        ├─ Session A                    depth=1
        │  └─ Session A (fork #1)       depth=2
        └─ Session B                    depth=1
    """
    if not os.path.exists(DB_PATH):
        return []

    query = (
        "SELECT s.id, s.title, s.parent_id, s.directory, "
        "  datetime(s.time_updated/1000, 'unixepoch') as updated,"
        "  (SELECT COUNT(*) FROM todo t WHERE t.session_id = s.id AND t.status = 'pending') as pending,"
        "  (SELECT COUNT(*) FROM todo t WHERE t.session_id = s.id AND t.status = 'in_progress') as in_progress,"
        "  (SELECT COUNT(*) FROM todo t WHERE t.session_id = s.id AND t.status = 'completed') as completed,"
        "  (SELECT COUNT(*) FROM todo t WHERE t.session_id = s.id AND t.status = 'cancelled') as cancelled "
        "FROM session s "
        "WHERE s.time_archived IS NULL "
        "  AND s.parent_id IS NULL "
        "ORDER BY s.time_updated DESC "
        "LIMIT ?;"
    )

    _FORK_RE = re.compile(r"^(.+?)\s*\(fork #(\d+)\)$")

    def _row_to_session(row, depth=0):
        # type: (Any, int) -> SessionSummary
        try:
            updated = _parse_db_datetime(str(row["updated"]))
        except Exception:
            updated = datetime.now()
        return SessionSummary(
            id=str(row["id"]),
            title=str(row["title"] or "(untitled)"),
            updated=updated,
            pending=int(row["pending"] or 0),
            in_progress=int(row["in_progress"] or 0),
            completed=int(row["completed"] or 0),
            cancelled=int(row["cancelled"] or 0),
            parent_id=str(row["parent_id"]) if row["parent_id"] else None,
            depth=depth,
            directory=str(row["directory"] or ""),
        )

    try:
        connection = _connect()
        try:
            rows = connection.execute(query, (limit,)).fetchall()
        finally:
            connection.close()

        # Group by directory (repo)
        groups = {}  # type: dict  # directory -> [rows]
        for row in rows:
            d = str(row["directory"] or "")
            groups.setdefault(d, []).append(row)

        # Build each repo group, then sort groups and sessions by effective recency
        repo_blocks = []  # type: list  # [(effective_time, header, session_list)]
        for directory, dir_rows in groups.items():
            repo_name = os.path.basename(directory) or directory

            # Detect forks: match 'Title (fork #N)' -> group under original 'Title'
            originals = []  # type: list
            fork_bucket = {}  # type: dict  # base_title -> [fork_sessions]

            for row in dir_rows:
                sess = _row_to_session(row, depth=1)
                m = _FORK_RE.match(sess.title)
                if m:
                    base = m.group(1)
                    fork_bucket.setdefault(base, []).append(sess)
                else:
                    originals.append(sess)

            # Build session families: (effective_time, original, [forks])
            families = []  # type: list
            for sess in originals:
                forks = fork_bucket.pop(sess.title, [])
                # Effective time = max of own time and all fork times
                effective = sess.updated
                for f in forks:
                    if f.updated > effective:
                        effective = f.updated
                families.append((effective, sess, forks))

            # Orphan forks (no matching original) become their own family
            for base_title, forks in fork_bucket.items():
                for fork in forks:
                    families.append((fork.updated, fork, []))

            # Sort families by effective time, newest first
            families.sort(key=lambda fam: fam[0], reverse=True)

            # Repo-level effective time = newest family
            repo_effective = families[0][0] if families else datetime.now()

            header = SessionSummary(
                id="group:" + directory,
                title=repo_name,
                updated=repo_effective,
                pending=0,
                in_progress=0,
                completed=0,
                cancelled=0,
                parent_id=None,
                depth=0,
                directory=directory,
                is_group_header=True,
            )

            # Flatten families into ordered session list
            family_sessions = []  # type: list
            for effective, sess, forks in families:
                family_sessions.append(sess)
                for fork in sorted(forks, key=lambda f: f.title):
                    fork.depth = 2
                    fork.parent_id = sess.id
                    family_sessions.append(fork)

            repo_blocks.append((repo_effective, header, family_sessions))

        # Sort repo groups by effective time, newest first
        repo_blocks.sort(key=lambda b: b[0], reverse=True)

        # Assemble final flat list
        sessions = []  # type: list
        for _, header, family_sessions in repo_blocks:
            sessions.append(header)
            sessions.extend(family_sessions)

    except Exception:
        return []
    return sessions


def fetch_todos_for_session(session_id):
    # type: (str) -> List[TodoItem]
    if not os.path.exists(DB_PATH):
        return []

    query = "SELECT t.status, t.content FROM todo t WHERE t.session_id = ?;"
    items = []
    try:
        connection = _connect()
        try:
            rows = connection.execute(query, (session_id,)).fetchall()
        finally:
            connection.close()
        for row in rows:
            items.append(
                TodoItem(
                    status=str(row["status"] or "pending"),
                    content=str(row["content"] or ""),
                )
            )
    except Exception:
        return []
    return items


def _parse_agent_type(title):
    # type: (str) -> Tuple[str, str]
    match = re.search(r"\(@(\S+)\s+subagent\)\s*$", title)
    if match:
        agent_type = match.group(1)
        description = title[: match.start()].strip()
        return agent_type, description
    return "unknown", title


def fetch_workers_for_session(session_id):
    # type: (str) -> List[BackgroundWorker]
    if not os.path.exists(DB_PATH):
        return []

    query = (
        "SELECT s.id, s.title, s.parent_id, "
        "  datetime(s.time_created/1000, 'unixepoch') as created, "
        "  datetime(s.time_updated/1000, 'unixepoch') as updated, "
        "  (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id) as msg_count "
        "FROM session s "
        "WHERE s.parent_id = ? "
        "ORDER BY s.time_created DESC;"
    )

    workers = []
    try:
        connection = _connect()
        try:
            rows = connection.execute(query, (session_id,)).fetchall()
        finally:
            connection.close()
        for row in rows:
            try:
                created = _parse_db_datetime(str(row["created"]))
            except Exception:
                created = datetime.now()
            try:
                updated = _parse_db_datetime(str(row["updated"]))
            except Exception:
                updated = datetime.now()
            agent_type, description = _parse_agent_type(str(row["title"] or ""))
            workers.append(
                BackgroundWorker(
                    id=str(row["id"]),
                    parent_id=session_id,
                    agent_type=agent_type,
                    description=description,
                    created=created,
                    updated=updated,
                    message_count=int(row["msg_count"] or 0),
                )
            )
    except Exception:
        return []
    return workers


def fetch_running_processes():
    # type: () -> List[RunningProcess]
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid,pcpu,rss,tty,command"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []

    if result.returncode not in (0, 1):
        return []

    entries = []
    session_pattern = re.compile(r"(?:-s|--session)\s+(ses_[A-Za-z0-9_\-]+)")
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("PID"):
            continue
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[0])
            cpu_percent = float(parts[1])
            rss_kb = int(parts[2])
        except Exception:
            continue
        terminal = parts[3]
        command_text = parts[4]

        argv0 = command_text.split(None, 1)[0]
        if os.path.basename(argv0) != "opencode":
            continue

        match = session_pattern.search(command_text)
        session_id = match.group(1) if match else None
        entries.append(
            RunningProcess(
                pid=pid,
                cpu_percent=cpu_percent,
                mem_mb=rss_kb // 1024,
                terminal=terminal,
                session_id=session_id,
            )
        )
    return entries


def fetch_worktrees(project_path=None):
    # type: (Optional[str]) -> List[WorktreeStatus]
    if not project_path:
        return []

    try:
        result = subprocess.run(
            ["git", "-C", project_path, "worktree", "list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    worktrees = []
    for line in result.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        match = re.match(r"^(\S+)\s+\S+\s+\[(.+)\]$", raw)
        if not match:
            continue
        path = match.group(1)
        branch = match.group(2)
        dirty = False
        try:
            status_result = subprocess.run(
                ["git", "-C", path, "status", "--porcelain"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            dirty = bool(status_result.stdout.strip())
        except Exception:
            dirty = False
        worktrees.append(WorktreeStatus(path=path, branch=branch, dirty=dirty))
    return worktrees


def fetch_session_costs(limit=20):
    # type: (int) -> List[SessionCost]
    if not os.path.exists(DB_PATH):
        return []

    query = (
        "SELECT "
        "  COALESCE(s.parent_id, s.id) as root_id, "
        "  (SELECT p.title FROM session p WHERE p.id = COALESCE(s.parent_id, s.id)) as title, "
        "  ROUND(SUM(CASE WHEN s.parent_id IS NULL "
        "    THEN json_extract(m.data, '$.cost') ELSE 0 END), 6) as direct_cost, "
        "  ROUND(SUM(CASE WHEN s.parent_id IS NOT NULL "
        "    THEN json_extract(m.data, '$.cost') ELSE 0 END), 6) as child_cost "
        "FROM session s "
        "JOIN message m ON m.session_id = s.id "
        "WHERE json_extract(m.data, '$.role') = 'assistant' "
        "  AND json_extract(m.data, '$.cost') > 0 "
        "GROUP BY root_id "
        "ORDER BY (direct_cost + child_cost) DESC "
        "LIMIT ?;"
    )

    costs = []
    try:
        connection = _connect()
        try:
            rows = connection.execute(query, (limit,)).fetchall()
        finally:
            connection.close()
        for row in rows:
            costs.append(
                SessionCost(
                    session_id=str(row["root_id"]),
                    title=str(row["title"] or "(untitled)"),
                    direct_cost=float(row["direct_cost"] or 0),
                    child_cost=float(row["child_cost"] or 0),
                )
            )
    except Exception:
        return []
    return costs


def fetch_daily_spend(days=14):
    # type: (int) -> List[DailySpend]
    if not os.path.exists(DB_PATH):
        return []
    if days <= 0:
        return []

    start_dt = datetime.now() - timedelta(days=days - 1)
    start_day = start_dt.strftime("%Y-%m-%d")

    query = (
        "SELECT "
        "  DATE(m.time_created / 1000, 'unixepoch', 'localtime') as day, "
        "  ROUND(SUM(json_extract(m.data, '$.cost')), 6) as total_cost "
        "FROM message m "
        "WHERE json_extract(m.data, '$.role') = 'assistant' "
        "  AND json_extract(m.data, '$.cost') > 0 "
        "  AND DATE(m.time_created / 1000, 'unixepoch', 'localtime') >= ? "
        "GROUP BY day "
        "ORDER BY day ASC;"
    )

    spend_by_day = {}  # type: dict
    try:
        connection = _connect()
        try:
            rows = connection.execute(query, (start_day,)).fetchall()
        finally:
            connection.close()
        for row in rows:
            day = str(row["day"] or "")
            if not day:
                continue
            spend_by_day[day] = float(row["total_cost"] or 0)
    except Exception:
        return []

    spends = []  # type: List[DailySpend]
    for i in range(days):
        day_dt = start_dt + timedelta(days=i)
        day_key = day_dt.strftime("%Y-%m-%d")
        spends.append(
            DailySpend(
                day=day_key,
                total_cost=spend_by_day.get(day_key, 0.0),
            )
        )
    return spends


def _compute_ci_status(checks):
    # type: (list) -> str
    if not checks:
        return CI_PENDING
    states = [str(item.get("state", "")).lower() for item in checks]
    if any(
        state in ("fail", "failed", "error", "cancelled", "timed_out")
        for state in states
    ):
        return CI_FAIL
    if any(
        state in ("pending", "queued", "in_progress", "waiting", "startup_failure")
        for state in states
    ):
        return CI_PENDING
    if all(
        state in ("pass", "passed", "success", "completed", "skipping", "skipped")
        for state in states
    ):
        return CI_PASS
    return CI_PENDING


def fetch_prs(project_path=None):
    # type: (Optional[str]) -> List[PullRequestSummary]
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--author",
                "@me",
                "--state",
                "open",
                "--json",
                "number,title,state,headRefName,updatedAt,url,reviewDecision",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
            cwd=project_path or None,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    try:
        payload = json.loads(result.stdout)
    except Exception:
        return []

    prs = []
    for item in payload:
        number = int(item.get("number", 0))
        if not number:
            continue
        ci_status = CI_PENDING
        try:
            checks_result = subprocess.run(
                ["gh", "pr", "checks", str(number), "--json", "name,state"],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
                cwd=project_path or None,
            )
            if checks_result.returncode == 0:
                checks = json.loads(checks_result.stdout)
                if isinstance(checks, list):
                    ci_status = _compute_ci_status(checks)
        except Exception:
            ci_status = CI_PENDING

        review_raw = str(item.get("reviewDecision", "") or "")
        review_map = {
            "APPROVED": NF_CHECK + " Approved",
            "CHANGES_REQUESTED": NF_TIMES + " Changes",
            "REVIEW_REQUIRED": NF_EYE + " Needs review",
        }
        review_status = review_map.get(review_raw, NF_SPINNER + " Pending")

        prs.append(
            PullRequestSummary(
                number=number,
                title=str(item.get("title", "")),
                state=str(item.get("state", "OPEN")),
                head_ref=str(item.get("headRefName", "")),
                updated_at=str(item.get("updatedAt", "")),
                ci_status=ci_status,
                review_status=review_status,
                url=str(item.get("url", "")),
            )
        )
    return prs


def relative_time(value):
    # type: (datetime) -> str
    delta = datetime.now() - value
    if delta < timedelta(minutes=1):
        return "now"
    if delta < timedelta(hours=1):
        minutes = max(1, int(delta.total_seconds() // 60))
        return "%sm" % minutes
    if delta < timedelta(hours=1):
        hours = max(1, int(delta.total_seconds() // 3600))
        return "%sh" % hours
    if delta < timedelta(days=1):
        hours = max(1, int(delta.total_seconds() // 3600))
        return "%sh" % hours
    days = max(1, delta.days)
    return "%sd" % days


def session_status(session, running_cpu):
    # type: (SessionSummary, Optional[float]) -> str
    if running_cpu is not None:
        if running_cpu < 5:
            return "waiting"
        return "running"
    has_active_todos = session.pending > 0 or session.in_progress > 0
    if has_active_todos:
        return "stalled"
    if datetime.now() - session.updated <= timedelta(days=3):
        return "done"
    return "old"


def build_recommendations(snapshot):
    # type: (DashboardSnapshot) -> List[Recommendation]
    recs = []
    running_ids = set()
    for p in snapshot.running_processes:
        if p.session_id and p.cpu_percent > 1:
            running_ids.add(p.session_id)

    # Critical: failing CI
    for pr in snapshot.prs:
        if pr.ci_status == CI_FAIL:
            recs.append(
                Recommendation(
                    priority="critical",
                    icon=NF_EXCL_CIRCLE,
                    text="Fix failing CI on #%s: %s" % (pr.number, pr.title[:38]),
                    action="pr:%s" % pr.number,
                )
            )

    # High: stalled sessions with pending work
    for session in snapshot.sessions[:20]:
        active = session.pending + session.in_progress
        if active > 0 and session.id not in running_ids:
            recs.append(
                Recommendation(
                    priority="high",
                    icon=NF_BOLT,
                    text="Resume: %s (%s pending)" % (session.title[:35], active),
                    action="session:%s" % session.id,
                )
            )

    # Medium: PRs with green CI needing review
    for pr in snapshot.prs:
        if pr.ci_status == CI_PASS and "Needs review" in pr.review_status:
            recs.append(
                Recommendation(
                    priority="medium",
                    icon=NF_EYE,
                    text="Request review on #%s -- CI green" % pr.number,
                    action="pr:%s" % pr.number,
                )
            )

    # Medium: PRs with pending CI
    for pr in snapshot.prs:
        if pr.ci_status == CI_PENDING:
            recs.append(
                Recommendation(
                    priority="medium",
                    icon=NF_SPINNER,
                    text="CI running on #%s: %s" % (pr.number, pr.title[:38]),
                    action="pr:%s" % pr.number,
                )
            )

    # Low: high unmapped CPU
    unmapped = sum(
        p.cpu_percent for p in snapshot.running_processes if not p.session_id
    )
    if unmapped > 20:
        recs.append(
            Recommendation(
                priority="low",
                icon=NF_SEARCH,
                text="Orphan process: %d%% unmapped CPU" % int(round(unmapped)),
                action=None,
            )
        )

    # Low: dirty worktrees without PRs
    pr_branches = set(pr.head_ref for pr in snapshot.prs)
    for wt in snapshot.worktrees:
        if wt.dirty and wt.branch not in pr_branches and wt.branch != "main":
            recs.append(
                Recommendation(
                    priority="low",
                    icon=NF_BRANCH,
                    text="WIP worktree: %s (no PR)" % wt.branch[:35],
                    action=None,
                )
            )

    return recs


def build_snapshot(limit=30):
    # type: (int) -> DashboardSnapshot
    errors = []
    if not os.path.exists(DB_PATH):
        errors.append("OpenCode DB not found at %s" % DB_PATH)

    sessions = fetch_sessions(limit=limit)
    todos_by_session = {}
    for session in sessions:
        todos_by_session[session.id] = fetch_todos_for_session(session.id)

    workers_by_session = {}
    for session in sessions:
        workers_by_session[session.id] = fetch_workers_for_session(session.id)

    running_processes = fetch_running_processes()
    project_path = discover_project_path()
    if not project_path:
        errors.append("Could not discover project path from DB")

    worktrees = fetch_worktrees(project_path=project_path)
    prs = fetch_prs(project_path=project_path)
    session_costs = fetch_session_costs(limit=20)
    daily_spend = fetch_daily_spend(days=14)

    snapshot = DashboardSnapshot(
        sessions=sessions,
        todos_by_session=todos_by_session,
        running_processes=running_processes,
        workers_by_session=workers_by_session,
        worktrees=worktrees,
        prs=prs,
        recommendations=[],
        session_costs=session_costs,
        daily_spend=daily_spend,
        project_path=project_path,
        errors=errors,
    )
    snapshot.recommendations = build_recommendations(snapshot)
    return snapshot


# ── Log tailing infrastructure ───────────────────────────────────


def get_wal_mtime():
    # type: () -> Optional[float]
    try:
        return os.path.getmtime(WAL_PATH)
    except OSError:
        return None


def find_latest_log():
    # type: () -> Optional[str]
    if not os.path.isdir(LOG_DIR):
        return None
    files = globmod.glob(os.path.join(LOG_DIR, "*.log"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


# Regex for log lines:
# INFO  2026-02-24T20:34:51 +499ms service=bus type=message.updated publishing
_LOG_RE = re.compile(
    r"^(\w+)\s+(\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2}))\s+\+(\d+)m?s\s+(.*)"
)

# Events to skip (too noisy)
SKIP_EVENTS = {
    "file.watcher.updated",
    "message.part.delta",
    "message.part.updated",
}

SIGNIFICANT_BUS_EVENTS = {
    "session.error",
    "session.diff",
    "session.compacted",
    "command.executed",
}


def parse_log_line(line):
    # type: (str) -> Optional[LogEvent]
    """Parse a single log line into a LogEvent, or None if irrelevant."""
    m = _LOG_RE.match(line.rstrip())
    if not m:
        return None

    time_str = m.group(3)  # HH:MM:SS
    rest = m.group(5)

    # Parse key=value pairs from the rest of the line
    fields = {}  # type: dict
    for token in rest.split():
        if "=" in token:
            k, v = token.split("=", 1)
            fields[k] = v

    service = fields.get("service", "")

    # VCS branch events: service=vcs from=X to=Y branch changed
    if service == "vcs":
        from_branch = fields.get("from", "")
        to_branch = fields.get("to", "")
        if from_branch and to_branch:
            return LogEvent(
                time_str=time_str,
                event_type="vcs.branch",
                fields={"from": from_branch, "to": to_branch},
            )
        return None

    # Bus events: service=bus type=X publishing
    if service == "bus" and "publishing" in rest:
        event_type = fields.get("type", "")
        if not event_type or event_type in SKIP_EVENTS:
            return None
        if event_type not in SIGNIFICANT_BUS_EVENTS:
            return None
        if event_type == "command.executed":
            command = ""
            command_match = re.search(r'\bcommand="([^"]+)"', rest)
            if command_match:
                command = command_match.group(1)
            else:
                command_match = re.search(r"\bcommand=([^\s]+)", rest)
                if command_match:
                    command = command_match.group(1)
            if not command:
                cmd_match = re.search(r'\bcmd="([^"]+)"', rest)
                if cmd_match:
                    command = cmd_match.group(1)
                else:
                    cmd_match = re.search(r"\bcmd=([^\s]+)", rest)
                    if cmd_match:
                        command = cmd_match.group(1)
            if command:
                fields["command"] = command
        return LogEvent(
            time_str=time_str,
            event_type=event_type,
            fields=fields,
        )

    return None
