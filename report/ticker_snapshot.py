"""The ticker snapshot /ask hands the model for any single-stock question.

2026-09-30, owner's swing-trader triage: what the stock is and what drives
it, the next binary event, liquidity, range, float and short interest, and
for a cash-burning company its cash and recent offering registrations.
Every figure is narrated with its date. Nothing here grades a figure
(owner: "this is all discretionary so don't dictate any thresholds just
narrate the data"), so there is no "low float" or "liquid" flag anywhere.

Source is Yahoo through yfinance (quote summary, daily history, and the SEC
filings list Yahoo mirrors, which spares an EDGAR client and its required
contact header). The next earnings date stays with `lookup_earnings_date`,
which the router prefetches beside this.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

CACHE_TTL_S = 15 * 60
_CACHE: dict[str, tuple[float, dict]] = {}

# Registration forms that put new shares or a shelf on file. 424B2 is left
# out: banks file it for every structured note and it is not equity supply.
OFFERING_FORMS = {"S-1", "S-1/A", "S-3", "S-3/A", "S-3ASR", "F-1", "F-3", "F-3ASR", "424B4", "424B5"}
FILINGS_LOOKBACK_DAYS = 365
MAX_FILINGS = 5
SUMMARY_CHARS = 360
ATR_DAYS = 14
GROWTH_BUDGET_S = 5.0   # start the growth reads only if the base fetch took less
ADV_DAYS = 30


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _first_sentences(text: str, limit: int = SUMMARY_CHARS) -> str:
    """Whole sentences of the business summary up to `limit` characters."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    out = ""
    for part in text.split(". "):
        nxt = (out + ". " if out else "") + part
        if len(nxt) > limit:
            break
        out = nxt
    return (out.rstrip(".") + ".") if out else text[:limit].rsplit(" ", 1)[0] + "..."


def history_stats(rows: list[dict]) -> dict:
    """ATR, average volume and dollar volume from daily bars (oldest first),
    each bar {high, low, close, volume}. The last bar is the current
    session, which counts toward relative volume, not the averages."""
    out: dict = {}
    bars = [b for b in rows if _num(b.get("close")) is not None]
    # Before the open Yahoo can end the series with today's row at zero
    # volume; that is not a session yet.
    while bars and not _num(bars[-1].get("volume")):
        bars.pop()
    if len(bars) < 2:
        return out
    trs = []
    for prev, cur in zip(bars, bars[1:]):
        h, l, pc = _num(cur.get("high")), _num(cur.get("low")), _num(prev.get("close"))
        if h is None or l is None or pc is None:
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if trs:
        window = trs[-ATR_DAYS:]
        atr = sum(window) / len(window)
        last = _num(bars[-1]["close"])
        out["atr_days"] = len(window)
        out["atr"] = round(atr, 2)
        if last:
            out["atr_pct"] = round(atr / last * 100, 2)
    done = bars[:-1][-ADV_DAYS:]
    vols = [_num(b.get("volume")) for b in done]
    vols = [v for v in vols if v is not None]
    if vols:
        out["adv_days"] = len(vols)
        out["adv_shares"] = round(sum(vols) / len(vols))
        dollar = [(_num(b.get("close")) or 0) * (_num(b.get("volume")) or 0) for b in done]
        out["adv_dollars"] = round(sum(dollar) / len(dollar))
        today = _num(bars[-1].get("volume"))
        if today is not None and out["adv_shares"]:
            out["today_volume"] = round(today)
            out["relative_volume"] = round(today / out["adv_shares"], 2)
    return out


def offering_filings(filings: list[dict], today: date | None = None) -> list[dict]:
    """Offering registrations in the last year, newest first."""
    today = today or datetime.now(timezone.utc).date()
    since = today - timedelta(days=FILINGS_LOOKBACK_DAYS)
    out = []
    for f in filings or []:
        form = (f.get("type") or "").strip().upper()
        d = f.get("date")
        if isinstance(d, datetime):
            d = d.date()
        elif isinstance(d, str):
            try:
                d = date.fromisoformat(d[:10])
            except ValueError:
                continue
        if form in OFFERING_FORMS and isinstance(d, date) and d >= since:
            out.append({"date": d.isoformat(), "form": form})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:MAX_FILINGS]


