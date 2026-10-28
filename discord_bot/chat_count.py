"""Chat counts computed in code (2026-10-28).

"How many times has Abe said slam or slammed in his channel in the last
21 days" (2026-10-06, re-run through the live harness 10-28) went to the
chat search, which ignores the member and the window and stops at 20
rows. The model counted 6 across the server, a check deleted the count,
and the room got a list of quotes. Abe's channel had 0. Two older
shortcuts in bot.py had the same flaw in other forms: the slur count
ignored the member named ("the most frequent slurs Kyle used" counted
the whole room) and always used 30 days, and the message count ignored
the window asked for and found the member with a LIKE on names.

All three now read the question here, count in SQL by author_id and
channel_id, match whole words in Python ("Islam" is not "slam"), and
post the lines as written, with no model call. A slur count that never
reaches Gemini also stays clear of its content filter, the reason the
slur shortcut was built (2026-06-04: four blocked answers in 90 seconds).

A question this module cannot place returns None and goes to the model
as before: a name that resolves to nobody, "you" (the bot's own posts
are not stored), or a count of something other than words or messages.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

KNOWN_SLURS = ("nigga", "nigger", "chink", "spic", "kike", "fag", "faggot", "pajeet")
_SLUR_META_RE = re.compile(
    r"\b(?:slurs?|slangs?|n[-\s]?word|word\s+(?:use\s+)?with\s+an?\s+n)\b", re.I)

_INTENT_RE = re.compile(
    r"\bhow\s+many\s+(?:total\s+)?(?:times|messages?|texts?|posts?)\b"
    r"|\bhow\s+many\s+(?:\w+\s+){0,2}(?:messages|slurs|slangs)\b"
    r"|\bword\s+count\b|\btally\b|\bnumber\s+of\s+(?:messages|times)\b"
    r"|\bcount\s+(?:of|the)\s+(?:words?|slurs?|slangs?|uses?|messages)\b"
    r"|\bmost\s+(?:frequent|used|common)\s+(?:slurs?|slangs?|curse|words?)\b", re.I)
# kept from the slur shortcut (2026-06-04): "how often" and "frequency"
# count only for slurs, which must not reach the model's filter
_SLUR_INTENT_RE = re.compile(r"\bhow\s+often\b|\bfrequency\b", re.I)
_MESSAGES_RE = re.compile(r"\b(?:messages?|texts?|posts?)\b", re.I)

_VERB = (r"(?:say|says|said|saying|type|typed|types|typing|use|used|uses|using|mention|mentions"
         r"|mentioned|write|wrote|written|spell|spelled|drop|dropped|drops)")
_AFTER_VERB_RE = re.compile(rf"\b{_VERB}\s+(?:the\s+(?:words?|term|phrase)\s+)?(.{{1,60}})", re.I)
_PASSIVE_RE = re.compile(
    rf"\bhow\s+many\s+times\s+(?:(?:was|were|is)\s+(?:the\s+(?:words?|term)\s+)?(.{{1,40}}?)"
    rf"|(?:has|have)\s+(?:the\s+(?:words?|term)\s+)?(.{{1,40}}?)\s+been)\s+{_VERB}\b", re.I)
_OF_RE = re.compile(r"\b(?:word\s+count|count|tally)\s+(?:on|of|for)\s+(?:the\s+words?\s+)?(.{1,40})", re.I)
_QUOTED_RE = re.compile(r"\"([^\"]{2,30})\"|(?<![A-Za-z])'([^']{2,30})'(?![A-Za-z])")
_TOK_RE = re.compile(r"\$?[a-z0-9][a-z0-9_'-]*|[,/]", re.I)
_STOP = frozenset("""
i me my mine you your he him his she her they them their we us our it its this that these those
i'm i've he's she's they're it's that's he'd she'd they'd there here what when where who why how
the a an to of in on at by for from with about since over during into than then so and or but not
no yes is was were be been are am do did does has have had will would should could can
today yesterday week month year day days hours last past this chat channel room server discord
something anything everything nothing word words term times time much many ever even
message messages text texts post posts
""".split())

_SELF_RE = re.compile(
    r"\b(?:have|did|do|had|has)\s+i\b|\bi'?ve\b|\bi\s+(?:ever\s+)?(?:said|say|sent|send|typed|type"
    r"|posted|post|used|use|wrote|dropped|mentioned)\b|\bmy\s+(?:messages|posts|texts)\b", re.I)
_SUBJECT_RE = re.compile(
    rf"\b(?:has|have|did|does|do|had)\s+(@?[\w.]+)\s+(?:ever\s+|even\s+|actually\s+|really\s+)?"
    rf"(?:{_VERB}|sent|send|sends|posted|post|posts)\b", re.I)
# not a member: the room as a whole, or a word that only looks like a subject
_ROOM_WORDS = frozenset({"the", "everyone", "anyone", "everybody", "anybody", "people", "we",
                         "y'all", "yall", "this", "chat", "room", "it", "there", "been", "they",
                         "someone", "somebody", "all", "i", "members", "guys"})

_THIS_CHAN_RE = re.compile(r"\bthis\s+channel\b|\bin\s+here\b", re.I)
_OWN_CHAN_RE = re.compile(r"\b(?:his|her|their|my|own|[\w.]+'s)\s+(?:own\s+)?(?:alerts?\s+)?channel\b", re.I)
_CHAN_ID_RE = re.compile(r"<#(\d+)>")

_LAST_N_RE = re.compile(
    r"\b(?:last|past|previous)\s+(\d{1,4})\s*(hours?|hrs?|days?|weeks?|wks?|months?|mos?|years?|yrs?)\b", re.I)
_LAST_ONE_RE = re.compile(r"\b(?:last|past)\s+(hour|day|week|month|year)\b", re.I)
_ALL_RE = re.compile(r"\b(?:total|ever|all[\s-]time|overall)\b", re.I)
_UNIT_DAYS = {"h": None, "d": 1, "w": 7, "m": 30, "y": 365}

# Owner, 2026-06-04: a member's message count with no window asked for is
# read from the larger of the channel's last 6 hours or its last 100 messages.
RECENT_HOURS = 6
RECENT_MESSAGES = 100


def _straight(t: str) -> str:
    return (t or "").translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'}))


def _phrase_terms(s: str) -> list[str]:
    """The words of 'slam or slammed in his channel': slam, slammed."""
    out: list[str] = []
    expect = True
    for tok in _TOK_RE.findall(s or ""):
        low = tok.lower().strip("'")
        if low in (",", "/", "or", "and"):
            if not out:
                break
            expect = True
            continue
        if not expect or low in _STOP or len(low) < 2:
            break
        out.append(low)
        expect = False
    return out[:4]


def _terms(t: str) -> tuple[list[str], bool]:
    """(the words to count, whether they are the slur list)."""
    low = t.lower()
    named = [s for s in KNOWN_SLURS if re.search(rf"(?<![a-z]){s}", low)]
    if named:
        return named, True
    if _SLUR_META_RE.search(t):
        return list(KNOWN_SLURS), True
    quoted = [(a or b).strip().lower() for a, b in _QUOTED_RE.findall(t)]
    if quoted:
        return quoted[:4], False
    for rx in (_PASSIVE_RE, _AFTER_VERB_RE, _OF_RE):
        m = rx.search(t)
        if m:
            got = _phrase_terms(next(g for g in m.groups() if g))
            if got:
                return got, False
    return [], False


def _window(t: str) -> dict | None:
    m = _LAST_N_RE.search(t)
    if m:
        return {"kind": "last", "n": int(m.group(1)), "unit": m.group(2)[0].lower()}
    m = _LAST_ONE_RE.search(t)
    if m:
        return {"kind": "last", "n": 1, "unit": m.group(1)[0].lower()}
    for kind, rx in (("today", r"\btoday\b"), ("yesterday", r"\byesterday\b"),
                     ("this_week", r"\bthis\s+week\b"), ("this_month", r"\bthis\s+month\b")):
        if re.search(rx, t, re.I):
            return {"kind": kind}
    if _ALL_RE.search(t):
        return {"kind": "all"}
    return None


def parse(text: str) -> dict | None:
    """What a count question asks for, or None when it is not one this
    module answers. `text` is the asker's own words (ask_router.asker_text)."""
    t = _straight(text).strip()
    if not t:
        return None
    counting = bool(_INTENT_RE.search(t))
    if not counting and not _SLUR_INTENT_RE.search(t):
        return None
    terms, slurs = _terms(t)
    if not counting and not slurs:
        return None
    messages = not terms and bool(_MESSAGES_RE.search(t))
    if not terms and not messages:
        return None
    subject = None
    m = _SUBJECT_RE.search(t)
    if m:
        subject = m.group(1).lstrip("@").lower()
        if subject == "you":
            return None            # the bot's own posts are not stored
    cid = _CHAN_ID_RE.search(t)
    scope = (int(cid.group(1)) if cid else "own" if _OWN_CHAN_RE.search(t)
             else "this" if _THIS_CHAN_RE.search(t) else "all")
    return {"text": t, "terms": terms, "slurs": slurs, "messages": messages,
            "self": bool(_SELF_RE.search(t)), "subject": subject, "scope": scope,
            "window": _window(t)}


