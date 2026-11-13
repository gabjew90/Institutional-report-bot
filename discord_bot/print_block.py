"""The result of a report, written in code (2026-11-13, owner: "build it in code").

After a print the model was handed the figures in an outline slot and
reworded them. On 2026-11-12 one question showed five defects in that
arrow alone: an estimate a cent off the one a check then added beneath
it, "beating consensus estimates" with no figure, "±$20¢", two source
tags joined, and slang. Each got a patch. The figures are in the feeds,
so the lines are rendered here, posted as written, and the model writes
arrows only for what the feeds do not carry: the business line, an
analyst's action, a desk's view, the options (stock_outline.build_outline
`posted`).

Lines, in order:

1. the price in the session's terms (price_block.quote_line);
2. EPS against its estimate and against the same quarter a year before,
   both from Yahoo's earnings dates;
3. revenue and its growth: Yahoo's quarterly revenue once it carries the
   quarter, else the calendar feed's reported figure against Yahoo's
   year-ago quarter (report/ticker_snapshot.py :: print_revenue). The
   revenue consensus is Yahoo's while it still lists the reported
   quarter. After that no feed carries it, and the estimate is taken from
   a news line only when that line gives the same revenue the feed does
   and names the figure as the estimate;
4. the guidance clauses of the news lines, as the search wrote them.

No block without the printed EPS, its estimate and the revenue: the
outline's RESULT slot and the print guard handle that turn as before.
"""
from __future__ import annotations

import re
from datetime import date

MAX_GUIDANCE_LINES = 2
NEWS_REVENUE_MATCH = 0.01      # the news line's revenue is the feed's, to rounding
NEWS_ESTIMATE_BAND = 0.20      # an estimate sits near the figure it is an estimate of

_UNIT = {"b": 1e9, "m": 1e6}
_MONEY_RE = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s*(billion|million|bn|mn|[BM])\b", re.I)
_REVENUE_WORD_RE = re.compile(r"\b(?:revenues?|sales)\b", re.I)
_EST_BEFORE_RE = re.compile(
    r"(?:estimates?|consensus|expectations?|forecasts?|expected|projected)\s+"
    r"(?:(?:of|at|for|was|were)\s+)?(?:(?:about|around|roughly)\s+)?$", re.I)
_EST_AFTER_RE = re.compile(
    r"^\s*(?:consensus\s+|analyst\s+|street\s+)?(?:estimates?|consensus|expectations?|forecasts?|expected)\b",
    re.I)
_BOLD_MONEY_RE = re.compile(r"(?<![±*\d])\$\d[\d,]*(?:\.\d+)?(?:\s?(?:trillion|billion|million|[TBM])\b)?")


def _eps(v: float) -> str:
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def _money(v: float) -> str:
    from discord_bot.earnings_block import _money as money
    return money(v)


def _day(iso: str) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
        return f"{d:%b} {d.day}"
    except ValueError:
        return str(iso)[:10]


def _change(now: float, ago: float) -> str:
    """'rose **12.0%** from a year earlier', or the year-ago figure itself
    when a rate would mean nothing (a loss on either side)."""
    if ago <= 0 or now < 0:
        return ""
    pct = (now / ago - 1) * 100
    if abs(pct) < 0.05:
        return "was flat with a year earlier"
    return f"{'rose' if pct > 0 else 'fell'} **{abs(pct):.1f}%** from a year earlier"


def _against(actual: float, estimate: float, est_text: str, pct: float | None = None) -> str:
    """'beat the **$4.05** estimate by **2.6%**'."""
    if actual == estimate:
        return f"matched the **{est_text}** estimate"
    by = f" by **{abs(pct):.1f}%**" if pct is not None else ""
    return f"{'beat' if actual > estimate else 'missed'} the **{est_text}** estimate{by}"


def eps_line(fresh: dict, symbol: str = "") -> str:
    a, e = float(fresh["actual"]), float(fresh["estimate"])
    pct = fresh.get("surprise_pct")
    if pct is None and e:
        pct = (a - e) / abs(e) * 100
    session = f" {fresh['session']}" if fresh.get("session") else ""
    who = f"**{symbol}** reported" if symbol else "Reported"
    text = f"→ {who} {_day(fresh.get('date') or '')}{session}: EPS **{_eps(a)}** " + _against(a, e, _eps(e), pct)
    ago = fresh.get("eps_year_ago")
    if ago is not None:
        change = _change(a, float(ago))
        text += f" and {change}" if change else f", against **{_eps(float(ago))}** a year earlier"
    return text + "."


def news_revenue_estimate(news_lines: list[str], revenue: float) -> tuple[float, str, str] | None:
    """(the estimate, as the line wrote it, the line's publisher) from a
    news line that states the reported revenue and the estimate it was set
    against. The line must give the feed's revenue, and the second figure
    must sit beside an estimate word and near the first: "up from $6.80
    billion a year ago" is not an estimate."""
    from discord_bot.news_tool import _PUBLISHER_RE
    for raw in news_lines or []:
        pm = _PUBLISHER_RE.search(raw)
        publisher = pm.group(1).strip() if pm else ""
        body = raw[:pm.start()] if pm else raw
        word = _REVENUE_WORD_RE.search(body)
        if not word:
            continue
        found = list(_MONEY_RE.finditer(body, word.end()))
        for i, m in enumerate(found[:-1]):
            x = float(m.group(1)) * _UNIT[m.group(2)[0].lower()]
            if abs(x - revenue) > NEWS_REVENUE_MATCH * revenue:
                continue
            nxt = found[i + 1]
            if nxt.start() - m.end() > 100:
                break
            y = float(nxt.group(1)) * _UNIT[nxt.group(2)[0].lower()]
            beside = (_EST_BEFORE_RE.search(body[m.end():nxt.start()])
                      or _EST_AFTER_RE.search(body[nxt.end():]))
            if beside and abs(y - x) <= NEWS_ESTIMATE_BAND * x:
                return y, f"${nxt.group(1)}{nxt.group(2)[0].upper()}", publisher
            break
    return None


