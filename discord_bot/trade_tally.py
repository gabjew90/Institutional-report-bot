"""A win rate or record from the trade log, counted and written in code
(2026-11-20, audit run 13 design 3).

"Do a calculation of the win rate of the member channel is the past
month" (2026-11-19) was answered with the two callers' tallies, because
the only tally the model could ask for was a caller's. An hour later
"what is the percent of returns on puts this discord has called out this
month" was answered from SQL the model wrote: "162 put positions, 67 wins
and 4 losses, 90 unclosed", which are row counts (one position is several
rows). The room: "that has to be inaccurate".

Like a chat count (discord_bot/chat_count.py), a record is a count, so it
is read from the question here, counted by `db.trade_tally`
(analyst_log/tally.py) and posted as written, with no model call:

  who     a member (by author id, in every channel they post in), a
          channel (the positions with a row posted there), or the room
  what    calls, puts, one ticker
  when    the window the asker named, 30 days when none, never more than
          the 45 days the log is kept in full

Two win rates are posted, each with what it counts: of the closes that
carry a result, and with every position that expired with no exit posted
counted as a loss. Members post results far more often than entries and
entries far more often than losing exits, so neither figure alone is the
record, and the lines never grade either one.

A question this cannot place returns None and goes to the model: a
pronoun with nobody named, a name nobody is logged under, a roast, or a
win rate that is not about the room's trades ("what win rate do I need at
2 to 1").
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from discord_bot import chat_count as _cc

DEFAULT_DAYS = 30
MAX_MEMBERS = 3
TOP_TRADERS = 5

_THINGS = r"(?:puts?|calls?|trades?|alerts?|options?|plays?|contracts?|positions?)"
_OWNER = r"(?:(?:the|his|her|their|my|our|all|every|\w+'s)\s+)?"
_INTENT_RE = re.compile(
    r"\bwin\s*[-/]?\s*rates?\b|\bwinrates?\b|\bwin\s+(?:percentage|percent|pct)\b|\bwin\s*%"
    r"|\bw\s*/\s*l\b|\bwin[\s/-]+loss\b|\bwins?\s+(?:and|to|vs\.?|versus)\s+loss(?:es)?\b"
    r"|\b(?:track|trading|trade)\s+record\b|\bhit\s+rate\b|\bbatting\s+average\b|\bsuccess\s+rate\b"
    r"|\b(?:percent(?:age)?|average|avg)\s+(?:of\s+)?(?:returns?|gains?)\s+(?:on|of|for|from)\b"
    rf"|\breturns?\s+on\s+{_OWNER}{_THINGS}\b"
    r"|\bhow\s+many\s+(?:wins?|winners?|losses|losers?|(?:winning|losing)\s+trades?)\b"
    rf"|\brecord\s+(?:on|with|for)\s+{_OWNER}{_THINGS}\b"
    # "what's Abe's record", "my record this month": a member's record,
    # never "NVDA's record high" or "a record quarter"
    r"|\b(?:my|\w+'s)\s+(?:(?:trading|trade|overall|recent|monthly|30[\s-]?day)\s+)?record\b"
    r"(?!\s+(?:high|low|close|quarter|year|month|week|day|revenue|earnings|sales|deliver\w*|run|breaking"
    r"|setting|profit|numbers?|levels?|label|player|store|books?))", re.I)
# Not a count of the room's trades: a roast, or arithmetic about win rates.
_NOT_A_TALLY_RE = re.compile(
    r"\broast|\bclown|\bcook(?:ed)?\b|\bflame\b|\bmake\s+fun\b|\bdunk\b|\binsult|\btalk\s+shit\b|\bjoke\b"
    r"|\bneed(?:ed|s)?\b|\brequired?\b|\bbreak[\s-]?even\b|\bkelly\b|\bgood\s+win\s*rate\b"
    r"|\bwhat\s+(?:is|does)\s+(?:a\s+)?(?:win\s*rate|hit\s+rate)\s+mean\b"
    r"|\bbacktest|\bhistorical(?:ly)?\b|\bstrategy\b|\bseasonal"
    # a record of some trades only ("the options he says are slam"): the
    # count here would answer a wider question than the one asked
    r"|\b(?:that|which|where|when|whenever|if|after|before)\s+(?:he|she|they|i|we|you)\b"
    r"|\b(?:he|she|they)\s+(?:says?|said|calls?|called|labels?|labell?ed|tags?|tagged|posts?|posted)\b|\""
    # the league's record, asked outside the league channel
    r"|\bfantasy\b|\bleague\b|\bstandings?\b|\bplayoffs?\b|\bsleeper\b|\bmatchups?\b"
    # a jab or a boast that mentions a record is banter, not a request
    r"|\bthan\s+(?:yours?|you|u|me|mine|his|hers|theirs)\b"
    r"|\b(?:trash|garbage|dogshit|shit|ass|fraud|sucks?|terrible|awful|lol|lmao|bro)\b", re.I)
# A request: a question, an instruction, or a bare phrase ("abe win rate").
_ASK_RE = re.compile(
    r"\?|\b(?:what(?:'s|s)?|how|who(?:'s|s)?|which|show|give|tell|post|pull|get|check|run|calc\w*|comput\w*"
    r"|do\s+a|can\s+you|could\s+you)\b", re.I)
BARE_PHRASE_WORDS = 8

_SELF_RE = re.compile(
    r"\bmy\s+(?:\w+\s+){0,2}(?:win|wins|losses|record|w\s*/\s*l|track|hit|returns?|trades?|puts?|calls?|alerts?|plays?)\b"
    r"|\bhow\s+many\s+(?:\w+\s+){1,2}(?:do|did|have)\s+i\b|\b(?:am|have)\s+i\s+(?:been\s+)?(?:winning|profitable|green)\b"
    r"|\bhow\s+am\s+i\s+doing\b|\b(?:do|did|have)\s+i\s+(?:have|won|win|hit)\b", re.I)
# a name written beside the record: "bk's win rate", "win rate of kyle"
_NAME_RES = (
    re.compile(r"(@?[\w.]{2,32})'s?\s+(?:\w+\s+){0,2}(?:win|record|w\s*/\s*l|track|hit|returns?|puts?|calls?|trades?|alerts?)\b", re.I),
    re.compile(r"\b(?:rate|record|w\s*/\s*l|returns?)\s+(?:of|for|on)\s+(@?[\w.]{2,32})\b", re.I),
    re.compile(r"\b(?:does|did|has|is)\s+(@?[\w.]{2,32})\s+(?:have|hit|win|won|winning|doing)\b", re.I),
)
# "abe or BK", "kyle vs sam": either side may be a member. These are
# looked up among the profiled members only (`_members`).
_PAIR_RE = re.compile(r"(@?[\w.]{2,32})\s+(?:or|vs\.?|versus|and|against)\s+(@?[\w.]{2,32})\b", re.I)
_NOT_A_NAME = frozenset("""
the a an my his her their our your this that these those what whats which who how its it is
win wins rate record track trading trade hit puts put calls call trades alerts alert options
plays room discord server chat channel group everyone everybody member members callers caller
month week year day today all overall total past last on of for in and or
""".split())
_ROOM_RE = re.compile(
    r"\b(?:this|the|our|whole|entire)\s+(?:discord|server|room|group|chat)\b|\bdiscord(?:'?s)?\b"
    r"|\beveryone(?:'?s)?\b|\beverybody(?:'?s)?\b|\b(?:all|the)\s+(?:the\s+)?(?:members|callers|alerts|channels)\b"
    r"|\broom(?:'?s)?\b|\bas\s+a\s+(?:group|room|whole)\b|\bserver(?:'?s)?\b|\bcollectively\b", re.I)

# Alert rooms by what the room calls them. IDs are channel_config.KNOWN's.
_CHANNEL_RES = (
    (1408508350202908845, re.compile(r"\bmembers?[\s'-]*s?\s*(?:alerts?|channel|room)\b", re.I)),
    (1317587853282119747, re.compile(r"\babe(?:'?s)?[\s-]+(?:alerts?|channel)\b", re.I)),
    (1420507457708621824, re.compile(r"\b(?:kyle|bk|bankerkyle)(?:'?s)?[\s-]+(?:alerts?|channel)\b", re.I)),
    (1415429535268343919, re.compile(r"\b(?:big[\s-]+)?(?:kloh|gloh)(?:'?s)?[\s-]+(?:alerts?|channel)\b", re.I)),
    (1415073939184422973, re.compile(r"\b(?:cemini|wiz[\s-]+of[\s-]+cemini)(?:'?s)?[\s-]+(?:alerts?|channel)\b", re.I)),
    (1405199403286794393, re.compile(r"\bspot[\s-]+bag(?:[\s-]+alerts?)?\b", re.I)),
    (1407763027339509950, re.compile(r"\bcrypto[\s-]+alerts?\b", re.I)),
    (1404879999239979251, re.compile(r"\bgain[\s-]+(?:and[\s-]+)?loss(?:[\s-]+porn)?\b", re.I)),
    (1317587853282119745, re.compile(r"\bstonks?[\s-]+yapping\b|\bmain\s+chat\b|\bgeneral\s+chat\b", re.I)),
)
_THIS_CHAN_RE = re.compile(r"\bthis\s+channel\b|\bin\s+here\b", re.I)

# "put it in a chart" is not the option
_PUT_RE = re.compile(r"\bputs\b|\bput\s+(?:options?|side|contracts?|trades?|alerts?|plays?)\b|\blong\s+put\b", re.I)
# "Abe's calls" are his alerts of any kind: calls are the option only
# beside a word that says so.
_CALL_RE = re.compile(
    r"\b(?:on|for|with|in|of|his|her|their|my|our)\s+calls\b(?=\s*(?:\?|$|this|last|past|in\s+the|over|today|and|or|vs))"
    r"|\b(?:on|in|for|with)\s+\$?[A-Za-z]{1,5}\s+calls\b"
    r"|\bcall\s+(?:options?|side|contracts?|trades?)\b|\blong\s+calls\b|\bcalls\s+(?:only|vs\.?|versus|and\s+puts|or\s+puts)\b"
    r"|\bputs\s+(?:and|or|vs\.?|versus)\s+calls\b", re.I)
_MTD_RE = re.compile(r"\bmonth[\s-]+to[\s-]+date\b|\bmtd\b", re.I)
_LONG_RE = re.compile(r"\bytd\b|\byear[\s-]+to[\s-]+date\b|\bthis\s+year\b|\blifetime\b|\bsince\s+(?:inception|the\s+start)\b",
                      re.I)
_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
_SLOT_RE = re.compile(r"\b(?:on|in|for|with|of|trading|playing)\s+\$?([A-Za-z]{1,5})\b")


def _ticker_candidates(t: str) -> list[str]:
    """Symbols the question may be narrowing to, by the router's own
    reading: a capitalised or named one anywhere ("NVDA", "tesla"), a
    lowercase one only in a slot after on/in/for ("win rate on spy").
    `run` drops any that is a member's name (BK and ABE are listed symbols)."""
    try:
        from discord_bot import ask_router as _r
        out = list(_r.extract_tickers(t, lowercase=False))
        loose = set(_r.extract_tickers(t))
    except Exception:
        return []
    for m in _SLOT_RE.finditer(t):
        tok = m.group(1).upper()
        if tok in loose and tok not in out:
            out.append(tok)
    return out[:4]