def is_count_question(question: str) -> bool:
    from discord_bot.ask_router import asker_text
    return parse(asker_text(question)) is not None


# ------------------------------------------------------------------ counting

def _bounds(window: dict | None, now: datetime, tz) -> tuple[str | None, str | None, str]:
    """(since, until, label) in UTC ISO for a window spec."""
    local = now.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    kind = (window or {}).get("kind")
    if kind == "last":
        n, unit = window["n"], window["unit"]
        delta = timedelta(hours=n) if unit == "h" else timedelta(days=n * _UNIT_DAYS[unit])
        word = {"h": "hour", "d": "day", "w": "week", "m": "month", "y": "year"}[unit]
        label = f"in the last {word}" if n == 1 else f"in the last {n} {word}s"
        return (now - delta).isoformat(), None, label
    if kind == "today":
        return midnight.astimezone(timezone.utc).isoformat(), None, "today"
    if kind == "yesterday":
        return ((midnight - timedelta(days=1)).astimezone(timezone.utc).isoformat(),
                midnight.astimezone(timezone.utc).isoformat(), "yesterday")
    if kind == "this_week":
        start = midnight - timedelta(days=midnight.weekday())
        return start.astimezone(timezone.utc).isoformat(), None, "this week (since Monday)"
    if kind == "this_month":
        start = midnight.replace(day=1)
        return start.astimezone(timezone.utc).isoformat(), None, "this month"
    return None, None, ""


