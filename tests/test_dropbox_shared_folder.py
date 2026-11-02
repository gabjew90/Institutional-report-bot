"""/Current read by its shared-folder id (2026-11-01): it stopped being mounted
in the bot's account on 2026-10-29 and every poll failed for three days."""
from datetime import datetime, timezone
from types import SimpleNamespace

from dropbox.common import PathRoot
from dropbox.files import FileMetadata

import db
from config import settings
from dropbox_client import watcher as W
from scheduler import jobs as J


def _entry(path):
    e = FileMetadata.__new__(FileMetadata)
    e._name_value = path.rsplit("/", 1)[1]
    e._path_display_value = path
    e._rev_value = "r1"
    e._size_value = 10
    e._server_modified_value = datetime(2026, 10, 31, tzinfo=timezone.utc)
    return e


def test_paths_are_stored_with_current_and_sent_without_it():
    assert W._stored_path("/2026/October/Oct 31/JPM/a.pdf") == "/Current/2026/October/Oct 31/JPM/a.pdf"
    assert W._api_path("/Current/2026/October/Oct 31/JPM/a.pdf") == "/2026/October/Oct 31/JPM/a.pdf"
    assert W._api_path("/Current") == ""
    assert W._api_path("/current/2026/a.pdf") == "/2026/a.pdf"
    assert W._api_path("/CurrentOther/a.pdf") == "/CurrentOther/a.pdf"


def test_with_no_id_the_account_tree_is_read_as_before(monkeypatch):
    monkeypatch.setattr(settings, "dropbox_shared_folder_id", "")
    assert W._api_path("/Current/2026/a.pdf") == "/Current/2026/a.pdf"
    assert W._stored_path("/Current/2026/a.pdf") == "/Current/2026/a.pdf"


def test_the_client_is_rooted_at_the_shared_folder(monkeypatch):
    class Fake:
        def __init__(self, **kw): self.root = None
        def with_path_root(self, root):
            self.root = root
            return self
    monkeypatch.setattr(W.dropbox, "Dropbox", Fake)
    monkeypatch.setattr(settings, "dropbox_shared_folder_id", "123")
    assert W._get_client().root == PathRoot.namespace_id("123")
    monkeypatch.setattr(settings, "dropbox_shared_folder_id", "")
    assert W._get_client().root is None


def test_a_listing_asks_for_the_root_and_returns_stored_paths(monkeypatch):
    asked = []

    class Dbx:
        def files_list_folder(self, folder, recursive=True):
            asked.append(folder)
            return SimpleNamespace(entries=[_entry("/2026/October/Oct 31/JPM/a.pdf")],
                                   has_more=False, cursor="c")
    monkeypatch.setattr(W, "_get_client", lambda: Dbx())
    monkeypatch.setattr(db, "get_dropbox_cursor", lambda: None)
    entries, _ = W.list_new_files.__wrapped__()
    assert asked == [""]
    assert entries[0].path == "/Current/2026/October/Oct 31/JPM/a.pdf"
    assert W.list_folder_files()[0].path == entries[0].path
    assert asked == ["", ""]


def test_a_download_and_a_link_send_the_path_without_current(monkeypatch, tmp_path):
    got = []

    class Dbx:
        def files_download_to_file(self, dest, path): got.append(path)
        def files_get_temporary_link(self, path):
            got.append(path)
            return SimpleNamespace(link="u")
    monkeypatch.setattr(W, "_get_client", lambda: Dbx())
    W.download_file.__wrapped__("/Current/2026/a.pdf", tmp_path / "a.pdf")
    assert W.get_temporary_link("/Current/2026/b.pdf") == "u"
    assert got == ["/2026/a.pdf", "/2026/b.pdf"]


def test_a_relist_comes_back_oldest_first_and_keeps_the_floors_second(monkeypatch):
    """A restart partway through a backlog must not move the floor past notes
    not fetched yet."""
    class Reset:
        def is_reset(self): return True

    def at(name, hour):
        e = _entry(f"/2026/{name}")
        e._server_modified_value = datetime(2026, 10, 31, hour)
        return e

    class Dbx:
        def files_list_folder_continue(self, cursor):
            raise W.dropbox.exceptions.ApiError("rid", Reset(), "msg", None)

        def files_list_folder(self, folder, recursive=True):
            return SimpleNamespace(entries=[at("late.pdf", 20), at("old.pdf", 5),
                                            at("tied.pdf", 10), at("mid.pdf", 15)],
                                   has_more=False, cursor="c")
    monkeypatch.setattr(W, "_get_client", lambda: Dbx())
    monkeypatch.setattr(db, "get_dropbox_cursor", lambda: "stale")
    monkeypatch.setattr(db, "get_latest_dropbox_modified_at", lambda: "2026-10-31T10:00:00")
    entries, _ = W.list_new_files.__wrapped__()
    assert [e.name for e in entries] == ["tied.pdf", "mid.pdf", "late.pdf"]


def test_a_second_failed_poll_in_a_row_alerts():
    assert J.poll_failure_alert(1, "x") == ""
    assert "failed 2 times in a row" in J.poll_failure_alert(2, "not_found")
    assert len(J.poll_failure_alert(5, "e" * 1000)) < 300


def test_the_feed_alert_no_longer_says_polling_is_fine():
    text = J.research_feed_alert(None)
    assert "polling fine" not in text and "failed Dropbox polls" in text