def _fantasy_channel(channel_id) -> bool:
    try:
        import channel_config
        return bool(channel_id) and int(channel_id) == channel_config.FANTASY_CHANNEL_ID
    except Exception:
        return False


def parse(text: str, channel_id: int | None = None) -> dict | None:
    """What a record question asks for, or None when it is not one this
    module answers. `text` is the asker's own words (ask_router.asker_text)."""
    t = _cc._straight(text).strip()
    if not t or not _INTENT_RE.search(t) or _NOT_A_TALLY_RE.search(t):
        return None
    if not _ASK_RE.search(t) and len(t.split()) > BARE_PHRASE_WORDS:
        return None            # a statement that mentions a record
    has_put, has_call = bool(_PUT_RE.search(t)), bool(_CALL_RE.search(t))
    if _fantasy_channel(channel_id) and not (has_put or has_call or re.search(
            r"\b(?:trades?|alerts?|options?|contracts?)\b", t, re.I)):
        return None            # "my record" there is the league's
    m = _cc._CHAN_ID_RE.search(t)
    chan = int(m.group(1)) if m else next((cid for cid, rx in _CHANNEL_RES if rx.search(t)), None)
    names = []
    for rx in _NAME_RES:
        for m in rx.finditer(t):
            n = m.group(1).lstrip("@").lower()
            if n not in _NOT_A_NAME and n not in names:
                names.append(n)
    pairs = []
    for m in _PAIR_RE.finditer(t):
        for g in m.groups():
            n = g.lstrip("@").lower()
            if n not in _NOT_A_NAME and n not in names and n not in pairs:
                pairs.append(n)
    window = (_cc._window(t) or ({"kind": "this_month"} if _MTD_RE.search(t) else None)
              or ({"kind": "all"} if _LONG_RE.search(t) else None))
    cash = _CASHTAG_RE.search(t)
    return {
        "text": t,
        "channel_id": chan,
        "this_channel": chan is None and bool(_THIS_CHAN_RE.search(t)),
        "self": bool(_SELF_RE.search(t)),
        "names": names,
        "pair_names": pairs[:6],
        "room": bool(_ROOM_RE.search(t)),
        "contract_type": "put" if has_put and not has_call else "call" if has_call and not has_put else None,
        "ticker": cash.group(1).upper() if cash else None,
        "tickers": [] if cash else _ticker_candidates(t),
        "window": window,
    }