def _day(iso: str | None, tz) -> str:
    if not iso:
        return ""
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
    return f"{d:%b} {d.day}"


def short_channel(name: str) -> str:
    """'💬-stonks-yapping-💬' -> 'stonks-yapping'."""
    return re.sub(r"^[\W_]+|[\W_]+$", "", name or "") or (name or "")


def _label(people: dict, uid: int, asker_id, bold: bool = True) -> str:
    """'**BK** (bankerkyle)', the way other answers name a member."""
    b = "**" if bold else ""
    if asker_id and int(uid) == int(asker_id):
        return f"{b}You{b}"
    disp, user = people.get(int(uid), ("", ""))
    if disp and user and disp.lower() != user.lower():
        return f"{b}{disp}{b} ({user})"
    return f"{b}{disp or user or f'member {uid}'}{b}"


def _matcher(terms: list[str], slurs: bool) -> re.Pattern:
    """Whole words with their ordinary endings: "slam" counts slams,
    slammed and slamming, never "Islam". A slur takes a plural only:
    an ending would make "spic" count "spicy" and "spices"."""
    def form(t: str) -> str:
        w = re.escape(t)
        if slurs:
            return w + r"(?:s|z)?"
        double = re.escape(t[-1]) + "?" if t[-1:].isalpha() else ""
        return w + double + r"(?:s|es|ed|d|ing|er|ers|est)?(?:'s)?"
    alts = sorted(terms, key=len, reverse=True)
    return re.compile(r"(?<![\w$])(?:" + "|".join(form(t) for t in alts) + r")(?![\w])", re.I)


def _owner_channel(uid: int, people: dict) -> int | None:
    from config import settings
    user = (people.get(int(uid), ("", ""))[1] or "").lower()
    for c in settings.resolve_analyst_callers():
        if user and c.get("username", "").lower() == user and c.get("channel_id"):
            return int(c["channel_id"])
    return None


