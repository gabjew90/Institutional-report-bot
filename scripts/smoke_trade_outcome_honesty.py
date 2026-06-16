"""Smoke test: trade-outcome honesty (2026-06-16).

Member ZHawk said the bot was "making up trades." Investigation: the
trades were REAL (existence grounded in analyst_trades) but the bot
fabricated their OUTCOMES at two layers:
  1. Profile builder — _format_analyst_trades_block rendered a bare
     OPEN row with no status, so the profile narrated a TSLA 410C that
     expired 06-12 as "Open, waiting for his reversal thesis to
     materialize" (4 days after expiry) and a no-close BTC as "still
     held in limbo."
  2. Live /ask — the model over-asserted "expired worthless… dead" and
     "still holding" on top.

Fix:
  - _format_analyst_trades_block now emits a STATUS TAG per row:
    expired/past-expiry-no-close -> OUTCOME UNKNOWN; open-no-exit ->
    "no exit posted, may have closed without posting"; exit-no-entry.
  - Profile BINDING RULE extended to status/outcome (not just %).
  - Live §4 gets a ZERO UNFORCED TRADE-OUTCOME ASSERTIONS rule.

This smoke covers the formatter tags + both prompt rules.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_UID = 390341447918026753


def _rows():
    return [
        # Expired by date, no close -> OUTCOME UNKNOWN
        {"author_id": _UID, "posted_at": "2026-06-09T17:38:00", "action": "open",
         "ticker": "TSLA", "contract_type": "call", "strike": 410.0,
         "expiry": "2020-01-15", "gain_pct": None, "price": 3.3,
         "inferred_status": "expired_unknown", "tracking_mode": "member"},
        # Open, no expiry (crypto), no close -> "no exit posted" (not held)
        {"author_id": _UID, "posted_at": "2026-06-01T00:48:00", "action": "open",
         "ticker": "BTC", "contract_type": "", "strike": None,
         "expiry": "", "gain_pct": None, "price": 73906.0,
         "inferred_status": None, "tracking_mode": "member"},
        # Genuinely future-dated open -> still "no exit posted" tag
        {"author_id": _UID, "posted_at": "2026-06-03T00:00:00", "action": "open",
         "ticker": "PURR", "contract_type": "call", "strike": 13.0,
         "expiry": "2099-12-18", "gain_pct": None, "price": 1.1,
         "inferred_status": None, "tracking_mode": "member"},
        # Closed with a real gain -> cite the %, no status tag
        {"author_id": _UID, "posted_at": "2026-05-18T14:00:00", "action": "close",
         "ticker": "GLW", "contract_type": "call", "strike": 185.0,
         "expiry": "2026-05-22", "gain_pct": 100.73, "price": None,
         "inferred_status": None, "tracking_mode": "member"},
    ]


def test_formatter_emits_status_tags():
    import backfill_user_profiles as bf
    with patch("backfill_user_profiles.db.get_recent_analyst_trades",
               return_value=_rows()):
        block = bf._format_analyst_trades_block(_UID)
    lines = {ln.split("  ")[2].strip().split()[0] if "  " in ln else "":
             ln for ln in block.splitlines()}

    tsla = next(l for l in block.splitlines() if "TSLA" in l)
    assert "EXPIRED" in tsla and "OUTCOME UNKNOWN" in tsla, tsla
    assert "worthless" in tsla.lower(), "tag must spell out the alternatives"

    btc = next(l for l in block.splitlines() if "BTC" in l)
    assert "OPEN per log" in btc and "still held" in btc, btc

    purr = next(l for l in block.splitlines() if "PURR" in l)
    assert "OPEN per log" in purr, purr

    glw = next(l for l in block.splitlines() if "GLW" in l)
    assert "gain +100.73%" in glw, glw
    assert "EXPIRED" not in glw and "OPEN per log" not in glw, (
        f"a closed trade with a real gain must not get an open/expired "
        f"tag: {glw}"
    )
    _ok("formatter: EXPIRED-outcome-unknown / open-no-exit / closed-with-"
        "gain tags correct")


def test_profile_prompt_status_rule():
    import backfill_user_profiles as bf
    p = bf.PROFILE_PROMPT
    assert "BINDING RULE on trade STATUS and OUTCOME" in p
    assert "OUTCOME UNKNOWN" in p
    assert "still held" in p.lower() or "still holding" in p.lower()
    assert "waiting to materialize" in p, (
        "rule must name the observed present-tense-narrative failure"
    )
    _ok("profile prompt: status/outcome binding rule present with the "
        "observed failure modes")


def test_live_ask_prompt_outcome_rule():
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as P
    assert "ZERO UNFORCED TRADE-OUTCOME ASSERTIONS" in P
    anchor = P.find("ZERO UNFORCED TRADE-OUTCOME ASSERTIONS")
    window = P[anchor:anchor + 1600]
    assert "outcome unrecorded" in window or "OUTCOME UNKNOWN" in window
    assert "still holding" in window.lower() or "still held" in window.lower()
    assert "expired worthless" in window.lower(), (
        "rule must anchor the observed 06-16 fabrication"
    )
    assert "gain_pct" in window, "must tie P&L claims to the gain_pct field"
    _ok("live §4: ZERO UNFORCED TRADE-OUTCOME rule with anchors + "
        "gain_pct tie")


if __name__ == "__main__":
    print("=== trade-outcome honesty smoke ===")
    test_formatter_emits_status_tags()
    test_profile_prompt_status_rule()
    test_live_ask_prompt_outcome_rule()
    print("\nALL TRADE-OUTCOME HONESTY SMOKE TESTS PASS")
