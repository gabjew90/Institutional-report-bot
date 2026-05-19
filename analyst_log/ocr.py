"""Gemini vision extraction for analyst trade screenshots.

Single source of truth for the prompt + extraction logic, shared between the
production watcher (`analyst_log.watcher`) and the offline probe script
(`scripts/test_analyst_ocr.py`).

Prompt calibration history: tuned over 3 probe runs against the live analyst
channel — see commit 9112ad4. Final config hit 13/13 action accuracy and
12/12 year inference on a 13-image sample. The two key design choices:

1. Asymmetric error preference. False-positive opens (phantom positions in
   the log) are MUCH worse than false-negative opens (self-correct on the
   next post). The prompt biases toward "viewing" when ambiguous.

2. Caption taxonomy split into STRONG / AMBIGUOUS / NONE buckets — strong
   close/open signals always win, ambiguous hype defaults to viewing.
"""

import json
import logging
from datetime import datetime, timezone

from google import genai
from google.genai import types

from config import settings

log = logging.getLogger(__name__)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.google_api_key)
    return _client


def build_prompt(today_iso: str, caption: str) -> str:
    """Build the Gemini extraction prompt for one analyst screenshot.

    `today_iso` is YYYY-MM-DD; used for expiry-year inference.
    `caption` is the user's text posted with the image (may be empty).
    """
    cap_block = (
        f'\n\nThe USER POSTED THIS CAPTION WITH THE IMAGE: "{caption.strip()}"\n'
        f'Use this caption as the PRIMARY signal for the `action` field — '
        f'see the caption-mapping table below.'
    ) if caption.strip() else (
        '\n\nNo text caption was posted with this image. Decide action '
        'from the image alone (see fallback rules below).'
    )
    return f"""\
You are looking at an image from a trading group chat. The user is "Abe," a \
trader who posts Robinhood screenshots with specific characteristics. Today's \
date is {today_iso}.

ABE'S TYPICAL POSTING FORMAT — a Robinhood notification card showing:
- Ticker + strike + Call/Put (e.g. "NVDA $150 Call")
- Month/day + Buy/Sell pill (e.g. "5/29 · Buy")
- Green +N% or red -N% gain pill on the right

These are NOTIFICATION cards (current P&L of an existing position), NOT order \
execution tickets. He DOES NOT post quantity, entry price, exit price, or \
spot price on the cards themselves. The "Buy" pill is the ORIGINAL order type, \
not the current action.

When Abe DOES post a stats screen for a specific contract (Bid / Ask / Mark / \
IV visible), extract a `price` value: compute the **midpoint of bid and ask** \
((bid + ask) / 2, rounded to 2 decimals) as a fair proxy for the fill price he \
likely paid or expected. Use Mark if Bid/Ask aren't both visible. Skip if \
neither bid/ask nor mark is legible. For notification cards (no bid/ask shown), \
leave price=null — the entry price isn't derivable from the gain-pill alone. \
For order tickets with an explicit price, use that price directly.

He sometimes also posts:
- A stats/quote screen (Bid/Ask/Mark/IV) when viewing an option to buy
- A tweet screenshot or chart (not a trade — flag as is_trade_screenshot=false)
{cap_block}

CAPTION → ACTION MAPPING — IMPORTANT ASYMMETRY:

False-positive opens (saying "open" when Abe was just viewing) are MUCH \
worse than false-negative opens (saying "viewing" when he actually opened). \
A wrongly-detected open creates a phantom position in the log that the bot \
will reference forever; a missed open self-corrects when Abe posts a \
notification card later. **When in doubt about an open, default to \
"viewing".** Closes are easier to verify (gain-pill + caption usually agree), \
so be more confident classifying closes.

STRONG CLOSE signals → action="close" (these are unambiguous; trust them):
- "I'm out", "OUT", "out!", "exiting", "exit", "sold", "bing bong", "done", \
  "took it", "took profit", "thx for the bag"
- **NOTE on "take/took half" specifically: this is a TRIM, not a CLOSE.** \
  "Took half", "take half", "took some off", "shaved", "scaled out", \
  "lightened up" = partial sell, position still alive. Don't classify \
  these as "close" just because the word "took" appears in the caption.

STRONG OPEN signals → action="open" (clear re-entry or fresh buy language):
- "re-entering", "reload", "back in", "rinse repeat", "opened", "loading", \
  "got some", "in at"
- "slam", "slammed", "slamming", "slam it", "slam this" — in this room's \
  vernacular, "slam" = slam the buy button = aggressive open. It is NOT \
  hype-watching language despite sounding excited; treat it like "loading".
- "add", "adding", "doubled up" → action="add"
- "trim", "trimming", "took some off", "take half", "took half", \
  "half off", "scaled out", "lightened up", "peeled some off", \
  "shaved" → action="trim"

AMBIGUOUS / HYPE captions on a stats_screen → action="viewing" (bias \
toward viewing, not open). Don't assume an open from excitement alone:
- "let's go", "fire", "🚀", "this one", "watch this", "👀"
- These could mean "I just bought" OR "look at this setup" — when on a \
  stats_screen (no execution evidence in the image), default to viewing.
- ONLY classify as open if the caption is genuinely unambiguous (per the \
  STRONG OPEN list above).

NO CAPTION (decision tree depends on screenshot_type AND gain_pct):

A. **Notification card with visible POSITIVE gain (>+5%)**: action="close". \
   Abe doesn't silently post profit screens unless he just took the win — \
   the card itself is the flex/announcement. Treat as a took-profit close. \
   Add a note "no close caption — inferred from gain pill" so /ask can \
   qualify language slightly.

B. **Notification card with near-zero gain (-5% to +5%)** or no gain visible: \
   action="open". A fresh notification card without much gain has just been \
   entered — Abe isn't flexing yet, this is the entry alert. He doesn't post \
   notification cards casually.

C. **Notification card with visible NEGATIVE gain (<-5%)**: action="unclear" \
   (lean toward "open" if you must pick). Posting a losing position silently \
   is unusual — could be a fresh open the trade went red on, could be a \
   stop-out without caption. Without more signal, don't claim a close.

D. **Stats screen for a SPECIFIC CONTRACT** (ticker + strike + expiry all \
   visible, "Buy [TICKER]" or "Sell [TICKER]" header, Bid/Ask/Mark/IV for \
   that ONE contract): action="open". Abe narrowing in on a specific \
   contract IS an entry signal — he doesn't post these for fun. The prior \
   conservative bias was wrong here: single-contract stats screens are \
   confirmed-or-imminent entries, not idle browsing.

   - Exception 1: if there's an explicit AMBIGUOUS-hype caption on a \
     stats screen ("🚀", "let's go", "this one"), that captioned-stats \
     case still goes to "viewing" — the asymmetric error preference \
     still applies when the user gave you some signal. NOTE: "slam" \
     is NOT ambiguous — it means open and overrides this exception. \
     Captionless single-contract stats screens default to "open".

E. **Pure option chain listing** (multiple strikes visible, no specific \
   contract selected) with no caption: action="viewing". This is true \
   browsing — he's surveying the chain.

F. **Order ticket / order-confirmation screen**: action="open". The screen \
   itself confirms execution.

EXPIRY YEAR INFERENCE: Robinhood notification cards show only "M/D" not the \
year. Use today's date ({today_iso}) to infer the year with this priority:

1. If the M/D expiry is on or after today's M/D → CURRENT YEAR.
2. If the M/D expiry is BEFORE today, but within the last 14 days → \
   CURRENT YEAR. Abe is posting about a contract that just expired or \
   is about to expire — those are this-year contracts, not next-year.
3. Only if the M/D expiry is more than 14 days BEFORE today (so clearly \
   stale) → NEXT YEAR.
4. Never use years before today's year unless the screenshot explicitly \
   shows them.

Example: today is 2026-05-16 and screenshot shows "5/15" expiry → 2026-05-15 \
(rule 2, just expired yesterday). Same screenshot with "1/15" expiry → \
2027-01-15 (rule 3, 4 months past — stale, must be next year).

Return STRICT JSON only — no prose, no markdown wrapper.

For trade screenshots:
{{
  "is_trade_screenshot": true,
  "screenshot_type": "notification_card | stats_screen | order_ticket | other",
  "ticker": "string (e.g. NVDA, NOW, MSFT)",
  "contract_type": "call | put | stock | crypto | unclear",
  "strike": number or null,
  "expiry": "YYYY-MM-DD" or null (use the inference rule above),
  "action": "open | add | trim | close | viewing | unclear",
  "action_source": "caption | image | both",
  "gain_pct": number or null (the +/-N% pill, if shown),
  "price": number or null (midpoint of bid/ask on stats screens, or explicit price on order tickets — null on notification cards),
  "caption_summary": "one-line plain English combining what's shown + the caption",
  "confidence": "high | medium | low",
  "notes": "anything notable — Robinhood UI ambiguity, partial info, etc"
}}

For non-trade images:
{{
  "is_trade_screenshot": false,
  "what_it_appears_to_be": "string description",
  "confidence": "high | medium | low"
}}

Output the JSON object ONLY.\
"""


