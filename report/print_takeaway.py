"""Quick Takeaway under an economic print (owner, 2026-09-30).

The print embed carries the numbers. The takeaway is two or three
bullets that say what they mean, written by Gemini from exactly two
inputs and nothing else:

  1. the print itself: every series with its actual, consensus, prior
     and verdict, plus any computed extras (a 3-month annualized rate,
     a saving-rate change, a revision);
  2. the bank research the pulse ingested in the last ten days that
     speaks to this release (previews, consensus calls, what the desks
     said would matter), each item carrying its bank.

It never sees prices, news or the room. Guardrails in code: every
figure in a bullet must appear in those inputs (discord_bot.
figure_provenance) or the bullet is dropped, the pulse voice rules
apply (ai_analysis.voice_rules), bullets are capped at three and 60
words, and any failure returns no takeaway so the print is never
delayed or blocked by it.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

MAX_BULLETS = 3
MAX_WORDS = 60
RESEARCH_DAYS = 10
RESEARCH_NOTES = 10
MAX_OTHER_NOTES = 2
TIER_ONE = ("goldman sachs", "morgan stanley", "jpmorgan", "j.p. morgan", "citi", "bofa",
            "bank of america", "deutsche bank", "ubs", "barclays")
_DEGREE_ADVERBS = re.compile(
    r"\b(?:perfectly|significantly|dramatically|massively|extremely|incredibly|remarkably|hugely)\b",
    re.I)

# What in the research counts as being about each release.
RESEARCH_TERMS = {
    "cpi": r"\bcpi\b|consumer price|core inflation|shelter inflation",
    "jobs": r"payrolls?|\bnfp\b|unemployment|jobs report|labor market|hourly earnings",
    "pce": r"\bpce\b|personal income|personal spending|saving rate|consumer spending",
    "fomc": r"\bfomc\b|\bfed\b.{0,40}\b(?:hike|cut|hold|decision|meeting|pause)|rate (?:hike|cut) odds|dot plot|fed funds",
}


def research_for_release(key: str, days: int = RESEARCH_DAYS, limit: int = RESEARCH_NOTES,
                         period: str = "") -> list[dict]:
    """Bank notes from the last `days` that mention the release: the
    matching macro_indicators (with their released/forecast status) and
    key_insights, one entry per PDF, ranked by reduce_research (`period`
    is the month name of the release). Never raises."""
    import db
    pat = RESEARCH_TERMS.get(key)
    if not pat:
        return []
    rx = re.compile(pat, re.I)
    try:
        rows = db.get_connection().execute(
            """SELECT a.created_at, a.analysis_json FROM pdf_analyses a
               WHERE a.id IN (SELECT MAX(id) FROM pdf_analyses GROUP BY pdf_file_id)
                 AND a.created_at >= datetime('now', ?)
                 AND a.priority IN ('high', 'medium')
               ORDER BY a.id DESC""",
            (f"-{int(days)} days",)).fetchall()
    except Exception as e:
        log.warning(f"print takeaway: research query failed: {e}")
        return []
    return reduce_research(
        [(r["created_at"], r["analysis_json"]) for r in rows], rx, limit, period)


def _tier(source: str) -> int:
    """0 for the big-bank desks, 1 for everyone else."""
    s = (source or "").lower()
    return 0 if any(re.search(rf"\b{re.escape(t)}", s) for t in TIER_ONE) else 1


def reduce_research(rows: list[tuple[str, str]], rx: re.Pattern, limit: int,
                    period: str = "") -> list[dict]:
    """One entry per PDF, ranked: tier-1 banks first, and within a tier the
    notes with a forecast for `period` (the month name the extraction
    stores, like "August") ahead of the rest. At most MAX_OTHER_NOTES
    notes from outside tier 1 are kept so a gold council or a regional
    bank cannot crowd out the desks. The sort is stable, so ties keep
    the newest-first order the rows arrive in."""
    found: list[dict] = []
    for created_at, raw in rows:
        try:
            a = json.loads(raw or "{}")
        except Exception:
            continue
        macro = [
            {k: m.get(k) for k in ("indicator", "reading", "interpretation", "status", "period") if m.get(k)}
            for m in (a.get("macro_indicators") or [])
            if isinstance(m, dict) and rx.search(json.dumps(m))
        ]
        insights = [str(s)[:300] for s in (a.get("key_insights") or []) if rx.search(str(s))]
        if not (macro or insights):
            continue
        found.append({
            "source": a.get("source") or "",
            "title": (a.get("title") or "")[:120],
            "published": (a.get("published_at") or created_at or "")[:10],
            "macro": macro[:3],
            "insights": insights[:3],
        })

    def same_period(n: dict) -> bool:
        return bool(period) and any(
            m.get("status") == "forecast" and period.lower() in str(m.get("period") or "").lower()
            for m in n["macro"])

    found.sort(key=lambda n: (_tier(n["source"]), 0 if same_period(n) else 1))
    out: list[dict] = []
    others = 0
    for n in found:
        if _tier(n["source"]) == 1:
            if others >= MAX_OTHER_NOTES:
                continue
            others += 1
        out.append(n)
        if len(out) >= limit:
            break
    return out


SYSTEM = (
    "You write the Quick Takeaway under an economic data print for a Discord of "
    "self-directed options and crypto traders. They are smart but not finance "
    "professionals: plain English, no jargon left untranslated.\n\n"
    "You get two blocks. DATA is the print: each series with actual, consensus, "
    "prior and a verdict, plus computed extras. RESEARCH is what banks wrote about "
    "this release in the last ten days, each item with its bank.\n\n"
    "Write two or three bullets. Each bullet is one short bold label (one to four "
    "words, like 'Disinflation in play' or 'The caveat') followed by one or two "
    "sentences, under 60 words. Say what the numbers mean and how they compare with "
    "what the banks expected. Attribute every bank view to the bank by name, inside "
    "the sentence ('BofA expected 60K', 'payback BofA had flagged'). Never open with "
    "the bank and a verb of saying ('BofA says', 'GS notes', 'JPM sees'): those "
    "bullets are discarded.\n\n"
    "Each bullet makes one point a trader can act on or check: what changed versus "
    "expectation, what it implies for the next Fed decision or for the series next "
    "month, and which bank said so.\n\n"
    "Hard rules: every number you write must appear in DATA or RESEARCH, and if a "
    "figure is not there you do not state it. No trade recommendations. No "
    "prediction of the market reaction unless a named bank made it. No em-dashes, "
    "no semicolons, no words like crucial, pivotal, robust, notably. No adverbs of "
    "degree (perfectly, significantly, dramatically): say the number and the gap "
    "instead. The room's own chat is never an input.\n\n"
    "Return JSON only: {\"bullets\": [{\"label\": str, \"text\": str}]}"
)


# The retry, sent only when a bullet repeated the table (restates_table).
RESTATED_FEEDBACK = (
    "Your last answer repeated numbers from DATA without saying what they mean. "
    "Write the bullets again. Each one says why the print came out this way or what "
    "it means for the Fed or next month, using what a named bank in RESEARCH said.")
RETRY_IF_FIRST_UNDER_S = 7.0
# The whole takeaway, research query included, must finish inside
# print_watch.TAKEAWAY_TIMEOUT_S (20 s); this leaves room for the guard.
BUDGET_S = 17.0


def build_user(title: str, rows: list[dict], extras: list[str], research: list[dict]) -> str:
    data = {"release": title, "series": rows, "extras": extras}
    return (f"DATA\n{json.dumps(data, ensure_ascii=False)}\n\n"
            f"RESEARCH\n{json.dumps(research, ensure_ascii=False)[:6000]}")


def evidence_text(rows: list[dict], extras: list[str], research: list[dict]) -> str:
    return json.dumps({"rows": rows, "extras": extras, "research": research}, ensure_ascii=False)


# Words that say what a print means rather than what it was: a cause, a
# trend, the Fed. A bullet whose figures all come from the print table
# and that carries none of these only repeats the table (2026-10-02 jobs
# takeaway: "The unemployment rate hit 4.2% ... Average hourly earnings
# grew 0.1%, missing the 0.3% consensus", while BofA's preview in the
# research said a weak headline would be seasonal payback and would not
# move Fed pricing).
_MEANING_RE = re.compile(
    r"\b(?:fed|fomc|rate cuts?|rate hikes?|cuts?|hikes?|policy|pric(?:ing|ed)|odds"
    r"|impl(?:y|ies)|means?|signals?|suggests?|points? to|payback|seasonal|underlying"
    r"|trend|revis(?:ion|ions|ed)|next month|cpi|because|driven|despite|offsets?)\b",
    re.I)


def restates_table(line: str, data_evidence: str) -> bool:
    """True when every figure in `line` is in the print's own rows and the
    line says nothing about cause, trend or the Fed."""
    from discord_bot import figure_provenance as fp
    figs, beyond_table = fp.unsourced_figures(line, data_evidence)
    return bool(figs) and not beyond_table and not _MEANING_RE.search(line)


def guard(bullets: list[dict], evidence: str, data_evidence: str | None = None,
          stats: dict | None = None) -> list[str]:
    """Keep bullets whose every figure is in the evidence and whose text
    passes the voice rules. With `data_evidence` (the print's own rows),
    a bullet that only repeats the table is dropped too and counted in
    stats["restated"]. Returns rendered '**Label:** text' lines."""
    from ai_analysis.voice_rules import compose_lint_patterns
    from discord_bot import figure_provenance as fp
    pats = [(re.compile(rx, re.I), kind) for rx, kind in compose_lint_patterns()]
    out: list[str] = []
    for b in bullets:
        if not isinstance(b, dict):
            continue
        # Em-dashes and semicolons are the hard voice ban. They are
        # rewritten in the strings that ship, not in a checked copy.
        label = re.sub(r"[*:]+$", "", str(b.get("label") or "").strip())
        label = label.replace("—", ",").replace(";", ",")
        text = str(b.get("text") or "").strip().replace("—", ",").replace(";", ",")
        if not label or not text:
            continue
        line = f"{label}: {text}"
        if len(line.split()) > MAX_WORDS + 6:
            continue
        broken = [kind for p, kind in pats if p.search(line)]
        if broken or _DEGREE_ADVERBS.search(line):
            log.info(f"print takeaway: dropped bullet for voice {broken or ['degree-adverb']}: {line[:80]!r}")
            continue
        _figs, missing = fp.unsourced_figures(line, evidence)
        if missing:
            log.info(f"print takeaway: dropped bullet with unsourced {[m.token for m in missing]}")
            continue
        if data_evidence is not None and restates_table(line, data_evidence):
            log.info(f"print takeaway: dropped bullet that repeats the table: {line[:80]!r}")
            if stats is not None:
                stats["restated"] = stats.get("restated", 0) + 1
            continue
        out.append(f"• **{label}:** {text}")
        if len(out) >= MAX_BULLETS:
            break
    return out


def generate(key: str, title: str, rows: list[dict], extras: list[str] | None = None,
             research: list[dict] | None = None, client=None, model: str | None = None,
             period: str = "") -> list[str]:
    """The rendered takeaway lines, or [] when nothing usable came back.
    `period` is the release's month name, used to rank the research."""
    from google.genai import types
    from config import settings
    import time
    started = time.monotonic()
    extras = extras or []
    if research is None:
        research = research_for_release(key, period=period)

    def ask(contents: str) -> list | None:
        resp = client.models.generate_content(
            model=model or settings.gemini_model, contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM, temperature=0.3, max_output_tokens=700,
                response_mime_type="application/json"),
        )
        data = json.loads(resp.text or "{}")
        bullets = data.get("bullets") if isinstance(data, dict) else data
        return bullets if isinstance(bullets, list) else None

    user = build_user(title, rows, extras, research)
    evidence = evidence_text(rows, extras, research)
    table = evidence_text(rows, extras, [])
    try:
        if client is None:
            from ai_analysis.usage_ledger import make_client
            client = make_client("print_takeaway")
        bullets = ask(user)
    except Exception as e:
        log.warning(f"print takeaway: generation failed (print posts without it): {e}")
        return []
    if bullets is None:
        return []
    stats: dict = {}
    lines = guard(bullets, evidence, table, stats)
    # One retry when a bullet only repeated the table and the research has
    # something to say. print_watch gives the whole takeaway 20 s
    # (TAKEAWAY_TIMEOUT_S) and posts without it on a timeout, so the retry
    # runs in its own thread and is abandoned, keeping the first answer,
    # once the budget is spent.
    elapsed = time.monotonic() - started
    if stats.get("restated") and research and elapsed < RETRY_IF_FIRST_UNDER_S:
        import concurrent.futures as cf
        pool = cf.ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(ask, user + "\n\n" + RESTATED_FEEDBACK)
        try:
            again = fut.result(timeout=max(0.0, BUDGET_S - elapsed))
        except Exception as e:
            log.info(f"print takeaway: retry failed or ran long, keeping the first answer: {e!r}")
            again = None
        finally:
            pool.shutdown(wait=False)
        if again is not None:
            retry_lines = guard(again, evidence, table, {})
            if len(retry_lines) > len(lines):
                lines = retry_lines
    return lines


