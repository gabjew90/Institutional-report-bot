"""Gemini vision extraction for analyst trade screenshots.

Single source of truth for the prompt + extraction logic, shared between the
production watcher (`analyst_log.watcher`) and the offline probe script
(`scripts/test_analyst_ocr.py`).

Prompt calibration history: tuned over 3 probe runs against the live analyst
channel — see commit 9112ad4. Final config hit 13/13 action accuracy and
12/12 year inference on a 13-image sample. The two key design choices:

1. Asymmetric error preference. False-positive opens (phantom positions in
   the log) are MUCH worse than false-negative opens (self-correct on the
   next post). The prompt biases toward `is_trade_screenshot=false` when
   ambiguous (previously biased to `action="viewing"`, but the resulting
   rows just polluted /ask RECENT TRADES — see 2026-05-28 QC).

2. Caption taxonomy split into STRONG / AMBIGUOUS / NONE buckets — strong
   close/open signals always win, ambiguous hype is downgraded to
   `is_trade_screenshot=false` (not "viewing").
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


def build_prompt(today_iso: str, caption: str, caller_name: str = "the caller") -> str:
    """Build the Gemini extraction prompt for one analyst screenshot.

    `today_iso` is YYYY-MM-DD; used for expiry-year inference.
    `caption` is the user's text posted with the image (may be empty).
    `caller_name` is the display name of the caller whose channel this
    screenshot is from (e.g. "Abe", "BK"). Used to scope the model's
    references — without this, the model defaults to assuming "Abe" no
    matter which channel the screenshot came from.
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
You are looking at an image from a trading group chat. The poster is **{caller_name}**, \
a trader who posts trade screenshots — Robinhood notification cards, stats \
screens, or order tickets. Today's date is {today_iso}.

**IMPORTANT:** every reference in your output to "the trader," "the user," "the \
caller," "they," or "he" must mean {caller_name}. Do not name a different \
trader. If you write a `caption_summary` or `notes` field, name {caller_name} \
or use a pronoun — never insert a name from your training data.

TYPICAL POSTING FORMAT — a Robinhood notification card showing:
- Ticker + strike + Call/Put (e.g. "NVDA $150 Call")
- Month/day + Buy/Sell pill (e.g. "5/29 · Buy")
- Green +N% or red -N% gain pill on the right

These are NOTIFICATION cards (current P&L of an existing position), NOT order \
execution tickets. The poster does NOT post quantity, entry price, exit price, \
or spot price on the cards themselves. The "Buy" pill is the ORIGINAL order \
type, not the current action.

Extract a `price` value from whichever of these is visible (in this priority):
1. **Avg cost / "Average cost" / "Avg price"** on a position or portfolio \
   screen — this is the actual fill price the poster paid; always prefer it.
2. **Midpoint of bid and ask** ((bid + ask) / 2, rounded to 2 decimals) on a \
   stats / quote screen — fair proxy for an expected fill.
3. **Mark price** on a stats / quote screen, if bid/ask aren't both legible.
4. **Explicit price on an order ticket** — use it directly.
Skip (price=null) only if none of the above is visible. Notification cards \
(gain-pill only, no bid/ask/mark/avg) → price=null — the entry price isn't \
derivable from the gain-pill alone.

The poster sometimes also posts:
- A stats/quote screen (Bid/Ask/Mark/IV) when viewing an option to buy
- A tweet screenshot or chart (not a trade — flag as is_trade_screenshot=false)
{cap_block}

CAPTION → ACTION MAPPING — IMPORTANT ASYMMETRY:

False-positive opens (saying "open" when the poster was just viewing) are MUCH \
worse than false-negative opens (treating an ambiguous post as not-a-trade \
when they actually opened). A wrongly-detected open creates a phantom \
position in the log that the bot will reference forever; a missed open \
self-corrects when they post a notification card later. **When in doubt \
about an open, set `is_trade_screenshot=false` with a \
`what_it_appears_to_be` like "stats screen with hype caption — no execution \
evidence."** Closes are easier to verify (gain-pill + caption usually \
agree), so be more confident classifying closes.

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

