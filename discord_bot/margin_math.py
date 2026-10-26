"""Margin-call arithmetic computed in code.

2026-10-26 audit A16: "Will Wock get a margin call if he full-ported 25k$
4x margin, NBIS$175, and dram $56?" got the NBIS stock outline and no
arithmetic, twice. The router reads the asker's own numbers (account,
leverage, entry prices), the executor prices the names, and this module
does the rest: the equity ratio now and the drop from entry that triggers
a call at common maintenance levels.
"""
from __future__ import annotations

import re

# Borrowing on margin, not a company's profit margin: "is MU cheap at 10x
# with that gross margin" is a valuation question (code review 2026-10-26).
_MARGIN_RE = re.compile(
    r"\bmargin(?:ed)?[\s-]*call|\bmargin(?:ed)?\s+out\b|\bon\s+margin\b|\bmaintenance\s+margin"
    r"|\b\d+(?:\.\d+)?\s*x\s+(?:margin|leverage|lev|leveraged)\b|\bliquidat\w*", re.I)
_LEV_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*x\b", re.I)
_ACCT_RE = re.compile(r"(?<![\w.$])\$?\s*(\d+(?:\.\d+)?)\s*([km])\b\s*\$?", re.I)
_ENTRY_RE = re.compile(r"(?<![A-Za-z])\$?([A-Za-z]{2,5})\s*(?:@|at\s+)?\s*\$\s*(\d+(?:\.\d+)?)\b")
_NOT_SYMBOLS = {"FOR", "AT", "TO", "AND", "OF", "IS", "WAS", "THE", "BUY", "SOLD", "PAID", "FROM",
                "NEAR", "INTO", "WITH", "ON", "IN", "OR", "BUT", "HIS", "HER", "MY", "ABOUT",
                "AROUND", "BOUGHT", "LOADED", "UNDER", "OVER", "ABOVE", "BELOW"}
# Common broker maintenance levels; brokers raise them on volatile names.
MAINTENANCE = (0.25, 0.30, 0.40)


def parse(text: str) -> dict | None:
    """The asker's margin setup, or None when this is not a margin
    question with a leverage figure in it."""
    t = text or ""
    if not _MARGIN_RE.search(t):
        return None
    lev = next((float(m.group(1)) for m in _LEV_RE.finditer(t)
                if 1.0 < float(m.group(1)) <= 20.0), None)
    if lev is None:
        return None
    account = None
    for m in _ACCT_RE.finditer(t):
        account = float(m.group(1)) * (1_000 if m.group(2).lower() == "k" else 1_000_000)
        break
    positions: list[dict] = []
    for m in _ENTRY_RE.finditer(t):
        sym = m.group(1).upper()
        if sym in _NOT_SYMBOLS or any(p["symbol"] == sym for p in positions):
            continue
        positions.append({"symbol": sym, "entry": float(m.group(2))})
    return {"leverage": lev, "account": account, "positions": positions[:5]}


def call_drop_pct(leverage: float, maintenance: float) -> float:
    """How far the position can fall from entry before equity drops below
    `maintenance`. Zero or negative: already below it at entry. Equity
    ratio after a fall d is 1 - (1 - 1/L) / (1 - d)."""
    return round((1 - (1 - 1 / leverage) / (1 - maintenance)) * 100, 1)


def compute(leverage: float, account: float | None, positions: list[dict],
            prices: dict[str, float]) -> dict:
    """Figures for the answer. Positions are taken as an equal split of
    the gross position, which the answer must say."""
    out: dict = {
        "leverage": leverage,
        "equity_at_entry_pct_of_position": round(100 / leverage, 1),
        "drop_from_entry_that_triggers_a_call_pct": {
            f"{int(m * 100)}% maintenance": call_drop_pct(leverage, m) for m in MAINTENANCE},
        "how_to_read_the_drops": ("a positive number is how far the whole position can fall "
                                  "from entry before the call; zero or negative means equity "
                                  "is already below that maintenance level at entry"),
        "rules": ("US Reg T allows 2x on stock held overnight and 25% minimum maintenance; "
                  "4x is intraday buying power for pattern day traders, so a 4x position held "
                  "overnight is called at the close unless the broker runs portfolio margin. "
                  "Brokers set their own maintenance and raise it on volatile names."),
    }
    if account:
        gross = account * leverage
        out.update({"account_equity": account, "gross_position": gross, "borrowed": gross - account})
    if not positions:
        return out
    out["assumption"] = "the gross position split equally across " + ", ".join(
        p["symbol"] for p in positions)
    share = 1.0 / len(positions)
    rows, ratio, known = [], 0.0, True
    for p in positions:
        px = prices.get(p["symbol"])
        row = {"symbol": p["symbol"], "entry": p["entry"], "price_now": px}
        if px is None:
            known = False
        else:
            row["move_from_entry_pct"] = round((px / p["entry"] - 1) * 100, 1)
            ratio += share * px / p["entry"]
        rows.append(row)
    out["positions"] = rows
    if known:
        # position value now as a fraction of the value at entry
        loan = 1 - 1 / leverage
        out["position_move_from_entry_pct"] = round((ratio - 1) * 100, 1)
        out["equity_now_pct_of_position"] = round((1 - loan / ratio) * 100, 1)
        out["below_maintenance_now"] = {
            f"{int(m * 100)}%": (1 - loan / ratio) < m for m in MAINTENANCE}
        if account:
            value = account * leverage * ratio
            out["position_value_now"] = round(value)
            out["equity_now"] = round(value - (account * leverage - account))
    return out
