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
ROOM_CROWDING = "room_crowding"
BANTER = "banter"
# A view on one stock ("what do you think of MU into earnings"). The
# bank research is prefetched so the answer leads with named desks, not
# with the room's own chatter (2026-09-30).
TICKER_OPINION = "ticker_opinion"
UNKNOWN = "unknown"

# A ledger or chat lookup is a data question too: it gets the straight-
# answer directive and the asker-mockery strip, not the banter path
# (2026-09-02 review: both shapes were hard-coded BANTER).
FACTUAL_SHAPES = {EARNINGS_SLATE, EARNINGS_DATE, PRICE, OPTIONS_CHAIN, ECON_CALENDAR,
                  PRICE_HISTORY, COMPANY_PROFILE, HISTORICAL_STAT, NEWS_EVENT, FANTASY,
                  MEMBER_LEDGER, CHAT_HISTORY, ROOM_CROWDING}

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
T_ROOM = "lookup_room_positions"
T_RESEARCH = "lookup_research"
T_SNAPSHOT = "lookup_ticker_snapshot"
T_AUCTION = "lookup_treasury_auctions"
# Prefetch only, never declared to the model (it already has Google):
# discord_bot/news_tool.py runs the grounded search in code.
T_NEWS = "ticker_news"
# Prefetch only: discord_bot/primer_tool.py, the stored business primer.
T_PRIMER = "ticker_primer"
ALL_TOOLS = {T_GOOGLE, T_CHAT, T_PROFILE, T_TRADES, T_PRICE, T_CHAIN, T_ECON, T_EDATE,
             T_SLATE, T_QUERY, T_HISTORY, T_FANTASY, T_ROOM, T_RESEARCH, T_SNAPSHOT,
             T_AUCTION}

# Which function tools a shape may see. Google is a separate flag.
# Chat search is a ROOM tool: it appears only where the question is
# about the room (ledger, chat history, fantasy, banter). A stock or
# market question cannot be answered from the room's own chatter, which
# is what the catch-all used to allow (2026-09-30, "what do you think of
# MU upcoming earnings" searched the room for MU earnings).
# Every single-stock shape carries the snapshot and the bank research
# (owner, 2026-09-30: a ticker question, whatever its shape, should say
# what the business is and what drives the number). An options question
# is a view on the stock with a chain attached, so it gets the same
# research, earnings date and Google as a view question; it used to see
# the chain and the price only, and "should i slam calls on ACN
# earnings" never reached Goldman's consensus-short preview.
_STOCK = {T_SNAPSHOT, T_RESEARCH}
TOOL_POLICY: dict[str, set[str]] = {
    EARNINGS_SLATE: {T_SLATE, T_EDATE, T_PRICE},
    EARNINGS_DATE: {T_EDATE, T_PRICE, T_CHAIN} | _STOCK,
    PRICE: {T_PRICE, T_HISTORY, T_CHAIN} | _STOCK,
    OPTIONS_CHAIN: {T_CHAIN, T_PRICE, T_EDATE} | _STOCK,
    ECON_CALENDAR: {T_ECON, T_SLATE, T_AUCTION, T_PRICE},
    PRICE_HISTORY: {T_HISTORY, T_PRICE} | _STOCK,
    COMPANY_PROFILE: {T_PRICE} | _STOCK,
    MEMBER_LEDGER: {T_TRADES, T_QUERY, T_PROFILE, T_PRICE, T_CHAT},
    CHAT_HISTORY: {T_CHAT, T_PROFILE, T_QUERY},
    FANTASY: {T_FANTASY, T_CHAT},
    HISTORICAL_STAT: {T_HISTORY},
    NEWS_EVENT: {T_PRICE, T_EDATE, T_CHAIN} | _STOCK,
    ROOM_CROWDING: {T_ROOM, T_TRADES, T_QUERY, T_PRICE},
    TICKER_OPINION: {T_EDATE, T_PRICE, T_CHAIN} | _STOCK,
    BANTER: ALL_TOOLS - {T_GOOGLE},
    UNKNOWN: ALL_TOOLS - {T_GOOGLE, T_CHAT},
}
GOOGLE_POLICY: dict[str, bool] = {
    EARNINGS_SLATE: False, EARNINGS_DATE: True, PRICE: True, OPTIONS_CHAIN: True,
    ECON_CALENDAR: True, PRICE_HISTORY: False, COMPANY_PROFILE: True, TICKER_OPINION: True,
    # Google is allowed on FANTASY: half the questions in that channel are
    # NFL news (injuries, player outlooks) that the league tool cannot
    # answer. League STATE still comes only from the injected payload,
    # the same split the PRICE shape uses (2026-09-03).
    MEMBER_LEDGER: False, CHAT_HISTORY: False, FANTASY: True,
    HISTORICAL_STAT: True, NEWS_EVENT: True, ROOM_CROWDING: False, BANTER: True, UNKNOWN: True,
}


@dataclass
class Route:
    shape: str
    tickers: list[str] = field(default_factory=list)
    prefetch: list[tuple[str, dict]] = field(default_factory=list)  # (tool, args)
    reason: str = ""
    # Set by the caller when the asker handed over another member's
    # question ("weigh in on this"): whose question the answer is about.
    deferred_author: str = ""
    # Set by the caller when a subjectless question ("what happened") took
    # its subject from the asker's own last messages.
    context_tickers: list[str] = field(default_factory=list)

    @property
    def deterministic(self) -> bool:
        return self.shape not in (BANTER, UNKNOWN)

    @property
    def is_factual(self) -> bool:
        return self.shape in FACTUAL_SHAPES

    @property
    def needs_web(self) -> bool:
        return GOOGLE_POLICY.get(self.shape, True) and self.shape in (
            NEWS_EVENT, HISTORICAL_STAT, COMPANY_PROFILE, TICKER_OPINION)

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
    # The price backstop's own stopword set, folded in 2026-09-17 (review
    # of 6cd427e5): currency codes, agencies, cloud units, reporting
    # shorthand, and shouted words. USD is a real ETF ticker, which is
    # exactly why "1.08 USD" must not become a quote.
    "USD", "EUR", "GBP", "JPY", "YOY", "QOQ", "FDA", "DOJ", "FTC", "AWS", "GCP", "OCI",
    "LLM", "OPEC", "BLS", "BEA", "REV", "RPO", "OTC", "EOD", "MCAP", "AH", "COO", "CTO",
    "ETFS", "PLUS", "AND", "THE", "FOR", "NOT", "ALL",
    # The stock outline's slot labels and hardware acronyms (2026-10-10
    # audit: the price backstop priced PRICE, NEWS and GPU on an NBIS
    # answer). GPU, CPU, TPU and NPU are not listed US stocks; DRAM, HBM
    # and ARR are (the room trades DRAM), so they stay tickers.
    "PRICE", "NEWS", "RESULT", "UPCOMING", "DRIVER", "DESK", "OPTIONS", "POSITIONING",
    "GPU", "GPUS", "CPU", "CPUS", "TPU", "NPU",
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


def extract_tickers(text: str, *, lowercase: bool = True,
                    all_tiers: bool = False) -> list[str]:
    """Cashtags first; bare uppercase tokens only when nothing is
    cashtagged; lowercase lead-in guesses only when `lowercase` and
    nothing else matched. Never a stopword. Order preserved, deduplicated.

    `all_tiers` keeps the bare-token pass even when a cashtag matched:
    the price backstop needs every symbol a sentence asserts a level
    for ("$SPX at 6500 while NVDA sits at $180"), where a question
    router wants the one the asker tagged."""
    text = text or ""
    out: list[str] = []
    for m in _CASHTAG_RE.finditer(text):
        t = m.group(1).upper()
        if t not in out:
            out.append(t)
    if out and not all_tiers:
        return out
    for m in _BARE_RE.finditer(text):
        t = m.group(1)
        if t in _NOT_TICKERS and t not in _KEEP:
            continue
        if t not in out:
            out.append(t)
    if out:
        return out
    if all_tiers:
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
    for m in list(_LOWER_LEADIN_RE.finditer(text)) + list(_LOWER_TRAILING_RE.finditer(text)):
        t = m.group(1).upper()
        if t in _NOT_TICKERS or t.lower() in _COMMON_WORDS:
            continue
        if t not in out:
            out.append(t)
    return out


