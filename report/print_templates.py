"""The alert's takeaway, built from fixed sentences filled with the
release's own figures (2026-10-28 redesign, owner-approved text in
docs/superpowers/specs/2026-10-28-print-alerts-deterministic-design.md).

No model call: the alert posts the moment the agency's number is read,
and the same verdict always reads the same way. Two or three sentences:
what came in hotter, cooler, stronger or weaker, the supporting facts,
then what it means for the Fed and the first market effect. Sentences
state facts and never say a component "drove" the result, since
contributions are not computed.
"""
from __future__ import annotations

from datetime import date

from report.print_data import CORE_MOM, Release
from report.print_verdict import DOVISH, HAWKISH, Verdict

# A food move this size earns a place beside shelter and energy, in points.
FOOD_MIN_PT = 0.3
# Net payroll revisions this size are worth a sentence, in thousands.
REVISION_SENTENCE_K = 50

_FED = {
    (HAWKISH, "tightening"): "That leans hawkish and adds to the case for another hike",
    (HAWKISH, "easing"): "That leans hawkish and weakens the case for another cut",
    (HAWKISH, "neutral"): "That leans hawkish",
    (DOVISH, "tightening"): "That leans dovish and eases pressure on the Fed to hike again",
    (DOVISH, "easing"): "That leans dovish and adds to the case for another cut",
    (DOVISH, "neutral"): "That leans dovish",
}
_YIELDS = {HAWKISH: "lift Treasury yields", DOVISH: "pull Treasury yields lower"}
NO_SIGNAL = "The report gives the Fed no clear new signal."
INCOMPLETE = "The initial release is incomplete."
_COUNT = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def month_name(period: str) -> str:
    return _MONTHS[int(period[5:7]) - 1]


def fed_sentence(direction: str | None, policy: dict) -> str:
    if direction not in (HAWKISH, DOVISH):
        return NO_SIGNAL
    base = _FED[(direction, policy.get("direction") or "neutral")]
    return f"{base}, and reports like this tend to {_YIELDS[direction]}."


def _moved(value: float) -> str:
    if round(value, 1) == 0:
        return "was flat"
    return f"{'rose' if value > 0 else 'fell'} {abs(value):.1f}%"


def _facts(items: list[tuple[str, float]], tail: str = "") -> str:
    """'Shelter rose 0.4% and energy 5.2% on the month.': one clause per
    item, the verb left out when it repeats the one before."""
    clauses, last_verb = [], None
    for name, value in items:
        moved = _moved(value)
        verb = moved.split()[0]
        clauses.append(f"{name} {moved.split(' ', 1)[1]}" if verb == last_verb and verb != "was"
                       else f"{name} {moved}")
        last_verb = verb
    text = clauses[0] if len(clauses) == 1 else ", ".join(clauses[:-1]) + " and " + clauses[-1]
    return text[0].upper() + text[1:] + tail + "."


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _point_phrase(size: float | None) -> str:
    return {0.25: "a quarter point", 0.5: "half a point", 0.75: "three quarters of a point",
            1.0: "a full point"}.get(round(size, 2) if size is not None else None,
                                     f"{size:.2f} point" if size else "")


# ------------------------------------------------------------ inflation

def _inflation_opening(rel: Release, v: Verdict, subject: str, plural: str) -> str:
    """`subject` names the core measure ("Core inflation", "Core producer
    prices"), `plural` the pair ("inflation", "producer prices")."""
    core = rel.value(CORE_MOM[rel.key])
    if v.kind == "hotter":
        return f"{subject} rose more than expected."
    if v.kind == "cooler":
        return (f"{subject} rose less than expected." if core is not None and core > 0
                else f"{subject} came in lower than expected.")
    if v.kind == "no_forecast":
        if core is not None:
            return f"{subject} {_moved(core)} on the month."
        yoy = rel.value(CORE_MOM[rel.key].replace("m/m", "y/y"))
        if yoy is not None:
            return f"{subject} ran at {yoy:.1f}% over the year."
    if v.kind == "mixed":
        return f"Core and headline {plural} pointed in opposite directions."
    if v.kind == "in_line":
        head = v.signals.get("headline")
        if head:
            return (f"Core {plural} matched expectations, while headline came in "
                    f"{'higher' if head > 0 else 'lower'}.")
        if head is None:
            # no headline forecast to compare (PCE's feed lists core only)
            return f"{subject} matched expectations."
        return f"Core and headline {plural} matched expectations."
    return ""


