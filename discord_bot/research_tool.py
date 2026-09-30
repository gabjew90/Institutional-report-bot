"""The /ask `lookup_research` tool: what the banks wrote about a ticker.

2026-09-30. The bot holds the deep analysis of every research PDF it
ingests, with a ticker index (`pdf_entities`), yet an opinion question
("what do you think of MU upcoming earnings") was answered from the
earnings date, the price and the room's own chat. The only route to the
research was the raw SQL tool, which the model rarely reached for. This
tool is the direct route, and the router prefetches it for opinion
questions so the bank views arrive before the model writes a word.
"""
from __future__ import annotations

import asyncio
import logging

from discord_bot.tool_docs import TOOL_DOCS as _TOOL_DOCS

log = logging.getLogger(__name__)

DEFAULT_DAYS = 14


def _build_research_tool():
    """FunctionDeclaration for `lookup_research`."""
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_research",
                description=_TOOL_DOCS["lookup_research"] + (
                    "\n\nArgs:\n"
                    "  symbol: one US ticker, e.g. 'MU', 'NVDA'.\n"
                    "  days: lookback in days (default 14, max 60).\n\n"
                    "Response shape: {status, symbol, days, banks: [names], "
                    "notes: [{source, title, report_type, published, "
                    "calls: [{action, rating, price_target, rationale, "
                    "conviction}], earnings: [str], insights: [str], "
                    "trade_ideas: [{description, rationale, risk, "
                    "time_horizon, conviction}]}]}. status=no_data means no "
                    "bank note in the window names the ticker."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "symbol": types.Schema(type=types.Type.STRING,
                                               description="Stock ticker, one per call."),
                        "days": types.Schema(type=types.Type.INTEGER,
                                             description="Lookback in days, default 14."),
                    },
                    required=["symbol"],
                ),
            )
        ]
    )


async def _execute_research(args: dict) -> dict:
    """Run the lookup_research tool call."""
    import db
    symbol = (args.get("symbol") or "").strip().upper()
    if not symbol:
        return {"status": "error", "error": "No symbol provided, re-call with a ticker."}
    try:
        days = int(args.get("days") or DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    days = max(1, min(days, 60))
    try:
        notes = await asyncio.to_thread(db.research_for_ticker, symbol, days)
    except Exception as e:
        log.warning(f"lookup_research: query raised: {e}")
        return {"status": "error", "symbol": symbol,
                "error": "research database read failed. Answer from the other tools "
                         "and say the bank notes could not be read."}
    if not notes:
        return {"status": "no_data", "symbol": symbol, "days": days,
                "error": f"No bank research note in the last {days} days names {symbol}. "
                         f"Say so. Do not invent a desk view."}
    banks = []
    for n in notes:
        if n.get("source") and n["source"] not in banks:
            banks.append(n["source"])
    return {"status": "ok", "symbol": symbol, "days": days, "banks": banks, "notes": notes}