# "think of mu" / "thoughts on mu" joined the lead-ins 2026-09-30: the
# bare opinion question typed in lowercase had no ticker and fell to the
# catch-all, so the research prefetch never ran.
_LOWER_LEADIN_RE = re.compile(
    r"\b(?:why\s+(?:is|are|did|was)|explain|what(?:'s|s| is)|how(?:'s|s| is)|is|odds|off|about|on|in"
    # "what happened to twst" (2026-10-06: the price came from Google only)
    r"|happened\s+(?:to|with)|news\s+(?:on|for|about|regarding)"
    r"|think\s+(?:of|about)|thoughts?\s+on|(?:bullish|bearish)\s+on|(?:take|view|read)\s+on)\s+"
    r"(?:the\s+)?([a-z]{2,5})\b", re.I)
# "mu thoughts?" / "mu bull or bear?": the ticker precedes the view word,
# which closes the question. Shared with _trailing_view so the uppercase
# and lowercase paths accept the same phrasings.
_TRAILING_VIEW = r"(?:thoughts|bull\s+or\s+bear|long\s+or\s+short)[?!.]*\s*$"
_LOWER_TRAILING_RE = re.compile(r"\b([a-z]{2,5})\s+" + _TRAILING_VIEW, re.I)
_COMMON_WORDS = {
    "the", "market", "gold", "oil", "this", "that", "it", "he", "she", "they", "we", "my",
    "our", "your", "his", "her", "abe", "kyle", "bk", "jamal", "room", "fed", "rate", "rates",
    "bond", "bonds", "crypto", "btc", "eth", "sol", "tech", "semis", "chips", "china", "japan",
    "death", "dump", "rip", "move", "drop", "pop", "crash", "news", "today", "tmrw", "week",
    "down", "up", "off", "on", "in", "at", "for", "with", "and", "or", "to", "of", "a", "an",
    "was", "were", "is", "are", "did", "does", "do", "so", "not", "no", "yes", "all", "some",
    "big", "small", "cap", "caps", "vol", "vix", "dollar", "yield", "yields", "stock", "stocks",
    "call", "calls", "put", "puts", "trade", "trades", "print", "beat", "miss", "odds", "guy",
    "guys", "man", "bro", "lol", "lmao", "wtf", "omg", "bruh", "smh", "idk", "imo", "tbh",
    "what", "who", "when", "where", "why", "how", "me",
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
    # 2026-09-30 review: adjectives that precede a trailing "thoughts?"
    # ("quick thoughts?", "bear thoughts") are not tickers.
    "quick", "bear", "bull", "bears", "bulls", "final", "honest", "other", "early",
    "wild", "crazy", "nice", "few", "two", "three", "ur", "yall", "hot", "fresh",
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
    r"\b(?:options?\s+chain|open\s+interest|\bOI\b|put[/ -]call|implied\s+(?:vol|move)|expected\s+move|\bIV\b"
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
    r"|messages?\s+(?:from|about)|how\s+many\s+messages"
    # "how many times" is the room's history only when someone said or
    # posted something ("how many times do I need to 10x $100" is math,
    # 2026-10-10 audit)
    r"|how\s+many\s+times\b[^?\n]{0,60}\b(?:said|say|says|posted|post|typed|mentioned|used|called|asked))\b",
    re.I)
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
    # "MHJ or Sutton in non PPR" (2026-09-13, gambling channel) went
    # UNKNOWN and the model spent its tool call on lookup_market_price
    # with the asker's message words as tickers. PPR is a fantasy word.
    r"|(?:non[\s-]*)?ppr|half[\s-]*ppr"
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
    # "should I pick him up" wants to know if he is hot, not who moved
    # last week, so add/pickup goes to trending and the retrospective
    # wording goes to transactions.
    ("trending", re.compile(
        r"\b(?:trending|most\s+added|hot\s+(?:pick|add)|everyone\s+(?:adding|dropping)"
        r"|should\s+i\s+(?:pick\s*up|add|grab|claim|stash)|worth\s+(?:adding|picking|grabbing|a\s+claim)"
        r"|pick\s*up\s+\w+\s*\?)\b", re.I)),
    ("transactions", re.compile(
        r"\b(?:waivers?|faab|free\s+agent|add[/\s]drop|dropped|who\s+(?:picked|added|dropped)"
        r"|pick(?:ed)?\s*up|traded?\s+(?:for|away|to)|trade\s+(?:offer|deadline)|wire)\b", re.I)),
    ("projections", re.compile(
        r"\b(?:project(?:ed|ions?)|outlook|rest\s+of\s+season|\bros\b"
        r"|start\s+or\s+sit|sit\s+or\s+start|who\s+(?:should|do|would)\s+i\s+start"
        r"|rank(?:ed|ings?)?|tiers?|best\s+(?:qb|rb|wr|te|flex|option|play)"
        r"|compare|versus|\bvs\.?\b|who(?:'s|s| is)\s+better|better\s+(?:start|play|option|than)"
        r"|flex\s+(?:spot|play|start)|bench|lineup)\b", re.I)),
    ("matchups", re.compile(
        r"\b(?:matchup|who\s+(?:am|is)\s+\w+\s+playing|score(?:s|board)?\s+(?:this|last)\s+week"
        r"|(?:winning|wins?)\s+(?:this|my|the)\s+(?:week|match)|gonna\s+win\s+(?:the\s+)?match"
        r"|beat\s+\w+\s+this\s+week)\b", re.I)),
    ("roster", re.compile(
        r"\b(?:roster|squad|whos?\s+on\s+\w+(?:'s)?\s+team|my\s+team"
        r"|who\s+should\s+i\s+drop|drop\s+for\b)\b", re.I)),
    ("league", re.compile(
        r"\b(?:league\s+(?:settings?|rules?|scoring|size|members)"
        r"|who(?:'s|s| is| are)\s+in\s+the\s+league)\b", re.I)),
    ("standings", re.compile(
        r"\b(?:standings?|records?|first\s+place|last\s+place|best\s+team"
        r"|who(?:'s|s| is)\s+(?:winning|leading|best|worst)"
        r"|(?:gonna|going\s+to)\s+win\s+(?:the\s+)?(?:league|championship|it\s+all))\b", re.I)),
]

# "my", "I", "me": the asker is asking about their own team, and the
# pipeline knows who they are, so the roster and projection lookups can
# be aimed at them instead of coming back "could not match a manager".
_FIRST_PERSON_RE = re.compile(r"\b(?:my|mine|i|me|i'?m)\b", re.I)
# Topics whose payload narrows usefully when a manager is named.
_MEMBER_TOPICS = {"roster", "projections"}


# Channel context (2026-09-03, owner: "if the asker is asking in the
# football channel, it's gonna be about the sleeper fantasy"). Matched on
# the name so a rename that keeps the words, or a second football
# channel, needs no config change.
_FANTASY_CHANNEL_RE = re.compile(r"(?:fantasy|football)", re.I)

# Inside that channel the bar for "this is a league question" drops:
# these words are too generic to gate on globally (start, bench, points,
# my team) but in the football channel there is nothing else they can
# mean. Only consulted when the question is otherwise UNKNOWN, so a
# price or earnings question asked in that channel keeps its own shape.
_FOOTBALL_RE = re.compile(
    r"\b(?:nfl|qb|rb|wr|te|dst|d/st|kicker|touchdown|tds?|snap\s+count|target\s+share"
    r"|injur(?:y|ed|ies)|questionable|doubtful|\bir\b|bye\s+week|handcuff|stream(?:er|ing)?"
    r"|start|sit|bench|flex|lineup|points?|matchup|trade|drop(?:ped|s)?|add(?:ed|s)?|pick(?:ed)?\s*up|claim"
    r"|bust|boom|sleeper|breakout|my\s+team|first\s+place|last\s+place"
    r"|outlook|rank(?:ed|ings?)?|tiers?|compare|versus|\bvs\.?\b|better"
    r"|gonna\s+win|whos?\s+winning|waiver|claim|stash|grab"
    r"|who(?:'s|s| is)\s+(?:winning|losing|best|worst)|record|standings?|playoffs?"
    r"|sunday|monday\s+night|thursday\s+night|red\s*zone|snaps?|targets?|carries"
    # Team names (2026-10-04: "what % chance of winning did Jamal have
    # before this Panthers?" matched nothing). Bare "chance" is not here:
    # "gimme a second chance" (a sex joke, 2026-10-07) got BK's Week 5
    # matchup. Win-chance phrasings go through _WIN_CHANCE_RE.
    r"|odds|probabilit(?:y|ies)|win\s*(?:pct|percentage)"
    # "how do I know if I'm doing well" in the football channel is about
    # the asker's team; it got trading advice (2026-10-07).
    r"|how\s+(?:am\s+i|are\s+we|is\s+my\s+team|'?s\s+my\s+team)\s+doing"
    r"|(?:am\s+i|i'?m|i\s+am|we'?re)\s+doing\s+(?:well|good|ok(?:ay)?|bad(?:ly)?|great|terrible|alright)"
    r"|cardinals|falcons|ravens|bills|panthers|bears|bengals|browns|cowboys|broncos"
    r"|lions|packers|texans|colts|jaguars|jags|chiefs|raiders|chargers|rams|dolphins"
    r"|vikings|patriots|pats|saints|giants|jets|eagles|steelers|49ers|niners|seahawks"
    r"|buccaneers|bucs|titans|commanders)\b", re.I)


