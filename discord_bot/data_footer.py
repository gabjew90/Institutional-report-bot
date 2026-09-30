"""The "Data:" footer for /ask answers built from the bot's own feeds.

A grounded answer ends with a numbered Sources list. An answer whose
figures came from a data tool (a price, an earnings date, a Sleeper
score) ended with nothing, so it read as uncited (owner, 2026-09-30:
"some don't have citations"). This names the feeds behind the tools
that returned data on the turn. Rendered from the tool trace in code,
so the model cannot omit or invent it.
"""
from __future__ import annotations

# tool name -> what the reader is told the figures came from
FEEDS: dict[str, str] = {
    "lookup_market_price": "live prices (Finnhub, Binance.US)",
    "lookup_earnings_date": "earnings calendar (Finnhub, Nasdaq)",
    "lookup_earnings_slate": "earnings calendar (Finnhub, Nasdaq)",
    "lookup_options_chain": "options chain (Yahoo)",
    "lookup_price_history": "price history (Yahoo)",
    "lookup_economic_calendar": "economic calendar (Finnhub, ForexFactory, BLS, BEA)",
    "lookup_fantasy_league": "Sleeper league",
    "lookup_user_profile": "room trade ledger",
    "lookup_trade_log": "room trade ledger",
    "lookup_room_positions": "room trade ledger",
    "search_chat_messages": "room chat",
    "query_data": "research database",
}

# Statuses that mean the call produced nothing (mirrors bot._FAILED_TOOL_STATUSES).
_FAILED = frozenset({"no_data", "error", "empty", "not_found", "timeout"})


def footer(tool_trace: list[dict] | None) -> str:
    """'\\n\\nData: a · b' for the feeds that returned data this turn, in
    first-use order and without repeats. Empty when no tool did."""
    seen: list[str] = []
    for entry in tool_trace or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status") or "ok") in _FAILED:
            continue
        label = FEEDS.get(str(entry.get("tool") or ""))
        if label and label not in seen:
            seen.append(label)
    if not seen:
        return ""
    return "\n\nData: " + " · ".join(seen)
