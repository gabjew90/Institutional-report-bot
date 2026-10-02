"""scripts/session_sync.py: the local/cloud sync hooks, on throwaway repos."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "session_sync", Path(__file__).resolve().parents[1] / "scripts" / "session_sync.py")
ss = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ss)


def sh(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """A bare origin with one commit on main, and a helper to clone it."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, capture_output=True)
    for repo_cfg in (("user.email", "t@t"), ("user.name", "t")):
        sh(seed, "config", *repo_cfg)
    (seed / "a.md").write_text("one\n")
    sh(seed, "add", "-A")
    sh(seed, "commit", "-q", "-m", "one")
    sh(seed, "push", "-q", "origin", "main")

    def clone(name):
        d = tmp_path / name
        subprocess.run(["git", "clone", "-q", str(bare), str(d)], check=True, capture_output=True)
        sh(d, "config", "user.email", "t@t")
        sh(d, "config", "user.name", "t")
        return d
    return clone


def test_throwaway_branch_moves_to_target(origin):
    repo = origin("attached")
    sh(repo, "checkout", "-q", "-b", "claude/admiring-x")
    notes = ss.sync_to(repo, "main", "memory")
    assert sh(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert any("moved from claude/admiring-x" in n for n in notes)


def test_branch_with_its_own_work_is_left_alone(origin):
    repo = origin("busy")
    sh(repo, "checkout", "-q", "-b", "claude/busy")
    (repo / "b.md").write_text("only here\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "local only")
    notes = ss.sync_to(repo, "main", "memory")
    assert sh(repo, "rev-parse", "--abbrev-ref", "HEAD") == "claude/busy"
    assert any("left as is" in n for n in notes)


def test_unpushed_target_branch_is_never_reset(origin):
    repo = origin("deploy")
    (repo / "d.md").write_text("unpushed deploy work\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "unpushed on main")
    keep = sh(repo, "rev-parse", "main")
    sh(repo, "checkout", "-q", "-b", "claude/elsewhere", "origin/main")
    notes = ss.sync_to(repo, "main", "code")
    assert sh(repo, "rev-parse", "main") == keep
    assert sh(repo, "rev-parse", "--abbrev-ref", "HEAD") == "claude/elsewhere"
    assert any("commits not on origin" in n for n in notes)


def test_up_to_date_is_quiet_and_unpushed_is_reported(origin):
    repo = origin("plain")
    assert ss.sync_to(repo, "main", "code") == []
    (repo / "c.md").write_text("new\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "unpushed")
    assert any("1 local commit(s) not pushed" in n for n in ss.sync_to(repo, "main", "code"))


def test_stop_pushes_memory_and_rebases_over_the_other_side(origin, monkeypatch):
    here, there = origin("here"), origin("there")
    # the other session pushes a memory first
    (there / "other.md").write_text("from cloud\n")
    sh(there, "add", "-A")
    sh(there, "commit", "-q", "-m", "cloud memory")
    sh(there, "push", "-q", "origin", "main")
    # this session writes one and its reply ends
    (here / "mine.md").write_text("from local\n")
    monkeypatch.setattr(ss, "memory_dir", lambda project: here)
    ss.stop()
    sh(there, "pull", "-q", "origin", "main")
    assert (there / "mine.md").exists() and (there / "other.md").exists()
    assert sh(here, "status", "--porcelain") == ""


def test_stop_with_nothing_changed_makes_no_commit(origin, monkeypatch):
    here = origin("idle")
    before = sh(here, "rev-parse", "HEAD")
    monkeypatch.setattr(ss, "memory_dir", lambda project: here)
    ss.stop()
    assert sh(here, "rev-parse", "HEAD") == before


def test_start_outside_the_repo_points_at_its_claude_md(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ss, "sync_to", lambda *a: [])
    monkeypatch.chdir(tmp_path)
    ss.start()
    out = capsys.readouterr().out
    assert "not the repo" in out and "CLAUDE.md" in out


def test_start_inside_the_repo_and_in_sync_is_silent(monkeypatch, capsys):
    monkeypatch.setattr(ss, "sync_to", lambda *a: [])
    monkeypatch.chdir(ss.project_dir())
    ss.start()
    assert capsys.readouterr().out == ""


def test_memory_dir_matches_claude_code_naming():
    assert ss.memory_dir(Path("/home/user/Institutional-report-bot")).parts[-2] == "-home-user-Institutional-report-bot"