_WIN_CHANCE_RE = re.compile(
    r"\b(?:(?:chances?|odds|probabilit(?:y|ies))\s+(?:of|to|at|for)\s+(?:win|winning|pull)"
    r"|win(?:ning)?\s+(?:chances?|odds|probabilit(?:y|ies)|pct|percentage)"
    r"|gonna\s+win|going\s+to\s+win|pull\s+(?:it\s+)?out\s+(?:a\s+|the\s+)?w(?:in)?)\b", re.I)


# "weigh in on this", "thoughts?", "answer him": the asker hands the bot
# someone else's question by replying to it. Route on THAT question and
# answer about ITS author (2026-10-04: BK replied "Weigh in on this." to
# 2Pale's "Any chance my fantasy team is gonna pull out a win?", the bot
# fetched BK's week first and told BK "you're 0-3 with a league-low 270",
# which was 2Pale's record).
_DEFERRAL_RE = re.compile(
    r"^\s*(?:<@!?\d+>\s*)?(?:weigh\s+in|thoughts|answer\s+(?:this|him|her|them|that|it)"
    r"|what\s+do\s+(?:you|u)\s+think|wdyt|ruling|verdict|settle\s+this|you\s+tell\s+(?:him|her|them)"
    r"|help\s+(?:him|her|them)|explain\s+(?:it\s+)?to\s+(?:him|her|them))"
    # only filler may follow: "thoughts on puka?" is a question of its own
    r"(?:\s+(?:on|about|for|with)\s+(?:this|that|it|him|her|them))?"
    r"(?:\s+(?:here|bot|omniwiz|pls|please|lol|bro))*\s*[?.!]*\s*$",
    re.I)
_REPLY_BLOCK_RE = re.compile(
    r"\[MESSAGE BEING REPLIED TO — from (?P<who>[^\]]*?) — user_id (?P<uid>\d+)\]\s*\n"
    r"\"(?P<parent>.*?)\"\s*\n\n\[[^\]\n]*message to you\]\s*\n(?P<own>.*)\Z", re.S)


# A question that names nothing: "what happened", "wtf is going on", "why".
# Its subject is whatever the asker was just looking at (2026-10-02:
# spockbones asked "what happened" right after "Fc stx 5" and "Fc wdc 5",
# and the bot answered with a Cornell story other members had posted).
_SUBJECTLESS_RE = re.compile(
    r"^\s*(?:wtf\s+|what\s+the\s+(?:hell|fuck)\s+)?"
    r"(?:what(?:'s|s| is)?\s+(?:just\s+)?(?:happened|happening|going\s+on)"
    r"|why(?:\s+(?:is|are|did)\s+(?:it|they|this|these|that))?\s*(?:up|down|dumping|ripping|moving)?"
    r"|what\s+happened\s+(?:here|there|today|just\s+now)"
    r"|happened|(?:is\s+)?going\s+on)\s*[?!.]*\s*$", re.I)
_CHART_CMD_RE = re.compile(r"^\s*fc\s+\$?([A-Za-z]{1,5})\b", re.I)


def is_subjectless(question: str) -> bool:
    """The asker's own words name no subject at all."""
    last = _last_line(question)
    return bool(_SUBJECTLESS_RE.match(last)) and not extract_tickers(last)


def recent_subject(messages: list[str], limit: int = 3) -> list[str]:
    """Tickers from the asker's own recent messages, newest first: a chart
    command ('Fc stx 5') or a ticker written in caps or as a cashtag."""
    out: list[str] = []
    for text in messages or []:
        m = _CHART_CMD_RE.match(text or "")
        found = [m.group(1).upper()] if m else extract_tickers(text or "", lowercase=False)
        for t in found:
            if t not in out:
                out.append(t)
        if len(out) >= limit:
            break
    return out[:limit]


def subject_note(tickers: list[str]) -> str:
    """The prompt block that names what a subjectless question is about."""
    cash = ", ".join(f"${t}" for t in tickers)
    return (f"SUBJECT FROM CONTEXT: the asker's question names nothing, and their own "
            f"last messages were about {cash}. Answer about {cash}, not about other "
            f"topics in the chat.")


def deferred_note(author: str) -> str:
    """The prompt block for a handed-over question."""
    return (f"HANDED-OVER QUESTION: the asker is passing on {author}'s question "
            f"(the message they replied to). Answer it about {author}: name "
            f"{author} and use {author}'s numbers. Do not say 'you' for "
            f"{author}'s team or record, and do not give the asker's own "
            f"figures as {author}'s.")


def deferred_question(question: str, bot_user_id: int | None = None) -> dict | None:
    """When the asker's whole message defers to the message they replied
    to, return {'question', 'author', 'user_id'} for that message. None
    otherwise, and never for a reply to the bot's own message."""
    m = _REPLY_BLOCK_RE.search(question or "")
    if not m:
        return None
    uid = int(m.group("uid"))
    if bot_user_id is not None and uid == int(bot_user_id):
        return None
    if not _DEFERRAL_RE.match(_last_line(question)):
        return None
    return {"question": m.group("parent").strip(), "author": m.group("who").strip(),
            "user_id": uid}

# A ledger question in the football channel means the league record, not
# the trade log, unless it names trading material.
_TRADING_LEDGER_RE = re.compile(
    r"\b(?:trades?|trade\s+log|book|holdings?|positions?|calls?|puts?|p&?l|pnl"
    r"|ported|portfolio|shares?|options?|tickers?)\b", re.I)


def in_fantasy_channel(channel_name: str | None, channel_id: int | None = None) -> bool:
    """The league channel, by its ID (a rename cannot break that), or any
    channel whose name says fantasy or football."""
    if channel_id:
        import channel_config
        if int(channel_id) == channel_config.FANTASY_CHANNEL_ID:
            return True
    return bool(_FANTASY_CHANNEL_RE.search(channel_name or ""))


def _as_fantasy(r: "Route", q: str, reason: str, *,
                default_topic: str | None = "standings",
                asker_manager: str = "") -> "Route":
    """Set the fantasy shape and the prefetch its topic calls for.

    `default_topic=None` for the channel fallback: a question that landed
    here only because it was asked in the football channel is a weak
    signal, and player-news asks ("any injury news on CMC") have no
    league topic at all. Injecting standings there would label an
    irrelevant payload authoritative.

    topic='roster' is never prefetched: it needs a manager the router
    cannot resolve, and prefetching injected 'could not match a manager'
    as the authoritative block."""
    topic = fantasy_topic(q, default=default_topic)
    r.shape, r.reason = FANTASY, f"{reason} -> topic {topic or 'none'}"
    prefetch: list[tuple[str, dict]] = []
    # A manager asking anything about the league gets their whole week
    # first (roster with projections and slots, this week's opponent,
    # record, standings). The model analyses from that instead of
    # guessing which slice to ask for (2026-09-03, owner). Draft,
    # transactions, trending and league settings are not in it, so
    # those topics still ride alongside.
    if asker_manager:
        prefetch.append((T_FANTASY, {"topic": "situation", "member": asker_manager}))
        if topic in ("draft", "transactions", "trending", "league"):
            prefetch.append((T_FANTASY, {"topic": topic}))
    else:
        args: dict = {"topic": topic}
        if topic and (topic != "roster"):
            prefetch.append((T_FANTASY, args))
    r.prefetch = prefetch
    return r


def fantasy_topic(question: str, *, default: str | None = "standings") -> str | None:
    """The Sleeper topic that answers this question. `default` is the
    fallback when no topic word appears, not the answer for everything."""
    for topic, rx in _TOPIC_RES:
        if rx.search(question or ""):
            return topic
    return default
