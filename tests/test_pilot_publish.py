"""Pilot source-text publisher (piece 1).

The publisher runs inside the production analysis pipeline, so its
contract is defensive: HIGH only, gated off by default, isolated to
the pilot branch, idempotent, and incapable of failing an analysis.
"""
import sys

from github_bridge.pilot_publish import _slug, publish_high_document


class _Settings:
    def __init__(self, enabled):
        self.pilot_publish_enabled = enabled


def _call(monkey_enabled, priority="high", **kw):
    """Invoke the publisher with a patched settings flag, capturing
    any github calls."""
    import config
    from github_bridge import client as gh
    calls = []
    orig_settings = config.settings
    orig_put, orig_get = gh.put_file, gh.get_file
    try:
        config.settings = _Settings(monkey_enabled)
        gh.put_file = lambda path, content, msg, ref=None: (
            calls.append((path, ref)) or {})
        gh.get_file = lambda path, ref=None: None
        return publish_high_document(
            pdf_file_id=kw.get("pdf_file_id", 7),
            file_name="note.pdf", source=kw.get("source", "Goldman"),
            title=kw.get("title", "AI Capex Update"), priority=priority,
            published_at="2026-09-01T12:00:00", full_text="body text",
        ), calls
    finally:
        config.settings = orig_settings
        gh.put_file, gh.get_file = orig_put, orig_get


def test_disabled_by_default_publishes_nothing():
    """The flag stays OFF until shakedown day -2: deploying the code
    must not start filling a branch."""
    ok, calls = _call(False)
    assert ok is False and calls == []


def test_medium_priority_is_not_published():
    """HIGH only — publishing MEDIUM would quietly change the
    experiment's corpus."""
    ok, calls = _call(True, priority="medium")
    assert ok is False and calls == []


def test_high_publishes_text_and_meta_to_the_pilot_branch():
    ok, calls = _call(True)
    assert ok is True
    paths = [p for p, _ in calls]
    refs = {r for _, r in calls}
    assert any(p.endswith(".txt") for p in paths)
    assert any(p.endswith(".meta.json") for p in paths)
    # Isolation is the whole reason option B was chosen.
    assert refs == {"pilot-data"}, refs
    assert all(p.startswith("pilot/source-text/2026-09-01/")
               for p in paths), paths


def test_already_published_is_skipped():
    """Idempotent by path — a duplicate would be read twice and
    double-count in every pilot metric."""
    import config
    from github_bridge import client as gh
    calls = []
    orig_settings, orig_put, orig_get = config.settings, gh.put_file, gh.get_file
    try:
        config.settings = _Settings(True)
        gh.put_file = lambda path, content, msg, ref=None: (
            calls.append(path) or {})
        gh.get_file = lambda path, ref=None: {"sha": "exists"}
        ok = publish_high_document(
            pdf_file_id=7, file_name="n.pdf", source="GS", title="t",
            priority="high", published_at="2026-09-01", full_text="x")
    finally:
        config.settings, gh.put_file, gh.get_file = (
            orig_settings, orig_put, orig_get)
    assert ok is False and calls == []


def test_never_raises_when_github_fails():
    """It runs inside the analysis pipeline: a raised exception here
    would be a production incident caused by an experiment."""
    import config
    from github_bridge import client as gh
    orig_settings, orig_get = config.settings, gh.get_file

    def boom(*a, **k):
        raise RuntimeError("github down")
    try:
        config.settings = _Settings(True)
        gh.get_file = boom
        ok = publish_high_document(
            pdf_file_id=7, file_name="n.pdf", source="GS", title="t",
            priority="high", published_at="2026-09-01", full_text="x")
    finally:
        config.settings, gh.get_file = orig_settings, orig_get
    assert ok is False


def test_slug_is_space_free():
    """The reader workflow reads a TSV of paths; a space in a filename
    would split a field."""
    s = _slug("AI Capex: Another V-Shaped Move Ahead?")
    assert " " not in s and "\t" not in s
    assert s == "ai-capex-another-v-shaped-move-ahead"


def test_slug_survives_an_empty_title():
    assert _slug("") == "untitled"


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