async def extract_trade_from_image(
    image_bytes: bytes,
    mime_type: str,
    caption: str,
    parent_caption: str | None = None,
) -> dict | None:
    """Run Gemini on an analyst trade screenshot. Returns parsed JSON dict
    on success, None on any failure (logged). Failures are non-fatal —
    the watcher should continue processing other images.

    If `parent_caption` is set, it's prepended to the caption as reply-
    chain context. Lets a reply like 'out' on an earlier 'BTO MSFT 430C'
    post resolve to a close of that contract when the image alone is
    ambiguous.
    """
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if parent_caption and parent_caption.strip():
        composed_caption = (
            f"[REPLY-PARENT (caller's earlier post):] {parent_caption.strip()[:500]}\n"
            f"[CURRENT CAPTION:] {caption}"
        )
    else:
        composed_caption = caption
    prompt = build_prompt(today_iso, composed_caption)
    try:
        client = _get_client()
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=600,
                response_mime_type="application/json",
            ),
        )
    except Exception as e:
        log.error(f"Gemini extraction call failed: {e}")
        return None

    text = (response.text or "").strip()
    if not text:
        log.warning("Gemini returned empty extraction response")
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.error(f"Gemini extraction returned non-JSON: {e} | text={text[:200]}")
        return None


# ============================================================================
# Caption-only extraction (no screenshot)
# ============================================================================

