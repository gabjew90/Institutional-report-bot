"""Audit fix 2 (2026-11-23): the rules about production access are code.

Three parts: db.get_connection() refuses a second process on a database the
worker owns, scripts/db_probe.py is the read-only way in, and a command hook
blocks the pattern-matching kill on the worker."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import db
from config import settings

REPO = Path(__file__).resolve().parents[1]


# ── the owner lock ──────────────────────────────────────────────────────────

@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "reports.db"))
    # Set then delete, so monkeypatch records the variable and puts back its old
    # state after a test in which claim_owner() wrote it straight to os.environ.
    monkeypatch.setenv(db._FAMILY_ENV, "x")
    monkeypatch.delenv(db._FAMILY_ENV)
    monkeypatch.setattr(db, "_owner_lock_fh", None)
    db.reset_connections()
    yield tmp_path / "reports.db"
    db.reset_connections()
    fh = db._owner_lock_fh
    if fh is not None:
        fh.close()


def _hold_owner_lock(path: Path):
    """Another open file description holding the lock: what a running worker looks like."""
    fcntl = pytest.importorskip("fcntl")
    fh = open(str(path) + ".owner", "a+")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


def test_with_no_owner_a_process_opens_the_database_as_before(fresh_db):
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM pdf_files").fetchone()[0] == 0


def test_a_stranger_is_refused_while_the_worker_holds_the_lock(fresh_db):
    held = _hold_owner_lock(fresh_db)
    try:
        with pytest.raises(db.LiveDatabaseError) as e:
            db.get_connection()
        assert "db_probe.py" in str(e.value) and "assume_schema_ready" in str(e.value)
        # Nothing was opened: the file the schema pass would have created is absent,
        # and a second call is refused again instead of handing back a connection.
        assert not fresh_db.exists()
        with pytest.raises(db.LiveDatabaseError):
            db.get_connection()
    finally:
        held.close()


def test_a_stranger_is_refused_on_a_worker_thread_too(fresh_db):
    import threading
    held = _hold_owner_lock(fresh_db)
    seen = []

    def work():
        try:
            db.get_connection()
            seen.append("opened")
        except db.LiveDatabaseError:
            seen.append("refused")
    try:
        t = threading.Thread(target=work)
        t.start()
        t.join(10)
    finally:
        held.close()
    assert seen == ["refused"]


def test_a_child_of_the_worker_is_not_a_stranger(fresh_db, monkeypatch):
    held = _hold_owner_lock(fresh_db)
    try:
        monkeypatch.setenv(db._FAMILY_ENV, "1")
        assert db.get_connection() is not None
    finally:
        held.close()


def test_a_deliberate_second_writer_skips_the_schema_pass(fresh_db):
    # The worker built the schema. The second process says so and gets a connection.
    db.get_connection()
    db.reset_connections()
    held = _hold_owner_lock(fresh_db)
    try:
        db.assume_schema_ready()
        conn = db.get_connection()
        assert conn.execute("SELECT COUNT(*) FROM pdf_files").fetchone()[0] == 0
    finally:
        held.close()


def test_the_lock_is_released_when_its_holder_goes(fresh_db):
    held = _hold_owner_lock(fresh_db)
    held.close()
    assert db.get_connection() is not None


def test_claim_owner_marks_the_process_and_holds_the_lock(fresh_db):
    fcntl = pytest.importorskip("fcntl")
    assert db.claim_owner() is True
    assert os.environ.get(db._FAMILY_ENV) == str(os.getpid())
    with open(str(fresh_db) + ".owner", "a+") as other:
        with pytest.raises(OSError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # The owner itself opens the database and runs the schema.
    assert db.get_connection().execute("SELECT COUNT(*) FROM pdf_files").fetchone()[0] == 0


def test_claim_owner_never_raises_and_always_marks_the_process(fresh_db, monkeypatch):
    # No lock support, a folder that cannot be made, a lock already held: boot goes on.
    blocker = fresh_db.parent / "a-file"
    blocker.write_text("x")
    monkeypatch.setattr(db, "_owner_lock_path", lambda: blocker / "sub" / "reports.db.owner")
    assert db.claim_owner() is False
    assert os.environ.get(db._FAMILY_ENV) == str(os.getpid())


def test_a_second_claimant_waits_instead_of_failing(fresh_db):
    pytest.importorskip("fcntl")
    import time
    held = _hold_owner_lock(fresh_db)
    assert db.claim_owner() is False          # boot is not blocked
    held.close()                              # the old container goes away
    for _ in range(100):
        if db._owner_lock_fh is not None:
            break
        time.sleep(0.05)
    assert db._owner_lock_fh is not None      # and the new one now holds the lock


def test_the_bot_claims_before_its_first_connection_and_the_batch_process_inherits():
    main_src = (REPO / "main.py").read_text(encoding="utf-8")
    assert "    db.claim_owner()\n    db.get_connection()\n" in main_src
    # The supervisor passes its whole environment on, the marker with it.
    proc_src = (REPO / "scheduler" / "batch_proc.py").read_text(encoding="utf-8")
    assert "env={**os.environ" in proc_src
    batch_src = (REPO / "scheduler" / "batch_main.py").read_text(encoding="utf-8")
    assert "db.assume_schema_ready()" in batch_src


# ── the read-only probe ─────────────────────────────────────────────────────

@pytest.fixture
def sample_db(tmp_path):
    path = tmp_path / "sample.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE notes (id INTEGER PRIMARY KEY, source TEXT NOT NULL, body TEXT);
        CREATE INDEX notes_source ON notes(source);
        INSERT INTO notes (source, body) VALUES ('GS', 'first'), ('JPM', NULL), ('GS', 'tab\there');
    """)
    conn.commit()
    conn.close()
    return path