AMBIGUOUS / HYPE captions on a stats_screen → `is_trade_screenshot=false` \
(bias toward not-a-trade, not open). Don't assume an open from excitement \
alone:
- "let's go", "fire", "🚀", "this one", "watch this", "👀", reaction \
  emojis like "🍆", short observations like "COO just left?"
- These could mean "I just bought" OR "look at this setup" — when on a \
  stats_screen (no execution evidence in the image), return \
  `is_trade_screenshot=false` with `what_it_appears_to_be="stats screen \
  with hype/reaction caption — no execution evidence"`. Do NOT use \
  action="viewing" — that label has been retired (it just produced \
  observation rows that polluted the trade log).
- ONLY classify as open if the caption is genuinely unambiguous (per the \
  STRONG OPEN list above).

NO CAPTION (decision tree depends on screenshot_type AND gain_pct):

A. **Notification card with visible POSITIVE gain (>+5%)**: action="close". \
   The poster doesn't silently post profit screens unless they just took the \
   win — the card itself is the flex/announcement. Treat as a took-profit \
   close. Add a note "no close caption — inferred from gain pill" so /ask \
   can qualify language slightly.

B. **Notification card with near-zero gain (-5% to +5%)** or no gain visible: \
   action="open". A fresh notification card without much gain has just been \
   entered — the entry alert, not a flex. They don't post notification cards \
   casually.

C. **Notification card with visible NEGATIVE gain (<-5%)**: action="unclear" \
   (lean toward "open" if you must pick). Posting a losing position silently \
   is unusual — could be a fresh open the trade went red on, could be a \
   stop-out without caption. Without more signal, don't claim a close.

D. **Stats screen for a SPECIFIC CONTRACT** (ticker + strike + expiry all \
   visible, "Buy [TICKER]" or "Sell [TICKER]" header, Bid/Ask/Mark/IV for \
   that ONE contract): action="open". Narrowing in on a specific contract \
   IS an entry signal — they don't post these for fun. The prior \
   conservative bias was wrong here: single-contract stats screens are \
   confirmed-or-imminent entries, not idle browsing.

   - Exception 1: if there's an explicit AMBIGUOUS-hype caption on a \
     stats screen ("🚀", "let's go", "this one"), that captioned-stats \
     case goes to `is_trade_screenshot=false` (the caption fired the \
     asymmetric-error rule). NOTE: "slam" is NOT ambiguous — it means \
     open and overrides this exception. Captionless single-contract \
     stats screens still default to "open".

E. **Pure option chain listing** (multiple strikes visible, no specific \
   contract selected) with no caption: `is_trade_screenshot=false`, \
   `what_it_appears_to_be="option chain browse — no contract selected"`. \
   Surveying the chain isn't an executed trade and doesn't belong in \
   the log.

F. **Order ticket / order-confirmation screen**: action="open". The screen \
   itself confirms execution.

EXPIRY YEAR INFERENCE: Robinhood notification cards show only "M/D" not the \
year. Use today's date ({today_iso}) to infer the year with this priority:

1. If the M/D expiry is on or after today's M/D → CURRENT YEAR.
2. If the M/D expiry is BEFORE today, but within the last 14 days → \
   CURRENT YEAR. They're posting about a contract that just expired or \
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
  "action": "open | add | trim | close" (no other values; ambiguous → is_trade_screenshot=false),
  "action_source": "caption | image | both",
  "gain_pct": number or null (the +/-N% pill, if shown),
  "price": number or null (priority: avg cost > bid/ask midpoint > mark > explicit ticket price; null on notification cards),
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
    caller_name: str = "the caller",
) -> dict | None:
    """Run Gemini on an analyst trade screenshot. Returns parsed JSON dict
    on success, None on any failure (logged). Failures are non-fatal —
    the watcher should continue processing other images.

    If `parent_caption` is set, it's prepended to the caption as reply-
    chain context. Lets a reply like 'out' on an earlier 'BTO MSFT 430C'
    post resolve to a close of that contract when the image alone is
    ambiguous.

    `caller_name` is the display name of the caller this screenshot came
    from (e.g. "Abe", "BK"). Passed into the prompt so the model knows
    whose post it's processing — without this, the model defaults to
    writing evidence text that says "Abe" regardless of source.
    """
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if parent_caption and parent_caption.strip():
        composed_caption = (
            f"[REPLY-PARENT (caller's earlier post):] {parent_caption.strip()[:500]}\n"
            f"[CURRENT CAPTION:] {caption}"
        )
    else:
        composed_caption = caption
    prompt = build_prompt(today_iso, composed_caption, caller_name=caller_name)
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
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.error(f"Gemini extraction returned non-JSON: {e} | text={text[:200]}")
        return None

    # Defensive: model sometimes returns a list when a portfolio
    # screenshot has multiple positions visible. Take the first item
    # (most-prominent position) — multi-position OCR is a future
    # enhancement, not a current pipeline feature.
    if isinstance(data, list):
        if not data:
            return None
        log.info(
            f"Gemini image extraction returned a list of {len(data)} items; "
            f"taking the first as the canonical position"
        )
        data = data[0]
    if not isinstance(data, dict):
        log.warning(f"Gemini image extraction returned non-dict: {type(data).__name__}")
        return None
    return data


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