def channel_in(text: str) -> int | None:
    """The channel a phrase names: a <#id> mention, a bare id, or what the
    room calls an alert channel ("member alerts", "kyle-alerts")."""
    t = (text or "").strip()
    m = _cc._CHAN_ID_RE.search(t)
    if m or t.isdigit():
        return int(m.group(1) if m else t)
    flat = re.sub(r"[^\w\s'-]+", " ", t)
    return next((cid for cid, rx in _CHANNEL_RES if rx.search(flat) or rx.search(flat + " channel")), None)


def spec_for(*, scope: str, channel: str | None = None, contract_type: str | None = None,
             ticker: str | None = None, days: int | None = None) -> dict | None:
    """The spec of a record asked for through the trade-log tool: the room
    or one channel. None when the channel named is not one the bot knows."""
    cid = channel_in(channel or "") if scope == "channel" else None
    if scope == "channel" and not cid:
        return None
    ct = (contract_type or "").strip().lower().rstrip("s")
    return {"text": "", "channel_id": cid, "this_channel": False, "self": False, "names": [],
            "pair_names": [], "room": scope == "room",
            "contract_type": ct if ct in ("call", "put") else None,
            "ticker": (ticker or "").strip().lstrip("$").upper() or None, "tickers": [],
            "window": {"kind": "last", "n": max(1, int(days)), "unit": "d"} if days else None}


