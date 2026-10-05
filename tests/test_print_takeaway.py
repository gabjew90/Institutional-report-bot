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
    # the restatement rule is enforced in code now, not asked for
    assert "restates a number from the table" not in T.SYSTEM


JOBS_ROWS = [
    {"label": "Non Farm Payrolls", "actual": "+29K", "consensus": "+89K", "prior": "+162K"},
    {"label": "Unemployment Rate", "actual": "4.2%", "consensus": "4.1%", "prior": "4.1%"},
    {"label": "Avg Hourly Earnings m/m", "actual": "+0.1%", "consensus": "+0.3%"},
]
BOFA = [{"source": "BofA Securities", "title": "Sep jobs preview", "published": "2026-09-27",
         "macro": [{"indicator": "Nonfarm Payrolls", "reading": "60k"}],
         "insights": ["BofA expects payrolls of 60k, driven by payback from August's seasonal factors; "
                      "underlying job growth remains resilient at 100k+."]}]


def test_a_bullet_that_only_repeats_the_table_is_dropped():
    """2026-10-02: the second jobs bullet restated the table."""
    ev = T.evidence_text(JOBS_ROWS, [], BOFA)
    table = T.evidence_text(JOBS_ROWS, [], [])
    stats = {}
    kept = T.guard([
        {"label": "Payroll miss", "text": "Payrolls rose 29K against an 89K consensus and BofA's 60K call."},
        {"label": "Unemployment and earnings", "text": "The unemployment rate hit 4.2%. Average hourly "
                                                       "earnings grew 0.1%, missing the 0.3% consensus."},
        {"label": "Seasonal payback", "text": "The weak September is the payback for August that BofA "
                                              "had flagged, with underlying growth near 100K."},
    ], ev, table, stats)
    assert [k.split(":**")[0] for k in kept] == ["• **Payroll miss", "• **Seasonal payback"]
    assert stats == {"restated": 1}


def test_a_bank_opening_with_a_verb_of_saying_is_dropped_and_the_prompt_says_so():
    """The pulse voice rule (source-prefix) discards 'BofA said ...'; the
    prompt has to tell the model the form that survives."""
    ev = T.evidence_text(JOBS_ROWS, [], BOFA)
    assert T.guard([{"label": "x", "text": "BofA said payrolls would be 60K."}], ev) == []
    assert "Never open with the bank and a verb of saying" in T.SYSTEM


class _Client:
    """Answers each call with the next canned bullet list."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.prompts.append(contents)
        return type("R", (), {"text": json.dumps({"bullets": self.answers.pop(0)})})()


RESTATED = [{"label": "Jobless rate", "text": "The unemployment rate hit 4.2% against 4.1% expected."}]
MEANING = [{"label": "Seasonal payback", "text": "BofA called a weak September payback for August, "
                                                 "with underlying growth near 100K."}]


def test_a_restated_answer_is_retried_once_with_feedback():
    c = _Client(RESTATED, MEANING)
    out = T.generate("jobs", "Jobs", JOBS_ROWS, [], BOFA, client=c, model="m")
    assert len(c.prompts) == 2 and T.RESTATED_FEEDBACK in c.prompts[1]
    assert out and out[0].startswith("• **Seasonal payback")


def test_a_retry_that_runs_long_keeps_the_first_answer(monkeypatch):
    import time as _time

    class Slow(_Client):
        def generate_content(self, model, contents, config):
            if self.prompts:
                _time.sleep(0.5)
            return super().generate_content(model, contents, config)

    first = [MEANING[0], RESTATED[0]]
    monkeypatch.setattr(T, "BUDGET_S", 0.2)
    c = Slow(first, MEANING + MEANING)
    out = T.generate("jobs", "Jobs", JOBS_ROWS, [], BOFA, client=c, model="m")
    assert [o.split(":**")[0] for o in out] == ["• **Seasonal payback"]


def test_no_retry_without_research_or_when_nothing_was_restated():
    c = _Client(RESTATED)
    assert T.generate("jobs", "Jobs", JOBS_ROWS, [], [], client=c, model="m") == []
    assert len(c.prompts) == 1
    c = _Client(MEANING)
    T.generate("jobs", "Jobs", JOBS_ROWS, [], BOFA, client=c, model="m")
    assert len(c.prompts) == 1