_CAPTION_ONLY_PROMPT = """\
You are extracting a structured trade record from a TEXT-ONLY message in
a trade-caller's alerts channel. The caller posts short alerts like:

  "Ndx 29000c @7.00"            → open NDX call, strike 29000, price 7.00
  "Sold all 5/22 435c @3.66"    → close call, strike 435, expiry 5/22, price 3.66
  "MSFT 430c 5/20 @3.65 BTO"    → open MSFT call, strike 430, expiry 5/20, price 3.65
  "Sold 5/20 615c @4.00 meta"   → close META call, strike 615, expiry 5/20, price 4.00
  "Took half PLTR 137c 5/29"    → trim PLTR call, strike 137, expiry 5/29
  "Re-entering AMD 145c 5/29"   → open AMD call, strike 145, expiry 5/29

NOT trades (return is_trade=false):
- "I really think China is a great buy here"   → commentary
- "Anyone else seeing this?"                   → discussion
- "These are 8 now!"                           → reaction (no contract specified)
- "Sold @19.00"                                → too sparse alone (no ticker/strike)
- "Slam"                                       → hype alone

THRESHOLD: a trade row requires at minimum **ticker + strike**. If either
cannot be confidently extracted from the caption alone, treat the message
as NOT a trade — set is_trade=false. Do NOT guess on ticker or strike. Do
NOT fall back to a 0 sentinel.

**Expiry default — 0DTE.** If the caption has NO expiry value (or you can't
parse one), set `expiry` to today's date ({today_iso}). Terse callers often
omit expiry on intraday lottos because the contract is same-day; assume
0DTE rather than dropping the trade. If the caption DOES have an explicit
M/D, use that with the year-inference rule below.

Caller shorthand to recognize (uppercase canonical):
- "rut" → RUT, "ndx" → NDX, "spx" → SPX, "spy" → SPY, "qqq" → QQQ
- "msft" → MSFT, "meta" → META, "pltr" → PLTR, "tsla" → TSLA
- "chyna" / "moar chyna" / "CHYYYNNAAA" → FXI (China large-cap ETF; common shorthand)
- "coin" → COIN, "mstr" → MSTR, "amd" → AMD, "nvda" → NVDA, "igv" → IGV

Strike syntax: a number directly followed by 'c' (call) or 'p' (put), e.g.
"94c" → strike 94 call, "29000c" → strike 29000 call. Decimal strikes allowed
("207.5c" → 207.5 call).

Expiry: "M/D" or "MM/DD" or "MM-DD" formats — use today's date ({today_iso})
to infer the year. If the M/D is on/after today → current year. If 1-14 days
ago → current year (just expired or about to). If more than 14 days ago →
next year. **If no expiry value appears in the caption at all, use today's
date** ({today_iso}) — that's the 0DTE default.

Price: read from "@PRICE" patterns. "@3.65" → price 3.65. If no price
visible, leave null.

Action keywords:
- "sold" / "sold all" / "exit" / "out" → close
- "took half" / "took some off" / "trimmed" / "scaled" → trim
- "added" / "more" / "doubled" → add
- "BTO" / "bought" / "re-entering" / "reload" / "back in" / "in at" → open
- No action keyword + new contract → open (default)
- "Slam" / "slammed" → open (this caller's vernacular for aggressive entry)

Return STRICT JSON only:

For trades:
{{
  "is_trade_screenshot": true,
  "screenshot_type": "caption_only",
  "ticker": "<UPPERCASE TICKER>",
  "contract_type": "call" | "put" | "stock",
  "strike": <number — REQUIRED, no sentinel>,
  "expiry": "YYYY-MM-DD" — REQUIRED,
  "action": "open" | "add" | "trim" | "close" | "viewing",
  "action_source": "caption",
  "gain_pct": null,
  "price": <number from @PRICE pattern, or null>,
  "caption_summary": "one-line plain English of what the caller is doing",
  "confidence": "high" | "medium" | "low",
  "notes": ""
}}

For non-trades:
{{
  "is_trade_screenshot": false,
  "what_it_appears_to_be": "<short description>",
  "confidence": "high" | "medium" | "low"
}}

CAPTION:
\"\"\"{caption}\"\"\"

Output the JSON object only.
"""


