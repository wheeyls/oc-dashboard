import json
from unittest.mock import MagicMock, patch

import pytest

from oc_dashboard.opencode import (
    ChildSession,
    HttpOpenCodeClient,
    SessionInfo,
    TodoInfo,
)


def _mock_response(data, status=200):
    resp = MagicMock()
    resp.status = status
    if data is None:
        resp.read.return_value = b""
    else:
        resp.read.return_value = json.dumps(data).encode()
    return resp


class TestSessionInfoFromApi:
    def test_full_data(self):
        info = SessionInfo.from_api(
            {
                "id": "ses_abc",
                "title": "My Session",
                "time": {"created": 1000, "updated": 2000},
                "parentID": "ses_parent",
                "directory": "/tmp/proj",
            }
        )
        assert info.id == "ses_abc"
        assert info.title == "My Session"
        assert info.updated_ms == 2000
        assert info.parent_id == "ses_parent"
        assert info.directory == "/tmp/proj"

    def test_minimal_data(self):
        info = SessionInfo.from_api({})
        assert info.id == ""
        assert info.title == ""
        assert info.updated_ms == 0
        assert info.parent_id is None


class TestTodoInfoFromApi:
    def test_full_data(self):
        todo = TodoInfo.from_api(
            {
                "id": "todo_1",
                "status": "completed",
                "content": "Fix bug",
            }
        )
        assert todo.id == "todo_1"
        assert todo.status == "completed"
        assert todo.content == "Fix bug"

    def test_defaults(self):
        todo = TodoInfo.from_api({})
        assert todo.status == "pending"
        assert todo.content == ""


class TestChildSessionFromApi:
    def test_full_data(self):
        child = ChildSession.from_api(
            {
                "id": "ses_child",
                "title": "Worker",
                "time": {"updated": 5000},
            }
        )
        assert child.id == "ses_child"
        assert child.title == "Worker"
        assert child.updated_ms == 5000


class TestHttpOpenCodeClient:
    @patch("urllib.request.urlopen")
    def test_healthy_true(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"healthy": True})
        client = HttpOpenCodeClient("http://127.0.0.1:4096")
        assert client.healthy()

    @patch("urllib.request.urlopen")
    def test_healthy_false_on_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("connection refused")
        client = HttpOpenCodeClient("http://127.0.0.1:4096")
        assert not client.healthy()

    @patch("urllib.request.urlopen")
    def test_list_sessions(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            [
                {"id": "ses_1", "title": "A", "time": {"updated": 100}},
                {"id": "ses_2", "title": "B", "time": {"updated": 200}},
            ]
        )
        client = HttpOpenCodeClient("http://127.0.0.1:4096")
        sessions = client.list_sessions(limit=10)
        assert len(sessions) == 2
        assert sessions[0].id == "ses_1"
        assert sessions[1].title == "B"

    @patch("urllib.request.urlopen")
    def test_list_sessions_non_list_returns_empty(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"error": "bad"})
        client = HttpOpenCodeClient("http://127.0.0.1:4096")
        assert client.list_sessions() == []

    @patch("urllib.request.urlopen")
    def test_create_session(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            {
                "id": "ses_new",
                "title": "Created",
                "time": {"created": 1000, "updated": 1000},
            }
        )
        client = HttpOpenCodeClient("http://127.0.0.1:4096")
        session = client.create_session(title="Created")
        assert session is not None
        assert session.id == "ses_new"

    @patch("urllib.request.urlopen")
    def test_create_session_failure(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({})
        client = HttpOpenCodeClient("http://127.0.0.1:4096")
        assert client.create_session() is None

    @patch("urllib.request.urlopen")
    def test_get_session_todos(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            [
                {"id": "t1", "status": "completed", "content": "Done"},
                {"id": "t2", "status": "pending", "content": "Todo"},
            ]
        )
        client = HttpOpenCodeClient("http://127.0.0.1:4096")
        todos = client.get_session_todos("ses_1")
        assert len(todos) == 2
        assert todos[0].status == "completed"

    @patch("urllib.request.urlopen")
    def test_get_session_children(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            [
                {"id": "ses_c1", "title": "Worker 1", "time": {"updated": 100}},
            ]
        )
        client = HttpOpenCodeClient("http://127.0.0.1:4096")
        children = client.get_session_children("ses_1")
        assert len(children) == 1
        assert children[0].title == "Worker 1"

    @patch("urllib.request.urlopen")
    def test_send_message_async(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(None)
        client = HttpOpenCodeClient("http://127.0.0.1:4096")
        assert client.send_message_async("ses_1", "Hello")

    @patch("urllib.request.urlopen")
    def test_get_project_path(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"worktree": "/home/user/proj"})
        client = HttpOpenCodeClient("http://127.0.0.1:4096")
        assert client.get_project_path() == "/home/user/proj"

    @patch("urllib.request.urlopen")
    def test_get_project_path_none(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(None)
        client = HttpOpenCodeClient("http://127.0.0.1:4096")
        assert client.get_project_path() is None

    @patch("urllib.request.urlopen")
    def test_directory_header_sent(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"healthy": True})
        client = HttpOpenCodeClient("http://127.0.0.1:4096", directory="/my/proj")
        client.healthy()
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("X-opencode-directory") == "/my/proj"

    @patch("urllib.request.urlopen")
    def test_select_session_in_tui(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"ok": True})
        client = HttpOpenCodeClient("http://127.0.0.1:4096")
        assert client.select_session_in_tui("ses_1")
