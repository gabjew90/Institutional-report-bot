"""Smoke: lookup_options_chain per-strike support (2026-07-29).

The tool fetched per-contract data (each strike's OI/volume/IV via
Yahoo) then aggregated it into expiration totals and threw the strike
detail away — so "MSFT 400c 7/31 OI" got declined ("I don't have
single-strike OI") when the number was one filter away from data we
already pull. Fix: optional `strike` + `contract_type` params return
the specific contract. (Historical/multi-day still needs snapshot
storage — this is the CURRENT snapshot only.)
"""

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_RAW = {
    "underlying_symbol": "MSFT",
    "underlying_spot_price": 418.0,
    "expiration_dates": ["2026-07-31", "2026-08-15"],
    "chain": {
        "expiration_iso": "2026-07-31",
        "calls": [
            {"strike": 395.0, "openInterest": 1200, "volume": 300,
             "impliedVolatility": 0.28, "bid": 25.0, "ask": 25.5,
             "lastPrice": 25.2, "contractSymbol": "MSFT260731C00395000"},
            {"strike": 400.0, "openInterest": 8421, "volume": 1530,
             "impliedVolatility": 0.242, "bid": 21.1, "ask": 21.4,
             "lastPrice": 21.25, "contractSymbol": "MSFT260731C00400000"},
        ],
        "puts": [
            {"strike": 400.0, "openInterest": 5000, "volume": 900,
             "impliedVolatility": 0.26, "bid": 2.9, "ask": 3.1,
             "lastPrice": 3.0, "contractSymbol": "MSFT260731P00400000"},
        ],
    },
    "source": "yahoo",
}


def _run(args):
    import discord_bot.bot as bot
    from report import market_data as md
    with patch.object(md, "_fetch_yahoo_options_chain", return_value=_RAW):
        return asyncio.run(bot._execute_options_chain(args))


def test_per_strike_call_returns_contract():
    res = _run({"symbol": "MSFT", "expiration": "2026-07-31",
                "strike": 400, "contract_type": "call"})
    c = res.get("contract") or {}
    assert res["status"] == "ok", res
    assert c.get("open_interest") == 8421, res
    assert c.get("volume") == 1530, res
    assert abs((c.get("implied_volatility") or 0) - 0.242) < 1e-6, res
    _ok("per-strike call returns the exact contract OI/volume/IV")


def test_per_strike_put_disambiguates():
    res = _run({"symbol": "MSFT", "strike": 400, "contract_type": "put"})
    c = res.get("contract") or {}
    assert c.get("open_interest") == 5000, f"should be the PUT: {res}"
    _ok("contract_type disambiguates call vs put at the same strike")


def test_missing_strike_clean_status():
    res = _run({"symbol": "MSFT", "strike": 999, "contract_type": "call"})
    assert res["status"] in ("no_strike", "not_found") or "not" in (
        res.get("error", "").lower()), res
    # surfaces nearby strikes so the model can re-ask honestly
    assert res.get("available_strikes"), res
    _ok("absent strike returns a clean status + available strikes")


def test_aggregate_path_unchanged():
    res = _run({"symbol": "MSFT"})
    assert res["status"] == "ok" and "summary" in res, res
    assert "contract" not in res, "no-strike call must stay aggregate"
    _ok("no-strike call still returns the expiration aggregate")


def test_declaration_exposes_strike():
    import discord_bot.bot as bot
    import inspect
    src = inspect.getsource(bot._build_options_chain_tool)
    assert '"strike"' in src and '"contract_type"' in src, (
        "tool declaration must expose strike + contract_type"
    )
    _ok("tool declaration exposes strike + contract_type params")


if __name__ == "__main__":
    print("=== options per-strike smoke ===")
    test_per_strike_call_returns_contract()
    test_per_strike_put_disambiguates()
    test_missing_strike_clean_status()
    test_aggregate_path_unchanged()
    test_declaration_exposes_strike()
    print("\nALL OPTIONS PER-STRIKE SMOKE TESTS PASS")
