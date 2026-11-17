"""A desk's rating and price target, posted by code (2026-11-17, audit run 12 A1).

"JPMorgan reiterates its Underweight rating with a $215 price target" was
posted on 2026-11-16. JPMorgan's equity research on NVDA says Overweight,
target cut to $215 from $320. The words came from another JPMorgan note, a
sales commentary stored with rating "Underweight" (its credit analysts'
view of the bonds) beside the equity target. The outline handed whichever
note sorted first to the model as a DESK slot with its stored fields.

Two things follow, and both are here.

Which stored fields a note may be credited with (`fields`, `clean_note`):
a rating house's own equity research is trusted, and anywhere else a field
is kept only when the note's own sentence states it. `lookup_research`
applies this to every note before anything reads it.

A rating and a target that pass are a posted line like the price or the
EPS (discord_bot/stock_body.py):

    → **JPMorgan** (Nov 12): rated Overweight, price target **$215**, cut from $320.

The model is left the reason only (stock_outline's DESK slot says so). The
previous target is read from the note's own sentence about this target
("cuts the price target to $215 from $320") and is left out when no
sentence gives it.

`strip_wrong_calls` is the net for every other place the model can name a
bank: a sentence that gives a bank a rating or a price target that none of
that bank's notes in the turn carry is removed. No model rewrites it.
"""
from __future__ import annotations

import re
from datetime import date

_PLACEHOLDERS = frozenset({"", "n/a", "na", "none", "null", "nr", "not rated", "-", "--", "unchanged"})
_MONEY = r"[^\s\d(),;:]{0,4}\d[\d,]*(?:\.\d+)?[a-zA-Z]{0,3}"
_TARGET_WORD_RE = re.compile(r"\btarget|\bPT\b|\bTP\b|\bPO\b|\bobjective\b", re.I)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _clean(value) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in _PLACEHOLDERS else text


def _number(text: str) -> float | None:
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _trim(money: str) -> str:
    """'$215.00' -> '$215', '€63.5' stays."""
    return re.sub(r"(\d)\.0+(?!\d)", r"\1", (money or "").strip())


def call_of(note: dict) -> dict | None:
    """The note's call on the name when it carries a rating or a target:
    the first call, the one stock_outline shows."""
    for c in (note.get("calls") or [])[:1]:
        if _clean(c.get("rating")) or _clean(c.get("price_target")):
            return c
    return None


def _note_sentences(note: dict, call: dict) -> list[str]:
    texts = [str(call.get("rationale") or "")]
    for key in ("insights", "earnings"):
        texts.extend(str(x) for x in note.get(key) or [])
    return [s for t in texts for s in _SENTENCE_RE.split(t) if s.strip()]


def _num_pattern(value: float) -> str:
    """The number as a note may write it: with or without thousands commas
    and trailing zeros, behind a currency sign, and not part of a longer one."""
    whole = f"{value:.2f}".rstrip("0").rstrip(".")
    digits = re.escape(whole.split(".")[0])
    frac = whole.split(".")[1] if "." in whole else ""
    return (r"(?<![\d.,])[^\s\d(),;:]{0,4}" + r",?".join(digits)
            + (rf"\.{frac}0*" if frac else r"(?:\.0+)?") + r"(?![\d,]|\.\d)")


def prior_target(note: dict, call: dict) -> tuple[str, str] | None:
    """("cut" | "raised", the previous target as the note writes it), from
    a sentence of the note that states this target against the old one."""
    target = _number(_clean(call.get("price_target")))
    if not target:
        return None
    num = _num_pattern(target)
    step = r"(?:cut|raised|lowered|up|down|increased|reduced|lifted|trimmed|hiked)"
    after = re.compile(
        num + r"(?:\s+(?:price\s+)?(?:target|PT|TP|PO))?\s*,?\s*\(?(?:" + step + r"\s+)?from\s+(" + _MONEY + ")",
        re.I)
    before = re.compile(r"\bfrom\s+(" + _MONEY + r")\s+to\s+" + num, re.I)
    for s in _note_sentences(note, call):
        if not _TARGET_WORD_RE.search(s):
            continue
        m = after.search(s) or before.search(s)
        if not m:
            continue
        old = _number(m.group(1))
        if not old or old == target or not (target / 3 <= old <= target * 3):
            continue
        return ("cut" if old > target else "raised"), _trim(m.group(1).rstrip(".,"))
    return None


def _when(published: str, preview: bool = False) -> str:
    try:
        d = date.fromisoformat(str(published or "")[:10])
    except ValueError:
        return " (before the report)" if preview else ""
    return f" ({d:%b} {d.day}{', before the report' if preview else ''})"