# ------------------------------------------------------------------ counting

def _known_names() -> dict[str, int]:
    """{lowercase name: user id} for the profiled members and the
    registered callers, by username, display name and caller name."""
    import db
    from config import settings
    out: dict[str, int] = {}
    try:
        rows = db.get_connection().execute(
            "SELECT user_id, username, display_name FROM user_profiles").fetchall()
    except Exception:
        return out
    by_user = {}
    for r in rows:
        for n in (r[1], r[2]):
            n = (n or "").strip().lower()
            if len(n) >= 2:
                out.setdefault(n, int(r[0]))
        by_user[(r[1] or "").strip().lower()] = int(r[0])
    for c in settings.resolve_analyst_callers():
        uid = by_user.get((c.get("username") or "").strip().lower())
        for n in (c.get("name"), c.get("display")):
            n = (n or "").strip().lower()
            if uid and len(n) >= 2:
                out.setdefault(n, uid)
    return out


def _members(spec: dict, asker_id, known: dict[str, int]) -> list[int]:
    """The members a question names, [] for none."""
    import db
    t = spec["text"]
    ids = [int(x) for x in re.findall(r"<@!?(\d+)>", t)]
    # a channel's own name is not a member named ("kyle alerts")
    named_text = t
    for _, rx in _CHANNEL_RES:
        named_text = rx.sub(" ", named_text)
    found = db.members_named_in_text(named_text)
    for uid in found:
        if int(uid) not in ids:
            ids.append(int(uid))
    # "BK" is too short for the alias map: exact on a logged name, for a
    # name the map did not already place
    placed = {s.lower().rstrip("s").rstrip("'") for surfaces in found.values() for s in surfaces}
    for n in [*spec["names"], *spec.get("pair_names", [])]:
        if n in placed or n.rstrip("s") in placed:
            continue
        if re.search(rf"\b{re.escape(n)}(?:'?s)?[\s-]+(?:alerts?|channel)\b", t, re.I):
            continue
        # A name beside the record may be anyone logged. One side of an
        # "or" is looked up among the profiled members only: each chat
        # lookup reads the whole message table.
        uid = known.get(n) or (db.chat_author_by_name(n) if n in spec["names"] else None)
        if uid and int(uid) not in ids:
            ids.append(int(uid))
    # a registered caller by name, with or without the apostrophe ("bks record")
    from config import settings
    for c in settings.resolve_analyst_callers():
        for n in {(c.get("name") or "").lower(), (c.get("display") or "").lower()} - {""}:
            uid = known.get(n)
            if uid and int(uid) not in ids and re.search(rf"\b{re.escape(n)}(?:'?s)?\b", named_text, re.I):
                ids.append(int(uid))
    if spec["self"] and asker_id and int(asker_id) not in ids:
        ids.insert(0, int(asker_id))
    return ids[:MAX_MEMBERS]