_STAT_RE = re.compile(
    r"\b(?:how\s+(?:has|does|did)\s+the\s+market\s+(?:do|perform|trade)|market\s+(?:performed?|history|historically)"
    r"|historically|on\s+average|average\s+(?:return|move|gain|loss)|last\s+time\s+(?:both|that|the|\S+\s+and)"
    r"|(?:seasonal|seasonality)|(?:september|october|december|january)\s+(?:effect|returns?|performance)"
    r"|(?:how\s+often|what\s+percent(?:age)?\s+of)\b)", re.I)
_NEWS_RE = re.compile(
    r"\b(?:why\s+(?:is|are|did|was|were)\s+\S+\s+(?:up|down|ripping|dumping|green|red|off|tanking|mooning|falling|rising|moving)"
    r"|what\s+happened\s+(?:to|with)\b|odds\s+\S+\s+(?:beats?|misses?)|(?:beat|miss)\s+odds"
    r"|explain\s+\S+\s+(?:death|dump|rip|crash|move|drop|pop)|what(?:'s| is)\s+(?:going\s+on|the\s+news)\s+with"
    # Figures a reader expects sourced, not recalled (2026-09-03 ask
    # log: a Kalshi/Polymarket probability and an NVDA shares-outstanding
    # count were answered from memory with no tool and no search after
    # the intent classifier called them banter). Web on, FACT register.
    r"|(?:probability|odds|chances?)\s+(?:of|that|on|according)|according\s+to\s+(?:kalshi|polymarket|the\s+\w+)"
    r"|shares\s+outstanding|market\s+cap(?:italization)?\b|(?:shares?\s+|free\s+)float\b|float\s+(?:of|for)\s+\$?[A-Za-z]{1,5}\b"
    r"|why\s+didn'?t\s+you\s+(?:tell|mention|flag|say)|(?:was|is)\s+there\s+(?:a|an)\s+\w+\s+(?:event|meeting|call|print)\s+today"
    # Elections and votes are news (2026-10-10 audit: "How many votes did
    # Hitler Mussolini win by today in Peru" went to banter with no search
    # and was answered "zero"; he had won a mayoral race by 25 votes).
    r"|how\s+many\s+votes|votes?\s+did\s+\S+|who\s+(?:won|wins|is\s+winning)\s+(?:the\s+)?"
    r"(?:election|race|vote|primary|runoff|referendum|mayor\w*|governor\w*|senate|house\s+seat)"
    r"|(?:election|runoff|primary|referendum)\s+results?"
    r")\b", re.I)
# A correction that states an event is checked, not argued from memory
# (2026-10-10 audit: "Wrong. Hitler Mussolini won." got a second joke). A
# correction about the asker ("nope you lost") or a named room member
# ("nope abe lost that trade", see classify's names_member) is banter.
_CORRECTION_RE = re.compile(
    r"^(?:wrong|false|incorrect|nope|not\s+true|that'?s\s+(?:wrong|false|not\s+true)|fake(?:\s+news)?)\b"
    r"(?![\s.,!]{0,4}(?:you|u|i|we|ur|your|my)\b)"
    r"[^\n]{0,80}\b(?:won|wins|lost|elected|died|passed\s+away|resigned|signed|announced|happened"
    r"|released|reported|confirmed)\b", re.I)
# The room's own book, aggregated (2026-09-04): "what's everyone piled
# into". Must be tested BEFORE the chat shape, whose `what's the room`
# alternative otherwise claims it and searches chat text for ticker
# mentions instead of counting logged positions.
# "What is the room in" asked about now reads the last 3 days of entries, not
# 14 (2026-10-07: "heading into tomorrow" was answered from two weeks).
_NOW_WINDOW_RE = re.compile(
    r"\b(?:right\s+now|today|tonight|tomorrow|tmrw|this\s+week|currently|rn|heading\s+into)\b")
_CROWD_RE = re.compile(
    r"\b(?:piled\s+into|crowded\s+(?:position|trade|name|into)|most\s+crowded"
    # Subject then a position verb. Bare "we ... in" is excluded: "are we
    # in a recession" is a macro question, not the room's book. "we all
    # in" and "same trade/boat" still qualify.
    r"|(?:everyone|everybody|most\s+(?:people|of\s+the\s+room|of\s+us)|the\s+(?:whole\s+)?room|we\s+all)\s+"
    r"(?:all\s+)?(?:in|holding|long|short|positioned\s+in|piled\s+in(?:to)?|loaded\s+(?:in|up\s+on))\b"
    r"|same\s+(?:trade|play|position|boat)|room(?:'s)?\s+(?:book|positioning|positions|exposure)"
    r"|what(?:'s| is)\s+the\s+room\s+(?:in|holding|long|short)|who(?:'s| is|s)\s+(?:all\s+)?in\s+\$?[A-Za-z]{1,5}\b)", re.I)
# Why the ROOM holds a view (2026-10-02: "why they all bullish cbrs" and
# "I'm asking why r people in. Chat bullish" were answered from web news,
# with an invented "heavy retail flow" line; the answer was in the room's
# own positions and messages).
# "people" and "everyone" alone are market participants ("why are people
# buying gold"); only a room marker makes them the room.
_ROOM_WHO = (r"(?:y'?all|yall|the\s+room|(?:the\s+)?chat|they\s+all|u\s+guys|you\s+guys"
             r"|(?:people|everyone|everybody|ppl)\s+(?:in\s+(?:here|chat|the\s+room)|here)"
             r"|(?:people|ppl)\s+in\b(?=.*\bchat\b))")
_ROOM_OPINION_RE = re.compile(
    rf"\bwhy\s+(?:are|r|is|do|does)?\s*{_ROOM_WHO}"
    rf"|\b{_ROOM_WHO}\s+(?:so\s+|all\s+)?(?:bullish|bearish|buying|loading|piling)\b", re.I)
_SINGLE_TICKER_OPINION_RE = re.compile(r"\b(?:thoughts?\s+on|bullish|bearish|buy|sell|long|short)\b", re.I)
# A view on a named stock. "how is X looking" stays a price question
# (_PRICE_RE), so only the "how does X look" form is here.
_OPINION_RE = re.compile(
    r"\b(?:what\s+do\s+(?:you|u|ya)\s+think\s+(?:of|about|on)|thoughts?\s+on"
    r"|(?:your|ur)\s+(?:take|read|view|opinion)\s+on|opinion\s+on|(?:bullish|bearish)\s+on"
    r"|how\s+(?:does|do)\s+\S+\s+look(?:ing)?\b|should\s+i\s+(?:buy|sell|hold|short|long|add|trim)"
    r"|worth\s+(?:buying|a\s+buy|holding|a\s+look)|(?:into|going\s+into|ahead\s+of)\s+(?:the\s+)?(?:print|earnings|er|report)"
    # "is hood a good short here" (2026-10-10 audit: no research or
    # outline ran, and the answer invented a "$106 support floor")
    r"|is\s+\$?[A-Za-z.]{1,6}\s+(?:a\s+)?(?:good\s+|great\s+|solid\s+|decent\s+|smart\s+)?(?:buy|sell|short|long|hold)"
    r"|(?:good|bad|decent)\s+(?:short|long|buy|entry|spot)\s+(?:here|now|at|rn)"
    r"|how\s+(?:do|does|will)\s+\S+\s+(?:do|hold\s+up|fare|trade)\s+(?:into|on|after)\s+earnings)\b", re.I)


def _trailing_view(q: str, tickers: list[str]) -> str | None:
    """The ticker that directly precedes a closing view word ("MU
    thoughts?", "$MU bull or bear?"), or None. Tied to the ticker so a
    price question that happens to end in "thoughts?" is not claimed."""
    for t in tickers:
        if re.search(r"(?<![A-Za-z])\$?" + re.escape(t) + r"\s+" + _TRAILING_VIEW, q, re.I):
            return t
    return None


# Funds and indices have no business line, float or bank coverage.
_NOT_STOCKS = {"SPY", "QQQ", "IWM", "DIA", "TLT", "GLD", "SLV", "USO", "UVXY", "VXX", "SQQQ",
               "TQQQ", "SOXL", "SOXS", "SMH", "SOXX", "XLE", "XLF", "XLK", "ARKK", "IBIT", "HYG",
               "EEM", "FXI", "KRE", "GDX", "VOO", "VTI"}
_BARE_TICKER_Q_RE = re.compile(r"^\s*\$?[A-Za-z]{1,5}\s*[?!.]*\s*$")
_RESULT_Q_RE = re.compile(
    r"\b(?:beat|beats|miss|missed|did|results?|reported|guid(?:e|ance)|numbers|how\s+(?:did|was))\b", re.I)


