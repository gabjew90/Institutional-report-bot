"""The check every trade row passes before it is stored (2026-11-06, run 8
audit design note 2).

Validation used to live in each extraction path (the live watcher, the
member batch, book replies), so a check one path had the others lacked,
and the profile's Recent trades printed whatever reached the table:

  M10  "Fc coin 5", a chart command, stored as COIN shares opened at 5;
       "NBIS at 400", a price remark, stored as an open; MRN, not a
       listed symbol, stored as a trade.
  M11  "Doubling down with Wiz here": Wiz is Wizard of Alt, a member,
       stored as WIZ shares.
  M12  "UNCL 167.5C +71900%" from an image, and "AURA FINAL BOSS".

`reject_reason` returns why a row is not a trade, or "" when it is.
`gain_or_none` blanks an option gain too large to be real when no open
for the contract is logged. db_parts.analyst calls both from every
writer, so a new extraction path gets the checks without asking.
"""
from __future__ import annotations

import re
import threading

# Index option roots and the crypto and futures roots the room trades
# options on (analyst_log.strike_check carries the same list for pricing).
_OPTION_ROOTS = {"SPX", "SPXW", "XSP", "NDX", "NDXP", "RUT", "RUTW", "MRUT", "VIX", "VIXW", "DJX",
                 "BTC", "ETH", "SOL", "XRP", "DOGE", "ES", "NQ", "MES", "MNQ", "YM",
                 "RTY", "CL", "GC", "SI", "ZB", "ZN", "NG", "IBIT", "ETHA"}
# Coins the room trades that need no lookup.
_COINS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC", "BCH",
          "XMR", "SHIB", "PEPE", "BNB", "TRX", "SUI", "HYPE", "TON", "NEAR", "APT",
          "ARB", "OP", "TAO", "WIF", "BONK", "FET", "RNDR", "INJ", "SEI", "TIA", "ENA"}
# A line that is a chart command: "fc coin 5" (the 5 is its timeframe).
_FC_LINE_RE = re.compile(r"(?m)^\s*fc\s+\$?[A-Za-z.]{1,8}\b[^\n]*$", re.I)
# A whole message that is only "TICKER at PRICE" is a remark about the
# price ("Nbis at 400", "mrn at $240"). Anything more ("Slammed nbis at 185
# w margin", "Another 40 at 16.8") is left to the extractor: the 30-day
# dry run on 2026-11-06 found a word list rejected real entries.
_PRICE_REMARK_RE = re.compile(r"^\s*\$?[A-Za-z]{1,6}\s+(?:at|@)\s*\$?\d+(?:\.\d+)?\s*[.!?]*\s*$", re.I)
# A gain this large on an option needs its open in the log (M12: +71900%).
OPTION_GAIN_CAP = 3000.0

_coin_cache: dict[str, bool] = {}
_coin_lock = threading.Lock()


def is_coin(sym: str, lookup: bool = True) -> bool:
    """True when CoinGecko lists a coin with this symbol. Cached per process;
    a failed lookup counts as a coin, so an outage never drops trades.
    `lookup=False` answers from the known list and the cache only (an
    unknown symbol counts as a coin): the live watcher stores trades on the
    Discord event loop, where an HTTP call would stall the gateway."""
    s = (sym or "").upper()
    if s in _COINS:
        return True
    with _coin_lock:
        if s in _coin_cache:
            return _coin_cache[s]
    if not lookup:
        return True
    try:
        from report.market_data import _fetch_json
        import urllib.parse
        found = _fetch_json("https://api.coingecko.com/api/v3/search?query=" + urllib.parse.quote(s.lower()))
        if found is None:
            return True
        hit = any(str(c.get("symbol") or "").upper() == s for c in found.get("coins") or [])
    except Exception:
        return True
    with _coin_lock:
        _coin_cache[s] = hit
    return hit


def _listed(sym: str) -> bool | None:
    try:
        from discord_bot import listed_symbols
        return listed_symbols.is_listed(sym)
    except Exception:
        return None


def _aliases() -> set[str]:
    try:
        import db
        return {a.lower() for a in db.member_aliases()}
    except Exception:
        return set()


def reject_reason(*, ticker: str | None, contract_type: str | None, action: str | None,
                  text: str | None, from_image: bool, aliases: set[str] | None = None,
                  lookup_coins: bool = False) -> str:
    """Why this row is not a trade, or "" when it is. `text` is the
    message's own text (the caption on a screenshot). `lookup_coins` lets
    an unknown symbol be checked against CoinGecko (off on the write path)."""
    sym = (ticker or "").strip().upper().lstrip("$")
    body = text or ""
    ct = (contract_type or "").lower()
    if _FC_LINE_RE.search(body) and not re.search(r"\d", _FC_LINE_RE.sub("", body)):
        # an fc line and no figure anywhere else: "Surprised coin isn't down
        # worse / Fc coin 5" (M10). "bought nvda 200c / fc nvda" keeps its trade.
        return "chart command"
    if not sym:
        return ""
    aliases = (_aliases() if not from_image else set()) if aliases is None else aliases
    if not from_image and sym.lower() in aliases and not re.search(r"\$" + re.escape(sym) + r"\b", body, re.I):
        return f"{sym} is a member's name"
    if sym not in _OPTION_ROOTS:
        listed = _listed(sym)
        if listed is False:
            if ct in ("call", "put"):
                return f"{sym} has no listed options"
            if not is_coin(sym, lookup=lookup_coins):
                return f"{sym} is not a listed symbol or a coin"
    if (not from_image and (action or "").lower() in ("open", "add") and ct not in ("call", "put")
            and _PRICE_REMARK_RE.match(body)):
        return "a price remark, not a trade"
    return ""


def gain_or_none(gain, contract_type: str | None, has_open: bool):
    """The gain to store: an option gain over OPTION_GAIN_CAP with no
    logged open is dropped (the trade stays, its gain is unknown)."""
    try:
        g = float(gain)
    except (TypeError, ValueError):
        return gain
    if (contract_type or "").lower() in ("call", "put") and g > OPTION_GAIN_CAP and not has_open:
        return None
    return gain
