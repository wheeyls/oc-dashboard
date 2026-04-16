import json
import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_ENV_PATH = os.path.expanduser("~/.config/oc-dashboard/.env")
_dotenv_cache = None  # type: Optional[Dict[str, str]]


def _load_dotenv():
    # type: () -> Dict[str, str]
    """Load optional ~/.config/oc-dashboard/.env. Returns cached result."""
    global _dotenv_cache
    if _dotenv_cache is not None:
        return _dotenv_cache
    env = {}  # type: Dict[str, str]
    try:
        with open(_ENV_PATH, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                if key:
                    env[key] = value
    except FileNotFoundError:
        pass
    _dotenv_cache = env
    return env


def opencode_env():
    # type: () -> Dict[str, str]
    """Return os.environ merged with .env overrides for subprocess use."""
    extra = _load_dotenv()
    if not extra:
        return dict(os.environ)
    merged = dict(os.environ)
    merged.update(extra)
    return merged


def opencode_env_prefix():
    # type: () -> str
    """Return 'KEY=val KEY2=val2 ' prefix for shell command strings."""
    extra = _load_dotenv()
    if not extra:
        return ""
    return " ".join("%s=%s" % (k, v) for k, v in extra.items()) + " "


@dataclass
class SessionInfo:
    id: str
    title: str
    updated_ms: int = 0
    parent_id: Optional[str] = None
    directory: str = ""

    @classmethod
    def from_api(cls, data):
        # type: (Dict[str, Any]) -> SessionInfo
        time_info = data.get("time", {})
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            updated_ms=int(time_info.get("updated", 0)),
            parent_id=data.get("parentID"),
            directory=str(data.get("directory", "")),
        )


@dataclass
class TodoInfo:
    id: str
    status: str
    content: str

    @classmethod
    def from_api(cls, data):
        # type: (Dict[str, Any]) -> TodoInfo
        return cls(
            id=str(data.get("id", "")),
            status=str(data.get("status", "pending")),
            content=str(data.get("content", "")),
        )


@dataclass
class ChildSession:
    id: str
    title: str
    updated_ms: int = 0
    message_count: int = 0

    @classmethod
    def from_api(cls, data):
        # type: (Dict[str, Any]) -> ChildSession
        time_info = data.get("time", {})
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            updated_ms=int(time_info.get("updated", 0)),
        )


class OpenCodeClient(ABC):
    """Interface for interacting with an OpenCode server."""

    @abstractmethod
    def healthy(self):
        # type: () -> bool
        ...

    @abstractmethod
    def list_sessions(self, limit=30):
        # type: (int) -> List[SessionInfo]
        ...

    @abstractmethod
    def create_session(self, title=None):
        # type: (Optional[str]) -> Optional[SessionInfo]
        ...

    @abstractmethod
    def get_session_todos(self, session_id):
        # type: (str) -> List[TodoInfo]
        ...

    @abstractmethod
    def get_session_children(self, session_id):
        # type: (str) -> List[ChildSession]
        ...

    @abstractmethod
    def send_message_async(self, session_id, text):
        # type: (str, str) -> bool
        ...

    @abstractmethod
    def select_session_in_tui(self, session_id):
        # type: (str) -> bool
        ...

    @abstractmethod
    def get_project_path(self):
        # type: () -> Optional[str]
        ...


_JSON_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}

_DEFAULT_PORT = 4096
_SERVE_STARTUP_TIMEOUT = 8


def _find_server_url():
    # type: () -> Optional[str]
    port = os.environ.get("OPENCODE_PORT", str(_DEFAULT_PORT))
    url = "http://127.0.0.1:%s" % port
    try:
        import urllib.request

        req = urllib.request.Request(url + "/global/health", headers=_JSON_HEADERS)
        resp = urllib.request.urlopen(req, timeout=2)
        data = json.loads(resp.read())
        if data.get("healthy"):
            return url
    except Exception:
        pass
    return None


def _start_server(port=None):
    # type: (Optional[int]) -> Optional[str]
    use_port = port or _DEFAULT_PORT
    cmd = ["opencode", "serve", "--port", str(use_port)]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=opencode_env(),
        )
    except Exception:
        return None
    url = "http://127.0.0.1:%d" % use_port
    deadline = time.monotonic() + _SERVE_STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.3)
        try:
            import urllib.request

            req = urllib.request.Request(url + "/global/health", headers=_JSON_HEADERS)
            resp = urllib.request.urlopen(req, timeout=2)
            data = json.loads(resp.read())
            if data.get("healthy"):
                return url
        except Exception:
            continue
    return None


def ensure_server():
    # type: () -> Optional[str]
    url = _find_server_url()
    if url:
        return url
    return _start_server()


class HttpOpenCodeClient(OpenCodeClient):
    def __init__(self, base_url, directory=None):
        # type: (str, Optional[str]) -> None
        self._base_url = base_url.rstrip("/")
        self._directory = directory

    def _request(self, method, path, body=None):
        # type: (str, str, Optional[Dict[str, Any]]) -> Any
        import urllib.request

        url = self._base_url + path
        headers = dict(_JSON_HEADERS)
        if self._directory:
            headers["x-opencode-directory"] = self._directory
        data_bytes = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data_bytes, headers=headers, method=method
        )
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            content = resp.read()
            if not content:
                return None
            return json.loads(content)
        except Exception:
            return None

    def _get(self, path):
        # type: (str) -> Any
        return self._request("GET", path)

    def _post(self, path, body=None):
        # type: (str, Optional[Dict[str, Any]]) -> Any
        return self._request("POST", path, body or {})

    def healthy(self):
        # type: () -> bool
        data = self._get("/global/health")
        return bool(data and data.get("healthy"))

    def list_sessions(self, limit=30):
        # type: (int) -> List[SessionInfo]
        data = self._get("/session?limit=%d&roots=true" % limit)
        if not isinstance(data, list):
            return []
        return [SessionInfo.from_api(item) for item in data]

    def create_session(self, title=None):
        # type: (Optional[str]) -> Optional[SessionInfo]
        body = {}  # type: Dict[str, Any]
        if title:
            body["title"] = title
        data = self._post("/session", body)
        if data and data.get("id"):
            return SessionInfo.from_api(data)
        return None

    def get_session_todos(self, session_id):
        # type: (str) -> List[TodoInfo]
        data = self._get("/session/%s/todo" % session_id)
        if not isinstance(data, list):
            return []
        return [TodoInfo.from_api(item) for item in data]

    def get_session_children(self, session_id):
        # type: (str) -> List[ChildSession]
        data = self._get("/session/%s/children" % session_id)
        if not isinstance(data, list):
            return []
        return [ChildSession.from_api(item) for item in data]

    def send_message_async(self, session_id, text):
        # type: (str, str) -> bool
        body = {"parts": [{"type": "text", "text": text}]}
        self._post("/session/%s/prompt_async" % session_id, body)
        return True

    def select_session_in_tui(self, session_id):
        # type: (str) -> bool
        result = self._post("/tui/select-session", {"sessionID": session_id})
        return bool(result)

    def get_project_path(self):
        # type: () -> Optional[str]
        data = self._get("/project/current")
        if data:
            return data.get("worktree")
        return None