_SOURCE_ALIASES = {"jpm": "jpmorgan", "gs": "goldman", "ms": "morgan stanley", "db": "deutsche bank",
                   "citigroup": "citi", "baml": "bofa"}


def own_broker(source: str) -> bool:
    """The note's source is a house that rates stocks, so the rating and
    target on its own note are its own. The Market Ear, a newsletter or a
    macro shop is not one."""
    import db  # noqa: F401  (the facade loads before db_parts)
    from db_parts.pdf import _BROKERS
    s = re.sub(r"\.", "", (source or "").strip().lower())
    s = _SOURCE_ALIASES.get(s, s)
    # the source opens with the house's name: "Zerohedge, summarizing HSBC
    # and Goldman Sachs research" is not Goldman's note
    return bool(s) and any(re.match(re.escape(b.lower().replace(".", "")) + r"(?![a-z])", s) for b in _BROKERS)


# A rating action must be in the note's words about the broker too.
_ACTION_STEMS = {"upgrade": "upgrad", "downgrade": "downgrad", "initiate": "initiat"}


# A sentence about the company's debt does not state a stock rating.
_CREDIT_RE = re.compile(r"\bcredit\b|\bspreads?\b|\bbonds?\b|\bCDS\b|\bnotes\s+due\b", re.I)
_RATING_ACTIONS = frozenset({"upgrade", "downgrade", "initiate", "reiterate", "price_target_change"})


def fields(note: dict) -> dict:
    """What the note's first call may be credited with, and to whom:
    {"holder", "relayed", "action", "rating", "target", "prior", "credit"}.

    The stored rating and target are trusted on one kind of note only: a
    rating house's own equity research. Everywhere else the extraction
    fills the field whether or not the note stated one, so there a field is
    kept only when a sentence of the note names the broker and states it,
    and a credit note's is never kept: it rates the company's debt.

    Why, all from NVDA in the week of 2026-11-10. JPMorgan's equity
    research was Overweight with the target cut to $215 from $320. Its
    credit research was Underweight on the bonds. Its sales commentary was
    stored as "Underweight, $215", the credit view beside the equity
    target, and that is the note behind the posted "JPMorgan reiterates its
    Underweight rating with a $215 price target" (audit run 12 A1). The
    Market Ear's "14 banks weigh in" was stored with Goldman and JPMorgan
    both "Neutral". Goldman's sales notes carry "Outperform" and
    "Overweight", ratings Goldman does not use. Over 21 days, 126 of 571
    house-and-ticker pairs with two or more notes had stored ratings on
    different sides."""
    source = str(note.get("source") or "").strip()
    kind = str(note.get("report_type") or "").strip().lower()
    out = {"holder": source, "relayed": False, "action": "", "rating": "", "target": "", "prior": None,
           "credit": kind == "credit"}
    calls = note.get("calls") or []
    if not calls or not source:
        return out
    call = calls[0]
    holder = str(call.get("call_by") or "").strip() or source
    action = str(call.get("action") or "").lower()
    rating = _clean(call.get("rating"))
    target = _trim(_clean(call.get("price_target")))
    relayed = holder != source
    if kind == "credit":
        rating = target = ""
        action = "" if action in _RATING_ACTIONS else action
    elif relayed or kind != "equity_research" or not own_broker(source):
        said = [s for s in _note_sentences(note, call) if names(s, holder) and not _CREDIT_RE.search(s)]
        if rating and not any(_in_text(rating, s, strict=True) for s in said):
            rating = ""
        value = _number(target)
        if target and not (value and any(re.search(_num_pattern(value), s) for s in said)):
            target = ""
        stem = _ACTION_STEMS.get(action)
        if stem and not any(stem in s.lower() for s in said):
            action = ""
    if (action == "reiterate" and not rating) or (action == "price_target_change" and not target):
        action = ""
    out.update(holder=holder, relayed=relayed, action=action, rating=rating, target=target)
    if target:
        out["prior"] = prior_target(note, call)
    return out


def clean_note(note: dict) -> dict:
    """The note with each call's action, rating and target as `fields`
    credits them, for every reader of the research: the model's payload,
    the outline and the checks. A dropped field is removed, not blanked."""
    calls = []
    for c in note.get("calls") or []:
        f = fields({**note, "calls": [c]})
        kept = {k: v for k, v in c.items() if k not in ("action", "rating", "price_target")}
        if f["action"]:
            kept["action"] = f["action"]
        if f["rating"]:
            kept["rating"] = f["rating"]
        if f["target"]:
            kept["price_target"] = f["target"]
        calls.append(kept)
    out = {**note, "calls": calls}
    if str(note.get("report_type") or "").lower() == "credit":
        out["desk"] = "credit research: its view is on the company's debt, not a stock rating"
    return out