**Expiry default — depends on action:**

- **OPEN / ADD** with no expiry in the caption → set `expiry` to today's
  date ({today_iso}) (the 0DTE default). Terse callers often omit expiry
  on intraday lottos because the contract is same-day; better to log the
  open at today's expiry than to drop it.

- **CLOSE / TRIM** with no expiry in the caption → set `expiry` to
  **null**. Do NOT default closes to today. The caller usually means
  "close the position I already have open" — the system will resolve
  which contract that is by matching the ticker + strike against their
  outstanding open positions. Defaulting a close to today incorrectly
  creates a phantom 0DTE close that doesn't match the earlier open.

- (no other actions exist — ambiguous captions → `is_trade_screenshot=false`)

If the caption DOES have an explicit M/D, use that with the year-inference
rule below regardless of action.

Caller shorthand to recognize (uppercase canonical):
- "rut" → RUT, "ndx" → NDX, "spx" → SPX, "spy" → SPY, "qqq" → QQQ
- "msft" → MSFT, "meta" → META, "pltr" → PLTR, "tsla" → TSLA
- "chyna" / "moar chyna" / "CHYYYNNAAA" → FXI (China large-cap ETF; common shorthand)
- "coin" → COIN, "mstr" → MSTR, "amd" → AMD, "nvda" → NVDA, "igv" → IGV

Strike syntax: a number directly followed by 'c' (call) or 'p' (put), e.g.
"94c" → strike 94 call, "29000c" → strike 29000 call. Decimal strikes allowed
("207.5c" → 207.5 call).

Expiry parsing: "M/D" or "MM/DD" or "MM-DD" formats — use today's date
({today_iso}) to infer the year. If the M/D is on/after today → current year.
If 1-14 days ago → current year (just expired or about to). If more than
14 days ago → next year.

