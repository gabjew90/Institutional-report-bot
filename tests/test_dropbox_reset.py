"""Dropbox cursor invalidation (2026-09-01 review, P1).

A reset used to raise on every poll forever. Now it re-lists from
scratch with a modified-time floor and carries on.
"""
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import dropbox
from dropbox.files import FileMetadata

import db
from dropbox_client import watcher as W


class _ResetErr:
    def is_reset(self): return True


class _OtherErr:
    def is_reset(self): return False


def _api_error(err):
    return dropbox.exceptions.ApiError("rid", err, "msg", None)


def _entry(name, when):
    e = FileMetadata.__new__(FileMetadata)
    e._name_value = name
    e._path_display_value = f"/Current/{name}"
    e._rev_value = "r1"
    e._size_value = 10
    e._server_modified_value = when
    return e


class _Dbx:
    def __init__(self, continue_err=None, entries=()):
        self.continue_err = continue_err
        self.entries = list(entries)
        self.listed_from_scratch = False

    def files_list_folder_continue(self, cursor):
        if self.continue_err is not None:
            raise self.continue_err
        return SimpleNamespace(entries=self.entries, has_more=False, cursor="c2")

    def files_list_folder(self, folder, recursive=True):
        self.listed_from_scratch = True
        return SimpleNamespace(entries=self.entries, has_more=False, cursor="fresh")


def _with(dbx, fn):
    o_client, o_cursor, o_floor = W._get_client, db.get_dropbox_cursor, db.get_latest_dropbox_modified_at
    W._get_client = lambda: dbx
    db.get_dropbox_cursor = lambda: "stale-cursor"
    db.get_latest_dropbox_modified_at = lambda: "2026-09-01T12:00:00+00:00"
    try:
        return fn()
    finally:
        W._get_client, db.get_dropbox_cursor, db.get_latest_dropbox_modified_at = o_client, o_cursor, o_floor


def test_reset_relists_from_scratch_and_continues():
    old = _entry("old.pdf", datetime(2026, 8, 1, tzinfo=timezone.utc))
    new = _entry("new.pdf", datetime(2026, 9, 1, 15, tzinfo=timezone.utc))
    dbx = _Dbx(continue_err=_api_error(_ResetErr()), entries=[old, new])
    entries, cursor = _with(dbx, W.list_new_files.__wrapped__)
    assert dbx.listed_from_scratch
    assert [e.name for e in entries] == ["new.pdf"], "floor must drop history"
    assert cursor == "fresh"


def test_non_reset_api_error_still_raises():
    dbx = _Dbx(continue_err=_api_error(_OtherErr()))
    try:
        _with(dbx, W.list_new_files.__wrapped__)
    except dropbox.exceptions.ApiError:
        return
    raise AssertionError("a non-reset ApiError must propagate")


def test_normal_poll_has_no_floor():
    old = _entry("old.pdf", datetime(2026, 8, 1, tzinfo=timezone.utc))
    dbx = _Dbx(entries=[old])
    entries, _ = _with(dbx, W.list_new_files.__wrapped__)
    assert [e.name for e in entries] == ["old.pdf"]
    assert not dbx.listed_from_scratch


def test_latest_modified_helper_reads_the_table():
    assert db.get_latest_dropbox_modified_at() is None or isinstance(
        db.get_latest_dropbox_modified_at(), str)


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
