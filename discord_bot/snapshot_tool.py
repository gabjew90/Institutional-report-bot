"""The /ask `lookup_ticker_snapshot` tool (2026-09-30): what a stock is and
the trading facts a swing trader checks first, from report/ticker_snapshot.
The router prefetches it for every single-stock question."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from discord_bot.tool_docs import TOOL_DOCS as _TOOL_DOCS

log = logging.getLogger(__name__)

SNAPSHOT_TIMEOUT_S = 12
# Its own small pool: a timeout cancels the await, not the thread, and a
# stalled Yahoo call left in the shared default executor would queue the
# bot's other to_thread work (DB reads, the other tools) behind it.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="snapshot")


def _build_snapshot_tool():
    """FunctionDeclaration for `lookup_ticker_snapshot`."""
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_ticker_snapshot",
                description=_TOOL_DOCS["lookup_ticker_snapshot"] + (
                    "\n\nArgs:\n  symbol: one US stock ticker, e.g. 'MU'.\n\n"
                    "Response shape: {status, symbol, name, sector, industry, business, "
                    "market_cap, price, week52_high, week52_low, pct_below_52w_high, "
                    "pct_above_52w_low, atr, atr_pct, beta, adv_shares, adv_dollars, "
                    "today_volume, relative_volume, float_shares, shares_outstanding, "
                    "short_pct_float, days_to_cover, short_interest_as_of, total_cash, "
                    "total_debt, operating_cashflow_ttm, free_cashflow_ttm, "
                    "offering_filings_12m: [{date, form}], as_of}. Absent fields were not "
                    "reported. status=not_a_stock for an ETF, index or fund."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"symbol": types.Schema(type=types.Type.STRING,
                                                       description="Stock ticker, one per call.")},
                    required=["symbol"],
                ),
            )
        ]
    )


async def _execute_snapshot(args: dict) -> dict:
    """Run the lookup_ticker_snapshot tool call."""
    from report import ticker_snapshot
    symbol = (args.get("symbol") or "").strip().upper().lstrip("$")
    if not symbol:
        return {"status": "error", "error": "No symbol provided, re-call with a ticker."}
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.run_in_executor(_POOL, ticker_snapshot.fetch, symbol),
                                      SNAPSHOT_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"status": "error", "symbol": symbol,
                "error": "Yahoo did not answer in time. Answer without these figures."}
    except Exception as e:
        log.warning(f"lookup_ticker_snapshot {symbol}: {e}")
        return {"status": "error", "symbol": symbol,
                "error": "Snapshot fetch failed. Answer without these figures."}
