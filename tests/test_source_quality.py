"""Source-quality classifier (warn-only) — review session 4.

The bljesak incident: a grounded answer cited a Bosnian regional news
site as sole support for a claim about a tweet. Grounded ✅, useless
source. The check counts the all-unlisted shape; these tests pin the
classifier's judgment on the incident, the sane majors, and the two
rules that keep the warn honest: one sane citation clears the answer,
and the .gov family passes without enumeration.
"""
import sys
from types import SimpleNamespace as NS

from discord_bot.bot import (_citation_domains, _domain_is_sane,
                             _source_quality_unlisted)


def _gm(*chunks):
    return NS(grounding_chunks=[
        NS(web=NS(title=t, uri=u)) for t, u in chunks])


# ------------------------------------------------------------ domains

def test_incident_domain_is_not_sane():
    assert not _domain_is_sane("bljesak.info")
    assert not _domain_is_sane("dnevnik.hr")


def test_majors_are_sane_with_subdomains():
    assert _domain_is_sane("reuters.com")
    assert _domain_is_sane("www.reuters.com")
    assert _domain_is_sane("graphics.reuters.com")
    assert _domain_is_sane("finance.yahoo.com")


def test_gov_family_passes_without_enumeration():
    assert _domain_is_sane("federalreserve.gov")
    assert _domain_is_sane("ustr.gov")
    assert _domain_is_sane("census.gov")


def test_lookalike_suffix_is_not_sane():
    """'notreuters.com' must not ride the suffix match."""
    assert not _domain_is_sane("notreuters.com")
    assert not _domain_is_sane("reuters.com.fake.io")


def test_company_ir_is_unlisted_by_design():
    """The acknowledged long tail — this is WHY the check is warn-only,
    and it must stay warn-only while this is true."""
    assert not _domain_is_sane("nvidia.com")
    assert not _domain_is_sane("investor.broadcom.com")


# --------------------------------------------------------- extraction

def test_title_domain_wins_over_vertex_redirect():
    """Gemini chunk uris are often vertexaisearch redirects; the title
    carries the real domain. The incident's exact shape."""
    gm = _gm(("bljesak.info",
              "https://vertexaisearch.cloud.google.com/grounding/x"))
    assert _citation_domains(gm) == ["bljesak.info"]


def test_real_uri_host_used_when_title_is_prose():
    gm = _gm(("Powell speech transcript",
              "https://www.federalreserve.gov/speech.htm"))
    assert _citation_domains(gm) == ["www.federalreserve.gov"]


# ------------------------------------------------------------ verdict

def test_all_unlisted_flags():
    gm = _gm(("bljesak.info", ""), ("dnevnik.hr", ""))
    assert _source_quality_unlisted(gm) == ["bljesak.info", "dnevnik.hr"]


def test_one_sane_citation_clears_the_answer():
    """A niche source ALONGSIDE a wire is corroboration, not the
    incident shape. Flagging it would punish good sourcing."""
    gm = _gm(("bljesak.info", ""),
             ("Reuters", "https://www.reuters.com/markets/x"))
    assert _source_quality_unlisted(gm) == []


def test_no_citations_no_flag():
    assert _source_quality_unlisted(_gm()) == []
    assert _source_quality_unlisted(None) == []


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
