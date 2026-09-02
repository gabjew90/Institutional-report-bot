"""tool_docs.py carries every tool's routing text and had no test.

The 2026-09-01 earnings-slate incident was a routing-text defect: the
docs sent "who reports today" sweeps to Google Search, which returns
partial lists. These pin the contract that made that possible.
"""
import re
import sys

from discord_bot import bot as B
from discord_bot.tool_docs import TOOL_DOCS


def _declared_tool_names() -> set[str]:
    names = set()
    for attr in dir(B):
        if attr.startswith("_build_") and attr.endswith("_tool"):
            try:
                tool = getattr(B, attr)()
            except Exception:
                continue
            for fd in getattr(tool, "function_declarations", []) or []:
                names.add(fd.name)
    return names


def test_every_declared_tool_has_docs():
    declared = _declared_tool_names()
    assert declared, "no tool declarations found"
    missing = sorted(n for n in declared if n not in TOOL_DOCS)
    assert not missing, f"tools without TOOL_DOCS entry: {missing}"


def test_every_doc_names_a_declared_tool():
    declared = _declared_tool_names()
    orphans = sorted(n for n in TOOL_DOCS if n not in declared)
    assert not orphans, f"TOOL_DOCS entries with no declaration: {orphans}"


def test_slate_sweeps_are_never_routed_to_google():
    """The defect: a slate question answered from a search snippet."""
    d = TOOL_DOCS["lookup_earnings_date"]
    assert "lookup_earnings_slate" in d
    assert not re.search(r"sweeps?\s*\(Google", d), d
    s = TOOL_DOCS["lookup_earnings_slate"]
    assert "NEVER" in TOOL_DOCS["lookup_earnings_date"] or "PARTIAL" in s


def test_unconfirmed_session_is_report_not_drop():
    s = TOOL_DOCS["lookup_earnings_slate"]
    assert "session_confirmed" in s and "not that the company is absent" in s


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