def is_stock(sym: str) -> bool:
    s = (sym or "").upper()
    return bool(s) and s not in _CRYPTO and s not in INDEX_SYMBOLS and s not in _NOT_STOCKS


def _stock_prefetch(sym: str, *, research: bool = True, news: bool | None = None) -> list:
    """Bank research, recent news and the snapshot for a single stock;
    nothing for a fund, an index or a coin. News follows research unless
    set: a price read does not need it, a view or a print does."""
    if not is_stock(sym):
        return []
    want_news = research if news is None else news
    out = []
    if research:
        out.append((T_RESEARCH, {"symbol": sym, "days": 14}))
    if want_news:
        out.append((T_NEWS, {"symbol": sym}))
    out.append((T_SNAPSHOT, {"symbol": sym}))
    # A price or date lookup does not wait on a cold primer build; it
    # starts one for the next question.
    out.append((T_PRIMER, {"symbol": sym, "wait": bool(research)}))
    return out


_VERBATIM_BLOCK_RE = re.compile(
    r"\[VERBATIM RECENT MESSAGES[^\]]*\]\s*\n(?:[ \t]+.*(?:\n|$))*\s*")


def _last_line(question: str) -> str:
    """The actual ask: after any reply/verbatim context blocks. A quoted
    member block can sit between '[X's message to you]' and the asker's
    words (2026-10-04, 'Weigh in on this.'), so it is stripped here too:
    routing must read the asker, not the people quoted to them.

    Curly quotes become straight ones: phones send "Abe’s" and "I’m", and
    every pattern here is written with "'" (2026-10-06/07: "Abe’s record"
    missed the ledger, "I’m doing well" missed the football channel)."""
    q = (question or "").strip()
    m = re.search(r"\[[^\]]*message to you\]\s*\n(.*)$", q, re.S)
    if m:
        return _straight(_VERBATIM_BLOCK_RE.sub("", m.group(1)).strip())
    if q.startswith("["):
        q = re.sub(r"^\[.*?\]\s*\n?", "", q, flags=re.S).strip()
        if "\n\n" in q:
            q = q.split("\n\n")[-1].strip()
    return _straight(q)


def asker_text(question: str) -> str:
    """The asker's own typed words, without the reply parent or the quoted
    member blocks. Every check that decides something from "what the asker
    said" reads this (2026-10-10 audit: the slur-count shortcut scored a
    quoted block of another member's messages, which held "how many
    times" and a slur, and answered BK's "thoughts?" with a slur tally)."""
    return _last_line(question)


_CURLY = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def _straight(q: str) -> str:
    return q.translate(_CURLY)


def classify(question: str, *, fantasy_enabled: bool = False,
             channel_name: str = "", asker_manager: str = "",
             channel_id: int | None = None, names_member: bool = False) -> Route:
    """Shape a question deterministically. Order matters: the more
    specific shape wins, and the ledger/chat shapes beat the data shapes
    when a member is named ("Abe's win rate on semi calls" is a ledger
    question even though it says 'calls')."""
    q = _last_line(question)
    ql = q.lower()
    in_channel = fantasy_enabled and in_fantasy_channel(channel_name, channel_id)
    tickers = extract_tickers(q)
    # Cashtag or uppercase only: a lowercase lead-in guess must not veto
    # the macro route ("when is the next fed meeting").
    strong = extract_tickers(q, lowercase=False)
    r = Route(shape=UNKNOWN, tickers=tickers)
    if not q:
        r.shape = UNKNOWN
        return r
    if fantasy_enabled and _FANTASY_RE.search(q):
        return _as_fantasy(r, q, "fantasy words", asker_manager=asker_manager)
    if _LEDGER_RE.search(q):
        if in_channel and not _TRADING_LEDGER_RE.search(q):
            # "what's Declan's record" in the football channel is the
            # league standing, not the trade log.
            return _as_fantasy(r, q, "ledger words in the football channel",
                               asker_manager=asker_manager)
        r.shape, r.reason = MEMBER_LEDGER, "member ledger words"
        return r
    # a "why" is about reasons even when it also names positions
    if _ROOM_OPINION_RE.search(q) and (re.search(r"\bwhy\b", ql) or not _CROWD_RE.search(q)):
        # WHY the room holds a view is in what it said: chat search, with
        # what it holds fetched up front. Crowding ("what's everyone in")
        # stays a count of logged positions, never a chat search.
        r.shape, r.reason = CHAT_HISTORY, "why the room holds a view"
        days = 3 if _NOW_WINDOW_RE.search(ql) else 14
        r.prefetch = [(T_ROOM, {"days": days})]
        return r
    if _CROWD_RE.search(q):
        r.shape, r.reason = ROOM_CROWDING, "room positioning words"
        days = 3 if _NOW_WINDOW_RE.search(ql) else 14
        r.prefetch = [(T_ROOM, {"days": days})]
        return r
    if _CHAT_RE.search(q) and not _PUBLIC_FIGURE_RE.search(q):
        r.shape, r.reason = CHAT_HISTORY, "room-history words"
        # "how many times has Abe said 'slam'": search before the first
        # model call. When the content filter blocked that call, the retry
        # dropped the tools and the bot said it could not search chat
        # (2026-10-06).
        term = _quoted_term(q)
        if term:
            r.prefetch = [(T_CHAT, {"keyword": term, "days": 90})]
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
    # "what WAS the implied move" is a post-print question: the chain now
    # prices the next expiry, so the answer lives in chat or the web and
    # the shape must keep those tools (2026-09-03 ask log, LULU).
    _past_move = re.search(r"\b(?:was|were|had)\b.{0,30}\b(?:implied|expected)\s+move", ql)
    if _CHAIN_RE.search(q) and not _past_move and (tickers or re.search(r"\b(spy|qqq|iwm)\b", ql)):
        r.shape, r.reason = OPTIONS_CHAIN, "options words + ticker"
        sym = tickers[0] if tickers else re.search(r"\b(spy|qqq|iwm)\b", ql).group(1).upper()
        earnings = is_stock(sym) and bool(re.search(r"\b(?:earnings|print|report|er)\b", ql))
        # About a print: the chain must be the first expiry that covers it
        # (2026-10-06: GS's earnings move was priced on the Oct 9 expiry
        # for an Oct 13 report).
        chain_args = {"symbol": sym, "through_earnings": True} if earnings else {"symbol": sym}
        r.prefetch = [(T_CHAIN, chain_args)] + _stock_prefetch(sym)
        if earnings:
            r.prefetch.append((T_EDATE, {"symbol": sym}))
        return r
    trailing = _trailing_view(q, tickers) if tickers else None
    if tickers and (_OPINION_RE.search(q) or trailing):
        r.shape, r.reason = TICKER_OPINION, "view words + ticker"
        sym = trailing or tickers[0]
        r.prefetch = _stock_prefetch(sym) + [(T_PRICE, {"symbols": [price_symbol(sym)]})]
        if re.search(r"\b(?:earnings|print|report|er)\b", ql):
            r.prefetch.append((T_EDATE, {"symbol": sym}))
        return r
    if _EDATE_RE.search(q) and tickers:
        r.shape, r.reason = EARNINGS_DATE, "single-ticker earnings shape"
        # A result question ("did PLTR beat") needs the news; a date
        # lookup ("when does NVDA report") does not pay for a search.
        r.prefetch = [(T_EDATE, {"symbol": tickers[0]})] + _stock_prefetch(
            tickers[0], research=False, news=bool(_RESULT_Q_RE.search(q)))
        return r
    # In the football channel a win-chance question is about the league
    # before it is a prediction-market question (2026-10-04: "what % chance
    # of winning did Jamal have" took the news shape, no league data).
    if in_channel and _WIN_CHANCE_RE.search(q):
        r = _as_fantasy(r, q, "win-chance words in the football channel",
                        default_topic="matchups", asker_manager=asker_manager)
        # Every game with its win estimate: "what were Jamal's odds" is
        # about another manager's game, and situation holds only the
        # asker's.
        if (T_FANTASY, {"topic": "matchups"}) not in r.prefetch:
            r.prefetch.append((T_FANTASY, {"topic": "matchups"}))
        return r
    if _NEWS_RE.search(q):
        r.shape, r.reason = NEWS_EVENT, "why/what-happened/odds shape"
        if tickers:
            r.prefetch = [(T_PRICE, {"symbols": [price_symbol(t) for t in tickers[:4]]})]
            if len(tickers) == 1:
                r.prefetch += _stock_prefetch(tickers[0])
            if re.search(r"\bodds\b|\bbeat|\bmiss", ql):
                r.prefetch.append((T_EDATE, {"symbol": tickers[0]}))
        else:
            # "why is memory down" names a group, not a ticker: price the
            # group's bellwethers and search the news on the first one
            # (2026-10-06: no tools ran, the move size was never given).
            basket = sector_basket(ql)
            if basket:
                r.tickers = basket
                r.prefetch = [(T_PRICE, {"symbols": basket}), (T_NEWS, {"symbol": basket[0]})]
        return r
    # `names_member`: the caller found a room member named in the asker's
    # words, so the correction is about the room, not the news.
    if _CORRECTION_RE.search(q) and not names_member:
        r.shape, r.reason = NEWS_EVENT, "correction states an event"
        return r
    # Treasury auction results come from TreasuryDirect (2026-10-07: "how
    # did the 10-year auction go" two minutes after the close got no result
    # and a market yield that read as the auction's).
    if _AUCTION_RE.search(q) and (auction_term(q) or _TREASURY_WORD_RE.search(q)):
        r.shape, r.reason = ECON_CALENDAR, "treasury auction"
        r.prefetch = [(T_AUCTION, {"term": auction_term(q)})]
        return r
    if _ECON_RE.search(q) and not strong:
        r.shape, r.reason = ECON_CALENDAR, "macro print words"
        r.prefetch = [(T_ECON, {"days": 7})]
        return r
    if _HISTORY_RE.search(q) and tickers:
        r.shape, r.reason = PRICE_HISTORY, "history words + ticker"
        r.prefetch = [(T_HISTORY, {"symbol": tickers[0]})] + _stock_prefetch(tickers[0], research=False)
        return r
    if _STAT_RE.search(q):
        r.shape, r.reason = HISTORICAL_STAT, "historical-statistic shape"
        return r
    if _PRICE_RE.search(q) and tickers:
        r.shape, r.reason = PRICE, "price shape + ticker"
        r.prefetch = [(T_PRICE, {"symbols": [price_symbol(t) for t in tickers[:6]]})]
        if len(tickers) == 1:
            r.prefetch += _stock_prefetch(tickers[0], research=False)
        return r
    if _PROFILE_RE.search(q) and tickers and not _SINGLE_TICKER_OPINION_RE.search(q):
        r.shape, r.reason = COMPANY_PROFILE, "what-does-X-do shape"
        r.prefetch = [(T_PRICE, {"symbols": [price_symbol(tickers[0])]})] + _stock_prefetch(tickers[0])
        return r
    # A bare ticker ("MU?", "$mu", "aeva??") is a view question: the room
    # asks that way and it fell to the catch-all with no data at all.
    # Cashtag or capitals only, and never a word we know ("BK?" is a
    # member, "WHAT?" is a word); a lowercase "huh?" stays banter.
    if (strong and is_stock(strong[0]) and _BARE_TICKER_Q_RE.match(q)
            and ("$" in q or strong[0].lower() not in _COMMON_WORDS)):
        tickers = strong
        r.shape, r.reason = TICKER_OPINION, "bare ticker"
        r.prefetch = _stock_prefetch(tickers[0]) + [(T_PRICE, {"symbols": [tickers[0]]})]
        return r
    # Last: in the football channel a question nothing else claimed is a
    # league question if it carries any football word. Words too generic
    # to gate on globally (start, bench, points, my team) are safe here.
    # Pure banter still falls through to the classifier.
    if in_channel and _FOOTBALL_RE.search(q):
        return _as_fantasy(r, q, "football words in the football channel",
                           default_topic=None, asker_manager=asker_manager)
    # A stock question no pattern above claimed still gets the stock data
    # (2026-10-06: "why is WDC hammered today" and "any news on SNAP" fell
    # to the catch-all with no price, snapshot or dated news). Needs a
    # market word or a cashtag, or a question about a carried ticker, so
    # "WTF?" and a "lol" reply to a stock answer stay banter.
    # A follow-up that names no ticker takes the one in the message it
    # replies to (2026-10-07: "the earnings numbers" 16 minutes after APLD
    # printed got consensus, "so is dogshit" got no data on CRWV). Only
    # here, after every other shape had its turn with the asker's words.
    carried = False
    if not tickers:
        parent = reply_parent_tickers(question)
        if parent:
            strong, carried = parent[:1], True
    if (strong and is_stock(strong[0]) and strong[0].lower() not in _COMMON_WORDS
            and ("$" in q or _MARKET_WORD_RE.search(q) or (carried and "?" in q))):
        r.tickers = strong
        r.shape, r.reason = TICKER_OPINION, (
            "ticker from the replied-to message" if carried else "ticker with no other shape")
        r.prefetch = _stock_prefetch(strong[0]) + [(T_PRICE, {"symbols": [strong[0]]})]
        if re.search(r"\b(?:earnings|print|report(?:ed|s)?|numbers|er)\b", ql):
            r.prefetch.append((T_EDATE, {"symbol": strong[0]}))
        return r
    # A reply to the bot's own trade-log post ("📝 Logged: OPEN TSLA 400C")
    # stays banter, but the trade's price comes from the feed, not a search
    # snippet (2026-10-10 audit: TSLA and RDDT were quoted from YouTube,
    # Kraken and Fidelity pages, RDDT "around $150" while it traded 153-156).
    logged = _LOGGED_PARENT_RE.search(question or "") if not tickers else None
    if logged:
        r.tickers = [logged.group(1)]
        r.prefetch = [(T_PRICE, {"symbols": [price_symbol(logged.group(1))]})]
    r.shape = UNKNOWN
    return r