**For missing expiry, see "Expiry default — depends on action" rule above.**

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
  "action": "open" | "add" | "trim" | "close",
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
    caller_name: str = "the caller",
) -> dict | None:
    """Caption-only extraction path for callers who post pure text
    alerts without a screenshot. Requires ticker + strike;
    expiry defaults to today (0DTE) if not present.

    If `parent_caption` is set, it's prepended as reply-chain context —
    e.g. caption='closed' + parent_caption='BTO MSFT 430C 5/20 @3.65'
    resolves to a CLOSE of that exact contract.

    `caller_name` is the display name of the poster (e.g. "BK").
    Used in the prompt so the model's evidence and notes name the
    actual caller rather than defaulting to "Abe."

    Returns parsed JSON dict on success, None on any failure (logged).
    Failures are non-fatal — the watcher should continue.
    """
    if not caption or not caption.strip():
        return None
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Compose the caption block — when this is a reply, the parent
    # message provides the position the reply is acting on. Prefix
    # with caller identity so the model attributes correctly.
    composed_pieces = [
        f"[POSTER: {caller_name}]"
    ]
    if parent_caption and parent_caption.strip():
        composed_pieces.append(
            f"[REPLY-PARENT ({caller_name}'s earlier post — use for ticker/strike/expiry "
            f"if the new message is sparse like 'closed' / 'sold @X'):]\n"
            f"{parent_caption.strip()[:500]}"
        )
        composed_pieces.append(
            f"[CURRENT MESSAGE (the action — apply action verb here to the "
            f"contract from the parent):]\n"
            f"{caption.strip()[:1000]}"
        )
    else:
        composed_pieces.append(caption.strip()[:1000])
    composed = "\n\n".join(composed_pieces)
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

    # Defensive: model sometimes returns a list when a single caption
    # mentions multiple positions ("Pltr weeklies and 5/29"). Take the
    # first item — multi-position-per-message is a future enhancement.
    if isinstance(data, list):
        if not data:
            return None
        log.info(
            f"Caption extraction returned a list of {len(data)} items; "
            f"taking the first"
        )
        data = data[0]
    if not isinstance(data, dict):
        log.warning(f"Caption extraction returned non-dict: {type(data).__name__}")
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
        # Action-aware expiry default — opens get 0DTE fallback, closes
        # stay null so the watcher can resolve them from the caller's
        # outstanding open positions (matching by ticker + strike).
        # Defaulting a close to today creates a phantom 0DTE close that
        # doesn't match the earlier open — this was the HOOD 5/22 bug.
        if not data.get("expiry"):
            action = (data.get("action") or "").lower()
            if action in ("open", "add"):
                data["expiry"] = today_iso
                data["notes"] = (
                    (data.get("notes") or "")
                    + " (expiry defaulted to today — 0DTE open)"
                ).strip()
            # close / trim → leave null; the watcher will resolve them
            # by matching outstanding open positions on ticker+strike.
            # Notes for forensic visibility.
            elif action in ("close", "trim"):
                data["notes"] = (
                    (data.get("notes") or "")
                    + " (expiry left null — watcher will match to open position)"
                ).strip()
    return data


# --- Unified text + vision + cached-OCR classifier (2026-06-02) -----------
#
# extract_trade_from_message accepts the FULL signal set for an arbitrary
# Discord message: text content, raw image bytes (live ingestion path), and
# cached OCR text from a prior eager-OCR run (backfill path — Discord CDN
# URLs expire ~24h so we can't re-fetch original images for old messages).
#
# Unlike extract_trade_from_image (above), this function is NOT bound to
# the analyst_callers registry. It runs for every message in the eager-OCR
# channel set (chat_eager_ocr_channels) and credits text-only trade
# narratives that today's image-only OCR pipeline misses entirely.
#
# Returns a fuzzy JSON dict on success — accept whatever fields Gemini
# could extract, as long as is_trade=true, confidence >= threshold, and
# a ticker is present. Below threshold or missing ticker → returns None.

# Confidence threshold for accepting a classification. Below this, skip
# the row. Tunable via env var if QC shows we're missing too many real
# trades (lower) or hallucinating too many fake ones (raise).
_MESSAGE_CLASSIFIER_MIN_CONFIDENCE = 0.6


_CLASSIFIER_PROMPT = """\
You read a Discord message from a trader's alert channel. The message
may be text, image (screenshot), cached OCR text from a prior
screenshot extraction, or any combination. Your job: decide if the
message describes an entry, add, close, or trim of an options or
crypto trade — and extract whatever fields are clearly visible.

Return STRICT JSON matching this schema:
{{
  "is_trade": bool,
  "action": "open" | "add" | "close" | "trim" | null,
  "ticker": str | null,
  "contract_type": "call" | "put" | "spot" | "future" | null,
  "strike": float | null,
  "expiry": str | null,            // YYYY-MM-DD or null
  "price": float | null,           // entry price or close price
  "gain_pct": float | null,        // for closes, % gain or loss
  "confidence": float              // 0.0–1.0
}}

Rules:
- If the message is opinion, news, a meme, a reply, or generic
  bullish/bearish chatter (e.g. "I'm long tech", "AI is overheated",
  "this stock looks good"), return {{"is_trade": false, "confidence": <your read>}}.
- A trade requires explicit evidence: a ticker AND an action verb
  ("opened", "buying", "closed", "sold", "trimmed", "exit", or
  visible from a screenshot's order ticket / P&L pane).
- Accept partial structure. "btc long at 73,906" is a valid trade
  (ticker BTC, action open, price 73906, contract_type spot/future as
  context allows) — strike + expiry can be null.
- "3x from entry" means gain_pct: 200.0. "+50%" means gain_pct: 50.0.
  "-30%" means gain_pct: -30.0.
- confidence reflects how clearly the message conveys a trade — not
  whether you think the trade is good.

Message author: {author}
Channel: {channel}

Text content (user's typed message):
{text}

Image OCR text (prior Gemini extraction from the screenshot, if any —
may be partial or empty; treat as a hint, not gospel):
{cached_ocr}

[Plus any image attachments provided directly in this prompt.]
"""