def _cpi(rel: Release, v: Verdict) -> list[str]:
    out = [_inflation_opening(rel, v, "Core inflation", "inflation")]
    items = [(n, rel.value(lbl)) for n, lbl in (("shelter", "Shelter m/m"), ("energy", "Energy m/m"))]
    food = rel.value("Food m/m")
    if food is not None and abs(food) >= FOOD_MIN_PT - 1e-9:
        items.append(("food", food))
    items = [(n, x) for n, x in items if x is not None]
    if items:
        out.append(_facts(items, " on the month"))
    return out


def _pce(rel: Release, v: Verdict) -> list[str]:
    subject = "Core PCE, the Fed's preferred inflation gauge," if v.kind in ("hotter", "cooler") \
        else "Core inflation"
    out = [_inflation_opening(rel, v, subject, "inflation")]
    real = rel.value("Real Consumer Spending m/m")
    saving, saving_row = rel.value("Saving Rate"), rel.rows.get("Saving Rate")
    parts = []
    if real is not None:
        parts.append(f"real consumer spending {_moved(real)}")
    if saving is not None:
        before = (saving_row or {}).get("prior_value")
        if before is None or round(before, 1) == round(saving, 1):
            parts.append(f"the saving rate held at {saving:.1f}%")
        else:
            parts.append(f"the saving rate {'rose' if saving > before else 'fell'} to {saving:.1f}%")
    if parts:
        out.append(_cap(_join(parts)) + ".")
    return out


def next_release(key: str, today_iso: str) -> str:
    """'Wed Nov 25' for the next scheduled release of `key`, or ''."""
    from world_context import OFFICIAL_RELEASES
    for day in sorted(OFFICIAL_RELEASES):
        if day > today_iso and key in OFFICIAL_RELEASES[day]:
            d = date.fromisoformat(day)
            return f"{d.strftime('%a %b')} {d.day}"
    return ""


def _ppi(rel: Release, v: Verdict, today_iso: str) -> list[str]:
    out = [_inflation_opening(rel, v, "Core producer prices", "producer prices")]
    if rel.prior_revisions:
        r = rel.prior_revisions[0]
        out.append(f"{month_name(r['period'])} was revised to {r['now']:+.1f}% from {r['was']:+.1f}%.")
    else:
        when = next_release("pce", today_iso)
        if when:
            out.append(f"PPI matters mostly for what it implies for core PCE, out {when}.")
    return out


# ----------------------------------------------------------------- jobs

def _jobs(rel: Release, v: Verdict) -> list[str]:
    s = v.signals
    rev = rel.payroll_revisions or {}
    net = rev.get("net")
    phrases = {
        ("unemployment", 1): "unemployment came in lower than expected",
        ("unemployment", -1): "unemployment came in higher than expected",
        ("wages", 1): "wage growth was faster than expected",
        ("wages", -1): "wage growth was slower than expected",
        ("payrolls", 1): "payroll growth beat expectations",
        ("payrolls", -1): "payroll growth fell short of expectations",
    }
    others = [(k, s.get(k)) for k in ("unemployment", "wages") if s.get(k)]
    if v.kind == "no_forecast":
        pay, unemp = rel.value("Non Farm Payrolls"), rel.value("Unemployment Rate")
        facts = []
        if pay is not None:
            facts.append(f"payrolls {'rose' if pay >= 0 else 'fell'} by {abs(pay):,.0f}K")
        if unemp is not None:
            facts.append(f"unemployment was {unemp:.1f}%")
        return [_cap(_join(facts)) + "."] if facts else []
    if s.get("cancelled"):
        beat = s["cancelled"] > 0
        first = (f"Payroll growth {'beat' if beat else 'fell short of'} expectations, but "
                 f"{'downward' if beat else 'upward'} revisions to the prior two months cancel it out.")
        out = [first]
        if others:
            out.append(_cap(_join([phrases[o] for o in others])) + ".")
        return out
    named = [(k, s.get(k)) for k in ("payrolls", "unemployment", "wages") if s.get(k)]
    if not named:
        out = ["Hiring, unemployment and wages all matched expectations."]
    else:
        stronger = [phrases[n] for n in named if n[1] > 0]
        weaker = [phrases[n] for n in named if n[1] < 0]
        text = _join(stronger) if stronger else ""
        if weaker:
            text = f"{text}, but {_join(weaker)}" if text else _join(weaker)
        out = [_cap(text) + "."]
    if net is not None and abs(net) >= REVISION_SENTENCE_K and len(rev.get("months") or []) == 2:
        out.append(f"The two prior months were revised {'down' if net < 0 else 'up'} by {abs(net):,.0f}K combined.")
    else:
        part = rel.rows.get("Participation Rate")
        a, p = (part or {}).get("actual_value"), (part or {}).get("prior_value")
        if a is not None and p is not None and round(a, 1) != round(p, 1):
            out.append(f"Participation {'rose' if a > p else 'fell'} to {a:.1f}%.")
    return out