# The log post's layout: the action word, then the bolded symbol
# ("OPEN **TSLA 400C 10-16**", "TRIM **RDDT 160C 10-16**").
_LOGGED_PARENT_RE = re.compile(
    r"\[MESSAGE BEING REPLIED TO[^\]]*\]\s*\n\"📝 Logged:[^\n]*?"
    r"\b(?:OPEN|ADD|TRIM|CLOSE|EXIT)\s+\*\*\$?([A-Z][A-Z.]{0,5})\b")


# Words that make a capitalised token a stock question. Everyday words
# (up, down, red, buy, cash) are left out: "SV up early?" is about a member.
_MARKET_WORD_RE = re.compile(
    r"\b(?:rip(?:ping|ped)|dump(?:ing|ed)|hammered|smoked|nuked|tank(?:ing|ed)?"
    r"|moon(?:ing)?|crash(?:ing|ed)?|pump(?:ing|ed)?|squeez(?:e|ing)|stock|shares?"
    r"|earnings|revenue|sales|guidance|news|chart|calls?|puts?|price\s+target|pt"
    r"|(?:market\s+)?cap|worth|debt|valuation|float|dilution|offering|iv|options?"
    r"|a\s+buy|a\s+sell|bull(?:ish)?|bear(?:ish)?)\b", re.I)


def reply_parent_tickers(question: str) -> list[str]:
    """Tickers the replied-to message names, cashtag or capitals only.
    One or two distinct ones only: a wider answer is not one subject."""
    m = re.search(r"\[MESSAGE BEING REPLIED TO[^\]]*\]\s*\n(.*?)(?=\n\[[^\]\n]*message to you\]|\Z)",
                  question or "", re.S)
    if not m:
        return []
    found = [t for t in extract_tickers(m.group(1), lowercase=False)
             if is_stock(t) and t.lower() not in _COMMON_WORDS]
    return found if 1 <= len(found) <= 2 else []


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