def run(spec: dict, asker_id: int | None, channel_id: int | None,
        now: datetime | None = None) -> dict | None:
    """Count what `spec` asks for. None when the question names someone
    this cannot place, or asks about this channel from nowhere."""
    import db
    import pytz
    from config import settings
    tz = pytz.timezone(settings.timezone)
    now = now or datetime.now(timezone.utc)
    t = spec["text"]

    ids: list[int] = [int(x) for x in re.findall(r"<@!?(\d+)>", t)]
    for uid, surfaces in db.members_named_in_text(t).items():
        if all(s in spec["terms"] for s in surfaces) or int(uid) in ids:
            continue
        ids.append(int(uid))
    if spec["self"] and asker_id and int(asker_id) not in ids:
        ids.insert(0, int(asker_id))
    if not ids and spec["subject"] and spec["subject"] not in _ROOM_WORDS:
        # "BK": too short for the alias map, exact on a logged name
        uid = db.chat_author_by_name(spec["subject"])
        if not uid:
            return None
        ids.append(uid)
    ids = ids[:4]
    people, _ = db.chat_names(author_ids=ids)

    note = None
    scope = spec["scope"]
    if scope == "this":
        scope_id = channel_id
        if not scope_id:
            return None
    elif scope == "own":
        scope_id = _owner_channel(ids[0], people) if ids else None
        if not scope_id:
            who = _label(people, ids[0], asker_id, bold=False) if ids else "this member"
            note = f"→ No alert channel of their own is on file for {who}, so this counts every channel the bot reads"
            if who == "You":
                note = "→ No alert channel of your own is on file, so this counts every channel the bot reads"
    elif isinstance(scope, int):
        scope_id = scope
    else:
        scope_id = None

    since, until, wlabel = _bounds(spec["window"], now, tz)
    result = {"status": "ok", "note": note, "scope_id": scope_id, "terms": spec["terms"],
              "slurs": spec["slurs"], "asker_id": asker_id, "entries": []}

    if spec["messages"] and spec["window"] is None and ids:
        pool_chan = scope_id or channel_id
        if not pool_chan:
            return None
        since6 = (now - timedelta(hours=RECENT_HOURS)).isoformat()
        six, last = db.chat_recent_pool(pool_chan, since_iso=since6, n=RECENT_MESSAGES)
        pool, by_hours = (six, True) if len(six) >= len(last) else (last, False)
        if not pool:
            return None
        _, chans = db.chat_names(channel_ids=[pool_chan])
        result.update(kind="recent", pool=len(pool), by_hours=by_hours,
                      span_start=pool[-1][1] if pool else None,
                      channel=short_channel(chans.get(int(pool_chan), "")))
        for uid in ids:
            result["entries"].append({"label": _label(people, uid, asker_id),
                                      "scope_n": sum(1 for a, _ in pool if int(a) == uid)})
        return result

    if spec["window"] is None or spec["window"]["kind"] == "all":
        start = db.chat_log_start(author_ids=ids or None,
                                  channel_id=scope_id if not ids else None)
        wlabel = f"since {_day(start, tz)}, when the log starts" if start else "in the log"
    result["window_label"] = wlabel

    tally: dict[tuple[int, int], int] = {}
    uses: dict[int, int] = {}
    per_term: dict[str, int] = {}
    latest: dict[int, tuple] = {}
    if spec["messages"]:
        result["kind"] = "messages"
        for a, c, n in db.chat_count_groups(author_ids=ids or None, since_iso=since, until_iso=until):
            tally[(int(a), int(c))] = tally.get((int(a), int(c)), 0) + int(n)
    else:
        result["kind"] = "words"
        rx = _matcher(spec["terms"], spec["slurs"])
        longest = sorted(spec["terms"], key=len, reverse=True)
        for a, c, content, posted in db.chat_count_rows(
                like_terms=spec["terms"], author_ids=ids or None, since_iso=since, until_iso=until):
            hits = rx.findall(content or "")
            if not hits:
                continue
            a, c = int(a), int(c)
            tally[(a, c)] = tally.get((a, c), 0) + 1
            uses[a] = uses.get(a, 0) + len(hits)
            for h in hits:
                term = next((x for x in longest if h.lower().startswith(x)), longest[-1])
                per_term[term] = per_term.get(term, 0) + 1
            if a not in latest:
                latest[a] = (content, posted, c)
        result["per_term"] = sorted(per_term.items(), key=lambda kv: -kv[1])

    chan_ids = {c for _, c in tally} | ({scope_id} if scope_id else set())
    authors = {a for a, _ in tally}
    more_people, chans = db.chat_names(author_ids=authors - set(people), channel_ids=chan_ids)
    people.update(more_people)
    result["channel"] = short_channel(chans.get(int(scope_id), "")) if scope_id else ""

    def entry(keep) -> dict:
        by_chan: dict[int, int] = {}
        for (a, c), n in tally.items():
            if keep(a):
                by_chan[c] = by_chan.get(c, 0) + n
        return {"scope_n": by_chan.get(int(scope_id), 0) if scope_id else sum(by_chan.values()),
                "all_n": sum(by_chan.values()),
                "by_channel": [(short_channel(chans.get(c, str(c))), n)
                               for c, n in sorted(by_chan.items(), key=lambda kv: -kv[1])]}

    if ids:
        for uid in ids:
            e = entry(lambda a, u=uid: a == u)
            e.update(label=_label(people, uid, asker_id), uses=uses.get(uid, 0))
            if uid in latest and not spec["slurs"]:
                content, posted, c = latest[uid]
                e["latest"] = (content, _day(posted, tz), short_channel(chans.get(c, "")))
            result["entries"].append(e)
    else:
        e = entry(lambda a: True)
        e.update(label="**The room**", uses=sum(uses.values()))
        by_author: dict[int, int] = {}
        for (a, c), n in tally.items():
            if not scope_id or c == int(scope_id):
                by_author[a] = by_author.get(a, 0) + n
        e["top"] = [(_label(people, a, None, bold=False), n)
                    for a, n in sorted(by_author.items(), key=lambda kv: -kv[1])[:3]]
        result["entries"].append(e)
    return result


