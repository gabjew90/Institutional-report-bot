"""_is_valid_source composite handling (2026-09-01).

The adjudicator writes a joint attribution as ONE string when two
banks assert the same fact ("Deutsche Bank + Goldman Sachs"). Exact
matching rejected those, so a theme whose only multi-bank fact was
jointly sourced got DISCARDED for a bug rather than a judgment — and
on 2026-09-01 the published MAIN EVENT was built on Jackson Hole
while the `jackson hole symposium` theme had been dropped, so the
pulse never named the event it was about.

The routine md carries this logic inside a heredoc, so this test
reimplements it from the same source text to keep the two honest:
the function body below is EXTRACTED from the routine, not retyped.
"""
import re
import sys
from pathlib import Path

ROUTINE = Path("docs/superpowers/routines/synthesis-routine.md")

VALID = {"Goldman Sachs", "Deutsche Bank", "JPMorgan", "Citi",
         "Morgan Stanley", "TS Lombard"}
AGENCY = {"Reuters", "Bloomberg"}


def _extract_impl():
    """Pull the live _is_valid_source body out of the routine and
    build a callable, so this test can never drift from what ships."""
    text = ROUTINE.read_text(encoding="utf-8")
    m = re.search(r"    def _is_valid_source\(name: str\) -> bool:\n"
                  r"(.*?)(?=\n    # Rule 2)", text, re.S)
    assert m, "could not locate _is_valid_source in the routine"
    body = m.group(1)
    src = ("def _is_valid_source(name):\n" +
           "\n".join(ln[4:] if ln.startswith("    ") else ln
                     for ln in body.splitlines()))
    ns = {"re": re, "valid_sources": VALID, "AGENCY_WHITELIST": AGENCY}
    exec(src, ns)
    return ns["_is_valid_source"]


_is_valid_source = _extract_impl()


def test_the_incident_composite_now_passes():
    """The exact string that dropped the Jackson Hole theme."""
    assert _is_valid_source("Deutsche Bank + Goldman Sachs")


def test_other_separators_the_adjudicator_uses():
    assert _is_valid_source("Goldman Sachs & Citi")
    assert _is_valid_source("Goldman Sachs, Citi")
    assert _is_valid_source("Goldman Sachs and Citi")
    assert _is_valid_source("Goldman Sachs / Citi")


def test_plain_names_still_pass():
    assert _is_valid_source("Goldman Sachs")
    assert _is_valid_source("Reuters")
    assert _is_valid_source("")


def test_a_composite_is_only_as_good_as_its_weakest_part():
    """The whole point of validating sources is catching invented
    ones. A composite must not become a laundering channel."""
    assert not _is_valid_source("Goldman Sachs + SomeMadeUpDesk")
    assert not _is_valid_source("MadeUpBank & AlsoFake")


def test_unknown_single_source_still_fails():
    assert not _is_valid_source("SomeMadeUpDesk")


def test_a_bank_name_containing_and_is_not_split_into_garbage():
    """Guard against the separator eating a real name: nothing in the
    whitelist has ' and ' in it today, but if one is ever added, the
    single-name check runs FIRST and wins."""
    v = dict.fromkeys(["Smith and Jones Bank"])
    ns_valid = set(v) | VALID
    text = ROUTINE.read_text(encoding="utf-8")
    m = re.search(r"    def _is_valid_source\(name: str\) -> bool:\n"
                  r"(.*?)(?=\n    # Rule 2)", text, re.S)
    src = ("def f(name):\n" +
           "\n".join(ln[4:] if ln.startswith("    ") else ln
                     for ln in m.group(1).splitlines()))
    ns = {"re": re, "valid_sources": ns_valid, "AGENCY_WHITELIST": AGENCY}
    exec(src, ns)
    assert ns["f"]("Smith and Jones Bank")


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