def inject_text(tool: str, result: dict, has_images: bool = False) -> str:
    """The authoritative block the model reads for a prefetched tool.

    `has_images` matters for exactly one shape today. A fantasy question
    prefetches the asker's OMNIBETA Sleeper roster and hands it over as
    authoritative, which is right until the asker attaches a screenshot
    of a DIFFERENT league and says "rate my team". On 2026-09-09 two of
    three such asks came back describing the same three Sleeper players
    (Mahomes, Etienne, Stevenson) for two different screenshots of two
    different leagues, and the asker re-asked with "using the screenshot
    attached" and got the same answer again. The payload outranked the
    picture it was supposed to be read beside.
    """
    import json as _json
    status = (result or {}).get("status", "ok")
    lead = {
        T_SLATE: "EARNINGS SLATE, system-fetched from the same feed as the calendar sheet. "
                 "Authoritative: answer from it, lead with the biggest names, never substitute a search list.",
        T_EDATE: "EARNINGS DATE, system-fetched from the earnings feed. Authoritative for date, timing and consensus.",
        T_PRICE: "LIVE PRICES, system-fetched. The number comes from here; Google may supply the why, never the price.",
        T_CHAIN: ("OPTIONS CHAIN, system-fetched. Every OI, volume, IV and strike figure comes from here or is "
                  "not stated. implied_move_dollars and implied_move_pct are the at-the-money straddle: the "
                  "move the market prices either way by that expiration. Say it that way; a bare IV "
                  "percentage means nothing to the reader."),
        T_ECON: ("ECONOMIC CALENDAR, system-fetched. Print dates, consensus and actuals come from here, never from memory. "
                 "A row with status past_no_data has NO print in the feed yet: say the number has not reached "
                 "the feed, give consensus and prior as such, and never fill the actual from memory or from the "
                 "previous month."),
        T_HISTORY: "PRICE HISTORY, system-fetched. Any period return or level path comes from here.",
        T_RESEARCH: ("INSTITUTIONAL RESEARCH ON THE TICKER, system-fetched from the bank notes "
                     "the bot ingested. This is the primary source for a view question. One arrow "
                     "per bank view, and every arrow carries the desk's own numbers from the "
                     "`calls`, `earnings`, `insights` and `data_points` fields: the estimate the "
                     "desk expects against the Street figure, the target or margin or cash figure "
                     "it cites, the positioning it flags (consensus long, expectations high after "
                     "the last beat), and what it says would break the call. A direction with no "
                     "number is not an answer, and 'bullish on AI demand' or 'structural tightness' "
                     "is not a reason. Where desks disagree, state the disagreement and what each "
                     "side is counting on. Attribute every view to its bank. Say each view in your "
                     "own plain words, tied to the business line that moves the number (from the "
                     "TICKER SNAPSHOT): never carry a desk's shorthand or an acronym the payload "
                     "does not explain; if you cannot explain a term, leave it out. Answer the "
                     "question asked: on a general stock question an upcoming print is one arrow, "
                     "not the whole answer. A note dated before a print that has since happened "
                     "is a preview: say the print happened and give the result from Google. "
                     "The room's chat is not a source for this. status=no_data: say no bank "
                     "note covers the name and give the public view, never a made-up desk call."),
        T_NEWS: ("RECENT NEWS ON THE TICKER, from a Google search the system ran just now; "
                 "each line is dated and names its publisher. A result, guidance or event here "
                 "supersedes any bank note written before it: if the company has reported, lead "
                 "with what it reported against the estimates. Name the publisher for anything "
                 "you take from it. status=no_data: nothing dated was found; do not fill it in."),
        T_PRIMER: ("BUSINESS PRIMER, a stored plain-English description of the company built from "
                   "a web search. Use its DRIVERS line to say which part of the business is driving "
                   "the figure being discussed (a beat, a guide, a margin, a move) and why: name "
                   "that segment and what it sells, as the reason for the figure, not a list of "
                   "segments, and never a code or acronym."),
        T_SNAPSHOT: ("TICKER SNAPSHOT, system-fetched from Yahoo. What the company does and the "
                     "trading facts on it. Use the figures that bear on the question, each with "
                     "its date where it has one (short interest is exchange-reported twice a "
                     "month). Narrate, never grade: no 'low float', 'liquid', 'squeeze setup', "
                     "'safe' or 'risky'; the reader judges. Use the business line and industry to "
                     "say what drives any metric you discuss. Give an earnings figure as its "
                     "growth first (y/y, or the beat or miss against the estimate, both computed "
                     "here) with the dollar figure beside it; a dollar total alone tells the "
                     "reader nothing."),
        T_AUCTION: ("TREASURY AUCTIONS, system-fetched from TreasuryDirect: results (high yield = "
                    "where it cleared, bid-to-cover, who bought) with the previous auction of the "
                    "same tenor, and the schedule. Authoritative for any auction figure; a market "
                    "yield is not an auction result."),
        T_CHAT: ("CHAT SEARCH, system-fetched from the room's messages for the word the asker "
                 "named. Count and quote from these rows; the search ran, so never say you "
                 "cannot search chat."),
        T_ROOM: ("ROOM POSITIONING, system-fetched from the member trade ledger. Counts are distinct "
                 "members by author_id who LOGGED AN ENTRY (open/add); members_exited is who posted a "
                 "close. The ledger is entry-biased (exits are posted far less often than entries): "
                 "say 'N entered X' and give members_entered_not_exited as an upper bound, "
                 "never as who holds it now; when exits outnumber entries the story is the room "
                 "getting out. Do not add names from chat."),
        T_FANTASY: (
            "LEAGUE STATE, system-fetched from Sleeper for the topic named in the payload. "
            "Authoritative over chat and SQL for that topic. Answer the question that was asked "
            "from it, with the analysis it calls for (a start/sit is a call with the swap and the "
            "projection gap, a matchup read is lineup against lineup, an outlook names the stakes), "
            "not a recital of the rows. If the question needs a slice this payload lacks (draft "
            "picks, waivers, trending adds, another manager's roster), call lookup_fantasy_league "
            "again with that topic rather than answering from this one. "
            "Pre-season standings are all zeros and are not a draft result. "
            "Google may supply player news, injuries and outlooks, never league state. "
            "NUMBERS OR IT IS NOT AN ANSWER: every player you rate carries this "
            "week's projected points, a rank, or a record. For players NOT in this "
            "league (a screenshot of another league, names in the question), call "
            "lookup_fantasy_league topic=projections, which returns this week's "
            "pts_ppr for EVERY NFL player and not just this league's rosters, and "
            "rate them off those. 'Elite', 'high-floor' and 'league-winning' with no "
            "figure beside them are adjectives, not analysis."),
    }.get(tool, f"{tool.upper()}, system-fetched. Authoritative.")
    tail = (" status=error or empty means the source is unavailable: say so; do not fill the gap from memory."
            if status not in ("ok",) else "")
    if has_images:
        # Every prefetch is injected as authoritative, and none of them
        # were fetched by looking at the picture. A screenshot is part of
        # the question whatever it holds: a roster, a bet slip, a chart,
        # a fill, a stat line, an article.
        tail += (" AN IMAGE IS ATTACHED TO THIS TURN and is part of the "
                 "question. This payload was fetched by the system and "
                 "does not describe the image. Read the image and answer "
                 "about what it actually shows; where the two disagree, "
                 "the image is the subject and this payload is background.")
        if tool == T_FANTASY:
            tail += (" Specifically: this payload is the asker's OMNIBETA "
                     "league, so a roster in the image is a different team.")
    if tool == T_RESEARCH and (result or {}).get("status") == "ok":
        # A per-bank digest, not nested JSON: on 2026-09-30 the model
        # summarized "$56.3B / 86.5% GM / $35.71 EPS, JPM expects the
        # guide well above" down to "structural tightness" when it read
        # the raw dump. Figures written out in prose get copied.
        return f"[{lead}{tail}]\n" + render_research(result)[:7000]
    if tool == T_NEWS and (result or {}).get("status") == "ok":
        pubs = ", ".join(s.get("title") or "" for s in result.get("sources") or [] if s.get("title"))
        return (f"[{lead}{tail}]\nNEWS ON {result.get('symbol')}:\n{result.get('digest')}"
                + (f"\n(searched: {pubs})" if pubs else
                   "\n(the search returned no links: attribute a line only to the publisher it "
                   "names, and do not present a line with no publisher as confirmed)"))
    if tool == T_PRIMER:
        if (result or {}).get("status") == "ok":
            return f"[{lead}{tail}]\nBUSINESS OF {result.get('symbol')}:\n{result.get('primer')}"
        return ""        # not built yet: nothing to say, and nothing to imply
    if tool == T_SNAPSHOT:
        if (result or {}).get("status") == "ok":
            from report.ticker_snapshot import render as _render_snapshot
            return f"[{lead}{tail}]\n" + _render_snapshot(result)
        # A guessed symbol that is a word ("apple") or a fund: one quiet
        # line, not an authoritative "unavailable" block that reads as
        # "there is no data on this company".
        return (f"[TICKER SNAPSHOT: none for {(result or {}).get('symbol') or 'this symbol'} "
                f"({(result or {}).get('status')}); it may be a fund or not a ticker. "
                f"This says nothing about the company.]")
    return f"[{lead}{tail}]\n" + _json.dumps(result, default=str)[:6000]


FRESH_PRINT_DAYS = 3


