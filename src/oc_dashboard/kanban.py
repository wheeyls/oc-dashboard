"""Kanban adapter layer.

Thin abstraction over a Kanban backend.  The *only* concrete implementation
shipped today is ``LocalJsonKanban`` which stores everything in a single JSON
file.  The interface is deliberately small so that a JIRA-MCP or Trello-MCP
adapter can be dropped in later without touching the rest of the dashboard.

Stages (ordered): pending -> in_progress -> done
"""

import json
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# ── Kanban stages (in board order) ────────────────────────

STAGES = ["pending", "in_progress", "done"]

STAGE_LABELS = {
    "pending": "Pending",
    "in_progress": "In Progress",
    "done": "Done",
}


# ── Data models ───────────────────────────────────────────


@dataclass
class KanbanProject:
    id: str
    title: str
    description: str
    stage: str  # one of STAGES
    created_at: str  # ISO-8601
    updated_at: str  # ISO-8601
    session_ids: List[str] = field(default_factory=list)
    pr_numbers: List[int] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "stage": self.stage,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "session_ids": list(self.session_ids),
            "pr_numbers": list(self.pr_numbers),
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, d):
        # type: (Dict[str, Any]) -> KanbanProject
        return cls(
            id=str(d.get("id", "")),
            title=str(d.get("title", "")),
            description=str(d.get("description", "")),
            stage=str(d.get("stage", "pending")),
            created_at=str(d.get("created_at", "")),
            updated_at=str(d.get("updated_at", "")),
            session_ids=list(d.get("session_ids", [])),
            pr_numbers=list(d.get("pr_numbers", [])),
            tags=list(d.get("tags", [])),
        )


# ── Abstract adapter ─────────────────────────────────────


class KanbanAdapter(ABC):
    """Interface that any Kanban backend must implement.

    Keep this surface tiny — only what the dashboard actually needs.
    A JIRA or Trello MCP adapter would subclass this.
    """

    @abstractmethod
    def list_projects(self):
        # type: () -> List[KanbanProject]
        """Return all projects, any stage."""
        ...

    @abstractmethod
    def get_project(self, project_id):
        # type: (str) -> Optional[KanbanProject]
        ...

    @abstractmethod
    def create_project(self, title, description="", stage="pending", tags=None):
        # type: (str, str, str, Optional[List[str]]) -> KanbanProject
        ...

    @abstractmethod
    def update_project(self, project_id, **kwargs):
        # type: (str, **Any) -> Optional[KanbanProject]
        """Update any field(s): title, description, stage, tags."""
        ...

    @abstractmethod
    def move_project(self, project_id, stage):
        # type: (str, str) -> Optional[KanbanProject]
        """Move a project to a new stage."""
        ...

    @abstractmethod
    def delete_project(self, project_id):
        # type: (str) -> bool
        ...

    @abstractmethod
    def link_session(self, project_id, session_id):
        # type: (str, str) -> bool
        """Associate an OpenCode session with a project."""
        ...

    @abstractmethod
    def unlink_session(self, project_id, session_id):
        # type: (str, str) -> bool
        ...

    @abstractmethod
    def link_pr(self, project_id, pr_number):
        # type: (str, int) -> bool
        """Associate a PR with a project."""
        ...

    @abstractmethod
    def unlink_pr(self, project_id, pr_number):
        # type: (str, int) -> bool
        ...


# ── Local JSON-file implementation ───────────────────────

_DEFAULT_PATH = os.path.expanduser("~/.local/share/oc-dashboard/kanban.json")


def _now_iso():
    # type: () -> str
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class LocalJsonKanban(KanbanAdapter):
    """Dead-simple JSON-file Kanban.  One file, atomic read/write."""

    def __init__(self, path=None):
        # type: (Optional[str]) -> None
        self._path = path or _DEFAULT_PATH
        self._projects = {}  # type: Dict[str, KanbanProject]
        self._load()

    # ── persistence ───────────────────────────────────────

    def _load(self):
        # type: () -> None
        if not os.path.exists(self._path):
            self._projects = {}
            return
        try:
            with open(self._path, "r") as fh:
                data = json.load(fh)
            projects = data.get("projects", [])
            self._projects = {}
            for d in projects:
                p = KanbanProject.from_dict(d)
                self._projects[p.id] = p
        except Exception:
            self._projects = {}

    def _save(self):
        # type: () -> None
        parent = os.path.dirname(self._path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        payload = {
            "version": 1,
            "projects": [p.to_dict() for p in self._projects.values()],
        }
        tmp = self._path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self._path)

    # ── adapter implementation ────────────────────────────

    def list_projects(self):
        # type: () -> List[KanbanProject]
        self._load()  # re-read for freshness
        return list(self._projects.values())

    def get_project(self, project_id):
        # type: (str) -> Optional[KanbanProject]
        self._load()
        return self._projects.get(project_id)

    def create_project(self, title, description="", stage="pending", tags=None):
        # type: (str, str, str, Optional[List[str]]) -> KanbanProject
        now = _now_iso()
        project = KanbanProject(
            id=str(uuid.uuid4())[:8],
            title=title,
            description=description,
            stage=stage if stage in STAGES else "pending",
            created_at=now,
            updated_at=now,
            tags=tags or [],
        )
        self._projects[project.id] = project
        self._save()
        return project

    def update_project(self, project_id, **kwargs):
        # type: (str, **Any) -> Optional[KanbanProject]
        p = self._projects.get(project_id)
        if not p:
            return None
        for key in ("title", "description", "tags"):
            if key in kwargs:
                setattr(p, key, kwargs[key])
        if "stage" in kwargs and kwargs["stage"] in STAGES:
            p.stage = kwargs["stage"]
        p.updated_at = _now_iso()
        self._save()
        return p

    def move_project(self, project_id, stage):
        # type: (str, str) -> Optional[KanbanProject]
        if stage not in STAGES:
            return None
        return self.update_project(project_id, stage=stage)

    def delete_project(self, project_id):
        # type: (str) -> bool
        if project_id in self._projects:
            del self._projects[project_id]
            self._save()
            return True
        return False

    def link_session(self, project_id, session_id):
        # type: (str, str) -> bool
        p = self._projects.get(project_id)
        if not p:
            return False
        if session_id not in p.session_ids:
            p.session_ids.append(session_id)
            p.updated_at = _now_iso()
            self._save()
        return True

    def unlink_session(self, project_id, session_id):
        # type: (str, str) -> bool
        p = self._projects.get(project_id)
        if not p:
            return False
        if session_id in p.session_ids:
            p.session_ids.remove(session_id)
            p.updated_at = _now_iso()
            self._save()
        return True

    def link_pr(self, project_id, pr_number):
        # type: (str, int) -> bool
        p = self._projects.get(project_id)
        if not p:
            return False
        if pr_number not in p.pr_numbers:
            p.pr_numbers.append(pr_number)
            p.updated_at = _now_iso()
            self._save()
        return True

    def unlink_pr(self, project_id, pr_number):
        # type: (str, int) -> bool
        p = self._projects.get(project_id)
        if not p:
            return False
        if pr_number in p.pr_numbers:
            p.pr_numbers.remove(pr_number)
            p.updated_at = _now_iso()
            self._save()
        return True
