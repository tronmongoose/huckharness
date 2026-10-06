"""Git panel routes over a real server rooted in a temp git repository."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from unittest import mock

import pytest

from coding_harness.core.paths import meta_dir
from coding_harness.modes import serve_git, serve_mode, ui_mode

_USAGE = {"tokens_in": 0, "tokens_out": 0, "thinking_tokens": None}


def _request(method, url, body=None, timeout=10):
    """(status, parsed JSON) for one call."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _git(repo, *args):
    """stdout of one git call in ``repo``."""
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A repo with one commit, a dirty tracked file and two untracked files; cwd is it."""
    root = tmp_path / "repo"
    root.mkdir()
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for key, value in (("user.name", "t"), ("user.email", "t@example.com"),
                       ("commit.gpgsign", "false"), ("core.hooksPath", str(hooks))):
        _git(root, "config", key, value)
    (root / "tracked.py").write_text("a = 1\n")
    _git(root, "add", "--", "tracked.py")
    _git(root, "commit", "-q", "-m", "init", "--", "tracked.py")
    (root / "tracked.py").write_text("a = 2\n")
    (root / "one.py").write_text("x\n")
    (root / "two.py").write_text("y\n")
    monkeypatch.chdir(root)
    return root


class _Gate:
    """Fake ollama.chat that holds every call until ``release`` is set."""

    def __init__(self):
        self.release = threading.Event()

    def __call__(self, **_kw):
        self.release.wait(5)
        return {"role": "assistant", "content": "done", "tool_calls": [], "_usage": _USAGE}


@pytest.fixture()
def server(repo):
    """(base url, repo, gate) with Sentinel stubbed and MCP off."""
    gate = _Gate()
    verdict = mock.MagicMock(allowed=True, reason="test", path="hook")
    started = threading.Event()
    box = {}

    def _ready(srv):
        box["srv"] = srv
        started.set()

    with mock.patch("coding_harness.core.session.ollama.chat", side_effect=gate), \
            mock.patch("coding_harness.tools.registry.sentinel.review", return_value=verdict):
        t = threading.Thread(target=serve_mode.run, daemon=True, kwargs=dict(
            host="127.0.0.1", port=0, enable_mcp=False, ready_callback=_ready))
        t.start()
        assert started.wait(5)
        srv = box["srv"]
        yield f"http://127.0.0.1:{srv.server_port}", repo, gate
        gate.release.set()
        srv.shutdown()
        srv.server_close()
        t.join(2)


def _session(base, autonomy):
    """An interactive session id at ``autonomy``."""
    status, body = _request("POST", f"{base}/v1/sessions",
                            {"autonomy": autonomy, "interactive": True})
    assert status == 201
    return body["session_id"]


def _commit(base, sid, paths, message="panel commit\n\nbody"):
    """POST a commit and return (status, body)."""
    return _request("POST", f"{base}/v1/git/commit",
                    {"session_id": sid, "message": message, "paths": paths})


def test_status_route_reports_branch_and_lists(server):
    base, _, _ = server
    status, body = _request("GET", f"{base}/v1/git")
    assert status == 200 and body["branch"] == "main" and not body["detached"]
    assert [r["path"] for r in body["unstaged"]] == ["tracked.py"]
    assert sorted(r["path"] for r in body["untracked"]) == ["one.py", "two.py"]


def test_commit_at_medium_stages_only_listed_paths(server):
    base, repo, _ = server
    sid = _session(base, "medium")
    status, body = _commit(base, sid, ["one.py", "tracked.py"])
    assert status == 200, body
    assert body["sha"] == _git(repo, "rev-parse", "HEAD").strip()
    assert body["message_first_line"] == "panel commit"
    assert sorted(_git(repo, "show", "--name-only", "--format=", "HEAD").split()) == \
        ["one.py", "tracked.py"]
    assert "?? two.py" in _git(repo, "status", "--porcelain")
    assert not list((meta_dir() / "git").glob("commit-*.txt"))


def test_commit_at_low_parks_and_deny_leaves_repo_unchanged(server):
    base, repo, _ = server
    sid = _session(base, "low")
    head = _git(repo, "rev-parse", "HEAD")
    before = _git(repo, "status", "--porcelain")
    box = {}
    t = threading.Thread(target=lambda: box.update(res=_commit(base, sid, ["one.py"])))
    t.start()
    pending = []
    for _ in range(200):
        _, got = _request("GET", f"{base}/v1/sessions/{sid}/permissions")
        pending = got.get("pending") or []
        if pending:
            break
        time.sleep(0.02)
    assert pending and pending[0]["tool"] == "GitCommit"
    assert "commit -F" in pending[0]["args_preview"]["command"]
    req_id = pending[0]["req_id"]
    _request("POST", f"{base}/v1/sessions/{sid}/permissions/{req_id}", {"decision": "deny"})
    t.join(5)
    assert box["res"][0] == 403
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain") == before


@pytest.mark.parametrize("path", ["../outside.py", "/etc/hosts", ".git/config", "missing.py"])
def test_commit_rejects_bad_paths(server, path):
    base, _, _ = server
    sid = _session(base, "medium")
    assert _commit(base, sid, [path])[0] == 400


def test_commit_needs_an_interactive_session(server):
    base, _, _ = server
    _, body = _request("POST", f"{base}/v1/sessions", {"autonomy": "medium"})
    assert _commit(base, body["session_id"], ["one.py"])[0] == 400


def test_commit_refused_mid_turn(server):
    base, repo, gate = server
    sid = _session(base, "medium")
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/turn", {"message": "hi"})
    assert status in (200, 202)
    assert _commit(base, sid, ["one.py"])[0] == 409
    gate.release.set()
    assert "?? one.py" in _git(repo, "status", "--porcelain")


def test_worktree_create_list_and_bad_topic(server):
    base, repo, _ = server
    assert _request("POST", f"{base}/v1/worktrees", {"topic": "../escape"})[0] == 400
    assert _request("POST", f"{base}/v1/worktrees", {"topic": "-x"})[0] == 400
    status, body = _request("POST", f"{base}/v1/worktrees", {"topic": "feat"})
    assert status == 201 and body["branch"] == "feat"
    assert os.path.basename(body["path"]) == "repo-feat"
    assert _request("POST", f"{base}/v1/worktrees", {"topic": "feat"})[0] == 409
    _, listed = _request("GET", f"{base}/v1/worktrees")
    rows = listed["worktrees"]
    assert rows[0]["is_main"] and rows[0]["current"]
    assert any(r["branch"] == "feat" and not r["current"] for r in rows)


def test_worktree_open_only_accepts_listed_paths(server, monkeypatch):
    base, repo, _ = server
    spawned = []
    monkeypatch.setattr(ui_mode, "spawn",
                        lambda cwd, **_kw: spawned.append(cwd) or "http://127.0.0.1:1")
    assert _request("POST", f"{base}/v1/worktrees/open", {"path": str(repo.parent)})[0] == 400
    _, made = _request("POST", f"{base}/v1/worktrees", {"topic": "side"})
    _, listed = _request("GET", f"{base}/v1/worktrees")
    path = next(r["path"] for r in listed["worktrees"] if r["branch"] == "side")
    status, body = _request("POST", f"{base}/v1/worktrees/open", {"path": path})
    assert status == 200 and body["origin"] == "http://127.0.0.1:1"
    assert spawned == [os.path.realpath(path)] and os.path.realpath(made["path"]) == spawned[0]


def test_commit_refuses_a_directory(server):
    base, repo, _ = server
    (repo / "sub").mkdir()
    (repo / "sub" / "a.py").write_text("a\n")
    (repo / "sub" / "b.py").write_text("b\n")
    head = _git(repo, "rev-parse", "HEAD")
    sid = _session(base, "medium")
    status, body = _commit(base, sid, ["sub"])
    assert status == 400 and "director" in body["error"]["message"]
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_commit_refuses_a_path_status_does_not_list(server):
    base, repo, _ = server
    (repo / "clean.py").write_text("c\n")
    _git(repo, "add", "--", "clean.py")
    _git(repo, "commit", "-q", "-m", "clean", "--", "clean.py")
    sid = _session(base, "medium")
    status, body = _commit(base, sid, ["clean.py"])
    assert status == 400 and "not a changed file" in body["error"]["message"]


def _parked(base, sid):
    """The first pending permission of ``sid`` once one appears."""
    for _ in range(200):
        _, got = _request("GET", f"{base}/v1/sessions/{sid}/permissions")
        if got.get("pending"):
            return got["pending"][0]
        time.sleep(0.02)
    raise AssertionError("nothing parked")


def test_turn_refused_while_a_commit_is_parked(server):
    base, repo, _ = server
    sid = _session(base, "low")
    box = {}
    t = threading.Thread(target=lambda: box.update(res=_commit(base, sid, ["one.py"])))
    t.start()
    req = _parked(base, sid)
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/turn", {"message": "hi"})
    assert status == 409
    _request("POST", f"{base}/v1/sessions/{sid}/permissions/{req['req_id']}",
             {"decision": "allow_once"})
    t.join(5)
    assert box["res"][0] == 200
    assert "one.py" in _git(repo, "show", "--name-only", "--format=", "HEAD")


def test_failed_commit_restores_the_index(server, tmp_path):
    base, repo, _ = server
    hook = tmp_path / "no-hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    _git(repo, "add", "--", "tracked.py")
    (repo / "tracked.py").write_text("a = 3\n")
    index = _git(repo, "ls-files", "-s")
    before = _git(repo, "status", "--porcelain")
    head = _git(repo, "rev-parse", "HEAD")
    sid = _session(base, "medium")
    status, _ = _commit(base, sid, ["tracked.py", "one.py"])
    assert status == 500
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "ls-files", "-s") == index
    assert _git(repo, "status", "--porcelain") == before


def test_message_file_is_owner_only(server, monkeypatch):
    base, _, _ = server
    real = serve_git.gitinfo.run_git
    modes = []

    def _spy(args, cwd, *a, **kw):
        if "commit" in args:
            modes.append(os.stat(args[args.index("-F") + 1]).st_mode & 0o777)
        return real(args, cwd, *a, **kw)

    monkeypatch.setattr(serve_git.gitinfo, "run_git", _spy)
    sid = _session(base, "medium")
    assert _commit(base, sid, ["one.py"])[0] == 200
    assert modes == [0o600]


def test_staged_rename_commits_both_sides(server):
    base, repo, _ = server
    _git(repo, "mv", "tracked.py", "moved.py")
    sid = _session(base, "medium")
    status, body = _commit(base, sid, ["moved.py", "tracked.py"])
    assert status == 200, body
    tree = _git(repo, "ls-tree", "--name-only", "HEAD").split()
    assert "moved.py" in tree and "tracked.py" not in tree


def test_dot_git_in_any_case_is_refused(server):
    base, _, _ = server
    sid = _session(base, "medium")
    assert _commit(base, sid, [".GIT/config"])[0] == 400