def fresh_print(snapshot: dict | None, today=None, now=None) -> dict | None:
    """The snapshot's last report when it came out in the last
    FRESH_PRINT_DAYS (New York calendar) and carries an actual and an
    estimate. Failing that, a scheduled report whose time has already
    passed but whose figures Yahoo has not filled yet (the first hours
    after a release): returned with `actual` None, so the notes are still
    marked previews while the print guard, which needs the figure, stays
    off. Else None."""
    from datetime import date, datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    et_now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    today = today or et_now.date()
    growth = (snapshot or {}).get("growth") or {}
    sym = (snapshot or {}).get("symbol")

    def _date(row):
        try:
            return date.fromisoformat(str(row.get("date"))[:10])
        except ValueError:
            return None
    last = growth.get("last_report") or {}
    d = _date(last) if last else None
    if (d and last.get("actual") is not None and last.get("estimate") is not None
            and timedelta(0) <= today - d <= timedelta(days=FRESH_PRINT_DAYS)):
        return {**last, "symbol": sym}
    nxt = growth.get("next_report") or {}
    nd = _date(nxt) if nxt else None
    if nd and nd <= today and today - nd <= timedelta(days=FRESH_PRINT_DAYS):
        released = nd < today or (
            nxt.get("session") == "before the open" and et_now.hour * 60 + et_now.minute >= 570
        ) or (nxt.get("session") == "after the close" and et_now.hour * 60 + et_now.minute >= 975)
        if released:
            return {"date": nxt["date"], "session": nxt.get("session") or "", "actual": None,
                    "estimate": nxt.get("estimate"), "symbol": sym}
    return None


def _is_preview(note: dict, fresh: dict) -> bool:
    """Written before the report: dated earlier, or dated the same day as
    an after-the-close report."""
    pub, rep = str(note.get("published") or "")[:10], str(fresh.get("date") or "")[:10]
    if not pub or not rep:
        return False
    return pub < rep or (pub == rep and fresh.get("session") != "before the open")


def render_research(result: dict) -> str:
    """The research payload as one paragraph per note: bank, date, title,
    then its calls, earnings lines, insights, figures, trade ideas and
    risks about the ticker, each on its own line."""
    sym = result.get("symbol") or ""
    out = [f"BANK RESEARCH ON {sym}, last {result.get('days', 14)} days, "
           f"{len(result.get('notes') or [])} notes from {', '.join(result.get('banks') or [])}."]
    fresh = result.get("fresh_print") or {}
    if fresh:
        # 2026-09-30/10-01: MU was answered twice from "bullish into the
        # print" previews after it had reported. Marked in code, from the
        # report date, so the label does not depend on the model noticing.
        head = f"RESULTS ARE OUT: {sym} reported {fresh['date']} {fresh.get('session', '')}".rstrip()
        if fresh.get("actual") is not None and fresh.get("estimate") is not None:
            head += f": EPS ${fresh['actual']:,.2f} vs ${fresh['estimate']:,.2f} estimated."
        else:
            head += ". The figures are not in the data yet: take them from the news block."
        out.append(head + " Notes marked PREVIEW were written before that report: give them as "
                   "what the desks expected, set against the result, never as the outlook into a "
                   "print still to come.")
    for n in result.get("notes") or []:
        tag = " PREVIEW, written before the report" if fresh and _is_preview(n, fresh) else ""
        out.append(f"\n{n.get('source')} ({n.get('published')}){tag}: {n.get('title')}")
        for c in n.get("calls") or []:
            pt = c.get("price_target")
            bits = [c.get("action"), c.get("rating"),
                    f"PT {pt}" if pt and str(pt).upper() != "N/A" else None,
                    f"{c['conviction']} conviction" if c.get("conviction") else None]
            head = " · ".join(b for b in bits if b and b != "N/A")
            out.append(f"  call: {head}. {c.get('rationale') or ''}".rstrip())
        for e in n.get("earnings") or []:
            out.append(f"  earnings: {e}")
        for i in n.get("insights") or []:
            out.append(f"  insight: {i}")
        for d in n.get("data_points") or []:
            ctx = f" ({d['context']})" if d.get("context") else ""
            out.append(f"  figure: {d.get('figure')} {d.get('metric')}{ctx}")
        for t in n.get("trade_ideas") or []:
            out.append(f"  trade idea: {t.get('description')}. {t.get('rationale') or ''} "
                       f"Risk: {t.get('risk') or 'not stated'}.")
        for r in n.get("risks") or []:
            out.append(f"  risk: {r}")
    return "\n".join(out)


# A group the room names instead of a ticker -> its bellwethers, a stock
# first (the news search runs on it), the group's ETF last.
SECTOR_BASKETS: list[tuple["re.Pattern", list[str]]] = [
    (re.compile(r"\b(?:memory|dram|nand|hbm)\b"), ["MU", "SNDK", "WDC", "STX"]),
    (re.compile(r"\b(?:semis?|semiconductors?|chips?|chip\s+stocks)\b"), ["NVDA", "AVGO", "AMD", "SMH"]),
    (re.compile(r"\b(?:banks?|financials)\b"), ["JPM", "BAC", "GS", "XLF"]),
    (re.compile(r"\b(?:energy|oil\s+stocks|oil\s+names)\b"), ["XOM", "CVX", "XLE"]),
    (re.compile(r"\b(?:software|saas)\b"), ["MSFT", "CRM", "NOW", "IGV"]),
    (re.compile(r"\b(?:homebuilders?|housing\s+stocks)\b"), ["DHI", "LEN", "XHB"]),
    (re.compile(r"\b(?:quantum)\b"), ["IONQ", "RGTI", "QBTS"]),
    (re.compile(r"\b(?:nuclear|uranium)\b"), ["CCJ", "OKLO", "SMR"]),
    (re.compile(r"\b(?:solar)\b"), ["FSLR", "ENPH", "TAN"]),
    (re.compile(r"\b(?:biotech)\b"), ["XBI", "IBB"]),
]


def sector_basket(ql: str) -> list[str]:
    for rx, syms in SECTOR_BASKETS:
        if rx.search(ql or ""):
            return list(syms)
    return []


# Double quotes, or single quotes that are not apostrophes ("Abe's crew
# said 'slam'" must give slam, not "s crew said ").
_QUOTED_RE = re.compile(r"[\"“]([^\"”]{2,30})[\"”]|(?<![A-Za-z])['‘]([^'’]{2,30})['’](?![A-Za-z])")
_SAID_WORD_RE = re.compile(
    r"\b(?:say|says|said|saying|type[ds]?|post(?:s|ed)?|use[ds]?|call(?:s|ed)?)\s+"
    r"(?:the\s+word\s+)?([a-z][a-z0-9]{2,20})\b", re.I)
_NOT_TERMS = {"that", "this", "something", "anything", "about", "it", "the", "what", "when"}


def _quoted_term(q: str) -> str:
    """The word a chat-count question asks about: quoted, or after say/said."""
    m = _QUOTED_RE.search(q or "")
    if m:
        return (m.group(1) or m.group(2)).strip()
    m = _SAID_WORD_RE.search(q or "")
    if m and m.group(1).lower() not in _NOT_TERMS:
        return m.group(1)
    return ""


_PROPER_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z]{3,})\b")


def subject_terms(question: str) -> list[str]:
    """Tickers and proper names the asker's own words name: the subjects an
    earlier answer may already have covered."""
    q = _last_line(question)
    out = [t for t in extract_tickers(q, lowercase=False) if t.lower() not in _COMMON_WORDS]
    for m in _PROPER_RE.finditer(q):
        w = m.group(1)
        if w.lower() not in _COMMON_WORDS and w not in out:
            out.append(w)
    return out[:4]


_AUCTION_RE = re.compile(r"\b(?:auction|auctions|bid[\s-]*to[\s-]*cover|tailed|stop[\s-]*through)\b", re.I)
_TENOR_RE = re.compile(r"\b(\d{1,2})\s*(?:-\s*)?(?:year|yr|y)\b|\b(\d{1,2})\s*(?:-\s*)?(?:week|wk)\b", re.I)


# An auction is a Treasury one only with a tenor or a bond word: "how much
# did the Christie's auction make" is not.
_TREASURY_WORD_RE = re.compile(
    r"\b(?:treasury|treasuries|bonds?|notes?|bills?|tips|tail(?:ed)?|bid[\s-]*to[\s-]*cover"
    r"|stop[\s-]*through|refunding)\b", re.I)


def auction_term(q: str) -> str:
    """'10-Year' from "10y auction", '13-Week' from "13 week bills", else ''."""
    m = _TENOR_RE.search(q or "")
    if not m:
        return ""
    return f"{m.group(1)}-Year" if m.group(1) else f"{m.group(2)}-Week"