def build(symbol: str, info: dict, history: list[dict], filings: list[dict],
          today: date | None = None) -> dict:
    """Assemble the snapshot from already-fetched Yahoo data. Pure, so it is
    tested without the network."""
    sym = (symbol or "").upper()
    qt = (info.get("quoteType") or "").upper()
    if qt and qt != "EQUITY":
        return {"status": "not_a_stock", "symbol": sym, "quote_type": qt,
                "name": info.get("longName") or info.get("shortName")}
    # Yahoo answers an unknown symbol ("APPLE" guessed from "why is apple
    # down") with a near-empty dict rather than an error.
    if not (qt or info.get("longName") or info.get("shortName")) and not history:
        return {"status": "no_data", "symbol": sym}
    price = _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice"))
    hi, lo = _num(info.get("fiftyTwoWeekHigh")), _num(info.get("fiftyTwoWeekLow"))
    snap: dict = {
        "status": "ok", "symbol": sym,
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"), "industry": info.get("industry"),
        "business": _first_sentences(info.get("longBusinessSummary") or ""),
        "market_cap": _num(info.get("marketCap")),
        "price": price, "beta": _num(info.get("beta")),
        "week52_high": hi, "week52_low": lo,
    }
    if price and hi:
        snap["pct_below_52w_high"] = round((hi - price) / hi * 100, 1)
    if price and lo:
        snap["pct_above_52w_low"] = round((price - lo) / lo * 100, 1)
    snap.update(history_stats(history))
    snap.update({
        "float_shares": _num(info.get("floatShares")),
        "shares_outstanding": _num(info.get("sharesOutstanding")),
        "short_pct_float": (round(_num(info.get("shortPercentOfFloat")) * 100, 1)
                            if _num(info.get("shortPercentOfFloat")) is not None else None),
        "days_to_cover": _num(info.get("shortRatio")),
        "total_cash": _num(info.get("totalCash")),
        "total_debt": _num(info.get("totalDebt")),
        "operating_cashflow_ttm": _num(info.get("operatingCashflow")),
        "free_cashflow_ttm": _num(info.get("freeCashflow")),
        "offering_filings_12m": offering_filings(filings, today),
    })
    si = _num(info.get("dateShortInterest"))
    if si:
        snap["short_interest_as_of"] = datetime.fromtimestamp(si, timezone.utc).date().isoformat()
    snap["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Drop absent fields; an empty filings list stays, it is itself a fact.
    return {k: v for k, v in snap.items()
            if k == "offering_filings_12m" or (v is not None and v != "")}


def _pct(v) -> float | None:
    f = _num(v)
    return None if f is None else round(f * 100, 1)


def _recent(quarter_end_iso: str, today: date | None = None, days: int = 75) -> bool:
    """A quarter that ended within `days`: its report is weeks old at most."""
    try:
        q = date.fromisoformat(str(quarter_end_iso)[:10])
    except ValueError:
        return False
    return ((today or datetime.now(timezone.utc).date()) - q).days <= days


def growth_block(rev_est: dict, eps_est: dict, eps_history: list[dict],
                 quarterly_revenue: list[tuple[str, float]], today: date | None = None) -> dict:
    """Growth, the way a trader reads a print (owner, 2026-10-01: "instead
    of absolute dollars ... better to also show YoY growth"). Computed here,
    not by the model.

    rev_est / eps_est: Yahoo's estimate tables keyed by period ('0q' is the
    quarter to be reported next, '0y' the current fiscal year), each row
    {avg, yearAgoRevenue|yearAgoEps, growth, numberOfAnalysts}.
    eps_history: [{quarter, epsActual, epsEstimate, surprisePercent}].
    quarterly_revenue: [(quarter_end_iso, revenue)], any order."""
    out: dict = {}
    for period, key in (("0q", "next_quarter"), ("0y", "fiscal_year")):
        r, e = (rev_est or {}).get(period) or {}, (eps_est or {}).get(period) or {}
        row = {
            "revenue": _num(r.get("avg")), "revenue_year_ago": _num(r.get("yearAgoRevenue")),
            "revenue_growth_pct": _pct(r.get("growth")),
            "eps": _num(e.get("avg")), "eps_year_ago": _num(e.get("yearAgoEps")),
            "eps_growth_pct": _pct(e.get("growth")),
            "analysts": _num(r.get("numberOfAnalysts") or e.get("numberOfAnalysts")),
        }
        if row["eps_year_ago"] is not None and row["eps_year_ago"] <= 0:
            # Growth from a loss or from zero is not a growth rate: -$0.10
            # to $0.20 printed as "-300%". The two figures carry it.
            row["eps_growth_pct"] = None
        row = {k: v for k, v in row.items() if v is not None}
        if row.get("revenue") is not None or row.get("eps") is not None:
            out[key] = row
    reports = []
    for h in sorted(eps_history or [], key=lambda x: str(x.get("quarter")), reverse=True)[:4]:
        act, est = _num(h.get("epsActual")), _num(h.get("epsEstimate"))
        if act is None:
            continue
        reports.append({"quarter": str(h.get("quarter"))[:10], "actual": act, "estimate": est,
                        "surprise_pct": _pct(h.get("surprisePercent"))})
    if reports:
        out["eps_reports"] = reports
        nq = out.get("next_quarter") or {}
        newest = reports[0]
        # Hours after a print Yahoo can still list the reported quarter as
        # "0q". Its consensus then matches the newest report's estimate;
        # calling it the quarter "to be reported next" previews a print
        # that already happened.
        if (nq.get("eps") is not None and newest.get("estimate")
                and abs(nq["eps"] - newest["estimate"]) <= 0.01 * abs(newest["estimate"])
                and _recent(newest["quarter"], today)):
            out.pop("next_quarter", None)
    revs = sorted(((str(q)[:10], _num(v)) for q, v in (quarterly_revenue or []) if _num(v)),
                  reverse=True)
    rev_reports = []
    for i, (q, v) in enumerate(revs[:2]):
        if i + 4 < len(revs) and revs[i + 4][1]:
            rev_reports.append({"quarter": q, "revenue": v,
                                "yoy_pct": round((v / revs[i + 4][1] - 1) * 100, 1)})
    if rev_reports:
        out["revenue_reports"] = rev_reports
    return out


def _fetch_growth(t, sym: str) -> dict:
    """The four Yahoo reads behind growth_block; any one may fail alone."""
    rev_est = eps_est = {}
    history: list[dict] = []
    qrev: list[tuple[str, float]] = []
    try:
        rev_est = _table(t.revenue_estimate)
    except Exception as e:
        log.info(f"snapshot {sym}: revenue estimate failed ({e})")
    try:
        eps_est = _table(t.earnings_estimate)
    except Exception as e:
        log.info(f"snapshot {sym}: eps estimate failed ({e})")
    try:
        eh = t.earnings_history
        history = [{"quarter": str(idx)[:10], **{str(k): v for k, v in row.items()}}
                   for idx, row in eh.iterrows()]
    except Exception as e:
        log.info(f"snapshot {sym}: earnings history failed ({e})")
    try:
        qis = t.quarterly_income_stmt
        if "Total Revenue" in qis.index:
            qrev = [(str(c)[:10], v) for c, v in qis.loc["Total Revenue"].items()]
    except Exception as e:
        log.info(f"snapshot {sym}: quarterly revenue failed ({e})")
    return growth_block(rev_est, eps_est, history, qrev)


def _table(df) -> dict:
    """A yfinance estimate DataFrame as {period: {column: value}}."""
    try:
        return {str(idx): {str(k): v for k, v in row.items()} for idx, row in df.iterrows()}
    except Exception:
        return {}


def fetch(symbol: str) -> dict:
    """Network fetch plus build, cached 15 minutes per symbol."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"status": "error", "error": "No symbol provided."}
    hit = _CACHE.get(sym)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    import yfinance as yf
    t0 = time.monotonic()
    t = yf.Ticker(sym)
    try:
        info = t.info or {}
    except Exception as e:
        log.info(f"snapshot {sym}: info failed ({e})")
        info = {}
    rows: list[dict] = []
    try:
        h = t.history(period="3mo", interval="1d", auto_adjust=False)
        for _, r in h.iterrows():
            rows.append({"high": r.get("High"), "low": r.get("Low"),
                         "close": r.get("Close"), "volume": r.get("Volume")})
    except Exception as e:
        log.info(f"snapshot {sym}: history failed ({e})")
    filings: list[dict] = []
    if (info.get("quoteType") or "EQUITY").upper() == "EQUITY":
        try:
            filings = list(t.sec_filings or [])
        except Exception as e:
            log.info(f"snapshot {sym}: filings failed ({e})")
    snap = build(sym, info, rows, filings)
    # The four growth reads come last and only with time to spare: the
    # executor gives the whole fetch 12 s, and a slow estimate table must
    # cost the growth lines, not the business, float and filings above.
    if snap.get("status") == "ok" and time.monotonic() - t0 < GROWTH_BUDGET_S:
        snap["growth"] = _fetch_growth(t, sym)
    # Cache only a complete read: a snapshot built after the quote summary
    # failed has no business line, float or filings, and serving it for
    # 15 minutes turns one Yahoo hiccup into a quarter hour of thin answers.
    if info and snap.get("status") in ("ok", "not_a_stock"):
        now = time.monotonic()
        for k, (t0, _) in list(_CACHE.items()):     # copied: executor threads write too
            if now - t0 < CACHE_TTL_S:
                continue
            _CACHE.pop(k, None)
        _CACHE[sym] = (now, snap)
    return snap


# ------------------------------------------------------------- rendering

def _money(v: float | None) -> str:
    if v is None:
        return "n/a"
    a = abs(v)
    sign = "-" if v < 0 else ""
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}${a / div:.1f}{unit}"
    return f"{sign}${a:,.0f}"


def _count(v: float | None) -> str:
    if v is None:
        return "n/a"
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:,.0f}"


def _signed(p: float | None) -> str:
    return "n/a" if p is None else f"{p:+.1f}%"


def _eps(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def render_growth(g: dict) -> list[str]:
    """Growth first, the dollar figure beside it."""
    lines = []
    for key, label in (("next_quarter", "quarter to be reported next"),
                       ("fiscal_year", "this fiscal year")):
        row = g.get(key)
        if not row:
            continue
        bits = []
        if row.get("revenue") is not None:
            bits.append(f"revenue {_signed(row.get('revenue_growth_pct'))} y/y "
                        f"({_money(row['revenue'])} vs {_money(row.get('revenue_year_ago'))})")
        if row.get("eps") is not None:
            bits.append(f"EPS {_signed(row.get('eps_growth_pct'))} y/y "
                        f"({_eps(row['eps'])} vs {_eps(row.get('eps_year_ago'))})")
        n = f", {int(row['analysts'])} analysts" if row.get("analysts") else ""
        lines.append(f"consensus, {label}{n}: " + ", ".join(bits))
    if g.get("eps_reports"):
        lines.append("EPS reports, actual vs estimate (newest first): " + "; ".join(
            f"quarter ended {r['quarter']}: {_eps(r['actual'])} vs {_eps(r.get('estimate'))} "
            f"({_signed(r.get('surprise_pct'))})" for r in g["eps_reports"]))
    if g.get("revenue_reports"):
        lines.append("reported revenue: " + "; ".join(
            f"quarter ended {r['quarter']}: {_money(r['revenue'])} ({_signed(r['yoy_pct'])} y/y)"
            for r in g["revenue_reports"]))
    return lines


def _market_open(now: datetime | None = None) -> bool:
    """Regular US session, 9:30 to 16:00 ET on a weekday that is not a
    market holiday."""
    from zoneinfo import ZoneInfo
    et = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    try:
        from world_context import is_us_market_holiday
        if is_us_market_holiday(et.date().isoformat()):
            return False
    except Exception:
        pass
    mins = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= mins < 16 * 60


def render(snap: dict) -> str:
    """The snapshot as a few labelled lines for the model to read."""
    s = snap
    lines = []
    head = f"{s['symbol']}: {s.get('name') or ''}".rstrip(": ")
    what = " / ".join(x for x in (s.get("sector"), s.get("industry")) if x)
    if what:
        head += f" ({what})"
    lines.append(head)
    if s.get("business"):
        lines.append(f"business: {s['business']}")
    if s.get("market_cap"):
        lines.append(f"market cap: {_money(s['market_cap'])}")
    rng = []
    if s.get("week52_low") is not None and s.get("week52_high") is not None:
        rng.append(f"52-week range ${s['week52_low']:,.2f} to ${s['week52_high']:,.2f}")
    if s.get("pct_below_52w_high") is not None:
        rng.append(f"{s['pct_below_52w_high']}% below the high")
    if s.get("pct_above_52w_low") is not None:
        rng.append(f"{s['pct_above_52w_low']}% above the low")
    if rng:
        lines.append("range: " + ", ".join(rng))
    mv = []
    if s.get("atr") is not None:
        mv.append(f"{s['atr_days']}-day average true range ${s['atr']:,.2f} ({s.get('atr_pct')}% of price)")
    if s.get("beta") is not None:
        mv.append(f"beta {s['beta']:.2f} vs the S&P 500")
    if mv:
        lines.append("movement: " + ", ".join(mv))
    if s.get("adv_shares"):
        liq = (f"{s['adv_days']}-day average volume {_count(s['adv_shares'])} shares "
               f"({_money(s.get('adv_dollars'))} a day)")
        if s.get("today_volume") is not None:
            liq += f", latest session {_count(s['today_volume'])} ({s['relative_volume']}x the average"
            liq += "; the session is still open, so this is partial)" if _market_open() else ")"
        lines.append("volume: " + liq)
    fl = []
    if s.get("float_shares"):
        fl.append(f"float {_count(s['float_shares'])} shares")
    if s.get("shares_outstanding"):
        fl.append(f"{_count(s['shares_outstanding'])} outstanding")
    if s.get("short_pct_float") is not None:
        sp = f"short interest {s['short_pct_float']}% of float"
        if s.get("days_to_cover") is not None:
            sp += f", {s['days_to_cover']:.1f} days to cover"
        if s.get("short_interest_as_of"):
            sp += f" (exchange-reported as of {s['short_interest_as_of']})"
        fl.append(sp)
    if fl:
        lines.append("shares: " + ", ".join(fl))
    bs = []
    for key, label in (("total_cash", "cash"), ("total_debt", "debt"),
                       ("operating_cashflow_ttm", "operating cash flow, trailing 12 months"),
                       ("free_cashflow_ttm", "free cash flow, trailing 12 months")):
        if s.get(key) is not None:
            bs.append(f"{label} {_money(s[key])}")
    if bs:
        lines.append("balance sheet: " + ", ".join(bs))
    lines += render_growth(s.get("growth") or {})
    offs = s.get("offering_filings_12m") or []
    if offs:
        lines.append("offering registrations filed in the last 12 months: "
                     + ", ".join(f"{o['form']} on {o['date']}" for o in offs))
    else:
        lines.append("offering registrations filed in the last 12 months: none")
    lines.append(f"(Yahoo, {s.get('as_of', '')})")
    return "\n".join(lines)