def _probe(db_path, *args, stdin=None):
    p = subprocess.run([sys.executable, str(REPO / "scripts" / "db_probe.py"), "--db", str(db_path), *args],
                       input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


def test_the_probe_reads(sample_db):
    code, out, err = _probe(sample_db, "SELECT source, COUNT(*) AS n FROM notes GROUP BY 1 ORDER BY 1")
    assert code == 0, err
    assert out.splitlines() == ["source\tn", "GS\t2", "JPM\t1"]


def test_the_probe_takes_sql_on_standard_input_and_writes_json(sample_db):
    code, out, err = _probe(sample_db, "-", "--json", stdin="SELECT id, body FROM notes WHERE id = 2")
    assert code == 0, err
    assert json.loads(out) == {"id": 2, "body": None}


def test_the_probe_lists_tables_and_a_schema(sample_db):
    code, out, _ = _probe(sample_db, "--tables")
    assert code == 0 and out.strip() == "notes\t3"
    code, out, _ = _probe(sample_db, "--schema", "notes")
    assert code == 0
    assert "id\tINTEGER\tPRIMARY KEY" in out and "source\tTEXT\tNOT NULL" in out
    assert "index notes_source (source)" in out
    code, _, err = _probe(sample_db, "--schema", "nothing")
    assert code == 1 and "no table named nothing" in err


@pytest.mark.parametrize("sql", [
    "DELETE FROM notes",
    "INSERT INTO notes (source) VALUES ('x')",
    "UPDATE notes SET body = 'x'",
    "DROP TABLE notes",
    "CREATE TABLE t (a)",
    "PRAGMA journal_mode=DELETE",
    "VACUUM",
])
def test_the_probe_cannot_write(sample_db, sql):
    before = sample_db.read_bytes()
    code, out, err = _probe(sample_db, sql)
    assert sample_db.read_bytes() == before
    conn = sqlite3.connect(sample_db)
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 3
    conn.close()
    if not sql.startswith("PRAGMA"):
        assert code == 1 and "db_probe:" in err, (code, out, err)


def test_the_probe_refuses_to_attach_another_file(sample_db, tmp_path):
    other = tmp_path / "other.db"
    code, _, err = _probe(sample_db, f"ATTACH DATABASE '{other.as_posix()}' AS o")
    assert code == 1 and "db_probe:" in err
    assert not other.exists()


def test_the_probe_stops_at_max_rows_and_says_so(sample_db):
    code, out, err = _probe(sample_db, "SELECT id FROM notes ORDER BY id", "--max-rows", "2")
    assert code == 0 and out.splitlines() == ["id", "1", "2"]
    assert "more than 2 rows" in err


def test_the_probe_interrupts_a_statement_that_runs_too_long(sample_db):
    endless = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"
    code, _, err = _probe(sample_db, endless, "--timeout", "0.5")
    assert code == 1 and "stopped after" in err


def test_the_probe_reports_a_missing_database(tmp_path):
    code, _, err = _probe(tmp_path / "absent.db", "SELECT 1")
    assert code != 0 and "no database at" in err
    assert not (tmp_path / "absent.db").exists()


def test_the_probe_imports_nothing_from_the_project():
    src = (REPO / "scripts" / "db_probe.py").read_text(encoding="utf-8")
    imports = [l.split()[1].split(".")[0] for l in src.splitlines()
               if l.startswith("import ") or l.startswith("from ")]
    assert set(imports) <= {"__future__", "argparse", "json", "os", "sqlite3", "sys", "time", "pathlib"}


# ── the command hook ────────────────────────────────────────────────────────

from scripts import command_guard as G  # noqa: E402

RAIL = "export MSYS_NO_PATHCONV=1 && env -u RAILWAY_API_TOKEN railway "


@pytest.mark.parametrize("command", [
    RAIL + 'ssh "pkill -f ask_live.py"',
    RAIL + "ssh 'killall python'",
    'R="env -u RAILWAY_TOKEN railway"; $R ssh "pkill -9 -f probe"',
    'echo "pkill -f x" | railway ssh "bash"',
    'railway.exe ssh "ps aux; pkill python"',
])
def test_a_pattern_kill_on_the_worker_is_blocked(command):
    reason = G.verdict(command)
    assert reason and "kill <that pid>" in reason


@pytest.mark.parametrize("command", [
    RAIL + 'ssh "kill 4312"',
    RAIL + 'ssh "ps aux | grep python"',
    RAIL + "logs --deployment | tail -50",
    "pkill -f local_server.py",
    "grep -rn pkill scripts/ docs/",
    RAIL + 'ssh "cat /tmp/pkill_notes.txt"',
    "git status",
    "",
])
def test_everything_else_runs(command):
    assert G.verdict(command) is None


def _hook(payload: str):
    p = subprocess.run([sys.executable, str(REPO / "scripts" / "command_guard.py")],
                       input=payload, capture_output=True, text=True)
    return p.returncode, p.stderr


def test_the_hook_blocks_with_exit_2_and_never_blocks_on_its_own_fault():
    code, err = _hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": RAIL + 'ssh "pkill -f x"'}}))
    assert code == 2 and "Blocked by scripts/command_guard.py" in err
    assert _hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))[0] == 0
    for broken in ("", "not json", "[]", '{"tool_input": null}', '{"tool_input": {"command": 7}}'):
        assert _hook(broken)[0] == 0, broken


def test_the_hook_is_registered_for_both_shell_tools_and_keeps_the_script_exit_code():
    cfg = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    entries = [e for e in cfg["hooks"]["PreToolUse"] if "command_guard.py" in json.dumps(e)]
    assert len(entries) == 1
    assert set(entries[0]["matcher"].split("|")) == {"Bash", "PowerShell"}
    command = entries[0]["hooks"][0]["command"]
    # A fallback chained with `||` would run the guard a second time with empty
    # input after a block and turn exit 2 into exit 0.
    assert "||" not in command and "exit 0" not in command
    # The session hooks that were there before are still there.
    assert "SessionStart" in cfg["hooks"] and "Stop" in cfg["hooks"]