# ----------------------------------------------------------------- FOMC

_TOPICS = (("growth", ("activity", "growth", "spending", "investment", "productivity")),
           ("inflation", ("inflation", "price")),
           ("jobs", ("job", "unemployment", "labor", "employment")))


def statement_topics(changes: list[tuple[str | None, str | None]]) -> list[str]:
    text = " ".join(f"{a or ''} {b or ''}" for a, b in changes).lower()
    return [name for name, words in _TOPICS if any(w in text for w in words)]


def _dissent_clause(dissents: list[dict]) -> str:
    bits = []
    for g in dissents:
        n = len(g["names"])
        what = {"hike": "a hike", "cut": "a cut", "hold": "no change"}[g["move"]]
        bits.append(f"{_COUNT.get(n, str(n))} official{'s' if n > 1 else ''} voted for {what}")
    return _join(bits)


def projection_sentence(proj: dict | None, year: str) -> str | None:
    """Next year's median rate projection against the previous projection
    meeting's: 'Officials now see rates at 3.6% at the end of 2027, up
    from 3.4% in September.'"""
    if not proj:
        return None
    nxt = str(int(year) + 1)
    now, was = proj["median"].get(nxt), proj["prev_median"].get(nxt)
    if now is None:
        return None
    if was is None or not proj.get("prev_label"):
        return f"Officials see rates at {now:.1f}% at the end of {nxt}."
    if round(now - was, 4) == 0:
        return f"Officials still see rates at {now:.1f}% at the end of {nxt}, as in {proj['prev_label']}."
    way = "up" if now > was else "down"
    return (f"Officials now see rates at {now:.1f}% at the end of {nxt}, "
            f"{way} from {was:.1f}% in {proj['prev_label']}.")


def _fomc(rel: Release, v: Verdict) -> list[str]:
    f = rel.fomc or {}
    rng = f"{f['low']:.2f}% to {f['high']:.2f}%"
    by = _point_phrase(f.get("size"))
    if f["action"] == "hold":
        s1 = f"The Fed held rates at {rng}"
    else:
        verb = "raised" if f["action"] == "hike" else "cut"
        s1 = f"The Fed {verb} rates {'by ' + by + ' ' if by else ''}to {rng}"
    decision = v.signals.get("decision")
    if decision == 0:
        s1 += ", as expected"
    elif decision is not None:
        s1 += ", which was not expected"
    if f.get("dissents"):
        s1 += f", and {_dissent_clause(f['dissents'])}"
    out = [s1 + "."]
    # at projection meetings the rate outlook takes the second sentence,
    # otherwise the statement's changed topics do
    outlook = projection_sentence(f.get("projections"), rel.period[:4])
    topics = statement_topics(f.get("changes") or [])
    if outlook:
        out.append(outlook)
    elif topics:
        out.append(f"The statement changed how it describes {_join(topics)}.")
    if v.direction in (HAWKISH, DOVISH):
        out.append(f"Decisions like this tend to {_YIELDS[v.direction]}.")
    elif decision == 0:
        out.append("With the decision expected, the press conference at 2:30 PM ET is the next test.")
    else:
        out.append("The press conference at 2:30 PM ET is the next test.")
    return out


# ---------------------------------------------------------------- entry

def takeaway(rel: Release, v: Verdict, policy: dict, today_iso: str) -> str:
    """The takeaway text, without its 'Takeaway:' label."""
    if v.kind == "incomplete":
        return INCOMPLETE
    if rel.key == "fomc":
        return " ".join(_fomc(rel, v))
    if rel.key == "jobs":
        body = _jobs(rel, v)
    elif rel.key == "pce":
        body = _pce(rel, v)
    elif rel.key == "ppi":
        body = _ppi(rel, v, today_iso)
    else:
        body = _cpi(rel, v)
    body = [b for b in body if b]
    if v.kind != "no_forecast":
        body = body[:2] + [fed_sentence(v.direction, policy)]
    return " ".join(body[:3])