def run(spec: dict, asker_id: int | None, channel_id: int | None,
        now: datetime | None = None) -> dict | None:
    """Count what `spec` asks for. None when the question names nobody and
    nothing this can place."""
    import db
    import pytz
    from config import settings
    tz = pytz.timezone(settings.timezone)
    now = now or datetime.now(timezone.utc)

    known = _known_names()
    ids = _members(spec, asker_id, known)
    chan = spec["channel_id"] or (channel_id if spec["this_channel"] else None)
    if spec["this_channel"] and not chan:
        return None
    if not ids and not chan and not spec["room"]:
        return None

    since, until, wlabel = _cc._bounds(spec["window"], now, tz)
    floor = now - timedelta(days=db.TALLY_MAX_DAYS)
    if since is None:
        days = db.TALLY_MAX_DAYS if (spec["window"] or {}).get("kind") == "all" else DEFAULT_DAYS
        since = (now - timedelta(days=days)).isoformat()
        wlabel = f"in the last {days} days"
    if datetime.fromisoformat(since) <= floor:
        wlabel = (f"in the last {db.TALLY_MAX_DAYS} days, as far back as the trade log "
                  "is kept in full")
    ticker = spec["ticker"]
    for cand in ([] if ticker else spec.get("tickers") or []):
        if cand.lower() in known or db.members_named_in_text(cand):
            continue            # a member, not a stock
        ticker = cand
        break
    filt = dict(since=since, until=until, contract_type=spec["contract_type"], ticker=ticker)

    people, chans = db.chat_names(author_ids=ids, channel_ids=[chan] if chan else [])
    subjects = []
    if ids:
        for uid in ids:
            t = db.trade_tally(author_ids=[uid], channel_id=chan, **filt)
            sub = {"label": _cc._label(people, uid, asker_id), "tally": t, "members": 0}
            own = None if chan else _cc._owner_channel(uid, people)
            if own:
                _, own_names = db.chat_names(channel_ids=[own])
                sub["alerts"] = {"channel": _cc.short_channel(own_names.get(int(own), "")),
                                 "tally": db.trade_tally(author_ids=[uid], channel_id=own, **filt)}
            subjects.append(sub)
    else:
        t = db.trade_tally(channel_id=chan, **filt)
        label = f"**#{_cc.short_channel(chans.get(int(chan), '') or str(chan))}**" if chan else "**The room**"
        active = sorted(t["by_trader"].items(),
                        key=lambda kv: -(kv[1]["wins"] + kv[1]["losses"] + kv[1]["expired_no_exit"]))
        top_ids = [a for a, c in active[:TOP_TRADERS]
                   if isinstance(a, int) and c["wins"] + c["losses"] + c["expired_no_exit"]]
        names, _ = db.chat_names(author_ids=top_ids)
        subjects.append({
            "label": label, "tally": t, "members": len(t["by_trader"]),
            "top": [(names.get(a, ("", ""))[0] or names.get(a, ("", ""))[1] or f"member {a}",
                     t["by_trader"][a]) for a in top_ids] if len(t["by_trader"]) > 1 else []})
    in_chan = f" in #{_cc.short_channel(chans.get(int(chan), '') or str(chan))}" if chan and ids else ""
    return {"status": "ok", "subjects": subjects, "window_label": wlabel, "in_channel": in_chan,
            "contract_type": spec["contract_type"], "ticker": ticker}