# ------------------------------------------------------------------ lines

def _words_phrase(terms: list[str], slurs: bool) -> str:
    if slurs and len(terms) > 2:
        return "a slur"
    return " or ".join(f"“{t}”" for t in terms)


def _plural(n: int, word: str) -> str:
    return f"**{n:,}** {word}{'' if n == 1 else 's'}"


def lines(res: dict) -> list[str]:
    if not res or res.get("status") != "ok":
        return []
    out: list[str] = []
    chan = f" in #{res['channel']}" if res.get("channel") else ""
    if res["kind"] == "recent":
        span = ""
        if res.get("span_start"):
            mins = max(1, int((datetime.now(timezone.utc) - datetime.fromisoformat(
                res["span_start"].replace("Z", "+00:00"))).total_seconds() // 60))
            span = f"{mins} minutes" if mins < 120 else f"{mins / 60:.1f} hours"
        for e in res["entries"]:
            if res["by_hours"]:
                out.append(f"→ {e['label']} sent {_plural(e['scope_n'], 'message')} of the "
                           f"**{res['pool']:,}** in #{res['channel']} in the last {RECENT_HOURS} hours")
            else:
                out.append(f"→ {e['label']} sent {_plural(e['scope_n'], 'message')} of the last "
                           f"**{res['pool']:,}** in #{res['channel']}"
                           + (f", which cover the last {span}" if span else ""))
        return out
    if res.get("note"):
        out.append(res["note"])
    words = _words_phrase(res["terms"], res["slurs"])
    for e in res["entries"]:
        n = e["scope_n"]
        if res["kind"] == "messages":
            who = "" if e["label"] == "**The room**" else f"{e['label']} sent "
            head = f"→ {who}{_plural(n, 'message')}{chan} {res['window_label']}"
        else:
            head = f"→ {e['label']}: {_plural(n, 'message')} with {words}{chan} {res['window_label']}"
            if not res.get("channel") and e.get("uses", 0) > n:
                head += f" (**{e['uses']:,}** uses)"
        out.append(head)
        if res.get("channel") and e["all_n"] != n:
            rest = [f"{k:,} in #{c}" for c, k in e["by_channel"][:4]]
            out.append(f"→ In every channel the bot reads: **{e['all_n']:,}** ({', '.join(rest)})")
        elif not res.get("channel") and len(e["by_channel"]) > 1:
            out.append("→ By channel: " + " · ".join(f"#{c} {k:,}" for c, k in e["by_channel"][:4]))
        if e.get("top") and n and not res["slurs"]:
            out.append("→ Most from: " + " · ".join(f"{who} {k:,}" for who, k in e["top"]))
        if e.get("latest") and len(res["entries"]) == 1:
            text, day, c = e["latest"]
            text = re.sub(r"\s+", " ", text).strip()
            text = text if len(text) <= 140 else text[:137].rstrip() + "..."
            out.append(f"→ Latest: “{text}” ({day}, #{c})")
    per_term = [kv for kv in res.get("per_term") or [] if kv[1]]
    if res["slurs"] and len(per_term) > 1:
        out.append("→ By word: " + " · ".join(f"“{t}” {k:,}" for t, k in per_term))
    return out


def answer(spec: dict, asker_id: int | None, channel_id: int | None) -> str | None:
    """The posted text, or None to hand the question to the model."""
    res = run(spec, asker_id, channel_id)
    out = lines(res) if res else []
    return "\n\n".join(out) if out else None
