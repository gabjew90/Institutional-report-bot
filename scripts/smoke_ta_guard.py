"""Smoke test: TA guard (2026-06-18).

The bot has NO indicator data source (no RSI/MACD feed, no chart
engine), so any indicator read it emits is invented from priors
(observed 2026-06-17: confabulated "GEO/CXW RSI", an "NDX 30,000
pivot"). The grounding backstop misses these because TA claims often
carry no number to trip the market-fact density test.

This guard catches two shapes when nothing sourced the answer:
  (a) INDICATOR reads (RSI/MACD/moving-average/overbought/oversold) —
      always invented when unsourced, so HARD-STRIPPED.
  (b) CHART-LEVEL claims (support/resistance/breakout/pivot/"holds $X")
      that are NOT tied to a named source — regenerated, then hedged.

Both are ignored when the answer is grounded or a data tool fired — a
web source can legitimately quote a level/indicator (same trust rule
as the market-fact backstop). Must NOT misfire on the roast register
or on attributed, relayed levels ("Goldman sees support at...").

Covers the detector + the splitter/strip helpers + that the
regenerate-then-strip block is wired into the answer path.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


class _GM:
    def __init__(self, chunks):
        self.grounding_chunks = chunks


_TA_INDICATOR = (
    "NVDA looks stretched here — RSI is sitting at 78, deeply overbought, "
    "and the MACD just rolled over. The 50-day moving average is the line "
    "to watch."
)
_TA_LEVEL_UNATTRIB = (
    "I'd watch the 175 level on SPCX — it's been solid support and a clean "
    "break above resistance opens a run to new highs on the breakout."
)
_TA_LEVEL_ATTRIB = (
    "Goldman's desk sees support around 175 and flags resistance into the "
    "highs, per their morning note."
)


def test_detector_fires_on_unsourced_indicator():
    from discord_bot.bot import _has_unsourced_ta as f
    assert f(_TA_INDICATOR, None, []) is True
    _ok("detector: fires on unsourced indicator read (RSI/MACD/MA)")


def test_detector_fires_on_unattributed_level():
    from discord_bot.bot import _has_unsourced_ta as f
    assert f(_TA_LEVEL_UNATTRIB, None, []) is True
    _ok("detector: fires on unattributed chart-level claim")


def test_detector_skips_attributed_level():
    from discord_bot.bot import _has_unsourced_ta as f
    assert f(_TA_LEVEL_ATTRIB, None, []) is False
    _ok("detector: attributed level (Goldman/desk/per note) does not fire")


def test_detector_skips_when_grounded():
    from discord_bot.bot import _has_unsourced_ta as f
    assert f(_TA_INDICATOR, _GM([{"web": "x"}]), []) is False
    _ok("detector: skips when Google grounding fired")


def test_detector_skips_when_tool_fired():
    from discord_bot.bot import _has_unsourced_ta as f
    assert f(_TA_INDICATOR, None, [{"tool": "lookup_market_price"}]) is False
    _ok("detector: skips when a data tool fired")


def test_detector_roast_safe():
    from discord_bot.bot import _has_unsourced_ta as f
    roasts = [
        "Enjoy the 200% gains while they last, you absolute clown.",
        "blowing $3,500 on soccer tickets while your portfolio gets nuked",
        "your read is so bad even a coin flip resents the comparison",
        "spending $150 on Pokemon cards while crying about rent",
    ]
    for r in roasts:
        assert f(r, None, []) is False, f"must NOT fire on roast: {r!r}"
    _ok("detector: roast-safe — no TA shapes in personal jabs")


def test_detector_skips_plain_fundamentals():
    from discord_bot.bot import _has_unsourced_ta as f
    plain = (
        "NVDA's data-center revenue is the whole story — if Blackwell ships "
        "on time the multiple holds. Watch the next earnings print for the "
        "gross-margin trajectory."
    )
    assert f(plain, None, []) is False, "fundamentals must not trip TA guard"
    _ok("detector: plain fundamentals/catalysts do not trip the guard")


def test_violations_separates_indicator_and_level():
    from discord_bot.bot import _ta_violations
    ind, lvl = _ta_violations(_TA_INDICATOR)
    assert ind and not lvl, (ind, lvl)
    ind2, lvl2 = _ta_violations(_TA_LEVEL_UNATTRIB)
    assert lvl2 and not ind2, (ind2, lvl2)
    _ok("_ta_violations: separates indicator sentences from level sentences")


def test_ma_collision_safe():
    from discord_bot.bot import _ta_violations
    # Bare "MA"/"Ma" must not register as a moving-average read.
    for s in [
        "Ma sold her position last week, classic top signal.",
        "He moved to MA last year and still can't read a chart.",
    ]:
        ind, lvl = _ta_violations(s)
        assert not ind, f"bare MA/Ma must not match indicator: {s!r} -> {ind}"
    # But EMA/SMA and N-day moving average DO register.
    ind2, _ = _ta_violations("The 200-day moving average held and the EMA crossed up.")
    assert ind2, "real moving-average reads must register"
    _ok("_ta_violations: bare MA/Ma safe; EMA/SMA/N-day moving avg register")


def test_strip_removes_indicator_sentences():
    from discord_bot.bot import _strip_sentences, _ta_violations
    ind, _ = _ta_violations(_TA_INDICATOR)
    stripped = _strip_sentences(_TA_INDICATOR, ind)
    ind_after, _ = _ta_violations(stripped)
    assert not ind_after, f"indicator sentences must be gone: {stripped!r}"
    _ok("_strip_sentences: removes invented indicator sentences cleanly")


def test_guard_block_wired():
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert "_has_unsourced_ta(" in src, "detector must gate the path"
    assert "NO CHART DATA" in src, "no-chart-data retry directive missing"
    assert "_strip_sentences(" in src, "indicator strip step missing"
    assert "no chart feed" in src, "level hedge fallback missing"
    _ok("TA guard wired into _answer_with_gemini: detect -> retry -> strip -> hedge")


if __name__ == "__main__":
    print("=== TA guard smoke ===")
    test_detector_fires_on_unsourced_indicator()
    test_detector_fires_on_unattributed_level()
    test_detector_skips_attributed_level()
    test_detector_skips_when_grounded()
    test_detector_skips_when_tool_fired()
    test_detector_roast_safe()
    test_detector_skips_plain_fundamentals()
    test_violations_separates_indicator_and_level()
    test_ma_collision_safe()
    test_strip_removes_indicator_sentences()
    test_guard_block_wired()
    print("\nALL TA GUARD SMOKE TESTS PASS")