# ------------------------------------------------------------------ lines

def _n(n: int, word: str, plural: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (plural or word + 's')}"


def _pct(v: float) -> str:
    return f"{v:+,.0f}%"


def _rate_of_closes(t: dict) -> int | None:
    closes = t["wins"] + t["losses"]
    return round(100 * t["wins"] / closes) if closes else None


def _at_expiry(c: dict) -> str:
    """What the contracts behind the no-exit expiries were worth at expiry,
    where the sweep has priced them: " (35 worthless at expiry, 13 in the
    money)". '' when none is priced."""
    w, m = c.get("expired_worthless", 0), c.get("expired_in_money", 0)
    if not (w or m):
        return ""
    rest = c["expired_no_exit"] - w - m
    return (f" ({w:,} worthless at expiry, {m:,} in the money"
            + (f", {rest:,} not priced" if rest > 0 else "") + ")")


def _lead(label: str, where: str, t: dict) -> str:
    closes = t["wins"] + t["losses"]
    exp = t["expired_no_exit"]
    if not closes:
        tail = "no close with a result logged"
        if exp:
            tail += f", and {_n(exp, 'position')} expired with no exit posted{_at_expiry(t)}"
        return f"→ {label}{where}: {tail}"
    out = (f"→ {label}{where}: win rate **{_rate_of_closes(t)}%** of the {_n(closes, 'close')} with a "
           f"result (**{t['wins']:,}** {'win' if t['wins'] == 1 else 'wins'}, "
           f"**{t['losses']:,}** {'loss' if t['losses'] == 1 else 'losses'})")
    worthless = t.get("expired_worthless", 0)
    if exp and t.get("win_rate_worthless_pct") is not None:
        # every such expiry is priced: count as losses only the contracts
        # that finished worthless, and say what the others finished as
        if worthless:
            out += (f", or **{round(t['win_rate_worthless_pct'])}%** when the "
                    f"{_n(worthless, 'position')} whose contract expired worthless with no exit posted "
                    + ("is" if worthless == 1 else "are") + " counted as "
                    + ("a loss" if worthless == 1 else "losses"))
        if t["expired_in_money"]:
            out += (f". {_n(t['expired_in_money'], 'more position')} expired in the money "
                    "with no exit posted")
    elif exp:
        out += (f", or **{round(t['win_rate_pct'])}%** when the {_n(exp, 'position')} that expired "
                "with no exit posted " + ("is" if exp == 1 else "are") + " counted as "
                + ("a loss" if exp == 1 else "losses") + _at_expiry(t))
    return out


