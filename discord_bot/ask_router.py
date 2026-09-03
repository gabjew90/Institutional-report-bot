"""Deterministic /ask router: question shape -> mandatory prefetch -> tool policy.

WHY (2026-09-02, owner: "recommend your long term structural fixes").
Every fabrication and mis-routing finding in the ask-QC queue is one of
two shapes: the model answered a data question from memory, or it
reached for the wrong tool (a slate question to Google, an
earnings-odds question through four chat searches). Both are decided
BEFORE the model writes a word, so the fix belongs before the model:

1. classify the question into a shape with code;
2. for a factual shape, call its tool ourselves and inject the result
   as the authoritative block (the earnings-slate prefetch, generalised);
3. declare only the tools that shape may use, so chat search is not
   reachable from a price question and Google is not reachable from a
   ledger question.

The Gemini intent classifier stays for the shapes this router does not
recognise (banter, opinion, open web questions). The post-hoc grounding
nets and validators stay as the backstop; each shape that lands here
retires prompt text under the /ask enforcement policy.

Deterministic first: everything in this module is regex and tables,
unit-tested against the room's real questions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------- shapes

EARNINGS_SLATE = "earnings_slate"
EARNINGS_DATE = "earnings_date"
PRICE = "price"
OPTIONS_CHAIN = "options_chain"
ECON_CALENDAR = "econ_calendar"
PRICE_HISTORY = "price_history"
COMPANY_PROFILE = "company_profile"
MEMBER_LEDGER = "member_ledger"
CHAT_HISTORY = "chat_history"
FANTASY = "fantasy"
HISTORICAL_STAT = "historical_stat"
NEWS_EVENT = "news_event"
BANTER = "banter"
UNKNOWN = "unknown"

# A ledger or chat lookup is a data question too: it gets the straight-
# answer directive and the asker-mockery strip, not the banter path
# (2026-09-02 review: both shapes were hard-coded BANTER).
FACTUAL_SHAPES = {EARNINGS_SLATE, EARNINGS_DATE, PRICE, OPTIONS_CHAIN, ECON_CALENDAR,
                  PRICE_HISTORY, COMPANY_PROFILE, HISTORICAL_STAT, NEWS_EVENT, FANTASY,
                  MEMBER_LEDGER, CHAT_HISTORY}

# Tool names as declared in discord_bot/ask_tools.py
T_GOOGLE = "google_search"
T_CHAT = "search_chat_messages"
T_PROFILE = "lookup_user_profile"
T_TRADES = "lookup_trade_log"
T_PRICE = "lookup_market_price"
T_CHAIN = "lookup_options_chain"
T_ECON = "lookup_economic_calendar"
T_EDATE = "lookup_earnings_date"
T_SLATE = "lookup_earnings_slate"
T_QUERY = "query_data"
T_HISTORY = "lookup_price_history"
T_FANTASY = "lookup_fantasy_league"
ALL_TOOLS = {T_GOOGLE, T_CHAT, T_PROFILE, T_TRADES, T_PRICE, T_CHAIN, T_ECON, T_EDATE,
             T_SLATE, T_QUERY, T_HISTORY, T_FANTASY}

# Which function tools a shape may see. Google is a separate flag.
TOOL_POLICY: dict[str, set[str]] = {
    EARNINGS_SLATE: {T_SLATE, T_EDATE, T_PRICE},
    EARNINGS_DATE: {T_EDATE, T_PRICE, T_CHAIN},
    PRICE: {T_PRICE, T_HISTORY, T_CHAIN},
    OPTIONS_CHAIN: {T_CHAIN, T_PRICE},
    ECON_CALENDAR: {T_ECON, T_SLATE},
    PRICE_HISTORY: {T_HISTORY, T_PRICE},
    COMPANY_PROFILE: {T_PRICE},
    MEMBER_LEDGER: {T_TRADES, T_QUERY, T_PROFILE, T_PRICE, T_CHAT},
    CHAT_HISTORY: {T_CHAT, T_PROFILE, T_QUERY},
    FANTASY: {T_FANTASY},
    HISTORICAL_STAT: {T_HISTORY},
    NEWS_EVENT: {T_PRICE, T_EDATE, T_CHAIN},
    BANTER: ALL_TOOLS - {T_GOOGLE},
    UNKNOWN: ALL_TOOLS - {T_GOOGLE},
}
GOOGLE_POLICY: dict[str, bool] = {
    EARNINGS_SLATE: False, EARNINGS_DATE: True, PRICE: True, OPTIONS_CHAIN: False,
    ECON_CALENDAR: True, PRICE_HISTORY: False, COMPANY_PROFILE: True,
    MEMBER_LEDGER: False, CHAT_HISTORY: False, FANTASY: False,
    HISTORICAL_STAT: True, NEWS_EVENT: True, BANTER: True, UNKNOWN: True,
}


@dataclass
class Route:
    shape: str
    tickers: list[str] = field(default_factory=list)
    prefetch: list[tuple[str, dict]] = field(default_factory=list)  # (tool, args)
    reason: str = ""

    @property
    def deterministic(self) -> bool:
        return self.shape not in (BANTER, UNKNOWN)

    @property
    def is_factual(self) -> bool:
        return self.shape in FACTUAL_SHAPES

    @property
    def needs_web(self) -> bool:
        return GOOGLE_POLICY.get(self.shape, True) and self.shape in (
            NEWS_EVENT, HISTORICAL_STAT, COMPANY_PROFILE)

    def allowed_tools(self) -> set[str]:
        return set(TOOL_POLICY.get(self.shape, ALL_TOOLS - {T_GOOGLE}))

    def google_allowed(self) -> bool:
        return GOOGLE_POLICY.get(self.shape, True)


# --------------------------------------------------------------- tickers

_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5}(?:\.[A-Za-z])?)\b")
_BARE_RE = re.compile(r"\b([A-Z]{2,5}(?:\.[A-Z])?)\b")
_NOT_TICKERS = {
    "AI", "IT", "US", "USA", "UK", "EU", "CEO", "CFO", "ETF", "IPO", "PMI", "GDP", "CPI",
    "PCE", "PPI", "NFP", "FOMC", "FED", "ISM", "EPS", "ATH", "ATL", "YTD", "QTD", "MTD",
    "AMC", "BMO", "PM", "AM", "ET", "EST", "EDT", "PT", "PST", "PDT", "UTC", "OTM", "ITM",
    "ATM", "IV", "OI", "DTE", "LOL", "LMAO", "OK", "OKAY", "IMO", "TBH", "FYI", "PNL",
    "TA", "FA", "DD", "NY", "NYC", "LA", "SF", "TX", "CA", "DM", "RSI", "MACD", "EMA",
    "SMA", "VWAP", "NDX", "SPX", "VIX", "DJIA", "DOW", "NASDAQ", "NYSE", "SEC", "IRS",
    "MAG", "GOAT", "WSB", "X", "TV", "PC", "AI", "API", "ID", "HR", "PR", "IR", "VP",
    "MD", "PHD", "PPP", "QE", "QT", "ZIRP", "YOLO", "FOMO", "ADR", "REIT", "ROI", "PE",
    "EV", "EBITDA", "ROIC", "FCF", "BTC", "ETH", "SOL",
}
_CRYPTO = {"BTC", "ETH", "SOL"}
# Index names the price tool quotes in Yahoo's caret form. They stay
# tickers here (a "what's SPX at" is a price question) and are mapped
# when the prefetch is built.
INDEX_SYMBOLS = {"SPX": "^GSPC", "NDX": "^NDX", "VIX": "^VIX", "DOW": "^DJI",
                 "DJIA": "^DJI", "RUT": "^RUT"}
_KEEP = _CRYPTO | set(INDEX_SYMBOLS)
_LOWER_KEEP_RE = re.compile(r"\b(btc|eth|sol|spx|ndx|vix|dow|rut)\b", re.I)


def price_symbol(t: str) -> str:
    return INDEX_SYMBOLS.get(t, t)


def extract_tickers(text: str, *, lowercase: bool = True) -> list[str]:
    """Cashtags first; bare uppercase tokens only when nothing is
    cashtagged; lowercase lead-in guesses only when `lowercase` and
    nothing else matched. Never a stopword. Order preserved, deduplicated."""
    text = text or ""
    out: list[str] = []
    for m in _CASHTAG_RE.finditer(text):
        t = m.group(1).upper()
        if t not in out:
            out.append(t)
    if out:
        return out
    for m in _BARE_RE.finditer(text):
        t = m.group(1)
        if t in _NOT_TICKERS and t not in _KEEP:
            continue
        if t not in out:
            out.append(t)
    if out:
        return out
    for m in _LOWER_KEEP_RE.finditer(text):
        t = m.group(1).upper()
        if t not in out:
            out.append(t)
    if out or not lowercase:
        return out
    # The room types tickers in lowercase ("why is mrvl down off avgo
    # earnings", "explain pltr death"). With no cashtag and no uppercase
    # token, take the 2-5 letter word that follows a lead-in verb, unless
    # it is an English word we know.
    for m in _LOWER_LEADIN_RE.finditer(text):
        t = m.group(1).upper()
        if t in _NOT_TICKERS or t.lower() in _COMMON_WORDS:
            continue
        if t not in out:
            out.append(t)
    return out


_LOWER_LEADIN_RE = re.compile(
    r"\b(?:why\s+(?:is|are|did|was)|explain|what(?:'s| is)|how(?:'s| is)|is|odds|off|about|on|in)\s+"
    r"(?:the\s+)?([a-z]{2,5})\b", re.I)
_COMMON_WORDS = {
    "the", "market", "gold", "oil", "this", "that", "it", "he", "she", "they", "we", "my",
    "our", "your", "his", "her", "abe", "kyle", "bk", "jamal", "room", "fed", "rate", "rates",
    "bond", "bonds", "crypto", "btc", "eth", "sol", "tech", "semis", "chips", "china", "japan",
    "death", "dump", "rip", "move", "drop", "pop", "crash", "news", "today", "tmrw", "week",
    "down", "up", "off", "on", "in", "at", "for", "with", "and", "or", "to", "of", "a", "an",
    "was", "were", "is", "are", "did", "does", "do", "so", "not", "no", "yes", "all", "some",
    "big", "small", "cap", "caps", "vol", "vix", "dollar", "yield", "yields", "stock", "stocks",
    "call", "calls", "put", "puts", "trade", "trades", "print", "beat", "miss", "odds", "guy",
    "guys", "man", "bro", "lol", "lmao", "what", "who", "when", "where", "why", "how", "me",
    "you", "him", "them", "there", "here", "now", "then", "still", "just", "even", "only",
    # 2026-09-02 review: "what's the price of gold" made PRICE the ticker
    # and "when is the next fed meeting" made NEXT one, which then
    # blocked the econ route. Ordinary 2-5 letter words after a lead-in.
    "price", "level", "quote", "next", "last", "first", "best", "worst", "going",
    "doing", "mean", "deal", "plan", "point", "data", "same", "much", "many", "more",
    "less", "good", "bad", "new", "old", "real", "true", "sure", "time", "year", "day",
    "days", "hour", "open", "close", "high", "low", "long", "short", "buy", "sell",
    "hold", "way", "thing", "lot", "bit", "kind", "sort", "type", "case", "risk",
    "play", "setup", "story", "take", "read", "view", "idea", "sense", "cost", "worth",
    "value", "cash", "money", "bank", "banks", "gas", "fund", "funds", "etf", "etfs",
    "index", "space", "name", "names", "note", "notes", "war", "jobs", "job", "world",
    "these", "those", "any", "each", "every", "such", "over", "under", "into", "than",
    "again", "after", "before", "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
    "sep", "sept", "oct", "nov", "dec", "march", "april", "june", "july", "cpi", "pce",
    "gdp", "nfp", "ppi", "ism", "fomc", "eps", "ath", "ytd", "vs", "per", "like",
}


# --------------------------------------------------------------- shape regexes

_SLATE_RE = re.compile(
    r"\b(?:who(?:'s|s| is| are)?\s+(?:all\s+)?report(?:s|ing)?"
    r"|(?:reports?|reporting|earnings)\b.{0,40}?\b(?:today|tonight|tomorrow|tmrw?"
    r"|this\s+week|next\s+week|after\s+(?:the\s+)?(?:close|bell)|before\s+(?:the\s+)?(?:open|bell)"
    r"|on\s+deck|slate|lineup|calendar))\b", re.I)
_EDATE_RE = re.compile(
    r"\b(?:when\s+(?:does|is|do|will)\b.{0,30}?\b(?:report|earnings)"
    r"|earnings\s+date|next\s+(?:quarter|earnings|report)"
    r"|did\s+\S+\s+(?:beat|miss)|(?:beat|miss)\s+(?:last\s+quarter|earnings|estimates)"
    r"|(?:expected|consensus|estimates?)\s+(?:for|on)\s+\S+\s+earnings"
    r"|what(?:'s| is)\s+expected\s+for)\b", re.I)
_PRICE_RE = re.compile(
    r"\b(?:what(?:'s| is|s)\s+(?:the\s+)?\S+\s+(?:at|trading\s+at|price|doing|going\s+for)"
    r"|price\s+(?:of|on|for)\b|how(?:'s| is)\s+\S+\s+(?:doing|looking|trading)"
    r"|is\s+\S+\s+(?:green|red|up|down)\b|\b(?:after\s*hours?|premarket|pre-market)\s+(?:print|move|price)"
    r"|current\s+(?:price|level|quote)|where(?:'s| is)\s+\S+\s+(?:at|trading)"
    r"|^\s*\$?\w{2,5}\s+(?:price|quote|level)\s*\??\s*$|quote\s+(?:on\s+|for\s+|me\s+)?\$?[a-z]{1,5}\b)", re.I)
_CHAIN_RE = re.compile(
    r"\b(?:options?\s+chain|open\s+interest|\bOI\b|put[/ -]call|implied\s+vol|\bIV\b"
    r"|straddle|strangle|max\s+pain|gamma|\d{2,5}\s?[cp]\b|(?:calls?|puts?)\s+(?:on|for)\s+\S+"
    r"|\b\d+(?:\.\d+)?\s?(?:c|p|calls?|puts?)\s+(?:exp|expir))", re.I)
_ECON_RE = re.compile(
    r"\b(?:CPI|PCE|PPI|GDP|NFP|non-?farm|payrolls|jobs\s+(?:report|number)|ISM|FOMC|Fed\s+(?:meeting|decision|rate)"
    r"|rate\s+(?:cut|hike|decision)|fed\b.{0,24}?\b(?:cut|cutting|hik|meeting|decision|pause)"
    r"|retail\s+sales|jobless|unemployment|econ(?:omic)?\s+(?:calendar|data|events?|prints?)"
    r"|data\s+(?:this\s+week|today|tomorrow)|powell|warsh)\b", re.I)
_HISTORY_RE = re.compile(
    r"\b(?:how\s+(?:has|did|have)\s+\S+\s+(?:done|performed|traded)|since\s+(?:january|the\s+start|ipo|\d{4})"
    r"|(?:last|past)\s+(?:\d+\s+)?(?:days?|weeks?|months?|years?)\s+(?:chart|performance|return|move)"
    r"|ytd\b|year\s+to\s+date|(?:1|3|6|12)[- ]?month\s+(?:return|performance)|from\s+\$?\d+\s+to\s+\$?\d+)\b", re.I)
_PROFILE_RE = re.compile(
    r"\b(?:what\s+(?:does|do)\s+\S+\s+(?:do|make|sell)|what\s+is\s+\S+\s*\??$|tell\s+me\s+about\s+\S+"
    r"|who\s+(?:is|are)\s+\S+\s*\??$|what\s+(?:kind|type)\s+of\s+company)\b", re.I)
_LEDGER_RE = re.compile(
    r"\b(?:(?:abe|kyle|bk|jamal|his|her|their|my|\w+'s)\s+(?:trades?|book|holdings?|positions?|calls?|win\s*rate|track\s+record|p&?l|pnl|record|ledger|open\s+positions?)"
    r"|win\s*rate|track\s+record|full\s*port(?:ed)?\s+into|current\s+holdings|open\s+positions|trade\s+log)\b", re.I)
_CHAT_RE = re.compile(
    r"\b(?:who\s+said|what\s+did\s+\S+\s+say|room\s+(?:saying|think|consensus)|what(?:'s| is)\s+the\s+room"
    r"|(?:earlier|yesterday|last\s+week)\s+(?:someone|\S+)\s+(?:said|posted|called)"
    r"|quote\s+(?:from\s+)?(?:abe|kyle|bk|jamal|him|her|them|me|\w+'s)\b"
    r"|messages?\s+(?:from|about)|how\s+many\s+(?:messages|times))\b", re.I)
# "what did powell say" is a news question, not a room-history one; the
# chat shape would strip Google and every market tool (2026-09-02 review).
_PUBLIC_FIGURE_RE = re.compile(
    r"\b(?:powell|warsh|fed|trump|musk|elon|jensen|huang|bessent|dimon|zuck(?:erberg)?"
    r"|altman|buffett|lutnick|hassett|waller|the\s+(?:president|treasury|ecb|boj|white\s+house))\b", re.I)
_FANTASY_RE = re.compile(
    r"\b(?:fantasy|sleeper|waiver|matchup|roster|standings|league|faab"
    # Bare "draft" is a league question in this room. DraftKings is not.
    r"|draft(?!\s*kings)\b(?!\s*kings)"
    r"|start\s+or\s+sit|sit\s+or\s+start|who\s+(?:should|do|would)\s+i\s+start"
    r"|free\s+agent|add[/\s]drop|pick\s*up\s+(?:off\s+)?(?:the\s+)?(?:waivers?|wire)"
    r"|flex\s+(?:spot|play|start)|points?\s+against|playoff\s+(?:odds|seed|picture)"
    # "my team" and "first place" are NOT gate words: in a trading room
    # they collide ("my team is bleeding on this trade", "first place in
    # the s&p sectors") and the fantasy shape strips Google and every
    # market tool, so a false positive is expensive.
    r"|trending\s+(?:adds?|drops?)|project(?:ed|ions?)\s+points?)\b", re.I)

# Which Sleeper topic answers the question. The old code prefetched
# standings for every fantasy question, so a waiver or draft ask got
# standings injected as authoritative, and pre-season standings are all
# zeros (2026-09-03 review, same defect class as the week-slate prefetch).
_TOPIC_RES: list[tuple[str, "re.Pattern"]] = [
    ("draft", re.compile(r"\bdraft(?!\s*kings)\b", re.I)),
    ("transactions", re.compile(
        r"\b(?:waivers?|faab|free\s+agent|add[/\s]drop|dropped|pick(?:ed)?\s*up|traded?\s+(?:for|away|to)"
        r"|trade\s+(?:offer|deadline)|wire)\b", re.I)),
    ("trending", re.compile(r"\b(?:trending|most\s+added|hot\s+pick|everyone\s+(?:adding|dropping))\b", re.I)),
    ("projections", re.compile(
        r"\b(?:project(?:ed|ions?)|start\s+or\s+sit|sit\s+or\s+start|who\s+(?:should|do|would)\s+i\s+start"
        r"|flex\s+(?:spot|play|start)|bench|lineup)\b", re.I)),
    ("matchups", re.compile(
        r"\b(?:matchup|who\s+(?:am|is)\s+\w+\s+playing|score(?:s|board)?\s+(?:this|last)\s+week"
        r"|winning\s+this\s+week|beat\s+\w+\s+this\s+week)\b", re.I)),
    ("roster", re.compile(r"\b(?:roster|squad|whos?\s+on\s+\w+(?:'s)?\s+team|my\s+team)\b", re.I)),
    ("league", re.compile(
        r"\b(?:league\s+(?:settings?|rules?|scoring|size|members)"
        r"|who(?:'s|s| is| are)\s+in\s+the\s+league)\b", re.I)),
    ("standings", re.compile(r"\b(?:standings?|records?|first\s+place|last\s+place|who(?:'s| is)\s+(?:winning|leading|best|worst))\b", re.I)),
]


def fantasy_topic(question: str) -> str:
    """The Sleeper topic that answers this question. 'standings' is the
    fallback, not the default for everything."""
    for topic, rx in _TOPIC_RES:
        if rx.search(question or ""):
            return topic
    return "standings"
_STAT_RE = re.compile(
    r"\b(?:how\s+(?:has|does|did)\s+the\s+market\s+(?:do|perform|trade)|market\s+(?:performed?|history|historically)"
    r"|historically|on\s+average|average\s+(?:return|move|gain|loss)|last\s+time\s+(?:both|that|the|\S+\s+and)"
    r"|(?:seasonal|seasonality)|(?:september|october|december|january)\s+(?:effect|returns?|performance)"
    r"|(?:how\s+often|what\s+percent(?:age)?\s+of)\b)", re.I)
_NEWS_RE = re.compile(
    r"\b(?:why\s+(?:is|are|did|was|were)\s+\S+\s+(?:up|down|ripping|dumping|green|red|off|tanking|mooning|falling|rising|moving)"
    r"|what\s+happened\s+(?:to|with)\b|odds\s+\S+\s+(?:beats?|misses?)|(?:beat|miss)\s+odds"
    r"|explain\s+\S+\s+(?:death|dump|rip|crash|move|drop|pop)|what(?:'s| is)\s+(?:going\s+on|the\s+news)\s+with)\b", re.I)
_SINGLE_TICKER_OPINION_RE = re.compile(r"\b(?:thoughts?\s+on|bullish|bearish|buy|sell|long|short)\b", re.I)


def _last_line(question: str) -> str:
    """The actual ask: after any reply/verbatim context blocks."""
    q = (question or "").strip()
    m = re.search(r"\[[^\]]*message to you\]\s*\n(.*)$", q, re.S)
    if m:
        return m.group(1).strip()
    if q.startswith("["):
        q = re.sub(r"^\[.*?\]\s*\n?", "", q, flags=re.S).strip()
        if "\n\n" in q:
            q = q.split("\n\n")[-1].strip()
    return q


def classify(question: str, *, fantasy_enabled: bool = False) -> Route:
    """Shape a question deterministically. Order matters: the more
    specific shape wins, and the ledger/chat shapes beat the data shapes
    when a member is named ("Abe's win rate on semi calls" is a ledger
    question even though it says 'calls')."""
    q = _last_line(question)
    ql = q.lower()
    tickers = extract_tickers(q)
    # Cashtag or uppercase only: a lowercase lead-in guess must not veto
    # the macro route ("when is the next fed meeting").
    strong = extract_tickers(q, lowercase=False)
    r = Route(shape=UNKNOWN, tickers=tickers)
    if not q:
        r.shape = UNKNOWN
        return r
    if fantasy_enabled and _FANTASY_RE.search(q):
        topic = fantasy_topic(q)
        r.shape, r.reason = FANTASY, f"fantasy words -> topic {topic}"
        # A roster needs a manager and the router cannot resolve one from
        # the question. Prefetching it would inject "could not match a
        # manager" as the authoritative block; the model calls the tool
        # itself with the name it reads.
        if topic != "roster":
            r.prefetch = [(T_FANTASY, {"topic": topic})]
        return r
    if _LEDGER_RE.search(q):
        r.shape, r.reason = MEMBER_LEDGER, "member ledger words"
        return r
    if _CHAT_RE.search(q) and not _PUBLIC_FIGURE_RE.search(q):
        r.shape, r.reason = CHAT_HISTORY, "room-history words"
        return r
    if _SLATE_RE.search(q) and not _EDATE_RE.search(q):
        if re.search(r"\b(?:this|next)\s+week\b", ql):
            # The slate tool answers one date. Injecting today's names
            # as the authoritative answer to a week question was wrong
            # (2026-09-02 review); the model calls the tool per day.
            r.shape, r.reason = EARNINGS_SLATE, "week slate: per-day tool, no prefetch"
            return r
        r.shape, r.reason = EARNINGS_SLATE, "who-reports shape"
        date = "tomorrow" if re.search(r"\b(tomorrow|tmrw?)\b", ql) else ""
        r.prefetch = [(T_SLATE, {"date": date})]
        return r
    if _CHAIN_RE.search(q) and (tickers or re.search(r"\b(spy|qqq|iwm)\b", ql)):
        r.shape, r.reason = OPTIONS_CHAIN, "options words + ticker"
        sym = tickers[0] if tickers else re.search(r"\b(spy|qqq|iwm)\b", ql).group(1).upper()
        r.prefetch = [(T_CHAIN, {"symbol": sym})]
        return r
    if _EDATE_RE.search(q) and tickers:
        r.shape, r.reason = EARNINGS_DATE, "single-ticker earnings shape"
        r.prefetch = [(T_EDATE, {"symbol": tickers[0]})]
        return r
    if _NEWS_RE.search(q):
        r.shape, r.reason = NEWS_EVENT, "why/what-happened/odds shape"
        if tickers:
            r.prefetch = [(T_PRICE, {"symbols": [price_symbol(t) for t in tickers[:4]]})]
            if re.search(r"\bodds\b|\bbeat|\bmiss", ql):
                r.prefetch.append((T_EDATE, {"symbol": tickers[0]}))
        return r
    if _ECON_RE.search(q) and not strong:
        r.shape, r.reason = ECON_CALENDAR, "macro print words"
        r.prefetch = [(T_ECON, {"days": 7})]
        return r
    if _HISTORY_RE.search(q) and tickers:
        r.shape, r.reason = PRICE_HISTORY, "history words + ticker"
        r.prefetch = [(T_HISTORY, {"symbol": tickers[0]})]
        return r
    if _STAT_RE.search(q):
        r.shape, r.reason = HISTORICAL_STAT, "historical-statistic shape"
        return r
    if _PRICE_RE.search(q) and tickers:
        r.shape, r.reason = PRICE, "price shape + ticker"
        r.prefetch = [(T_PRICE, {"symbols": [price_symbol(t) for t in tickers[:6]]})]
        return r
    if _PROFILE_RE.search(q) and tickers and not _SINGLE_TICKER_OPINION_RE.search(q):
        r.shape, r.reason = COMPANY_PROFILE, "what-does-X-do shape"
        r.prefetch = [(T_PRICE, {"symbols": [price_symbol(tickers[0])]})]
        return r
    r.shape = UNKNOWN
    return r


def filter_tools(route: Route, tools: list, *, google_tool=None) -> list:
    """Keep only the FunctionDeclaration tools the shape allows; drop the
    Google tool when the policy says so. `tools` is the production list
    (types.Tool objects); an unknown declaration name is kept."""
    allowed = route.allowed_tools()
    out = []
    for t in tools:
        decls = getattr(t, "function_declarations", None)
        if decls:
            names = {d.name for d in decls}
            if names & allowed or not (names & ALL_TOOLS):
                out.append(t)
            continue
        if getattr(t, "google_search", None) is not None:
            if route.google_allowed():
                out.append(t)
            continue
        # code execution and anything else declaration-less stays
        out.append(t)
    return out


def inject_text(tool: str, result: dict) -> str:
    """The authoritative block the model reads for a prefetched tool."""
    import json as _json
    status = (result or {}).get("status", "ok")
    lead = {
        T_SLATE: "EARNINGS SLATE, system-fetched from the same feed as the calendar sheet. "
                 "Authoritative: answer from it, lead with the biggest names, never substitute a search list.",
        T_EDATE: "EARNINGS DATE, system-fetched from the earnings feed. Authoritative for date, timing and consensus.",
        T_PRICE: "LIVE PRICES, system-fetched. The number comes from here; Google may supply the why, never the price.",
        T_CHAIN: "OPTIONS CHAIN, system-fetched. Every OI, volume, IV and strike figure comes from here or is not stated.",
        T_ECON: "ECONOMIC CALENDAR, system-fetched. Print dates, consensus and actuals come from here, never from memory.",
        T_HISTORY: "PRICE HISTORY, system-fetched. Any period return or level path comes from here.",
        T_FANTASY: (
            "LEAGUE STATE, system-fetched from Sleeper for the topic named in the payload. "
            "Authoritative over chat and SQL for that topic. If the question needs a different "
            "slice (draft picks, matchups, a manager's roster, waivers, projections), call "
            "lookup_fantasy_league again with that topic rather than answering from this one. "
            "Pre-season standings are all zeros and are not a draft result."),
    }.get(tool, f"{tool.upper()}, system-fetched. Authoritative.")
    tail = (" status=error or empty means the source is unavailable: say so; do not fill the gap from memory."
            if status not in ("ok",) else "")
    return f"[{lead}{tail}]\n" + _json.dumps(result, default=str)[:6000]