_ACTION_HEAD = {"upgrade": ("upgraded to {r}", "upgraded"),
                "downgrade": ("downgraded to {r}", "downgraded"),
                "initiate": ("initiated at {r}", "initiated coverage")}


def line(note: dict, preview: bool = False) -> str:
    """The posted line for a note's rating and target, '' when the note
    carries neither. `preview`: the note was written before a report that
    is now out."""
    f = fields(note)
    with_rating, bare = _ACTION_HEAD.get(f["action"], ("rated {r}", ""))
    parts = []
    if not (f["rating"] or f["target"]):
        return ""
    head = with_rating.format(r=f["rating"]) if f["rating"] else bare
    if head:
        parts.append(head)
    if f["target"]:
        piece = f"price target **{f['target']}**"
        if f["prior"]:
            piece += f", {f['prior'][0]} from {f['prior'][1]}"
        parts.append(piece)
    who = f"**{f['holder']}**"
    if f["relayed"]:
        # the note relays another broker's action (2026-10-26 audit A21)
        who += f", relayed by {note.get('source')}"
    return f"→ {who}{_when(note.get('published'), preview)}: {', '.join(parts)}."


def reason(note: dict) -> str:
    """What is left for the model once the line is posted: the call's
    rationale and the desk's own figures."""
    call = call_of(note) or {}
    first_earnings = (note.get("earnings") or [""])[0]
    text = str(call.get("rationale") or "").strip()
    if first_earnings and first_earnings not in text:
        text = f"{text.rstrip('.')}. Its figures: {first_earnings}" if text else first_earnings
    return text


# ── the net: a rating or target a bank's notes do not carry ──────────────

_RATING = (r"strong\s+buy|buy|sell|hold|neutral|overweight|underweight|outperform(?:er)?|"
           r"underperform(?:er)?|equal[- ]?weight|market[- ]?weight|market[- ]?perform|"
           r"sector[- ]?(?:perform|weight|outperform)|peer[- ]?perform|accumulate|reduce")
# the words between the verb and the rating ("upgraded NVDA to", "keeps its");
# the ticker stays case-sensitive under the patterns' IGNORECASE
_FILLER = (r"(?:\s+(?:it|its|the|their|a|an|at|to|as|with|on|of|own|coverage|stock|shares|"
           r"name|rating|(?-i:[A-Z]{1,5})))*")
_RATING_CLAIM_RES = (
    re.compile(rf"\b({_RATING})[- ]rat(?:ing|ed)\b", re.I),
    re.compile(r"\b(?:rat(?:ed|es|ing)|reiterat\w+|maintain\w*|retain\w*|keeps?|kept|upgrad\w+|"
               r"downgrad\w+|initiat\w+|resum\w+|reinstat\w+)" + _FILLER + rf"\s+({_RATING})\b", re.I),
)
_CASE_SENSITIVE = frozenset({"buy", "sell", "hold", "reduce", "neutral", "accumulate"})
_TARGET_CLAIM_RES = (
    re.compile(r"\b(?:price\s+target|price\s+objective|target(?:\s+price)?|PT)\b(?:\s+(?:of|at|to|is|was|"
               r"sits\s+at|stands\s+at|goes\s+to|now))?\s+\**([$€£]\s?\d[\d,]*(?:\.\d+)?)", re.I),
    re.compile(r"([$€£]\s?\d[\d,]*(?:\.\d+)?)\**\s+(?:price\s+)?(?:target|objective)\b", re.I),
)
_ALIASES = {"jpmorgan": ("jpm", "j.p. morgan", "jp morgan"), "goldman": ("gs",),
            "bofa": ("bank of america",), "bank of": ("bofa",), "morgan stanley": ("ms",),
            "deutsche": ("db",)}


def _norm(rating: str) -> str:
    r = re.sub(r"[\s-]+", "", (rating or "").lower())
    return r[:-2] if r.endswith("performer") else r


def _names(bank: str) -> tuple[str, ...]:
    """How an answer may name the bank: 'Goldman Sachs' -> goldman, gs."""
    from discord_bot.desk_claims import _key
    key = _key(bank)
    return (key, *_ALIASES.get(key, ())) if key else ()


def _named(text_lower: str, names: tuple[str, ...]) -> bool:
    return any(re.search(r"(?<![a-z])" + re.escape(nm) + r"(?![a-z])", text_lower) for nm in names)


def names(text: str, bank: str) -> bool:
    """Whether `text` names the bank."""
    return _named((text or "").lower(), _names(bank))