def _detail(t: dict, split: bool) -> list[str]:
    out = []
    bits = []
    if t["opened"]:
        bits.append(f"**{t['opened']:,}** {'position' if t['opened'] == 1 else 'positions'} opened, "
                    f"**{t['open']:,}** still open.")
    if t["flat"]:
        bits.append(f"{_n(t['flat'], 'more close')} had no result logged.")
    if t["closes_without_entry"]:
        bits.append(f"{t['closes_without_entry']:,} of the closes with a result had no entry logged.")
    if t["discarded"]:
        bits.append(f"{_n(t['discarded'], 'close')} left out: the logged gain cannot be a result.")
    if bits:
        out.append("→ " + " ".join(bits).rstrip("."))
    if t["avg_win_pct"] is not None or t["avg_loss_pct"] is not None:
        parts = []
        if t["avg_win_pct"] is not None:
            w = f"Average winning close **{_pct(t['avg_win_pct'])}**"
            if t["wins"] >= 5 and t["median_win_pct"] is not None:
                w += f" (median {_pct(t['median_win_pct'])})"
            parts.append(w)
        if t["avg_loss_pct"] is not None:
            parts.append(("average " if parts else "Average ") + f"losing close **{_pct(t['avg_loss_pct'])}**")
        out.append("→ " + ", ".join(parts))
    if split:
        kinds = []
        for key, name in (("call", "Calls"), ("put", "Puts"), ("other", "Shares and other")):
            c = t["by_kind"][key]
            if not (c["wins"] or c["losses"] or c["expired_no_exit"]):
                continue
            s = f"{name}: {_n(c['wins'], 'win')}, {_n(c['losses'], 'loss', 'losses')}"
            if c["expired_no_exit"]:
                s += f", {c['expired_no_exit']:,} expired with no exit{_at_expiry(c)}"
            kinds.append(s)
        if len(kinds) > 1:
            out.append("→ " + ". ".join(kinds))
    return out


def _short(c: dict) -> str:
    s = f"{c['wins']}W {c['losses']}L"
    return s + (f" {c['expired_no_exit']} expired" if c["expired_no_exit"] else "")


def lines(res: dict) -> list[str]:
    if not res or res.get("status") != "ok":
        return []
    filt = "".join(f", {x}" for x in (
        res.get("ticker"), {"put": "puts", "call": "calls"}.get(res.get("contract_type") or "")) if x)
    where = f"{filt}{res['in_channel']}, {res['window_label']}"
    out: list[str] = []
    solo = len(res["subjects"]) == 1
    for sub in res["subjects"]:
        t = sub["tally"]
        label = sub["label"]
        if not (t["wins"] or t["losses"] or t["opened"] or t["flat"]):
            who = "you" if label == "**You**" else label
            out.append(f"→ No trades logged for {who}{where}")
            continue
        members = f", {_n(sub['members'], 'member')}" if sub.get("members", 0) > 1 else ""
        out.append(_lead(label, f"{filt}{res['in_channel']}{members}, {res['window_label']}", t))
        if not solo:
            continue
        out += _detail(t, split=not res.get("contract_type"))
        if sub.get("top"):
            out.append("→ Most active: " + " · ".join(f"**{n}** {_short(c)}" for n, c in sub["top"]))
        al = sub.get("alerts")
        if al and (al["tally"]["opened"] or al["tally"]["wins"] or al["tally"]["losses"]):
            a = al["tally"]
            out.append(f"→ In #{al['channel']} alone: {_n(a['opened'], 'alert')} opened, "
                       f"{_n(a['wins'], 'win')}, {_n(a['losses'], 'loss', 'losses')}, "
                       f"{a['expired_no_exit']:,} expired with no exit posted{_at_expiry(a)}, "
                       f"{a['open']:,} still open")
    return out


def answer(spec: dict, asker_id: int | None, channel_id: int | None) -> str | None:
    """The posted text, or None to hand the question to the model."""
    res = run(spec, asker_id, channel_id)
    out = lines(res) if res else []
    return "\n\n".join(out) if out else None