async def extract_trade_from_caption(
    caption: str,
    parent_caption: str | None = None,
) -> dict | None:
    """Caption-only extraction path for callers who post pure text
    alerts without a screenshot. Requires ticker + strike;
    expiry defaults to today (0DTE) if not present.

    If `parent_caption` is set, it's prepended as reply-chain context —
    e.g. caption='closed' + parent_caption='BTO MSFT 430C 5/20 @3.65'
    resolves to a CLOSE of that exact contract.

    Returns parsed JSON dict on success, None on any failure (logged).
    Failures are non-fatal — the watcher should continue.
    """
    if not caption or not caption.strip():
        return None
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Compose the caption block — when this is a reply, the parent
    # message provides the position the reply is acting on.
    if parent_caption and parent_caption.strip():
        composed = (
            f"[REPLY-PARENT (caller's earlier post — use for ticker/strike/expiry "
            f"if the new message is sparse like 'closed' / 'sold @X'):]\n"
            f"{parent_caption.strip()[:500]}\n\n"
            f"[CURRENT MESSAGE (the action — apply action verb here to the "
            f"contract from the parent):]\n"
            f"{caption.strip()[:1000]}"
        )
    else:
        composed = caption.strip()[:1000]
    prompt = _CAPTION_ONLY_PROMPT.format(
        today_iso=today_iso, caption=composed
    )
    try:
        client = _get_client()
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=600,
                response_mime_type="application/json",
            ),
        )
    except Exception as e:
        log.error(f"Caption extraction call failed: {e}")
        return None

    text = (response.text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.error(f"Caption extraction non-JSON: {e} | text={text[:200]}")
        return None

    # Enforce the minimum threshold post-hoc — model might over-claim
    # is_trade=true on sparse input. Ticker + strike are required; expiry
    # defaults to today (0DTE) if the model didn't provide one. If ticker
    # or strike is missing, downgrade to non-trade so we don't write a
    # junk row.
    if data.get("is_trade_screenshot"):
        missing = []
        if not data.get("ticker"):
            missing.append("ticker")
        strike = data.get("strike")
        if strike is None or strike == 0:
            missing.append("strike")
        if missing:
            log.debug(
                f"Caption extraction downgraded to non-trade — "
                f"missing required fields: {missing} | caption={caption[:120]!r}"
            )
            return {
                "is_trade_screenshot": False,
                "what_it_appears_to_be": (
                    f"caption-only post but missing {', '.join(missing)} for a complete trade record"
                ),
                "confidence": "high",
            }
        # 0DTE default — if model didn't fill expiry, use today
        if not data.get("expiry"):
            data["expiry"] = today_iso
            data["notes"] = (
                (data.get("notes") or "")
                + " (expiry defaulted to today — 0DTE)"
            ).strip()
    return data