async def _call_gemini_classifier(prompt_parts: list, model: str):
    """Thin wrapper around the Gemini SDK call so tests can stub it
    without intercepting the whole genai client. Returns the raw
    response object — caller parses `.text`."""
    client = _get_client()
    response = await client.aio.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=prompt_parts)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=400,
            temperature=0.1,
        ),
    )
    return response


async def extract_trade_from_message(
    text: str,
    image_bytes_list: list[bytes],
    *,
    author_username: str,
    channel_name: str,
    cached_ocr_text: str = "",
) -> dict | None:
    """Classify a Discord message as a trade entry/close — text,
    image, AND/OR cached prior OCR. Returns None if the model says it's
    not a trade, confidence is too low, or no ticker was extractable.

    Otherwise returns a dict matching the row-write schema, plus
    extraction_source ∈ {'text', 'image', 'mixed'}.

    `cached_ocr_text` is used by the backfill path: the eager-OCR
    pipeline has already extracted text from screenshots into
    chat_messages.image_ocr_text. Discord CDN URLs expire ~24h so we
    can't re-fetch the original bytes for old messages. Passing the
    cached OCR text lets the classifier see what the screenshot
    contained without re-fetching. Live ingestion passes
    image_bytes_list instead (real-time has the bytes); backfill passes
    cached_ocr_text.

    extraction_source is tagged based on which signals contributed:
      - text only (no image bytes, no cached_ocr): 'text'
      - image bytes only OR cached_ocr only: 'image'
      - text + (image bytes OR cached_ocr): 'mixed'
    """
    text = (text or "").strip()
    cached_ocr_text = (cached_ocr_text or "").strip()
    has_text = bool(text)
    has_images = bool(image_bytes_list)
    has_cached_ocr = bool(cached_ocr_text)
    if not has_text and not has_images and not has_cached_ocr:
        return None  # nothing to classify

    # Build the prompt parts: text + each image as bytes.
    text_section = text if has_text else "(none)"
    ocr_section = cached_ocr_text if has_cached_ocr else "(none)"
    prompt_text = _CLASSIFIER_PROMPT.format(
        author=author_username, channel=channel_name,
        text=text_section,
        cached_ocr=ocr_section,
    )
    parts = [types.Part.from_text(text=prompt_text)]
    for img_bytes in image_bytes_list:
        parts.append(
            types.Part.from_bytes(
                data=img_bytes, mime_type="image/png",
            )
        )

    model = settings.gemini_model
    try:
        response = await _call_gemini_classifier(parts, model)
    except Exception as e:
        # Soft fail — don't raise into the caller's task chain
        log.warning(
            f"extract_trade_from_message: Gemini call failed for "
            f"{author_username} in {channel_name}: {type(e).__name__}: {e}"
        )
        return None

    try:
        payload = json.loads((response.text or "").strip() or "{}")
    except Exception as e:
        log.warning(
            f"extract_trade_from_message: malformed JSON "
            f"({type(e).__name__}: {e}); response.text={response.text!r}"
        )
        return None

    # Validation: only accept if model says it's a trade, confidence is
    # high enough, and there's a ticker (unstitchable without one).
    if not payload.get("is_trade"):
        return None
    confidence = payload.get("confidence") or 0
    if confidence < _MESSAGE_CLASSIFIER_MIN_CONFIDENCE:
        return None
    if not payload.get("ticker"):
        return None

    # Tag extraction_source based on which modalities contributed.
    # cached OCR is treated as image evidence (it represents an image,
    # just one we couldn't re-fetch live bytes for).
    image_present = has_images or has_cached_ocr
    if has_text and image_present:
        extraction_source = "mixed"
    elif image_present:
        extraction_source = "image"
    else:
        extraction_source = "text"
    payload["extraction_source"] = extraction_source
    return payload