def _in_text(rating: str, text: str, strict: bool = False) -> bool:
    """The rating as a word of the note's own text ('Equal Weight' for
    'equal-weight'; not 'buy' inside 'buyback'). `strict`: a rating that is
    also an ordinary word (buy, sell, hold) must be written as a rating,
    with its capital ("tells clients to buy the dip" is not a Buy rating)."""
    parts = [re.escape(p) for p in re.split(r"[\s-]+", rating.strip()) if p]
    if not parts:
        return False
    if strict and rating.strip().lower() in _CASE_SENSITIVE:
        return bool(re.search(r"\b" + re.escape(rating.strip().capitalize()) + r"\b", text or ""))
    return bool(re.search(r"\b" + r"[\s-]?".join(parts) + r"\b", text or "", re.I))


def _bank_facts(notes: list[dict]) -> dict[str, dict]:
    """bank key -> {"names", "ratings", "targets", "text", "rating_text"} from
    its notes. `rating_text` is what a stock rating may be read from: the
    sentences that are not about the company's debt, credit notes left out."""
    from discord_bot.desk_claims import _key
    out: dict[str, dict] = {}
    for n in notes or []:
        key = _key(str(n.get("source") or ""))
        if not key:
            continue
        f = out.setdefault(key, {"names": _names(str(n.get("source") or "")), "ratings": set(),
                                 "targets": [], "text": "", "rating_text": ""})
        before = len(f["text"])
        for c in n.get("calls") or []:
            got = fields({**n, "calls": [c]})        # a roundup's fields only when its text states them
            if got["rating"]:
                f["ratings"].add(_norm(got["rating"]))
            for v in (got["target"], (got["prior"] or ("", ""))[1]):
                if _number(v) is not None:
                    f["targets"].append(_number(v))
            f["text"] += " " + str(c.get("rationale") or "")
        for k in ("insights", "earnings", "risks"):
            f["text"] += " " + " ".join(str(x) for x in n.get(k) or [])
        for d in n.get("data_points") or []:
            f["text"] += " " + " ".join(str(d.get(k) or "") for k in ("figure", "metric", "context"))
        for t in n.get("trade_ideas") or []:
            f["text"] += " " + " ".join(str(t.get(k) or "") for k in ("description", "rationale"))
        if str(n.get("report_type") or "").lower() != "credit":
            f["rating_text"] += " " + " ".join(
                x for x in _SENTENCE_RE.split(f["text"][before:]) if not _CREDIT_RE.search(x))
    return out


def _wrong(sentence: str, facts: dict) -> bool:
    """The sentence gives this bank a rating or a target none of its notes
    carry, as a field or in their words."""
    for i, rx in enumerate(_RATING_CLAIM_RES):
        for m in rx.finditer(sentence):
            word = m.group(1)
            if i and word.lower() in _CASE_SENSITIVE and not word[0].isupper():
                continue                    # after a verb, "keeps a hold on costs" is prose
            if _norm(word) not in facts["ratings"] and not _in_text(word, facts["rating_text"]):
                return True
    values = list(facts["targets"])
    values += [v for v in (_number(x) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", facts["text"])) if v]
    for rx in _TARGET_CLAIM_RES:
        for m in rx.finditer(sentence):
            v = _number(m.group(1))
            if v is not None and not any(abs(v - have) < 0.005 for have in values):
                return True
    return False


def strip_wrong_calls(answer: str, notes: list[dict]) -> tuple[str, list[str]]:
    """(the answer without those sentences, the sentences removed). Only a
    sentence that names exactly one of the turn's banks is judged: with two
    banks in one sentence a rating cannot be tied to either. A posted desk
    line (it opens "→ **Bank**") is code's and is never touched."""
    banks = _bank_facts(notes)
    if not answer or not banks:
        return answer, []
    removed: list[str] = []
    out_lines = []
    for raw in answer.split("\n"):
        lead = raw[:len(raw) - len(raw.lstrip("→ \t"))] if raw.lstrip().startswith("→") else ""
        kept, cut = [], False
        for s in _SENTENCE_RE.split(raw[len(lead):]):
            hits = [k for k, f in banks.items() if _named(s.lower(), f["names"])]
            if len(hits) == 1 and s.count("**") % 2 == 0 and _wrong(s, banks[hits[0]]):
                removed.append(s.strip())
                cut = True
            else:
                kept.append(s)
        if not cut:
            out_lines.append(raw)
        elif any(k.strip() for k in kept):
            out_lines.append(lead + " ".join(kept))
        # a line whose every sentence went is dropped with its arrow
    if not removed:
        return answer, []
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip(), removed