def revenue_line(fresh: dict, news_lines: list[str] | None = None) -> str:
    rev = float(fresh["revenue"])
    text = f"→ Revenue **{_money(rev)}**"
    parts = []
    est = fresh.get("revenue_estimate")
    if est:
        parts.append(_against(rev, float(est), _money(float(est)), (rev - float(est)) / float(est) * 100))
    else:
        cited = news_revenue_estimate(news_lines or [], rev)
        if cited:
            y, y_text, publisher = cited
            parts.append(_against(rev, y, y_text) + (f" cited by {publisher}" if publisher else ""))
    ago = fresh.get("revenue_year_ago")
    change = _change(rev, float(ago)) if ago else ""
    if change:
        parts.append(change)
    if not parts:
        return text + "."
    if len(parts) == 1 and parts[0] is change:
        # "Revenue $10.41B, up 53.1% from a year earlier."
        return text + ", " + change.replace("rose", "up").replace("fell", "down").replace("was flat", "flat") + "."
    return text + " " + " and ".join(parts) + "."


def guidance_lines(news_lines: list[str], since: str = "") -> list[str]:
    """The guidance clauses of the news lines, as written: capitalised,
    dollar figures bold, the publisher kept."""
    from discord_bot.stock_outline import _split_news
    out = []
    for clause in _split_news(news_lines or [], cut_moves=True, since=since)["guidance"]:
        clause = clause.strip()
        if not clause:
            continue
        clause = _BOLD_MONEY_RE.sub(lambda m: f"**{m.group(0)}**", clause[0].upper() + clause[1:])
        out.append("→ " + clause.rstrip(".") + ".")
    return out[:MAX_GUIDANCE_LINES]


def build(symbol: str, fresh: dict | None, snapshot: dict | None = None,
          price: dict | None = None, news: dict | None = None) -> dict | None:
    """{lines, has_price, eps, price} for a print the feeds carry in full,
    else None. Plain data: the ask log serializes _ask_meta."""
    symbol = (symbol or "").upper()
    f = fresh or {}
    if not symbol or f.get("actual") is None or f.get("estimate") is None or not f.get("revenue"):
        return None
    from discord_bot.stock_outline import MAX_NEWS_LINES
    news_lines = []
    if (news or {}).get("status") == "ok":
        news_lines = [ln for ln in (news.get("digest") or "").splitlines() if ln.strip()][:MAX_NEWS_LINES]
    lines, quoted = [], None
    for q in (price or {}).get("quotes") or []:
        if str(q.get("symbol") or "").upper() == symbol and q.get("price") is not None:
            from discord_bot.price_block import quote_line
            lines.append(quote_line(q, str((price or {}).get("session") or "")))
            quoted = float(q["price"])
            break
    lines.append(eps_line(f, "" if lines else symbol))
    lines.append(revenue_line(f, news_lines))
    lines.extend(guidance_lines(news_lines, since=str(f.get("date") or "")))
    return {"lines": [ln for ln in lines if ln], "has_price": quoted is not None,
            "eps": float(f["actual"]), "price": quoted, "symbol": symbol,
            "revenue_source": f.get("revenue_source") or ""}


def _has_figure(text: str, v: float) -> bool:
    """`v` as a dollar amount to the cent ('$4.16'; '$1.4' for $1.40), not
    a rounded '$4' and not another quantity with the same digits ('$1.4B',
    '1.4%')."""
    v = abs(v)
    forms = {f"{v:,.2f}", f"{v:.2f}"}
    if round(v, 1) == round(v, 2):
        forms |= {f"{v:,.1f}", f"{v:.1f}"}
    return any(re.search(r"\$\s?" + re.escape(f) + r"(?!\d)(?!\.\d)"
                         r"(?!\s?(?:trillion|billion|million|bn|mn|[TBMK]\b|%))", text) for f in forms)


def compose(answer: str, block: dict) -> tuple[str, int]:
    """(the posted lines followed by the model's arrows, how many of its
    arrows were dropped). An arrow is dropped when it restates the lines:
    it carries the printed EPS or the quoted price, or says nothing the
    lines do not."""
    from discord_bot.answer_block import _NOT_A_LEAD, adds_nothing
    from discord_bot.data_footer import strip_model_sources
    from discord_bot.figure_provenance import _lines
    body = "\n\n".join(block["lines"])
    kept, dropped = [], 0
    for part in _lines(strip_model_sources(answer or "").strip()):
        part = part.strip()
        if not part or part.startswith(_NOT_A_LEAD):
            continue
        if (_has_figure(part, block["eps"])
                or (block.get("price") is not None and _has_figure(part, block["price"]))
                or adds_nothing(part, body, "")):
            dropped += 1
            continue
        kept.append(part)
    return "\n\n".join([body, *kept]), dropped
