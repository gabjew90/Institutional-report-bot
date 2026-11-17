"""A desk's rating and price target, posted by code (2026-11-17, audit run 12 A1).

"JPMorgan reiterates its Underweight rating with a $215 price target" was
posted on 2026-11-16. The note says Overweight, target cut to $215 from
$320, and its stored call carried exactly that: rating "Overweight", price
target "$215.00". The outline handed those fields to the model as a DESK
slot and the model worded them, inverting the rating.

A rating and a target are structured fields of the note, so they are a
posted line like the price or the EPS (discord_bot/stock_body.py):

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


def prior_target(note: dict, call: dict) -> tuple[str, str] | None:
    """("cut" | "raised", the previous target as the note writes it), from
    a sentence of the note that states this target against the old one."""
    target = _number(_clean(call.get("price_target")))
    if not target:
        return None
    whole = f"{target:.2f}".rstrip("0").rstrip(".")
    digits = re.escape(whole.split(".")[0])
    frac = whole.split(".")[1] if "." in whole else ""
    # the same number with or without thousands commas and trailing zeros
    num = r"(?<![\d.,])[^\s\d(),;:]{0,4}" + r",?".join(digits) + (
        rf"\.{frac}0*" if frac else r"(?:\.0+)?") + r"(?![\d,]|\.\d)"
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


_ACTION_HEAD = {"upgrade": ("upgraded to {r}", "upgraded"),
                "downgrade": ("downgraded to {r}", "downgraded"),
                "initiate": ("initiated at {r}", "initiated coverage")}


def line(note: dict, preview: bool = False) -> str:
    """The posted line for a note's rating and target, '' when the note
    carries neither. `preview`: the note was written before a report that
    is now out."""
    call = call_of(note)
    bank = str(note.get("source") or "").strip()
    if not call or not bank:
        return ""
    rating = _clean(call.get("rating"))
    target = _trim(_clean(call.get("price_target")))
    with_rating, bare = _ACTION_HEAD.get(str(call.get("action") or "").lower(), ("rated {r}", ""))
    parts = []
    head = with_rating.format(r=rating) if rating else bare
    if head:
        parts.append(head)
    if target:
        piece = f"price target **{target}**"
        prior = prior_target(note, call)
        if prior:
            piece += f", {prior[0]} from {prior[1]}"
        parts.append(piece)
    if not parts:
        return ""
    who = f"**{bank}**"
    if call.get("call_by"):
        # the note relays another broker's action (2026-10-26 audit A21)
        who = f"**{call['call_by']}**, as relayed in a {bank} note"
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


def _in_text(rating: str, text: str) -> bool:
    """The rating as a word of the note's own text ('Equal Weight' for
    'equal-weight'; not 'buy' inside 'buyback')."""
    parts = [re.escape(p) for p in re.split(r"[\s-]+", rating.strip()) if p]
    return bool(parts) and bool(re.search(r"\b" + r"[\s-]?".join(parts) + r"\b", text or "", re.I))


def _bank_facts(notes: list[dict]) -> dict[str, dict]:
    """bank key -> {"names", "ratings", "targets", "text"} from its notes."""
    from discord_bot.desk_claims import _key
    out: dict[str, dict] = {}
    for n in notes or []:
        key = _key(str(n.get("source") or ""))
        if not key:
            continue
        f = out.setdefault(key, {"names": _names(str(n.get("source") or "")), "ratings": set(),
                                 "targets": [], "text": ""})
        for c in n.get("calls") or []:
            if _clean(c.get("rating")):
                f["ratings"].add(_norm(_clean(c["rating"])))
            for v in (_clean(c.get("price_target")), (prior_target(n, c) or ("", ""))[1]):
                if _number(v) is not None:
                    f["targets"].append(_number(v))
            f["text"] += " " + str(c.get("rationale") or "")
        for k in ("insights", "earnings", "risks"):
            f["text"] += " " + " ".join(str(x) for x in n.get(k) or [])
        for d in n.get("data_points") or []:
            f["text"] += " " + " ".join(str(d.get(k) or "") for k in ("figure", "metric", "context"))
        for t in n.get("trade_ideas") or []:
            f["text"] += " " + " ".join(str(t.get(k) or "") for k in ("description", "rationale"))
    return out


def _wrong(sentence: str, facts: dict) -> bool:
    """The sentence gives this bank a rating or a target none of its notes
    carry, as a field or in their words."""
    for i, rx in enumerate(_RATING_CLAIM_RES):
        for m in rx.finditer(sentence):
            word = m.group(1)
            if i and word.lower() in _CASE_SENSITIVE and not word[0].isupper():
                continue                    # after a verb, "keeps a hold on costs" is prose
            if _norm(word) not in facts["ratings"] and not _in_text(word, facts["text"]):
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
