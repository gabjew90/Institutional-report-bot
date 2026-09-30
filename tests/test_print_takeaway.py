"""Quick Takeaway inputs and guards (2026-09-30)."""
import json
import re

from report import print_takeaway as T


def _note(source, status="forecast", period="August", reading="0.3%", created="2026-09-28"):
    a = {"source": source, "title": "t", "published_at": created,
         "macro_indicators": [{"indicator": "Core PCE m/m", "reading": reading, "status": status, "period": period}],
         "key_insights": ["PCE preview"]}
    return (created, json.dumps(a))


def test_research_ranks_tier_one_first_then_same_period_forecasts():
    rx = re.compile(T.RESEARCH_TERMS["pce"], re.I)
    rows = [_note("PNC Economics"), _note("World Gold Council"), _note("Goldman Sachs", status="released"),
            _note("Morgan Stanley"), _note("Scotiabank"), _note("JPMorgan", period="July")]
    out = T.reduce_research(rows, rx, limit=10, period="August")
    assert [n["source"] for n in out] == ["Morgan Stanley", "Goldman Sachs", "JPMorgan", "PNC Economics", "World Gold Council"]
    assert "Scotiabank" not in [n["source"] for n in out], "at most two non-tier-1 notes"


def test_guard_drops_unsourced_figures_and_degree_adverbs():
    ev = T.evidence_text([{"label": "Core PCE m/m", "actual": "+0.2%", "consensus": "+0.3%"}], [], [])
    kept = T.guard([
        {"label": "Cooler core", "text": "Core PCE rose 0.2% against a 0.3% consensus."},
        {"label": "Made up", "text": "That is a 2.4% annualized pace."},
        {"label": "Fluff", "text": "Inflation matched expectations perfectly at 0.2%."},
    ], ev)
    assert kept == ["• **Cooler core:** Core PCE rose 0.2% against a 0.3% consensus."]


def test_prompt_forbids_degree_adverbs_and_the_room():
    assert "adverb" in T.SYSTEM and "room" in T.SYSTEM.lower()


def test_prompt_asks_for_one_actionable_point_per_bullet():
    assert "one point a trader can act on or check" in T.SYSTEM
    assert "restates a number from the table" in T.SYSTEM
